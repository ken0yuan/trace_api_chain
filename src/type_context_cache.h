/* Exact reusable type descriptions. Weak guards never retain workload classes,
 * functions, code objects or instances. Every hit rechecks namespace versions,
 * MRO identity, code-object identity, names and module source ownership. */
struct TypeContextCache {PyObject *entries;uint64_t hits,misses,invalidations;};
#define TYPE_CONTEXT_CAPSULE "callchain.exact_type_context/1"
static void type_context_cache_destroy(PyObject *capsule) {
    TypeContextCache *cache=PyCapsule_GetPointer(capsule,TYPE_CONTEXT_CAPSULE);
    if (cache) {Py_DECREF(cache->entries);PyMem_Free(cache);}
}
static TypeContextCache *type_context_cache_pointer(PyObject *value) {
    return PyCapsule_GetPointer(value,TYPE_CONTEXT_CAPSULE);
}
static PyObject *new_type_context_cache(PyObject *self,PyObject *ignored) {
    TypeContextCache *cache=PyMem_Calloc(1,sizeof(*cache));
    if (!cache) return PyErr_NoMemory();
    cache->entries=PyDict_New();
    if (!cache->entries) {PyMem_Free(cache);return NULL;}
    PyObject *result=PyCapsule_New(cache,TYPE_CONTEXT_CAPSULE,type_context_cache_destroy);
    if (!result) {Py_DECREF(cache->entries);PyMem_Free(cache);}
    return result;
}
static PyObject *type_context_cache_stats(PyObject *self,PyObject *value) {
    TypeContextCache *cache=type_context_cache_pointer(value);
    if (!cache) return NULL;
    return Py_BuildValue("{s:K,s:K,s:K,s:n,s:i}","hits",cache->hits,"misses",cache->misses,
        "invalidations",cache->invalidations,"entries",PyDict_Size(cache->entries),"limit",4096);
}
static PyObject *type_owner_state(PyTypeObject *type) {
    GraphSignature g={0};g.supported=1;PyObject *name=NULL,*result=NULL;
    if (type->tp_flags & Py_TPFLAGS_HEAPTYPE) {
        name=PyDict_GetItemString(type->tp_dict,"__module__");name=Py_NewRef(name ? name : Py_None);
    } else {
        const char *dot=strrchr(type->tp_name,'.');
        name=dot ? PyUnicode_FromStringAndSize(type->tp_name,dot-type->tp_name) : PyUnicode_FromString("builtins");
    }
    if (!name) goto done;
    if (name!=Py_None && !PyUnicode_CheckExact(name)) {result=Py_NewRef(Py_None);goto done;}
    if (signature_owner_file(&g,name)<0 || signature_owner_file(&g,Py_None)<0) goto done;
    result=g.supported ? PyBytes_FromStringAndSize(g.bytes,g.size) : Py_NewRef(Py_None);
done:Py_XDECREF(name);PyMem_Free(g.bytes);return result;
}
static PyObject *type_context_guard(PyTypeObject *type) {
    PyObject *weak=NULL,*bases=NULL,*owner=NULL,*result=NULL;
    if (!type->tp_mro || !PyTuple_CheckExact(type->tp_mro)) Py_RETURN_NONE;
    weak=PyWeakref_NewRef((PyObject *)type,NULL);if (!weak) goto done;
    bases=PyTuple_New(PyTuple_GET_SIZE(type->tp_mro));if (!bases) goto done;
    for (Py_ssize_t i=0;i<PyTuple_GET_SIZE(type->tp_mro);i++) {
        PyTypeObject *base=(PyTypeObject *)PyTuple_GET_ITEM(type->tp_mro,i);
        if (!PyDict_CheckExact(base->tp_dict)) {result=Py_NewRef(Py_None);goto done;}
        PyObject *functions=PyList_New(0),*baseweak=NULL,*row=NULL;
        if (!functions) goto done;
        Py_ssize_t pos=0;PyObject *name,*member;GraphSignature g={0};g.supported=1;
        while (g.supported && PyDict_Next(base->tp_dict,&pos,&name,&member)) {
            PyObject *fn=exact_class_function(&g,member);if (!fn) continue;
            PyObject *wf=PyWeakref_NewRef(fn,NULL),*wc=PyWeakref_NewRef(((PyFunctionObject *)fn)->func_code,NULL),*pair=NULL;
            if (wf && wc) pair=PyTuple_Pack(2,wf,wc);
            Py_XDECREF(wf);Py_XDECREF(wc);
            if (!pair || PyList_Append(functions,pair)<0) {Py_XDECREF(pair);Py_DECREF(functions);goto done;}
            Py_DECREF(pair);
        }
        if (!g.supported) {Py_DECREF(functions);result=Py_NewRef(Py_None);goto done;}
        baseweak=PyWeakref_NewRef((PyObject *)base,NULL);
        if (baseweak) row=Py_BuildValue("(OKO)",baseweak,(unsigned long long)((PyDictObject *)base->tp_dict)->ma_version_tag,functions);
        Py_XDECREF(baseweak);Py_DECREF(functions);if (!row) goto done;
        PyTuple_SET_ITEM(bases,i,row);
    }
    owner=type_owner_state(type);if (!owner) goto done;
    if (owner==Py_None) {result=Py_NewRef(Py_None);goto done;}
    int heap=!!(type->tp_flags & Py_TPFLAGS_HEAPTYPE);
    PyObject *name=heap ? ((PyHeapTypeObject *)type)->ht_name : Py_None;
    PyObject *qualname=heap ? ((PyHeapTypeObject *)type)->ht_qualname : Py_None;
    result=Py_BuildValue("(OKiOOOO)",weak,(unsigned long long)(uintptr_t)type->tp_mro,heap,name,qualname,bases,owner);
done:Py_XDECREF(weak);Py_XDECREF(bases);Py_XDECREF(owner);return result;
}
static int type_context_guard_valid(PyObject *guard,PyTypeObject *type,int namespace_only) {
    if (guard==Py_None || PyWeakref_GetObject(PyTuple_GET_ITEM(guard,0))!=(PyObject *)type) return 0;
    if (namespace_only) {
        /* A namespace depends only on this base's own bindings and code. Do
         * not repeatedly validate inherited namespaces already handled by the
         * graph's shared-base table; full type descriptions still check all. */
        PyObject *row=PyTuple_GET_ITEM(PyTuple_GET_ITEM(guard,5),0);
        if (PyWeakref_GetObject(PyTuple_GET_ITEM(row,0))!=(PyObject *)type || !PyDict_CheckExact(type->tp_dict) ||
            ((PyDictObject *)type->tp_dict)->ma_version_tag!=PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(row,1))) return 0;
        PyObject *functions=PyTuple_GET_ITEM(row,2);
        for (Py_ssize_t i=0;i<PyList_GET_SIZE(functions);i++) {
            PyObject *pair=PyList_GET_ITEM(functions,i),*fn=PyWeakref_GetObject(PyTuple_GET_ITEM(pair,0)),*code=PyWeakref_GetObject(PyTuple_GET_ITEM(pair,1));
            if (fn==Py_None || code==Py_None || ((PyFunctionObject *)fn)->func_code!=code) return 0;
        }
        return 1;
    }
    if ((uintptr_t)type->tp_mro!=PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(guard,1))) return 0;
    int heap=!!(type->tp_flags & Py_TPFLAGS_HEAPTYPE);
    if (heap!=PyLong_AsLong(PyTuple_GET_ITEM(guard,2))) return 0;
    if (heap && (((PyHeapTypeObject *)type)->ht_name!=PyTuple_GET_ITEM(guard,3) ||
                 ((PyHeapTypeObject *)type)->ht_qualname!=PyTuple_GET_ITEM(guard,4))) return 0;
    PyObject *bases=PyTuple_GET_ITEM(guard,5);
    if (!type->tp_mro || PyTuple_GET_SIZE(type->tp_mro)!=PyTuple_GET_SIZE(bases)) return 0;
    for (Py_ssize_t i=0;i<PyTuple_GET_SIZE(bases);i++) {
        PyObject *row=PyTuple_GET_ITEM(bases,i);PyTypeObject *base=(PyTypeObject *)PyTuple_GET_ITEM(type->tp_mro,i);
        if (PyWeakref_GetObject(PyTuple_GET_ITEM(row,0))!=(PyObject *)base || !PyDict_CheckExact(base->tp_dict) ||
            ((PyDictObject *)base->tp_dict)->ma_version_tag!=PyLong_AsUnsignedLongLong(PyTuple_GET_ITEM(row,1))) return 0;
        PyObject *functions=PyTuple_GET_ITEM(row,2);
        for (Py_ssize_t j=0;j<PyList_GET_SIZE(functions);j++) {
            PyObject *pair=PyList_GET_ITEM(functions,j),*fn=PyWeakref_GetObject(PyTuple_GET_ITEM(pair,0)),*code=PyWeakref_GetObject(PyTuple_GET_ITEM(pair,1));
            if (fn==Py_None || code==Py_None || ((PyFunctionObject *)fn)->func_code!=code) return 0;
        }
    }
    PyObject *owner=type_owner_state(type);if (!owner) return -1;
    int equal=PyObject_RichCompareBool(owner,PyTuple_GET_ITEM(guard,6),Py_EQ);Py_DECREF(owner);return equal;
}
static PyObject *cached_type_description(TypeContextCache *cache,PyTypeObject *type,int part) {
    PyObject *key=PyLong_FromVoidPtr(type),*guard=NULL,*context=NULL,*namespace=NULL,*result=NULL;
    if (!key) return NULL;
    PyObject *entry=PyDict_GetItem(cache->entries,key);
    if (entry) {
        int valid=type_context_guard_valid(PyTuple_GET_ITEM(entry,0),type,part==2);
        if (valid<0) goto done;
        if (valid) {cache->hits++;result=Py_NewRef(PyTuple_GET_ITEM(entry,part));goto done;}
        cache->invalidations++;
    }
    cache->misses++;guard=type_context_guard(type);if (!guard) goto done;
    int before=type_context_guard_valid(guard,type,0);if (before<0) goto done;
    context=uncached_type_metadata_context((PyObject *)type);if (!context) goto done;
    if (context==Py_None) {result=Py_NewRef(Py_None);goto done;}
    int after=before ? type_context_guard_valid(guard,type,0) : 0;if (after<0) goto done;
    if (part==2 || after) {
        GraphSignature base={0};base.supported=1;
        if (signature_namespace(&base,type->tp_dict)>=0 && signature_class_functions(&base,type->tp_dict)>=0)
            namespace=base.supported ? PyBytes_FromStringAndSize(base.bytes,base.size) : Py_NewRef(Py_None);
        PyMem_Free(base.bytes);if (!namespace) goto done;
    }
    if (after && namespace!=Py_None) {
        int valid=type_context_guard_valid(guard,type,0);if (valid<0) goto done;
        if (valid) {
            PyObject *pair=PyTuple_Pack(3,guard,context,namespace);
            if (!pair || PyDict_SetItem(cache->entries,key,pair)<0) {Py_XDECREF(pair);goto done;}
            Py_DECREF(pair);
            if (PyDict_Size(cache->entries)>4096) {
                Py_ssize_t pos=0;PyObject *old,*value;
                if (PyDict_Next(cache->entries,&pos,&old,&value) && PyDict_DelItem(cache->entries,old)<0) goto done;
            }
        }
    }
    result=Py_NewRef(part==1 ? context : namespace);
done:Py_XDECREF(guard);Py_XDECREF(context);Py_XDECREF(namespace);Py_DECREF(key);return result;
}
static PyObject *cached_type_metadata_context(TypeContextCache *cache,PyTypeObject *type) {
    return cached_type_description(cache,type,1);
}
static PyObject *cached_base_namespace(TypeContextCache *cache,PyTypeObject *type) {
    return cached_type_description(cache,type,2);
}
static PyObject *type_metadata_context(PyObject *self,PyObject *args) {
    PyObject *type,*capsule=Py_None;
    if (!PyArg_ParseTuple(args,"O|O",&type,&capsule)) return NULL;
    if (!PyType_Check(type)) {PyErr_SetString(PyExc_TypeError,"type required");return NULL;}
    if (capsule==Py_None) return uncached_type_metadata_context(type);
    TypeContextCache *cache=type_context_cache_pointer(capsule);if (!cache) return NULL;
    return cached_type_metadata_context(cache,(PyTypeObject *)type);
}
