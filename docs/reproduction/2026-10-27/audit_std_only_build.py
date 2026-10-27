from pathlib import Path
import datetime,hashlib,json,os,re,subprocess,base64
R=Path('/home/workspace-ycy/compile-performance');D=R/'runtime-scenarios-c03-c05/exclude-std-only-2026-10-27'
case=R/'cases/instrumented-r9-lld22-exclude-std'
expected='-finstrument-functions-exclude-file-list=/usr/include/c++/11/,/usr/include/aarch64-linux-gnu/c++/11/'
result={'excluded_definition_paths':['/usr/include/c++/11/','/usr/include/aarch64-linux-gnu/c++/11/'],'components':{},'libraries':[]}
for component in ('pytorch','torch-npu'):
 stem=case.name+'-'+component
 build=json.loads((R/'results'/(stem+'.json')).read_text())
 assert build['status']=='success'
 started=datetime.datetime.fromisoformat(build['started_at']).timestamp()
 calls=[json.loads(line) for line in (R/'logs'/(stem+'.compiler.jsonl')).read_text().splitlines()]
 outputs={};missing=[]
 for c in calls:
  assert '-finstrument-functions' in c['args'] and '-g1' in c['args'] and expected in c['args']
  assert all(x==expected for x in c['args'] if x.startswith('-finstrument-functions-exclude'))
  assert c['compiler'] in ('/usr/bin/gcc','/usr/bin/g++')
  if '-o' in c['args']:
   name=c['args'][c['args'].index('-o')+1];p=Path(name)
   if not p.is_absolute():p=Path(c['cwd'])/p
   if p.exists() and p.is_file():outputs[str(p)]=p.stat().st_mtime
   else:missing.append(str(p))
 stale=[p for p,mtime in outputs.items() if mtime<started-1]
 assert not stale,stale
 result['components'][component]={'build':build,'audited_invocations':len(calls),'existing_compile_outputs':len(outputs),
  'all_existing_outputs_new':True,'missing_temporary_outputs':len(missing),
  'compiler_audit_sha256':hashlib.sha256((R/'logs'/(stem+'.compiler.jsonl')).read_bytes()).hexdigest()}
site=case/'venv311/lib/python3.11/site-packages'
for package in ('torch','torch_npu'):
 for p in sorted((site/package/'lib').glob('*.so')):
  dynamic=subprocess.check_output(['readelf','-d',str(p)],text=True)
  result['libraries'].append({'package':package,'path':str(p),'file':p.name,'bytes':p.stat().st_size,
   'needed':re.findall(r'\(NEEDED\).*?\[(.*?)\]',dynamic)})
result['status']='passed'
p=D/'build-audit.json';p.write_text(json.dumps(result,indent=2))
print('ARTIFACT '+json.dumps({'name':'std-only-build-audit.json','data':base64.b64encode(p.read_bytes()).decode()}))
print(json.dumps({c:{k:v for k,v in r.items() if k!='build'} for c,r in result['components'].items()},indent=2))
