import json
from pathlib import Path
import sqlite3
import pytest
from test_database import capture
from trace_api_chain.staged import prepare_file,commit_file
from trace_api_chain.finalize import cleanup_verified
from trace_api_chain.database import query

def setup(tmp_path):
    source=tmp_path/'inputs';source.mkdir();data=capture(source,'def test_one():\n backend.run(1)\n')
    plans=tmp_path/'plans';plans.mkdir();plan=plans/'one.json';prepare_file(data['output'],plan,mode='objects',slot='one')
    graph=tmp_path/'graph.sqlite';report=commit_file(graph,plan);relations=tmp_path/'relations.sqlite'
    result={'id':'one','file':'test_case.py','nodeid':data['graph']['test'],'capture_dir':str(data['output']),'relations_dir':str(tmp_path/'raw-relations')}
    detail={'checks':['raw_mapping_and_owner_match','destination_mapping_and_phases_equal','prior_positive_subset','completed_instances_preserved','foreign_key_check','integrity_check'],'boundaries':[]}
    with sqlite3.connect(relations) as db:
        db.executescript('CREATE TABLE capture_runs(attempt TEXT PRIMARY KEY,module TEXT,detail TEXT); CREATE TABLE modules(id INTEGER,module TEXT);CREATE TABLE tests(module_id INTEGER,nodeid TEXT,capture_state TEXT,source_attempt TEXT);')
        db.execute('INSERT INTO capture_runs VALUES(?,?,?)',(result['relations_dir'],result['file'],json.dumps(detail)))
        db.execute('INSERT INTO modules VALUES(1,?)',(result['file'],));db.execute('INSERT INTO tests VALUES(1,?,?,?)',(result['nodeid'],'complete',result['id']))
    (data['output']/'dual-plugin-load.json').write_text(json.dumps({'relations':'fixture_api_relations','callchain':'fixture_callchain'}))
    workspace=plan.with_name(plan.name+'.work');targets=[data['output'],workspace,plan,plan.with_name(plan.name+'.ready.json')]
    return result,tmp_path/'result.json',graph,relations,workspace/'graph.json',targets,[source,plans]

def test_delete_intermediates_only_after_both_databases_and_keep_sql_query(tmp_path):
    args=setup(tmp_path);before=query(args[2],'fixture_api.Backend.run')
    cleanup_verified(*args)
    assert all(not p.exists() for p in args[5])
    assert json.loads(args[1].read_text())['finalization']['state']=='cleaned'
    assert query(args[2],'fixture_api.Backend.run')==before
    cleanup_verified(*args)  # A repeated receipt does not recollect or rebuild.

@pytest.mark.parametrize('fault',['graph_slot','relation_receipt','graph_file','fork_branch'])
def test_uncommitted_or_new_fork_evidence_is_never_deleted(tmp_path,fault):
    args=setup(tmp_path)
    if fault=='graph_slot':
        with sqlite3.connect(args[2]) as db:db.execute("UPDATE capture_slots SET status='partial'")
    elif fault=='relation_receipt':
        with sqlite3.connect(args[3]) as db:db.execute('DELETE FROM capture_runs')
    elif fault=='graph_file':args[4].write_text('{}')
    else:
        p=Path(args[0]['capture_dir'])/'fork-fragments/123';p.mkdir(parents=True);(p/'fork-fragment.json').write_text('{}')
    with pytest.raises(ValueError):cleanup_verified(*args)
    assert all(p.exists() for p in args[5])

def test_cleanup_crash_restarts_from_durable_intent_without_raw_capture(tmp_path,monkeypatch):
    args=setup(tmp_path)
    from trace_api_chain import finalize
    original=finalize.shutil.rmtree;calls=[]
    def fail_second(path):
        calls.append(path)
        if len(calls)==2:raise OSError('forced cleanup interruption')
        original(path)
    with monkeypatch.context() as context:
        context.setattr(finalize.shutil,'rmtree',fail_second)
        with pytest.raises(OSError):cleanup_verified(*args)
    assert not Path(args[0]['capture_dir']).exists()
    restored=json.loads(args[1].read_text());assert restored['finalization']['state']=='verified_committed'
    cleanup_verified(restored,*args[1:]);assert all(not p.exists() for p in args[5])

def test_cleanup_rejects_source_outside_task_roots(tmp_path):
    args=setup(tmp_path);source=tmp_path/'source-repository';source.mkdir();(source/'source.py').write_text('precious')
    altered=list(args);altered[5]=[*args[5],source]
    with pytest.raises(ValueError,match='outside_task_root'):cleanup_verified(*altered)
    assert (source/'source.py').read_text()=='precious' and all(p.exists() for p in args[5])

def test_repository_prefixed_relation_node_is_exactly_mapped(tmp_path):
    args=setup(tmp_path)
    with sqlite3.connect(args[3]) as db:db.execute("UPDATE tests SET nodeid='pytorch/'||nodeid")
    cleanup_verified(*args);assert all(not p.exists() for p in args[5])

@pytest.mark.parametrize('fault',['ambiguous','other_attempt'])
def test_cleanup_rejects_ambiguous_node_or_different_capture_attempt(tmp_path,fault):
    args=setup(tmp_path)
    with sqlite3.connect(args[3]) as db:
        if fault=='ambiguous':db.execute("INSERT INTO tests SELECT module_id,'pytorch/'||nodeid,capture_state,source_attempt FROM tests")
        else:db.execute("UPDATE tests SET source_attempt='another-test-result'")
    with pytest.raises(ValueError,match='ambiguous'):cleanup_verified(*args)
    assert all(p.exists() for p in args[5])
