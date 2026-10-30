from pathlib import Path
import hashlib,json,os,time,shutil
S=Path('/home/workspace-ycy/compile-performance/timeout-tbe-writer-repair-2026-10-29');O=S/'verified-writer-duplicate-cleanup-v1';O.mkdir();keep=S/'writer-real-benchmark/reference.sqlite';copies=[S/'writer-real-benchmark/session.sqlite',S/'writer-real-benchmark-v4/session.sqlite'];expected='ea0978b1b1573b2bfa63154ea12a7b4c91a860214ea8e524a6abc225c76e60ee'
assert json.loads((keep.parent/'validation.json').read_text())['entire_database_bytes_equal']
assert json.loads((copies[1].parent/'validation.json').read_text())['entire_40GB_database_bytes_equal']
def ensure_unopened():
 names={str(p.resolve()) for p in [keep,*copies]}
 for proc in Path('/proc').iterdir():
  if not proc.name.isdigit():continue
  try:
   for fd in (proc/'fd').iterdir():
    try:assert os.readlink(fd) not in names,'A comparison database is in use'
    except OSError:pass
  except (FileNotFoundError,PermissionError):pass
ensure_unopened()
for p in [keep,*copies]:
 assert p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(S.resolve())
 assert not any(Path(str(p)+suffix).exists() for suffix in ['-wal','-journal'])
rows=[]
for p in [keep,*copies]:
 with p.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
 assert sha==expected,(p,sha)
 rows.append({'path':str(p),'bytes':p.stat().st_size,'sha256':sha,'removed':False})
ensure_unopened();receipt={'at':time.time(),'retained_complete_reference_database':str(keep),'sha256':expected,'files':rows,'unique_formal_or_failed_evidence_deleted':False,'state':'verified_pending_cleanup'};(O/'validation.json').write_text(json.dumps(receipt,indent=2))
for row,p in zip(rows[1:],copies):
 assert p.parent in [S/'writer-real-benchmark',S/'writer-real-benchmark-v4'];p.unlink();row['removed']=True
receipt.update(state='cleaned',removed_duplicate_bytes=sum(r['bytes'] for r in rows if r['removed']),free_disk_gib=shutil.disk_usage(S).free/1024**3);(O/'validation.json').write_text(json.dumps(receipt,indent=2));print(json.dumps(receipt),flush=True)
