"""Observation errors must not corrupt the actual call/return stack."""
import json
import sys
import types
from pathlib import Path

import pytest

from trace_api_chain.catalog import Registry
from trace_api_chain.object_capture import Recorder
from trace_api_chain.replay import rebuild
import trace_api_chain.replay as replay_module


SOURCE='''def helper(value):
    return value + 1
def api():
    for i in range(3):
        helper(i)
        try:
            [][0]
        except IndexError:
            pass
        try:
            int('not-an-integer')
        except ValueError:
            pass
        try:
            [].index(1)
        except ValueError:
            pass
        len([])
'''


def capture_fault(tmp_path, configure):
    path=tmp_path/'fault_api.py';path.write_text(SOURCE)
    module=types.ModuleType('fault_api');module.__file__=str(path)
    exec(compile(SOURCE,str(path),'exec'),vars(module));sys.modules['fault_api']=module
    recorder=Recorder(tmp_path/'events.jsonl','test::one',tmp_path/'test.py',namespaces=('fault_api',))
    configure(recorder)
    recorder.begin()
    try:module.api()
    finally:recorder.end();sys.modules.pop('fault_api',None)
    events=[json.loads(x) for x in (tmp_path/'events.jsonl').read_text().splitlines()]
    contract=json.loads(Path(replay_module.__file__).with_name('contract.json').read_text())
    graph=rebuild(events,[],contract)
    assert not recorder.stack
    assert 'call_return_mismatch' not in recorder.gaps
    assert 'unclosed_calls' not in recorder.gaps
    assert graph['status']=='evidence_scope_incomplete'
    assert all(x['returned'] is not None for x in graph['nodes'].values())
    assert sum(e['event']=='call' for e in events)==sum(e['event']=='return' for e in events)
    return recorder,events


@pytest.mark.parametrize('phase',['entry','native_entry','exit','return'])
def test_failed_snapshot_never_loses_the_matching_return(tmp_path,phase):
    def configure(recorder):
        original=recorder.snapshot
        def snapshot(frame,event,values=None):
            if event==phase:raise ValueError('forced observation failure')
            return original(frame,event,values)
        recorder.snapshot=snapshot
    recorder,events=capture_fault(tmp_path,configure)
    assert 'collector_error:ValueError' in recorder.gaps
    errors=[e for e in events if e['event']=='gap' and e['reason']=='collector_error:ValueError']
    assert len(errors)==1 and errors[0]['traceback']


@pytest.mark.parametrize('method',['match','excluded'])
def test_failed_api_matching_keeps_internal_call_structure(tmp_path,method):
    def configure(recorder):
        original=getattr(recorder.registry,method)
        def fail(frame,*args):
            if frame.f_code.co_name=='helper':raise TypeError('forced classification failure')
            return original(frame,*args)
        setattr(recorder.registry,method,fail)
    recorder,events=capture_fault(tmp_path,configure)
    helpers=[e for e in events if e['event']=='call' and e['name']=='helper']
    assert len(helpers)==3
    assert 'collector_error:TypeError' in recorder.gaps


@pytest.mark.parametrize('method',['begin','end'])
def test_failed_native_bookkeeping_never_poison_python_stack(tmp_path,method):
    class Native:
        storage='events'
        def __init__(self):self.stack=[];self.failed=False
        def begin(self,uid,identity):
            self.stack.append(uid)
            if method=='begin' and not self.failed:self.failed=True;raise OSError('forced begin failure')
        def end(self,uid):
            assert self.stack.pop()==uid
            if method=='end' and not self.failed:self.failed=True;raise OSError('forced end failure')
    native=Native()
    recorder,events=capture_fault(tmp_path,lambda recorder:setattr(recorder,'native',native))
    assert native.failed and not native.stack
    assert 'collector_error:OSError' in recorder.gaps


def test_real_return_mismatch_is_reported_without_inventing_unwind(tmp_path):
    recorder=Recorder(tmp_path/'events.jsonl','test::one',tmp_path/'test.py')
    frame=sys._getframe();recorder.stack=[{'uid':1,'kind':'python','frame':id(frame)+1,'callable':None}]
    recorder.profile(frame,'return',None)
    assert len(recorder.stack)==1
    assert 'call_return_mismatch' in recorder.gaps
    recorder.file.close()
    error=json.loads((tmp_path/'events.jsonl').read_text().splitlines()[-1])
    assert error['expected_frame']!=error['observed_frame']


def test_failed_definition_observation_still_records_the_frame(tmp_path):
    def configure(recorder):
        original=recorder.code
        def code(value,module):
            if value.co_name=='helper':raise ValueError('forced code lookup failure')
            return original(value,module)
        recorder.code=code
    recorder,events=capture_fault(tmp_path,configure)
    helpers=[e for e in events if e['event']=='call' and e['name']=='helper']
    assert len(helpers)==3 and all(e['definition'] is None for e in helpers)
    assert 'collector_error:ValueError' in recorder.gaps
