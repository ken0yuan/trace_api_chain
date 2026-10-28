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
REPLAY = queue.Queue(maxsize=8)
START = time.time()


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


def env(devices='7'):
    e = os.environ.copy()
    for k in ('LD_PRELOAD', 'LD_AUDIT', 'PYTEST_PLUGINS', 'PYTEST_ADDOPTS',
              'TRACE_API_CHAIN_INTERNAL', 'SCENARIO_API_FILTER', 'CI',
              'PYTORCH_TESTING_DEVICE_FOR_CUSTOM', 'DISABLED_TESTS_FILE'):
        e.pop(k, None)
    site = V/'venv311/lib/python3.11/site-packages'
    e.update(PYTHONPATH=':'.join(map(str, [OUT/'plugin-runtime', R/'plugin-runtime', site, OUT, SOURCE,
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
           '-p', 'whitelist_trace_collect', '--collect-only', '-q', file]
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


def execute(item, devices):
    if not resource_ok():return
    ident=item['id']; cap=OUT/'captures'/ident; log=OUT/'logs'/f'{ident}.log'
    result_path=OUT/'results'/f'{ident}.json'
    if result_path.exists():return read(result_path)
    cmd=[PY,'-m','pytest','-c',SOURCE/'pytest.ini','--rootdir',SOURCE,
         '-p','whitelist_trace_collect','-p','api_test_relations.pytest_plugin',
         '-p','trace_api_chain.pytest_plugin', item['nodeid'],'-q','--tb=short',
         '--callchain-out',cap,'--callchain-mode','full','--callchain-native-storage','paths',
         '--callchain-api-catalog',LEDGER,'--callchain-max-events','10000000',
         '--callchain-max-paths','4000000','--callchain-max-references','1000000',
         '--callchain-timeout',str(item.get('capture_timeout_seconds',1800)),'--junitxml',OUT/'junit'/f'{ident}.xml',
         '--api-relations-out',OUT/'relations'/ident,'--api-relations-repo',SOURCE,
         '--api-relations-catalog',LEDGER,'--api-relations-import','torch_npu',
         '--api-relations-label','ascend-npu-dual-plugins']
    run_result=run(cmd,log,env(devices),item.get('capture_timeout_seconds',1800)+120)
    capture=read(cap/'capture-result.json',{})
    receipt=read(cap/'pytest-receipt.json',{})
    relations=read(OUT/'relations'/ident/'progress.json',{})
    inner_timeout=capture.get('error_type')=='TimeoutExpired'
    run_result['timeout']=run_result['timeout'] or inner_timeout
    run_result['timeout_layer']='capture_worker' if inner_timeout else 'outer_controller' if run_result['timeout'] else None
    result={**item,**run_result,'execution_host':'179.163.3.2','hardware':'910B2','devices':devices,'capture':capture,
            'test_reports':receipt.get('reports',[]),'test_body_started':receipt.get('started'),
            'test_body_ended':receipt.get('ended'),'capture_dir':str(cap),
            'log':str(log),'relations':relations,'relations_dir':str(OUT/'relations'/ident),
            'dual_capture_valid':False,
            'replay_status':'pending' if capture.get('status')=='captured' else 'not_eligible'}
    write(result_path,result)
    with LOCK:
        COUNTS['attempted']+=1;COUNTS['capture_'+capture.get('status','missing')]+=1
        COUNTS['relations_'+relations.get('state','missing')]+=1
        reports=receipt.get('reports',[])
        outcome='failed' if any(x['outcome']=='failed' for x in reports) else 'skipped' if any(x['outcome']=='skipped' for x in reports) else 'passed' if any(x.get('when')=='call' and x['outcome']=='passed' for x in reports) else 'no_test_result'
        COUNTS['test_'+outcome]+=1
    status('running',last_nodeid=item['nodeid'])
    print('CAPTURE',ident,capture.get('status'),outcome,round(run_result['elapsed_seconds'],2),flush=True)
    REPLAY.put(result_path)
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


def replayer():
    while True:
        path=REPLAY.get()
        try:
            if path is None:return
            result=read(path);cap=Path(result['capture_dir']);cache=cap/'symbol-cache'
            if result['relations'].get('state')=='capture_finished' and not (result.get('relations_merge',{}).get('returncode')==0 and result.get('relations_raw_removed')):
                command=[PY,'-m','api_test_relations','append','--out',result['relations_dir'],
                         '--database',OUT/'api-test-relations.sqlite','--module',result['file']]
                merged=run(command,OUT/'relation-merge-logs'/f"{result['id']}.log",env(''),600)
                result['relations_merge']=merged
                result['relations_raw_removed']=not Path(result['relations_dir']).exists()
                result['relations_database']=str(OUT/'api-test-relations.sqlite')
                with LOCK:COUNTS['relations_merged_and_cleaned' if merged['returncode']==0 and result['relations_raw_removed'] else 'relations_merge_failed']+=1
                if merged['returncode'] or not result['relations_raw_removed']:
                    STOP.set()  # Preserve failed evidence and avoid repeated storage errors.
                write(path,result)
            if result['capture'].get('status')!='captured':
                with LOCK:COUNTS['dual_incomplete']+=1
                write(path,result);status('running',last_processed=result['nodeid']);continue
            if not cache.exists():cache.symlink_to(OUT/'shared-symbol-cache',target_is_directory=True)
            cmd=[PY,'-c','from trace_api_chain.cli import main;raise SystemExit(main())',
                 'replay',cap,'--db',OUT/'api-callchains.sqlite','--db-cache-mib','4096','--slot',result['id'],
                 '--source-root','pytorch='+str(SOURCE),
                 '--source-root','torch_npu='+str(V/'torch-npu')]
            replay=run(cmd,OUT/'replay-logs'/f"{result['id']}.log",env(''),3600)
            graph_valid,result['graph_validation']=verified_replay_report(cap,result['id'],OUT/'api-callchains.sqlite')
            result['replay']=replay;result['replay_status']=('complete' if graph_valid else 'incomplete') if replay['returncode']==0 else 'failed'
            result['dual_capture_valid']=replay['returncode']==0 and graph_valid and result.get('test_body_ended',False) and result['relations'].get('gaps')==0 and result.get('relations_merge',{}).get('returncode')==0 and result.get('relations_raw_removed',False)
            write(path,result)
            with LOCK:COUNTS['replay_'+result['replay_status']]+=1
            with LOCK:COUNTS['dual_valid' if result['dual_capture_valid'] else 'dual_incomplete']+=1
            status('running',last_replay=result['nodeid'])
            print('REPLAY',result['id'],result['replay_status'],round(replay['elapsed_seconds'],2),flush=True)
        except Exception:
            traceback.print_exc();STOP.set()
        finally:REPLAY.task_done()


def main():
    for n in ('collection','logs','captures','relations','results','junit','replay-logs','relation-merge-logs','shared-symbol-cache'):(OUT/n).mkdir(exist_ok=True)
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
        if previous.get('relations_merge',{}).get('returncode')==0 and previous.get('relations_raw_removed'):COUNTS['relations_merged_and_cleaned']+=1
        if previous.get('replay_status') in ('complete','incomplete','failed'):COUNTS['replay_'+previous['replay_status']]+=1
        COUNTS['dual_valid' if previous.get('dual_capture_valid') else 'dual_incomplete']+=1
        COUNTS['resumed_recorded']+=1
        if previous.get('dual_capture_valid'):COUNTS['resumed_valid']+=1
    status('collecting')
    plan=collect()
    if not plan:status('blocked_no_collectable_cases');return
    initialize_relations(plan)
    status('preflight_capture',runnable=len(plan))
    thread=threading.Thread(target=replayer,name='sqlite-replay');thread.start()
    try:
        # Recover captured evidence from an interrupted import before new tests.
        for p in sorted((OUT/'results').glob('*.json')):
            prior=read(p,{})
            if prior.get('capture',{}).get('status')=='captured' and prior.get('replay_status')!='complete':REPLAY.put(p)
        REPLAY.join()
        # A real whitelist item validates the actual module/test/plugin integration.
        pilot=next((i for i in plan if i['file']=='test/nn/test_dropout.py' and 'Dropout1d' in i['nodeid']),plan[0])
        probe=execute(pilot,'7')
        REPLAY.join()
        latest=read(OUT/'results'/f"{pilot['id']}.json",{})
        if not probe or not latest.get('dual_capture_valid') or latest.get('replay_status')!='complete':
            status('blocked_preflight',pilot=pilot['nodeid'],result=latest);return
        status('running',runnable=len(plan),regular_workers=len(DEVICES),replay_workers=1)
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
        REPLAY.join()
        for item in plan:
            if not resource_ok():break
            if item['isolated'] and item['id']!=pilot['id']:execute(item,'0,1,2,3,4,5,6,7');REPLAY.join()
        REPLAY.join()
        status('stopped_resource_or_request' if STOP.is_set() else 'completed',
               runnable=len(plan),whitelist_count=len(read(WHITE)),
               note='Completion means all collectable cases attempted; test failures, skipped bodies and trace gaps are separate outcomes. See not-runnable.json for unreached whitelist entries.')
    finally:
        REPLAY.put(None);thread.join()


if __name__=='__main__':
    try:main()
    except BaseException as e:
        traceback.print_exc();status('driver_failed',error=repr(e));raise
