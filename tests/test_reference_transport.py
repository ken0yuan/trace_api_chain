import json
import sys
from types import SimpleNamespace

import pytest

from trace_api_chain.object_capture import Recorder
from trace_api_chain.reference_transport import normalize_events
from trace_api_chain.replay import EvidenceError


def recorded(tmp_path,deferred,shared,methods,branch):
    path=tmp_path/('transport.jsonl' if deferred else 'legacy.jsonl')
    r=Recorder(path,'test::transport',tmp_path/'test.py',native=SimpleNamespace(storage='paths'),
               registry=SimpleNamespace(catalog=[]),max_references=1000000,
               reference_deltas=True,defer_reference_states=deferred)
    for uid,method in enumerate(methods,1):
        if uid==3:shared['deep']['values'].append(branch)
        if uid==5:shared['deep']['values'].pop()
        if uid==4:shared['reordered']=shared.pop('0')
        if uid==6:shared['0']=shared.pop('reordered')
        r.emit('call',uid=uid);r.stack=[{'uid':uid}]
        r.snapshot(sys._getframe(),'native_entry',{'callable':method,'receiver':shared})
        r.snapshot(sys._getframe(),'global_read',{'global:shared':shared})
        r.emit('return',uid=uid);r.retire_views(uid);r.stack=[]
    r.emit('end',reference_storage=r.reference_storage);r.file.close()
    assert not r.gaps
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_complete_normalized_events_equal_including_new_deep_branch(tmp_path):
    shared={str(i):{'leaf':[i]} for i in range(160)};shared['deep']={'values':[]}
    methods=[shared.get for _ in range(6)]
    branch={'new':[]}
    original=dict(shared)
    before=recorded(tmp_path,False,shared,methods,branch)
    # Restore the same initial order without replacing any workload objects.
    shared.clear();shared.update(original)
    after=recorded(tmp_path,True,shared,methods,branch)
    assert normalize_events(before) is before
    assert normalize_events(after)==before
    blocks=[row for row in after if row['event']=='reference_block']
    assert blocks and sum(row['event']=='reference_snapshot' for row in after)==12
    assert all(row['event'] not in ('reference_group','reference_group_delta','references') for row in after)


def start():
    return {'event':'start','reference_policy':'shared-adjacency-states/2','reference_transport':'deferred-adjacency/1'}


def test_unknown_block_rejected():
    rows=[start(),{'event':'call','uid':1},{'event':'reference_snapshot','uid':1,'phase':'entry',
          'names':['x'],'roots':[],'parts':[{'block':'absent'}],'notes':[]}]
    with pytest.raises(EvidenceError,match='unknown_reference_block'):normalize_events(rows)


def test_snapshot_after_return_rejected():
    rows=[start(),{'event':'call','uid':1},{'event':'return','uid':1},
          {'event':'reference_snapshot','uid':1,'phase':'entry','names':[],
           'roots':[],'parts':[],'notes':[]}]
    with pytest.raises(EvidenceError,match='without_active_call'):normalize_events(rows)


def test_conflicting_parts_rejected():
    rows=[start(),{'event':'call','uid':1},{'event':'reference_snapshot','uid':1,'phase':'entry',
          'names':[],'roots':[],'parts':[{'rows':[['a',[['a','item:0','b']]]]},{'rows':[['a',[]]]}], 'notes':[]}]
    with pytest.raises(EvidenceError,match='conflicting_reference'):normalize_events(rows)


def test_unknown_delta_base_rejected():
    rows=[start(),{'event':'reference_block_delta','id':'x','base':'missing','order':[],'rows':[]}]
    with pytest.raises(EvidenceError,match='unknown_reference_block_base'):normalize_events(rows)
