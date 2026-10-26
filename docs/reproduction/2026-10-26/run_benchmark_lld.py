import argparse
import datetime
import fcntl
import json
import os
import pathlib
import shutil
import subprocess
import time
import traceback

ROOT = pathlib.Path('/home/workspace-ycy/compile-performance')
JOBS = 128

def save(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2))
    tmp.replace(path)

def clone_env(source, target):
    shutil.copytree(source, target, symlinks=True)
    for path in (target / 'bin').iterdir():
        if path.is_symlink():
            dest = os.readlink(path)
            if dest.startswith(str(source)):
                path.unlink()
                path.symlink_to(dest.replace(str(source), str(target), 1))
        elif path.is_file():
            data = path.read_bytes()
            if b'\0' not in data[:4096]:
                new = data.replace(str(source).encode(), str(target).encode())
                if new != data:
                    path.write_bytes(new)
    cfg = target / 'pyvenv.cfg'
    cfg.write_text(cfg.read_text().replace(str(source), str(target)))

def materialize_torch(case, env):
    site = case / 'venv311/lib/python3.11/site-packages'
    marker = case / 'torch-materialized.json'
    if marker.exists():
        return
    installs = list((case/'pytorch/build/install').glob('*/torch'))
    if len(installs) != 1:
        raise RuntimeError('Expected one PyTorch native installation tree')
    install = installs[0].parent
    for package in ('torch', 'torchgen', 'functorch'):
        shutil.copytree(case/'pytorch'/package, site/package, dirs_exist_ok=True, symlinks=True, ignore=shutil.ignore_patterns('__pycache__'))
        if (install/package).exists():
            shutil.copytree(install/package, site/package, dirs_exist_ok=True, symlinks=True)
    pth=site/'_editable_skbc_torch.pth'
    pth.rename(site/'_editable_skbc_torch.pth.saved')
    probe=subprocess.check_output([str(case/'venv311/bin/python'),'-c',"import torch,json; print(json.dumps({'version':torch.__version__,'file':torch.__file__,'sum':float(torch.ones(2).sum())}))"],env=env,cwd=ROOT,text=True)
    save(marker,{'reason':'Materialize editable Python sources plus native install artifacts into the isolated case venv so downstream torch-npu sees a normal include/lib/share layout and does not import PyTorch tools as its own tools namespace. No recompilation; excluded from build timing.','native_install_tree':str(install),'probe':json.loads(probe)})

WRAPPER = '''#!/usr/bin/python3
import os,sys,time,json,fcntl,subprocess,pathlib
name=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
extra=os.environ.get('BENCH_EXTRA_FLAGS','').split()
if name in ('gcc','g++','cc','c++'):
 real='/usr/bin/'+('g++' if name in ('g++','c++') else 'gcc')
 if os.environ.get('BENCH_LINKER')=='lld':args=args+['-fuse-ld=lld']
 compile_call='-c' in args or any(x.endswith(('.c','.cc','.cpp','.cxx','.C')) for x in args)
 if compile_call and os.getcwd().endswith('/third_party/dvm/dvm'):
  args=args+['-Wno-error=mismatched-new-delete']
 if compile_call:args=args+extra
 if compile_call:
  record={'compiler':real,'args':args,'cwd':os.getcwd(),'time':time.time(),'pid':os.getpid()}
  with open(os.environ['BENCH_AUDIT'],'a') as f:
   fcntl.flock(f,fcntl.LOCK_EX);f.write(json.dumps(record)+'\\n');f.flush()
 os.execv(real,[real]+args)
else:
 real=os.environ['BENCH_REAL_NINJA']
 started=time.monotonic()
 code=subprocess.call([real]+args)
 record={'args':args,'cwd':os.getcwd(),'elapsed_seconds':time.monotonic()-started,'returncode':code}
 with open(os.environ['BENCH_NATIVE_TIMES'],'a') as f:
  fcntl.flock(f,fcntl.LOCK_EX);f.write(json.dumps(record)+'\\n');f.flush()
 sys.exit(code)
'''

