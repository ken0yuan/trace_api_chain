from pathlib import Path
import sys,os,subprocess,json,time,traceback
R=Path('/home/workspace-ycy/compile-performance');D=R/'runtime-scenarios-c03-c05';device=sys.argv[1]
PY=R/'cases/instrumented-r7-lld22/venv311/bin/python'
env=os.environ.copy();env.update(PYTHONPATH=str(D/'plugin-candidate-root')+':'+str(R/'plugin-runtime'),
 SCENARIO_DEVICE=device,PYTORCH_TESTING_DEVICE_ONLY_FOR=device,TORCH_DEVICE_BACKEND_AUTOLOAD='0',
 PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',PYTORCH_PRINT_REPRO_ON_FAILURE='0')
for k in ('SCENARIO_API_FILTER','LD_PRELOAD','TRACE_API_CHAIN_INTERNAL'):env.pop(k,None)
rows=[];path=D/f'ledger-capture-{device}.json'
def save():
 p=path.with_suffix('.tmp');p.write_text(json.dumps(rows,indent=2));p.replace(path)
for case in json.loads((D/f'case-manifest-{device}.json').read_text()):
 ident=case['id'];cap=D/'captures-ledger'/ident
 if ident=='C03_fill__cpu':cap=D/'captures-ledger-preflight'/ident
 row=dict(id=ident,device=device,capture_dir=str(cap));rows.append(row)
 try:
  if not (cap/'capture-result.json').exists():
   cmd=[str(PY),'-m','pytest','-p','trace_api_chain.pytest_plugin',f'test_scenario_trace_warm.py::test_selected[{ident}]','-q','--tb=short',
    '--callchain-out='+str(cap),'--callchain-mode=full','--callchain-native-storage=paths','--callchain-api-catalog='+str(D/'api-ledger-10362.json'),
    '--callchain-max-events=10000000','--callchain-max-paths=1000000','--callchain-max-references=1000000','--callchain-timeout=1800']
   row['command']=cmd;save()
   with (D/(ident+'-ledger-capture.log')).open('w') as log:
    p=subprocess.run(cmd,cwd=D,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=1900)
   row['exit_code']=p.returncode
  row['capture']=json.loads((cap/'capture-result.json').read_text())
  row['sizes']={p.name:p.stat().st_size for p in cap.iterdir() if p.is_file()}
  save()
  if row['capture']['status']!='captured':raise RuntimeError('Capture not complete: '+ident)
 except BaseException as error:
  row['error']=str(error);row['traceback']=traceback.format_exc();save();raise
print('CAPTURES_DONE',device,flush=True)
