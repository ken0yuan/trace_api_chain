import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
import trace_api_chain
from trace_api_chain.object_capture import Recorder
from trace_api_chain.replay import rebuild,EvidenceError

CONTRACT=json.loads(Path(trace_api_chain.__file__).with_name('contract.json').read_text())


def group_legacy_references(events):
    output=[];definitions={};views={}
    for event in copy.deepcopy(events):
        if event['event']=='start':event['reference_policy']='shared-adjacency-states/1'
        if event['event']!='references':output.append(event);continue
        key=(event['uid'],event['scope']);live=views.setdefault(key,{})
        old=set(live.values());edges={}
        for group in live.values():
            for edge in definitions[group]['edges']:edges.setdefault(edge[0],set()).add(tuple(edge))
        for edge in event['removed']:edges[edge[0]].remove(tuple(edge))
        for edge in event['added']:edges.setdefault(edge[0],set()).add(tuple(edge))
        live.clear()
        for parent,state in edges.items():
            identity=(parent,frozenset(state));found=next((gid for gid,row in definitions.items() if (row['parent'],frozenset(map(tuple,row['edges'])))==identity),None)
            if found is None:
                found='group:'+str(len(definitions)+1);row={'event':'reference_group','id':found,'parent':parent,'edges':sorted(state,key=str)};definitions[found]=row;output.append(row)
            live[parent]=found
        event['added_groups']=sorted(set(live.values())-old);event['removed_groups']=sorted(old-set(live.values()));event.pop('added');event.pop('removed');output.append(event)
    return output


def capture(path,grouped):
    native=SimpleNamespace(storage='paths') if grouped else None
    recorder=Recorder(path,'test::one',path.parent/'test.py',native=native,registry=SimpleNamespace(catalog={'fixture:api'}))
    child={'values':[1,2]};values={'first':child,'second':[child]}
    for uid in range(1,4):
        recorder.emit('call',uid=uid,parent=None,kind='python',tid=1,time_ns=uid*10,api_id='fixture:api')
        recorder.stack=[{'uid':uid}]
        recorder.snapshot(sys._getframe(),'entry',values)
        recorder.snapshot(sys._getframe(),'locals',values)
        if uid==2:child['values'].append(3)
        recorder.snapshot(sys._getframe(),'exit',values)
        recorder.emit('return',uid=uid,time_ns=uid*10+5)
    recorder.emit('end',gaps=[]);recorder.file.close()
    assert not recorder.gaps
    return [json.loads(line) for line in path.read_text().splitlines()]


def semantic_states(events):
    groups={};views={};last={};output=[]
    for row in events:
        if row['event']=='reference_group':groups[row['id']]=set(map(tuple,row['edges']))
        if row['event']!='references':continue
        key=(row['uid'],row['scope']);view=views.setdefault(key,set())
        if 'added_groups' in row:
            for gid in row['removed_groups']:view.difference_update(groups[gid])
            for gid in row['added_groups']:view.update(groups[gid])
        else:view.difference_update(map(tuple,row['removed']));view.update(map(tuple,row['added']))
        state=(frozenset(view),tuple(row.get('notes',[])))
        if last.get(key)!=state:output.append((key,state));last[key]=state
    return output


def test_shared_reference_states_preserve_aliases_mutations_and_call_views(tmp_path):
    events=capture(tmp_path/'grouped.jsonl',True)
    graph=rebuild(events,[],CONTRACT)
    # Independent legacy encoding of the exact same captured identities.
    legacy=[];groups={};views={}
    for row in events:
        if row['event']=='reference_group':groups[row['id']]=row['edges'];continue
        row=dict(row)
        if row['event']=='references':
            row['added']=[e for gid in row.pop('added_groups') for e in groups[gid]]
            row['removed']=[e for gid in row.pop('removed_groups') for e in groups[gid]]
        legacy.append(row)
    reference=rebuild(legacy,[],CONTRACT)
    assert semantic_states(events)==semantic_states(legacy)
    def topology(nodes):return {uid:{k:v for k,v in node.items() if k!='input_sequence'} for uid,node in nodes.items()}
    assert topology(graph['nodes'])==topology(reference['nodes']) and graph['edges']==reference['edges']
    assert graph['object_observations']!=[] and not graph['gaps']
    assert sum(len(row['edges']) for row in graph['reference_groups'].values())<sum(len(row.get('added',[])) for row in legacy)


