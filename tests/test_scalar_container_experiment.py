"""Whole-container classification and explicitly incomplete object evidence."""
import json
import sys
from pathlib import Path

import pytest
import trace_api_chain
from trace_api_chain.frame_identity import scalar_container_summary,graph_signature
from trace_api_chain.object_capture import Recorder
from trace_api_chain.replay import rebuild,EvidenceError
from test_pytest_entry import PRELUDE,launch


@pytest.mark.parametrize('value,expected',[
    ([1]*100,None),([1]*101,('list',101,'int',None)),
    (tuple('x' for _ in range(101)),('tuple',101,'str',None)),
    ({str(i):i for i in range(101)},('dict',101,'int','str')),
    (set(range(101)),('set',101,'int',None)),
    (frozenset(range(101)),('frozenset',101,'int',None)),
    ([1]*101+[2.0],None),([1]*101+[True],None),
    ([b'x']*101,('list',101,'bytes',None)),
    ([False]*101,('list',101,'bool',None)),
    ([1.0]*101,('list',101,'float',None)),
])
def test_exact_builtin_whole_container_classification(value,expected):
    assert scalar_container_summary(value)==expected


def test_functions_instances_subclasses_and_late_non_scalar_are_not_summarized():
    def handler():return 1
    class Item:pass
    class Count(int):pass
    class List(list):
        def __iter__(self):raise AssertionError('custom iteration executed')
    for value in ([handler]*200,[Item()]*200,[1]*101+[handler],
                  {'key'+str(i):handler for i in range(101)},[Count(1)]*101,
                  List([1]*101),[None]*101):
        assert scalar_container_summary(value) is None


def test_real_cpu_tensor_members_are_not_summarized():
    torch=pytest.importorskip('torch')
    value=torch.zeros(1,device='cpu')
    assert scalar_container_summary([value]*101) is None


def test_dictionary_checks_all_keys_and_values_independently():
    value={str(i):i for i in range(101)}
    value['last']=lambda:None
    assert scalar_container_summary(value) is None
    value.pop('last');value[200]=200
    assert scalar_container_summary(value) is None


def make_recorder(tmp_path,name='summary',**kwargs):
    r=Recorder(tmp_path/(name+'.jsonl'),'fixture',tmp_path/'test.py',scalar_container_threshold=100,**kwargs)
    r.stack=[{'uid':1}];return r


def rows(r):
    r.file.close();return [json.loads(line) for line in r.capture_path.read_text().splitlines()]


def test_summary_omits_member_identities_and_new_function_restores_expansion(tmp_path):
    values=['item-'+str(i) for i in range(110)];outer={'values':values}
    r=make_recorder(tmp_path)
    r.snapshot(sys._getframe(),'locals',{'outer':outer})
    assert not any(meta['address']==id(values[100]) for meta in r.object_metadata.values())
    assert len(r.scalar_summary_states)==1
    def new_handler():return 'important'
    values[-1]=new_handler
    r.snapshot(sys._getframe(),'locals',{'outer':outer})
    assert any(meta['address']==id(new_handler) and meta['kind']=='function' for meta in r.object_metadata.values())
    evidence=rows(r);summary=[x for x in evidence if x['event']=='data_container_summary']
    assert summary and summary[0]['element_count']==110 and summary[0]['element_type']=='str'
    assert evidence[0]['object_scope_complete'] is False and not r.gaps


def test_summary_signature_ignores_only_declared_members_and_detects_other_changes():
    values=[1]*101;root={'data':values};kwargs={'scalar_threshold':100}
    first=graph_signature(root,10000,{},**kwargs)
    values[0]=999
    assert graph_signature(root,10000,{},**kwargs)==first  # Explicitly not value history.
    values.append(2)
    assert graph_signature(root,10000,{},**kwargs)!=first
    previous=graph_signature(root,10000,{},**kwargs);values[-1]=lambda:None
    assert graph_signature(root,10000,{},**kwargs)!=previous


def test_mixed_non_scalar_descendant_beyond_two_levels_is_still_observed(tmp_path):
    class Item:pass
    leaf=Item();root={'a':{'b':{'c':{'leaves':[leaf]*101}}}}
    r=make_recorder(tmp_path);r.snapshot(sys._getframe(),'locals',{'root':root});rows(r)
    assert any(meta['address']==id(leaf) for meta in r.object_metadata.values())
    assert not r.scalar_summary_states and not r.gaps


