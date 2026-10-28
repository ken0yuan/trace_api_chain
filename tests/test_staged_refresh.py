import importlib.util
import json
import multiprocessing
from pathlib import Path
import sqlite3
import pytest
from test_database import capture
from trace_api_chain import database
from trace_api_chain.staged import prepare_file,commit_file

def table_contents(path):
    with sqlite3.connect(path) as db:
        tables=[r[0] for r in db.execute("select name from sqlite_master where type='table' order by name")]
        result={}
        for name in tables:
            columns=[r[1] for r in db.execute('pragma table_info('+name+')')]
            rows=list(db.execute('select * from '+name))
            if name=='capture_slots':
                # Implementation-policy hashes intentionally differ across versions.
                index=columns.index('import_digest');rows=[row[:index]+('POLICY',)+row[index+1:] for row in rows]
            result[name]=sorted(rows,key=repr)
        return result

def baseline(tmp_path):
    import trace_api_chain
    package=Path(trace_api_chain.__file__).parent;folder=tmp_path/'reference';folder.mkdir()
    source=Path(__file__).parent/'fixtures/database_032.py';target=folder/'database.py';target.write_bytes(source.read_bytes())
    for name in ['source_entities.py','changed_objects.py','dwarf_lookup.py']: (folder/name).write_bytes((package/name).read_bytes())
    spec=importlib.util.spec_from_file_location('trace_api_chain._reference_database',target)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def test_staged_handoff_matches_032_all_sql_tables_and_is_idempotent(tmp_path):
    data=capture(tmp_path,"m.exported=m.Backend.run\ndef test_one():\n backend.run(1)\n m.exported(backend,2)\n")
    old=tmp_path/'old.sqlite';new=tmp_path/'new.sqlite';reference=baseline(tmp_path)
    for slot in ['one','two']:reference.refresh(old,data,slot=slot)
    for slot in ['one','two']:
        artifact=tmp_path/(slot+'.json')
        prepare_file(data['output'],artifact,mode='objects',slot=slot)
        assert commit_file(new,artifact)['status']=='refreshed'
    assert commit_file(new,artifact)['status']=='unchanged'
    assert table_contents(old)==table_contents(new)
    assert len(table_contents(new))==14

def test_commit_does_not_repeat_graph_build_or_normalization(tmp_path,monkeypatch):
    data=capture(tmp_path,'def test_one():\n backend.run(1)\n');target=tmp_path/'handoff.json'
    prepare_file(data['output'],target,mode='objects',slot='one')
    def forbidden(*args,**kwargs):raise AssertionError('parallel stage recomputed inside writer')
    monkeypatch.setattr('trace_api_chain.pipeline.prepare',forbidden);monkeypatch.setattr(database,'normalize',forbidden)
    assert commit_file(tmp_path/'all.sqlite',target)['gaps']==[]

@pytest.mark.parametrize('changed',['plan','graph','raw','manifest','policy'])
def test_writer_rejects_changed_handoff_inputs_before_opening_database(tmp_path,changed):
    data=capture(tmp_path,'def test_one():\n backend.run(1)\n');target=tmp_path/'handoff.json'
    prepare_file(data['output'],target,mode='objects',slot='one');db=tmp_path/'all.sqlite'
    if changed=='plan':target.write_text(target.read_text()+' ')
    elif changed=='graph':(target.with_name(target.name+'.work')/'graph.json').write_text('{}')
    elif changed=='raw':(data['output']/'events.jsonl').write_text('{}\n')
    elif changed=='manifest':(target.with_name(target.name+'.work')/'evidence/manifest.json').write_text('{}')
    else:
        p=target.with_name(target.name+'.ready.json');r=json.loads(p.read_text());r['policy']={};p.write_text(json.dumps(r))
    with pytest.raises(ValueError):commit_file(db,target)
    assert not db.exists()

def test_prepare_reads_immutable_capture_and_uses_only_private_cache(tmp_path):
    data=capture(tmp_path,'def test_one():\n backend.run(1)\n');out=data['output']
    shared=tmp_path/'old-shared-cache';shared.mkdir();(shared/'unrelated.json').write_text('{"old":1}')
    (out/'symbol-cache').symlink_to(shared,target_is_directory=True)
    before={str(p.relative_to(out)):p.read_bytes() for p in out.rglob('*') if p.is_file() and 'symbol-cache' not in p.parts}
    target=tmp_path/'handoff.json';receipt=prepare_file(out,target,mode='objects',slot='one')
    workspace=Path(receipt['workspace']);assert not (workspace/'symbol-cache').is_symlink()
    assert (shared/'unrelated.json').read_text()=='{"old":1}'
    assert before=={str(p.relative_to(out)):p.read_bytes() for p in out.rglob('*') if p.is_file() and 'symbol-cache' not in p.parts}
    assert prepare_file(out,target,mode='objects',slot='one')['reused'] is True
