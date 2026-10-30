"""Cancel redundant diagnostic repeats; retain metrics and remove only byte-proven copies."""
from pathlib import Path
import os,signal,time,json,hashlib,sqlite3,re,shutil
R=Path('/home/workspace-ycy/compile-performance');S=R/'timeout-tbe-writer-repair-2026-10-29';N=R/'pipeline-whitelist-dual-plugins-032-2026-10-28';B=S/'stream-canonical-full-original-graph-v1';O=S/'stream-canonical-lean-pair-v1';O.mkdir(exist_ok=True);source=S/'stream-reader-full-original-000610-v1/capture/graph.json';expected='b4ed4343c1844f1d8d88b72084052219a218d95000fe8112c609b9611dd40ec9';script=os.fsencode(S/'benchmark-full-graph-serialization-v1.py');scheduler=326722
def active(pid):
 try:return Path('/proc',str(pid),'stat').read_text().rsplit(')',1)[1].split()[0]!='Z'
 except OSError:return False
def args(pid):return [a for a in Path('/proc',str(pid),'cmdline').read_bytes().split(b'\0') if a]
def workers():
 found=[]
 for p in Path('/proc').iterdir():
  if not p.name.isdigit():continue
  try:
   pid=int(p.name);a=args(pid)
   if active(pid) and os.getpgid(pid)==scheduler:
    if not a and not active(pid):continue
    assert len(a)==5 and a[1]==script and a[2]==b'worker' and a[3] in (b'old',b'new') and a[4] in (b'0',b'1',b'2',b'3'),(pid,a)
    found.append((pid,int(a[4])))
  except (FileNotFoundError,ProcessLookupError):pass
 return found
if active(scheduler):
 a=args(scheduler);assert len(a)==2 and a[1]==script
 os.kill(scheduler,signal.SIGTERM)  # Only our repeat scheduler; not the formal controller or a pytest process.
while active(scheduler):time.sleep(.1)
signalled=[]
for pid,index in workers():
 if index>=2:
  os.kill(pid,signal.SIGTERM);signalled.append(pid)  # Extra repeats are diagnostics, not NPU tests.
while workers():
 (O/'state.json').write_text(json.dumps({'at':time.time(),'phase':'waiting_current_single_comparison_worker','extra_repeat_workers_cancelled':signalled,'formal_tests_signalled':False}))
 time.sleep(.2)
(B/'cancellation.json').write_text(json.dumps({'at':time.time(),'reason':'User requested minimal diagnostic storage; retain at most one old/new pair','repeat_scheduler_stopped':scheduler,'extra_repeat_workers_cancelled':signalled,'current_first_new_worker_allowed_to_finish':True,'no_formal_controller_or_pytest_signalled':True},indent=2))
def verify_stores_readonly():
 r=json.loads((N/'results/000610-78572d656a5a.json').read_text());assert r['finalization']['graph_sha256']==expected and r['finalization']['graph_gaps']==[]
 # These two connections cannot write: URI mode=ro plus query_only. No plugin
 # imports and no INSERT/UPDATE/DELETE/DDL statements are used in this checker.
 with sqlite3.connect((N/'api-callchains.sqlite').as_uri()+'?mode=ro',uri=True) as db:
  db.execute('PRAGMA query_only=ON');row=db.execute('SELECT status,summary FROM capture_slots WHERE id=?',(r['id'],)).fetchone();assert row and row[0]=='current';summary=json.loads(row[1]);assert summary['gaps']==[] and summary['graph_digest']==expected and summary['test']==r['nodeid'] and summary['raw_evidence']==r['capture_dir']
 with sqlite3.connect((N/'api-test-relations.sqlite').as_uri()+'?mode=ro',uri=True) as db:
  db.execute('PRAGMA query_only=ON');row=db.execute('SELECT module,detail FROM capture_runs WHERE attempt=?',(r['relations_dir'],)).fetchone();assert row and row[0]==r['file'];detail=json.loads(row[1]);required={'raw_mapping_and_owner_match','destination_mapping_and_phases_equal','prior_positive_subset','completed_instances_preserved','foreign_key_check','integrity_check'};assert required<=set(detail['checks']) and not detail.get('boundaries');rows=db.execute('SELECT t.capture_state,t.source_attempt FROM tests t JOIN modules m ON m.id=t.module_id WHERE m.module=? AND t.nodeid IN (?,?)',(r['file'],r['nodeid'],'pytorch/'+r['nodeid'])).fetchall();assert rows==[('complete',r['id'])]
def digest(path):
 with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
verify_stores_readonly();assert source.is_file() and not source.is_symlink() and digest(source)==expected
copies=[];preserved=[]
for p in B.iterdir():
 if not re.fullmatch(r'(old|new)-[0-3]\.graph\.json(?:\.[A-Za-z0-9_-]+\.tmp)?',p.name):continue
 assert p.is_file() and not p.is_symlink() and p.parent.resolve()==B.resolve();size=p.stat().st_size
 if size>source.stat().st_size:preserved.append(str(p));continue
 h=hashlib.sha256();same=True
 with p.open('rb') as candidate,source.open('rb') as original:
  while True:
   chunk=candidate.read(1024*1024)
   if not chunk:break
   h.update(chunk)
   if chunk!=original.read(len(chunk)):same=False;break
 if not same:preserved.append(str(p));continue
 copies.append({'path':str(p),'bytes':size,'sha256':h.hexdigest(),'complete_copy':size==source.stat().st_size,'proof':'Every byte equals the retained committed graph, or its exact prefix','removed':False})
verify_stores_readonly();assert digest(source)==expected
pair=[]
for index in [0,1]:
 p=B/('worker-'+str(index)+'.json')
 if p.exists():pair.append(json.loads(p.read_text()))
receipt={'at':time.time(),'scope':'Single old/new frozen diagnostic comparison; no extra repeats, no cold-cache claim','source_graph_kept':str(source),'source_graph_sha256':expected,'source_two_stores_verified_by_readonly_queries':True,'old_new_records':pair,'copies':copies,'nonidentical_artifacts_preserved':preserved,'cleanup':'verified_pending','formal_or_raw_capture_data_deleted':False}
(O/'validation.json').write_text(json.dumps(receipt,indent=2))
for record in copies:
 p=Path(record['path']);assert p.parent.resolve()==B.resolve() and p.is_file() and not p.is_symlink();p.unlink();record['removed']=True
receipt.update(cleanup='complete',removed_duplicate_logical_bytes=sum(r['bytes'] for r in copies),free_disk_gib=shutil.disk_usage(N).free/1024**3);(O/'validation.json').write_text(json.dumps(receipt,indent=2));(O/'state.json').write_text(json.dumps({'at':time.time(),'phase':'verified_duplicate_diagnostics_cleaned'}));print(json.dumps(receipt),flush=True)
