#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <structmember.h>
#include <descrobject.h>
#include <internal/pycore_frame.h>

#if PY_MAJOR_VERSION != 3 || PY_MINOR_VERSION != 11
#error "The frame identity adapter requires CPython 3.11."
#endif

static PyObject *frame_function(PyObject *self, PyObject *value) {
    if (!PyFrame_Check(value)) {
        PyErr_SetString(PyExc_TypeError, "expected a frame");
        return NULL;
    }
    PyFrameObject *frame = (PyFrameObject *)value;
    if (!frame->f_frame || !frame->f_frame->f_func) Py_RETURN_NONE;
    return Py_NewRef((PyObject *)frame->f_frame->f_func);
}

static PyObject *leaf_metadata_context(void);

/* Collision-free observed identities of a flat builtin sequence. No payloads,
 * Python callbacks, object retention or assumptions about mutable descendants. */
static PyObject *flat_sequence_signature(PyObject *self,PyObject *value) {
    if (!PyList_CheckExact(value) && !PyTuple_CheckExact(value)) Py_RETURN_NONE;
    Py_ssize_t count=PyList_CheckExact(value) ? PyList_GET_SIZE(value) : PyTuple_GET_SIZE(value);
    if (count<32) Py_RETURN_NONE;
    if (count>PY_SSIZE_T_MAX/(2*sizeof(uintptr_t))) return PyErr_NoMemory();
    PyObject *result=PyBytes_FromStringAndSize(NULL,count*2*sizeof(uintptr_t));
    if (!result) return NULL;
    uintptr_t *out=(uintptr_t *)PyBytes_AS_STRING(result);
    for (Py_ssize_t i=0;i<count;i++) {
        PyObject *item=PyList_CheckExact(value) ? PyList_GET_ITEM(value,i) : PyTuple_GET_ITEM(value,i);
        if (!(PyUnicode_CheckExact(item) || PyBytes_CheckExact(item) || PyLong_CheckExact(item) ||
              PyFloat_CheckExact(item) || PyComplex_CheckExact(item) || PyBool_Check(item) || item==Py_None)) {
            Py_DECREF(result);Py_RETURN_NONE;
        }
        out[2*i]=(uintptr_t)item;out[2*i+1]=(uintptr_t)Py_TYPE(item);
    }
    PyObject *context=leaf_metadata_context();
    if (!context) {Py_DECREF(result);return NULL;}
    if (context==Py_None) {Py_DECREF(result);return context;}
    PyObject *complete=PyTuple_Pack(2,context,result);Py_DECREF(context);Py_DECREF(result);return complete;
}


/* Exact validation of a previously observed object graph. Unsupported storage
 * falls back to Python observation. Only identities, binding names and mutable
 * namespace versions participate; scalar payloads are outside the contract. */
/* Compare already validated identity vectors, without touching workload objects. */
static PyObject *flat_sequence_changes(PyObject *self,PyObject *args) {
    PyObject *before,*after;
    if (!PyArg_ParseTuple(args,"O!O!",&PyBytes_Type,&before,&PyBytes_Type,&after)) return NULL;
    Py_ssize_t stride=2*sizeof(uintptr_t),old_size=PyBytes_GET_SIZE(before),new_size=PyBytes_GET_SIZE(after);
    if (old_size%stride || new_size%stride) {PyErr_SetString(PyExc_ValueError,"invalid flat identity vector");return NULL;}
    PyObject *rows=PyList_New(0);if (!rows) return NULL;
    for (Py_ssize_t i=0;i<new_size/stride;i++) {
        if (i<old_size/stride && !memcmp(PyBytes_AS_STRING(before)+i*stride,PyBytes_AS_STRING(after)+i*stride,stride)) continue;
        PyObject *index=PyLong_FromSsize_t(i);
        if (!index || PyList_Append(rows,index)<0) {Py_XDECREF(index);Py_DECREF(rows);return NULL;}
        Py_DECREF(index);
    }
    return rows;
}

typedef struct TypeContextCache TypeContextCache;
static PyObject *cached_type_metadata_context(TypeContextCache *,PyTypeObject *);
static PyObject *cached_base_namespace(TypeContextCache *,PyTypeObject *);

/* Membership only; never owns or dereferences workload addresses. The queue
 * retains the same references as before. No iteration order enters encoding. */
