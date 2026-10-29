def restore_captured_graph_order(graph,events):
    # Canonical graph JSON sorts dictionary keys. Restore only insertion order
    # known from retained input_sequence and actual raw source-read records.
    # Every graph value must still compare equal before replacing a container.
    nodes=dict(sorted(graph['nodes'].items(),key=lambda item:(0 if item[0].startswith('p:') else 1,item[1]['input_sequence'])))
    assert nodes==graph['nodes']
    representatives={};contexts={}
    for row in events:
        if row['event']=='call':contexts[str(row['uid'])]=representatives.setdefault(row['path_context'],str(row['uid']))
    reads=[dict(row,uid=int(contexts[str(row['uid'])])) for row in events if row['event']=='source_read']
    assert reads==graph['source_reads'],'Captured source-read values/order may not change'
    graph['nodes']=nodes;graph['source_reads']=reads

from pathlib import Path
import sys,json,time,hashlib,os,sqlite3,importlib.util,importlib
R=Path('/home/workspace-ycy/compile-performance');S=R/'timeout-tbe-writer-repair-2026-10-29';N=R/'pipeline-whitelist-dual-plugins-032-2026-10-28';C=R/'capture-repair-2026-10-28/repair-stream-frozen-rows-v4-20261030';OLD=R/'capture-repair-2026-10-28/release-unlimited-c9829bd-60d9541-20261030/runtime';O=S/'stream-reader-full-original-000610-v1';V=S/'stream-reader-full-original-000610-resume-v3';V.mkdir();private=O/'capture';ident='000610-78572d656a5a';old=json.loads((O/'baseline/refresh-plan.json').read_text());origin=Path(old['summary']['raw_evidence']);started=time.time()
def state(phase,**extra):
 v={'phase':phase,'at':time.time(),'started_at':started,'id':ident,'production_unmodified':True,**extra};(V/'state.json').write_text(json.dumps(v,indent=2));print(json.dumps(v),flush=True)
# Gate this larger check on the complete small frozen roundtrip proof.
proof_path=S/'graph-roundtrip-captured-order-proof-v1/validation.json'
while not proof_path.exists():
 small_process=S/'prove-graph-roundtrip-captured-order-v1.process.json'
 if small_process.exists():
  q=Path('/proc')/str(json.loads(small_process.read_text())['pid'])
  if not q.exists() or (q/'stat').read_text().rsplit(')',1)[1].split()[0]=='Z':raise RuntimeError('small_roundtrip_proof_not_verified')
 time.sleep(5)
small=json.loads(proof_path.read_text());assert small['complete_plan_equal_after_restoring_actual_capture_order'] and len(small['all_14_tables_equal_no_ignored_fields'])==14
prior=Path('/proc/321018')
while prior.exists() and (prior/'stat').read_text().rsplit(')',1)[1].split()[0]!='Z':time.sleep(5)
assert not (S/'stream-reader-full-original-000610-resume-v2/validation.json').exists()
while True:
 available=int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:')))*1024
 if available>=512*1024**3:break
 state('waiting_private_validation_memory',available_gib=available/1024**3);time.sleep(30)
sys.dont_write_bytecode=True;sys.path.insert(0,str(C/'runtime'));from trace_api_chain import database,source_entities;from trace_api_chain.changed_objects import canonical_path;from trace_api_chain.native import digest;from trace_api_chain.writer_session import WriterSession
manifest=json.loads((private/'evidence/manifest.json').read_text());mapping={}
for relative,expected in manifest['raw_sha256'].items():
 if not relative.startswith('observer-runtime/'):continue
 physical=private/relative;assert digest(physical)==expected
 logical=origin/relative;mapping[str(logical)]=(physical,expected)
assert mapping
class ArchivedSources(source_entities.SourceEntities):
 def resolve_source(self,filename):
  raw=os.path.abspath(os.fspath(filename))
  if raw in mapping:return super().resolve_source(mapping[raw][0])
  return super().resolve_source(filename)
 def canonical_source_path(self,filename):
  raw=os.path.abspath(os.fspath(filename))
  for logical,(physical,_) in mapping.items():
   if raw in (logical,str(physical)):
    return canonical_path(logical,self.roots)
  return super().canonical_source_path(filename)
database.SourceEntities=ArchivedSources
(V/'source-archive-bindings.json').write_text(json.dumps({logical:{'archive':str(p),'sha256':sha,'stat_size':p.stat().st_size,'mtime_ns':p.stat().st_mtime_ns} for logical,(p,sha) in mapping.items()},indent=2))
state('loading_already_verified_graph_without_replay',graph_bytes=(private/'graph.json').stat().st_size)
assert digest(private/'graph.json')==old['summary']['graph_digest']==json.loads((O/'baseline/graph-verified.json').read_text())['graph_sha256']
graph=json.loads((private/'graph.json').read_text());events=[]
with (private/'events.jsonl').open() as f:
 for line in f:
  if line.startswith(('{"event":"start",','{"event":"call",','{"event":"definition",','{"event":"object",','{"event":"source_read",')):events.append(json.loads(line))
assert events and events[0]['event']=='start'
restore_captured_graph_order(graph,events)
state('canonical_graph_values_preserved_actual_capture_order_restored')
native=[json.loads(line) for line in (private/'evidence/native.jsonl').open()]
from trace_api_chain.symbols import resolve
identities={}
for r in native:
 for name in ['source_identity','caller_identity']:
  i=r[name]
  if i.get('path') and isinstance(i.get('offset'),int):identities[i['path'],i['offset']]=i
