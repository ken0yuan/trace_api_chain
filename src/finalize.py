"""Delete task intermediates only after both committed databases are verified."""
import json
from pathlib import Path
import shutil
import sqlite3
import time
from .native import digest
from .cache_io import atomic_json

def committed_state(graph_database,relations_database,result,expected_graph):
    with sqlite3.connect(Path(graph_database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT status,summary FROM capture_slots WHERE id=?',(result['id'],)).fetchone()
    if not row or row[0]!='current':raise ValueError('graph_slot_not_current')
    summary=json.loads(row[1])
    if summary.get('gaps')!=[] or summary.get('graph_digest')!=expected_graph:raise ValueError('graph_slot_not_verified')
    if summary.get('test')!=result['nodeid'] or Path(summary['raw_evidence']).resolve()!=Path(result['capture_dir']).resolve():raise ValueError('graph_slot_task_binding_mismatch')
    attempt=str(Path(result['relations_dir']).resolve())
    with sqlite3.connect(Path(relations_database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT module,detail FROM capture_runs WHERE attempt=?',(attempt,)).fetchone()
        if not row or row[0]!=result['file']:raise ValueError('relation_attempt_not_committed')
        detail=json.loads(row[1]);required={'raw_mapping_and_owner_match','destination_mapping_and_phases_equal','prior_positive_subset','completed_instances_preserved','foreign_key_check','integrity_check'}
        if not required.issubset(detail.get('checks',[])) or detail.get('boundaries'):raise ValueError('relation_attempt_not_verified')
        # The relation store's documented repository label prefixes pytest's
        # relative nodeid with "pytorch/". Accept only that exact path mapping,
        # never a suffix/function-name match or another capture attempt.
        node=result['nodeid'];names=(node,'pytorch/'+node) if not node.startswith('pytorch/') else (node,node.removeprefix('pytorch/'))
        tests=db.execute('SELECT t.capture_state,t.source_attempt FROM tests t JOIN modules m ON m.id=t.module_id WHERE m.module=? AND t.nodeid IN (?,?)',(result['file'],*names)).fetchall()
        if len(tests)!=1 or tests[0]!=('complete',result['id']):raise ValueError('relation_test_not_complete_or_ambiguous')
    return summary,detail

def cleanup_verified(result,receipt_path,graph_database,relations_database,graph_file,targets,allowed_roots):
    receipt_path=Path(receipt_path).resolve();targets=[Path(p).absolute() for p in targets]
    roots=[Path(p).resolve() for p in allowed_roots]
    databases=[Path(graph_database).resolve(),Path(relations_database).resolve()]
    for target in targets:
        resolved=target.resolve()
        if not any(resolved!=root and resolved.is_relative_to(root) for root in roots):raise ValueError('cleanup_target_outside_task_root:'+str(target))
        if target.is_symlink():raise ValueError('cleanup_target_is_symlink:'+str(target))
        if any(p==resolved or p.is_relative_to(resolved) for p in [receipt_path,*databases]):raise ValueError('cleanup_contains_final_output')
    cap=Path(result['capture_dir'])
    if list((cap/'fork-fragments').glob('*/fork-fragment.json')):raise ValueError('unvalidated_fork_entities_preserved')
    paths=[str(p) for p in targets]
    prior=result.get('finalization',{})
    if prior and prior.get('state') not in ('verified_committed','cleaned'):raise ValueError('invalid_cleanup_receipt_state')
    if prior.get('state') in ('verified_committed','cleaned'):
        if prior.get('targets')!=paths:raise ValueError('cleanup_retry_target_mismatch')
        expected=prior['graph_sha256']
    else:
        graph_file=Path(graph_file);expected=digest(graph_file)
        report=json.loads((cap/'replay-result.json').read_text())
        if report.get('gaps')!=[] or report.get('graph_digest')!=expected:raise ValueError('graph_file_not_verified')
    summary,relation=committed_state(graph_database,relations_database,result,expected)
    if not prior:
        load=json.loads((cap/'dual-plugin-load.json').read_text())
        result['finalization']={'state':'verified_committed','verified_at':time.time(),'graph_sha256':expected,
            'graph_gaps':[],'slot':result['id'],'relation_attempt':str(Path(result['relations_dir']).resolve()),
            'relation_checks':relation['checks'],'dual_plugin_load':load,'targets':paths,
            'scope':'calling-thread host graph; unvalidated fork fragments are never deleted'}
        atomic_json(receipt_path,result)
    # Intent was durably published first. A crash here can resume cleanup without
    # reconstructing or recollecting an already committed task.
    for target in targets:
        if target.is_dir():shutil.rmtree(target)
        elif target.exists():target.unlink()
    result['finalization'].update(state='cleaned',cleaned_at=time.time(),all_task_intermediates_removed=True)
    atomic_json(receipt_path,result)
    return result['finalization']
