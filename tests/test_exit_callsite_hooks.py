"""Exercise the real C hook stack, including optimized call-site variation."""
import json
import os
import struct
import subprocess
import sys

import pytest


@pytest.mark.parametrize('events,expected_flags,variations',[
    ([['begin',1],['enter',100,300],['exit',100,400]],0,1),
    # Recursive positions remain separate even when both report one source function.
    ([['begin',1],['enter',100,300],['enter',100,300],['exit',100,400],['exit',100,500]],0,2),
    ([['begin',1],['enter',100,300],['enter',200,300],['exit',100,400]],32|64,0),
    ([['begin',1],['enter',100,300],['begin',2],['exit',100,400]],32|64,0),
    ([['begin',1],['exit',100,400]],16,0),
    ([['begin',1],['enter',100,300]],64,0),
])
def test_hook_exit_pairing(tmp_path,events,expected_flags,variations):
    from trace_api_chain.native import build_hook
    hook=build_hook(tmp_path);out=tmp_path/'paths.bin'
    script='''import ctypes,json,sys
h=ctypes.CDLL(sys.argv[1]);h.trace_paths_begin.argtypes=[ctypes.c_uint64]
h.trace_paths_finish.argtypes=[ctypes.c_char_p]
h.trace_paths_exit_callsite_variations.restype=ctypes.c_uint64
for name in ('__cyg_profile_func_enter','__cyg_profile_func_exit'):
 getattr(h,name).argtypes=[ctypes.c_void_p,ctypes.c_void_p]
for event in json.loads(sys.argv[3]):
 name={'begin':'trace_paths_begin','enter':'__cyg_profile_func_enter','exit':'__cyg_profile_func_exit'}[event[0]]
 getattr(h,name)(*event[1:])
result=h.trace_paths_finish(sys.argv[2].encode())
print(json.dumps({'result':result,'variations':h.trace_paths_exit_callsite_variations()}))
'''
    env=dict(os.environ,TRACE_API_CHAIN_MAX_PATHS='128',TRACE_API_CHAIN_MAX_EVENTS='128')
    env.pop('LD_PRELOAD',None)
    p=subprocess.run([sys.executable,'-c',script,str(hook),str(out),json.dumps(events)],env=env,capture_output=True,text=True,check=True)
    receipt=json.loads(p.stdout);raw=out.read_bytes();header=struct.unpack_from('<8sQQQIIII',raw)
    assert header[6]==expected_flags
    assert receipt=={'result':-1 if expected_flags else 0,'variations':variations}
    if not expected_flags:
        n,s=header[1:3]
        nodes=list(struct.iter_unpack('<QQII',raw[48:48+n*24]))
        supports=list(struct.iter_unpack('<QQQII',raw[48+n*24:]))
        assert n==sum(e[0]=='enter' for e in events)
        assert [x[2] for x in nodes]==list(range(n))
        assert all(x[1]==300 for x in nodes)  # Preserve entry call sites.
        assert s==n and all(x[1]==x[2]==1 for x in supports)
