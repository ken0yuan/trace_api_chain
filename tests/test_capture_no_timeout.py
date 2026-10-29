import json
from types import SimpleNamespace

import pytest

from trace_api_chain.pytest_plugin import pytest_cmdline_main


@pytest.mark.parametrize('seconds,expected',[(0,None),(42,42)])
def test_capture_zero_disables_actual_worker_timeout(tmp_path,monkeypatch,seconds,expected):
    output=tmp_path/'capture'
    values={'--callchain-out':str(output),'--callchain-mode':'objects','--callchain-timeout':seconds,
            '--callchain-max-events':100,'--callchain-max-paths':100,'--callchain-max-references':100,
            '--callchain-native-storage':'paths'}
    config=SimpleNamespace(args=['test.py::test_case'],option=SimpleNamespace(collectonly=False),
                           invocation_params=SimpleNamespace(args=['test.py::test_case'],dir=tmp_path),
                           getoption=lambda key,default=None:values.get(key,default))
    monkeypatch.delenv('TRACE_API_CHAIN_INTERNAL',raising=False)
    def run(command,**kwargs):
        assert kwargs['timeout'] is expected or kwargs['timeout']==expected
        (output/'pytest-receipt.json').write_text(json.dumps({'started':True,'ended':True,'gaps':[]}))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr('trace_api_chain.pytest_plugin.subprocess.run',run)
    assert pytest_cmdline_main(config)==0
    report=json.loads((output/'capture-result.json').read_text())
    assert report['timeout_seconds']==expected and report['timeout_disabled']==(expected is None)
    assert report['finished_at']>=report['started_at']