typedef struct IdentitySet {uintptr_t *items;size_t size,capacity;} IdentitySet;
static size_t identity_hash(uintptr_t value) {
    value^=value>>4;value*=((uintptr_t)0x9e3779b97f4a7c15ULL);value^=value>>(sizeof(uintptr_t)*4);return (size_t)value;
}
static int identity_seen(IdentitySet *set,uintptr_t value) {
    if (!set->capacity || set->size>=set->capacity/2) {
        if (set->capacity>(size_t)PY_SSIZE_T_MAX/(2*sizeof(uintptr_t))) {PyErr_NoMemory();return -1;}
        size_t capacity=set->capacity ? set->capacity*2 : 128;
        uintptr_t *items=PyMem_Calloc(capacity,sizeof(uintptr_t));if (!items) {PyErr_NoMemory();return -1;}
        for (size_t i=0;i<set->capacity;i++) if (set->items[i]) {
            size_t slot=identity_hash(set->items[i])&(capacity-1);
            while (items[slot]) slot=(slot+1)&(capacity-1);items[slot]=set->items[i];
        }
        PyMem_Free(set->items);set->items=items;set->capacity=capacity;
    }
    size_t slot=identity_hash(value)&(set->capacity-1);
    while (set->items[slot]) {if (set->items[slot]==value) return 1;slot=(slot+1)&(set->capacity-1);}
    set->items[slot]=value;set->size++;return 0;
}
static void identity_clear(IdentitySet *set) {PyMem_Free(set->items);set->items=NULL;set->size=set->capacity=0;}

