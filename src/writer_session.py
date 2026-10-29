"""Reuse a verified SQLite connection without rescanning unchanged history.

The original importer and its handoff policy remain byte-for-byte unchanged.
Every write still runs with SQLite foreign keys enabled. Under BEGIN IMMEDIATE,
audit the existing database once, or again whenever another connection committed.
Immediate constraints (and commit-time deferred constraints) preserve that
invariant for our own transactions. No entities, paths or evidence are filtered.
"""
from pathlib import Path
import time
import types

from . import database


class WriterSession:
    def __init__(self, path, cache_mib=4096):
        self.path = Path(path).resolve()
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
        self.cleanup_pending = False
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

    def commit_plan(self, plan):
        if self.closed:
            raise ValueError('writer_session_closed')
        self.cleanup_ids = {row[0] for row in plan.get('paths', [])}
        return self._import(self.path, plan, cache_mib=self.cache_mib)

    def commit_file(self, target):
        from .staged import load_file
        from .native import save
        plan, receipt = load_file(target)
        report = self.commit_plan(plan)
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
        if normalized == 'BEGIN IMMEDIATE':
            return self.owner._begin(sql, parameters)
        if normalized == 'DELETE FROM CAPTURE_SLOTS WHERE ID=?':
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
