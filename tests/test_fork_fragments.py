"""Fork continuations retain raw evidence without corrupting their parent."""
import json
import os
import subprocess
import pytest
from test_pytest_entry import launch

pytestmark=pytest.mark.skipif(not hasattr(os,'fork'),reason='requires POSIX fork')

MODULE_CODE="""import os
def child_api():return 23
def fork_api():
 pid=os.fork()
 if pid==0:child_api()
 return pid
"""


def source(mode):
    program="""import os,sys,types,pytest
from pathlib import Path
module=types.ModuleType('fixture_api');path=Path(__file__).with_name('backend.py')
CODE_ASSIGNMENT
path.write_text(code);module.__file__=str(path);exec(compile(code,str(path),'exec'),vars(module));sys.modules['fixture_api']=module
def test_one():
 pid=module.fork_api()
 if pid==0:
  MODE
  return
 os.waitpid(pid,0)
 assert module.child_api()==23
"""
    return program.replace('CODE_ASSIGNMENT','code='+repr(MODULE_CODE)).replace('MODE',{
        'normal':'pass','pytest_exit':'pytest.exit("child finished",returncode=0)',
        'sys_exit':'sys.exit(0)','os_exit':'os._exit(0)'}[mode])


@pytest.mark.parametrize('mode',['normal','pytest_exit','sys_exit','os_exit'])
def test_fork_child_keeps_new_python_calls_in_an_unvalidated_fragment(tmp_path,mode):
    out=tmp_path/'capture'
    result=launch(tmp_path,source(mode),'--callchain-out='+str(out),'--callchain-mode=objects','--callchain-namespaces=fixture_api')
    assert result.returncode==0,result.stdout+result.stderr
    rows=[json.loads(line) for line in (out/'events.jsonl').read_text().splitlines()]
    assert sum(row['event']=='start' for row in rows)==1
    assert sum(row['event']=='end' for row in rows)==1
    owner=rows[0]['pid'];calls=[row for row in rows if row['event']=='call']
    assert len({row['uid'] for row in calls})==len(calls)
    assert {row['tid'] for row in calls}=={owner}
    from trace_api_chain.pipeline import prepare
    assert prepare(out,'objects')['graph']['gaps']==[]
    manifests=list((out/'fork-fragments').glob('*/fork-fragment.json'));assert len(manifests)==1
    manifest=json.loads(manifests[0].read_text());assert manifest['state']=='retained_unvalidated_fork_continuation'
    assert manifest['writer_pid']!=owner and manifest['selected_owner_pid']==owner
    child=[json.loads(line) for line in (manifests[0].parent/'events.jsonl').read_text().splitlines()]
    assert any(row['event']=='call' and row.get('name')=='child_api' and row['tid']==manifest['writer_pid'] for row in child)
    assert any(row['event']=='gap' and row.get('reason')=='fork_continuation_not_validated' for row in child)


def test_closed_parent_capture_is_not_overwritten_by_forked_teardown(tmp_path):
    out=tmp_path/'capture'
    body="""import os,pytest,types,sys
module=types.ModuleType('fixture_api');module.__file__=__file__
exec('def api():return 1',vars(module));sys.modules['fixture_api']=module
@pytest.fixture(autouse=True)
def teardown_fork():
 yield
 pid=os.fork()
 if pid==0:pytest.exit('teardown child',returncode=0)
 os.waitpid(pid,0)
def test_one():assert module.api()==1
"""
    result=launch(tmp_path,body,'--callchain-out='+str(out),'--callchain-mode=objects','--callchain-namespaces=fixture_api')
    assert result.returncode==0,result.stdout+result.stderr
    from trace_api_chain.pipeline import prepare
    assert prepare(out,'objects')['graph']['gaps']==[]
    receipt=json.loads((out/'pytest-receipt.json').read_text());assert not receipt.get('fork_fragment')
    assert any(row['when']=='teardown' and row['outcome']=='passed' for row in receipt['reports'])


@pytest.mark.parametrize('storage',['events','paths'])
@pytest.mark.parametrize('exit_mode',['normal','os_exit'])
def test_native_child_branch_is_retained_without_parent_window_pollution(tmp_path,storage,exit_mode):
    from test_compact_paths import setup_fixture
    setup_fixture(tmp_path)
    (tmp_path/'fixture_api.py').write_text('import os,native_fixture\ndef api_a(n):\n pid=os.fork()\n native_fixture.run(n,1 if pid==0 else 0)\n return pid\n')
    body='import os,fixture_api\ndef test_one():\n pid=fixture_api.api_a(1)\n if pid==0:CHILD_EXIT\n os.waitpid(pid,0)\n'.replace('CHILD_EXIT','return' if exit_mode=='normal' else 'os._exit(0)')
    out=tmp_path/'capture'
    result=launch(tmp_path,body,'--callchain-out='+str(out),'--callchain-native-storage='+storage,'--callchain-api-catalog='+str(tmp_path/'ledger.json'),'--callchain-namespaces=fixture_api')
    assert result.returncode==0,result.stdout+result.stderr
    from trace_api_chain.pipeline import prepare
    from trace_api_chain.compact import decode_paths
    from trace_api_chain.native import decode
    assert prepare(out)['graph']['gaps']==[]
    manifest=next((out/'fork-fragments').glob('*/fork-fragment.json'));child=manifest.parent
    native,modules,gaps=(decode_paths(child) if storage=='paths' else decode(child));assert 'fork_continuation_not_validated' in gaps
    binary=next(tmp_path.glob('native_fixture*.so'))
    symbols=subprocess.check_output(['nm','--defined-only',str(binary)],text=True)
    address=next(int(line.split()[0],16) for line in symbols.splitlines() if line.split()[-1]=='leaf_b')
    assert any(row['path']==str(binary) and row['offset']==address for row in native)
