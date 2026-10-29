from pathlib import Path
from types import SimpleNamespace
import sys,os,json,time,shutil,importlib.util,hashlib
R=Path('/home/workspace-ycy/compile-performance');S=R/'timeout-tbe-writer-repair-2026-10-29';N=R/'pipeline-whitelist-dual-plugins-032-2026-10-28';C=R/'capture-repair-2026-10-28/release-metadata-fbcccb4-60d9541-20261030';U=S/'metadata-original-lstm-fbcccb4-v2';ident='001229-2f370e1cb610'
while not (C/'test-results.json').exists():time.sleep(2)
assert json.loads((C/'test-results.json').read_text())=={'api':0,'trace':0}
assert json.loads((S/'metadata-replay-full-frozen-proof-v2/validation.json').read_text())['complete_plan_equal_without_ignored_fields']
for p in Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:
  cmd=(p/'cmdline').read_bytes().replace(b'\0',b' ').decode(errors='replace');state=(p/'stat').read_text().rsplit(')',1)[1].split()[0]
  if state!='Z' and '-m pytest ' in cmd:
   env=dict(x.split(b'=',1) for x in (p/'environ').read_bytes().split(b'\0') if b'=' in x);assert '0' not in env.get(b'ASCEND_RT_VISIBLE_DEVICES',b'').decode().split(','),(p.name,cmd)
 except (OSError,ValueError):pass
U.mkdir();runtime=U/'plugin-runtime-035-private';shutil.copytree(C/'runtime',runtime)
hashes={}
for label,package in [('trace','trace_api_chain'),('api','api_test_relations')]:
 root=C/label/'src';root=root/package if (root/package).is_dir() else root
 for source in root.rglob('*'):
  if not source.is_file() or source.suffix not in ('.py','.c','.h','.json'):continue
  target=runtime/package/source.relative_to(root)
  assert target.is_file(),str(target)
  assert source.read_bytes()==target.read_bytes(),str(source);hashes[package+'/'+source.relative_to(root).as_posix()]=hashlib.sha256(target.read_bytes()).hexdigest()
assert len(hashes)>40
os.environ.update(CALLCHAIN_NO_TIMEOUT='1',CALLCHAIN_DEFER_REFERENCE_STATES='1',CALLCHAIN_CODE_REVISION='fbcccb42c02d59a219fd3870776de78a78ea033f',CALLCHAIN_API_REVISION='60d954168a5050b720a21b31735b2c07c5db9509',CALLCHAIN_READONLY_SYMBOL_INDEX=str(S/'offline-reuse-real-validation-v1/readonly-symbol-index'))
helper=(C/'trace/tools/whitelist_trace_collect_035.py').read_text().replace(str(N),str(U));(U/'whitelist_trace_collect_035.py').write_text(helper)
spec=importlib.util.spec_from_file_location('metadata_lstm_driver',C/'trace/tools/whitelist_trace_driver_035.py');driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver);driver.OUT=U;driver.NO_TIMEOUT=True;driver.PIPELINE=SimpleNamespace(submit=lambda path:None)
sys.path.insert(0,str(runtime));sys.path.insert(0,str(U));item=next(i for i in json.loads((N/'plan.json').read_text()) if i['id']==ident)
for name in ['results','captures','relations','logs','junit']:(U/name).mkdir()
driver.write(U/'process.json',{'pid':os.getpid(),'started_at':time.time(),'card':'0','id':ident,'runtime':str(runtime),'trace_commit':os.environ['CALLCHAIN_CODE_REVISION'],'api_commit':os.environ['CALLCHAIN_API_REVISION'],'archive_source_files_bytewise_verified':hashes,'timeout_disabled':True,'formal_data_unchanged':True,'old_id_not_new_unique':True})
driver.initialize_relations([item]);result=driver.execute(item,'0');path=U/'results'/(ident+'.json');passed=any(x.get('when')=='call' and x.get('outcome')=='passed' for x in result['test_reports'])
driver.write(U/'capture-validation.json',{'finished_at':time.time(),'test_passed':passed,'capture':result['capture'],'elapsed_seconds':result['elapsed_seconds'],'formal_result_unchanged':True})
assert passed and result['capture']['status']=='captured' and result['capture']['gaps']==[]
while True:
 available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
 if available>=512*1024**3:break
 driver.write(U/'state.json',{'phase':'waiting_private_preparation_memory','available_gib':available/1024**3,'at':time.time()});time.sleep(30)
driver.write(U/'state.json',{'phase':'preparing_real_original_lstm','at':time.time()});prepared=driver.organize(path);result=driver.read(path)
from trace_api_chain.writer_session import WriterSession
session=WriterSession(U/'api-callchains.sqlite',preserve_prior_slots=True);start=time.monotonic()
try:report=session.commit_file(prepared['plan'])
finally:session.close()
result['replay']={'returncode':0,'elapsed_seconds':time.monotonic()-start,'timeout_disabled':True};valid,result['graph_validation']=driver.verify_graph_result(Path(result['capture_dir']),ident,U/'api-callchains.sqlite',prepared['graph']);assert valid
driver.append_relationships(result);result.update(replay_status='complete',dual_capture_valid=True);driver.write(path,result)
from trace_api_chain.finalize import committed_state
committed_state(U/'api-callchains.sqlite',U/'api-test-relations.sqlite',result,result['graph_validation']['graph_digest'])
proof={'finished_at':time.time(),'id':ident,'test_passed':True,'capture_seconds':result['elapsed_seconds'],'prepare_seconds':result['preparation']['elapsed_seconds'],'graph_commit_seconds':result['replay']['elapsed_seconds'],'api_append_seconds':result['relations_merge']['elapsed_seconds'],'graph_sha256':result['graph_validation']['graph_digest'],'graph_gaps':[],'strict_two_store_verified':True,'two_plugins':json.loads((Path(result['capture_dir'])/'dual-plugin-load.json').read_text()),'fork_fragments_preserved_unvalidated':list(map(str,(Path(result['capture_dir'])/'fork-fragments').glob('*/fork-fragment.json'))),'no_cleanup_or_formal_replacement':True,'scope':'selected calling-thread host graph; no claim that fork or device graphs are validated'};driver.write(U/'validation.json',proof);print(json.dumps(proof),flush=True)
