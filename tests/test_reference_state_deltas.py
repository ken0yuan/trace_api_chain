import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import trace_api_chain
from trace_api_chain.object_capture import Recorder
from trace_api_chain.replay import rebuild,canonical,EvidenceError,require
from trace_api_chain.reference_codec import expand_group

CONTRACT=json.loads(Path(trace_api_chain.__file__).with_name('contract.json').read_text())


def test_delta_capture_reconstructs_byte_identical_full_graph_with_new_branches(tmp_path):
    registry={str(i):[str(i)] for i in range(150)}
    values={'registry':registry}
    recorders=[]
    for label,delta in [('full',False),('delta',True)]:
        r=Recorder(tmp_path/(label+'.jsonl'),'fixture',tmp_path/'test.py',native=SimpleNamespace(storage='paths'),
                   registry=SimpleNamespace(catalog={'fixture:api'}),reference_deltas=delta)
        recorders.append(r)
    for uid in range(1,5):
        for r in recorders:
            r.emit('call',uid=uid,parent=None,kind='python',tid=1,time_ns=uid*100,api_id='fixture:api')
            r.stack=[{'uid':uid}]
        for phase in range(5):
            for r in recorders:r.snapshot(sys._getframe(),'locals',values)
            registry['new:'+str(uid)+':'+str(phase)]=['new object',{}]
            registry.pop(str(phase),None)
        for r in recorders:r.emit('return',uid=uid,time_ns=uid*100+50);r.stack=[]
    for r in recorders:r.emit('end',gaps=[]);r.file.close()
    full=[json.loads(x) for x in (tmp_path/'full.jsonl').read_text().splitlines()]
    delta=[json.loads(x) for x in (tmp_path/'delta.jsonl').read_text().splitlines()]
    assert any(x['event']=='reference_group_delta' for x in delta)
    assert canonical(rebuild(full,[],CONTRACT))==canonical(rebuild(delta,[],CONTRACT))
    assert (tmp_path/'delta.jsonl').stat().st_size < (tmp_path/'full.jsonl').stat().st_size
    assert recorders[1].reference_storage['written_edges']<recorders[1].reference_storage['logical_edges']


def test_delta_base_is_storage_reuse_not_cross_call_ancestry():
    objects={'parent':{},'common':{},'left':{},'right':{}}
    groups={'base':{'event':'reference_group','id':'base','parent':'parent',
                    'edges':[['parent','same','common'],['parent','branch','left']]}}
    row={'event':'reference_group_delta','id':'next','parent':'parent','base':'base',
         'added':[['parent','branch','right']],'removed':[['parent','branch','left']]}
    result=expand_group(row,groups,objects,require,'shared-adjacency-states/2')
    assert result['edges']==[['parent','branch','right'],['parent','same','common']]
    assert groups['base']['edges']==[['parent','same','common'],['parent','branch','left']]


@pytest.mark.parametrize('damage,reason',[
 ('base','unknown_reference_delta_base'),('parent','reference_delta_parent_mismatch'),
 ('remove','reference_delta_remove_without_add'),('duplicate','reference_delta_duplicate_add'),
 ('intersection','reference_delta_added_and_removed'),('unknown','unknown_object_reference'),
 ('policy','reference_delta_policy_missing')])
def test_bad_deltas_fail_closed(damage,reason):
    objects={'parent':{},'other':{},'one':{},'two':{}}
    groups={'base':{'event':'reference_group','id':'base','parent':'parent','edges':[['parent','edge','one']]}}
    row={'event':'reference_group_delta','id':'next','parent':'parent','base':'base','added':[['parent','edge','two']],'removed':[]}
    policy='shared-adjacency-states/2'
    if damage=='base':row['base']='missing'
    elif damage=='parent':row['parent']='other'
    elif damage=='remove':row['removed']=[['parent','absent','two']]
    elif damage=='duplicate':row['added']=[['parent','edge','one']]
    elif damage=='intersection':row['removed']=copy.deepcopy(row['added'])
    elif damage=='unknown':row['added']=[['parent','edge','missing']]
    else:policy='shared-adjacency-states/1'
    with pytest.raises(EvidenceError,match=reason):expand_group(row,groups,objects,require,policy)
