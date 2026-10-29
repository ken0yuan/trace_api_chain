"""One canonical SQL graph, refreshed offline; raw event history stays outside it."""
import json
from pathlib import Path
import sqlite3
import time

from .native import digest,save
from .source_entities import SourceEntities,key

SCHEMA='''
CREATE TABLE IF NOT EXISTS schema_info(version INTEGER NOT NULL);
INSERT INTO schema_info SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_info);
CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY, path TEXT, kind TEXT NOT NULL,
 name TEXT NOT NULL, source_sha256 TEXT, language TEXT, status TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aliases(entity_id TEXT REFERENCES entities(id), alias TEXT, origin TEXT,
 PRIMARY KEY(entity_id,alias,origin));
CREATE TABLE IF NOT EXISTS capture_slots(id TEXT PRIMARY KEY, test TEXT, input_digest TEXT,
 import_digest TEXT, evidence_directory TEXT, status TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS slot_entities(slot_id TEXT REFERENCES capture_slots(id) ON DELETE CASCADE,
 entity_id TEXT REFERENCES entities(id), source_sha256 TEXT, PRIMARY KEY(slot_id,entity_id));
CREATE TABLE IF NOT EXISTS apis(id TEXT PRIMARY KEY, entity_id TEXT REFERENCES entities(id));
CREATE TABLE IF NOT EXISTS api_aliases(api_id TEXT REFERENCES apis(id), alias TEXT,
 PRIMARY KEY(api_id,alias));
CREATE TABLE IF NOT EXISTS api_alias_support(slot_id TEXT REFERENCES capture_slots(id) ON DELETE CASCADE,
 api_id TEXT REFERENCES apis(id), alias TEXT, PRIMARY KEY(slot_id,api_id,alias));
CREATE TABLE IF NOT EXISTS paths(id TEXT PRIMARY KEY, api_id TEXT REFERENCES apis(id),
 parent_id TEXT REFERENCES paths(id), entity_id TEXT REFERENCES entities(id), edge_kind TEXT, depth INTEGER);
CREATE TABLE IF NOT EXISTS path_support(slot_id TEXT REFERENCES capture_slots(id) ON DELETE CASCADE,
 path_id TEXT REFERENCES paths(id), PRIMARY KEY(slot_id,path_id));
CREATE TABLE IF NOT EXISTS observed_objects(slot_id TEXT REFERENCES capture_slots(id) ON DELETE CASCADE,
 path_id TEXT REFERENCES paths(id), entity_id TEXT REFERENCES entities(id), PRIMARY KEY(slot_id,path_id,entity_id));
CREATE TABLE IF NOT EXISTS object_references(slot_id TEXT REFERENCES capture_slots(id) ON DELETE CASCADE,
 owner_path TEXT REFERENCES paths(id), source_entity TEXT REFERENCES entities(id),
 target_entity TEXT REFERENCES entities(id), role TEXT, PRIMARY KEY(slot_id,owner_path,source_entity,target_entity,role));
CREATE TABLE IF NOT EXISTS source_dependencies(slot_id TEXT REFERENCES capture_slots(id) ON DELETE CASCADE,
 path_id TEXT REFERENCES paths(id), entity_id TEXT REFERENCES entities(id), relation TEXT, evidence TEXT,
 PRIMARY KEY(slot_id,path_id,entity_id,relation));
CREATE INDEX IF NOT EXISTS dependency_entity ON source_dependencies(entity_id);
CREATE TABLE IF NOT EXISTS pending(slot_id TEXT REFERENCES capture_slots(id) ON DELETE CASCADE,
 observation TEXT, reason TEXT, detail TEXT, PRIMARY KEY(slot_id,observation,reason));
CREATE INDEX IF NOT EXISTS paths_api ON paths(api_id,parent_id);
CREATE INDEX IF NOT EXISTS paths_entity ON paths(entity_id);
CREATE INDEX IF NOT EXISTS support_path ON path_support(path_id);
CREATE INDEX IF NOT EXISTS api_alias_lookup ON api_aliases(alias);
CREATE INDEX IF NOT EXISTS entities_source_version ON entities(path,source_sha256);
CREATE INDEX IF NOT EXISTS slot_entities_entity ON slot_entities(entity_id,slot_id);
CREATE INDEX IF NOT EXISTS paths_parent ON paths(parent_id);
CREATE VIEW IF NOT EXISTS canonical_relations AS
 SELECT DISTINCT parent.entity_id AS src_id,child.entity_id AS dst_id,child.edge_kind
 FROM paths child JOIN paths parent ON parent.id=child.parent_id
 JOIN path_support s ON s.path_id=child.id JOIN capture_slots c ON c.id=s.slot_id
 WHERE c.status IN ('current','partial');
'''


