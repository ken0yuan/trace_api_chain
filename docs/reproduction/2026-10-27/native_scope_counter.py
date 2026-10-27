import ctypes
import json
import os
from pathlib import Path
import subprocess
import torch

torch.set_num_threads(1)
lib=ctypes.CDLL(None)
for name in ('fill_diag_entries','fill_diag_exits','fill_diag_dropped','fill_diag_fn','fill_diag_count'):
    getattr(lib,name).restype=ctypes.c_uint64
class DlInfo(ctypes.Structure):
    _fields_=[('filename',ctypes.c_char_p),('base',ctypes.c_void_p),('name',ctypes.c_char_p),('address',ctypes.c_void_p)]
lib.dladdr.argtypes=[ctypes.c_void_p,ctypes.POINTER(DlInfo)]

def capture(name,size,fn):
    for _ in range(2):fn()
    lib.fill_diag_begin()
    out=fn()
    lib.fill_diag_end()
    entries=lib.fill_diag_entries();exits=lib.fill_diag_exits()
    counts=[]
    for i in range(65536):
        count=lib.fill_diag_count(i)
        if count:counts.append((count,lib.fill_diag_fn(i)))
    assert entries==exits,(entries,exits)
    assert lib.fill_diag_dropped()==0
    assert sum(c for c,_ in counts)==entries
    top=[]
    for count,address in sorted(counts,reverse=True):
        info=DlInfo();lib.dladdr(address,ctypes.byref(info))
        top.append({'count':count,'library':info.filename.decode() if info.filename else None,
            'offset':hex(address-(info.base or 0)), 'dynamic_symbol':info.name.decode() if info.name else None})
    return {'operation':name,'numel':size,'entries':entries,'exits':exits,
        'unique_functions':len(counts),'dropped':0,'top':top}

rows=[]
for size in (2,10000):
    x=torch.full((size,),100.,dtype=torch.float32)
    y=torch.full((size,),100.,dtype=torch.float32)
    values=[100]*size
    for name,fn in [('fill_',lambda:x.fill_(100)),('tensor_from_list',lambda:torch.tensor(values,dtype=torch.float32)),
        ('isclose',lambda:torch.isclose(x,y,rtol=1.3e-6,atol=1e-5,equal_nan=True))]:
        rows.append(capture(name,size,fn))
Path(__file__).with_name('native-hook-counts-raw.json').write_text(json.dumps({'rows':rows},indent=2))
by_lib={}
for row in rows:
    for f in row['top']:
        if f['library']:by_lib.setdefault(f['library'],{})[f['offset']]=None
for library, offsets in by_lib.items():
    values=list(offsets)
    root=Path('/home/workspace-ycy/compile-performance/system-tools/root')
    env=os.environ.copy()
    env['LD_LIBRARY_PATH']=str(root/'usr/lib/aarch64-linux-gnu')+':'+env.get('LD_LIBRARY_PATH','')
    p=subprocess.run([str(root/'usr/bin/eu-addr2line'),'-f','-C','-e',library,*values],env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90)
    lines=p.stdout.splitlines()
    if p.returncode==0 and len(lines)==2*len(values):
        for i,off in enumerate(values):offsets[off]={'function':lines[i*2],'source':lines[i*2+1]}
for row in rows:
    for f in row['top']:
        f['resolved']=by_lib.get(f['library'],{}).get(f['offset'])
for row in rows:
    scopes={k:0 for k in ('official_cpp_headers','pybind11','python_headers','pytorch','other_third_party','other','unresolved')}
    for f in row['top']:
        source=(f.get('resolved') or {}).get('source','')
        if source.startswith(('/usr/include/c++/11/','/usr/include/aarch64-linux-gnu/c++/11/')):kind='official_cpp_headers'
        elif '/pybind11/' in source:kind='pybind11'
        elif '/include/python3.11/' in source:kind='python_headers'
        elif '/pytorch/third_party/' in source:kind='other_third_party'
        elif '/pytorch/' in source or '/torch/include/' in source:kind='pytorch'
        elif not source or source.startswith('??'):kind='unresolved'
        else:kind='other'
        scopes[kind]+=f['count']
    row['scope_counts']=scopes
    row['functions']=row['top']
    row['top']=row['functions'][:35]
result={'scope':'Separate counter diagnostic, NOT timing data. One CPU float32 operation per row, after warmup; no raw events are stored.',
        'isclose_options':{'rtol':1.3e-6,'atol':1e-5,'equal_nan':True},'rows':rows}
Path(__file__).with_name('native-hook-counts.json').write_text(json.dumps(result,indent=2))
print(json.dumps([{k:v for k,v in r.items() if k not in ('top','functions')} for r in rows],indent=2))
