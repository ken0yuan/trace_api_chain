"""Capacity-only admission guard. Never signal tests, delete evidence, or resume unrelated faults."""
from pathlib import Path
import argparse,fcntl,hashlib,json,os,shutil,sqlite3,subprocess,sys,time,uuid

LOW=1024;HIGH=1280

def read(path,default=None):
    try:return json.loads(Path(path).read_text())
    except (OSError,ValueError):return default

def atomic(path,value):
    path=Path(path);tmp=path.with_name(path.name+'.tmp');fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def digest(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def decision(free_gib,controllers,owned,stop_exists,blocked,activity):
    if blocked:return 'blocked_'+blocked
    if len(controllers)>1:return 'blocked_duplicate_controllers'
    if owned:
        if controllers or activity:return 'draining'
        return 'resume' if free_gib>=HIGH else 'waiting_capacity'
    if stop_exists:return 'external_stop_respected'
    if controllers and free_gib<LOW:return 'pause'
    return 'observing'

def marker_matches(path,token):
    p=Path(path)
    return p.is_file() and not p.is_symlink() and read(p)=={'owner':'capacity-guard/1','token':token}

def create_marker(path,token):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:json.dump({'owner':'capacity-guard/1','token':token},f);f.flush();os.fsync(f.fileno())

def release_marker(path,token,archive):
    if not marker_matches(path,token):raise ValueError('owned_stop_marker_changed')
    os.rename(path,archive)
    if not marker_matches(archive,token):
        if not Path(path).exists():os.link(archive,path)
        raise ValueError('stop_marker_changed_during_release')
    if Path(path).exists():raise ValueError('new_external_stop_after_release')

def inventory(root,driver):
    controllers=[];activity=[]
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():continue
        try:
            st=(p/'stat').read_text().rsplit(')',1)[1].split()
            if st[0]=='Z':continue
            args=[os.fsdecode(a) for a in (p/'cmdline').read_bytes().split(b'\0') if a]
            if str(driver) in args:controllers.append(int(p.name));continue
            cmd=' '.join(args)
            if ('-m pytest' in cmd or (str(root) in cmd and (' prepare ' in cmd or '-m api_test_relations append' in cmd))):activity.append(int(p.name))
        except OSError:pass
    return controllers,activity

def faults(root):
    bad=[];pending=[]
    for p in (root/'results').glob('*.json'):
        r=read(p,{})
        if r.get('pipeline_error'):bad.append(r.get('id',p.stem))
        if not r.get('retry_pending') and r.get('capture',{}).get('status')=='captured' and r.get('finalization',{}).get('state')!='cleaned':pending.append(p.stem)
    if read(root/'status.json',{}).get('stage')=='driver_error':bad.append('driver_error')
    return bad,pending

def verify_recipe(recipe,root):
    info=read(root/'process.json',{})
    for key in ['trace_commit','api_commit','runtime']:
        if info.get(key)!=recipe['process'][key]:raise ValueError('verified_version_changed:'+key)
    for name,sha in recipe['sha256'].items():
        if digest(name)!=sha:raise ValueError('verified_file_changed:'+name)
    if digest(recipe['environment_file'])!=recipe['environment_sha256']:raise ValueError('resume_environment_changed')

def verify_stores_readonly(root):
    rows=[read(p) for p in (root/'results').glob('*.json')];rows=[r for r in rows if r and r.get('finalization',{}).get('state')=='cleaned']
    with sqlite3.connect((root/'api-callchains.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        db.execute('PRAGMA query_only=ON');slots={a:(b,json.loads(c)) for a,b,c in db.execute('SELECT id,status,summary FROM capture_slots')}
    with sqlite3.connect((root/'api-test-relations.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        for r in rows:
            f=r['finalization'];slot=slots.get(r['id']);assert slot and slot[0]=='current' and slot[1]['gaps']==f['graph_gaps']==[] and slot[1]['graph_digest']==f['graph_sha256']
            item=db.execute('SELECT module,detail FROM capture_runs WHERE attempt=?',(r['relations_dir'],)).fetchone();assert item and item[0]==r['file'];detail=json.loads(item[1]);required={'raw_mapping_and_owner_match','destination_mapping_and_phases_equal','prior_positive_subset','completed_instances_preserved','foreign_key_check','integrity_check'};assert required<=set(detail['checks']) and not detail.get('boundaries')
            tests=db.execute('SELECT t.capture_state,t.source_attempt FROM tests t JOIN modules m ON m.id=t.module_id WHERE m.module=? AND t.nodeid IN (?,?)',(r['file'],r['nodeid'],'pytorch/'+r['nodeid'])).fetchall();assert tests==[('complete',r['id'])]
    return len(rows)

def child(recipe_path,token,lease):
    recipe=read(recipe_path);root=Path(recipe['root']);verify_recipe(recipe,root)
    if not recipe.get('resume_environment_bound'):raise SystemExit('resume_environment_not_verified')
    if sys.stdin.buffer.read(2)!=b'GO':raise SystemExit('resume_gate_closed')
    controllers,activity=inventory(root,Path(recipe['driver']))
    if controllers or activity or (root/'STOP').exists() or (root/'CAPACITY_MANUAL_HOLD').exists():raise SystemExit('resume_preflight_busy_or_stopped')
    bad,pending=faults(root)
    if bad or pending:raise SystemExit('resume_fault_or_undrained_inputs')
    os.fstat(lease);os.set_inheritable(lease,True)
    atomic(Path(recipe_path).parent/('child-exec-'+token+'.json'),{'pid':os.getpid(),'at':time.time(),'token':token})
    env=read(recipe['environment_file']);os.chdir(root);os.execve(recipe['python'],[recipe['python'],'-u',recipe['driver']],env)

class Guard:
    def __init__(self,recipe_path):
        self.recipe_path=Path(recipe_path);self.base=self.recipe_path.parent;self.recipe=read(recipe_path);self.root=Path(self.recipe['root']);self.state_path=self.base/'state.json';self.state=read(self.state_path,{'phase':'observing','pause':None})

    def record(self,phase,**extra):
        self.state.update(phase=phase,checked_at=time.time(),**extra);atomic(self.state_path,self.state)

    def tick(self):
        root=self.root;recipe=self.recipe;free=shutil.disk_usage(root).free/1024**3;controllers,activity=inventory(root,Path(recipe['driver']));bad,pending=faults(root);pause=self.state.get('pause');blocked=None
        try:verify_recipe(recipe,root)
        except Exception as e:blocked='version_mismatch';self.state['version_error']=str(e)
        if bad:blocked='pipeline_fault'
        if (root/'CAPACITY_MANUAL_HOLD').exists():blocked='manual_hold'
        if self.state.get('phase') in ('starting','blocked_resume_uncertain'):
            if controllers and self.state.get('new_pid') in controllers:
                self.state['pause']=None;self.record('observing',reconciled_started_pid=self.state['new_pid']);return
            self.record('blocked_resume_uncertain',free_disk_gib=free);return
        if pause and not marker_matches(root/'STOP',pause['token']):blocked='stop_ownership_changed'
        action=decision(free,controllers,bool(pause),(root/'STOP').exists(),blocked,activity or (pending if pause else []))
        common={'free_disk_gib':free,'controllers':controllers,'activity_pids':activity,'pipeline_faults':bad,'pending_preparations':len(pending),'low_threshold_gib':LOW,'resume_threshold_gib':HIGH,'signals_sent':False,'capture_data_deleted':False}
        if action=='pause':
            token=uuid.uuid4().hex;pause={'token':token,'at':time.time(),'controller':controllers[0],'process':read(root/'process.json')};self.state['pause']=pause;self.record('pause_intent',**common)
            try:create_marker(root/'STOP',token)
            except FileExistsError:self.state['pause']=None;self.record('external_stop_respected',**common);return
            self.record('draining',**common);return
        if action=='resume':
            if not recipe.get('resume_environment_bound'):self.record('blocked_resume_environment_not_verified',**common);return
            if pending:self.record('draining_pending_results',**common);return
            verified=verify_stores_readonly(root);verify_recipe(recipe,root)
            lease=os.open(self.base/'driver.lease',os.O_RDWR|os.O_CREAT,0o600)
            try:
                fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
                if any(inventory(root,Path(recipe['driver']))):raise ValueError('activity_appeared_before_resume')
                token=pause['token'];archive=self.base/('released-stop-'+token+'.json');release_marker(root/'STOP',token,archive)
                env=read(recipe['environment_file']);previous=read(root/'process.json');self.record('starting',verified_cleaned_results=verified,**common)
                with (root/'driver.log').open('ab',buffering=0) as log:
                    p=subprocess.Popen([recipe['python'],'-u',str(Path(__file__).resolve()),'--child',str(self.recipe_path),token,str(lease)],cwd=root,env=env,stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,pass_fds=(lease,))
                self.state['new_pid']=p.pid;self.record('starting',**common)
                info={**previous,'pid':p.pid,'started_at':time.time(),'version_initial_started_at':previous.get('version_initial_started_at',previous.get('started_at')),'capacity_resume_token':token,'capacity_resume_recipe':str(self.recipe_path)};atomic(root/'process.json',info)
                receipt={'at':time.time(),'pid':p.pid,'previous_pid':previous.get('pid'),'trace_commit':info['trace_commit'],'api_commit':info['api_commit'],'runtime':info['runtime'],'driver_sha256':recipe['sha256'][recipe['driver']],'source_deployment':recipe['source_deployment'],'reason':'owned_capacity_pause_drained_and_space_recovered','verified_cleaned_results':verified};atomic(root/'capacity-resume-deployment.json',receipt)
                p.stdin.write(b'GO');p.stdin.close()
                for _ in range(50):
                    if p.pid in inventory(root,Path(recipe['driver']))[0]:self.state['pause']=None;self.record('observing',last_resume=receipt,**common);return
                    if p.poll() is not None:break
                    time.sleep(.1)
                self.record('blocked_resume_uncertain',**common)
            finally:os.close(lease)
            return
        self.record(action,**common)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--recipe');parser.add_argument('--child',nargs=3);args=parser.parse_args()
    if args.child:child(args.child[0],args.child[1],int(args.child[2]));return
    recipe=Path(args.recipe);lock=recipe.parent/'guard.lock'
    with lock.open('a') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);guard=Guard(recipe)
        while True:
            try:guard.tick()
            except Exception as e:guard.record('blocked_error',error=type(e).__name__+':'+str(e))
            time.sleep(60)

if __name__=='__main__':main()
