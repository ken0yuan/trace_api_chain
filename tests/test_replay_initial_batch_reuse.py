import copy
import json
from pathlib import Path

import pytest
import trace_api_chain
from trace_api_chain.replay import rebuild,EvidenceError

CONTRACT=json.loads(Path(trace_api_chain.__file__).with_name('contract.json').read_text())


def evidence():
    events=[{'event':'start','pid':7,'scope_policy':CONTRACT['version'],'reference_policy':'shared-adjacency-states/2','api_catalog':['fixture:api']}]
    for name in ['target','new-target',*[f'parent:{i}' for i in range(80)]]:events.append({'event':'object','id':name,'kind':'instance'})
    for i in range(80):events.append({'event':'reference_group','id':f'group:{i}','parent':f'parent:{i}','edges':[[f'parent:{i}','child','target']]})
    events.append({'event':'reference_group','id':'changed','parent':'parent:0','edges':[['parent:0','child','new-target']]})
    for uid in range(1,7):
        events.append({'event':'call','uid':uid,'parent':None,'kind':'native_boundary','tid':9,'time_ns':uid*10,'api_id':'fixture:api'})
        events.append({'event':'references','uid':uid,'scope':'native','added_groups':[f'group:{i}' for i in range(80)],'removed_groups':[]})
        if uid in (1,5):events.append({'event':'references','uid':uid,'scope':'native','added_groups':['changed'],'removed_groups':['group:0']})
        events.append({'event':'return','uid':uid,'time_ns':uid*10+1})
    events.append({'event':'end','gaps':[]});return events


def test_complete_graph_equal_with_changed_successors_and_independent_views():
    rows=evidence();assert rebuild(copy.deepcopy(rows),[],CONTRACT)==rebuild(copy.deepcopy(rows),[],CONTRACT,reuse_reference_batches=True)


@pytest.mark.parametrize('damage,reason',[('unknown','unknown_reference_group'),('duplicate','duplicate_reference_group_update'),('parent','multiple_reference_states_for_parent'),('redefine','duplicate_reference_group')])
def test_invalid_updates_after_a_warm_cache_remain_rejected(damage,reason):
    rows=evidence();row=next(e for e in rows if e['event']=='references' and e['uid']==6)
    if damage=='unknown':row['added_groups'].append('missing')
    elif damage=='duplicate':row['added_groups'].append(row['added_groups'][0])
    elif damage=='parent':row['added_groups'].append('changed')
    else:rows.insert(rows.index(row),{'event':'reference_group','id':'group:0','parent':'parent:0','edges':[]})
    for reuse in (False,True):
        with pytest.raises(EvidenceError,match=reason):rebuild(copy.deepcopy(rows),[],CONTRACT,reuse_reference_batches=reuse)
