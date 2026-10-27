from pathlib import Path
import datetime,json,math,os,queue,statistics,subprocess,threading,time,traceback

R=Path('/home/workspace-ycy/compile-performance');D=R/'runtime-scenarios-c03-c05'
OUT=D/'exclude-std-only-2026-10-27'
CASES={'baseline':'baseline-r7-lld22','full':'instrumented-r7-lld22','std_excluded':'instrumented-r9-lld22-exclude-std'}
ORDER=tuple(CASES)
result={'protocol':{'variants':CASES,'scenario_rounds':9,'warmups':3,'split_rounds':5,'micro_rounds':7,
 'order':'Rotate three variant positions each round; serial measurements; one Torch/OMP/MKL thread; NPU synchronized.',
 'scope':'Only GCC standard-library paths excluded in std_excluded. No plugin or preload during all timing measurements.',
 'split_scope':'680 original fills across 17 dtypes and two sizes; separately time input, expected list, expected Tensor, fill, assertions, cleanup.',
 'micro_scope':'Preallocated Tensor, 20 original scalar values repeated 20 times; timer outside loop.'},'correctness':{},'environment':{},'scenario_raw':[],'detail_raw':[]}
workers={}

def atomic(name,value):
 p=OUT/name;temp=p.with_suffix(p.suffix+'.tmp');temp.write_text(json.dumps(value,indent=2));temp.replace(p)
