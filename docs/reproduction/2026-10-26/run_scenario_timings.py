import datetime
import json
import math
import os
import pathlib
import queue
import statistics
import subprocess
import threading
import time
import traceback

R = pathlib.Path('/home/workspace-ycy/compile-performance')
D = R / 'runtime-scenarios-c03-c05'
STATUS = D / 'timing-status.json'

def save(path, data):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, indent=2))
    temp.replace(path)

def state(stage, **values):
    save(STATUS, dict(status=stage, updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(), **values))

class Worker:
    def __init__(self, variant, device):
        self.variant, self.device = variant, device
        self.messages = queue.Queue()
        self.stderr = (D / (variant+'-'+device+'-timing.stderr.log')).open('w')
        self.stdout = (D / (variant+'-'+device+'-timing.stdout.log')).open('w')
        env = os.environ.copy()
        env.update(TORCH_DEVICE_BACKEND_AUTOLOAD='0', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1',
                   PYTORCH_TESTING_DEVICE_ONLY_FOR=device, SCENARIO_DEVICE=device,
                   PYTORCH_PRINT_REPRO_ON_FAILURE='0')
        self.process = subprocess.Popen([str(R/'cases'/(variant+'-r7-lld22')/'venv311/bin/python'),
            '-u', str(D/'scenario_timing_worker.py')], cwd=D, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr, text=True, bufsize=1)
        def read():
            for line in self.process.stdout:
                self.stdout.write(line); self.stdout.flush()
                if line.startswith('SCENARIO_JSON '):
                    self.messages.put(json.loads(line[len('SCENARIO_JSON '):]))
            self.messages.put({'worker_exited': self.process.poll()})
        threading.Thread(target=read, daemon=True).start()

    def receive(self):
        value = self.messages.get(timeout=600)
        if 'worker_exited' in value:
            raise RuntimeError(f'{self.variant}/{self.device} worker exited: {value}')
        return value

    def call(self, request):
        self.process.stdin.write(json.dumps(request)+'\n'); self.process.stdin.flush()
        return self.receive()

    def stop(self):
        if self.process.poll() is None:
            self.process.stdin.write('{"mode":"stop"}\n'); self.process.stdin.flush()
            try: self.process.wait(timeout=60)
            except subprocess.TimeoutExpired: self.process.terminate()
        self.stderr.close(); self.stdout.close()

workers = {}
try:
    state('starting_workers')
    comparable = {}
    for device in ('cpu', 'npu'):
        rows = {v: json.loads((D/(v+'-'+device+'-correctness.json')).read_text())
                for v in ('baseline', 'instrumented')}
        passed = [{x['id'] for x in rows[v] if x['status']=='passed'} for v in rows]
        comparable[device] = sorted(passed[0] & passed[1])
        for variant in ('baseline', 'instrumented'):
            workers[variant, device] = Worker(variant, device)
    versions = {}
    for key, worker in workers.items():
        versions['/'.join(key)] = worker.receive()
    warmups, batches = {}, {}
    for device in ('cpu', 'npu'):
        batches[device] = {}
        for variant in ('baseline', 'instrumented'):
            state('warming_up', variant=variant, device=device)
            value = workers[variant, device].call({'mode':'warmup', 'cases':comparable[device]})
            warmups['/'.join((variant, device))] = value
            if any(x['status']!='passed' for x in value['rows']):
                raise RuntimeError('A previously passing case failed during warmup')
            if variant == 'baseline':
                for row in value['rows']:
                    batches[device][row['id']] = max(1, min(50, math.ceil(.03/max(row['calibration_seconds'], 1e-8))))
    raw = []
    for round_no in range(9):
        order = ('baseline','instrumented') if round_no%2==0 else ('instrumented','baseline')
        for device in ('cpu','npu'):
            for variant in order:
                state('measuring', round=round_no+1, rounds=9, variant=variant, device=device)
                value = workers[variant, device].call({'mode':'measure', 'round':round_no,
                    'cases':comparable[device], 'batches':batches[device]})
                raw.append({'variant':variant,'device':device,'round':round_no,**value})
                save(D/'timing-raw.json',raw)
    summary = []
    for device, cases in comparable.items():
        for ident in cases:
            by = {}
            for variant in ('baseline','instrumented'):
                samples = [x['seconds_per_call'] for result in raw if result['variant']==variant and result['device']==device
                           for x in result['rows'] if x['id']==ident and x['status']=='passed']
                if len(samples)!=9:raise RuntimeError('Missing successful timing samples for '+ident)
                by[variant] = {'median_seconds':statistics.median(samples), 'min_seconds':min(samples),
                    'max_seconds':max(samples),'round_means_seconds':samples,'calls_per_round':batches[device][ident]}
            a,b = by['baseline']['median_seconds'],by['instrumented']['median_seconds']
            summary.append({'id':ident,'device':device,**by,'ratio':b/a,'overhead_percent':(b/a-1)*100})
    result = {'status':'success','comparisons':summary,'comparable_cases':comparable,'warmups':warmups,
        'worker_versions':versions,'rounds':9,'scope':'Original selected test-method body including input creation and assertions; excludes imports, class setup, per-test setUp/tearDown. NPU synchronized before and after every call. No trace plugin enabled.',
        'protocol':'Three warmups; same baseline-calibrated batch size in both variants; nine batch means; alternate variant order each round; one operation stream at a time; torch/OMP/MKL CPU threads=1. Median of nine batch means is reported.'}
    save(D/'runtime-comparison.json',result);state('success',comparisons=len(summary))
except BaseException as error:
    state('failed',error=str(error),traceback=traceback.format_exc());raise
finally:
    for worker in workers.values():worker.stop()