typedef struct GraphSignature {
    char *bytes;Py_ssize_t size,capacity;
    PyObject **queue;Py_ssize_t used,queued,queue_capacity,budget,references;
    IdentitySet seen,types,base_namespaces;
    PyObject *boundaries,*field_plans;
    int supported;
    TypeContextCache *type_cache;
} GraphSignature;
static getter standard_dict_getters[4];
static int standard_dict_getter_count;
#if defined(__GNUC__) || defined(__clang__)
#define SIGNATURE_INLINE static inline __attribute__((always_inline))
#else
#define SIGNATURE_INLINE static inline
#endif
SIGNATURE_INLINE int signature_write(GraphSignature *g,const void *data,Py_ssize_t size) {
    if (size<0 || size>PY_SSIZE_T_MAX-g->size) {PyErr_NoMemory();return -1;}
    if (g->size+size>g->capacity) {
        Py_ssize_t capacity=(g->size+size)*2+256;
        void *next=PyMem_Realloc(g->bytes,capacity);
        if (!next) {PyErr_NoMemory();return -1;}
        g->bytes=next;g->capacity=capacity;
    }
    memcpy(g->bytes+g->size,data,size);g->size+=size;return 0;
}
SIGNATURE_INLINE int signature_number(GraphSignature *g,uintptr_t value) {return signature_write(g,&value,sizeof(value));}
static int signature_text(GraphSignature *g,PyObject *value) {
    if (!value || !PyUnicode_CheckExact(value)) {g->supported=0;return 0;}
    Py_ssize_t length;const char *text=PyUnicode_AsUTF8AndSize(value,&length);
    if (!text || signature_number(g,length)<0) return -1;
    return signature_write(g,text,length);
}
static int signature_push(GraphSignature *g,PyObject *value) {
    if (!value) return signature_number(g,0);
    if (signature_number(g,(uintptr_t)value)<0 || signature_number(g,(uintptr_t)Py_TYPE(value))<0) return -1;
    if (++g->references>g->budget) {g->supported=0;return 0;}
    if (PyUnicode_CheckExact(value) || PyBytes_CheckExact(value) || PyLong_CheckExact(value) ||
        PyFloat_CheckExact(value) || PyComplex_CheckExact(value) || PyBool_Check(value) ||
        value==Py_None || Py_IS_TYPE(value,&PyCode_Type)) return 0;
    if (g->queued==g->queue_capacity) {
        Py_ssize_t capacity=g->queue_capacity*2+128;
        void *next=PyMem_Realloc(g->queue,capacity*sizeof(PyObject *));
        if (!next) {PyErr_NoMemory();return -1;}
        g->queue=next;g->queue_capacity=capacity;
    }
    g->queue[g->queued++]=Py_NewRef(value);return 0;
}
static int signature_owner_file(GraphSignature *g,PyObject *name) {
    PyObject *module=PyDict_GetItem(PyImport_GetModuleDict(),name);
    int present=module && PyModule_CheckExact(module);
    if (signature_number(g,present)<0) return -1;
    if (!present) return 0;
    PyObject *file=PyDict_GetItemString(PyModule_GetDict(module),"__file__");
    if (!file || file==Py_None) return signature_number(g,0);
    if (signature_number(g,1)<0) return -1;
    return signature_text(g,file);
}
static PyObject *leaf_metadata_context(void) {
    GraphSignature g={0};g.supported=1;
    PyObject *name=PyUnicode_FromString("builtins"),*result=NULL;
    if (!name) return NULL;
    if (signature_owner_file(&g,name)<0 || signature_owner_file(&g,Py_None)<0) goto done;
    result=g.supported ? PyBytes_FromStringAndSize(g.bytes,g.size) : Py_NewRef(Py_None);
done:
    Py_DECREF(name);PyMem_Free(g.bytes);return result;
}
static int signature_namespace(GraphSignature *g,PyObject *namespace) {
    if (!PyDict_CheckExact(namespace)) {g->supported=0;return 0;}
    return signature_number(g,((PyDictObject *)namespace)->ma_version_tag);
}
static PyObject *exact_class_function(GraphSignature *g,PyObject *member) {
    if (Py_IS_TYPE(member,&PyFunction_Type)) return member;
    if (!Py_IS_TYPE(member,&PyStaticMethod_Type) && !Py_IS_TYPE(member,&PyClassMethod_Type)) return NULL;
    /* Read only the builtin wrapper's stored function. Calling __get__ or a
     * user-defined __getattribute__ here would change the observed workload. */
    PyObject *descriptor=PyDict_GetItemString(Py_TYPE(member)->tp_dict,"__func__");
    if (!descriptor || !Py_IS_TYPE(descriptor,&PyMemberDescr_Type) ||
        !PyType_IsSubtype(Py_TYPE(member),PyDescr_TYPE(descriptor))) {g->supported=0;return NULL;}
    PyMemberDef *field=((PyMemberDescrObject *)descriptor)->d_member;
    if (field->type!=T_OBJECT && field->type!=T_OBJECT_EX) {g->supported=0;return NULL;}
    PyObject *function=*(PyObject **)((char *)member+field->offset);
    return function && Py_IS_TYPE(function,&PyFunction_Type) ? function : NULL;
}
static int signature_class_functions(GraphSignature *g,PyObject *namespace) {
    uintptr_t count=0;Py_ssize_t pos=0,count_offset=g->size;PyObject *name,*member;
    if (signature_number(g,0)<0) return -1;
    while (g->supported && PyDict_Next(namespace,&pos,&name,&member)) {
        PyObject *function=exact_class_function(g,member);
        if (!function) continue;
        count++;
        PyCodeObject *code=(PyCodeObject *)((PyFunctionObject *)function)->func_code;
        /* Replacing function.__code__ leaves the class dictionary version
         * unchanged. Source coordinates also prevent a recycled code address
         * from making a different source definition look unchanged. */
        if (signature_text(g,name)<0 || signature_number(g,(uintptr_t)function)<0 ||
            signature_number(g,(uintptr_t)code)<0 || signature_text(g,code->co_filename)<0 ||
            signature_number(g,code->co_firstlineno)<0 || signature_text(g,code->co_qualname)<0) return -1;
    }
    memcpy(g->bytes+count_offset,&count,sizeof(count));
    return 0;
}
static int signature_base_namespace(GraphSignature *g,PyTypeObject *base) {
    /* Shared MRO bases occur in many concrete types. Encode the exact base
     * identity and a reference after its first complete namespace description.
     * This is per observation only: every later snapshot rereads all versions
     * and function source coordinates, including in-place __code__ changes. */
    if (signature_number(g,(uintptr_t)base)<0) return -1;
    int seen=identity_seen(&g->base_namespaces,(uintptr_t)base);
    if (seen<0 || signature_number(g,!seen)<0) return -1;
    if (seen) return 0;
    if (g->type_cache) {
        PyObject *context=cached_base_namespace(g->type_cache,base);
        if (!context) return -1;
        if (context==Py_None) {Py_DECREF(context);g->supported=0;return 0;}
        int status=signature_write(g,PyBytes_AS_STRING(context),PyBytes_GET_SIZE(context));
        Py_DECREF(context);return status;
    }
    if (signature_namespace(g,base->tp_dict)<0) return -1;
    return g->supported ? signature_class_functions(g,base->tp_dict) : 0;
}
static int signature_type(GraphSignature *g,PyTypeObject *type) {
    int seen=identity_seen(&g->types,(uintptr_t)type);
    if (seen) return seen<0 ? -1 : 0;
    if (signature_number(g,(uintptr_t)type->tp_mro)<0) return -1;
    if (type->tp_flags & Py_TPFLAGS_HEAPTYPE) {
        if (signature_text(g,((PyHeapTypeObject *)type)->ht_qualname)<0 ||
            signature_text(g,((PyHeapTypeObject *)type)->ht_name)<0) return -1;
    }
    if (!type->tp_mro || !PyTuple_CheckExact(type->tp_mro)) {g->supported=0;return 0;}
    for (Py_ssize_t i=0;i<PyTuple_GET_SIZE(type->tp_mro);i++) {
        PyTypeObject *base=(PyTypeObject *)PyTuple_GET_ITEM(type->tp_mro,i);
        if (signature_base_namespace(g,base)<0) return -1;
        if (!g->supported) return 0;
    }
    if (type->tp_flags & Py_TPFLAGS_HEAPTYPE) {
        PyObject *name=PyDict_GetItemString(type->tp_dict,"__module__");
        if (!name || name==Py_None) return signature_owner_file(g,Py_None);
        if (!PyUnicode_CheckExact(name)) {g->supported=0;return 0;}
        if (signature_owner_file(g,name)<0) return -1;
    } else {
        const char *dot=strrchr(type->tp_name,'.');
        PyObject *name=dot ? PyUnicode_FromStringAndSize(type->tp_name,dot-type->tp_name) : PyUnicode_FromString("builtins");
        if (!name) return -1;
        int status=signature_owner_file(g,name);Py_DECREF(name);
        if (status<0) return -1;
    }
    return 0;
}
static PyTypeObject *member_projection_type(PyObject *value,PyMemberDef *member) {
    switch (member->type) {
    case T_BOOL:return &PyBool_Type;
    case T_FLOAT:case T_DOUBLE:return &PyFloat_Type;
    case T_CHAR:case T_STRING_INPLACE:return &PyUnicode_Type;
    case T_STRING:return *(char **)((char *)value+member->offset) ? &PyUnicode_Type : Py_TYPE(Py_None);
    case T_NONE:return Py_TYPE(Py_None);
    case T_SHORT:case T_INT:case T_LONG:case T_BYTE:case T_UBYTE:case T_USHORT:
    case T_UINT:case T_ULONG:case T_LONGLONG:case T_ULONGLONG:case T_PYSSIZET:return &PyLong_Type;
    default:return NULL;
    }
}
static PyObject *uncached_type_metadata_context(PyObject *value) {
    if (!PyType_Check(value)) {PyErr_SetString(PyExc_TypeError,"type required");return NULL;}
    GraphSignature g={0};g.supported=1;
    PyObject *result=NULL;
    if (signature_type(&g,(PyTypeObject *)value)>=0 && signature_owner_file(&g,Py_None)>=0)
        result=g.supported ? PyBytes_FromStringAndSize(g.bytes,g.size) : Py_NewRef(Py_None);
    identity_clear(&g.types);identity_clear(&g.base_namespaces);PyMem_Free(g.bytes);return result;
}
#include "type_context_cache.h"