@pytest.mark.parametrize('damage,reason',[('unknown','unknown_reference_group'),('policy','reference_group_policy_missing'),('parent','unknown_object_reference'),('duplicate','multiple_reference_states_for_parent')])
def test_replay_rejects_corrupt_shared_reference_evidence(tmp_path,damage,reason):
    events=capture(tmp_path/'corrupt.jsonl',True)
    group=next(e for e in events if e['event']=='reference_group' and e['edges'])
    view=next(e for e in events if e['event']=='references' and group['id'] in e['added_groups'])
    if damage=='unknown':view['added_groups'].append('missing')
    elif damage=='policy':events[0].pop('reference_policy')
    elif damage=='parent':group['edges'][0][0]='wrong-parent'
    else:
        second=copy.deepcopy(group);second['id']='duplicate-parent';events.insert(events.index(view),second);view['added_groups'].append(second['id'])
    with pytest.raises(EvidenceError,match=reason):rebuild(events,[],CONTRACT)


def test_shared_reference_database_matches_legacy_evidence(tmp_path):
    from test_database import capture as fixture_capture
    from trace_api_chain.database import refresh,query
    data=fixture_capture(tmp_path,'def test_one():\n backend.run(1)\n backend.run(2)\n')
    refresh(tmp_path/'legacy.sqlite',data);expected=query(tmp_path/'legacy.sqlite','fixture_api.Backend.run')
    grouped=group_legacy_references(data['events'])
    data['events']=grouped;data['graph']=rebuild(grouped,data['native'],CONTRACT)
    refresh(tmp_path/'shared.sqlite',data)
    assert query(tmp_path/'shared.sqlite','fixture_api.Backend.run')==expected


def test_incremental_group_replacement_keeps_other_parents_and_rejects_stale_removal():
    events=[{'event':'start','pid':7,'scope_policy':CONTRACT['version'],'api_catalog':['fixture:api'],'reference_policy':'shared-adjacency-states/1'},
        {'event':'call','uid':1,'parent':None,'kind':'python','tid':9,'time_ns':10,'api_id':'fixture:api'},
        *[{'event':'object','id':name,'kind':'instance'} for name in ['a','b','c']],
        {'event':'reference_group','id':'a-old','parent':'a','edges':[['a','child','b']]},
        {'event':'reference_group','id':'a-new','parent':'a','edges':[['a','child','c']]},
        {'event':'reference_group','id':'b-state','parent':'b','edges':[['b','alias','c']]},
        {'event':'references','uid':1,'scope':'frame','added_groups':['a-old','b-state'],'removed_groups':[]},
        {'event':'references','uid':1,'scope':'frame','added_groups':['a-new'],'removed_groups':['a-old']},
        {'event':'references','uid':1,'scope':'frame','added_groups':[],'removed_groups':['b-state']},
        {'event':'return','uid':1,'time_ns':20}, {'event':'end','gaps':[]}]
    assert not rebuild(events,[],CONTRACT)['gaps']
    broken=copy.deepcopy(events);broken[-3]['removed_groups']=['a-old']
    with pytest.raises(EvidenceError,match='reference_remove_without_add'):rebuild(broken,[],CONTRACT)
    after_return=copy.deepcopy(events);after_return.insert(-1,dict(events[-3]))
    with pytest.raises(EvidenceError,match='references_after_call_return'):rebuild(after_return,[],CONTRACT)
