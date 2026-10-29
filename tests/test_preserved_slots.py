"""A replacement cannot erase prior proven branches, even on rollback/retry."""
import copy
import sqlite3
import pytest
from trace_api_chain import database
from trace_api_chain.writer_session import WriterSession
from test_database import capture
from test_staged_refresh import table_contents


SUPPORT=('slot_entities','api_alias_support','path_support','observed_objects',
         'object_references','source_dependencies','pending')


def contribution(tmp_path):
    data=capture(tmp_path,'def test_one():\n backend.run(1)\n')
    plan=database.prepare_refresh(data,slot='one')
    eid=next(iter(plan['entities']));pid=plan['paths'][0][0]
    plan['object_links'].append((pid,eid))
    plan['reference_links'].append((pid,eid,eid,'preserved_branch'))
    plan['dependency_rows'].append((pid,eid,'fixture','original-evidence'))
    plan['pending'].append(('unknown-object','retained-unresolved','original-detail'))
    return plan


def replacement(plan):
    value=copy.deepcopy(plan);value['import_digest']+='-new-execution'
    for key in ('paths','supports','object_links','reference_links','dependency_rows','pending'):
        value[key]=[]
    return value


def test_replacement_preserves_every_original_support_row_and_receipt(tmp_path):
    session=WriterSession(tmp_path/'graph.sqlite',preserve_prior_slots=True)
    try:
        old=contribution(tmp_path);session.commit_plan(old);db=session.connection
        receipt=tuple(db.execute('SELECT * FROM capture_slots WHERE id=?',('one',)).fetchone())
        original={t:[tuple(r)[1:] for r in db.execute('SELECT * FROM '+t+' WHERE slot_id=?',('one',))] for t in SUPPORT}
        old_paths={r[0] for r in db.execute('SELECT id FROM paths')}
        report=session.commit_plan(replacement(old));archives=report['preserved_previous_slots']
        assert len(archives)==1
        archived=archives[0]['archive_slot']
        assert tuple(db.execute('SELECT * FROM capture_slots WHERE id=?',(archived,)).fetchone())==(archived,*receipt[1:])
        for table in SUPPORT:
            assert [tuple(r)[1:] for r in db.execute('SELECT * FROM '+table+' WHERE slot_id=?',(archived,))]==original[table]
        assert old_paths<={r[0] for r in db.execute('SELECT id FROM paths')}
        assert db.execute('SELECT count(*) FROM path_support WHERE slot_id=?',(archived,)).fetchone()[0]>0
        before=table_contents(session.path)
        assert session.commit_plan(replacement(old))['preserved_previous_slots']==[]
        assert table_contents(session.path)==before
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    finally:session.close()


def test_archive_and_replacement_roll_back_together_on_invalid_new_reference(tmp_path):
    session=WriterSession(tmp_path/'rollback.sqlite',preserve_prior_slots=True)
    try:
        old=contribution(tmp_path);session.commit_plan(old);before=table_contents(session.path)
        bad=replacement(old);bad['aliases'].append(('missing-entity','bad','injected'))
        with pytest.raises(sqlite3.IntegrityError):session.commit_plan(bad)
        assert table_contents(session.path)==before
        assert not session.connection.in_transaction
    finally:session.close()