def connect(path,readonly=False,cache_mib=4096):
    if type(cache_mib) is not int or not 1<=cache_mib<=16384:
        raise ValueError('database_cache_mib_must_be_between_1_and_16384')
    path=Path(path)
    if readonly:
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    else:
        path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(path,timeout=60)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON')
    if not readonly:db.execute('PRAGMA cache_size='+str(-cache_mib*1024))
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if tables and ('schema_info' not in tables or db.execute('SELECT version FROM schema_info').fetchone()[0]!=1):
        db.close();raise ValueError('incompatible_database_schema: existing legacy databases are not overwritten')
    if not readonly:db.executescript(SCHEMA)
    return db


def dwarf_definitions(prepared):
    """Resolve each observed function PC once, with a hash-bound offline cache."""
    from .dwarf_lookup import lookup,reader_source
    cache_dir=prepared['output']/'symbol-cache';cache_dir.mkdir(exist_ok=True)
    groups={}
    for row in prepared['native']:
        if row['event'] not in ('enter','path'):continue
        i=row['source_identity']
        if i.get('sha256') and isinstance(i.get('offset'),int):groups.setdefault((i['path'],i['sha256']),set()).add(i['offset'])
    result={}
    for (path,sha),offsets in sorted(groups.items()):
        cache_file=cache_dir/('definitions-'+key([sha,digest(reader_source()),digest(Path(__file__).with_name('dwarf_lookup.py'))])+'.json')
        cached=json.loads(cache_file.read_text()) if cache_file.exists() else {}
        if digest(Path(path))!=sha:continue
        missing=sorted(o for o in offsets if str(o) not in cached)
        if missing:
            print('trace-api-chain replay: DWARF definitions '+Path(path).name+' '+str(len(missing)),flush=True)
            cached.update(lookup(path,missing,cache_dir))
            save(cache_file,cached)
        for offset in offsets:result[path,offset]=cached[str(offset)]
    return result


