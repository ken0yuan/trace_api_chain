"""Durable, hash-bound handoff from parallel normalization to one SQL writer."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from .cache_io import atomic_json
from .native import digest

def policy():
    return {name:digest(Path(__file__).with_name(name)) for name in
            ('staged.py','database.py','pipeline.py','replay.py','compact.py','symbols.py','source_entities.py','reference_codec.py','reference_transport.py','reference_state.py')}

def prepare_file(capture,target,mode=None,roots=None,slot=None,symbol_index=None):
    from .pipeline import prepare
    from .database import prepare_refresh
    capture=Path(capture).resolve();target=Path(target).resolve();target.parent.mkdir(parents=True,exist_ok=True)
    ready=target.with_name(target.name+'.ready.json')
    if target.exists() and ready.exists():
        _,receipt=load_file(target)
        if receipt['capture']!=str(capture) or (slot and receipt['slot']!=slot):raise ValueError('refresh_handoff_reuse_binding_mismatch')
        return {**receipt,'reused':True}
    workspace=target.with_name(target.name+'.work')
    if workspace.exists():raise ValueError('unfinished_refresh_workspace_retained:'+str(workspace))
    workspace.mkdir()
    # Completed capture files are immutable input. Hard links avoid copying
    # their bytes; derived outputs and caches belong only to this task.
    excluded={'symbol-cache','evidence','graph.json','replay-result.json','fork-fragments'}
    def link_inputs(source,destination):
        for entry in source.iterdir():
            if source==capture and entry.name in excluded:continue
            out=destination/entry.name
            if entry.is_dir() and not entry.is_symlink():out.mkdir();link_inputs(entry,out)
            else:
                if entry.is_symlink():
                    resolved=entry.resolve(strict=True)
                    if not resolved.is_relative_to(capture):raise ValueError('capture_input_symlink_outside_task:'+str(entry))
                    os.link(resolved,out)
                else:os.link(entry,out)
    link_inputs(capture,workspace)
    data=prepare(workspace,mode,symbol_index=symbol_index);plan=prepare_refresh(data,roots,slot)
    plan['summary']['raw_evidence']=str(capture)
    payload=json.dumps(plan,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    checksum=hashlib.sha256(payload).hexdigest()
    with tempfile.NamedTemporaryFile(dir=target.parent,prefix=target.name+'.',suffix='.tmp',delete=False) as stream:
        stream.write(payload);stream.flush();os.fsync(stream.fileno());temporary=stream.name
    os.replace(temporary,target)
    receipt={'schema':'callchain_refresh_handoff/1','file':target.name,'sha256':checksum,
             'policy':policy(),'capture':str(capture),'workspace':str(workspace),'slot':plan['slot'],
             'manifest_sha256':digest(workspace/'evidence/manifest.json'),
             'graph_sha256':plan['summary']['graph_digest'],'gaps':plan['summary']['gaps']}
    atomic_json(ready,receipt)
    return receipt

def load_file(target):
    target=Path(target).resolve();receipt=json.loads(target.with_name(target.name+'.ready.json').read_text())
    if receipt.get('schema')!='callchain_refresh_handoff/1' or receipt.get('file')!=target.name:raise ValueError('invalid_refresh_handoff')
    if receipt.get('policy')!=policy():raise ValueError('refresh_handoff_policy_changed')
    before=target.stat();payload=target.read_bytes();after=target.stat()
    if (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):raise ValueError('refresh_plan_changed_during_read')
    if hashlib.sha256(payload).hexdigest()!=receipt['sha256']:raise ValueError('refresh_plan_checksum_mismatch')
    plan=json.loads(payload);capture=Path(receipt['capture']);workspace=Path(receipt['workspace'])
    if workspace!=target.with_name(target.name+'.work'):raise ValueError('refresh_workspace_binding_mismatch')
    manifest_path=workspace/'evidence/manifest.json'
    if digest(manifest_path)!=receipt['manifest_sha256']:raise ValueError('refresh_manifest_changed')
    manifest=json.loads(manifest_path.read_text())
    for name,sha in manifest['sha256'].items():
        if digest(workspace/'evidence'/name)!=sha:raise ValueError('frozen_evidence_changed:'+name)
    for name,sha in manifest.get('raw_sha256',{}).items():
        if digest(capture/name)!=sha or digest(workspace/name)!=sha:raise ValueError('raw_evidence_changed:'+name)
    if digest(workspace/'graph.json')!=receipt['graph_sha256']:raise ValueError('refresh_graph_changed')
    if plan['summary']['graph_digest']!=receipt['graph_sha256'] or plan['slot']!=receipt['slot'] or plan['summary']['raw_evidence']!=str(capture):raise ValueError('refresh_handoff_binding_mismatch')
    return plan,receipt

def commit_file(database,target,cache_mib=4096):
    from .database import commit_refresh
    from .native import save
    plan,receipt=load_file(target)
    report=commit_refresh(database,plan,cache_mib)
    save(Path(receipt['capture'])/'replay-result.json',report)
    return report
