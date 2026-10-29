"""First-visit ownership must apply across roots, before recursive validation."""
import gc
import json
import sys
import weakref

from trace_api_chain.object_capture import Recorder


def make(tmp_path):
    r=Recorder(tmp_path/'events.jsonl','test::one',tmp_path/'test.py');r.stack=[{'uid':1}]
    r.snapshot_component_limit=0
    return r


def test_native_callable_receiver_is_not_recursively_validated_twice(tmp_path,monkeypatch):
    import trace_api_chain.frame_identity as identity
    registry={str(i):[i,i+1] for i in range(120)};callable=registry.get
    r=make(tmp_path);original=identity.graph_signature;seen=[]
    def observe(roots,*args,**kwargs):
        if roots.get('value') is registry:seen.append(registry)
        return original(roots,*args,**kwargs)
    monkeypatch.setattr(identity,'graph_signature',observe)
    for i in range(3):
        if i==2:registry['new']={'branch':object()}
        seen.clear()
        r.snapshot(sys._getframe(),'native_entry',{'callable':callable,'receiver':registry})
        assert len(seen)==1
    assert r.snapshot_native_root_stubs==3 and not r.gaps;r.file.close()


def test_new_reference_in_later_snapshot_is_observed_through_same_alias(tmp_path):
    root={'a':{'b':[]}};callable=root.get;r=make(tmp_path)
    r.snapshot(sys._getframe(),'native_entry',{'callable':callable,'receiver':root})
    new=object();root['a']['b'].append(new)
    r.snapshot(sys._getframe(),'native_entry',{'callable':callable,'receiver':root})
    assert any(meta['address']==id(new) for meta in r.object_metadata.values())
    r.file.close();events=[json.loads(x) for x in r.capture_path.read_text().splitlines()]
    refs=[tuple(edge) for row in events if row['event']=='references' for edge in row['added']]
    assert any(parent is None and role=='root:receiver' for parent,role,target in refs)
    assert any(role=='item:0' and target.startswith('object:'+str(id(new))+':') for parent,role,target in refs)
    assert not r.gaps


def test_cached_first_root_does_not_need_metadata_cache_entry_for_receiver(tmp_path,monkeypatch):
    import trace_api_chain.frame_identity as identity
    registry={str(i):[i] for i in range(100)};callable=registry.get;r=make(tmp_path)
    r.snapshot(sys._getframe(),'native_entry',{'callable':callable,'receiver':registry})
    r.metadata_cache.pop((id(registry),id(type(registry))),None)
    original=identity.graph_signature;seen=[]
    def observe(roots,*args,**kwargs):
        if roots.get('value') is registry:seen.append(registry)
        return original(roots,*args,**kwargs)
    monkeypatch.setattr(identity,'graph_signature',observe)
    r.snapshot(sys._getframe(),'native_entry',{'callable':callable,'receiver':registry})
    assert len(seen)==1 and r.snapshot_native_root_stubs==2 and not r.gaps;r.file.close()


def test_multiple_reachable_root_aliases_cycles_and_scalar_aliases_are_kept(tmp_path):
    shared=[];outer={'shared':shared};shared.append(outer);leaf='shared leaf'
    outer['leaf']=leaf;r=make(tmp_path)
    r.snapshot(sys._getframe(),'locals',{'outer':outer,'shared':shared,'again':shared,'leaf':leaf})
    r.file.close();rows=[json.loads(x) for x in r.capture_path.read_text().splitlines()]
    refs=[tuple(edge) for row in rows if row['event']=='references' for edge in row['added']]
    assert {role for parent,role,target in refs if parent is None}=={'root:outer','root:shared','root:again','root:leaf'}
    assert len({target for parent,role,target in refs if parent is None and role in ('root:shared','root:again')})==1
    assert any(role=='item:0' for parent,role,target in refs) and not r.gaps


def test_root_memo_does_not_retain_workload_instances(tmp_path):
    class Item:pass
    item=Item();ref=weakref.ref(item);outer=[item];r=make(tmp_path)
    r.snapshot(sys._getframe(),'locals',{'outer':outer,'alias':item})
    del item,outer;gc.collect()
    assert ref() is None;r.file.close()
