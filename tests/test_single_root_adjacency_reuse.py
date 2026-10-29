import json
import sys
from types import SimpleNamespace

from trace_api_chain.object_capture import Recorder


def recorder(tmp_path, budget=100000):
    rec=Recorder(tmp_path/'events.jsonl','test::one',tmp_path/'test.py',
                 registry=SimpleNamespace(catalog=[]),native=SimpleNamespace(storage='paths'),
                 reference_deltas=True,max_references=budget)
    rec.stack=[{'uid':1}]
    return rec


def test_exact_single_root_borrows_adjacency_without_copying(tmp_path):
    rec=recorder(tmp_path);value={'nested':list(range(200))}
    rec.snapshot(sys._getframe(),'global_read',{'global:value':value})
    signature=rec.root_states[(id(value),id(type(value)))];adjacency=rec.snapshot_cache[signature][1]
    assert rec.views[(1,'global:global:value')][1] is adjacency
    assert rec.snapshot_cache_reference_counts[signature]==sum(map(len,adjacency.values()))
    rec.snapshot(sys._getframe(),'global_read',{'global:value':value})
    assert rec.views[(1,'global:global:value')][1] is adjacency
    rec.file.close()


def test_new_deep_reference_and_removal_still_change_evidence(tmp_path):
    rec=recorder(tmp_path);value={'nested':[]}
    rec.snapshot(sys._getframe(),'locals',{'value':value})
    first=rec.views[(1,'frame')][1];new={'further':[object()]};value['nested'].append(new)
    rec.snapshot(sys._getframe(),'locals',{'value':value})
    second=rec.views[(1,'frame')][1]
    assert second!=first
    assert any(str(id(new)) in token for token in second)
    value['nested'].clear();rec.snapshot(sys._getframe(),'locals',{'value':value})
    assert rec.views[(1,'frame')][1]!=second
    assert not rec.gaps
    rec.file.close()


def test_budget_exceeded_keeps_original_gap_and_partial_path(tmp_path):
    rec=recorder(tmp_path,10);value=list(range(40))
    rec.snapshot(sys._getframe(),'locals',{'value':value})
    assert 'reference_budget_exceeded' in rec.gaps
    rec.file.close()
    events=[json.loads(line) for line in (tmp_path/'events.jsonl').read_text().splitlines()]
    assert any(e['event']=='references' and 'reference_budget_exceeded' in e['notes'] for e in events)


def test_replacing_root_state_retires_its_cached_edge_count(tmp_path):
    rec=recorder(tmp_path);value={'nested':[]}
    rec.snapshot(sys._getframe(),'locals',{'value':value})
    previous=rec.root_states[(id(value),id(type(value)))];value['new']=object()
    rec.snapshot(sys._getframe(),'locals',{'value':value})
    current=rec.root_states[(id(value),id(type(value)))]
    assert current!=previous
    assert previous not in rec.snapshot_cache_reference_counts
    assert rec.snapshot_cache_reference_counts[current]==sum(map(len,rec.snapshot_cache[current][1].values()))
    rec.file.close()
