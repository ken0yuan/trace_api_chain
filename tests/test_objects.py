import json
import sys
import types
from pathlib import Path

from trace_api_chain.object_capture import Recorder,instance_fields
from trace_api_chain.presentation import build,affected,render


def record(tmp_path, source, invoke):
    module=types.ModuleType('fixture_api')
    path=tmp_path/'api.py';path.write_text(source,encoding='utf-8');module.__file__=str(path)
    exec(compile(source,str(path),'exec'),module.__dict__)
    sys.modules['fixture_api']=module
    recorder=Recorder(tmp_path/'events.jsonl','test_real.py::test_one',tmp_path/'test_real.py',namespaces=('fixture_api',))
    oldp=sys.getprofile();oldt=sys.gettrace()
    recorder.begin()
    try:invoke(module)
    finally:recorder.end();sys.modules.pop('fixture_api',None)
    assert sys.getprofile() is oldp and sys.gettrace() is oldt
    return build(tmp_path/'events.jsonl',resolve_native=False),path


def test_changed_unexecuted_branch_matches_called_owner(tmp_path):
    tree,path=record(tmp_path,'def api(flag):\n    if flag:\n        return "changed"\n    return None\n',lambda m:m.api(False))
    hits=affected(tree,str(path),3)
    assert hits and hits[0]['reasons']==['called_function']
    assert not tree['gaps']
    assert len(tree['roots'])==1
    assert all(n['returned'] for n in tree['calls'].values())


def test_all_container_items_depth_slots_cycles_and_aliases(tmp_path):
    source='''class Item:
    __slots__=('child',)
    def __init__(self): self.child=None
def api(items):
    holder={'deep':[[items]]}
    alias=holder
    return holder
'''
    def invoke(m):
        # Construct the inputs outside capture's API root; only api is traced.
        items=[m.Item.__new__(m.Item) for _ in range(40)]
        for obj in items:obj.child=obj
        m.api(items)
    tree,path=record(tmp_path,source,invoke)
    items=[o for o in tree['objects'].values() if o['type']['qualname']=='Item']
    assert len(items)==40
    assert all(tree['static_definitions'].get(o.get('static_definition'),{}).get('status')=='mapped' for o in items)
    refs=[r for n in tree['calls'].values() for observation in n['reference_observations'] for r in observation['added']]
    assert any(r[1]=='item:39' for r in refs)
    assert any(r[1]=='slot:child' and r[0]==r[2] for r in refs)
    assert not tree['gaps']


def test_no_getattribute_property_repr_or_class_spoof_is_executed():
    class Hostile:
        @property
        def __class__(self):raise AssertionError('spoofed class read')
        @property
        def value(self):raise AssertionError('property evaluated')
        def __getattribute__(self,name):raise AssertionError('custom attribute access')
        def __repr__(self):raise AssertionError('repr evaluated')
    obj=Hostile();object.__setattr__(obj,'child',[])
    fields,notes=instance_fields(obj)
    assert fields==[('attribute:child',[])]
    assert 'property_not_evaluated:value' in notes


def test_repeated_calls_remain_distinct_and_share_static_definition(tmp_path):
    tree,path=record(tmp_path,'def child(): return None\ndef api(): child(); child()\n',lambda m:m.api())
    children=[n for n in tree['calls'].values() if n['name']=='child']
    assert len(children)==2 and children[0]['uid']!=children[1]['uid']
    assert children[0]['definition']==children[1]['definition']
    root=tree['calls'][tree['roots'][0]]
    assert root['children']==[n['uid'] for n in children]
    hits=affected(tree,str(path),1)
    assert sum('called_function' in hit['reasons'] for hit in hits)==2
    assert any(hit['call']==root['uid'] and hit['reasons']==['observed_object'] for hit in hits)


def test_explicit_budget_gap_never_claims_full_objects(tmp_path):
    recorder=Recorder(tmp_path/'events.jsonl','test::one',tmp_path/'test.py',max_references=2)
    recorder.stack=[{'uid':1}]
    recorder.snapshot(sys._getframe(),'entry',{'items':list(range(10))})
    recorder.file.close()
    assert 'reference_budget_exceeded' in recorder.gaps
    assert 'reference_budget_exceeded' in (tmp_path/'events.jsonl').read_text()