def run_stage(case, variant, component, env):
    source = case / component
    python = case / 'venv311/bin/python'
    stem = ROOT / 'logs' / (case.name + '-' + component)
    env = dict(env, BENCH_AUDIT=str(stem)+'.compiler.jsonl',
               BENCH_NATIVE_TIMES=str(stem)+'.native-times.jsonl')
    if component == 'pytorch':
        command = [str(python), '-m', 'pip', 'install', '-e', '.', '-v', '--no-build-isolation', '--no-deps', '--no-index']
    else:
        # torch-npu auto-detects Ninja; its TorchAir install helper invokes make.
        # Leaving the global generator unset lets that nested project use Makefiles.
        env.pop('CMAKE_GENERATOR', None)
        command = ['bash', 'ci/build.sh', '--python=3.11']
    result_path = ROOT / 'results' / (case.name + '-' + component + '.json')
    result = {'variant':variant,'component':component,'case':str(case),'command':command,
              'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=source,text=True).strip(),
              'flags':env['BENCH_EXTRA_FLAGS'],'max_jobs':JOBS,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'linker':env.get('BENCH_LINKER','bfd'),
              'lld_layout':env.get('BENCH_LLD_LAYOUT'),
              'lld_version':env.get('BENCH_LLD_VERSION'),
              'dvm_warning_policy':'Only DVM compile calls use -Wno-error=mismatched-new-delete in both variants; diagnostics remain enabled and code generation is unchanged.' if component=='torch-npu' else None,
              'status':'running','load_before':os.getloadavg(),'log':str(stem)+'.log',
              'timing_scope':'clean configure + code generation + compile + link + package/install; excludes source download, dependency setup, source/env copies',
              'environment':{k:env.get(k) for k in ('CC','CXX','CFLAGS','CXXFLAGS','MAX_JOBS','CMAKE_BUILD_PARALLEL_LEVEL','CMAKE_GENERATOR','CMAKE_BUILD_TYPE','PYTHONPATH','LD_LIBRARY_PATH','CCACHE_DISABLE')}}
    save(result_path,result)
    start = time.monotonic()
    with open(str(stem)+'.log','wb') as log:
        process = subprocess.Popen(command,cwd=source,env=env,stdout=log,stderr=subprocess.STDOUT)
        result['pid']=process.pid
        save(result_path,result)
        _, wait_status, usage = os.wait4(process.pid, 0)
        code = os.waitstatus_to_exitcode(wait_status)
        process.returncode = code
        save(pathlib.Path(str(stem)+'.resources.json'), {'user_cpu_seconds':usage.ru_utime,'system_cpu_seconds':usage.ru_stime,'max_rss_kib':usage.ru_maxrss,'minor_faults':usage.ru_minflt,'major_faults':usage.ru_majflt,'voluntary_context_switches':usage.ru_nvcsw,'involuntary_context_switches':usage.ru_nivcsw})
    result.update(elapsed_seconds=time.monotonic()-start,returncode=code,load_after=os.getloadavg(),finished_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),status='built' if code==0 else 'failed')
    audit = pathlib.Path(env['BENCH_AUDIT'])
    entries = [json.loads(s) for s in audit.read_text().splitlines()] if audit.exists() else []
    instrumented = sum('-finstrument-functions' in e['args'] and '-g1' in e['args'] for e in entries)
    result['compiler_invocations'] = len(entries)
    result['invocations_with_both_flags'] = instrumented
    result['flags_verified'] = bool(entries) and (instrumented==len(entries) if variant=='instrumented' else instrumented==0)
    save(result_path,result)
    if code:
        raise RuntimeError(f'{case.name}/{component} failed: {code}; see {stem}.log')
    if not result['flags_verified']:
        raise RuntimeError('Compiler flags audit failed')
    # Installation and smoke checks below are outside the recorded build time.
    if component == 'torch-npu':
        wheels = list((source/'dist').glob('torch_npu*.whl'))
        if len(wheels)!=1:
            raise RuntimeError('Expected one newly built torch_npu wheel')
        with open(str(stem)+'.install.log','wb') as log:
            subprocess.run([str(python),'-m','pip','install','--no-deps','--no-index',str(wheels[0])],env=env,cwd=ROOT,stdout=log,stderr=subprocess.STDOUT,check=True)
    probe = "import torch,json;print(json.dumps({'torch':torch.__version__,'file':torch.__file__,'sum':float(torch.ones(2).sum())}))"
    if component == 'torch-npu':
        probe = "import torch,torch_npu,json;print(json.dumps({'torch':torch.__version__,'torch_npu':torch_npu.__version__,'file':torch_npu.__file__}))"
    checked=subprocess.run([str(python),'-c',probe],env=env,cwd=ROOT,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    pathlib.Path(str(stem)+'.smoke.log').write_text(checked.stdout)
    result.update(smoke_returncode=checked.returncode,status='success' if checked.returncode==0 else 'smoke_failed')
    save(result_path,result)
    if checked.returncode:
        raise RuntimeError('Built package failed smoke check: '+checked.stdout[-2000:])
    return result

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--variants',nargs='+',default=['baseline','instrumented'])
    parser.add_argument('--attempt',default='r1')
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--linker',choices=['bfd','lld'],default='bfd')
    parser.add_argument('--lld-layout',choices=['script','native'],default='native')
    parser.add_argument('--lld-path',default='/home/workspace-ycy/compile-performance/toolchains/llvm22/bin/ld.lld')
    args=parser.parse_args()
    lock=open(ROOT/'results/benchmark.lock','w')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    status=ROOT/'results/benchmark-status.json'
    save(status,{'status':'waiting_for_submodules','pid':os.getpid()})
    while not (ROOT/'results/submodules-status.json').exists():
        time.sleep(10)
    modules=json.loads((ROOT/'results/submodules-status.json').read_text())
    if any(modules.values()):
        raise RuntimeError('Submodule initialization failed: '+str(modules))
    if not (ROOT/'environment/build-tools.json').exists():
        raise RuntimeError('Build tools not ready')
    for name in ('pytorch','torch-npu'):
        text=subprocess.check_output(['git','submodule','status','--recursive'],cwd=ROOT/'sources'/name,text=True)
        (ROOT/'environment'/('submodules-'+name+'.txt')).write_text(text)
    toolbin=ROOT/('tool-wrappers-'+args.attempt)
    toolbin.mkdir(exist_ok=True)
    if args.linker=='lld' and not (toolbin/'ld.lld').exists():
        (toolbin/'ld.lld').symlink_to(args.lld_path)
    for name in ('gcc','g++','cc','c++','ninja'):
        p=toolbin/name;p.write_text(WRAPPER);p.chmod(0o755)
    results=[]
    for variant in args.variants:
        case=ROOT/'cases'/(variant+'-'+args.attempt)
        exists=case.exists()
        if exists and not args.resume:
            raise RuntimeError('Case already exists; use --resume after reviewing its status')
        case.mkdir(parents=True,exist_ok=True)
        save(status,{'status':'preparing','variant':variant,'pid':os.getpid()})
        if not exists:
            clone_env(ROOT/'venv311',case/'venv311')
            for component in ('pytorch','torch-npu'):
                subprocess.run(['cp','-a','--reflink=auto',str(ROOT/'sources'/component),str(case/component)],check=True)
        env=os.environ.copy()
        for key in ('CFLAGS','CXXFLAGS','CPPFLAGS','LDFLAGS','CMAKE_ARGS','DEBUG','REL_WITH_DEB_INFO','CMAKE_C_COMPILER_LAUNCHER','CMAKE_CXX_COMPILER_LAUNCHER'):
            env.pop(key,None)
        flags='-finstrument-functions -g1' if variant=='instrumented' else ''
        env.update(PATH=str(toolbin)+':'+str(case/'venv311/bin')+':'+str(ROOT/'build-tools/bin')+':'+env['PATH'],
                   PYTHONPATH=str(ROOT/'build-tools')+':'+env.get('PYTHONPATH',''),
                   VIRTUAL_ENV=str(case/'venv311'),CC=str(toolbin/'gcc'),CXX=str(toolbin/'g++'),
                   CFLAGS=flags,CXXFLAGS=flags,BENCH_EXTRA_FLAGS=flags,
                   BENCH_REAL_NINJA=str(case/'venv311/bin/ninja'),MAX_JOBS=str(JOBS),CMAKE_BUILD_PARALLEL_LEVEL=str(JOBS),
                   CMAKE_GENERATOR='Ninja',CMAKE_BUILD_TYPE='Release',CMAKE_EXPORT_COMPILE_COMMANDS='ON',BUILD_IGNORE_SVE_UNAVAILABLE='1',
                   CCACHE_DISABLE='1',SCCACHE_DISABLE='1',PIP_DISABLE_PIP_VERSION_CHECK='1',
                   TORCH_DEVICE_BACKEND_AUTOLOAD='0',GIT_TERMINAL_PROMPT='0')
        if args.linker=='lld':
            # Use one verified isolated linker for both clean-build variants.
            kind='DEFAULT' if args.lld_layout=='script' else 'LLD'
            env.update(BENCH_LINKER='lld',BENCH_LLD_LAYOUT=args.lld_layout,
                       BENCH_LLD_VERSION=subprocess.check_output([args.lld_path,'--version'],text=True).strip(),
                       LDFLAGS='-fuse-ld=lld -Wl,--threads=8',
                       CMAKE_ARGS='-DCMAKE_LINKER_TYPE='+kind+' -DCMAKE_LINKER='+args.lld_path)
        for component in ('pytorch','torch-npu'):
            previous=ROOT/'results'/(case.name+'-'+component+'.json')
            if previous.exists():
                old=json.loads(previous.read_text())
                if old['status']=='success':
                    results.append(old)
                    continue
                if (case/component/'build').exists():
                    raise RuntimeError('Refusing to time an incremental retry; preserve failed build and use a fresh source directory')
                stamp=datetime.datetime.now().strftime('%H%M%S')
                for p in (ROOT/'logs').glob(case.name+'-'+component+'.*'):
                    p.rename(p.with_name(p.name+'.failed-'+stamp))
                previous.rename(previous.with_name(previous.name+'.failed-'+stamp))
            if component=='torch-npu':
                materialize_torch(case, env)
            save(status,{'status':'running','variant':variant,'component':component,'pid':os.getpid()})
            results.append(run_stage(case,variant,component,env))
    save(ROOT/'results/build-results.json',results)
    comparisons=[]
    for component in ('pytorch','torch-npu'):
        by={v['variant']:v for v in results if v['component']==component}
        if 'baseline' in by and 'instrumented' in by:
            b=by['baseline']['elapsed_seconds'];i=by['instrumented']['elapsed_seconds']
            comparisons.append({'component':component,'baseline_seconds':b,'instrumented_seconds':i,'difference_seconds':i-b,'overhead_percent':(i/b-1)*100,'ratio':i/b})
    save(ROOT/'results/comparison.json',comparisons)
    save(status,{'status':'complete','comparisons':comparisons})

if __name__=='__main__':
    try:
        main()
    except BaseException as exc:
        save(ROOT/'results/benchmark-status.json',{'status':'failed','error':str(exc),'traceback':traceback.format_exc()})
        raise
