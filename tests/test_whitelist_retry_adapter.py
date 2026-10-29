import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def adapter(tmp_path,monkeypatch):
    path=Path(__file__).parents[1]/'tools/whitelist_trace_driver_035.py'
    spec=importlib.util.spec_from_file_location('retry_adapter',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'OUT',tmp_path)
    monkeypatch.setattr(module,'env',lambda devices:{})
    monkeypatch.setattr(module,'resource_ok',lambda:True)
    submitted=[];module.PIPELINE=SimpleNamespace(submit=lambda path:submitted.append(path))
    return module,submitted


def test_repair_retry_preserves_original_evidence_and_unique_attempt_count(tmp_path,monkeypatch):
    d,submitted=adapter(tmp_path,monkeypatch)
    item={'id':'case-one','file':'test_one.py','nodeid':'test_one.py::test_one'}
    original=tmp_path/'captures/case-one/original.raw';original.parent.mkdir(parents=True);original.write_bytes(b'failed evidence')
    history=tmp_path/'retry-history/case-one/original.json'
    previous={**item,'capture':{'status':'failed'},'relations':{'state':'initialized'},'test_reports':[],
              'retry_pending':{'batch':'repair-20261029','history':str(history)}}
    d.write(history,previous);d.write(tmp_path/'results/case-one.json',previous);d.COUNTS.update(d.attempt_counts(previous));commands=[]
    def run(command,log,environment,timeout):
        commands.append(command)
        assert timeout==1920 and command[command.index('--callchain-timeout')+1]=='1800'
        assert 'trace_api_chain.pytest_plugin' in command and 'api_test_relations.pytest_plugin' in command
        cap=Path(command[command.index('--callchain-out')+1]);relations=Path(command[command.index('--api-relations-out')+1])
        d.write(cap/'capture-result.json',{'status':'captured','gaps':[]})
        d.write(cap/'pytest-receipt.json',{'started':1,'ended':2,'reports':[{'when':'call','outcome':'passed'}]})
        d.write(relations/'progress.json',{'state':'capture_finished','gaps':0})
        return {'returncode':0,'elapsed_seconds':1,'timeout':False}
    monkeypatch.setattr(d,'run',run)
    result=d.execute(item,'5')
    assert d.COUNTS['attempted']==1 and d.COUNTS['repair_retry_attempts']==1
    assert d.COUNTS['capture_failed']==0 and d.COUNTS['capture_captured']==1
    assert d.COUNTS['test_no_test_result']==0 and d.COUNTS['test_passed']==1
    assert result['previous_attempts']==[str(history)] and history.exists()
    assert original.read_bytes()==b'failed evidence'
    assert Path(result['capture_dir'])==tmp_path/'retry-attempts/repair-20261029/captures/case-one'
    assert d.execute(item,'5')==result and len(commands)==1 and len(submitted)==1


def test_complete_result_is_never_recollected_without_explicit_retry(tmp_path,monkeypatch):
    d,submitted=adapter(tmp_path,monkeypatch)
    result={'id':'one','dual_capture_valid':True,'capture':{'status':'captured'}}
    d.write(tmp_path/'results/one.json',result)
    monkeypatch.setattr(d,'run',lambda *args:pytest.fail('completed test rerun'))
    assert d.execute({'id':'one'},'0')==result and not submitted


def test_retry_rejects_escaping_or_unfinished_paths_without_overwriting(tmp_path,monkeypatch):
    d,_=adapter(tmp_path,monkeypatch)
    item={'id':'one'};prior={'retry_pending':{'batch':'../escape','history':'retained'}}
    d.write(tmp_path/'results/one.json',prior)
    with pytest.raises(ValueError,match='invalid_retry_batch'):d.execute(item,'0')
    prior['retry_pending']['batch']='safe';d.write(tmp_path/'results/one.json',prior)
    raw=tmp_path/'retry-attempts/safe/captures/one/raw';raw.parent.mkdir(parents=True);raw.write_bytes(b'new entities')
    with pytest.raises(ValueError,match='unfinished_retry_evidence_preserved'):d.execute(item,'0')
    assert raw.read_bytes()==b'new entities'


@pytest.mark.parametrize('policy',['shared-adjacency-states/1','shared-adjacency-states/2','reference-deltas/1'])
def test_older_raw_capture_uses_current_preparer_without_recollection(tmp_path,monkeypatch,policy):
    d,_=adapter(tmp_path,monkeypatch)
    cap=tmp_path/'captures/one';cap.mkdir(parents=True)
    raw=cap/'events.jsonl';raw.write_text(json.dumps({'event':'start','reference_policy':policy})+'\n')
    original=raw.read_bytes();target=tmp_path/'normalizations/one/refresh-plan.json'
    assert d.preparation_runtime(cap,target)==tmp_path/'plugin-runtime-035-private'
    assert raw.read_bytes()==original and not target.exists()


def test_existing_handoff_keeps_exact_frozen_reader_and_unknown_is_rejected(tmp_path,monkeypatch):
    d,_=adapter(tmp_path,monkeypatch)
    target=tmp_path/'normalizations/one/refresh-plan.json';target.parent.mkdir(parents=True)
    receipt=target.with_name(target.name+'.ready.json');receipt.write_text(json.dumps({'policy':d.LEGACY_POLICY}))
    cap=tmp_path/'captures/one'  # No raw access is needed to reuse the verified handoff.
    assert d.preparation_runtime(cap,target)==d.LEGACY_RUNTIME
    receipt.write_text(json.dumps({'policy':{'unrecognized':'must-not-ignore'}}))
    with pytest.raises(ValueError,match='unknown_frozen_handoff_policy'):d.preparation_runtime(cap,target)
