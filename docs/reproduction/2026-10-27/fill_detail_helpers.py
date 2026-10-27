"""CPU fill investigation: no pytest runner, trace hooks, or profiler for timings."""
import collections
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback

import torch
import scenario_harness as h

torch.set_num_threads(1)
DTYPES = h.all_types_complex_float8_and(torch.half, torch.bool, torch.bfloat16, torch.chalf)
BOUND_DTYPES = (torch.uint8, torch.int8, torch.float8_e4m3fn,
                torch.float8_e4m3fnuz, torch.float8_e5m2, torch.float8_e5m2fnuz)
CLOCK = time.perf_counter_ns


def emit(value):
    print('FILL_JSON ' + json.dumps(value), flush=True)


def original():
    with h.prepared_case('C03_fill__cpu') as (_, method):
        start = CLOCK()
        method()
        return {'total_ns': CLOCK() - start}


def split():
    """Same dtype/shape/value order and assertions as the upstream method.

    The original assertion argument is split into its Python list and tensor
    creation, then comparison. Explicit del preserves temporary lifetime;
    cleanup is timed separately. Timers add overhead, assessed against original.
    """
    groups = []
    with h.prepared_case('C03_fill__cpu') as (_, method):
        case = method.__self__
        started = CLOCK()
        for dt in DTYPES:
            start = CLOCK()
            small = torch.tensor((10, 10), dtype=dt, device='cpu')
            small_ns = CLOCK() - start
            start = CLOCK()
            large = torch.empty(10000, dtype=dt, device='cpu')
            large_ns = CLOCK() - start
            for x, input_ns in ((small, small_ns), (large, large_ns)):
                row = collections.Counter(input_create_ns=input_ns)
                numel = x.numel()
                bound = 100 if dt in BOUND_DTYPES else 2000
                for n in range(-bound, bound, bound // 10):
                    start = CLOCK()
                    x.fill_(n)
                    row['fill_ns'] += CLOCK() - start
                    start = CLOCK()
                    values = [n] * numel
                    row['expected_list_ns'] += CLOCK() - start
                    start = CLOCK()
                    expected = torch.tensor(values, dtype=dt, device='cpu')
                    row['expected_tensor_ns'] += CLOCK() - start
                    start = CLOCK()
                    del values
                    row['cleanup_ns'] += CLOCK() - start
                    start = CLOCK()
                    case.assertEqual(x, expected)
                    row['tensor_assert_ns'] += CLOCK() - start
                    start = CLOCK()
                    del expected
                    row['cleanup_ns'] += CLOCK() - start
                    start = CLOCK()
                    case.assertEqual(dt, x.dtype)
                    row['dtype_assert_ns'] += CLOCK() - start
                    row['calls'] += 1
                groups.append({'dtype': str(dt), 'numel': numel, **row})
            del x, small, large
        total = CLOCK() - started
    sums = {k: sum(g.get(k, 0) for g in groups) for k in groups[0] if k.endswith('_ns')}
    sums['other_ns'] = total - sum(sums.values())
    return {'total_ns': total, 'stages': sums, 'groups': groups,
            'fill_calls': sum(g['calls'] for g in groups)}


def micro_fill(repeats):
    rows = []
    for dt in DTYPES:
        for size in (2, 10000):
            x = torch.empty(size, dtype=dt, device='cpu')
            bound = 100 if dt in BOUND_DTYPES else 2000
            values = tuple(range(-bound, bound, bound // 10))
            for n in values:
                x.fill_(n)
            start = CLOCK()
            for _ in range(repeats):
                for n in values:
                    x.fill_(n)
            elapsed = CLOCK() - start
            rows.append({'dtype': str(dt), 'numel': size, 'calls': repeats * len(values),
                         'total_ns': elapsed, 'ns_per_call': elapsed / (repeats * len(values))})
    return {'rows': rows, 'repeats': repeats}


def assertion_detail():
    originals = {name: getattr(torch, name) for name in ('isclose', 'all')}
    stats = {}
    def wrap(name):
        def measured(*args, **kwargs):
            x = args[0]
            key = (name, str(x.dtype), x.numel(), repr(sorted(kwargs.items())))
            start = CLOCK()
            result = originals[name](*args, **kwargs)
            elapsed = CLOCK() - start
            row = stats.setdefault(key, {'operation': name, 'dtype': str(x.dtype),
                'numel': x.numel(), 'kwargs': repr(sorted(kwargs.items())), 'calls': 0, 'total_ns': 0})
            row['calls'] += 1
            row['total_ns'] += elapsed
            return result
        return measured
    try:
        for name in originals:
            setattr(torch, name, wrap(name))
        value = original()
    finally:
        for name, fn in originals.items():
            setattr(torch, name, fn)
    return {**value, 'operations': list(stats.values())}


def metadata():
    class DlInfo(ctypes.Structure):
        _fields_ = [('dli_fname', ctypes.c_char_p), ('dli_fbase', ctypes.c_void_p),
                    ('dli_sname', ctypes.c_char_p), ('dli_saddr', ctypes.c_void_p)]
    process = ctypes.CDLL(None)
    info = DlInfo()
    process.dladdr.argtypes = [ctypes.c_void_p, ctypes.POINTER(DlInfo)]
    process.dladdr.restype = ctypes.c_int
    hook = ctypes.cast(process.__cyg_profile_func_enter, ctypes.c_void_p)
    process.dladdr(hook, ctypes.byref(info))
    maps = Path('/proc/self/maps').read_text().splitlines()
    trace_maps = [s for s in maps if 'trace_api_chain' in s or 'callchain' in s]
    assert not trace_maps, trace_maps
    assert not any(s.startswith('trace_api_chain') for s in sys.modules)
    assert not os.environ.get('LD_PRELOAD')
    return {'torch_version': torch.__version__, 'torch_file': torch.__file__,
            'torch_threads': torch.get_num_threads(), 'affinity': sorted(os.sched_getaffinity(0)),
            'LD_PRELOAD': os.environ.get('LD_PRELOAD'), 'trace_maps': trace_maps,
            'hook_provider': info.dli_fname.decode() if info.dli_fname else None,
            'dtype_names': [str(x) for x in DTYPES],
            'reference_sha256': hashlib.sha256((h.REFERENCE/'test/test_tensor_creation_ops.py').read_bytes()).hexdigest()}
