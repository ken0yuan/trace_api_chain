import copy
import sqlite3

import pytest

from trace_api_chain import database
from trace_api_chain.writer_session import WriterSession
from test_database import capture
from test_staged_refresh import table_contents


def test_session_preserves_every_table_for_append_replace_and_noop(tmp_path):
    data = capture(tmp_path, 'def test_one():\n backend.run(1)\n backend.run(2)\n')
    old = tmp_path / 'reference.sqlite'
    new = tmp_path / 'session.sqlite'
    session = WriterSession(new)
    try:
        for index, slot in enumerate(['first', 'second', 'first', 'first']):
            plan = database.prepare_refresh(data, slot=slot)
            if index >= 2:
                plan['import_digest'] += '-replacement'
                plan['aliases'].append((next(iter(plan['entities'])), 'new_alias', 'fixture'))
            assert database.commit_refresh(old, plan) == session.commit_plan(plan)
            assert table_contents(old) == table_contents(new)
        assert len(table_contents(new)) == 14
        assert session.audit_count == 1
        assert session.reused_check_count == 3
        assert session.connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    finally:
        session.close()


def test_corrupt_preexisting_database_is_rejected_before_import(tmp_path):
    target = tmp_path / 'corrupt.sqlite'
    db = database.connect(target)
    db.close()
    with sqlite3.connect(target) as db:
        db.execute("INSERT INTO aliases VALUES('missing','alias','fixture')")
    data = capture(tmp_path, 'def test_one():\n backend.run(1)\n')
    session = WriterSession(target)
    try:
        with pytest.raises(ValueError, match='foreign_key_check_failed'):
            session.commit_plan(database.prepare_refresh(data, slot='first'))
        assert not session.connection.execute('SELECT 1 FROM capture_slots').fetchall()
    finally:
        session.close()


def test_external_commit_forces_another_audit_and_rejects_corruption(tmp_path):
    data = capture(tmp_path, 'def test_one():\n backend.run(1)\n')
    target = tmp_path / 'external.sqlite'
    session = WriterSession(target)
    try:
        session.commit_plan(database.prepare_refresh(data, slot='first'))
        with sqlite3.connect(target) as db:
            db.execute("INSERT INTO aliases VALUES('missing','bad','external')")
        with pytest.raises(ValueError, match='foreign_key_check_failed'):
            session.commit_plan(database.prepare_refresh(data, slot='second'))
        assert session.audit_count == 2
        assert session.connection.execute('SELECT count(*) FROM capture_slots').fetchone()[0] == 1
    finally:
        session.close()


def test_new_foreign_key_violation_rolls_back_and_preserves_prior_data(tmp_path):
    data = capture(tmp_path, 'def test_one():\n backend.run(1)\n')
    target = tmp_path / 'rollback.sqlite'
    session = WriterSession(target)
    try:
        session.commit_plan(database.prepare_refresh(data, slot='first'))
        before = table_contents(target)
        plan = copy.deepcopy(database.prepare_refresh(data, slot='second'))
        plan['aliases'].append(('missing', 'alias', 'injected'))
        with pytest.raises(sqlite3.IntegrityError):
            session.commit_plan(plan)
        assert table_contents(target) == before
        assert not session.connection.in_transaction
        session.commit_plan(database.prepare_refresh(data, slot='second'))
        assert session.connection.execute('SELECT count(*) FROM capture_slots').fetchone()[0] == 2
    finally:
        session.close()


def test_targeted_cleanup_replaces_slots_and_removes_only_original_orphans(tmp_path):
    data = capture(tmp_path, 'def test_one():\n backend.run(1)\n')
    old = tmp_path / 'reference.sqlite'
    new = tmp_path / 'session.sqlite'
    session = WriterSession(new)
    try:
        for slot, empty in [('first', False), ('second', False), ('first', True), ('second', True)]:
            plan = database.prepare_refresh(data, slot=slot)
            if empty:
                plan['import_digest'] += '-empty-paths'
                for key in ['paths', 'supports', 'object_links', 'reference_links', 'dependency_rows']:
                    plan[key] = []
            database.commit_refresh(old, plan)
            session.commit_plan(plan)
            assert table_contents(old) == table_contents(new)
        assert not session.connection.execute('SELECT 1 FROM paths').fetchall()
        assert session.full_cleanup_count == 1
        assert session.targeted_cleanup_count == 3
    finally:
        session.close()


def test_targeted_cleanup_preserves_null_id_semantics_and_external_orphans(tmp_path):
    data = capture(tmp_path, 'def test_one():\n backend.run(1)\n')
    old = tmp_path / 'reference.sqlite'
    new = tmp_path / 'session.sqlite'
    session = WriterSession(new)
    try:
        for slot in ['first', 'second']:
            plan = database.prepare_refresh(data, slot=slot)
            if slot == 'second':
                api, entity = next(iter(plan['api_entities'].items()))
                plan['paths'].extend([(None, api, None, entity, 'orphan', 0),
                                      ('unobserved', api, None, entity, 'orphan', 0)])
            database.commit_refresh(old, plan)
            session.commit_plan(plan)
            assert table_contents(old) == table_contents(new)
        for path in [old, new]:
            with sqlite3.connect(path) as db:
                api, entity = db.execute('SELECT id,entity_id FROM apis LIMIT 1').fetchone()
                db.execute('INSERT INTO paths VALUES(?,?,?,?,?,?)', ('external', api, None, entity, 'orphan', 0))
        plan = database.prepare_refresh(data, slot='third')
        database.commit_refresh(old, plan)
        session.commit_plan(plan)
        assert table_contents(old) == table_contents(new)
        assert session.audit_count == 2 and session.full_cleanup_count == 2
    finally:
        session.close()
