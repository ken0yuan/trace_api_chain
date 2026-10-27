"""Compiler hook loading, explicit Python windows, and raw address decoding."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess


def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def digest(path):
    with open(path,'rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

class DlInfo(ctypes.Structure):
    _fields_=[('filename',ctypes.c_char_p),('base',ctypes.c_void_p),('symbol',ctypes.c_char_p),('address',ctypes.c_void_p)]
class LinkMap(ctypes.Structure):
    _fields_=[('bias',ctypes.c_void_p)]

def native_identity(callable):
    try:
        get=ctypes.pythonapi.PyCFunction_GetFunction;get.argtypes=[ctypes.py_object];get.restype=ctypes.c_void_p
        pc=get(callable);lib=ctypes.CDLL(None);info=DlInfo();link=ctypes.c_void_p()
        lib.dladdr1.argtypes=[ctypes.c_void_p,ctypes.POINTER(DlInfo),ctypes.POINTER(ctypes.c_void_p),ctypes.c_int]
        if not lib.dladdr1(pc,ctypes.byref(info),ctypes.byref(link),2):return {'status':'native_address_unresolved'}
        path=os.fsdecode(info.filename);bias=ctypes.cast(link,ctypes.POINTER(LinkMap)).contents.bias or 0
        stat=os.stat(path)
        return {'module':path,'module_offset':pc-bias,'load_bias':bias,'func_ptr':pc,
                'module_stamp':[stat.st_size,stat.st_mtime_ns]}
    except Exception as e:return {'status':'native_address_unresolved','reason':type(e).__name__}

def build_hook(output):
    source=Path(__file__).with_name('hooks.c')
    target=output/'compiler-hooks.so'
    proc=subprocess.run(['gcc','-shared','-fPIC','-O2','-fno-instrument-functions',str(source),'-o',str(target),'-ldl'],capture_output=True,text=True)
    (output/'hook-build.log').write_text(proc.stdout+proc.stderr)
    if proc.returncode:raise RuntimeError('compiler_hook_build_failed; see hook-build.log')
    return target

class Bridge:
    def __init__(self,output,storage='events'):
        self.output=Path(output);self.stack=[];self.active=None;self.segments=[];self.gaps=[]
        self.storage=storage
        self.contexts={}
        self.hook=ctypes.CDLL(str(self.output/'compiler-hooks.so'))
        self.hook.trace_index_configure.argtypes=[ctypes.c_char_p]
        self.hook.trace_index_configure.restype=ctypes.c_int
        self.hook.trace_index_stop.restype=ctypes.c_int
        self.hook.trace_index_event_count.restype=ctypes.c_size_t
        if self.hook.trace_index_interposed()!=1:raise RuntimeError('compiler_hook_not_interposed')
        if storage=='paths':
            self.hook.trace_paths_begin.argtypes=[ctypes.c_uint64]
            self.hook.trace_paths_event_count.restype=ctypes.c_uint64
            self.hook.trace_paths_exit_callsite_variations.restype=ctypes.c_uint64
            self.hook.trace_paths_finish.argtypes=[ctypes.c_char_p]
        self.maps_before=Path('/proc/self/maps').read_text()

    def pause(self):
        if self.active is not None:
            if self.storage=='paths':
                status=self.hook.trace_paths_pause()
                self.active['events']=self.hook.trace_paths_event_count()-self.active.pop('start_events')
            else:
                status=self.hook.trace_index_stop()
                self.active['events']=self.hook.trace_index_event_count()
            self.active['stop_status']=status
            if status:self.gaps.append('overflow_or_write_failure:'+str(self.active['file'] or self.active['ordinal']))
            self.active=None

    def resume_current(self):
        if self.stack and self.active is None:
            name='segment-'+str(len(self.segments)).zfill(6)+'.bin'
            segment={'uid':self.stack[-1],'file':name,'ordinal':len(self.segments)}
            if self.storage=='paths':
                segment['file']=None;segment['start_events']=self.hook.trace_paths_event_count()
                segment['context']=self.contexts[self.stack[-1]]
                result=self.hook.trace_paths_begin(int(segment['context']))
            else:result=self.hook.trace_index_configure(os.fsencode(self.output/name))
            if result:
                raise RuntimeError('native_window_open_failed')
            self.active=segment;self.segments.append(segment)

    def begin(self,uid,identity,context=None):
        self.contexts[uid]=context if context is not None else uid
        self.pause();self.stack.append(uid);self.resume_current()

    def end(self,uid):
        self.pause()
        if not self.stack or self.stack[-1]!=uid:
            self.gaps.append('window_return_mismatch');return
        self.stack.pop();self.resume_current()

    def close(self):
        self.pause()
        if self.stack:self.gaps.append('unfinished_native_windows')
        if self.storage=='paths' and self.hook.trace_paths_finish(os.fsencode(self.output/'native-paths.bin')):
            self.gaps.append('native_path_capture_incomplete')
        after=Path('/proc/self/maps').read_text()
        (self.output/'maps-before.txt').write_text(self.maps_before)
        (self.output/'maps.txt').write_text(after)
        stamps={}
        for line in after.splitlines():
            parts=line.split(None,5)
            if len(parts)==6 and parts[5].startswith('/'):
                try:
                    stat=os.stat(parts[5]);stamps[parts[5]]=[stat.st_size,stat.st_mtime_ns,stat.st_ino]
                except OSError:pass
        summary={'segments':self.segments,'gaps':self.gaps,'module_stamps':stamps,'native_storage':self.storage}
        if self.storage=='paths':summary['exit_callsite_variations']=self.hook.trace_paths_exit_callsite_variations()
        save(self.output/'windows.json',summary)

class Modules:
    def __init__(self,maps,stamps):
        self.rows=[];self.headers={};self.stamps=stamps;self.identities={};self.gaps=set()
        self.address_cache={}
        for line in maps.splitlines():
            p=line.split(None,5)
            if len(p)!=6 or not p[5].startswith('/'):continue
            lo,hi=map(lambda x:int(x,16),p[0].split('-'))
            self.rows.append((lo,hi,int(p[2],16),p[5]))

    def identify(self,pc):
        # Mappings and module stamps are one immutable capture snapshot.
        if pc in self.address_cache:return self.address_cache[pc]
        rows=[r for r in self.rows if r[0]<=pc<r[1]]
        if len(rows)!=1:
            self.gaps.add('address_module_unresolved');return {'path':None,'offset':None,'status':'unresolved'}
        lo,hi,file_offset,path=rows[0]
        if path not in self.headers:
            try:
                stat=os.stat(path)
                if [stat.st_size,stat.st_mtime_ns,stat.st_ino]!=self.stamps.get(path):raise ValueError('binary_changed_since_capture')
                with open(path,'rb') as f:
                    h=f.read(64)
                    if h[:6]!=b'\x7fELF\x02\x01':raise ValueError('unsupported_ELF')
                    kind=struct.unpack_from('<H',h,16)[0]
                    phoff=struct.unpack_from('<Q',h,32)[0]
                    size,count=struct.unpack_from('<HH',h,54)
                    segments=[]
                    for i in range(count):
                        f.seek(phoff+i*size);ph=f.read(size)
                        typ,flags,offset,vaddr,_,filesz,memsz,_=struct.unpack_from('<IIQQQQQQ',ph)
                        if typ==1 and flags&1:segments.append((offset,vaddr,filesz))
                self.headers[path]=(kind,segments,digest(path))
            except (OSError,ValueError,struct.error) as e:
                self.headers[path]=None;self.gaps.add(str(e))
        header=self.headers[path]
        if header is None:return {'path':path,'offset':None,'status':'unresolved'}
        kind,segments,sha=header
        page=os.sysconf('SC_PAGE_SIZE')
        biases={lo-(vaddr+file_offset-offset) for offset,vaddr,filesz in segments if offset//page*page<=file_offset<((offset+filesz+page-1)//page)*page}
        if kind==2:biases={0}
        if len(biases)!=1:
            self.gaps.add('ELF_load_bias_ambiguous');return {'path':path,'offset':None,'status':'unresolved'}
        result={'path':path,'offset':pc-biases.pop(),'sha256':sha}
        self.identities[path,result['offset']]=result
        self.address_cache[pc]=result
        return result

def decode(output):
    windows=json.loads((output/'windows.json').read_text())
    modules=Modules((output/'maps.txt').read_text(),windows['module_stamps'])
    records=[];sequence=0
    for ordinal,s in enumerate(windows['segments']):
        if s['ordinal']!=ordinal:raise ValueError('native_segment_order')
        raw=(output/s['file']).read_bytes()
        if raw[:8]!=b'CCADDR01' or (len(raw)-8)%40:raise ValueError('native_truncated_record')
        if (len(raw)-8)//40!=s['events']:raise ValueError('native_receipt_count_mismatch')
        if s['stop_status']:raise ValueError('native_capture_loss:'+s['file'])
        for ns,fn,caller,pid,tid,kind,seq in struct.iter_unpack('<QQQIIII',raw[8:]):
            if seq!=sequence:raise ValueError('native_sequence_gap')
            sequence+=1
            if kind not in (1,2):raise ValueError('native_unknown_event')
            target=modules.identify(fn);callsite=modules.identify(max(0,caller-1))
            records.append({'time':ns,'pid':pid,'tid':tid,'event':'enter' if kind==1 else 'exit',
                            'absolute_function':fn,'absolute_caller':caller,'py_uid':s['uid'],
                            'path':target['path'],'offset':target['offset'],'source_identity':target,
                            'caller_identity':callsite})
    return records,modules,list(windows['gaps'])