def normalize(prepared,roots=None,definitions=None):
    graph=prepared['graph'];events=prepared['events'];native=prepared['native']
    source=SourceEntities(roots or prepared['receipt'].get('source_roots',{}),immutable_sources=True)
    definitions=dwarf_definitions(prepared) if definitions is None and native else (definitions or {})
    calls={str(e['uid']):e for e in events if e['event']=='call'}
    defmap={e['id']:e for e in events if e['event']=='definition'}
    objects={e['id']:e for e in events if e['event']=='object'}
    mapping={};aliases=set();pending=[];native_map={};live_entry={}
    for row in native:
        if row['event'] not in ('enter','path'):continue
        i=row['source_identity'];identity=(i.get('path'),i.get('offset'))
        if identity not in native_map:
            native_map[identity]=source.cpp(i,definitions.get(identity),prepared['resolved'].get(identity))
        live_entry.setdefault((str(row['py_uid']),row['absolute_function']),native_map[identity])
    for uid,node in graph['nodes'].items():
        if uid.startswith('n:'):
            r=native[node['input_sequence']];i=r['source_identity'];eid=native_map[i.get('path'),i.get('offset')]
        else:
            c=calls[uid[2:]];eid=None;reason='native_binding_not_observed'
            if c.get('definition'):eid,reason=source.python(defmap[c['definition']])
            else:eid=live_entry.get((uid[2:],(c.get('native_identity') or {}).get('func_ptr')))
            if not eid:
                identity=(c.get('native_identity') or {})
                eid=source.unresolved([graph['test'],c.get('definition') or uid,identity.get('module'),identity.get('module_offset')],c.get('name'),'python',reason)
            if c.get('name'):aliases.add((eid,c['name'],'runtime_label'))
        mapping[uid]=eid
    object_map={}
    for oid,obj in objects.items():
        if obj.get('definition') in defmap:eid,reason=source.python(defmap[obj['definition']])
        elif obj.get('kind') in ('instance','class'):eid,reason=source.instance(obj)
        elif obj.get('kind')=='module':eid,reason=source.named(graph.get('module_sources',{}).get((obj.get('static_owner') or {}).get('module'),{}))
        else:eid,reason=None,'no_source_definition_anchor'
        if eid:
            object_map[oid]=eid
            label=(obj.get('static_owner') or obj.get('type') or {}).get('qualname')
            if label:aliases.add((eid,label,'runtime_type_label'))
        else:pending.append((oid,reason,json.dumps(obj,ensure_ascii=False)))
    return source,mapping,object_map,aliases,pending,calls


