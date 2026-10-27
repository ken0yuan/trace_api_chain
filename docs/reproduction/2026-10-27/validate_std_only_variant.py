from pathlib import Path
import base64,hashlib,json,os,sqlite3,struct,subprocess,time,traceback

R=Path('/home/workspace-ycy/compile-performance');D=R/'runtime-scenarios-c03-c05';OUT=D/'exclude-std-only-2026-10-27'
NEW=R/'cases/instrumented-r9-lld22-exclude-std';PY=NEW/'venv311/bin/python'
proof={'scope':{'excluded_definition_paths':['/usr/include/c++/11/','/usr/include/aarch64-linux-gnu/c++/11/'],
 'retained':['PyTorch','torch-npu','pybind11','Python headers','all other third parties'],
 'tracing':'Calling thread, host Python and instrumented native functions; standard-library functions intentionally omitted; uninstrumented device/CANN internals remain out of scope.'},'native':{},'captures':[]}

def save(stage,**extra):
 p=OUT/'trace-status.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps({'stage':stage,**extra},indent=2));tmp.replace(p)
 p=OUT/'validation-results.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(proof,indent=2));tmp.replace(p)

def plugin_env(device):
 env=os.environ.copy()
 for k in ('LD_PRELOAD','LD_AUDIT','SCENARIO_API_FILTER','TRACE_API_CHAIN_INTERNAL'):env.pop(k,None)
 env.update(PYTHONPATH=str(R/'plugin-runtime')+':'+str(D),SCENARIO_DEVICE=device,PYTORCH_TESTING_DEVICE_ONLY_FOR=device,
  TORCH_DEVICE_BACKEND_AUTOLOAD='0',PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',PYTORCH_PRINT_REPRO_ON_FAILURE='0')
 for k,v in {'PATH':str(R/'system-tools/root/usr/bin'),'C_INCLUDE_PATH':str(R/'system-tools/root/usr/include'),
  'LIBRARY_PATH':str(R/'system-tools/root/usr/lib/aarch64-linux-gnu'),'LD_LIBRARY_PATH':str(R/'system-tools/root/usr/lib/aarch64-linux-gnu')}.items():env[k]=v+':'+env.get(k,'')
 return env

try:
 save('waiting_for_timings')
 while True:
  p=OUT/'postbuild-status.json';state=json.loads(p.read_text()) if p.exists() else {}
  if state.get('stage')=='complete':break
  if state.get('stage')=='failed':raise RuntimeError(state)
  time.sleep(10)
 # Count-only diagnostic is deliberately separated from all elapsed-time tests.
 for variant,case in [('full',R/'cases/instrumented-r7-lld22'),('std_excluded',NEW)]:
  save('native_counter',variant=variant)
  folder=OUT/('native-'+variant);folder.mkdir(exist_ok=True)
  (folder/'native_scope_counter.py').write_bytes((OUT/'native_scope_counter.py').read_bytes())
  env=os.environ.copy();env.pop('PYTHONPATH',None);env.pop('LD_AUDIT',None)
  env.update(LD_PRELOAD=str(D/'fill-breakdown-2026-10-27/fill_hook_counts.so'),TORCH_DEVICE_BACKEND_AUTOLOAD='0',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1')
  with (folder/'driver.log').open('w') as log:
   subprocess.run([str(case/'venv311/bin/python'),str(folder/'native_scope_counter.py')],cwd=folder,env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=600)
  proof['native'][variant]=json.loads((folder/'native-hook-counts.json').read_text())
 for row in proof['native']['std_excluded']['rows']:
  assert row['scope_counts']['official_cpp_headers']==0,row
  assert row['scope_counts']['pytorch']>0,row
  if row['operation']=='tensor_from_list':
   assert row['scope_counts']['pybind11']>0 and row['scope_counts']['python_headers']>0,row
 proof['standard_headers_absent_from_resolved_counts']=True
 proof['pybind_python_and_pytorch_preserved']=True
 save('native_counter_complete')
 ledger=D/'api-ledger-10362.json';ledger_sha=hashlib.sha256(ledger.read_bytes()).hexdigest()
 assert ledger_sha=='c086abe9b812c2c315f1c208dd25b7c83f9d5b0fe825710f25f5fb78d2a0cd48'
 proof['ledger']={'apis':len(json.loads(ledger.read_text())['apis']),'sha256':ledger_sha}
 dbpath=OUT/'api-callchains.sqlite'
 cache=OUT/'shared-symbol-cache';cache.mkdir(exist_ok=True)
 expected=json.loads((OUT/'experiment-results.json').read_text())['correctness']
 olddb=sqlite3.connect('file:'+str(D/'ledger-api-callchains.sqlite')+'?mode=ro',uri=True)
 for device in ('cpu','npu'):
  manifest=json.loads((D/('case-manifest-'+device+'.json')).read_text())
  statuses={x['id']:x['status'] for x in expected['std_excluded/'+device]}
  env=plugin_env(device)
  for case in manifest:
   ident=case['id'];cap=OUT/'captures'/ident;row={'id':ident,'device':device,'capture_dir':str(cap)};proof['captures'].append(row)
   save('capturing',case=ident)
   cmd=[str(PY),'-m','pytest','-c',str(OUT/'pytest.ini'),'--confcutdir='+str(OUT),'-p','trace_api_chain.pytest_plugin',
    str(OUT/'test_std_trace.py')+'::test_selected['+ident+']','-q','--tb=short','--callchain-out='+str(cap),
    '--callchain-mode=full','--callchain-native-storage=paths','--callchain-api-catalog='+str(ledger),
    '--callchain-max-events=10000000','--callchain-max-paths=1000000','--callchain-max-references=1000000','--callchain-timeout=1800']
   row['command']=cmd
   with (OUT/(ident+'-capture.log')).open('w') as log:
    p=subprocess.run(cmd,cwd=OUT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=1900)
   row['exit_code']=p.returncode
   receipt=json.loads((cap/'capture-result.json').read_text());row['capture']=receipt
   assert receipt['status']=='captured' and not receipt['gaps'],receipt
   assert (p.returncode==0)==(statuses[ident]=='passed'),(ident,p.returncode,statuses[ident])
   catalog=json.loads((cap/'api-catalog.json').read_text())
   assert catalog['shared_ledger']['sha256']==ledger_sha
   header=struct.unpack('<8sQQQIIII',(cap/'native-paths.bin').open('rb').read(48))
   row.update(native_path_nodes=header[1],context_supports=header[2],native_enter_exit_count=header[3],native_failure_flags=header[6],
    native_bytes=(cap/'native-paths.bin').stat().st_size,python_bytes=(cap/'events.jsonl').stat().st_size)
   assert header[6]==0
   if not (cap/'symbol-cache').exists():(cap/'symbol-cache').symlink_to(cache,target_is_directory=True)
   save('replaying',case=ident)
   cmd=[str(PY),'-c','from trace_api_chain.cli import main;raise SystemExit(main())','replay',str(cap),'--db',str(dbpath),'--slot',ident,
    '--source-root','pytorch='+str(NEW/'pytorch'),'--source-root','torch_npu='+str(NEW/'torch-npu'),'--source-root','upstream_tests='+str(D/'upstream-reference')]
   with (OUT/(ident+'-replay.log')).open('w') as log:
    p=subprocess.run(cmd,cwd=OUT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=3600)
   assert p.returncode==0,ident
   row['replay']=json.loads((cap/'replay-result.json').read_text())
   db=sqlite3.connect(dbpath)
   aliases={x[0] for x in db.execute('SELECT alias FROM api_alias_support WHERE slot_id=?',(ident,))}
   old={x[0] for x in olddb.execute('SELECT alias FROM api_alias_support WHERE slot_id=?',(ident,))}
   row['observed_api_aliases']=sorted(aliases);row['vs_previous_full']={'missing':sorted(old-aliases),'additional':sorted(aliases-old)}
   db.close();row['stage']='complete';save('case_complete',case=ident)
 olddb.close()
 db=sqlite3.connect(dbpath);db.row_factory=sqlite3.Row
 assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
 assert not db.execute('PRAGMA foreign_key_check').fetchall()
 assert db.execute("SELECT COUNT(*) FROM capture_slots WHERE status='current'").fetchone()[0]==22
 proof['database']={'path':str(dbpath),'bytes':dbpath.stat().st_size,'integrity':'ok',
  'counts':{t:db.execute('SELECT COUNT(*) FROM '+t).fetchone()[0] for t in ('entities','paths','path_support','canonical_relations','apis','api_aliases','pending')},
  'source_status':[dict(x) for x in db.execute('SELECT language,status,COUNT(*) AS count FROM entities GROUP BY language,status')]}
 proof['mapped_cpp_by_source']={}
 for record in db.execute("SELECT path,COUNT(*) AS n FROM entities WHERE language='cpp' AND status='mapped' GROUP BY path"):
  path=record['path'];key='torch_npu' if path.startswith('torch_npu/') else 'pytorch' if path.startswith('pytorch/') else 'other'
  proof['mapped_cpp_by_source'][key]=proof['mapped_cpp_by_source'].get(key,0)+record['n']
 db.close()
 proof['functional_pass']=sum(r['exit_code']==0 for r in proof['captures'])
 proof['functional_fail']=sum(r['exit_code']!=0 for r in proof['captures'])
 proof['all_no_capture_gaps']=all(not r['capture']['gaps'] for r in proof['captures'])
 proof['all_previous_api_aliases_preserved']=all(not r['vs_previous_full']['missing'] for r in proof['captures'])
 save('complete')
except BaseException:
 save('failed',traceback=traceback.format_exc());raise
