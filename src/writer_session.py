"""Reuse a verified SQLite connection without rescanning unchanged history.

The original importer and its handoff policy remain byte-for-byte unchanged.
Every write still runs with SQLite foreign keys enabled. Under BEGIN IMMEDIATE,
audit the existing database once, or again whenever another connection committed.
Immediate constraints (and commit-time deferred constraints) preserve that
invariant for our own transactions. No entities, paths or evidence are filtered.
"""
from pathlib import Path
import hashlib
import json
import time
import types

from . import database


def merge_entity_detail(previous,current):
    """Keep exact prior metadata observations and correlated field values."""
    if previous==current:return current
    field='observed_metadata_variants'
    merged=dict(current)
    for key,value in previous.items():
        if key!=field and key not in merged:merged[key]=value
    for key in ('runtime_names','linkage_names'):
        if key in previous or key in current:
            merged[key]=sorted(set(previous.get(key,[]))|set(current.get(key,[])))
    variants={}
    for value in (previous,current):
        observations=value.get(field) or [{key:item for key,item in value.items() if key!=field}]
        for observation in observations:
            variants[json.dumps(observation,sort_keys=True,ensure_ascii=False)]=observation
    merged[field]=[variants[key] for key in sorted(variants)]
    return merged


class WriterSession:
    def __init__(self, path, cache_mib=4096, preserve_prior_slots=False):
        if type(preserve_prior_slots) is not bool:
            raise TypeError('preserve_prior_slots_must_be_bool')
        self.path = Path(path).resolve()
        self.preserve_prior_slots = preserve_prior_slots
        self.archived_this_commit = []
        self.cache_mib = cache_mib
        self.connection = database.connect(self.path, cache_mib=cache_mib)
        stat = self.path.stat()
        self.file_identity = (stat.st_dev, stat.st_ino)
        self.audited_version = None
        self.audit_count = 0
        self.reused_check_count = 0
        self.audit_seconds = 0.0
        self.active = False
        self.closed = False
        self.cleanup_baseline_ready = False
        self.cleanup_pending = False
        self.cleanup_ids = set()
        self.full_cleanup_count = 0
        self.targeted_cleanup_count = 0
        original = database.commit_refresh
        if original.__closure__:
            raise ValueError('unsupported_importer_closure')
        # Bind unchanged import bytecode to a private connection provider. Do not
        # monkeypatch module globals used by other callers or change plan hashes.
        scope = dict(original.__globals__, connect=self._connect)
        self._import = types.FunctionType(original.__code__, scope,
                                          original.__name__, original.__defaults__)
        self._importers = {original:self._import}
        self.old_entity_details={}

    def _connect(self, path, readonly=False, cache_mib=4096):
        if self.closed or readonly or Path(path).resolve() != self.path or cache_mib != self.cache_mib:
            raise ValueError('writer_session_connection_mismatch')
        return _Connection(self)

    def _begin(self, sql, parameters):
        stat = self.path.stat()
        if (stat.st_dev, stat.st_ino) != self.file_identity:
            raise ValueError('writer_session_database_replaced')
        cursor = self.connection.execute(sql, parameters)
        self.active = True
        self.archived_this_commit = []
        self.cleanup_pending = False
        self.old_entity_details={}
        if self.connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
            raise ValueError('writer_session_foreign_keys_disabled')
        version = self.connection.execute('PRAGMA data_version').fetchone()[0]
        if self.audited_version != version:
            self.cleanup_baseline_ready = False
            started = time.monotonic()
            violations = self.connection.execute('PRAGMA foreign_key_check').fetchall()
            self.audit_seconds += time.monotonic() - started
            self.audit_count += 1
            if violations:
                self.audited_version = None
                raise ValueError('database_foreign_key_check_failed')
            self.audited_version = version
        return cursor

    def _cleanup(self, sql, parameters):
        db = self.connection
        if not self.cleanup_baseline_ready:
            cursor = db.execute(sql, parameters)
            self.full_cleanup_count += 1
            self.cleanup_pending = True
            return cursor
        # Only paths whose support can change in the unchanged importer may
        # become newly unsupported: paths of the replaced slot and input paths.
        # The first pass (or any external commit) uses the original global SQL.
        db.execute('CREATE TEMP TABLE IF NOT EXISTS writer_cleanup_candidates(id TEXT PRIMARY KEY)')
        db.execute('DELETE FROM temp.writer_cleanup_candidates')
        db.executemany('INSERT INTO temp.writer_cleanup_candidates VALUES(?)',
                       [(pid,) for pid in sorted(p for p in self.cleanup_ids if p is not None)])
        cursor = db.execute('''DELETE FROM paths WHERE rowid IN (
            SELECT p.rowid FROM temp.writer_cleanup_candidates c CROSS JOIN paths p
            WHERE p.id=c.id AND NOT EXISTS (
                SELECT 1 FROM path_support s WHERE s.path_id=p.id) ORDER BY p.id)''')
        if None in self.cleanup_ids:
            # TEXT PRIMARY KEY historically permits NULL. Preserve the original
            # rowid-based cleanup semantics instead of relying on NULL equality.
            cursor = db.execute('''DELETE FROM paths WHERE rowid IN (
                SELECT p.rowid FROM paths p WHERE p.id IS NULL AND NOT EXISTS (
                    SELECT 1 FROM path_support s WHERE s.path_id=p.id) ORDER BY p.id)''')
        self.targeted_cleanup_count += 1
        return cursor

    def commit_plan(self, plan, importer=None):
        if self.closed:
            raise ValueError('writer_session_closed')
        self.cleanup_ids = {row[0] for row in plan.get('paths', [])}
        bound=self._import
        if importer is not None:
            if not isinstance(importer,types.FunctionType) or importer.__closure__:
                raise ValueError('unsupported_frozen_importer')
            bound=self._importers.get(importer)
            if bound is None:
                scope=dict(importer.__globals__,connect=self._connect)
                bound=types.FunctionType(importer.__code__,scope,importer.__name__,importer.__defaults__)
                self._importers[importer]=bound
        report = bound(self.path, plan, cache_mib=self.cache_mib)
        self.old_entity_details={}
        if self.preserve_prior_slots:
            report['preserved_previous_slots'] = list(self.archived_this_commit)
        return report

    def _preserve_slot(self, slot):
        """Keep exact prior source support inside the replacement transaction.

        A later execution can take fewer branches. Its current receipt must not
        erase branches already proven by another complete execution. The old
        receipt and all seven support tables remain queryable under a stable
        history slot; shared entities/paths are never copied or filtered.
        """
        db = self.connection
        if not self.active or not db.in_transaction:
            raise ValueError('slot_preservation_requires_writer_transaction')
        old = db.execute('SELECT * FROM capture_slots WHERE id=?', (slot,)).fetchone()
        if old is None:
            return
        json.loads(old['summary'])  # Reject an invalid receipt before replacement.
        suffix = hashlib.sha256(json.dumps(list(old), ensure_ascii=False).encode()).hexdigest()
        archive = slot + '@history:' + suffix
        expected = (archive, *tuple(old)[1:])
        existing = db.execute('SELECT * FROM capture_slots WHERE id=?', (archive,)).fetchone()
        if existing is not None and tuple(existing) != expected:
            raise ValueError('preserved_slot_receipt_conflict')
        if existing is None:
            db.execute('INSERT INTO capture_slots VALUES(?,?,?,?,?,?,?)', expected)
        tables = ('slot_entities', 'api_alias_support', 'path_support', 'observed_objects',
                  'object_references', 'source_dependencies', 'pending')
        counts = {}
        for table in tables:
            columns = [row[1] for row in db.execute('PRAGMA table_info(' + table + ')')]
            if not columns or columns[0] != 'slot_id':
                raise ValueError('unsupported_preservation_table:' + table)
            payload = ','.join('"' + name + '"' for name in columns[1:])
            if existing is None:
                db.execute('INSERT INTO ' + table + ' SELECT ?,' + payload +
                           ' FROM ' + table + ' WHERE slot_id=?', (archive, slot))
            count = db.execute('SELECT count(*) FROM ' + table + ' WHERE slot_id=?', (slot,)).fetchone()[0]
            if count != db.execute('SELECT count(*) FROM ' + table + ' WHERE slot_id=?', (archive,)).fetchone()[0]:
                raise ValueError('preserved_slot_row_count_conflict:' + table)
            for first, second in ((slot, archive), (archive, slot)):
                difference = db.execute('SELECT ' + payload + ' FROM ' + table +
                                        ' WHERE slot_id=? EXCEPT SELECT ' + payload + ' FROM ' +
                                        table + ' WHERE slot_id=? LIMIT 1', (first, second)).fetchone()
                if difference is not None:
                    raise ValueError('preserved_slot_content_conflict:' + table)
            counts[table] = count
        self.archived_this_commit.append({'source_slot': slot, 'archive_slot': archive,
                                         'import_digest': old['import_digest'], 'rows': counts})

    def commit_file(self, target, loader=None):
        from .staged import load_file
        from .native import save
        plan, receipt = (loader or load_file)(target)
        factory=getattr(loader,'importer',None) if loader else None
        report = self.commit_plan(plan,importer=factory(plan) if factory else None)
        report['writer_validation'] = {
            'policy': 'sqlite-enforced-incremental-foreign-keys/1',
            'baseline_or_external_change_audits': self.audit_count,
            'unchanged_history_scans_reused': self.reused_check_count,
            'full_audit_seconds': self.audit_seconds,
            'global_cleanup_scans': self.full_cleanup_count,
            'changed_path_cleanup_passes': self.targeted_cleanup_count,
        }
        save(Path(receipt['capture']) / 'replay-result.json', report)
        return report

    def close(self):
        if not self.closed:
            self.connection.close()
            self.closed = True


