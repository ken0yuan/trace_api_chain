"""Run the frozen pipeline whitelist with existing, single-item trace captures.

This adapter does not modify the pipeline, source tests, installed libraries or
trace plugin. Every whitelist entry is reconciled, including collection errors.
"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import collections, hashlib, json, os, queue, shutil, signal, sqlite3, subprocess, sys, threading, time, traceback

OUT = Path(__file__).resolve().parent
R = Path('/home/workspace-ycy/compile-performance')
V = R / 'cases/instrumented-r9-lld22-exclude-std'
SOURCE = V / 'pytorch'
PY = V / 'venv311/bin/python'
REPLICA = Path('/home/workspace-ycy/ci-flow-replica-20261027-01a17c01')
VENDOR = REPLICA / 'vendor/.github'
WHITE = OUT / 'whitelist.json'
LEDGER = R / 'runtime-scenarios-c03-c05/api-ledger-10362.json'
DEVICES = [0, 1, 2, 3, 4, 5, 6, 7]
LOCK = threading.Lock()
STOP = threading.Event()
COUNTS = collections.Counter()
PIPELINE = None
WRITER_SESSION = None
START = float(os.environ.get('CALLCHAIN_JOB_STARTED_AT',time.time()))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def read(path, fallback=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return fallback


def status(stage, **extra):
    with LOCK:
        write(OUT/'status.json', dict(stage=stage, pid=os.getpid(), started_at=START,
              updated_at=time.time(), elapsed_seconds=time.time()-START,
              counters=dict(COUNTS), **extra))


def env(devices='7',runtime=None):
    e = os.environ.copy()
    for k in ('LD_PRELOAD', 'LD_AUDIT', 'PYTEST_PLUGINS', 'PYTEST_ADDOPTS',
              'TRACE_API_CHAIN_INTERNAL', 'SCENARIO_API_FILTER', 'CI',
              'PYTORCH_TESTING_DEVICE_FOR_CUSTOM', 'DISABLED_TESTS_FILE'):
        e.pop(k, None)
    site = V/'venv311/lib/python3.11/site-packages'
    e.update(PYTHONPATH=':'.join(map(str, [runtime or OUT/'plugin-runtime-035-private', site, OUT, SOURCE,
        SOURCE/'test', SOURCE/'test/distributed/pipelining',
        SOURCE/'test/distributed/local_tensor_tutorial_examples', VENDOR/'scripts'])),
        PYTHONNOUSERSITE='1', PYTHONSAFEPATH='1', PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1',
        TORCH_DEVICE_BACKEND_AUTOLOAD='1', PYTORCH_TEST_NPU='1', NO_TD='1',
        PYTORCH_TESTING_DEVICE_ONLY_FOR='privateuse1',
        PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1',
        OPENBLAS_NUM_THREADS='1', ASCEND_RT_VISIBLE_DEVICES=devices)
    for key, path in [('PATH', R/'system-tools/root/usr/bin'),
                      ('C_INCLUDE_PATH', R/'system-tools/root/usr/include'),
                      ('LIBRARY_PATH', R/'system-tools/root/usr/lib/aarch64-linux-gnu'),
                      ('LD_LIBRARY_PATH', R/'system-tools/root/usr/lib/aarch64-linux-gnu')]:
        e[key] = str(path) + (':' + e[key] if e.get(key) else '')
    return e


def run(command, log, environment, timeout):
    start = time.monotonic()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('w') as output:
        output.write(json.dumps({'command':list(map(str,command)), 'devices':environment.get('ASCEND_RT_VISIBLE_DEVICES')})+'\n')
        output.flush()
        p = subprocess.Popen(list(map(str,command)), cwd=SOURCE, env=environment,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        timedout = False
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timedout = True
            os.killpg(p.pid, signal.SIGTERM)
            try: p.wait(timeout=10)
            except subprocess.TimeoutExpired: os.killpg(p.pid, signal.SIGKILL); p.wait()
            rc = 124
        finally:
            # Only this explicitly created job's process group; clean any orphan
            # child left by plugin's own subprocess timeout before returning card.
            try: os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError: pass
    return {'returncode':rc, 'elapsed_seconds':time.monotonic()-start, 'timeout':timedout}


def collect_file(file):
    token = hashlib.sha256(file.encode()).hexdigest()[:16]
    dump = OUT/'collection'/f'{token}.json'
    e = env(); e['TRACE_WHITELIST_COLLECTION'] = str(dump)
    cmd = [PY, '-m', 'pytest', '-c', SOURCE/'pytest.ini', '--rootdir', SOURCE,
           '-p', 'whitelist_trace_collect_035', '--collect-only', '-q', file]
    result = run(cmd, OUT/'collection'/f'{token}.log', e, 1800)
    items = read(dump, [])
    result.update(file=file, items=items, log=str(OUT/'collection'/f'{token}.log'))
    write(OUT/'collection'/f'{token}.receipt.json', result)
    with LOCK: COUNTS['files_collected'] += 1
    status('collecting', last_file=file, last_returncode=result['returncode'])
    print('COLLECT',file,result['returncode'],len(items),flush=True)
    return result


def category(file, config):
    cats = config['categories']
    for name,cfg in cats.items():
        if file in cfg.get('files',[]): return name
    for name,cfg in cats.items():
        if any(file.startswith(p.rstrip('/')+'/') for p in cfg.get('paths',[])): return name
    return 'others'


def collect():
    import yaml
    reuse = read(OUT/'collection-reused.json')
    if reuse:
        assert hashlib.sha256(WHITE.read_bytes()).hexdigest() == reuse['whitelist_sha256']
        for file, digest in reuse['test_file_sha256'].items():
            assert hashlib.sha256((SOURCE/file).read_bytes()).hexdigest() == digest, file
        return read(OUT/'plan.json')
    white = read(WHITE)
    by_file = collections.defaultdict(set)
    for n in white: by_file[n.split('::')[0]].add(n)
    cfg = yaml.safe_load((OUT/'categories.yml').read_text())
    plan, missing, receipts = [], [], []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(collect_file,f) for f in sorted(by_file)]
        for future in as_completed(futures):
            r = future.result(); receipts.append({k:v for k,v in r.items() if k!='items'})
            found = {i['nodeid']: i for i in r['items']}
            for n in sorted(by_file[r['file']]):
                if n in found and r['returncode']==0:
                    cat=category(r['file'],cfg); marks=found[n]['markers']
                    plan.append(dict(nodeid=n, file=r['file'], category=cat, markers=marks,
                        isolated=('distributed/' in r['file'] or cat=='distributed' or 'multigpu' in marks or 'serial' in marks),
                        capture_scope=('parent_process_calling_thread_only; child ranks not validated' if 'distributed/' in r['file'] else 'calling_thread_host_objects; compiled graph/device internals not guaranteed' if cat=='graph' else 'calling_thread_instrumented_host_objects')))
                else:
                    missing.append(dict(nodeid=n,reason='collection_error' if r['returncode'] else 'not_collected_in_current_source_environment',log=r['log']))
    plan.sort(key=lambda x:(x['isolated'],x['category'],x['nodeid']))
    for i,item in enumerate(plan):item['id']=f'{i:06d}-'+hashlib.sha256(item['nodeid'].encode()).hexdigest()[:12]
    assert len(plan)+len(missing)==len(white)
    write(OUT/'plan.json',plan);write(OUT/'not-runnable.json',missing)
    write(OUT/'collection-summary.json',dict(whitelist_count=len(white), runnable=len(plan),
        not_runnable=len(missing), missing_reasons=dict(collections.Counter(m['reason'] for m in missing)),
        categories=dict(collections.Counter(i['category'] for i in plan)),
        isolated_cases=sum(i['isolated'] for i in plan), files=receipts))
    return plan


def resource_ok():
    # Preserve useful evidence and avoid filling the user's shared filesystem.
    if (OUT/'STOP').exists() or shutil.disk_usage(OUT).free < 150*1024**3:
        STOP.set();return False
    return not STOP.is_set()


def initialize_relations(plan):
    path=OUT/'api-test-relations.sqlite'
    if path.exists():
        # Resume the verified migrated database without replacing any completed evidence.
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
            assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            schema=json.loads(db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()[0])
            assert schema=='api_test_relations_single_database/2'
            assert db.execute('SELECT count(*) FROM apis').fetchone()[0]==len(read(LEDGER)['apis'])
        return
    with sqlite3.connect(path) as db:
        db.executescript('''
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE apis(id INTEGER PRIMARY KEY,api_id TEXT UNIQUE,name TEXT,language TEXT,kind TEXT);
CREATE TABLE modules(id INTEGER PRIMARY KEY,module TEXT UNIQUE,file TEXT,capture_state TEXT);
CREATE TABLE tests(id INTEGER PRIMARY KEY,module_id INTEGER REFERENCES modules(id),nodeid TEXT,file TEXT,capture_state TEXT,source_version TEXT,source_attempt TEXT,UNIQUE(module_id,nodeid));
CREATE TABLE phases(id INTEGER PRIMARY KEY,name TEXT UNIQUE);
CREATE TABLE alias_groups(id INTEGER PRIMARY KEY,api_ids_json TEXT UNIQUE);
CREATE TABLE observations(api_key INTEGER REFERENCES apis(id),test_key INTEGER REFERENCES tests(id),phase_key INTEGER REFERENCES phases(id),alias_group_key INTEGER REFERENCES alias_groups(id),PRIMARY KEY(api_key,test_key,phase_key,alias_group_key));
CREATE TABLE test_phases(test_id INTEGER REFERENCES tests(id),phase_id INTEGER REFERENCES phases(id),state TEXT,PRIMARY KEY(test_id,phase_id));
CREATE TABLE capture_gaps(module_id INTEGER REFERENCES modules(id),test_id INTEGER REFERENCES tests(id),phase TEXT,reason TEXT);
''')
        db.execute('INSERT INTO metadata VALUES (?,?)',('schema',json.dumps('api_test_relations_single_database/2')))
        db.executemany('INSERT INTO apis(api_id,name,language,kind) VALUES (?,?,?,?)',
            [(a['api_id'],a['qualified_name'],a['language'],a['kind']) for a in read(LEDGER)['apis']])
        db.executemany('INSERT INTO modules(module,file,capture_state) VALUES (?,?,?)',
            [(file,'pytorch/'+file,'pending') for file in sorted({i['file'] for i in plan})])


def attempt_counts(result):
    reports=result.get('test_reports',[])
    outcome='failed' if any(x['outcome']=='failed' for x in reports) else 'skipped' if any(x['outcome']=='skipped' for x in reports) else 'passed' if any(x.get('when')=='call' and x['outcome']=='passed' for x in reports) else 'no_test_result'
    return collections.Counter({'attempted':1,'capture_'+result.get('capture',{}).get('status','missing'):1,
        'relations_'+result.get('relations',{}).get('state','missing'):1,'test_'+outcome:1})


def artifact_base(result):
    base=Path(result.get('artifact_base',OUT)).resolve()
    if base!=OUT.resolve() and base.parent!=(OUT/'retry-attempts').resolve():
        raise ValueError('attempt_artifact_base_outside_task')
    return base


def execute(item, devices):
    if not resource_ok():return
    ident=item['id'];result_path=OUT/'results'/f'{ident}.json';previous=read(result_path)
    if previous and not previous.get('retry_pending'):return previous
    base=OUT
    if previous:
        batch=previous['retry_pending']['batch']
        if not isinstance(batch,str) or not batch or len(batch)>64 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in batch):
            raise ValueError('invalid_retry_batch')
        base=artifact_base({'artifact_base':str(OUT/'retry-attempts'/batch)})
        if (base/'captures'/ident).exists():raise ValueError('unfinished_retry_evidence_preserved:'+ident)
    for name in ('captures','relations','logs','junit'):(base/name).mkdir(parents=True,exist_ok=True)
    cap=base/'captures'/ident;log=base/'logs'/f'{ident}.log'
    cmd=[PY,'-m','pytest','-c',SOURCE/'pytest.ini','--rootdir',SOURCE,
         '-p','whitelist_trace_collect_035','-p','api_test_relations.pytest_plugin',
         '-p','trace_api_chain.pytest_plugin', item['nodeid'],'-q','--tb=short',
         '--callchain-out',cap,'--callchain-mode','full','--callchain-native-storage','paths',
         '--callchain-api-catalog',LEDGER,'--callchain-max-events','10000000',
         '--callchain-max-paths','4000000','--callchain-max-references','1000000',
         '--callchain-timeout',str(item.get('capture_timeout_seconds',1800)),'--junitxml',base/'junit'/f'{ident}.xml',
         '--api-relations-out',base/'relations'/ident,'--api-relations-repo',SOURCE,
         '--api-relations-catalog',LEDGER,'--api-relations-import','torch_npu',
         '--api-relations-label','ascend-npu-dual-plugins']
    run_result=run(cmd,log,env(devices),item.get('capture_timeout_seconds',1800)+120)
    capture=read(cap/'capture-result.json',{})
    receipt=read(cap/'pytest-receipt.json',{})
    relations=read(base/'relations'/ident/'progress.json',{})
    inner_timeout=capture.get('error_type')=='TimeoutExpired'
    run_result['timeout']=run_result['timeout'] or inner_timeout
    run_result['timeout_layer']='capture_worker' if inner_timeout else 'outer_controller' if run_result['timeout'] else None
    result={**item,**run_result,'execution_host':'179.163.3.2','hardware':'910B2','devices':devices,'capture':capture,
            'test_reports':receipt.get('reports',[]),'test_body_started':receipt.get('started'),
            'test_body_ended':receipt.get('ended'),'capture_dir':str(cap),
            'log':str(log),'relations':relations,'relations_dir':str(base/'relations'/ident),
            'artifact_base':str(base),'dual_capture_valid':False,
            'replay_status':'pending' if capture.get('status')=='captured' else 'not_eligible'}
    if previous:
        result['previous_attempts']=previous.get('previous_attempts',[])+[previous['retry_pending']['history']]
    write(result_path,result)
    with LOCK:
        current=attempt_counts(result);old=attempt_counts(previous) if previous else collections.Counter()
        for key in current.keys() | old.keys():COUNTS[key]+=current[key]-old[key]
        if previous:COUNTS['repair_retry_attempts']+=1
    reports=receipt.get('reports',[])
    outcome='failed' if any(x['outcome']=='failed' for x in reports) else 'skipped' if any(x['outcome']=='skipped' for x in reports) else 'passed' if any(x.get('when')=='call' and x['outcome']=='passed' for x in reports) else 'no_test_result'
    status('running',last_nodeid=item['nodeid'])
    print('CAPTURE',ident,capture.get('status'),outcome,round(run_result['elapsed_seconds'],2),flush=True)
    PIPELINE.submit(result_path)
    return result


def verified_replay_report(cap,slot,database):
    """Trust the committed replay result only when its graph bytes still match."""
    report=read(cap/'replay-result.json',{})
    valid=bool(report) and report.get('status') in ('refreshed','unchanged') and report.get('slot')==slot and report.get('gaps')==[]
    if valid:
        try:
            with (cap/'graph.json').open('rb') as stream:graph_sha=hashlib.file_digest(stream,'sha256').hexdigest()
            valid=graph_sha==report.get('graph_digest')
            db=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)
            try:row=db.execute('SELECT status,summary FROM capture_slots WHERE id=?',(slot,)).fetchone()
            finally:db.close()
            stored=json.loads(row[1]) if row else {}
            valid=valid and row is not None and row[0]=='current' and stored.get('gaps')==[] and stored.get('graph_digest')==graph_sha
        except (OSError,ValueError,sqlite3.Error):valid=False
    return valid,{'status':'observed_scope_validated' if valid else 'unverified_or_incomplete',
        'gaps':report.get('gaps'),'graph_digest':report.get('graph_digest'),
        'native_activations':report.get('native_invocations'),'captured_calls':report.get('captured_calls'),
        'verification':'replay result + graph SHA256 + committed database slot'}



"""Injected into the existing whitelist adapter; no collector semantics change."""
LEGACY_POLICY={'staged.py': '6d60fca259f9c4de8c3fc044615e5b012658b3afc2b90060948e1e448e7f8156', 'database.py': '3e35c4431c34eb1e3e43b04f22fafbee5f8220c868ca924e4b57b689bc9a4108', 'pipeline.py': 'bc4eee52713fca2acb3a53fba9c08b46bd49f2cdedd1b53e7aa9c9deba9c615a', 'replay.py': '2032c60b9d194804c9f68ade45dc78b5b60a58697bc2dbf74659285f0c97cab2', 'compact.py': '71fa80b3c40ed8c9ffb8205d311c300312b82ee182ad6870938111c8ab3ea3ea', 'symbols.py': '4c90e0329f4cfabea69e6c91a1c00351cb17e4b50d4b75a04fb8f3adf14bd965', 'source_entities.py': '1552d3e29de3a79d867e386a483cebe00ea5c12fd6a868689504fa06ee43e4e3'}
LEGACY_NATIVE_SHA256='b4ec02bef08d8fd9d123a86082fbfa3864232a946a891a464a255b919bd5a163'
LEGACY_RUNTIME=OUT/'plugin-runtime-034-private'
LEGACY_LOADER=None


def preparation_runtime(cap,target):
    from trace_api_chain.staged import policy
    receipt=target.with_name(target.name+'.ready.json')
    if receipt.exists():
        observed=read(receipt,{}).get('policy')
        if observed==LEGACY_POLICY:return LEGACY_RUNTIME
        if observed==policy():return OUT/'plugin-runtime-035-private'
        raise ValueError('unknown_frozen_handoff_policy')
    with (cap/'events.jsonl').open() as stream:first=json.loads(stream.readline())
    # A completed handoff stays bound to its exact frozen reader/importer above.
    # Raw captures with no handoff can use the current lossless reader and its
    # per-task source indexes, including captures collected by older runtimes.
    if first.get('reference_policy') in ('shared-adjacency-states/2','shared-adjacency-states/1','reference-deltas/1'):
        return OUT/'plugin-runtime-035-private'
    raise ValueError('unknown_capture_reference_policy')


def offline_symbol_index_args(runtime):
    value=os.environ.get('CALLCHAIN_READONLY_SYMBOL_INDEX')
    if not value or Path(runtime)!=OUT/'plugin-runtime-035-private':return []
    path=Path(value)
    if not path.is_absolute():raise ValueError('readonly_symbol_index_must_be_absolute')
    return ['--symbol-index',str(path)]


def organize(path):
    result=read(path);cap=Path(result['capture_dir']);base=artifact_base(result)
    if result.get('finalization',{}).get('state')=='cleaned':return {'kind':'cleaned'}
    if result.get('finalization',{}).get('state')=='verified_committed':return {'kind':'cleanup_only','graph':str(cap/'graph.json')}
    if result.get('replay_status')=='complete':return {'kind':'existing','graph':str(cap/'graph.json')}
    if result.get('capture',{}).get('status')!='captured':return {'kind':'ineligible'}
    target=base/'normalizations'/result['id']/'refresh-plan.json'
    command=[PY,'-c','from trace_api_chain.cli import main;raise SystemExit(main())','prepare',cap,
             '--out',target,'--slot',result['id'],'--source-root','pytorch='+str(SOURCE),
             '--source-root','torch_npu='+str(V/'torch-npu')]
    runtime=preparation_runtime(cap,target)
    command+=offline_symbol_index_args(runtime)
    prepared=run(command,base/'prepare-logs'/f"{result['id']}.log",env('',runtime=runtime),7200)
    result['preparation']=prepared;result['preparation'].update(plan=str(target),runtime=str(runtime))
    if prepared['returncode']:
        result['preparation']['status']='failed';write(path,result);raise RuntimeError('offline_preparation_failed:'+result['id'])
    ready=read(target.with_name(target.name+'.ready.json'),{})
    if ready.get('gaps')!=[]:raise ValueError('offline_preparation_has_gaps:'+result['id'])
    result['preparation'].update(status='ready',plan_sha256=ready['sha256'],workspace=ready['workspace'])
    write(path,result)
    with LOCK:COUNTS['organized']+=1
    status('running',last_organized=result['nodeid'])
    print('ORGANIZED',result['id'],round(prepared['elapsed_seconds'],2),flush=True)
    return {'kind':'prepared','plan':str(target),'graph':str(Path(ready['workspace'])/'graph.json')}


def verify_graph_result(cap,slot,database,graph):
    report=read(cap/'replay-result.json',{})
    valid=bool(report) and report.get('status') in ('refreshed','unchanged') and report.get('slot')==slot and report.get('gaps')==[]
    if valid:
        with Path(graph).open('rb') as stream:checksum=hashlib.file_digest(stream,'sha256').hexdigest()
        db=sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)
        try:row=db.execute('SELECT status,summary FROM capture_slots WHERE id=?',(slot,)).fetchone()
        finally:db.close()
        stored=json.loads(row[1]) if row else {}
        valid=row is not None and row[0]=='current' and stored.get('gaps')==[] and checksum==report.get('graph_digest')==stored.get('graph_digest')
    return valid,{'status':'observed_scope_validated' if valid else 'unverified_or_incomplete','gaps':report.get('gaps'),
                  'graph_digest':report.get('graph_digest'),'native_activations':report.get('native_invocations'),
                  'captured_calls':report.get('captured_calls'),'verification':'independent graph SHA + committed slot + persisted cleanup receipt'}


def append_relationships(result):
    # An earlier process may have committed successfully but exited before its
    # controller recorded the receipt. Verify that committed record first.
    from trace_api_chain.finalize import committed_state
    digest=result['graph_validation']['graph_digest']
    try:committed_state(OUT/'api-callchains.sqlite',OUT/'api-test-relations.sqlite',result,digest)
    except (ValueError,sqlite3.Error):
        command=[PY,'-m','api_test_relations','append','--out',result['relations_dir'],
                 '--database',OUT/'api-test-relations.sqlite','--module',result['file']]
        merged=run(command,artifact_base(result)/'relation-merge-logs'/f"{result['id']}.log",env(''),600)
        result['relations_merge']=merged
        committed_state(OUT/'api-callchains.sqlite',OUT/'api-test-relations.sqlite',result,digest)
    result.setdefault('relations_merge',{}).update(verified_committed_receipt=True)
    result['relations_database']=str(OUT/'api-test-relations.sqlite')


def commit_organized(path,prepared):
    from trace_api_chain.finalize import cleanup_verified
    result=read(path);cap=Path(result['capture_dir']);base=artifact_base(result);kind=prepared['kind']
    if kind=='cleaned':return
    if kind=='ineligible':
        # An incomplete trace is never discarded or promoted to complete.
        write(path,result);return
    old_status=result.get('replay_status');old_dual=result.get('dual_capture_valid') is True
    old_clean=result.get('relations_raw_removed') is True
    if kind=='prepared':
        global WRITER_SESSION,LEGACY_LOADER
        from trace_api_chain.writer_session import WriterSession
        # This callback belongs exclusively to the single sqlite-writer thread.
        # Keep its verified connection/cache between contributions; API append
        # remains sequential below, after the graph transaction completes.
        if WRITER_SESSION is None:WRITER_SESSION=WriterSession(OUT/'api-callchains.sqlite',cache_mib=4096,preserve_prior_slots=True)
        started=time.monotonic()
        ready=read(Path(prepared['plan']).with_name(Path(prepared['plan']).name+'.ready.json'))
        loader=None
        if ready['policy']==LEGACY_POLICY:
            from trace_api_chain.handoff_compat import FrozenHandoffLoader
            if LEGACY_LOADER is None:LEGACY_LOADER=FrozenHandoffLoader(LEGACY_RUNTIME,LEGACY_POLICY,LEGACY_NATIVE_SHA256)
            loader=LEGACY_LOADER
        report=WRITER_SESSION.commit_file(prepared['plan'],loader=loader)
        report['handoff_reader']='frozen-0.3.4' if loader else 'active-0.3.5'
        committed={'returncode':0,'elapsed_seconds':time.monotonic()-started,'timeout':False,
                   'writer_validation':report['writer_validation'],'handoff_reader':report['handoff_reader']}
        write(base/'commit-logs'/f"{result['id']}.log",report)
        result['replay']=committed
    if kind=='cleanup_only':
        from trace_api_chain.finalize import committed_state
        committed_state(OUT/'api-callchains.sqlite',OUT/'api-test-relations.sqlite',result,result['finalization']['graph_sha256'])
    else:
        valid,result['graph_validation']=verify_graph_result(cap,result['id'],OUT/'api-callchains.sqlite',prepared['graph'])
        if not valid:write(path,result);raise ValueError('committed_graph_not_complete:'+result['id'])
    append_relationships(result)
    result['replay_status']='complete';result['dual_capture_valid']=bool(result.get('test_body_ended')) and result.get('relations',{}).get('gaps')==0
    if not result['dual_capture_valid']:write(path,result);raise ValueError('dual_capture_not_valid:'+result['id'])
    write(path,result)
    if any(r.get('outcome')=='failed' for r in result.get('test_reports',[])):
        import xml.etree.ElementTree as ET
        junit=base/'junit'/f"{result['id']}.xml"
        if junit.exists():
            tree=ET.parse(junit)
            result['test_diagnostics']=[{'kind':node.tag,'message':node.get('message'),'detail':node.text} for node in tree.iter() if node.tag in ('failure','error')]
    roots=[base/name for name in ['captures','relations','logs','junit','normalizations','prepare-logs','commit-logs','replay-logs','relation-merge-logs']]
    targets=[cap,base/'normalizations'/result['id'],Path(result['relations_dir']),
             base/'logs'/f"{result['id']}.log",base/'junit'/f"{result['id']}.xml",
             *[base/name/f"{result['id']}.log" for name in ['prepare-logs','commit-logs','replay-logs','relation-merge-logs']]]
    cleanup_verified(result,path,OUT/'api-callchains.sqlite',OUT/'api-test-relations.sqlite',prepared['graph'],targets,roots)
    result['relations_raw_removed']=True;write(path,result)
    with LOCK:
        if old_status!='complete':
            if old_status in ('failed','incomplete'):COUNTS['replay_'+old_status]-=1
            COUNTS['replay_complete']+=1
        if not old_dual:COUNTS['dual_valid']+=1
        if not old_clean:COUNTS['relations_merged_and_cleaned']+=1
        COUNTS['intermediates_cleaned']+=1
    status('running',last_committed=result['nodeid'])
    print('COMMITTED_AND_CLEANED',result['id'],flush=True)


def close_writer():
    global WRITER_SESSION
    if WRITER_SESSION is not None:
        WRITER_SESSION.close()
        WRITER_SESSION=None


def pipeline_error(path,error):
    result=read(path,{})
    result['pipeline_error']={'type':type(error).__name__,'message':str(error),'at':time.time()}
    if result.get('replay_status')!='complete':result['replay_status']='failed';result['dual_capture_valid']=False
    write(path,result);STOP.set()
    with LOCK:COUNTS['pipeline_errors']+=1
    status('pipeline_error',last_error=result['pipeline_error'])
    print('PIPELINE_ERROR',result.get('id'),repr(error),flush=True)


def main():
    global PIPELINE
    sys.path.insert(0,str(OUT/'plugin-runtime-035-private'))
    from trace_api_chain.dispatch import ParallelReplay
    for n in ('collection','logs','captures','relations','results','junit','replay-logs','relation-merge-logs','prepare-logs','commit-logs','normalizations'):(OUT/n).mkdir(exist_ok=True)
    # Resume every completed attempt; failed/incomplete attempts remain visible.
    for p in (OUT/'results').glob('*.json'):
        previous=read(p,{})
        if not previous:continue
        COUNTS['attempted']+=1
        COUNTS['capture_'+previous.get('capture',{}).get('status','missing')]+=1
        COUNTS['relations_'+previous.get('relations',{}).get('state','missing')]+=1
        reports=previous.get('test_reports',[])
        outcome='failed' if any(x['outcome']=='failed' for x in reports) else 'skipped' if any(x['outcome']=='skipped' for x in reports) else 'passed' if any(x.get('when')=='call' and x['outcome']=='passed' for x in reports) else 'no_test_result'
        COUNTS['test_'+outcome]+=1
        if (previous.get('relations_merge',{}).get('returncode')==0 or previous.get('relations_merge',{}).get('verified_committed_receipt')) and previous.get('relations_raw_removed'):COUNTS['relations_merged_and_cleaned']+=1
        if previous.get('replay_status') in ('complete','incomplete','failed'):COUNTS['replay_'+previous['replay_status']]+=1

        if previous.get('dual_capture_valid'):COUNTS['dual_valid']+=1
        elif previous.get('replay_status') in ('failed','incomplete'):COUNTS['dual_incomplete']+=1
        if previous.get('finalization',{}).get('state')=='cleaned':COUNTS['intermediates_cleaned']+=1
        COUNTS['resumed_recorded']+=1
        if previous.get('dual_capture_valid'):COUNTS['resumed_valid']+=1
    status('collecting')
    plan=collect()
    if not plan:status('blocked_no_collectable_cases');return
    initialize_relations(plan)
    status('preflight_capture',runnable=len(plan))
    PIPELINE=ParallelReplay(organize,commit_organized,pipeline_error,workers=8,ready_limit=16,on_writer_close=close_writer)
    try:
        # Recover captured evidence from an interrupted import before new tests.
        for p in sorted((OUT/'results').glob('*.json')):
            prior=read(p,{})
            if not prior.get('retry_pending') and prior.get('capture',{}).get('status')=='captured' and prior.get('finalization',{}).get('state')!='cleaned':PIPELINE.submit(p)
        # Maintenance may resume the verified writer while original repair
        # cases still use the cards. This gate never blocks offline consumers.
        if (OUT/'CAPTURE_PAUSE').exists():
            status('maintenance_capture_paused',runnable=len(plan),capture_waits_for_replay=False)
            print('CAPTURE_ADMISSION_PAUSED; offline consumers continue',flush=True)
            while (OUT/'CAPTURE_PAUSE').exists() and resource_ok():time.sleep(1)
            if not resource_ok():return
        # Existing results are durable. Collection resumes while old results normalize.
        # A real whitelist item validates the actual module/test/plugin integration.
        pilot=next((i for i in plan if i['file']=='test/nn/test_dropout.py' and 'Dropout1d' in i['nodeid']),plan[0])
        prior_pilot=read(OUT/'results'/f"{pilot['id']}.json",{})
        probe=prior_pilot if prior_pilot.get('dual_capture_valid') else execute(pilot,'7')
        if not prior_pilot.get('dual_capture_valid'):PIPELINE.join()
        latest=read(OUT/'results'/f"{pilot['id']}.json",{})
        if not probe or not latest.get('dual_capture_valid') or latest.get('replay_status')!='complete':
            status('blocked_preflight',pilot=pilot['nodeid'],result=latest);return
        status('running',runnable=len(plan),regular_workers=len(DEVICES),replay_workers=1,organize_workers=8,ready_limit=16,capture_waits_for_replay=False)
        jobs=queue.Queue()
        for item in plan:
            if not item['isolated'] and item['id']!=pilot['id']:jobs.put(item)
        def worker(device):
            while resource_ok():
                try:item=jobs.get_nowait()
                except queue.Empty:return
                try:execute(item,str(device))
                finally:jobs.task_done()
        with ThreadPoolExecutor(max_workers=len(DEVICES)) as pool:
            futures=[pool.submit(worker,d) for d in DEVICES]
            for f in futures:f.result()
        # Offline CPU work overlaps the following isolated device jobs too.
        for item in plan:
            if not resource_ok():break
            if item['isolated'] and item['id']!=pilot['id']:execute(item,'0,1,2,3,4,5,6,7')
        PIPELINE.join()
        status('stopped_resource_or_request' if STOP.is_set() else 'completed',
               runnable=len(plan),whitelist_count=len(read(WHITE)),
               note='Completion means all collectable cases attempted; test failures, skipped bodies and trace gaps are separate outcomes. See not-runnable.json for unreached whitelist entries.')
    finally:
        PIPELINE.close()


if __name__=='__main__':
    try:main()
    except BaseException as e:
        traceback.print_exc();status('driver_failed',error=repr(e));raise
