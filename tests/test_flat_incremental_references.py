"""Changed positions must retain exactly the full observer's object evidence."""
import json
import sys
import builtins
from trace_api_chain.object_capture import Recorder


def test_incremental_flat_leaves_preserve_all_object_and_reference_rows(tmp_path,monkeypatch):
    fast=Recorder(tmp_path/'fast.jsonl','test::one',tmp_path/'test.py')
    full=Recorder(tmp_path/'full.jsonl','test::one',tmp_path/'test.py')
    full.incremental_flat_snapshot=lambda *args:None
    for r in (fast,full):r.stack=[{'uid':1}]
    values=[None]*180
    def observe():
        for r in (fast,full):r.snapshot(sys._getframe(),'locals',{'values':values,'alias':values})
    observe()
    for index in range(45):
        values[index]='new-name-'+str(index);observe()
    for index in range(12):
        values.append('new-tail-'+str(index));observe()
    values.extend(['new-batch-a','new-batch-b']);observe()
    values.reverse();observe()
    del values[35:];observe()
    values[0]={'new_branch':[]};observe()
    values[0]['new_branch'].append(object());observe()
    values[0]=False;observe()
    monkeypatch.setattr(builtins,'__file__','different-builtins-source.py',raising=False);observe()
    values[1]='after-owner-change';observe()
    for r in (fast,full):r.file.close()
    def rows(name):return [json.loads(line) for line in (tmp_path/name).read_text().splitlines()][1:]
    assert rows('fast.jsonl')==rows('full.jsonl')
    assert fast.flat_incremental_updates>=45
    assert fast.flat_unchanged_leaves_reused>7000
    assert fast.metadata_cache_hits<full.metadata_cache_hits
    assert not fast.gaps and not full.gaps


def test_current_root_state_replaces_old_cache_versions_without_losing_evidence(tmp_path):
    r=Recorder(tmp_path/'history.jsonl','test::one',tmp_path/'test.py');r.stack=[{'uid':1}]
    values=list(range(200))
    for index in range(80):
        values[index]='new-'+str(index)
        r.snapshot(sys._getframe(),'locals',{'values':values})
    assert len(r.snapshot_cache)==1
    assert len(r.root_states)==1
    assert len(r.snapshot_cache_roots)==1
    assert r.snapshot_cache_bytes==sum(r.snapshot_cache_costs.values())
    r.file.close()
    rows=[json.loads(line) for line in (tmp_path/'history.jsonl').read_text().splitlines()]
    added=[edge for row in rows if row['event']=='references' for edge in row['added']]
    assert len(added)>200
    assert not r.gaps