def prepare_refresh(prepared,roots=None,slot=None,definitions=None):
    """Compute one complete contribution without opening any database."""
    if prepared['graph'].get('reference_depth_limit') is not None and not (slot or '').startswith('experiment:reference-depth-2:'):
        raise ValueError('bounded_capture_requires_experiment_slot')
    source,mapping,object_map,aliases,pending,calls=normalize(prepared,roots,definitions)
    graph=prepared['graph'];start=next(e for e in prepared['events'] if e['event']=='start')
    test_file=source.path(start['test_file']) if start.get('test_file') else graph['test']
    slot=slot or key([test_file,graph['test'],start.get('namespaces',[]),prepared['mode']])
    paths={};supports=set();api_aliases=set();api_entities={};contexts={};object_links=set();reference_links=set()
    path_ids={};api_ids={}
    def path_id(api,parent,entity,relation):
        identity=(api,parent,entity,relation)
        if identity not in path_ids:path_ids[identity]=key(list(identity))
        return path_ids[identity]
    relations={(e['from'],e['to']):e['kind'] for e in graph['edges']}
    # Input insertion order is Python pass then native pass, not topological;
    # derive contexts recursively using only validated parent links.
    def visit(uid):
        if uid in contexts:return contexts[uid]
        node=graph['nodes'][uid];parent=node['parent'];current=[]
        for api,parent_path,depth in (visit(parent) if parent is not None else []):
            relation=relations[parent,uid];pid=path_id(api,parent_path,mapping[uid],relation)
            paths[pid]=(api,parent_path,mapping[uid],relation,depth+1);supports.add(pid)
            current.append((api,pid,depth+1))
        call=calls.get(uid[2:]) if uid.startswith('p:') else None
        names=(call or {}).get('api_aliases') or ([call['api_id']] if call and call.get('api_id') else [])
        if names:
            if mapping[uid] not in api_ids:api_ids[mapping[uid]]=key(['api',mapping[uid]])
            api=api_ids[mapping[uid]];api_entities[api]=mapping[uid]
            for name in names:api_aliases.add((api,name.removeprefix('runtime:')))
            pid=path_id(api,None,mapping[uid],'api_entry');paths[pid]=(api,None,mapping[uid],'api_entry',0);supports.add(pid)
            current.append((api,pid,0))
        contexts[uid]=current;return current
    for uid in graph['nodes']:visit(uid)
    from .replay import added_reference_rows
    for e,added in added_reference_rows(graph,unique_groups=True):
        uid='p:'+str(e['uid'])
        for parent,role,target in added:
            target_entity=object_map.get(target)
            if not target_entity:continue
            for api,pid,depth in contexts[uid]:
                object_links.add((pid,target_entity))
                parent_entity=object_map.get(parent) if parent else mapping[uid]
                if parent_entity:reference_links.add((pid,parent_entity,target_entity,role))
    dependencies={}
    def dependency(uid,eid,relation,evidence):
        encoded=json.dumps(evidence,sort_keys=True,ensure_ascii=False)
        for api,pid,depth in contexts[uid]:
            dependencies.setdefault((pid,eid,relation),set()).add(encoded)
    for uid,eid in mapping.items():
        for owner in source.containers(eid):dependency(uid,owner,'defined_in',{'member':eid})
    for pid,eid in object_links:
        for owner in source.containers(eid):
            dependencies.setdefault((pid,owner,'defined_in'),set()).add(json.dumps({'member':eid},sort_keys=True))
    for index,read in enumerate(graph.get('source_reads',[])):
        uid='p:'+str(read['uid']);record=read['namespace'];name=read['name']
        eid,reason=source.named(record,name)
        if not eid:
            pending.append(('source_read:'+str(index),reason,json.dumps(read,ensure_ascii=False)));continue
        evidence={k:read[k] for k in ('file','line','offset','scope','name')}
        dependency(uid,eid,'observed_binding_read',evidence)
        for owner in source.containers(eid):dependency(uid,owner,'read_from_container',{'binding':eid})
        for origin in source.import_origins(record,name,graph.get('module_sources',{})):
            dependency(uid,origin,'static_import_origin',{'via_binding':eid,**evidence})
            for owner in source.containers(origin):dependency(uid,owner,'static_import_container',{'via_binding':eid,'origin':origin})
    dependency_rows=[(*k,json.dumps([json.loads(v) for v in sorted(values)],ensure_ascii=False)) for k,values in sorted(dependencies.items())]
    input_digest=key(prepared['manifest']['sha256'])
    policy=[digest(Path(__file__)),digest(Path(__file__).with_name('source_entities.py')),digest(Path(__file__).with_name('changed_objects.py'))]
    import_digest=key([input_digest,policy,source.entities,mapping,sorted(paths),sorted(object_links),dependency_rows])
    summary={'test':graph['test'],'captured_calls':len(graph['nodes']),'canonical_entities':len(source.entities),
             'api_definitions':len(api_entities),'normalized_paths':len(paths),'pending_objects':len(pending),
             'source_dependencies':len(dependency_rows),'source_read_policy':graph.get('source_read_policy'),
             'gaps':graph['gaps'],'raw_evidence':str(prepared['output']),'graph_digest':digest(prepared['output']/'graph.json')}
    if graph.get('reference_depth_limit') is not None:
        summary.update(reference_depth_limit=graph['reference_depth_limit'],object_scope_complete=False,
                       experimental=True,reference_boundary_objects=len(graph.get('reference_boundaries',[])))
    if graph.get('native_storage')=='paths':
        summary.update(native_storage='paths',native_invocations=graph['native_activations'],
                       native_path_nodes=graph['native_path_nodes'],runtime_event_history_preserved=False)
    source.verify_sources()
    return {'schema':'callchain_refresh_plan/1','policy':policy,'slot':slot,
            'input_digest':input_digest,'import_digest':import_digest,'summary':summary,
            'entities':source.entities,'aliases':sorted(aliases),'api_entities':api_entities,
            'api_aliases':sorted(api_aliases),
            'paths':[(pid,*value) for pid,value in sorted(paths.items(),key=lambda x:x[1][4])],
            'supports':sorted(supports),'object_links':sorted(object_links),
            'reference_links':sorted(reference_links),'dependency_rows':dependency_rows,'pending':pending}


