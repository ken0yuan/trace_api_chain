import json
import statistics
import sys
import time
import traceback
import torch
import scenario_harness as h

torch.set_num_threads(1)

def emit(value):
    print('SCENARIO_JSON ' + json.dumps(value), flush=True)

def once(ident):
    with h.prepared_case(ident) as (case, method):
        started = time.perf_counter_ns()
        method()
        h.synchronize(case)
        return (time.perf_counter_ns() - started) / 1e9

emit({'ready': True, 'device': h.DEVICE, 'torch': torch.__version__, 'cases': h.manifest()})
for line in sys.stdin:
    command = json.loads(line)
    if command['mode'] == 'stop':
        break
    rows = []
    for ident in command['cases']:
        try:
            if command['mode'] == 'warmup':
                values = [once(ident) for _ in range(3)]
                row = {'id': ident, 'status': 'passed', 'warmup_seconds': values,
                       'calibration_seconds': statistics.median(values[1:])}
            else:
                values = [once(ident) for _ in range(command['batches'][ident])]
                row = {'id': ident, 'status': 'passed', 'iterations': len(values),
                       'seconds_per_call': statistics.mean(values), 'seconds': values}
        except Exception as error:
            row = {'id': ident, 'status': 'failed', 'error': str(error)[:1500],
                   'traceback': traceback.format_exc()}
        rows.append(row)
    emit({'mode': command['mode'], 'round': command.get('round'), 'rows': rows})