resolved=resolve(identities.values(),private/'symbol-cache',index_dir=S/'offline-reuse-real-validation-v1/readonly-symbol-index')
prepared={'output':private,'graph':graph,'events':events,'native':native,'resolved':resolved,'receipt':json.loads((private/'pytest-receipt.json').read_text()),'manifest':manifest,'mode':'full'}
state('normalizing_with_hash_verified_archived_sources');phase=time.monotonic();roots={'pytorch':str(R/'cases/instrumented-r9-lld22-exclude-std/pytorch'),'torch_npu':str(R/'cases/instrumented-r9-lld22-exclude-std/torch-npu')};plan=database.prepare_refresh(prepared,roots,ident);plan=json.loads(json.dumps(plan));(V/'candidate-plan.json').write_text(json.dumps(plan));seconds=time.monotonic()-phase
expected=json.loads(json.dumps(old));expected['summary']['raw_evidence']=str(private);assert expected==plan,'Archived source restoration must make every plan field equal'
for logical,(physical,sha) in mapping.items():assert digest(physical)==sha
state('complete_plan_exact_after_archived_source_binding',normalization_seconds=seconds)
del prepared,graph,events,native,old,expected,plan
import gc;gc.collect()
spec=importlib.util.spec_from_file_location('original_legacy_stream',OLD/'trace_api_chain/__init__.py',submodule_search_locations=[str(OLD/'trace_api_chain')]);pkg=importlib.util.module_from_spec(spec);sys.modules[spec.name]=pkg;spec.loader.exec_module(pkg);legacy=importlib.import_module('original_legacy_stream.reference_transport');from trace_api_chain.reference_transport import expand_events,_PrivateEventReader
def event_hash(iterator):
 h=hashlib.sha256();count=0
 for row in iterator:h.update(json.dumps(row,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()+b'\n');count+=1
 return {'count':count,'sha256':h.hexdigest()}
state('streaming_complete_independent_event_comparison');phase=time.monotonic()
with (private/'events.jsonl').open() as f:a=event_hash(legacy.expand_events(json.loads(line) for line in f))
(V/'legacy-event-sha.json').write_text(json.dumps(a))
with (private/'events.jsonl').open() as f:b=event_hash(expand_events(_PrivateEventReader(f),_private_rows=True))
assert a==b;event_seconds=time.monotonic()-phase;state('complete_events_exact',events=a,seconds=event_seconds)
old=json.loads((O/'baseline/refresh-plan.json').read_text());plan=json.loads((V/'candidate-plan.json').read_text());state('committing_private_graphs');session=WriterSession(V/'api-callchains.sqlite',preserve_prior_slots=True)
try:report=session.commit_plan(plan)
finally:session.close()
database.commit_refresh(V/'baseline.sqlite',old);(private/'replay-result.json').write_text(json.dumps(report))
def tables(path,normalize=False):
 result={}
 with sqlite3.connect(path) as db:
  for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
   cols=[v[1] for v in db.execute('PRAGMA table_info('+name+')')];h=hashlib.sha256();count=0
   for row in db.execute('SELECT * FROM '+name+' ORDER BY '+','.join('"'+c+'"' for c in cols)):
    row=list(row)
    if name=='capture_slots' and normalize:
     row[4]=old['summary']['raw_evidence'];summary=json.loads(row[6]);summary['raw_evidence']=old['summary']['raw_evidence'];row[6]=json.dumps(summary,ensure_ascii=False)
    h.update(json.dumps(row,ensure_ascii=False,separators=(',',':')).encode()+b'\n');count+=1
   result[name]={'rows':count,'sha256':h.hexdigest()}
 return result
before=tables(V/'baseline.sqlite');after=tables(V/'api-callchains.sqlite',True);assert len(before)==14 and before==after
state('verifying_private_api_store')
import shutil
runtime=V/'plugin-runtime-035-private';shutil.copytree(C/'runtime',runtime);spec=importlib.util.spec_from_file_location('original_stream_validation_driver',C/'trace/tools/whitelist_trace_driver_035.py');driver=importlib.util.module_from_spec(spec);spec.loader.exec_module(driver);driver.OUT=V;driver.NO_TIMEOUT=True;row=json.loads((N/'results'/(ident+'.json')).read_text());driver.initialize_relations([row]);result=dict(row,capture_dir=str(private),relations_dir=str(O/'relations'/ident));driver.append_relationships(result)
from trace_api_chain.finalize import committed_state
committed_state(V/'api-callchains.sqlite',V/'api-test-relations.sqlite',result,plan['summary']['graph_digest'])
proof={'finished_at':time.time(),'id':ident,'complete_graph_sha256':plan['summary']['graph_digest'],'complete_events':a,'complete_plan_equal':True,'all_14_tables_equal_with_only_private_receipt_path_mapping':before,'source_bindings':str(V/'source-archive-bindings.json'),'strict_two_stores_verified':True,'captured_order_restored_from_exact_raw_rows':True,'small_roundtrip_14_table_proof':str(proof_path),'normalization_seconds':seconds,'event_check_seconds':event_seconds,'original_prepare_seconds':json.loads((O/'state.json').read_text())['prepare_seconds'],'production_unmodified':True,'no_npu_test_rerun':True};(V/'validation.json').write_text(json.dumps(proof,indent=2));state('complete_full_original_verified')
