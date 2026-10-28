import json
import sys

from trace_api_chain.object_capture import Recorder


def test_reference_to_recorder_kept_without_recursively_recording_its_evidence(tmp_path):
    recorder=Recorder(tmp_path/'events.jsonl','test::one',tmp_path/'test.py')
    recorder.stack=[{'uid':1}]
    workload={'collector':recorder,'data':[1,2,3]}
    recorder.snapshot(sys._getframe(),'entry',{'holder':workload})
    recorder.file.close()
    rows=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    observer=next(row['id'] for row in rows if row['event']=='object' and row.get('type',{}).get('qualname')=='Recorder')
    refs=[ref for row in rows if row['event']=='references' for ref in row['added']]
    assert any(ref[2]==observer for ref in refs)
    assert not any(ref[0]==observer for ref in refs)
    assert any(ref[1]=='item:2' for ref in refs)
    assert not recorder.gaps
    assert any('observer_state_not_traversed' in row.get('notes',[]) for row in rows)


def test_only_registered_identity_is_an_observer_boundary(tmp_path):
    class State:
        def __init__(self):self.values=[10,20]
    observer=State();workload=State()
    recorder=Recorder(tmp_path/'events.jsonl','test::one',tmp_path/'test.py',observer_roots=[observer])
    recorder.stack=[{'uid':1}]
    recorder.snapshot(sys._getframe(),'entry',{'observer':observer,'workload':workload})
    recorder.file.close()
    rows=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    refs=[ref for row in rows if row['event']=='references' for ref in row['added']]
    roots={role:target for parent,role,target in refs if parent is None}
    assert not any(parent==roots['root:observer'] for parent,_,_ in refs)
    assert any(parent==roots['root:workload'] and role=='attribute:values' for parent,role,_ in refs)
