import json
import statistics
import sys
import time
import traceback
import unittest
from pathlib import Path
import torch
import scenario_harness as h
import fill_detail_helpers as f

torch.set_num_threads(1)

def emit(v):print('EXPERIMENT_JSON '+json.dumps(v),flush=True)
def once(ident):
    with h.prepared_case(ident) as (case,method):
        start=time.perf_counter_ns();method();h.synchronize(case)
        return (time.perf_counter_ns()-start)/1e9

libraries=sorted({line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines()
 if any(s in line.split()[-1].rsplit('/',1)[-1] for s in ('libtorch','libc10'))})
expected_venv=str(Path(sys.executable).parent.parent)
assert libraries and all(p.startswith(expected_venv+'/') for p in libraries),libraries
emit({'ready':True,'device':h.DEVICE,'cases':h.manifest(),'loaded_torch_libraries':libraries,**f.metadata()})
for line in sys.stdin:
    cmd=json.loads(line);mode=cmd['mode']
    if mode=='stop':break
    try:
        if mode in ('correctness','scenario_warmup','scenario_measure'):
            rows=[]
            for ident in cmd.get('cases',list(h.CASES)):
                row={'id':ident}
                try:
                    count=1 if mode=='correctness' else 3 if mode=='scenario_warmup' else cmd['batches'][ident]
                    values=[once(ident) for _ in range(count)]
                    row.update(status='passed',seconds=values,seconds_per_call=statistics.mean(values),
                        calibration_seconds=statistics.median(values[1:] or values))
                except unittest.SkipTest as e:row.update(status='skipped',error=str(e))
                except Exception as e:row.update(status='failed',error=str(e),traceback=traceback.format_exc())
                rows.append(row)
            emit({'status':'passed','mode':mode,'rows':rows})
        else:
            fn={'split':f.split,'micro_fill':lambda:f.micro_fill(20),'assertion_detail':f.assertion_detail}[mode]
            emit({'status':'passed','mode':mode,**fn()})
    except BaseException:emit({'status':'failed','traceback':traceback.format_exc()})
