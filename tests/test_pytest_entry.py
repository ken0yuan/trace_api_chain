"""Behavioral checks using the installed pytest11 entry point in child processes."""
import json
import os
from pathlib import Path
import subprocess
import sys


def launch(tmp_path,source,*args):
    test=tmp_path/'test_case.py';test.write_text(source)
    env={k:v for k,v in os.environ.items() if k not in ('TRACE_API_CHAIN_INTERNAL','PYTEST_DISABLE_PLUGIN_AUTOLOAD','PYTEST_ADDOPTS')}
    return subprocess.run([sys.executable,'-m','pytest',str(test)+'::test_one','-q',*args],
                          cwd=tmp_path,env=env,capture_output=True,text=True,timeout=90)

PRELUDE='''import types, sys
from pathlib import Path
m=types.ModuleType('fixture_api')
source="""class Worker:
    def process(self, x): return x+1
class Backend:
    def __init__(self): self.__worker=Worker()
    def run(self,x): return self.__consume(x)
    def __consume(self,x): return self.__worker.process(x)
def api(x): return x+1
"""
p=Path(__file__).with_name('backend.py');p.write_text(source);m.__file__=str(p)
exec(compile(source,str(p),'exec'),m.__dict__);sys.modules['fixture_api']=m
backend=m.Backend()
'''


def test_plugin_auto_discovered_and_inactive_by_default(tmp_path):
    result=launch(tmp_path,"from pathlib import Path\ndef test_one():\n p=Path('count');p.write_text(p.read_text()+'x' if p.exists() else 'x')\n")
    assert result.returncode==0,result.stdout+result.stderr
    assert (tmp_path/'count').read_text()=='x'
    assert not list(tmp_path.rglob('events.jsonl'))


def test_private_member_precreated_instance_and_real_method_calls(tmp_path):
    out=tmp_path/'capture'
    result=launch(tmp_path,PRELUDE+'def test_one():\n assert backend.run(2)==3\n',
                  '--callchain-out='+str(out),'--callchain-mode=objects','--callchain-namespaces=fixture_api')
    assert result.returncode==0,result.stdout+result.stderr
    assert not (out/'graph.json').exists()
    assert not (out/'tree').exists()
    assert json.loads((out/'capture-result.json').read_text())['stage']=='capture_only'
    from trace_api_chain.pipeline import prepare
    from trace_api_chain.database import refresh,render_api
    data=prepare(out,'objects');db=tmp_path/'all-libraries.sqlite'
    refresh(db,data)
    render_api(db,'fixture_api.Backend.run',tmp_path/'html')
    graph=json.loads((out/'graph.json').read_text())
    labels={n['label'] for n in graph['nodes'].values()}
    assert {'Backend.run','Backend.__consume','Worker.process'}<=labels
    assert not any('__init__' in name for name in labels)
    refs=[r for row in graph['reference_observations'] for r in row['observation']['added']]
    assert any(r[1]=='attribute:_Backend__worker' for r in refs)
    assert graph['status']=='observed_scope_validated'
    assert (tmp_path/'html/C03.html').is_file()


def test_compile_alias_and_returned_wrapper_subtree_excluded(tmp_path):
    out=tmp_path/'capture'
    extra='''torch=types.ModuleType('torch')
exec("def compile(fn):\\n def wrapped(x): return fn(x)\\n return wrapped\\n",torch.__dict__)
sys.modules['torch']=torch
alias=torch.compile
def test_one():
    compiled=alias(m.api)
    assert compiled(1)==2
    assert m.api(2)==3
'''
    result=launch(tmp_path,PRELUDE+extra,'--callchain-out='+str(out),'--callchain-mode=objects','--callchain-namespaces=fixture_api,torch')
    assert result.returncode==0,result.stdout+result.stderr
    from trace_api_chain.pipeline import prepare
    prepare(out,'objects')
    graph=json.loads((out/'graph.json').read_text())
    assert [n['label'] for n in graph['nodes'].values()]==['api']
    assert {e['api_id'] for e in graph['exclusions']}=={'python:torch.compile','python:torch.compile_runtime'}


def test_profile_replacement_is_not_success(tmp_path):
    out=tmp_path/'capture'
    result=launch(tmp_path,PRELUDE+'import cProfile\ndef test_one():\n m.api(1)\n p=cProfile.Profile();p.enable();m.api(2);p.disable()\n',
                  '--callchain-out='+str(out),'--callchain-mode=objects','--callchain-namespaces=fixture_api')
    assert result.returncode!=0
    report=json.loads((out/'capture-result.json').read_text())
    assert 'hook_replaced' in report['gaps']
    assert (out/'events.jsonl').is_file()


def test_test_failure_preserved_with_evidence(tmp_path):
    out=tmp_path/'capture'
    result=launch(tmp_path,PRELUDE+'def test_one():\n assert m.api(1)==999\n',
                  '--callchain-out='+str(out),'--callchain-mode=objects','--callchain-namespaces=fixture_api')
    assert result.returncode==1,result.stdout+result.stderr
    assert json.loads((out/'pytest-receipt.json').read_text())['pytest_exit_code']==1
    assert not (out/'tree/C03.html').exists()


def test_worker_startup_failure_reports_exit_code_instead_of_missing_receipt(tmp_path):
    (tmp_path/'conftest.py').write_text("import os\ndef pytest_configure(config):\n if os.environ.get('TRACE_API_CHAIN_INTERNAL')=='1':raise RuntimeError('worker startup failed')\n")
    out=tmp_path/'capture'
    result=launch(tmp_path,'def test_one():pass\n','--callchain-out='+str(out),'--callchain-mode=objects')
    assert result.returncode!=0
    report=json.loads((out/'capture-result.json').read_text())
    assert report['error_type']=='RuntimeError'
    assert 'capture_worker_exited_before_receipt: exit_code=3' in report['error']
    assert 'worker startup failed' in result.stdout+result.stderr


def test_first_replay_reuses_the_validated_graph_and_frozen_inputs_stay_checked(tmp_path,monkeypatch):
    import pytest
    import trace_api_chain.pipeline as pipeline
    out=tmp_path/'capture'
    result=launch(tmp_path,PRELUDE+'def test_one():\n m.api(1)\n','--callchain-out='+str(out),'--callchain-mode=objects','--callchain-namespaces=fixture_api')
    assert result.returncode==0,result.stdout+result.stderr
    original=pipeline.rebuild;calls=[]
    def observed(*args,**kwargs):
        assert kwargs=={'reuse_reference_batches':True}
        calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(pipeline,'rebuild',observed)
    first=pipeline.prepare(out,'objects')['graph']
    assert len(calls)==1
    second=pipeline.prepare(out,'objects')['graph']
    assert first==second and len(calls)==2
    with (out/'evidence/python.jsonl').open('a') as stream:stream.write('\n')
    with pytest.raises(ValueError,match='frozen_evidence_changed'):pipeline.prepare(out,'objects')
    assert len(calls)==2