static PyObject *member_storage(PyObject *self,PyObject *args) {
    PyObject *value,*descriptor;
    if (!PyArg_ParseTuple(args,"OO",&value,&descriptor)) return NULL;
    if (!Py_IS_TYPE(descriptor,&PyMemberDescr_Type)) {PyErr_SetString(PyExc_TypeError,"member storage descriptor required");return NULL;}
    PyMemberDescrObject *field=(PyMemberDescrObject *)descriptor;
    if (!PyType_IsSubtype(Py_TYPE(value),PyDescr_TYPE(descriptor))) {PyErr_SetString(PyExc_TypeError,"member owner mismatch");return NULL;}
    PyTypeObject *projection=member_projection_type(value,field->d_member);
    if (!projection) Py_RETURN_NONE;
    return Py_BuildValue("niO",field->d_member->offset,field->d_member->type,(PyObject *)projection);
}
static int signature_field_plan_valid(PyObject *entry,PyTypeObject *type) {
    PyObject *mro=PyTuple_GET_ITEM(entry,0),*versions=PyTuple_GET_ITEM(entry,1);
    if (type->tp_mro!=mro) return 0;
    for (Py_ssize_t i=0;i<PyTuple_GET_SIZE(mro);i++) {
        PyTypeObject *base=(PyTypeObject *)PyTuple_GET_ITEM(mro,i);
        if (!PyDict_CheckExact(base->tp_dict) ||
            ((PyDictObject *)base->tp_dict)->ma_version_tag!=PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(versions,i))) return 0;
    }
    return 1;
}
/* Persistent plans store only names, indices and weak guards. Descriptors and
 * MRO tuples would retain workload classes, so bind current descriptors only
 * inside the current signature call, after validating every namespace. */
