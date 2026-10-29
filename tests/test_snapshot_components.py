"""Reusing a stable interior must retain all changes outside and inside it."""
import json
import sys
import types

from trace_api_chain.object_capture import Recorder


def test_interior_reuse_preserves_new_branches_aliases_cycles_and_notes(tmp_path):
    class Module(types.ModuleType):
        @property
        def protected(self):
            raise AssertionError('observer must not evaluate a property')
    stable=Module('component_fixture')
    stable.values={str(i):{'payload':[i,i+1]} for i in range(90)}
    outer={'stable':stable};stable.cycle=outer
    # Keep the large component acyclic with respect to the changing outer root.
    del stable.cycle
    optimized=Recorder(tmp_path/'optimized.jsonl','test::one',tmp_path/'test.py')
    reference=Recorder(tmp_path/'reference.jsonl','test::one',tmp_path/'test.py')
    reference.snapshot_component_limit=0
    for r in (optimized,reference):r.stack=[{'uid':1}]
    def observe():
        for r in (optimized,reference):r.snapshot(sys._getframe(),'locals',{'outer':outer})
    observe()
    for i in range(5):
        outer['new-'+str(i)]={'value':[i],'alias':stable};observe()
    stable.values['0']['payload'].append('new-inner-entity');observe()
    stable.values['new-inner-branch']={'alias':stable};observe()
    stable.values['self']=stable.values;observe()
    del outer['new-1'];observe()
    del stable.values['self'];observe()
    stable.__class__.__qualname__='RenamedModule';observe()
    for r in (optimized,reference):r.file.close()
    def rows(name):return [json.loads(line) for line in (tmp_path/name).read_text().splitlines()][1:]
    assert rows('optimized.jsonl')==rows('reference.jsonl')
    assert optimized.component_cache_promotions>0
    assert optimized.component_cache_reuses>0
    assert optimized.metadata_cache_hits<reference.metadata_cache_hits
    assert not optimized.gaps and not reference.gaps


def test_temporary_native_receivers_do_not_hide_changes_in_the_receiver(tmp_path):
    stable={str(i):[i] for i in range(100)}
    registry={'large':stable}
    optimized=Recorder(tmp_path/'fast.jsonl','test::one',tmp_path/'test.py')
    reference=Recorder(tmp_path/'full.jsonl','test::one',tmp_path/'test.py')
    reference.snapshot_component_limit=0
    for r in (optimized,reference):r.stack=[{'uid':1}]
    for i in range(7):
        registry['new-'+str(i)]=[i]
        if i==4:stable['new']={'branch':[]}
        if i==5:stable['new']['branch'].append('retained')
        callable=registry.get
        for r in (optimized,reference):
            r.snapshot(sys._getframe(),'native_entry',{'callable':callable,'receiver':registry})
    for r in (optimized,reference):r.file.close()
    def rows(name):return [json.loads(line) for line in (tmp_path/name).read_text().splitlines()][1:]
    assert rows('fast.jsonl')==rows('full.jsonl')
    assert optimized.component_cache_reuses>0
    assert not optimized.gaps and not reference.gaps


def test_shared_opaque_leaf_does_not_disable_exact_interior_reuse(tmp_path):
    leaf=types.ModuleType('already_seen_leaf')
    component={'shared_leaf':leaf,'large':{str(i):[i] for i in range(100)}}
    outer={'first_leaf':leaf,'component':component}
    fast=Recorder(tmp_path/'shared-fast.jsonl','test::one',tmp_path/'test.py')
    full=Recorder(tmp_path/'shared-full.jsonl','test::one',tmp_path/'test.py')
    full.snapshot_component_limit=0
    for r in (fast,full):r.stack=[{'uid':1}]
    for i in range(6):
        outer[str(i)]=[i]
        if i==4:component['large']['new_entity']={'branch':[]}
        for r in (fast,full):r.snapshot(sys._getframe(),'locals',{'outer':outer})
    for r in (fast,full):r.file.close()
    def rows(name):return [json.loads(line) for line in (tmp_path/name).read_text().splitlines()][1:]
    assert rows('shared-fast.jsonl')==rows('shared-full.jsonl')
    assert fast.component_cache_reuses>0
    assert fast.metadata_cache_hits<full.metadata_cache_hits


def test_same_snapshot_aliases_validate_cached_component_once(tmp_path,monkeypatch):
    import trace_api_chain.frame_identity as identity
    component={str(i):[i] for i in range(100)}
    outer=[component]*48
    fast=Recorder(tmp_path/'alias-fast.jsonl','test::one',tmp_path/'test.py')
    full=Recorder(tmp_path/'alias-full.jsonl','test::one',tmp_path/'test.py')
    for r in (fast,full):
        r.stack=[{'uid':1}]
        r.snapshot(sys._getframe(),'locals',{'component':component})
        # This case tests first-visit ownership, independently of promotions.
        r.snapshot_component_limit=0
    original=identity.graph_signature;calls=[]
    def observed(roots,*args,**kwargs):
        if roots.get('value') is component:calls.append(component)
        return original(roots,*args,**kwargs)
    monkeypatch.setattr(identity,'graph_signature',observed)
    fast.snapshot(sys._getframe(),'locals',{'outer':outer})
    assert len(calls)==1
    full.snapshot_cache.clear();full.root_states.clear()
    full.snapshot(sys._getframe(),'locals',{'outer':outer})
    # A later changed descendant must still be observed, despite identical
    # component/root identities and all 48 incoming aliases being unchanged.
    component['new']={'branch':[]}
    calls.clear()
    fast.snapshot(sys._getframe(),'locals',{'outer':outer})
    assert len(calls)==1  # Changed component: expand once, not once per alias.
    full.snapshot(sys._getframe(),'locals',{'outer':outer})
    component['new']['branch'].append('new-entity')
    component['self']=component
    for r in (fast,full):r.snapshot(sys._getframe(),'locals',{'outer':outer})
    for r in (fast,full):r.file.close()
    def rows(name):return [json.loads(line) for line in (tmp_path/name).read_text().splitlines()][1:]
    assert rows('alias-fast.jsonl')==rows('alias-full.jsonl')
    assert not fast.gaps and not full.gaps


def test_cached_adjacency_is_borrowed_and_never_mutated_by_new_edges(tmp_path):
    from trace_api_chain.object_capture import add_reference
    component={str(i):[i] for i in range(100)}
    r=Recorder(tmp_path/'borrowed.jsonl','test::one',tmp_path/'test.py');r.stack=[{'uid':1}]
    r.snapshot(sys._getframe(),'locals',{'component':component})
    prior=r.snapshot_cache[r.root_states[(id(component),id(type(component)))]]
    r._snapshot_known_nodes=set();r._snapshot_current_groups={};r._snapshot_current_notes=set()
    chunk=r.snapshot_root([component,component])
    parent=prior[0];original=prior[1][parent]
    assert chunk[1][parent] is original
    groups=dict(chunk[1]);existing=next(iter(original));add_reference(groups,parent,existing)
    assert groups[parent] is original
    new=(parent,'new-actual-reference','new-object');add_reference(groups,parent,new)
    assert new in groups[parent] and new not in original
    assert len(groups[parent])==len(original)+1 and prior[1][parent] is original
    assert not r.gaps;r.file.close()