def test_render_is_offline_and_escapes_embedded_source(tmp_path):
    tree,path=record(tmp_path,'def api():\n    # </script><script>bad()</script>\n    return None\n',lambda m:m.api())
    path.unlink()
    render(tree,tmp_path/'tree')
    html=(tmp_path/'tree/C03.html').read_text(encoding='utf-8')
    assert '</script><script>bad()' not in html
    assert '源码与对象' in html
    assert (tmp_path/'tree/object-tree.json').exists()


def test_only_runtime_loaded_global_is_observed(tmp_path):
    tree,path=record(tmp_path,'class Item: pass\nUSED=Item()\nUNUSED=Item()\ndef api(flag):\n    if flag: return UNUSED\n    return USED\n',lambda m:m.api(False))
    references=[r[1] for n in tree['calls'].values() for observation in n['reference_observations'] for r in observation['added']]
    assert 'root:global:USED' in references
    assert 'root:global:UNUSED' not in references


def test_builtin_method_reuse_does_not_overwrite_semantic_identity(tmp_path):
    recorder=Recorder(tmp_path/'events.jsonl','test::one',tmp_path/'test.py')
    # A stable function object can also change its code; earlier ownership
    # records must remain valid after the mutation.
    def first():pass
    def second():return None
    old,_=recorder.metadata(first)
    first.__code__=second.__code__
    new,_=recorder.metadata(first)
    recorder.file.close()
    assert old!=new


def test_snapshot_cache_preserves_mutations_aliases_and_class_changes(tmp_path):
    recorder=Recorder(tmp_path/'events.jsonl','test::one',tmp_path/'test.py')
    recorder.stack=[{'uid':1}]
    class Box:pass
    box=Box();box.child=[]
    recorder.snapshot(sys._getframe(),'entry',{'a':box,'b':box})
    box.child.append(Box())
    recorder.snapshot(sys._getframe(),'locals',{'a':box,'b':box})
    Box.__module__='new_module'
    recorder.snapshot(sys._getframe(),'locals',{'a':box,'b':box})
    recorder.file.close()
    events=[json.loads(x) for x in (tmp_path/'events.jsonl').read_text().splitlines()]
    refs=[x for x in events if x['event']=='references']
    assert any(r[1]=='item:0' for r in refs[1]['added'])
    assert {r[1] for r in refs[0]['added'] if r[0] is None}=={'root:a','root:b'}
    assert any(x['event']=='object' and x['type']['module']=='new_module' for x in events)


def test_data_protocol_alias_does_not_break_calls_or_claim_structural_owner(tmp_path):
    source='''from typing import Protocol, runtime_checkable
def shared(self):
    return 7
@runtime_checkable
class Interface(Protocol):
    flag: bool
    run = shared
class Actual:
    flag = True
    run = shared
def api():
    return Actual().run()
'''
    tree,path=record(tmp_path,source,lambda m:m.api())
    assert not tree['gaps']
    assert all(n['returned'] for n in tree['calls'].values())
    events=[json.loads(x) for x in (tmp_path/'events.jsonl').read_text().splitlines()]
    aliases={a for e in events if e['event']=='call' for a in e.get('api_aliases',[])}
    assert 'runtime:fixture_api.Actual.run' in aliases
    assert 'runtime:fixture_api.Interface.run' not in aliases


def test_nominal_subclass_matching_never_calls_custom_subclasscheck(tmp_path):
    source='''class Meta(type):
    def __subclasscheck__(cls, other):
        raise AssertionError('collector invoked user subclass hook')
class Parent(metaclass=Meta):
    def run(self):
        return 11
class Child(Parent):
    pass
def api():
    return Child().run()
'''
    tree,path=record(tmp_path,source,lambda m:m.api())
    assert not tree['gaps']
    events=[json.loads(x) for x in (tmp_path/'events.jsonl').read_text().splitlines()]
    aliases={a for e in events if e['event']=='call' for a in e.get('api_aliases',[])}
    assert 'runtime:fixture_api.Parent.run' in aliases
    assert 'runtime:fixture_api.Child.run' in aliases


def test_generators_and_python_and_c_exceptions_remain_balanced(tmp_path):
    source='''def numbers():
    yield 1
    yield 2
def fail():
    raise ValueError('expected user exception')
def api():
    items=list(numbers())
    try:
        items.index(3)
    except ValueError:
        pass
    try:
        fail()
    except ValueError:
        pass
    return items
'''
    tree,path=record(tmp_path,source,lambda m:m.api())
    assert not tree['gaps']
    assert all(n['returned'] for n in tree['calls'].values())
    events=[json.loads(x) for x in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert any(e['event']=='return' and e['kind']=='c_exception' for e in events)