def commit_refresh(db_path,plan,cache_mib=4096):
    """Only the final single-writer stage opens and changes the SQL database."""
    if plan.get('schema')!='callchain_refresh_plan/1':raise ValueError('unsupported_refresh_plan')
    policy=[digest(Path(__file__)),digest(Path(__file__).with_name('source_entities.py')),digest(Path(__file__).with_name('changed_objects.py'))]
    if plan.get('policy')!=policy:raise ValueError('refresh_plan_policy_changed')
    slot=plan['slot'];summary=plan['summary'];input_digest=plan['input_digest'];import_digest=plan['import_digest']
    if summary.get('reference_depth_limit') is not None and (not slot.startswith('experiment:reference-depth-2:') or not Path(db_path).name.endswith('.depth2.sqlite')):
        raise ValueError('bounded_capture_requires_isolated_experiment_database')
    entities=plan['entities'];aliases=plan['aliases'];api_entities=plan['api_entities'];api_aliases=plan['api_aliases']
    paths=plan['paths'];supports=plan['supports'];object_links=plan['object_links'];reference_links=plan['reference_links']
    dependency_rows=plan['dependency_rows'];pending=plan['pending']
    db=connect(db_path,cache_mib=cache_mib)
    try:
        db.execute('BEGIN IMMEDIATE')
        existing=db.execute('SELECT import_digest FROM capture_slots WHERE id=?',(slot,)).fetchone()
        if existing and existing[0]==import_digest:
            db.rollback();return {**summary,'status':'unchanged','slot':slot}
        db.execute('DELETE FROM capture_slots WHERE id=?',(slot,))
        db.execute('INSERT INTO capture_slots VALUES(?,?,?,?,?,?,?)',(slot,summary['test'],input_digest,import_digest,summary['raw_evidence'],'bounded' if summary.get('reference_depth_limit') is not None else ('partial' if summary['gaps'] else 'current'),json.dumps(summary,ensure_ascii=False)))
        versions={(e['path'],e['source_sha256']) for e in entities.values() if e['path'] and e['source_sha256']}
        for path,sha in versions:
            db.execute("UPDATE capture_slots SET status='stale' WHERE id<>? AND id IN (SELECT s.slot_id FROM slot_entities s JOIN entities e ON e.id=s.entity_id WHERE e.path=? AND s.source_sha256 IS NOT NULL AND s.source_sha256<>?)",(slot,path,sha))
            db.execute("UPDATE entities SET status='stale' WHERE path=? AND source_sha256 IS NOT NULL AND source_sha256<>?",(path,sha))
        for eid,e in entities.items():
            old=db.execute('SELECT source_sha256 FROM entities WHERE id=?',(eid,)).fetchone()
            if old and old[0] and e['source_sha256'] and old[0]!=e['source_sha256']:
                db.execute("UPDATE capture_slots SET status='stale' WHERE id<>? AND id IN (SELECT slot_id FROM slot_entities WHERE entity_id=?)",(slot,eid))
            db.execute('INSERT INTO entities VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,name=excluded.name,source_sha256=excluded.source_sha256,status=excluded.status,detail=excluded.detail',
                       (eid,e['path'],e['kind'],e['name'],e['source_sha256'],e['language'],e['status'],json.dumps(e,ensure_ascii=False)))
            db.execute('INSERT INTO slot_entities VALUES(?,?,?)',(slot,eid,e['source_sha256']))
        db.executemany('INSERT OR IGNORE INTO aliases VALUES(?,?,?)',sorted(aliases))
        db.executemany('INSERT OR IGNORE INTO apis VALUES(?,?)',sorted(api_entities.items()))
        db.executemany('INSERT OR IGNORE INTO api_aliases VALUES(?,?)',sorted(api_aliases))
        db.executemany('INSERT INTO api_alias_support VALUES(?,?,?)',[(slot,*x) for x in sorted(api_aliases)])
        for value in paths:db.execute('INSERT OR IGNORE INTO paths VALUES(?,?,?,?,?,?)',value)
        db.executemany('INSERT INTO path_support VALUES(?,?)',[(slot,p) for p in sorted(supports)])
        db.executemany('INSERT INTO observed_objects VALUES(?,?,?)',[(slot,*x) for x in sorted(object_links)])
        db.executemany('INSERT INTO object_references VALUES(?,?,?,?,?)',[(slot,*x) for x in sorted(reference_links)])
        db.executemany('INSERT INTO source_dependencies VALUES(?,?,?,?,?)',[(slot,*x) for x in dependency_rows])
        db.executemany('INSERT INTO pending VALUES(?,?,?,?)',[(slot,*x) for x in pending])
        # Scan the covering identity index in key order, so support lookups
        # follow the same order instead of randomly probing the entire history.
        # Select rowids to retain the previous cleanup semantics for NULL ids.
        db.execute('DELETE FROM paths WHERE rowid IN (SELECT p.rowid FROM paths p WHERE NOT EXISTS (SELECT 1 FROM path_support s WHERE s.path_id=p.id) ORDER BY p.id)')
        if db.execute('PRAGMA foreign_key_check').fetchall():raise ValueError('database_foreign_key_check_failed')
        db.commit()
    except BaseException:
        db.rollback();raise
    finally:db.close()
    return {**summary,'status':'refreshed','slot':slot}


