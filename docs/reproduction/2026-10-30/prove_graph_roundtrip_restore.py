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
import json,sys,time,sqlite3,hashlib
R=Path('/home/workspace-ycy/compile-performance');S=R/'timeout-tbe-writer-repair-2026-10-29';C=R/'capture-repair-2026-10-28/repair-stream-frozen-rows-v4-20261030';F=S/'stream-reader-full-frozen-proof-v3';O=S/'graph-roundtrip-captured-order-proof-v1';O.mkdir();sys.dont_write_bytecode=True;sys.path.insert(0,str(C/'runtime'));from trace_api_chain import database;from trace_api_chain.native import digest;from trace_api_chain.symbols import resolve
old=json.loads((F/'refresh-plan.json').read_text());private=F/'refresh-plan.json.work';source=Path(old['summary']['raw_evidence']);assert digest(private/'graph.json')==old['summary']['graph_digest'];graph=json.loads((private/'graph.json').read_text());events=[]
with (source/'events.jsonl').open() as stream:
 for line in stream:
  if line.startswith(('{"event":"start",','{"event":"call",','{"event":"definition",','{"event":"object",','{"event":"source_read",')):events.append(json.loads(line))
native=[json.loads(line) for line in (private/'evidence/native.jsonl').open()];identities={}
for row in native:
 for name in ['source_identity','caller_identity']:
  i=row[name]
  if i.get('path') and isinstance(i.get('offset'),int):identities[i['path'],i['offset']]=i
resolved=resolve(identities.values(),private/'symbol-cache',index_dir=S/'offline-reuse-real-validation-v1/readonly-symbol-index');prepared={'output':private,'graph':graph,'events':events,'native':native,'resolved':resolved,'receipt':json.loads((private/'pytest-receipt.json').read_text()),'manifest':json.loads((private/'evidence/manifest.json').read_text()),'mode':'full'};roots={'pytorch':str(R/'cases/instrumented-r9-lld22-exclude-std/pytorch'),'torch_npu':str(R/'cases/instrumented-r9-lld22-exclude-std/torch-npu')}
before=database.prepare_refresh(prepared,roots,old['slot']);before=json.loads(json.dumps(before));before['summary']['raw_evidence']=old['summary']['raw_evidence'];mismatch=[key for key in old if old[key]!=before[key]]
restore_captured_graph_order(graph,events);plan=database.prepare_refresh(prepared,roots,old['slot']);plan=json.loads(json.dumps(plan));plan['summary']['raw_evidence']=old['summary']['raw_evidence'];assert plan==old,'Every source/entity/topology/plan field must match'
database.commit_refresh(O/'before.sqlite',old);database.commit_refresh(O/'after.sqlite',plan)
def hashes(path):
 out={}
 with sqlite3.connect(path) as db:
  for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
   cols=[row[1] for row in db.execute('PRAGMA table_info('+name+')')];h=hashlib.sha256();count=0
   for row in db.execute('SELECT * FROM '+name+' ORDER BY '+','.join('"'+c+'"' for c in cols)):h.update(json.dumps(row,ensure_ascii=False,separators=(',',':')).encode()+b'\n');count+=1
   out[name]={'rows':count,'sha256':h.hexdigest()}
 return out
a=hashes(O/'before.sqlite');b=hashes(O/'after.sqlite');assert len(a)==14 and a==b
proof={'finished_at':time.time(),'unrestored_roundtrip_different_fields':mismatch,'graph_values_unchanged':True,'complete_plan_equal_after_restoring_actual_capture_order':True,'all_14_tables_equal_no_ignored_fields':a,'source':'actual frozen canary; no NPU rerun; no production changes'};(O/'validation.json').write_text(json.dumps(proof,indent=2));print('VERIFIED',json.dumps(proof),flush=True)
