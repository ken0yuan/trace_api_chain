"""Import fresh, validated repair slots without replacing any existing slot.

This maintenance tool accepts only unchanged source versions and a single current
slot per input. Normal source-version replacement remains the replay CLI's job.
All imports in a batch and the final full foreign-key check share one transaction.
"""
from pathlib import Path
import json
import sqlite3
import time

from trace_api_chain.database import connect


def merge_verified_graphs(destination, inputs):
    inputs=[Path(p).resolve() for p in inputs]
    if not 0<len(inputs)<=8:raise ValueError('repair_batch_must_contain_1_to_8_databases')
    initialized=connect(destination);initialized.close()
    db=sqlite3.connect(Path(destination).resolve().as_uri(),uri=True,timeout=60)
    db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA main.cache_size=-262144')
    db.execute('PRAGMA temp_store=MEMORY')
    slots=[]
    try:
        for i,path in enumerate(inputs):
            if not path.is_file():raise ValueError('repair_input_missing')
            db.execute('ATTACH DATABASE ? AS repair'+str(i),(path.as_uri()+'?mode=ro',))
        db.execute('BEGIN IMMEDIATE')
        for i,path in enumerate(inputs):
            src='repair'+str(i)
            version=db.execute('SELECT version FROM '+src+'.schema_info').fetchall()
            if [r[0] for r in version]!=[1]:raise ValueError('unsupported_repair_schema')
            records=list(db.execute('SELECT id,status,summary FROM '+src+'.capture_slots'))
            if len(records)!=1 or records[0][1]!='current':raise ValueError('repair_requires_one_complete_slot')
            slot=records[0][0];summary=json.loads(records[0][2])
            started=time.monotonic()
            print('VERIFYING_REPAIR_SLOT',slot,flush=True)
            if summary.get('gaps'):raise ValueError('repair_has_capture_gaps')
            if db.execute('SELECT 1 FROM capture_slots WHERE id=?',(slot,)).fetchone():raise ValueError('repair_cannot_replace_existing_slot')
            if db.execute('PRAGMA '+src+'.foreign_key_check').fetchone():raise ValueError('repair_input_foreign_key_failure')
            conflict=db.execute('SELECT 1 FROM '+src+'.entities r JOIN entities e ON e.id=r.id WHERE e.path IS NOT r.path OR e.kind IS NOT r.kind OR e.name IS NOT r.name OR (e.source_sha256 IS NOT NULL AND r.source_sha256 IS NOT NULL AND e.source_sha256<>r.source_sha256) LIMIT 1').fetchone()
            if conflict:raise ValueError('repair_source_identity_or_version_conflict')
            conflict=db.execute('SELECT 1 FROM (SELECT DISTINCT path,source_sha256 FROM '+src+'.entities WHERE path IS NOT NULL AND source_sha256 IS NOT NULL) r JOIN entities e ON e.path=r.path WHERE e.source_sha256 IS NOT NULL AND e.source_sha256<>r.source_sha256 LIMIT 1').fetchone()
            if conflict:raise ValueError('repair_source_version_changed')
            if db.execute('SELECT 1 FROM '+src+'.apis r JOIN apis e ON e.id=r.id WHERE e.entity_id<>r.entity_id LIMIT 1').fetchone():raise ValueError('repair_api_identity_conflict')
            if db.execute('SELECT 1 FROM '+src+'.paths r JOIN paths e ON e.id=r.id WHERE e.api_id IS NOT r.api_id OR e.parent_id IS NOT r.parent_id OR e.entity_id IS NOT r.entity_id OR e.edge_kind IS NOT r.edge_kind OR e.depth IS NOT r.depth LIMIT 1').fetchone():raise ValueError('repair_path_identity_conflict')
            db.execute('INSERT INTO entities SELECT * FROM '+src+'.entities WHERE true ON CONFLICT(id) DO UPDATE SET path=excluded.path,name=excluded.name,source_sha256=excluded.source_sha256,status=excluded.status,detail=excluded.detail')
            db.execute('INSERT INTO capture_slots SELECT * FROM '+src+'.capture_slots')
            for table in ('slot_entities','aliases','apis','api_aliases','api_alias_support'):
                db.execute('INSERT OR IGNORE INTO '+table+' SELECT * FROM '+src+'.'+table)
            db.execute('INSERT OR IGNORE INTO paths SELECT * FROM '+src+'.paths ORDER BY depth')
            for table in ('path_support','observed_objects','object_references','source_dependencies','pending'):
                db.execute('INSERT INTO '+table+' SELECT * FROM '+src+'.'+table)
            # Every source row must survive import; global tables may have more.
            tables=[r[0] for r in db.execute('SELECT name FROM '+src+".sqlite_master WHERE type='table' AND name<>'schema_info'")]
            for table in tables:
                if table=='entities':
                    # Entity annotations follow the normal latest-observation rule.
                    continue
                if table=='paths':
                    # Existing path values were checked above; new paths have
                    # only their primary-key constraint. Probe the input keys,
                    # rather than scanning every path of every prior test.
                    missing=db.execute('SELECT 1 FROM '+src+'.paths r LEFT JOIN main.paths p ON p.id=r.id WHERE p.id IS NULL LIMIT 1').fetchone()
                    if missing:raise ValueError('repair_evidence_missing:paths')
                    continue
                cols=[r[1] for r in db.execute('PRAGMA main.table_info('+table+')')]
                selection=' WHERE slot_id=?' if 'slot_id' in cols else ''
                missing=db.execute('SELECT 1 FROM (SELECT * FROM '+src+'.'+table+' EXCEPT SELECT * FROM main.'+table+selection+') LIMIT 1',(slot,) if selection else ()).fetchone()
                if missing:raise ValueError('repair_evidence_missing:'+table)
            slots.append(slot)
            print('VERIFIED_REPAIR_SLOT',slot,round(time.monotonic()-started,3),flush=True)
        if db.execute('PRAGMA foreign_key_check').fetchone():raise ValueError('repair_destination_foreign_key_failure')
        db.commit()
        rows={'slots_imported':slots,'foreign_key_check':'passed','transaction':'committed'}
        return rows
    except BaseException:
        db.rollback();raise
    finally:db.close()
