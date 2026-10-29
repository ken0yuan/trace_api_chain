import io
import json

import pytest

from trace_api_chain.reference_transport import _PrivateEventReader,normalize_events,read_events
from trace_api_chain.replay import EvidenceError


def encoded(rows):
    return b''.join(json.dumps(row,separators=(',',':')).encode()+b'\n' for row in rows)


def snapshots():
    rows=[{'event':'start','reference_policy':'shared-adjacency-states/2','reference_transport':'deferred-adjacency/1'}]
    for uid in range(1,12):
        rows.append({'event':'call','uid':uid})
        child='new' if uid==6 else 'old'
        snapshot={'event':'reference_snapshot','uid':uid,'phase':'line','names':['x'],
                  'roots':[[None,'root:x','x']],'parts':[{'rows':[['x',[['x','child',child]]]]}],'notes':[]}
        rows.extend([snapshot,dict(snapshot,phase='return'),{'event':'return','uid':uid}])
    rows.append({'event':'end'})
    return rows


def test_private_cache_keeps_uid_phase_and_every_changed_child(tmp_path):
    rows=snapshots();payload=encoded(rows);reader=_PrivateEventReader(io.BytesIO(payload));parsed=list(reader)
    assert parsed==rows and reader.hits>=18
    assert parsed[2]['parts'] is parsed[3]['parts']
    assert parsed[2]['uid']!=parsed[6]['uid']
    path=tmp_path/'events.jsonl';path.write_bytes(payload)
    assert read_events(path)==normalize_events(rows)


@pytest.mark.parametrize('line',[
    b'{"phase":"line","event":"reference_snapshot","uid":1,"names":[],"roots":[],"parts":[],"notes":[]}\n',
    b'{ "event": "reference_snapshot", "uid": 1, "phase": "line", "names": [], "roots": [], "parts": [], "notes": []}\n',
    b'{"event":"reference_snapshot","uid":1,"phase":"line","names":[],"roots":[],"parts":[],"notes":[],"uid":2}\n',
    b'{"event":"reference_snapshot","uid":1,"phase":"line","names":[],"roots":[],"parts":[],"notes":[],"names":["last"]}\n',
])
def test_unfamiliar_or_duplicate_keys_equal_standard_json(line):
    assert list(_PrivateEventReader(io.BytesIO(line)))==[json.loads(line)]


def test_cache_eviction_retains_every_record():
    rows=snapshots();reader=_PrivateEventReader(io.BytesIO(encoded(rows)),limit=1)
    assert list(reader)==rows and not reader.cache and reader.hits==0


def test_invalid_duplicate_edges_still_rejected(tmp_path):
    rows=snapshots();rows[2]['parts'][0]['rows'][0][1].append(['x','child','old'])
    path=tmp_path/'bad.jsonl';path.write_bytes(encoded(rows))
    with pytest.raises(EvidenceError,match='duplicate_reference_block_edge'):read_events(path)


def test_familiar_malformed_json_is_not_repaired():
    line=b'{"event":"reference_snapshot","uid":1,"phase":"line","names":[],"roots":[],"parts":[],"notes":[],}\n'
    with pytest.raises(ValueError):list(_PrivateEventReader(io.BytesIO(line)))


def test_legacy_and_empty_streams_keep_complete_content(tmp_path):
    path=tmp_path/'legacy.jsonl';rows=[{'event':'start'},{'event':'object','id':'x'},{'event':'end'}]
    path.write_bytes(encoded(rows));assert read_events(path)==rows
    path.write_bytes(b'');assert read_events(path)==[]