def refresh(db_path,prepared,roots=None,slot=None,definitions=None,cache_mib=4096):
    """Compatibility entry point: prepare then atomically commit one test."""
    return commit_refresh(db_path,prepare_refresh(prepared,roots,slot,definitions),cache_mib)


def list_apis(db_path):
    db=connect(db_path,readonly=True)
    try:
        return [dict(r) for r in db.execute('''SELECT DISTINCT a.alias,a.api_id,e.name AS source_name,e.path,e.status
              FROM api_alias_support a JOIN capture_slots ac ON ac.id=a.slot_id
              JOIN apis api ON api.id=a.api_id JOIN entities e ON e.id=api.entity_id
              JOIN paths p ON p.api_id=api.id JOIN path_support s ON s.path_id=p.id
              JOIN capture_slots c ON c.id=s.slot_id WHERE c.status IN ('current','partial') AND ac.status IN ('current','partial') ORDER BY a.alias''')]
    finally:db.close()


def query(db_path,api):
    db=connect(db_path,readonly=True)
    try:
        db.execute('BEGIN')  # All parts of an export see one committed refresh.
        apis=[r[0] for r in db.execute("SELECT a.api_id FROM api_alias_support a JOIN capture_slots c ON c.id=a.slot_id WHERE a.alias=? AND c.status IN ('current','partial') UNION SELECT id FROM apis WHERE id=?",(api,api))]
        if not apis:raise ValueError('API not present in observed database: '+api)
        placeholders=','.join('?' for _ in apis)
        nodes=[dict(r) for r in db.execute(f'''SELECT DISTINCT p.* FROM paths p JOIN path_support s ON s.path_id=p.id
             JOIN capture_slots c ON c.id=s.slot_id WHERE p.api_id IN ({placeholders}) AND c.status IN ('current','partial') ORDER BY p.depth,p.id''',apis)]
        refs=[dict(r) for r in db.execute(f'''SELECT DISTINCT o.path_id,o.entity_id FROM observed_objects o
             JOIN capture_slots c ON c.id=o.slot_id JOIN paths p ON p.id=o.path_id
             WHERE c.status IN ('current','partial') AND p.api_id IN ({placeholders})''',apis)]
        references=[dict(r) for r in db.execute(f'''SELECT DISTINCT o.owner_path,o.source_entity,o.target_entity,o.role
             FROM object_references o JOIN capture_slots c ON c.id=o.slot_id JOIN paths p ON p.id=o.owner_path
             WHERE c.status IN ('current','partial') AND p.api_id IN ({placeholders})''',apis)]
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        dependencies=[dict(r) for r in db.execute(f'''SELECT o.path_id,o.entity_id,o.relation,o.evidence,o.slot_id
             FROM source_dependencies o JOIN capture_slots c ON c.id=o.slot_id JOIN paths p ON p.id=o.path_id
             WHERE c.status IN ('current','partial') AND p.api_id IN ({placeholders}) ORDER BY o.path_id,o.entity_id,o.relation,o.slot_id''',apis)] if 'source_dependencies' in tables else []
        for row in dependencies:row['evidence']=json.loads(row['evidence'])
        evidence=[dict(r) for r in db.execute(f'''SELECT DISTINCT c.id,c.test,c.status,c.evidence_directory,c.summary FROM capture_slots c
            JOIN path_support s ON s.slot_id=c.id JOIN paths p ON p.id=s.path_id
            WHERE p.api_id IN ({placeholders}) AND c.status IN ('current','partial')''',apis)]
        used={n['entity_id'] for n in nodes}|{r['entity_id'] for r in refs}|{r[k] for r in references for k in ('source_entity','target_entity')}|{r['entity_id'] for r in dependencies}
        entities={}
        ordered=sorted(used)
        for index in range(0,len(ordered),500):
            batch=ordered[index:index+500];marks=','.join('?' for _ in batch)
            entities.update((r['id'],json.loads(r['detail'])) for r in db.execute(f'SELECT id,detail FROM entities WHERE id IN ({marks})',batch))
        return {'api':api,'nodes':nodes,'entities':entities,'observed_objects':refs,'object_references':references,
                'source_dependencies':dependencies,
                'evidence':evidence,'semantics':'observed canonical paths, not a synthetic single execution',
                'runtime_event_history_in_database':False}
    finally:db.close()


