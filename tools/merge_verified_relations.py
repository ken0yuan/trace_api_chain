"""Add verified re-test evidence while preserving every prior completed record."""
from pathlib import Path
import json
import sqlite3


def merge_verified_relations(destination, source):
    destination=Path(destination).resolve();source=Path(source).resolve()
    if not source.is_file():raise ValueError('repair_relations_source_missing')
    db=sqlite3.connect(destination.as_uri(),uri=True,timeout=60)
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('ATTACH DATABASE ? AS repair',(source.as_uri()+'?mode=ro',))
        for schema in ('main','repair'):
            version=json.loads(db.execute('SELECT value FROM '+schema+".metadata WHERE key='schema'").fetchone()[0])
            if version!='api_test_relations_single_database/2':raise ValueError('repair_relations_schema_mismatch')
        if db.execute('PRAGMA repair.foreign_key_check').fetchone():raise ValueError('repair_relations_invalid')
        if db.execute('SELECT 1 FROM repair.tests WHERE capture_state<>\'complete\' LIMIT 1').fetchone():raise ValueError('repair_relations_incomplete')
        if db.execute('SELECT 1 FROM repair.capture_gaps LIMIT 1').fetchone():raise ValueError('repair_relations_capture_gap')
        required={'raw_mapping_and_owner_match','destination_mapping_and_phases_equal','foreign_key_check','integrity_check'}
        for (detail,) in db.execute('SELECT detail FROM repair.capture_runs'):
            receipt=json.loads(detail)
            if receipt.get('cleanup')!='complete' or not required<=set(receipt.get('checks',[])):raise ValueError('repair_relations_unverified_capture')
        columns='api_id,name,language,kind'
        if db.execute('SELECT 1 FROM (SELECT '+columns+' FROM repair.apis EXCEPT SELECT '+columns+' FROM main.apis) LIMIT 1').fetchone() or db.execute('SELECT 1 FROM (SELECT '+columns+' FROM main.apis EXCEPT SELECT '+columns+' FROM repair.apis) LIMIT 1').fetchone():raise ValueError('repair_relations_catalog_changed')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TEMP TABLE prior_observations AS SELECT * FROM observations')
        db.execute('CREATE TEMP TABLE prior_evidence AS SELECT * FROM observation_evidence')
        db.execute('CREATE TEMP TABLE prior_tests AS SELECT * FROM tests')
        db.execute('CREATE TEMP TABLE prior_phases AS SELECT * FROM test_phases')
        db.execute('''CREATE TEMP TABLE test_map AS SELECT r.id AS old_id,t.id AS new_id
            FROM repair.tests r JOIN repair.modules rm ON rm.id=r.module_id
            JOIN modules m ON m.module=rm.module AND m.file=rm.file
            JOIN tests t ON t.module_id=m.id AND t.nodeid=r.nodeid AND t.file=r.file
            WHERE t.capture_state='complete' ''')
        if db.execute('SELECT count(*) FROM test_map').fetchone()[0]!=db.execute('SELECT count(*) FROM repair.tests').fetchone()[0]:raise ValueError('repair_relations_test_identity_changed')
        db.execute('INSERT OR IGNORE INTO phases(name) SELECT name FROM repair.phases')
        db.execute('INSERT OR IGNORE INTO alias_groups(api_ids_json) SELECT api_ids_json FROM repair.alias_groups')
        db.execute('''CREATE TEMP TABLE incoming_phases AS SELECT t.new_id AS test_id,p.id AS phase_id,r.state
            FROM repair.test_phases r JOIN test_map t ON t.old_id=r.test_id
            JOIN repair.phases rp ON rp.id=r.phase_id JOIN phases p ON p.name=rp.name''')
        db.execute('INSERT OR IGNORE INTO test_phases SELECT * FROM incoming_phases')
        if db.execute('SELECT 1 FROM (SELECT * FROM incoming_phases EXCEPT SELECT * FROM test_phases) LIMIT 1').fetchone():raise ValueError('repair_relations_phase_conflict')
        joins=''' JOIN repair.apis ra ON ra.id=r.api_key JOIN apis a ON a.api_id=ra.api_id
            JOIN test_map t ON t.old_id=r.test_key JOIN repair.phases rp ON rp.id=r.phase_key JOIN phases p ON p.name=rp.name
            JOIN repair.alias_groups rg ON rg.id=r.alias_group_key JOIN alias_groups g ON g.api_ids_json=rg.api_ids_json'''
        db.execute('CREATE TEMP TABLE incoming_observations AS SELECT a.id AS api_key,t.new_id AS test_key,p.id AS phase_key,g.id AS alias_group_key FROM repair.observations r'+joins)
        db.execute('CREATE TEMP TABLE incoming_evidence AS SELECT a.id AS api_key,t.new_id AS test_key,p.id AS phase_key,g.id AS alias_group_key,r.site,r.source_capture,r.source_shard FROM repair.observation_evidence r'+joins)
        for table,incoming in (('observations','incoming_observations'),('observation_evidence','incoming_evidence')):
            source_table=table
            if db.execute('SELECT count(*) FROM '+incoming).fetchone()[0]!=db.execute('SELECT count(*) FROM repair.'+source_table).fetchone()[0]:raise ValueError('repair_relations_mapping_incomplete')
            db.execute('INSERT OR IGNORE INTO '+table+' SELECT * FROM '+incoming)
            if db.execute('SELECT 1 FROM (SELECT * FROM '+incoming+' EXCEPT SELECT * FROM '+table+') LIMIT 1').fetchone():raise ValueError('repair_relations_evidence_missing')
        if db.execute('SELECT 1 FROM repair.capture_runs r JOIN main.capture_runs m ON m.attempt=r.attempt WHERE m.detail<>r.detail LIMIT 1').fetchone():raise ValueError('repair_relations_attempt_conflict')
        db.execute('INSERT OR IGNORE INTO capture_runs SELECT * FROM repair.capture_runs')
        for prior,current in (('prior_observations','observations'),('prior_evidence','observation_evidence'),('prior_tests','tests'),('prior_phases','test_phases')):
            if db.execute('SELECT 1 FROM (SELECT * FROM '+prior+' EXCEPT SELECT * FROM '+current+') LIMIT 1').fetchone():raise ValueError('repair_relations_prior_evidence_changed')
        if db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('repair_relations_foreign_key_failure')
        if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise ValueError('repair_relations_integrity_failure')
        summary={'tests_verified':db.execute('SELECT count(*) FROM test_map').fetchone()[0],
                 'new_observations':db.execute('SELECT count(*) FROM observations').fetchone()[0]-db.execute('SELECT count(*) FROM prior_observations').fetchone()[0],
                 'new_evidence':db.execute('SELECT count(*) FROM observation_evidence').fetchone()[0]-db.execute('SELECT count(*) FROM prior_evidence').fetchone()[0],
                 'prior_evidence_preserved':True,'completed_tests_preserved':True,'foreign_key_check':'passed','integrity_check':'passed'}
        db.commit();return summary
    except BaseException:db.rollback();raise
    finally:db.close()
