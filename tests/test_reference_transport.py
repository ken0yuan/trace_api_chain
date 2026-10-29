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
    assert blocks and sum(row['event']=='reference_snapshot_ref' for row in after)==12
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


def test_exact_templates_keep_all_new_children_and_changed_order():
    from trace_api_chain.reference_transport import SnapshotTransport
    events=[{**start(),'reference_transport':'deferred-adjacency/2'},{'event':'call','uid':1}]
    def emit(kind,**row):events.append({'event':kind,**row})
    transport=SnapshotTransport(emit)
    groups={str(i):frozenset([(str(i),'child','value')]) for i in range(40)}
    roots=frozenset([(None,'root','0')])
    for phase in ('entry','line','line','return'):transport.snapshot(1,phase,['root'],roots,[groups],[])
    changed=dict(groups);changed['0']=frozenset([('0','child','new'),('0','branch','other')])
    transport.snapshot(1,'changed',['root'],roots,[changed],[])
    reordered={'0':changed['0'],**dict(reversed(list(changed.items())[1:]))}
    transport.snapshot(1,'reordered',['root'],roots,[reordered],[])
    assert len([e for e in events if e['event']=='reference_snapshot_template'])==3
    assert len([e for e in events if e['event']=='reference_snapshot_ref'])==6
    deltas=[e for e in events if e['event']=='reference_block_delta']
    assert deltas[0]['order_base'] is True and 'order' not in deltas[0]
    assert deltas[1]['order']==list(reordered)
    normalized=normalize_events(events)
    assert any('new' in str(e) and 'other' in str(e) for e in normalized)
    assert normalize_events([{**e,'reference_transport':'deferred-adjacency/1'} if e['event']=='start' else e for e in events])==normalized


def test_unknown_snapshot_template_is_rejected():
    events=[start(),{'event':'call','uid':1},{'event':'reference_snapshot_ref','uid':1,'phase':'line','template':'missing'}]
    with pytest.raises(EvidenceError,match='unknown_reference_snapshot_template'):normalize_events(events)


def test_template_ref_cannot_extend_ended_call():
    events=[start(),{'event':'call','uid':1},{'event':'reference_snapshot_template','id':'a','names':[],'roots':[],'parts':[],'notes':[]},
            {'event':'return','uid':1},{'event':'reference_snapshot_ref','uid':1,'phase':'line','template':'a'}]
    with pytest.raises(EvidenceError,match='without_active_call'):normalize_events(events)


def test_ambiguous_order_is_rejected():
    events=[start(),{'event':'reference_block','id':'a','rows':[]},
            {'event':'reference_block_delta','id':'b','base':'a','order':[],'order_base':True,'rows':[]}]
    with pytest.raises(EvidenceError,match='ambiguous_reference_block_order'):normalize_events(events)
