import pathlib,shutil,subprocess,json,os,importlib.metadata as md,re,hashlib
root=pathlib.Path('/home/workspace-ycy/compile-performance')
src=pathlib.Path('/home/workspace-ycy/npu-test-infra/venv/test')
dst=root/'venv311'
assert not dst.exists(), 'venv311 already exists'
norm=lambda s:re.sub(r'[-_.]+','-',s).lower()
excluded={'torch','torch-npu'}
site=src/'lib/python3.11/site-packages'
distributions=list(md.distributions(path=[str(site)]))
omit=set()
for dist in distributions:
 if norm(dist.metadata['Name']) in excluded:
  for f in dist.files or []:
   p=pathlib.Path(os.path.abspath(site/str(f)))
   assert p.is_relative_to(src),str(p)
   if p.is_relative_to(site):omit.add(site/p.relative_to(site).parts[0])
   else:omit.add(p)
assert site/'torch' in omit and site/'torch_npu' in omit
for dist in distributions:
 if norm(dist.metadata['Name']) not in excluded:
  for f in dist.files or []:
   p=pathlib.Path(os.path.abspath(site/str(f)))
   assert not any(p==x or p.is_relative_to(x) for x in omit), 'Shared package path: '+str(p)
def ignore(directory,names):
 return [n for n in names if pathlib.Path(directory)/n in omit or n=='__pycache__']
shutil.copytree(src,dst,symlinks=True,ignore=ignore)
for path in (dst/'bin').iterdir():
 if path.is_symlink():
  target=os.readlink(path)
  if target.startswith(str(src)):
   path.unlink();path.symlink_to(target.replace(str(src),str(dst),1))
 elif path.is_file():
  raw=path.read_bytes()
  if b'\0' not in raw[:4096]:
   replaced=raw.replace(str(src).encode(),str(dst).encode())
   if replaced!=raw:path.write_bytes(replaced)
cfg=dst/'pyvenv.cfg';cfg.write_text(cfg.read_text().replace(str(src),str(dst)))
query="import importlib.metadata as m,json;print(json.dumps(sorted([(d.metadata['Name'],d.version,str(d._path)) for d in m.distributions()])))"
def inventory(p):return json.loads(subprocess.check_output([str(p/'bin/python'),'-c',query],text=True))
before=inventory(src);after=inventory(dst)
expected={(norm(n),v) for n,v,p in before if norm(n) not in excluded}
actual={(norm(n),v) for n,v,p in after}
assert expected==actual, {'missing':sorted(expected-actual),'extra':sorted(actual-expected)}
probe=subprocess.check_output([str(dst/'bin/python'),'-c',"import sys,importlib.util,json;print(json.dumps({'prefix':sys.prefix,'torch':str(importlib.util.find_spec('torch')),'torch_npu':str(importlib.util.find_spec('torch_npu'))}))"],text=True)
assert json.loads(probe)['torch']=='None' and json.loads(probe)['torch_npu']=='None'
report={'source':str(src),'destination':str(dst),'excluded':sorted(excluded),'source_effective_packages':before,'destination_effective_packages':after,'versions_equal_except_excluded':True,'probe':json.loads(probe),'environment':{k:v for k,v in os.environ.items() if k.startswith(('ASCEND','CANN')) or k in ('PYTHONPATH','LD_LIBRARY_PATH')}}
(root/'environment/venv311-manifest.json').write_text(json.dumps(report,indent=2))
(root/'environment/requirements-without-torch.txt').write_text('\n'.join(n+'=='+v for n,v in sorted(expected))+'\n')
print(json.dumps({'venv':str(dst),'matching_package_count':len(actual),'probe':json.loads(probe)}),flush=True)
