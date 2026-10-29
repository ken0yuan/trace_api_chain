"""Inactive by default; --callchain-out performs one instrumented pytest run."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from .native import build_hook,save

INTERNAL='TRACE_API_CHAIN_INTERNAL'


def pytest_addoption(parser):
    group=parser.getgroup('trace-api-chain')
    group.addoption('--callchain-out',help='New directory for raw capture only; SQL replay and HTML run separately')
    group.addoption('--callchain-mode',choices=('full','objects'),default='full',help='full: Python + compiler hooks; objects: Python observations')
    group.addoption('--callchain-timeout',type=int,default=3600)
    group.addoption('--callchain-max-events',type=int,default=262144,help='events: records per segment; paths: context/path supports; overflow fails capture')
    group.addoption('--callchain-max-references',type=int,default=100000)
    group.addoption('--callchain-namespaces',default='torch,torch_npu',help='Loaded public module namespaces for the runtime API catalog')
    group.addoption('--callchain-api-catalog',help='Shared documentation API ledger; every test uses all resolvable Python entry identities in this ledger')
    group.addoption('--callchain-native-storage',choices=('events','paths'),default='events',help='paths: count repeated native stack paths while preserving window-specific branches')
    group.addoption('--callchain-max-paths',type=int,default=262144)
    group.addoption('--callchain-reference-storage',choices=('full','delta'),default='delta',help='Lossless shared adjacency states; delta reuses unchanged edges without removing observations')
    group.addoption('--callchain-reference-depth',choices=('full','2'),default='full',help='Experimental: roots at depth zero, retain objects through two reference edges; actual calls remain unrestricted')
    group.addoption('--callchain-scalar-container-summary',action='store_true',help='Experimental: summarize exact builtin containers with more than 100 homogeneous primitive elements; retain all actual calls')


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    target=config.getoption('--callchain-out')
    if not target or os.environ.get(INTERNAL)=='1' or config.option.collectonly:return None
    if len(config.args)!=1 or '::' not in config.args[0]:
        raise pytest.UsageError('--callchain-out requires exactly one explicit pytest nodeid')
    if not 0<config.getoption('--callchain-max-events')<=10000000:
        raise pytest.UsageError('--callchain-max-events must be 1..10000000')
    if not 0<config.getoption('--callchain-max-paths')<=10000000:
        raise pytest.UsageError('--callchain-max-paths must be 1..10000000')
    if config.getoption('--callchain-max-references')<=0 or config.getoption('--callchain-timeout')<=0:
        raise pytest.UsageError('capture budgets and timeout must be positive')
    if config.getoption('--callchain-scalar-container-summary') and config.getoption('--callchain-reference-depth')!='full':
        raise pytest.UsageError('compare scalar summary and depth2 independently; do not combine')
    output=Path(target).resolve()
    if output.exists():raise pytest.UsageError('Evidence directory already exists: '+str(output))
    output.mkdir(parents=True)
    env=dict(os.environ);env[INTERNAL]='1'
    if config.getoption('--api-relations-out',default=None):
        env['TRACE_API_CHAIN_OBSERVER_RUNTIME']=str(output/'observer-runtime')
    env['TRACE_API_CHAIN_MAX_EVENTS']=str(config.getoption('--callchain-max-events'))
    env['TRACE_API_CHAIN_MAX_PATHS']=str(config.getoption('--callchain-max-paths'))
    mode=config.getoption('--callchain-mode')
    started=time.monotonic()
    try:
        if mode=='full':
            if not sys.platform.startswith('linux'):raise RuntimeError('compiler capture requires Linux/glibc')
            hook=build_hook(output)
            env['LD_PRELOAD']=str(hook)+((':'+env['LD_PRELOAD']) if env.get('LD_PRELOAD') else '')
        command=[sys.executable,'-m','pytest','-p','trace_api_chain.pytest_plugin',*config.invocation_params.args]
        print('trace-api-chain: capturing selected test once; output='+str(output),flush=True)
        process=subprocess.run(command,cwd=config.invocation_params.dir,env=env,timeout=config.getoption('--callchain-timeout'))
        receipt_path=output/'pytest-receipt.json'
        if not receipt_path.exists():
            raise RuntimeError('capture_worker_exited_before_receipt: exit_code='+str(process.returncode)+'; inspect pytest output for startup/collection failure')
        receipt=json.loads(receipt_path.read_text())
        if not receipt.get('started') or not receipt.get('ended'):raise RuntimeError('selected_test_body_not_completed')
        bounded=config.getoption('--callchain-reference-depth')=='2'
        summarized=config.getoption('--callchain-scalar-container-summary')
        report={'status':'capture_incomplete' if receipt.get('gaps') else ('captured_summary' if summarized else ('captured_bounded' if bounded else 'captured')),
                'stage':'capture_only','mode':mode,'gaps':receipt.get('gaps',[]),
                'native_storage':config.getoption('--callchain-native-storage'),
                'pytest_exit_code':process.returncode,'elapsed_seconds':time.monotonic()-started,
                'replay_performed':False,'database_written':False,'html_generated':False}
        if bounded:report.update(reference_depth_limit=2,object_scope_complete=False,experimental=True,
                                 reference_boundary_objects=receipt.get('reference_boundary_objects',0))
        if summarized:report.update(scalar_container_summary_threshold=100,object_scope_complete=False,experimental=True,
                                    summarized_container_identities=receipt.get('summarized_container_identities',0),
                                    summary_updates=receipt.get('summary_updates',0))
        save(output/'capture-result.json',report)
        print('trace-api-chain: '+report['status']+'; raw evidence: '+str(output),flush=True)
        return process.returncode or (2 if report['gaps'] else 0)
    except Exception as e:
        save(output/'capture-result.json',{'status':'failed','error':str(e),'error_type':type(e).__name__,
                                         'elapsed_seconds':time.monotonic()-started,'raw_evidence_preserved':True})
        print('trace-api-chain: capture failed: '+str(e),file=sys.stderr)
        return 2


def pytest_configure(config):
    if os.environ.get(INTERNAL)!='1' or not config.getoption('--callchain-out'):return
    worker=Worker(config)
    config.pluginmanager.register(worker,'trace_api_chain_worker')


class Worker:
    def __init__(self,config):
        self.owner_pid=os.getpid()
        self.config=config;self.output=Path(config.getoption('--callchain-out')).resolve()
        self.recorder=None;self.bridge=None
        self.receipt={'started':False,'ended':False,'reports':[],'selected':[]}

    def pytest_collection_modifyitems(self,session,config,items):
        if len(items)!=1:raise pytest.UsageError('trace-api-chain requires exactly one collected pytest item')
        from .catalog import Registry
        from .object_capture import Recorder,test_body_code
        from .native import Bridge
        item=items[0]
        code=test_body_code(item)
        namespaces=tuple(n.strip() for n in config.getoption('--callchain-namespaces').split(',') if n.strip())
        registry=Registry(namespaces,catalog_path=config.getoption('--callchain-api-catalog'))
        save(self.output/'api-catalog.json',registry.describe())
        if config.getoption('--callchain-mode')=='full':self.bridge=Bridge(self.output,storage=config.getoption('--callchain-native-storage'))
        relation=getattr(self.config,'_relation_state',{}).get('observer')
        observers=[self]
        if relation is not None:
            observers.extend([relation,relation.collector,relation.registry,relation.bridge])
        self.recorder=Recorder(self.output/'events.jsonl',item.nodeid,str(item.path),namespaces=namespaces,
                               max_references=config.getoption('--callchain-max-references'),
                               run_id='pytest:'+item.nodeid,native=self.bridge,registry=registry,observer_roots=observers,
                               reference_deltas=config.getoption('--callchain-reference-storage')=='delta',
                               reference_depth_limit=2 if config.getoption('--callchain-reference-depth')=='2' else None,
                               scalar_container_threshold=100 if config.getoption('--callchain-scalar-container-summary') else None)
        self.recorder.body_code=code
        self.receipt['selected']=[item.nodeid]
        case=getattr(item,'_testcase',None)
        original=case._callTestMethod if case is not None else item.runtest
        def body(*args,**kwargs):
            self.recorder.begin();self.receipt['started']=True
            try:return original(*args,**kwargs)
            finally:
                self.recorder.end();self.receipt['ended']=True
        if case is not None:case._callTestMethod=body
        else:item.runtest=body

    def pytest_runtest_logreport(self,report):
        self.receipt['reports'].append({'nodeid':report.nodeid,'when':report.when,'outcome':report.outcome,
                                       'duration':report.duration})

    def pytest_sessionfinish(self,session,exitstatus):
        if os.getpid()!=self.owner_pid:
            if not self.recorder or (self.recorder.closed and self.recorder.fragment_pid!=os.getpid()):return
            self.recorder.redirect_fork_fragment()
            self.output=self.recorder.capture_path.parent
            self.receipt['fork_fragment']=True
        if self.bridge:self.bridge.close()
        if self.recorder and not self.recorder.closed:
            if self.recorder.started:self.recorder.end()
            else:
                self.recorder.emit('end',calls=0,roots=0,objects=0,gaps=['test_body_not_recorded'],all_objects_complete=False)
                self.recorder.file.close()
        if self.recorder:
            self.receipt['fork_fragments']=[str(path.parent) for path in sorted((self.output/'fork-fragments').glob('*/fork-fragment.json'))]
            self.receipt['fork_fragments_validated']=False
        self.receipt['pytest_exit_code']=int(exitstatus)
        if self.recorder and self.recorder.reference_depth_limit is not None:
            self.receipt.update(reference_depth_limit=self.recorder.reference_depth_limit,
                                reference_boundary_objects=len(self.recorder.depth_boundary_objects),
                                object_scope_complete=False,experimental=True)
        if self.recorder and self.recorder.scalar_container_threshold is not None:
            self.receipt.update(scalar_container_summary_threshold=self.recorder.scalar_container_threshold,
                                summarized_container_identities=len(self.recorder.scalar_summary_states),
                                summary_updates=self.recorder.scalar_summary_updates,object_scope_complete=False,experimental=True)
        self.receipt['gaps']=sorted(set((list(self.recorder.gaps) if self.recorder else [])+
                                      (self.bridge.gaps if self.bridge else [])))
        self.receipt['source_roots']={}
        for module_name,label in (('torch','pytorch'),('torch_npu','torch_npu')):
            module=sys.modules.get(module_name)
            filename=vars(module).get('__file__') if module is not None else None
            if filename:self.receipt['source_roots'][label]=str(Path(filename).resolve().parent.parent)
        save(self.output/'pytest-receipt.json',self.receipt)