def render_api(db_path,api,output):
    from . import presentation
    doc=query(db_path,api);calls={};objects={};static={};roots=[]
    for eid,e in doc['entities'].items():
        static[eid]={'status':e['status'],'qualname':e['name'],'file':e['path'],'first_line':e['line'],
                     'end_line':e['end_line'],'text':e.get('source_text'),'evidence':e['evidence']}
    for n in doc['nodes']:
        e=doc['entities'][n['entity_id']]
        calls[n['id']]={'uid':n['id'],'parent':n['parent_id'],'kind':'canonical_source_path','name':e['name'],
                        'definition':n['entity_id'],'children':[],'objects':[],'reference_observations':[],
                        'returned':True,'relation':n['edge_kind']}
        if n['parent_id'] is None:roots.append(n['id'])
    for n in doc['nodes']:
        if n['parent_id']:calls[n['parent_id']]['children'].append(n['id'])
    for ref in doc['observed_objects']:
        eid=ref['entity_id'];objects[eid]={**doc['entities'][eid],'static_definition':eid}
        calls[ref['path_id']]['objects'].append(eid)
    for ref in doc['object_references']:
        calls[ref['owner_path']]['reference_observations'].append({
            'scope':'merged_source_entities','source':ref['source_entity'],
            'target':ref['target_entity'],'role':ref['role']})
    gaps=sorted({g for r in doc['evidence'] for g in json.loads(r['summary'])['gaps']})
    tree={'schema':'sql-canonical-api/1','test':api,'calls':calls,'objects':objects,'roots':roots,'detached':[],
          'definitions':{eid:{'static_definition':eid} for eid in static},'static_definitions':static,'gaps':gaps,
          'boundaries':['SQL source-entity aggregation: repeated instances and aliases share source definitions.',
                        'Paths preserve API and ancestor context; aggregate relationships do not assert one chronological execution.',
                        'Only observed captures are included; unobserved PyTorch/TorchNPU APIs are not invented.'],
          'stats':{'sql_query':True,'captures':len(doc['evidence'])}}
    original=presentation.PAGE
    try:
        presentation.PAGE=original.replace('真实测试 · 调用树','C03 · SQL API 源码调用链').replace('次调用 ·','个归一化路径节点 ·').replace('个对象地址/类型记录','个已观测源码对象').replace('主线只显示实际函数调用','主线显示有运行证据的归一化源码路径')
        presentation.render(tree,output,'C03')
    finally:presentation.PAGE=original
    save(Path(output)/'database-query.json',doc)
    return {'html':str(Path(output)/'C03.html'),'paths':len(calls),'source_entities':len(doc['entities'])}