static int weak_field_plan_valid(PyObject *entry,PyTypeObject *type) {
    if (PyWeakref_GetObject(PyTuple_GET_ITEM(entry,0))!=(PyObject *)type ||
        (uintptr_t)type->tp_mro!=PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(entry,1))) return 0;
    PyObject *bases=PyTuple_GET_ITEM(entry,2);
    if (!type->tp_mro || !PyTuple_CheckExact(type->tp_mro) || PyTuple_GET_SIZE(type->tp_mro)!=PyTuple_GET_SIZE(bases)) return 0;
    for (Py_ssize_t i=0;i<PyTuple_GET_SIZE(bases);i++) {
        PyTypeObject *base=(PyTypeObject *)PyTuple_GET_ITEM(type->tp_mro,i);PyObject *row=PyTuple_GET_ITEM(bases,i);
        if (PyWeakref_GetObject(PyTuple_GET_ITEM(row,0))!=(PyObject *)base || !PyDict_CheckExact(base->tp_dict) ||
            ((PyDictObject *)base->tp_dict)->ma_version_tag!=PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(row,1))) return 0;
    }
    return 1;
}
static PyObject *weak_field_plan_entry(PyTypeObject *type,PyObject *mro,PyObject *versions,PyObject *specs) {
    PyObject *weak=PyWeakref_NewRef((PyObject *)type,NULL),*bases=NULL,*result=NULL;
    if (!weak) return NULL;
    bases=PyTuple_New(PyTuple_GET_SIZE(mro));if (!bases) goto done;
    for (Py_ssize_t i=0;i<PyTuple_GET_SIZE(mro);i++) {
        PyObject *base=PyTuple_GET_ITEM(mro,i),*ref=PyWeakref_NewRef(base,NULL),*row=NULL;
        if (ref) row=PyTuple_Pack(2,ref,PyTuple_GET_ITEM(versions,i));
        Py_XDECREF(ref);if (!row) goto done;PyTuple_SET_ITEM(bases,i,row);
    }
    result=Py_BuildValue("(OKOO)",weak,(unsigned long long)(uintptr_t)mro,bases,specs);
done:Py_DECREF(weak);Py_XDECREF(bases);return result;
}
static PyObject *bind_weak_field_plan(PyObject *entry,PyTypeObject *type) {
    PyObject *specs=PyTuple_GET_ITEM(entry,3),*result=PyTuple_New(PyList_GET_SIZE(specs));if (!result) return NULL;
    for (Py_ssize_t i=0;i<PyList_GET_SIZE(specs);i++) {
        PyObject *spec=PyList_GET_ITEM(specs,i),*name=PyTuple_GET_ITEM(spec,1);
        Py_ssize_t index=PyLong_AsSsize_t(PyTuple_GET_ITEM(spec,0));
        if (index<0 || !type->tp_mro || index>=PyTuple_GET_SIZE(type->tp_mro)) {Py_DECREF(result);return NULL;}
        PyTypeObject *base=(PyTypeObject *)PyTuple_GET_ITEM(type->tp_mro,index);
        PyObject *descriptor=PyDict_GetItemWithError(base->tp_dict,name),*member=NULL;
        if (descriptor) member=PyTuple_Pack(2,name,descriptor);
        if (!member) {Py_DECREF(result);return NULL;}PyTuple_SET_ITEM(result,i,member);
    }
    return result;
}
static PyObject *signature_field_plan(GraphSignature *g,PyTypeObject *type) {
    /* A plan belongs only to this graph_signature call. It stores descriptors,
     * never instance values: all real dict/slot references are still read for
     * every instance below. Integer keys avoid metaclass __hash__ callbacks. */
    PyObject *key=PyLong_FromVoidPtr(type),*names=NULL,*members=NULL,*plan=NULL,*mro=NULL,*versions=NULL,*entry=NULL,*specs=NULL;
    if (!key) return NULL;
    PyObject *cached=PyDict_GetItem(g->field_plans,key);
    if (cached && signature_field_plan_valid(cached,type)) {Py_DECREF(key);return Py_NewRef(PyTuple_GET_ITEM(cached,2));}
    if (g->type_cache) {
        PyObject *persistent=PyDict_GetItem(g->type_cache->field_entries,key);
        if (persistent && weak_field_plan_valid(persistent,type)) {
            /* Keep the entry alive while allocations may trigger finalizers. */
            Py_INCREF(persistent);plan=bind_weak_field_plan(persistent,type);
            if (!plan) {Py_DECREF(persistent);if (!PyErr_Occurred()) g->supported=0;goto done;}
            if (!weak_field_plan_valid(persistent,type)) {Py_DECREF(persistent);g->supported=0;Py_CLEAR(plan);goto done;}
            mro=Py_NewRef(type->tp_mro);versions=PyTuple_New(PyTuple_GET_SIZE(mro));
            if (!versions) {Py_DECREF(persistent);Py_CLEAR(plan);goto done;}
            PyObject *bases=PyTuple_GET_ITEM(persistent,2);
            for (Py_ssize_t i=0;i<PyTuple_GET_SIZE(mro);i++) PyTuple_SET_ITEM(versions,i,Py_NewRef(PyTuple_GET_ITEM(PyTuple_GET_ITEM(bases,i),1)));
            entry=PyTuple_Pack(3,mro,versions,plan);
            if (!entry || PyDict_SetItem(g->field_plans,key,entry)<0) {Py_DECREF(persistent);Py_CLEAR(plan);goto done;}
            if (!weak_field_plan_valid(persistent,type) || !signature_field_plan_valid(entry,type)) {g->supported=0;Py_CLEAR(plan);}
            else g->type_cache->field_hits++;
            Py_DECREF(persistent);goto done;
        }
        if (persistent) g->type_cache->field_invalidations++;
        g->type_cache->field_misses++;g->type_cache->field_scans++;
    }
    if (!type->tp_mro || !PyTuple_CheckExact(type->tp_mro)) {g->supported=0;goto done;}
    mro=Py_NewRef(type->tp_mro);versions=PyTuple_New(PyTuple_GET_SIZE(mro));
    if (!versions) goto done;
    for (Py_ssize_t i=0;i<PyTuple_GET_SIZE(mro);i++) {
        PyTypeObject *base=(PyTypeObject *)PyTuple_GET_ITEM(mro,i);
        if (!PyDict_CheckExact(base->tp_dict)) {g->supported=0;goto done;}
        PyObject *version=PyLong_FromUnsignedLongLong(((PyDictObject *)base->tp_dict)->ma_version_tag);
        if (!version) goto done;
        PyTuple_SET_ITEM(versions,i,version);
    }
    names=PySet_New(NULL);members=PyList_New(0);specs=PyList_New(0);
    if (!names || !members || !specs) goto done;
    for (Py_ssize_t i=0;g->supported && i<PyTuple_GET_SIZE(mro);i++) {
        PyTypeObject *base=(PyTypeObject *)PyTuple_GET_ITEM(mro,i);
        Py_ssize_t pos=0;PyObject *name,*descriptor;
        while (g->supported && PyDict_Next(base->tp_dict,&pos,&name,&descriptor)) {
            if (!PyUnicode_CheckExact(name)) {g->supported=0;break;}
            int known=PySet_Contains(names,name);
            if (known<0 || (!known && PySet_Add(names,name)<0)) goto done;
            if (known) continue;
            int selected=0;
            if (Py_IS_TYPE(descriptor,&PyGetSetDescr_Type) && PyUnicode_CompareWithASCIIString(name,"__dict__")==0) {
                if (!PyType_IsSubtype(type,PyDescr_TYPE(descriptor))) {g->supported=0;break;}
                PyGetSetDescrObject *getset=(PyGetSetDescrObject *)descriptor;
                int recognized=getset->d_getset->get==PyObject_GenericGetDict;
                for (int j=0;j<standard_dict_getter_count;j++)
                    if (getset->d_getset->get==standard_dict_getters[j]) recognized=1;
                if (!recognized) {g->supported=0;break;}
                selected=1;
            } else if (Py_IS_TYPE(descriptor,&PyMemberDescr_Type)) {
                if (!PyType_IsSubtype(type,PyDescr_TYPE(descriptor))) {g->supported=0;break;}
                selected=1;
            }
            if (selected) {
                PyObject *member=PyTuple_Pack(2,name,descriptor);
                if (!member) goto done;
                PyObject *spec=Py_BuildValue("(nO)",i,name);
                int status=spec ? PyList_Append(members,member) : -1;
                if (!status) status=PyList_Append(specs,spec);
                Py_XDECREF(spec);Py_DECREF(member);
                if (status<0) goto done;
            }
        }
    }
    if (g->supported) {
        plan=PyList_AsTuple(members);
        if (plan) entry=PyTuple_Pack(3,mro,versions,plan);
        if (!entry || PyDict_SetItem(g->field_plans,key,entry)<0) Py_CLEAR(plan);
        else if (!signature_field_plan_valid(entry,type)) {g->supported=0;Py_CLEAR(plan);}
        else if (g->type_cache) {
            PyObject *persistent=weak_field_plan_entry(type,mro,versions,specs);
            if (!persistent) Py_CLEAR(plan);
            else {
                if (weak_field_plan_valid(persistent,type)) {
                    if (PyDict_SetItem(g->type_cache->field_entries,key,persistent)<0) Py_CLEAR(plan);
                    if (PyDict_Size(g->type_cache->field_entries)>4096) {
                        Py_ssize_t p=0;PyObject *old,*row;
                        if (PyDict_Next(g->type_cache->field_entries,&p,&old,&row) && PyDict_DelItem(g->type_cache->field_entries,old)<0) Py_CLEAR(plan);
                    }
                } else {g->supported=0;Py_CLEAR(plan);}
                Py_DECREF(persistent);
            }
        }
    }
done:
    Py_DECREF(key);Py_XDECREF(names);Py_XDECREF(members);Py_XDECREF(mro);Py_XDECREF(versions);Py_XDECREF(entry);Py_XDECREF(specs);return plan;
}
static int signature_fields(GraphSignature *g,PyObject *value) {
    PyTypeObject *type=Py_TYPE(value);PyObject *plan=signature_field_plan(g,type);
    if (!plan) return PyErr_Occurred() ? -1 : 0;
    int result=0;
    for (Py_ssize_t i=0;g->supported && !result && i<PyTuple_GET_SIZE(plan);i++) {
        PyObject *member=PyTuple_GET_ITEM(plan,i);
        PyObject *name=PyTuple_GET_ITEM(member,0),*descriptor=PyTuple_GET_ITEM(member,1);
        /* Keep the per-instance owner check as well as the plan's check: no
         * descriptor offset is ever applied to an unrelated current value. */
        if (!PyType_IsSubtype(Py_TYPE(value),PyDescr_TYPE(descriptor))) {g->supported=0;break;}
        if (Py_IS_TYPE(descriptor,&PyGetSetDescr_Type)) {
                PyObject *fields=Py_TYPE(descriptor)->tp_descr_get(descriptor,value,(PyObject *)type);
                if (!fields) {PyErr_Clear();g->supported=0;break;}
                if (!PyDict_CheckExact(fields)) {Py_DECREF(fields);g->supported=0;break;}
                if (signature_namespace(g,fields)<0) result=-1;
                Py_ssize_t p=0;PyObject *key,*child;
                while (!result && PyDict_Next(fields,&p,&key,&child)) {
                    if (!PyUnicode_CheckExact(key)) continue;
                    if (signature_text(g,key)<0 || signature_push(g,child)<0) result=-1;
                }
                Py_DECREF(fields);
        } else {
                PyMemberDef *member=((PyMemberDescrObject *)descriptor)->d_member;
                if (member->type==T_OBJECT || member->type==T_OBJECT_EX) {
                    PyObject *child=*(PyObject **)((char *)value+member->offset);
                    if (signature_text(g,name)<0 || signature_push(g,child)<0) result=-1;
                } else {
                    PyTypeObject *projection=member_projection_type(value,member);
                    if (!projection) {g->supported=0;break;}
                    if (signature_text(g,name)<0 || signature_number(g,member->type)<0 ||
                        signature_number(g,member->offset)<0 || signature_number(g,(uintptr_t)projection)<0 ||
                        signature_type(g,PyDescr_TYPE(descriptor))<0) result=-1;
                }
        }
    }
    Py_DECREF(plan);return result;
}
static int signature_node(GraphSignature *g,PyObject *value) {
    PyTypeObject *type=Py_TYPE(value);
    if (signature_type(g,type)<0 || !g->supported) return PyErr_Occurred() ? -1 : 0;
    PyObject *address=PyLong_FromVoidPtr(value);
    if (!address) return -1;
    int boundary=PyDict_GetItem(g->boundaries,address)==value;Py_DECREF(address);
    if (boundary) return 0;
    if (type==&PyList_Type || type==&PyTuple_Type) {
        Py_ssize_t count=Py_SIZE(value);
        if (signature_number(g,count)<0) return -1;
        for (Py_ssize_t i=0;g->supported && i<count;i++)
            if (signature_push(g,type==&PyList_Type ? PyList_GET_ITEM(value,i) : PyTuple_GET_ITEM(value,i))<0) return -1;
    } else if (type==&PyDict_Type) {
        if (signature_namespace(g,value)<0 || signature_number(g,PyDict_Size(value))<0) return -1;
        Py_ssize_t p=0;PyObject *key,*child;
        while (g->supported && PyDict_Next(value,&p,&key,&child))
            if (signature_push(g,key)<0 || signature_push(g,child)<0) return -1;
    } else if (type==&PySet_Type || type==&PyFrozenSet_Type) {
        if (signature_number(g,PySet_GET_SIZE(value))<0) return -1;
        Py_ssize_t p=0;PyObject *child;Py_hash_t hash;
        while (g->supported && _PySet_NextEntry(value,&p,&child,&hash))
            if (signature_push(g,child)<0) return -1;
    } else if (type==&PyFunction_Type) {
        PyFunctionObject *fn=(PyFunctionObject *)value;
        if (signature_number(g,(uintptr_t)fn->func_code)<0 || signature_number(g,fn->func_module && fn->func_module!=Py_None)<0 ||
            (fn->func_module && fn->func_module!=Py_None && signature_text(g,fn->func_module)<0) ||
            signature_push(g,fn->func_defaults)<0 || signature_push(g,fn->func_kwdefaults)<0) return -1;
        if (fn->func_closure) {
            for (Py_ssize_t i=0;g->supported && i<PyTuple_GET_SIZE(fn->func_closure);i++)
                if (signature_push(g,PyCell_GET(PyTuple_GET_ITEM(fn->func_closure,i)))<0) return -1;
        }
    } else if (type==&PyMethod_Type) {
        if (signature_push(g,PyMethod_GET_SELF(value))<0 || signature_push(g,PyMethod_GET_FUNCTION(value))<0) return -1;
    } else if (PyCFunction_Check(value)) {
        PyCFunctionObject *fn=(PyCFunctionObject *)value;
        if (signature_number(g,(uintptr_t)fn->m_ml)<0 ||
            (fn->m_module && fn->m_module!=Py_None && signature_text(g,fn->m_module)<0) || signature_push(g,fn->m_self)<0) return -1;
    } else if (type==&PyModule_Type) {
        PyObject *name=PyDict_GetItemString(PyModule_GetDict(value),"__name__");
        if (!name || !PyUnicode_CheckExact(name)) {g->supported=0;return 0;}
        if (signature_text(g,name)<0 || signature_owner_file(g,name)<0) return -1;
    } else if (PyType_Check(value)) {
        if (signature_type(g,(PyTypeObject *)value)<0) return -1;
    } else if (!(type==&PyUnicode_Type || type==&PyBytes_Type || type==&PyLong_Type || type==&PyFloat_Type ||
                 type==&PyComplex_Type || type==&PyBool_Type || type==Py_TYPE(Py_None) || type==&PyCode_Type)) {
        return signature_fields(g,value);
    }
    return 0;
}
static PyObject *configure_dict_getter(PyObject *self,PyObject *descriptor) {
    if (!Py_IS_TYPE(descriptor,&PyGetSetDescr_Type)) {PyErr_SetString(PyExc_TypeError,"builtin dict storage descriptor required");return NULL;}
    getter function=((PyGetSetDescrObject *)descriptor)->d_getset->get;
    for (int i=0;i<standard_dict_getter_count;i++) if (standard_dict_getters[i]==function) Py_RETURN_NONE;
    if (standard_dict_getter_count>=4) {PyErr_SetString(PyExc_RuntimeError,"builtin storage descriptor capacity exceeded");return NULL;}
    standard_dict_getters[standard_dict_getter_count++]=function;Py_RETURN_NONE;
}
static PyObject *graph_signature(PyObject *self,PyObject *args) {
    PyObject *roots,*boundaries,*capsule=Py_None;Py_ssize_t budget;
    if (!PyArg_ParseTuple(args,"O!nO!|O",&PyDict_Type,&roots,&budget,&PyDict_Type,&boundaries,&capsule)) return NULL;
    GraphSignature g={0};g.budget=budget;g.boundaries=boundaries;g.supported=1;g.field_plans=PyDict_New();
    PyObject *result=NULL;Py_ssize_t pos=0;PyObject *name,*value;
    if (capsule!=Py_None) {g.type_cache=type_context_cache_pointer(capsule);if (!g.type_cache) goto done;}
    PyObject *metadata_context=leaf_metadata_context();
    if (!metadata_context) goto done;
    if (metadata_context==Py_None) {Py_DECREF(metadata_context);result=Py_NewRef(Py_None);goto done;}
    int context_status=signature_write(&g,PyBytes_AS_STRING(metadata_context),PyBytes_GET_SIZE(metadata_context));
    Py_DECREF(metadata_context);if (context_status<0) goto done;
    if (!g.field_plans || signature_number(&g,PyDict_Size(roots))<0) goto done;
    while (g.supported && PyDict_Next(roots,&pos,&name,&value))
        if (signature_text(&g,name)<0 || signature_push(&g,value)<0) goto done;
    while (g.supported && g.used<g.queued) {
        value=g.queue[g.used++];int seen=identity_seen(&g.seen,(uintptr_t)value);
        if (seen<0 || (!seen && signature_node(&g,value)<0)) goto done;
    }
    result=g.supported ? PyBytes_FromStringAndSize(g.bytes,g.size) : Py_NewRef(Py_None);
done:
    for (Py_ssize_t i=0;i<g.queued;i++) Py_DECREF(g.queue[i]);
    PyMem_Free(g.queue);PyMem_Free(g.bytes);identity_clear(&g.seen);identity_clear(&g.types);Py_XDECREF(g.field_plans);identity_clear(&g.base_namespaces);
    return result;
}
static PyObject *native_definition(PyObject *self,PyObject *value) {
    if (PyCFunction_Check(value)) return PyLong_FromVoidPtr(((PyCFunctionObject *)value)->m_ml);
    if (Py_IS_TYPE(value,&PyMethodDescr_Type) || Py_IS_TYPE(value,&PyClassMethodDescr_Type))
        return PyLong_FromVoidPtr(((PyMethodDescrObject *)value)->d_method);
    Py_RETURN_NONE;
}

