import json
from types import SimpleNamespace

from trace_api_chain.native import Bridge, save


def bridge():
    value = Bridge.__new__(Bridge)
    value.storage = 'paths'
    value.stack = []
    value.contexts = {}
    value.active = None
    value.segments = []
    value.gaps = []
    value.path_receipts = {}
    value.window_activations = 0
    value.path_failed = False
    counter = [0]
    status = [0]
    value.hook = SimpleNamespace(trace_paths_event_count=lambda: counter[0],
                                 trace_paths_begin=lambda context: 0,
                                 trace_paths_pause=lambda: status[0],
                                 trace_paths_failure=lambda: status[0])
    return value, counter, status


def test_context_receipts_preserve_counts_first_owner_and_distinct_branches():
    value, counter, _ = bridge()
    for uid in range(1, 1001):
        context = 1 if uid % 2 else 2
        value.begin(uid, None, context=context)
        counter[0] += uid * 2
        value.end(uid)
    assert not value.gaps
    assert len(value.segments) == 2
    assert [r['uid'] for r in value.segments] == [1, 2]
    assert [r['ordinal'] for r in value.segments] == [0, 1]
    assert [r['activations'] for r in value.segments] == [500, 500]
    assert sum(r['events'] for r in value.segments) == counter[0]
    assert value.window_activations == 1000
    assert value.contexts == {}


def test_nested_windows_keep_parent_context_until_actual_return():
    value, counter, _ = bridge()
    value.begin(1, None, context=10)
    counter[0] += 2
    value.begin(2, None, context=20)
    counter[0] += 4
    value.end(2)
    assert value.stack == [1] and value.contexts == {1: 10}
    counter[0] += 6
    value.end(1)
    assert value.segments == [
        {'uid': 1, 'file': None, 'ordinal': 0, 'context': 10, 'events': 8,
         'stop_status': 0, 'activations': 2},
        {'uid': 2, 'file': None, 'ordinal': 1, 'context': 20, 'events': 4,
         'stop_status': 0, 'activations': 1}]


def test_window_failure_is_retained_in_aggregate_and_gap():
    value, counter, status = bridge()
    value.begin(1, None, context=10)
    counter[0] += 2
    status[0] = 32
    value.end(1)
    assert value.segments[0]['stop_status'] == 32
    assert 'native_return_function_or_window_mismatch' in value.gaps
    assert value.path_failed


def test_compact_serialization_preserves_every_receipt_field(tmp_path):
    value = {'segments': [{'uid': i, 'events': i * 2, 'name': '对象'} for i in range(50)],
             'gaps': [], 'module_stamps': {'library': [1, 2, 3]}}
    pretty, compact = tmp_path / 'pretty.json', tmp_path / 'compact.json'
    save(pretty, value)
    save(compact, value, compact=True)
    assert json.loads(pretty.read_text()) == json.loads(compact.read_text()) == value
    assert compact.stat().st_size < pretty.stat().st_size
