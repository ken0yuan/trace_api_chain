import contextlib
import json
import sys
import types

from trace_api_chain.object_capture import Recorder


def run(tmp_path,source,call):
    module=types.ModuleType('identity_fixture');module.__file__=str(tmp_path/'api.py')
    exec(compile(source,module.__file__,'exec'),vars(module));sys.modules[module.__name__]=module
    recorder=Recorder(tmp_path/'events.jsonl','test::one',tmp_path/'test.py',namespaces=(module.__name__,))
    recorder.begin()
    try:call(module)
    finally:recorder.end();sys.modules.pop(module.__name__,None)
    rows=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert not recorder.gaps
    return rows


def test_distinct_contextmanager_closure_does_not_open_an_api_window(tmp_path):
    @contextlib.contextmanager
    def unrelated():yield 123
    def call(module):
        assert unrelated.__code__ is module.public.__code__
        with unrelated():pass
        with module.public():pass
    rows=run(tmp_path,'from contextlib import contextmanager\n@contextmanager\ndef public():\n yield None\n',call)
    roots=[r for r in rows if r['event']=='call' and r['parent'] is None]
    assert len(roots)==1 and roots[0]['api_aliases']==['runtime:identity_fixture.public']


def test_functiontype_clone_with_shared_globals_is_not_the_registered_api(tmp_path):
    def call(module):
        foreign=types.FunctionType(module.public.__code__,module.public.__globals__,'foreign',(2,))
        foreign();module.public()
    rows=run(tmp_path,'def public(value=1):return value\n',call)
    roots=[r for r in rows if r['event']=='call' and r['parent'] is None]
    assert len(roots)==1


def test_catalog_aliases_only_include_the_executing_closure(tmp_path):
    source='''def factory():
 def wrapper():return 1
 return wrapper
first=factory()
second=factory()
alias=first
del factory
'''
    rows=run(tmp_path,source,lambda module:(module.first(),module.second()))
    roots=[r for r in rows if r['event']=='call' and r['parent'] is None]
    assert [r['api_aliases'] for r in roots]==[
        ['runtime:identity_fixture.alias','runtime:identity_fixture.first'],
        ['runtime:identity_fixture.second']]


def test_inherited_native_descriptor_does_not_alias_unrelated_base_instances(tmp_path):
    def call(module):
        [].append(1)
        obj=module.EventList();saved=obj.append
        obj.append(2)
        module.EventList.append=lambda self,value:None
        saved(3)
        [].append(4)
    rows=run(tmp_path,'class EventList(list):pass\n',call)
    roots=[r for r in rows if r['event']=='call' and r['parent'] is None]
    assert len(roots)==2
    assert all(r['api_aliases']==['runtime:identity_fixture.EventList.append'] for r in roots)


def test_static_class_and_nonstandard_instance_receiver_names(tmp_path):
    source="""class API:
 @staticmethod
 def static(value):return value
 @classmethod
 def create(Klass):return 1
 def instance(receiver):return 2
"""
    rows=run(tmp_path,source,lambda module:(module.API.static(1),module.API.create(),module.API().instance()))
    roots=[r for r in rows if r['event']=='call' and r['parent'] is None]
    assert {r['api_id'] for r in roots}=={'runtime:identity_fixture.API.static','runtime:identity_fixture.API.create','runtime:identity_fixture.API.instance'}