class _Connection:
    def __init__(self, owner):
        self.owner = owner

    def execute(self, sql, parameters=()):
        normalized = sql.strip().upper()
        if self.owner.preserve_prior_slots and normalized=='SELECT SOURCE_SHA256 FROM ENTITIES WHERE ID=?':
            cursor=self.owner.connection.execute('SELECT source_sha256,detail FROM entities WHERE id=?',parameters)
            return _EntityCursor(self.owner,parameters[0],cursor)
        if self.owner.preserve_prior_slots and normalized.startswith('INSERT INTO ENTITIES VALUES('):
            entity=parameters[0]
            if entity not in self.owner.old_entity_details:
                old=self.owner.connection.execute('SELECT detail FROM entities WHERE id=?',(entity,)).fetchone()
                self.owner.old_entity_details[entity]=old[0] if old else None
            previous=self.owner.old_entity_details[entity]
            if previous is not None:
                old=json.loads(previous);new=json.loads(parameters[-1]);merged=merge_entity_detail(old,new)
                if merged!=new:parameters=(*parameters[:-1],json.dumps(merged,ensure_ascii=False))
        if normalized == 'BEGIN IMMEDIATE':
            return self.owner._begin(sql, parameters)
        if normalized == 'DELETE FROM CAPTURE_SLOTS WHERE ID=?':
            if self.owner.preserve_prior_slots:
                self.owner._preserve_slot(parameters[0])
            self.owner.cleanup_ids.update(row[0] for row in self.owner.connection.execute(
                'SELECT path_id FROM path_support WHERE slot_id=?', parameters))
        if normalized.startswith('DELETE FROM PATHS WHERE ROWID IN'):
            return self.owner._cleanup(sql, parameters)
        if normalized == 'PRAGMA FOREIGN_KEY_CHECK':
            db = self.owner.connection
            if not self.owner.active or not db.in_transaction or self.owner.audited_version is None:
                raise ValueError('writer_session_unverified_transaction')
            if db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                raise ValueError('writer_session_foreign_keys_disabled')
            self.owner.reused_check_count += 1
            return db.execute('SELECT NULL WHERE 0')
        return self.owner.connection.execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.owner.connection.executemany(sql, parameters)

    def commit(self):
        self.owner.connection.commit()
        if self.owner.cleanup_pending:
            self.owner.cleanup_baseline_ready = True
        self.owner.active = False

    def rollback(self):
        self.owner.connection.rollback()
        self.owner.active = False

    def close(self):
        # commit_refresh closes its borrowed handle, not the session owner.
        pass


class _EntityCursor:
    """Reuse the importer's existing entity lookup, rather than query twice."""
    def __init__(self,owner,entity,cursor):self.owner=owner;self.entity=entity;self.cursor=cursor
    def fetchone(self):
        row=self.cursor.fetchone();self.owner.old_entity_details[self.entity]=row[1] if row else None
        return row
