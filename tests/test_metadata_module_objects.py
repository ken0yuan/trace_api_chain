import ctypes
import json
import math
import types

from trace_api_chain.object_capture import Recorder
from trace_api_chain.frame_identity import native_definition


def recorder(tmp_path):
    return Recorder(tmp_path/'events.jsonl','test::module-object',tmp_path/'test.py',
                    native=types.SimpleNamespace(storage='paths'),registry=types.SimpleNamespace(catalog=[]),max_references=100000)


def native_with_module(module):
    factory=ctypes.pythonapi.PyCFunction_NewEx
    factory.argtypes=[ctypes.c_void_p,ctypes.py_object,ctypes.py_object];factory.restype=ctypes.py_object
    return factory(native_definition(math.sin),math.sin.__self__,module)


def test_native_module_object_and_all_new_namespace_children_are_preserved(tmp_path):
    module=types.ModuleType('_native_actual_module');module.payload={'deep':[]}
    native=native_with_module(module);assert native.__module__ is module and native(0)==0
    r=recorder(tmp_path);r.snapshot_root(native);before=set(r.object_metadata)
    token,meta=r.metadata(native);assert meta['module']=='_native_actual_module' and meta['module_reference']['address']==id(module)
    module_token=r.metadata(module)[0];namespace_token=r.metadata(vars(module))[0]
    root,groups,*_=r.snapshot_root(native)
    assert (root,'metadata:module',module_token) in groups[root]
    assert (module_token,'metadata:module_namespace',namespace_token) in groups[module_token]
    branch={'brand_new_child':[]};module.payload['deep'].append(branch);r.snapshot_root(native)
    assert r.metadata(branch)[0] in r.object_metadata and before<=set(r.object_metadata)
    assert not r.gaps;json.dumps(meta);r.file.close()


def test_module_name_change_invalidates_native_metadata_cache(tmp_path):
    module=types.ModuleType('before');native=native_with_module(module);r=recorder(tmp_path)
    before=r.metadata(native);module.__name__='after';after=r.metadata(native)
    assert before[0]!=after[0] and after[1]['module']=='after';r.file.close()


def test_python_function_with_module_object_keeps_definition_and_reference(tmp_path):
    def function():pass
    module=types.ModuleType('_function_module');function.__module__=module;r=recorder(tmp_path)
    root,groups,*_=r.snapshot_root(function);assert (root,'metadata:module',r.metadata(module)[0]) in groups[root]
    r.file.flush();events=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    definition=next(e for e in events if e['event']=='definition');assert definition['module']=='_function_module' and definition['module_reference']['address']==id(module)
    assert not r.gaps;r.file.close()


def test_class_module_object_and_unhashable_function_module_are_safe(tmp_path):
    module=types.ModuleType('_class_module')
    class Example:pass
    Example.__module__=module;r=recorder(tmp_path);value=Example();root,groups,*_=r.snapshot_root(value)
    assert (root,'metadata:type_module',r.metadata(module)[0]) in groups[root]
    def function():pass
    field={'all_children':[]};function.__module__=field;root,groups,*_=r.snapshot_root(function)
    assert (root,'metadata:module',r.metadata(field)[0]) in groups[root]
    assert not r.gaps;r.file.close()


def test_string_subclass_keeps_original_module_text_without_user_hooks(tmp_path):
    class Text(str):
        def __str__(self):raise AssertionError('user conversion was invoked')
    def function():pass
    function.__module__=Text('existing.module');r=recorder(tmp_path);r.snapshot_root(function);r.file.flush()
    events=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    definition=next(e for e in events if e['event']=='definition');assert definition['module']=='existing.module'
    assert not r.gaps;r.file.close()


def test_previous_json_literal_metadata_is_preserved_in_reference_marker(tmp_path):
    def function():pass
    module_value={'literal':[1,False,None,'value']};function.__module__=module_value;r=recorder(tmp_path);r.snapshot_root(function);r.file.flush()
    events=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()];definition=next(e for e in events if e['event']=='definition')
    assert json.loads(definition['module_reference']['json_snapshot'])==module_value
    assert not r.gaps;r.file.close()
