from pathlib import Path
import os,json,subprocess,shutil,time,hashlib

R=Path('/home/workspace-ycy/compile-performance');D=R/'trace-fault-2026-10-27';O=R/'pipeline-whitelist-dual-plugins-018-2026-10-27';V=R/'cases/instrumented-r9-lld22-exclude-std';site=V/'venv311/lib/python3.11/site-packages';PY=V/'venv311/bin/python'
tests=json.loads((D/'final-cache4096-regressions.json').read_text());assert tests['returncode']==0
comparison=json.loads((D/'large-db-final-benchmark/comparison.json').read_text());assert comparison['speedup']>1.5
wheel=next((D/'dist-final-cache4096').glob('*.whl'));sha=hashlib.sha256(wheel.read_bytes()).hexdigest();assert sha==tests['wheel_sha256']=='6bb3f54ac6af618ab1d133f546a5f060cb195ee8229c6f4d84e5c6104c19f70b'
pid=json.loads((O/'process.json').read_text())['pid'];assert (Path('/proc')/str(pid)/'cmdline').exists()
# The old replay has already computed its policy fingerprint before opening
# this transaction. New processes will pick up the new implementation.
journal=O/'api-callchains.sqlite-journal';assert journal.exists() and journal.stat().st_size>0,'Wait for the active replay transaction before atomic module updates'
before=D/'deployment-before-large-replay-fix.json';shutil.copy2(D/'deployment.json',before)
with (D/'large-replay-deploy-wheel.log').open('w') as log:
    subprocess.run([str(PY),'-m','pip','install','--force-reinstall','--no-deps','--no-compile','--no-index',str(wheel)],stdout=log,stderr=subprocess.STDOUT,check=True)
for root in (O/'plugin-runtime',R/'plugin-runtime'):
    package=root/'trace_api_chain'
    for source in (site/'trace_api_chain').iterdir():
        if not source.is_file() or source.suffix not in ('.py','.c','.h'):continue
        if source.name in ('database.py','cli.py'):continue
        assert source.read_bytes()==(package/source.name).read_bytes(),('Unexpected capture implementation change',source.name)
    for name in ('database.py','cli.py'):
        target=package/name;temporary=target.with_suffix('.deployment-tmp');temporary.write_bytes((site/'trace_api_chain'/name).read_bytes());temporary.replace(target)
    for source in (site/'trace_api_chain-0.3.1.dist-info').iterdir():
        if source.is_file():shutil.copy2(source,root/'trace_api_chain-0.3.1.dist-info'/source.name)
driver=O/'whitelist_trace_driver.py';text=driver.read_text();needle="'replay',cap,'--db',OUT/'api-callchains.sqlite','--slot',result['id'],"
assert needle in text
text=text.replace(needle,"'replay',cap,'--db',OUT/'api-callchains.sqlite','--db-cache-mib','4096','--slot',result['id'],");compile(text,str(driver),'exec');driver.write_text(text)
manifest=O/'manifest.json';data=json.loads(manifest.read_text());data.update(trace_wheel_sha256=sha,sqlite_writer_cache_mib=4096,replay_optimization_validation=str(D/'large-db-final-benchmark/comparison.json'))
for name in ('database.py','cli.py'):data['plugin_snapshots']['trace_api_chain'][name]=hashlib.sha256((O/'plugin-runtime/trace_api_chain'/name).read_bytes()).hexdigest()
manifest.write_text(json.dumps(data,indent=2))
p=D/'deployment.json';data=json.loads(p.read_text());data.pop('wheel_only_change',None);data.update(trace_wheel_sha256=sha,wheel_path=str(wheel),sqlite_writer_cache_mib=4096,replay_optimization_deployed_at=time.time(),capture_code_unchanged=True,old_replay_allowed_to_finish=True,replay_optimization_validation=str(D/'large-db-final-benchmark/comparison.json'));p.write_text(json.dumps(data,indent=2))
env=os.environ.copy();env.update(PYTHONPATH=str(O/'plugin-runtime'),PYTHONNOUSERSITE='1')
program='import inspect,trace_api_chain,importlib.metadata as m;from trace_api_chain.database import refresh;assert trace_api_chain.__version__==m.version("trace-api-chain")=="0.3.1";assert inspect.signature(refresh).parameters["cache_mib"].default==4096;print("VERSION_AND_WRITER_BUDGET_OK")'
q=subprocess.run([str(PY),'-c',program],env=env,text=True,capture_output=True,check=True)
print('LARGE_REPLAY_OPTIMIZATION_DEPLOYED',sha,q.stdout.strip(),flush=True)
