"""Offline event replay. Never imported by the pytest capture path."""
import json
from pathlib import Path
import shutil
from .native import decode,digest,save
from .replay import rebuild,canonical
from .symbols import resolve

def prepare(output,mode=None):
    output=Path(output)
    mode=mode or json.loads((output/'capture-result.json').read_text()).get('mode','full')
    evidence=output/'evidence';manifest_path=evidence/'manifest.json'
    if manifest_path.exists():
        manifest=json.loads(manifest_path.read_text())
        for name,sha in manifest['sha256'].items():
            if digest(evidence/name)!=sha:raise ValueError('frozen_evidence_changed:'+name)
        for name,sha in manifest.get('raw_sha256',{}).items():
            if digest(output/name)!=sha:raise ValueError('raw_evidence_changed:'+name)
        events=[json.loads(x) for x in (evidence/'python.jsonl').read_text().splitlines()]
        native=[json.loads(x) for x in (evidence/'native.jsonl').read_text().splitlines()]
        gaps=manifest.get('capture_gaps',[])
    else:
        events=[json.loads(x) for x in (output/'events.jsonl').read_text().splitlines()]
        native=[];gaps=[]
        if mode=='full':
            native,modules,gaps=decode(output);gaps+=sorted(modules.gaps)
            windows=json.loads((output/'windows.json').read_text())
            if windows['segments'] and not native:raise ValueError('no_compiler_events: instrumented target library required')
        rebuild(events,native,json.loads(Path(__file__).with_name('contract.json').read_text()))
        evidence.mkdir(exist_ok=True);shutil.copyfile(output/'events.jsonl',evidence/'python.jsonl')
        with (evidence/'native.jsonl').open('wb') as f:
            for row in native:f.write(canonical(row))
        raw=[output/'events.jsonl',*sorted(output.glob('segment-*.bin'))]
        raw += [output/name for name in ('windows.json','maps.txt','maps-before.txt','api-catalog.json','pytest-receipt.json') if (output/name).exists()]
        manifest={'sha256':{name:digest(evidence/name) for name in ('python.jsonl','native.jsonl')},
                  'raw_sha256':{p.name:digest(p) for p in raw},'capture_gaps':gaps,'mode':mode}
        save(manifest_path,manifest)
    graph=rebuild(events,native,json.loads(Path(__file__).with_name('contract.json').read_text()))
    graph['gaps']=sorted(set(graph['gaps']+gaps))
    if graph['gaps']:graph['status']='evidence_scope_incomplete'
    (output/'graph.json').write_bytes(canonical(graph))
    identities={}
    for row in native:
        for field in ('source_identity','caller_identity'):
            identity=row[field]
            if identity.get('path') and isinstance(identity.get('offset'),int):identities[identity['path'],identity['offset']]=identity
    print('trace-api-chain replay: resolving addresses offline',flush=True)
    try:resolved=resolve(identities.values(),output/'symbol-cache') if identities else {}
    except RuntimeError:resolved={}
    receipt=json.loads((output/'pytest-receipt.json').read_text())
    return {'output':output,'events':events,'native':native,'graph':graph,'resolved':resolved,
            'manifest':manifest,'receipt':receipt,'mode':mode}