#include "reference_children.h"

static PyMethodDef methods[] = {
    {"flat_sequence_changes", flat_sequence_changes, METH_VARARGS, "Exact changed positions in validated immutable-leaf identity vectors."},
    {"reference_children", reference_children, METH_VARARGS, "Enumerate exact builtin references without Python per-edge construction."},
    {"new_type_context_cache", new_type_context_cache, METH_NOARGS, "Bounded weak exact type metadata reuse."},
    {"field_plan_cache_stats", field_plan_cache_stats, METH_O, "Weak exact field layout reuse counters; no instance values cached."},
    {"type_context_cache_stats", type_context_cache_stats, METH_O, "Exact type metadata reuse counters."},
    {"type_metadata_context", type_metadata_context, METH_VARARGS, "Validate observed type metadata without traversing instance references."},
    {"member_storage", member_storage, METH_VARARGS, "Describe unboxed native storage without manufacturing Python values."},
    {"native_definition", native_definition, METH_O, "Exact native method-definition identity, including saved bound methods."},
    {"configure_dict_getter", configure_dict_getter, METH_O, "Recognize CPython builtin instance dictionary storage."},
    {"graph_signature", graph_signature, METH_VARARGS, "Collision-free validator for supported observed reference graphs."},
    {"flat_sequence_signature", flat_sequence_signature, METH_O, "Exact identities of flat immutable builtin leaves."},
    {"frame_function", frame_function, METH_O, "Return the exact executing function."},
    {NULL, NULL, 0, NULL}
};
static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "_callchain_frame_identity", NULL, -1, methods
};
PyMODINIT_FUNC PyInit__callchain_frame_identity(void) {
    return PyModule_Create(&module);
}
