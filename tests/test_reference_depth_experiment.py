"""An explicit object-depth experiment must never truncate actual calls."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import trace_api_chain
from trace_api_chain.object_capture import Recorder
from trace_api_chain.replay import rebuild,canonical,EvidenceError
from test_pytest_entry import launch,PRELUDE

CONTRACT=json.loads(Path(trace_api_chain.__file__).with_name('contract.json').read_text())


def recorder(tmp_path,name='bounded',**kwargs):
    r=Recorder(tmp_path/(name+'.jsonl'),'fixture',tmp_path/'test.py',reference_depth_limit=2,**kwargs)
    r.stack=[{'uid':1}];return r


def finish(r):
    r.file.close();return [json.loads(x) for x in r.capture_path.read_text().splitlines()]


def test_two_edges_keep_boundary_identity_without_hidden_recursive_validator(tmp_path,monkeypatch):
    import trace_api_chain.frame_identity as identity
    monkeypatch.setattr(identity,'graph_signature',lambda *args:pytest.fail('unbounded graph validator executed'))
    hidden=object();boundary={'third':hidden};root={'first':{'second':boundary}}
    r=recorder(tmp_path);r.snapshot(sys._getframe(),'locals',{'root':root});rows=finish(r)
    observed={row['address'] for row in rows if row['event']=='object'}
    assert id(root) in observed and id(boundary) in observed and id(hidden) not in observed
    cuts=[row for row in rows if row['event']=='reference_boundary']
    assert any(row['object'].startswith('object:'+str(id(boundary))+':') for row in cuts)
    assert all(row['depth']==row['limit']==2 for row in cuts)
    assert rows[0]['reference_depth_limit']==2 and rows[0]['object_scope_complete'] is False
    assert not r.gaps


def test_shallow_alias_and_later_new_root_keep_new_references(tmp_path):
    child={'payload':object()};boundary={'third':child};root={'first':{'second':boundary}}
    r=recorder(tmp_path)
    r.snapshot(sys._getframe(),'locals',{'deep_first':root,'direct':boundary})
    assert any(meta['address']==id(child['payload']) for meta in r.object_metadata.values())
    fresh=object();child['new']=fresh
    r.snapshot(sys._getframe(),'locals',{'deep_only':root})
    assert not any(meta['address']==id(fresh) for meta in r.object_metadata.values())
    r.snapshot(sys._getframe(),'locals',{'new_direct_root':child})
    assert any(meta['address']==id(fresh) for meta in r.object_metadata.values())
    finish(r);assert not r.gaps


def test_cyclic_aliases_and_depth_boundary_do_not_execute_properties(tmp_path):
    class Item:
        @property
        def dangerous(self):raise AssertionError('property executed')
    instance=Item();instance.self=instance;instance.values={'nested':[object()]}
    r=recorder(tmp_path);r.snapshot(sys._getframe(),'locals',{'one':instance,'alias':instance})
    rows=finish(r)
    refs=[edge for row in rows if row['event']=='references' for edge in row.get('added',[])]
    assert any(role=='attribute:self' and parent==target for parent,role,target in refs)
    assert sum(parent is None and role.startswith('root:') for parent,role,target in refs)==2
    assert not r.gaps


def test_flat_append_reuse_matches_uncached_bounded_observation(tmp_path):
    fast=recorder(tmp_path,'fast');full=recorder(tmp_path,'uncached')
    full.incremental_flat_snapshot=lambda *args:None
    values=list(range(80))
    for i in range(8):
        values.append('new-'+str(i))
        for r in (fast,full):
            if r is full:r.snapshot_cache.clear();r.root_states.clear()
            r.snapshot(sys._getframe(),'locals',{'values':values,'alias':values})
    values[0]={'deeper':{'third':object()}}
    for r in (fast,full):r.snapshot(sys._getframe(),'locals',{'values':values})
    left=finish(fast);right=finish(full)
    assert left[1:]==right[1:]
    assert fast.flat_incremental_updates>0 and not fast.gaps and not full.gaps


def test_real_function_call_topology_remains_unbounded_and_sql_is_isolated(tmp_path):
    from trace_api_chain.pipeline import prepare
    from trace_api_chain.database import prepare_refresh,commit_refresh
    # Four nested calls are deeper than the reference limit and must all remain.
    source=PRELUDE+'def test_one():\n assert backend.run(2)==3\n'
    captures={}
    for mode in ('full','2'):
        out=tmp_path/('capture-'+mode)
        result=launch(tmp_path,source,'--callchain-out='+str(out),'--callchain-mode=objects',
                      '--callchain-namespaces=fixture_api','--callchain-reference-depth='+mode)
        assert result.returncode==0,result.stdout+result.stderr
        captures[mode]=prepare(out,'objects')
    def calls(graph):
        return [(k,n['kind'],n['parent'],n['label'],n['api_id']) for k,n in graph['nodes'].items()]
    a=captures['full']['graph'];b=captures['2']['graph']
    assert calls(a)==calls(b) and a['edges']==b['edges']
    assert {'Backend.run','Backend.__consume','Worker.process'}<={n['label'] for n in b['nodes'].values()}
    assert not b['gaps'] and b['status']=='bounded_object_scope_validated'
    assert b['object_scope_complete'] is False and b['reference_depth_limit']==2
    report=json.loads((tmp_path/'capture-2/capture-result.json').read_text())
    assert report['status']=='captured_bounded' and report['experimental']
    with pytest.raises(ValueError,match='bounded_capture_requires_experiment_slot'):prepare_refresh(captures['2'])
    plan=prepare_refresh(captures['2'],slot='experiment:reference-depth-2:fixture')
    formal=tmp_path/'api-callchains.sqlite'
    with pytest.raises(ValueError,match='isolated_experiment_database'):commit_refresh(formal,plan)
    assert not formal.exists()
    isolated=tmp_path/'fixture.depth2.sqlite';commit_refresh(isolated,plan)
    import sqlite3
    with sqlite3.connect(isolated) as db:
        status,summary=db.execute('SELECT status,summary FROM capture_slots').fetchone()
    assert status=='bounded' and json.loads(summary)['object_scope_complete'] is False


def test_undeclared_boundary_evidence_is_rejected(tmp_path):
    r=recorder(tmp_path);r.snapshot(sys._getframe(),'locals',{'root':{'a':{'b':{}}}})
    rows=finish(r)
    rows[0].pop('reference_depth_limit');rows[0].pop('object_expansion_policy')
    # Insert a minimal enclosing call so rejection addresses the declaration.
    call={'event':'call','uid':1,'parent':None,'kind':'python','tid':1,'time_ns':1,'api_id':'fixture:api'}
    rows[0]['api_catalog']=['fixture:api'];rows.insert(1,call)
    with pytest.raises(EvidenceError,match='undeclared_reference_depth_boundary'):rebuild(rows,[],CONTRACT)
