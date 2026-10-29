/* Enumerate the same builtin storage as the Python BFS, without invoking user
 * properties. Unsupported storage and special projection values fall back. */
static int reference_child(PyObject *rows,PyObject *parent,PyObject *role,PyObject *child,PyObject *storage) {
    if (!role) return -1;
    if (Py_TYPE(child)==(PyTypeObject *)storage) {Py_DECREF(role);return 1;}
    PyObject *row=PyTuple_Pack(3,parent,role,child);Py_DECREF(role);
    if (!row) return -1;
    int status=PyList_Append(rows,row);Py_DECREF(row);return status;
}
static PyObject *reference_children(PyObject *self,PyObject *args) {
    PyObject *value,*parent,*storage,*rows=NULL,*copy=NULL,*defaults=NULL,*keywords=NULL,*closure=NULL,*names=NULL,*code=NULL;
    if (!PyArg_ParseTuple(args,"OOO",&value,&parent,&storage)) return NULL;
    if (!PyType_Check(storage) || !PyUnicode_CheckExact(parent)) {PyErr_SetString(PyExc_TypeError,"storage type and exact parent token required");return NULL;}
    PyTypeObject *type=Py_TYPE(value);int status=0;
    if (type==&PyUnicode_Type || type==&PyBytes_Type || type==&PyLong_Type || type==&PyFloat_Type ||
        type==&PyComplex_Type || type==&PyBool_Type || value==Py_None || type==&PyCode_Type || type==&PyModule_Type || PyType_Check(value))
        return PyTuple_New(0);
    if (!(type==&PyList_Type || type==&PyTuple_Type || type==&PyDict_Type || type==&PyFunction_Type || type==&PyMethod_Type || type==&PyCFunction_Type)) Py_RETURN_NONE;
    rows=PyList_New(0);if (!rows) return NULL;
    if (type==&PyList_Type || type==&PyTuple_Type) {
        copy=type==&PyList_Type ? PySequence_Tuple(value) : Py_NewRef(value);
        if (!copy) goto error;
        for (Py_ssize_t i=0;i<PyTuple_GET_SIZE(copy);i++) {
            status=reference_child(rows,parent,PyUnicode_FromFormat("item:%zd",i),PyTuple_GET_ITEM(copy,i),storage);
            if (status) goto done;
        }
    } else if (type==&PyDict_Type) {
        copy=PyDict_Copy(value);if (!copy) goto error;
        Py_ssize_t pos=0,index=0;PyObject *key,*child;
        while (PyDict_Next(copy,&pos,&key,&child)) {
            status=reference_child(rows,parent,PyUnicode_FromFormat("key:%zd",index),key,storage);
            if (status) goto done;
            status=reference_child(rows,parent,PyUnicode_FromFormat("value:%zd",index),child,storage);
            if (status) goto done;
            index++;
        }
    } else if (type==&PyFunction_Type) {
        PyFunctionObject *fn=(PyFunctionObject *)value;
        defaults=Py_XNewRef(fn->func_defaults);keywords=fn->func_kwdefaults ? PyDict_Copy(fn->func_kwdefaults) : NULL;
        if (fn->func_kwdefaults && !keywords) goto error;
        closure=Py_XNewRef(fn->func_closure);code=Py_NewRef(fn->func_code);
        if (defaults) for (Py_ssize_t i=0;i<PyTuple_GET_SIZE(defaults);i++) {
            status=reference_child(rows,parent,PyUnicode_FromFormat("default:%zd",i),PyTuple_GET_ITEM(defaults,i),storage);
            if (status) goto done;
        }
        if (keywords) {
            Py_ssize_t pos=0;PyObject *name,*child;
            while (PyDict_Next(keywords,&pos,&name,&child)) {
                if (!PyUnicode_CheckExact(name)) {status=1;goto done;}
                status=reference_child(rows,parent,PyUnicode_FromFormat("keyword_default:%U",name),child,storage);
                if (status) goto done;
            }
        }
        if (closure) {
            names=PyCode_GetFreevars((PyCodeObject *)code);if (!names) goto error;
            Py_ssize_t size=PyTuple_GET_SIZE(names)<PyTuple_GET_SIZE(closure) ? PyTuple_GET_SIZE(names) : PyTuple_GET_SIZE(closure);
            for (Py_ssize_t i=0;i<size;i++) {
                PyObject *child=PyCell_GET(PyTuple_GET_ITEM(closure,i));if (!child) continue;
                status=reference_child(rows,parent,PyUnicode_FromFormat("closure:%U",PyTuple_GET_ITEM(names,i)),child,storage);
                if (status) goto done;
            }
        }
    } else if (type==&PyMethod_Type) {
        status=reference_child(rows,parent,PyUnicode_FromString("receiver"),PyMethod_GET_SELF(value),storage);if (status) goto done;
        status=reference_child(rows,parent,PyUnicode_FromString("function"),PyMethod_GET_FUNCTION(value),storage);
    } else {
        PyObject *receiver=((PyCFunctionObject *)value)->m_self;
        if (receiver && receiver!=Py_None) status=reference_child(rows,parent,PyUnicode_FromString("receiver"),receiver,storage);
    }
    goto done;
error:status=-1;
done:
    Py_XDECREF(copy);Py_XDECREF(defaults);Py_XDECREF(keywords);Py_XDECREF(closure);Py_XDECREF(names);Py_XDECREF(code);
    if (status) {Py_DECREF(rows);return status>0 ? Py_NewRef(Py_None) : NULL;}
    return rows;
}
