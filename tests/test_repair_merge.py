import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

from trace_api_chain.database import refresh,query
from test_database import capture

spec=importlib.util.spec_from_file_location('verified_repair',Path(__file__).parents[1]/'tools/merge_verified_graphs.py')
repair=importlib.util.module_from_spec(spec);spec.loader.exec_module(repair)


def test_repair_merge_matches_normal_import_and_preserves_other_slots(tmp_path):
    data=capture(tmp_path,'def test_one():\n backend.run(1)\n')
    destination=tmp_path/'destination.sqlite';normal=tmp_path/'normal.sqlite';source=tmp_path/'repair.sqlite'
    for db in (destination,normal):refresh(db,data,slot='existing')
    refresh(source,data,slot='repaired');refresh(normal,data,slot='repaired')
    report=repair.merge_verified_graphs(destination,[source])
    assert report['slots_imported']==['repaired']
    with sqlite3.connect(destination) as db:
        db.execute('ATTACH DATABASE ? AS normal',(str(normal),))
        for (table,) in db.execute("SELECT name FROM main.sqlite_master WHERE type='table'").fetchall():
            left='SELECT * FROM main.'+table;right='SELECT * FROM normal.'+table
            assert not db.execute('SELECT 1 FROM ('+left+' EXCEPT '+right+') LIMIT 1').fetchone(),table
            assert not db.execute('SELECT 1 FROM ('+right+' EXCEPT '+left+') LIMIT 1').fetchone(),table
    assert query(destination,'fixture_api.Backend.run')==query(normal,'fixture_api.Backend.run')


def test_failed_batch_rolls_back_all_imported_slots(tmp_path):
    data=capture(tmp_path,'def test_one():\n backend.run(1)\n')
    destination=tmp_path/'destination.sqlite';source=tmp_path/'repair.sqlite'
    refresh(destination,data,slot='existing');refresh(source,data,slot='new')
    before=query(destination,'fixture_api.Backend.run')
    # The second copy repeats the same slot, so the entire batch must roll back.
    with pytest.raises(ValueError,match='replace_existing_slot'):
        repair.merge_verified_graphs(destination,[source,source])
    assert query(destination,'fixture_api.Backend.run')==before


def test_repair_refuses_changed_source_and_incomplete_capture(tmp_path):
    data=capture(tmp_path,'def test_one():\n backend.run(1)\n')
    destination=tmp_path/'destination.sqlite';source=tmp_path/'repair.sqlite'
    refresh(destination,data,slot='existing');refresh(source,data,slot='new')
    with sqlite3.connect(source) as db:db.execute("UPDATE capture_slots SET status='partial'")
    with pytest.raises(ValueError,match='complete_slot'):repair.merge_verified_graphs(destination,[source])
    with sqlite3.connect(source) as db:
        db.execute("UPDATE capture_slots SET status='current'")
        db.execute("UPDATE entities SET source_sha256='different' WHERE source_sha256 IS NOT NULL")
    with pytest.raises(ValueError,match='source_identity_or_version_conflict'):
        repair.merge_verified_graphs(destination,[source])
