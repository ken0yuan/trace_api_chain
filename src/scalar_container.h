/* Experimental structural classifier. Exact builtins only, no user callbacks,
 * no payload copies, and every member is checked (not just the first 101). */
static int summary_scalar_type(PyTypeObject *type) {
    return type==&PyLong_Type || type==&PyFloat_Type || type==&PyBool_Type ||
           type==&PyUnicode_Type || type==&PyBytes_Type;
}
static PyObject *scalar_container_info(PyObject *value,Py_ssize_t threshold) {
    PyTypeObject *type=Py_TYPE(value),*element=NULL,*key_type=NULL;
    Py_ssize_t size,pos=0;PyObject *key,*item;const char *kind;
    if (type==&PyList_Type || type==&PyTuple_Type) {
        size=Py_SIZE(value);if (size<=threshold) Py_RETURN_NONE;
        kind=type==&PyList_Type ? "list" : "tuple";
        for (Py_ssize_t i=0;i<size;i++) {
            item=type==&PyList_Type ? PyList_GET_ITEM(value,i) : PyTuple_GET_ITEM(value,i);
            if (!element) element=Py_TYPE(item);
            if (Py_TYPE(item)!=element || !summary_scalar_type(element)) Py_RETURN_NONE;
        }
    } else if (type==&PyDict_Type) {
        size=PyDict_Size(value);if (size<=threshold) Py_RETURN_NONE;kind="dict";
        while (PyDict_Next(value,&pos,&key,&item)) {
            if (!element) {element=Py_TYPE(item);key_type=Py_TYPE(key);}
            if (Py_TYPE(item)!=element || Py_TYPE(key)!=key_type ||
                !summary_scalar_type(element) || !summary_scalar_type(key_type)) Py_RETURN_NONE;
        }
    } else if (type==&PySet_Type || type==&PyFrozenSet_Type) {
        size=PySet_GET_SIZE(value);if (size<=threshold) Py_RETURN_NONE;
        kind=type==&PySet_Type ? "set" : "frozenset";Py_hash_t hash;
        while (_PySet_NextEntry(value,&pos,&item,&hash)) {
            if (!element) element=Py_TYPE(item);
            if (Py_TYPE(item)!=element || !summary_scalar_type(element)) Py_RETURN_NONE;
        }
    } else Py_RETURN_NONE;
    return Py_BuildValue("snsz",kind,size,element->tp_name,key_type ? key_type->tp_name : NULL);
}
static PyObject *scalar_container_summary(PyObject *self,PyObject *args) {
    PyObject *value;Py_ssize_t threshold=100;
    if (!PyArg_ParseTuple(args,"O|n",&value,&threshold)) return NULL;
    if (threshold<1) {PyErr_SetString(PyExc_ValueError,"positive scalar container threshold required");return NULL;}
    return scalar_container_info(value,threshold);
}
