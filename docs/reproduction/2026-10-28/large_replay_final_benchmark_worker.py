from pathlib import Path
import os,sqlite3,subprocess,time,json,shutil,collections,sys

R=Path('/home/workspace-ycy/compile-performance');D=R/'trace-fault-2026-10-27';O=R/'pipeline-whitelist-dual-plugins-018-2026-10-27';L=D/'large-db-final-benchmark';PY=R/'cases/instrumented-r9-lld22-exclude-std/venv311/bin/python'

def mark(stage,**extra):
    p=L/'status.json';p.write_text(json.dumps({'stage':stage,'updated_at':time.time(),**extra},indent=2));print('STAGE',stage,extra,flush=True)

if len(sys.argv)>1:
    name=sys.argv[1];timings=collections.defaultdict(lambda:[0,0.,0.]);original=sqlite3.connect
    class Timed(sqlite3.Connection):
        def execute(self,sql,*args,**kwargs):
            if name=='cache4096' and sql=='DELETE FROM paths WHERE NOT EXISTS (SELECT 1 FROM path_support s WHERE s.path_id=paths.id)':sql='DELETE FROM paths WHERE rowid IN (SELECT p.rowid FROM paths p WHERE NOT EXISTS (SELECT 1 FROM path_support s WHERE s.path_id=p.id) ORDER BY p.id)'
            start=time.monotonic()
            try:return super().execute(sql,*args,**kwargs)
            finally:
                elapsed=time.monotonic()-start;v=timings[sql];v[0]+=1;v[1]+=elapsed;v[2]=max(v[2],elapsed)
                if elapsed>2:print('SLOW_SQL',round(elapsed,3),sql[:300],flush=True)
        def executemany(self,sql,*args,**kwargs):
            start=time.monotonic()
            try:return super().executemany(sql,*args,**kwargs)
            finally:
                elapsed=time.monotonic()-start;v=timings['MANY '+sql];v[0]+=1;v[1]+=elapsed;v[2]=max(v[2],elapsed)
    def connection(*args,**kwargs):
        kwargs['factory']=Timed;db=original(*args,**kwargs)
        if name=='cache4096':db.execute('PRAGMA cache_size=-4194304')
        return db
    sqlite3.connect=connection
    from trace_api_chain.pipeline import prepare
    from trace_api_chain.database import refresh
    start=time.monotonic();data=prepare(D/'profile-capture');prepared=time.monotonic()-start;start=time.monotonic()
    result=refresh(L/(name+'.sqlite'),data,roots={'pytorch':str(R/'cases/instrumented-r9-lld22-exclude-std/pytorch'),'torch_npu':str(R/'cases/instrumented-r9-lld22-exclude-std/torch-npu')},slot='diagnostic-large-final-comparison')
    refreshed=time.monotonic()-start
    rows=[{'sql':sql,'count':v[0],'seconds':v[1],'max_seconds':v[2]} for sql,v in timings.items()]
    report={'prepare_seconds':prepared,'refresh_seconds':refreshed,'total_seconds':prepared+refreshed,'result':result,'sql_timings':sorted(rows,key=lambda r:r['seconds'],reverse=True)}
    (L/(name+'.json')).write_text(json.dumps(report,indent=2));print('BENCHMARK_DONE',name,prepared,refreshed,flush=True)
else:
    L.mkdir();mark('copy_equal_frozen_snapshots')
    frozen=D/'large-db-benchmark/default.sqlite'
    shutil.copy2(frozen,L/'default.sqlite');shutil.copy2(frozen,L/'cache4096.sqlite')
    mark('snapshots_ready',database_bytes=frozen.stat().st_size)
    for name in ('default','cache4096'):
        mark(name)
        env=os.environ.copy();env.update(PYTHONPATH=str(D/'installed-final'),PYTHONNOUSERSITE='1',PYTHONSAFEPATH='1')
        with (L/(name+'.log')).open('w') as log:result=subprocess.run([str(PY),'-u',__file__,name],env=env,stdout=log,stderr=subprocess.STDOUT)
        if result.returncode:mark('failed',condition=name,returncode=result.returncode);raise SystemExit(result.returncode)
    mark('comparing')
    a=json.loads((L/'default.json').read_text());b=json.loads((L/'cache4096.json').read_text());assert a['result']==b['result']
    compared={}
    with sqlite3.connect(L/'default.sqlite') as db:
        db.execute('ATTACH DATABASE ? AS other',(str(L/'cache4096.sqlite'),))
        for (table,) in db.execute("SELECT name FROM main.sqlite_master WHERE type='table'").fetchall():
            left=db.execute('SELECT count(*) FROM main.'+table).fetchone()[0];right=db.execute('SELECT count(*) FROM other.'+table).fetchone()[0];assert left==right,(table,left,right)
            cols=[r[1] for r in db.execute('PRAGMA main.table_info('+table+')')]
            where=" WHERE slot_id='diagnostic-large-final-comparison'" if 'slot_id' in cols else " WHERE id='diagnostic-large-final-comparison'" if table=='capture_slots' else ''
            if where or left<100000:
                x='SELECT * FROM main.'+table+where;y='SELECT * FROM other.'+table+where
                assert not db.execute('SELECT 1 FROM ('+x+' EXCEPT '+y+') LIMIT 1').fetchone(),table
                assert not db.execute('SELECT 1 FROM ('+y+' EXCEPT '+x+') LIMIT 1').fetchone(),table
            compared[table]={'rows':left,'comparison':'slot rows both directions' if where else 'all rows both directions' if left<100000 else 'counts; SQL implementation identical'}
    report={'default':a,'cache4096':b,'speedup':a['total_seconds']/b['total_seconds'],'table_comparison':compared,'note':'Same online-backup snapshot, same frozen capture, same code, identical full FK checks. Only history cleanup uses the equivalent ordered covering-index query and the SQLite writer page cache is set to 4096 MiB; all other statements and full foreign key checks remain identical.'}
    (L/'comparison.json').write_text(json.dumps(report,indent=2));mark('complete',speedup=report['speedup'])