def test_only_declared_container_member_edges_are_omitted(tmp_path):
    class Item:pass
    def handler(x):return x
    instance=Item();instance.nested={'function':handler,'cycle':instance}
    data=['unique-'+str(i) for i in range(110)]
    config={str(i):i for i in range(110)}
    root={'data':data,'config':config,'instance':instance,'handler':handler}
    full=Recorder(tmp_path/'complete.jsonl','fixture',tmp_path/'test.py');full.stack=[{'uid':1}]
    summary=make_recorder(tmp_path,'projected')
    for r in (full,summary):r.snapshot(sys._getframe(),'locals',{'root':root,'shared_data_leaf':data[0]})
    a=rows(full);b=rows(summary)
    def edges(evidence):
        return {tuple(edge) for row in evidence if row['event']=='references' for edge in row.get('added',[])}
    omitted={row['object'] for row in b if row['event']=='data_container_summary'}
    assert len(omitted)==2
    assert {edge for edge in edges(a) if edge[0] not in omitted}==edges(b)
    important={row['id'] for row in a if row['event']=='object' and row['type']['qualname'] not in ('str','int','float','bool','bytes')}
    retained={row['id'] for row in b if row['event']=='object'}
    assert important<=retained
    assert any(row['event']=='object' and row['address']==id(data[0]) for row in b)
    assert not full.gaps and not summary.gaps


def test_full_and_summary_modes_preserve_real_calls_and_isolate_sql(tmp_path):
    from trace_api_chain.pipeline import prepare
    from trace_api_chain.database import prepare_refresh,commit_refresh
    source=PRELUDE.replace('def run(self,x): return self.__consume(x)',
                           'def run(self,x):\n        values = [1] * 150\n        return self.__consume(x)')
    source+='def test_one():\n assert backend.run(2)==3\n'
    prepared={}
    for mode in ('full','summary'):
        out=tmp_path/mode;flags=['--callchain-scalar-container-summary'] if mode=='summary' else []
        result=launch(tmp_path,source,'--callchain-out='+str(out),'--callchain-mode=objects',
                      '--callchain-namespaces=fixture_api',*flags)
        assert result.returncode==0,result.stdout+result.stderr
        prepared[mode]=prepare(out,'objects')
    def topology(graph):return [(n['kind'],n['parent'],n['label'],n['api_id']) for n in graph['nodes'].values()]
    full=prepared['full']['graph'];summary=prepared['summary']['graph']
    assert topology(full)==topology(summary) and full['edges']==summary['edges']
    assert summary['status']=='summary_object_scope_validated' and not summary['gaps']
    assert any(row['element_count']==150 for row in summary['data_container_summaries'])
    assert summary['object_scope_complete'] is False
    assert json.loads((tmp_path/'summary/capture-result.json').read_text())['status']=='captured_summary'
    with pytest.raises(ValueError,match='summary_capture_requires_experiment_slot'):prepare_refresh(prepared['summary'])
    plan=prepare_refresh(prepared['summary'],slot='experiment:scalar-container-summary:fixture')
    formal=tmp_path/'api-callchains.sqlite'
    with pytest.raises(ValueError,match='isolated_experiment_database'):commit_refresh(formal,plan)
    assert not formal.exists();commit_refresh(tmp_path/'test.summary.sqlite',plan)
    import sqlite3
    with sqlite3.connect(tmp_path/'test.summary.sqlite') as db:
        assert db.execute('SELECT status FROM capture_slots').fetchone()[0]=='summary'
        assert db.execute("SELECT count(*) FROM pending WHERE reason='experimental_members_not_recorded'").fetchone()[0]>0
    events=prepared['summary']['events'];forged=json.loads(json.dumps(events))
    next(row for row in forged if row['event']=='data_container_summary')['element_type']='function'
    contract=json.loads(Path(trace_api_chain.__file__).with_name('contract.json').read_text())
    with pytest.raises(EvidenceError,match='invalid_scalar_container_summary_members'):rebuild(forged,[],contract)


def test_depth_and_summary_modes_cannot_accidentally_combine(tmp_path):
    with pytest.raises(ValueError,match='independently'):make_recorder(tmp_path,reference_depth_limit=2)
    result=launch(tmp_path,'def test_one():pass\n','--callchain-out='+str(tmp_path/'bad'),
                  '--callchain-reference-depth=2','--callchain-scalar-container-summary')
    assert result.returncode!=0 and not (tmp_path/'bad').exists()