def state(stage,**extra):
 atomic('postbuild-status.json',{'stage':stage,'updated_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),**extra})
 atomic('experiment-results.json',result)

class Worker:
 def __init__(self,variant,device):
  env=os.environ.copy()
  for k in ('LD_PRELOAD','LD_AUDIT','SCENARIO_API_FILTER','TRACE_API_CHAIN_INTERNAL'):env.pop(k,None)
  env.update(PYTHONPATH=str(D),SCENARIO_DEVICE=device,PYTORCH_TESTING_DEVICE_ONLY_FOR=device,
   TORCH_DEVICE_BACKEND_AUTOLOAD='0',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',PYTORCH_PRINT_REPRO_ON_FAILURE='0')
  self.queue=queue.Queue();self.log=(OUT/(variant+'-'+device+'-worker.log')).open('w')
  self.p=subprocess.Popen([str(R/'cases'/CASES[variant]/'venv311/bin/python'),'-u',str(OUT/'std_only_experiment_worker.py')],
   cwd=OUT,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,bufsize=1)
  def read():
   for line in self.p.stdout:
    if line.startswith('EXPERIMENT_JSON '):self.queue.put(json.loads(line[len('EXPERIMENT_JSON '):]))
    else:self.log.write(line);self.log.flush()
   self.queue.put({'status':'failed','traceback':'Worker exited '+str(self.p.poll())})
  threading.Thread(target=read,daemon=True).start()
 def receive(self):
  v=self.queue.get(timeout=900)
  if v.get('status')=='failed':raise RuntimeError(v['traceback'])
  return v
 def call(self,**cmd):
  self.p.stdin.write(json.dumps(cmd)+'\n');self.p.stdin.flush();return self.receive()
 def stop(self):
  if self.p.poll() is None:
   self.p.stdin.write('{"mode":"stop"}\n');self.p.stdin.flush()
   try:self.p.wait(timeout=30)
   except subprocess.TimeoutExpired:self.p.terminate()
  self.log.close()

try:
 state('waiting_for_build')
 while True:
  p=OUT/'build-status.json';status=json.loads(p.read_text()) if p.exists() else {}
  if status.get('status')=='complete':break
  if status.get('status') in ('failed','stopped_for_scope_discussion'):raise RuntimeError(status)
  time.sleep(10)
 result['build_results']=json.loads((OUT/'build-results.json').read_text())
 assert all(x['status']=='success' and x['flags_verified'] and x['invocations_with_exclusion']==x['compiler_invocations'] for x in result['build_results'])
 # Effective package versions must match, excluding the two rebuilt libraries.
 packages={}
 code="import importlib.metadata as m,json;print(json.dumps({d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions() if d.metadata['Name'].lower().replace('_','-') not in ('torch','torch-npu')}))"
 for v,case in CASES.items():
  packages[v]=json.loads(subprocess.check_output([str(R/'cases'/case/'venv311/bin/python'),'-c',code],text=True))
 assert packages['baseline']==packages['full']==packages['std_excluded']
 result['dependency_versions']=packages['baseline'];result['dependency_versions_match']=True
 for device in ('cpu','npu'):
  for v in ORDER:
   state('starting_worker',variant=v,device=device)
   w=Worker(v,device);workers[v,device]=w
   result['environment'][v+'/'+device]=w.receive()
 for device in ('cpu','npu'):
  for v in ORDER:
   state('correctness',variant=v,device=device)
   result['correctness'][v+'/'+device]=workers[v,device].call(mode='correctness')['rows']
 comparable={}
 for device in ('cpu','npu'):
  statuses=[{x['id']:x['status'] for x in result['correctness'][v+'/'+device]} for v in ORDER]
  assert statuses[0]==statuses[1]==statuses[2],statuses
  comparable[device]=sorted(k for k,value in statuses[0].items() if value=='passed')
 assert sum(map(len,comparable.values()))==17,comparable
 result['comparable_cases']=comparable
 batches={};warmups={}
 for device in ('cpu','npu'):
  batches[device]={}
  for v in ORDER:
   state('scenario_warmup',variant=v,device=device)
   value=workers[v,device].call(mode='scenario_warmup',cases=comparable[device]);warmups[v+'/'+device]=value
   assert all(x['status']=='passed' for x in value['rows'])
   if v=='baseline':
    for x in value['rows']:batches[device][x['id']]=max(1,min(50,math.ceil(.03/max(x['calibration_seconds'],1e-8))))
 result['warmups']=warmups;result['batches']=batches
 for turn in range(9):
  order=ORDER[turn%3:]+ORDER[:turn%3]
  for device in ('cpu','npu'):
   for v in order:
    state('scenario_measure',round=turn+1,variant=v,device=device)
    value=workers[v,device].call(mode='scenario_measure',cases=comparable[device],batches=batches[device])
    assert all(x['status']=='passed' for x in value['rows'])
    result['scenario_raw'].append({'variant':v,'device':device,'round':turn,**value})
 summary=[]
 for device,ids in comparable.items():
  for ident in ids:
   row={'id':ident,'device':device}
   for v in ORDER:
    samples=[x['seconds_per_call'] for r in result['scenario_raw'] if r['variant']==v and r['device']==device for x in r['rows'] if x['id']==ident]
    assert len(samples)==9
    row[v]={'median_seconds':statistics.median(samples),'min_seconds':min(samples),'max_seconds':max(samples),
     'cv_percent':statistics.stdev(samples)/statistics.mean(samples)*100,'samples':samples,'calls_per_round':batches[device][ident]}
   row['std_vs_baseline']=row['std_excluded']['median_seconds']/row['baseline']['median_seconds']
   row['std_vs_full']=row['std_excluded']['median_seconds']/row['full']['median_seconds']
   summary.append(row)
 result['scenario_summary']=summary;state('scenario_complete')
 for mode,count in (('split_warmup',1),('split',5),('micro_fill',7),('assertion_detail',3)):
  for turn in range(count):
   order=ORDER[turn%3:]+ORDER[:turn%3]
   for v in order:
    state('fill_detail',mode=mode,round=turn+1,variant=v)
    value=workers[v,'cpu'].call(mode='split' if mode=='split_warmup' else mode)
    result['detail_raw'].append({'variant':v,'phase':mode,'round':turn,**value})
 detail_summary={}
 for v in ORDER:
  rows=[x for x in result['detail_raw'] if x['variant']==v and x['phase']=='split']
  s={'stages_ns':{k:statistics.median(x['stages'][k] for x in rows) for k in rows[0]['stages']},
   'split_total_ns':statistics.median(x['total_ns'] for x in rows),'fill_calls':rows[0]['fill_calls']}
  s['groups']=[{**{k:g[k] for k in ('dtype','numel','calls')},**{k:statistics.median(x['groups'][i][k] for x in rows) for k in g if k.endswith('_ns')}} for i,g in enumerate(rows[0]['groups'])]
  rows=[x for x in result['detail_raw'] if x['variant']==v and x['phase']=='micro_fill']
  s['micro_fill']=[{**{k:g[k] for k in ('dtype','numel','calls')},'ns_per_call':statistics.median(x['rows'][i]['ns_per_call'] for x in rows)} for i,g in enumerate(rows[0]['rows'])]
  rows=[x for x in result['detail_raw'] if x['variant']==v and x['phase']=='assertion_detail']
  s['assertion_detail']={name:statistics.median(sum(x['total_ns'] for x in r['operations'] if x['operation']==name) for r in rows) for name in ('isclose','all')}
  detail_summary[v]=s
 result['detail_summary']=detail_summary
 state('complete')
except BaseException:
 state('failed',traceback=traceback.format_exc());raise
finally:
 for w in workers.values():w.stop()
