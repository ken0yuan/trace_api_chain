from pathlib import Path
import os,subprocess,json,time,sys,traceback
R=Path('/home/workspace-ycy/compile-performance');D=R/'runtime-scenarios-c03-c05';PY=R/'cases/instrumented-r7-lld22/venv311/bin/python'
env=os.environ.copy();env['PYTHONPATH']=str(D/'plugin-candidate-root')+':'+str(R/'plugin-runtime')
for k,v in {'PATH':str(R/'system-tools/root/usr/bin'),'C_INCLUDE_PATH':str(R/'system-tools/root/usr/include'),'LIBRARY_PATH':str(R/'system-tools/root/usr/lib/aarch64-linux-gnu'),'LD_LIBRARY_PATH':str(R/'system-tools/root/usr/lib/aarch64-linux-gnu')}.items():env[k]=v+(':'+env[k] if env.get(k) else '')
rows=[];path=D/'ledger-replay-results.json'
def save():
 p=path.with_suffix('.tmp');p.write_text(json.dumps(rows,indent=2));p.replace(path)
for device in ('cpu','npu'):
 for case in json.loads((D/f'case-manifest-{device}.json').read_text()):
  ident=case['id'];cap=D/'captures-ledger'/ident
  if ident=='C03_fill__cpu':cap=D/'captures-ledger-preflight'/ident
  row=dict(id=ident,capture_dir=str(cap),stage='waiting_for_capture');rows.append(row);save()
  deadline=time.monotonic()+3600
  while not (cap/'capture-result.json').exists():
   if time.monotonic()>deadline:raise RuntimeError('Timed out waiting for '+ident)
   time.sleep(3)
  receipt=json.loads((cap/'capture-result.json').read_text())
  if receipt.get('status')!='captured':row.update(stage='capture_failed',receipt=receipt);save();continue
  cache=cap/'symbol-cache'
  if not cache.exists():cache.symlink_to(D/'shared-symbol-cache',target_is_directory=True)
  cmd=[str(PY),'-c','from trace_api_chain.cli import main; raise SystemExit(main())','replay',str(cap),'--db',str(D/'ledger-api-callchains.sqlite'),'--slot',ident,
   '--source-root','pytorch='+str(R/'cases/instrumented-r7-lld22/pytorch'),'--source-root','torch_npu='+str(R/'cases/instrumented-r7-lld22/torch-npu'),'--source-root','upstream_tests='+str(D/'upstream-reference')]
  row['stage']='replaying';save();start=time.monotonic()
  with (D/(ident+'-ledger-replay.log')).open('w') as log:p=subprocess.run(cmd,cwd=D,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=3600)
  row.update(stage='finished' if p.returncode==0 else 'failed',exit_code=p.returncode,seconds=time.monotonic()-start)
  if (cap/'replay-result.json').exists():row['replay']=json.loads((cap/'replay-result.json').read_text())
  save()
  if p.returncode:raise RuntimeError('Replay failed: '+ident)
print('ALL_REPLAY_DONE',flush=True)
