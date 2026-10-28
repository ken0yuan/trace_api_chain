"""Exact CPython function identity; shared code objects are not API identity."""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import tempfile
import threading

_function=None
_flat_signature=None
_graph_signature=None
_native_definition=None
_member_storage=None
_type_context=None
_lock=threading.Lock()


def adapter():
    global _function,_flat_signature,_graph_signature,_native_definition,_member_storage,_type_context
    if _function is not None:return _function
    with _lock:
        if _function is not None:return _function
        if sys.implementation.name!='cpython' or sys.version_info[:2]!=(3,11):
            raise RuntimeError('exact API function identity requires CPython 3.11')
        source=Path(__file__).with_suffix('.c')
        key=hashlib.sha256(source.read_bytes()+str(sysconfig.get_config_var('SOABI')).encode()).hexdigest()
        root=Path(tempfile.gettempdir())/('callchain-identity-'+str(os.getuid()))
        root.mkdir(mode=0o700,exist_ok=True)
        if root.is_symlink() or root.stat().st_uid!=os.getuid():
            raise RuntimeError('unsafe function identity cache')
        target=root/(key+sysconfig.get_config_var('EXT_SUFFIX'))
        import fcntl
        with (root/'build.lock').open('w') as guard:
            fcntl.flock(guard,fcntl.LOCK_EX)
            if not target.exists():
                temporary=target.with_name(target.name+'.'+str(os.getpid())+'.tmp')
                command=['gcc','-shared','-fPIC','-O2','-fno-instrument-functions',
                         '-I'+sysconfig.get_path('include'),str(source),'-o',str(temporary)]
                result=subprocess.run(command,capture_output=True,text=True,timeout=60)
                if result.returncode:
                    raise RuntimeError('frame identity adapter build failed: '+result.stderr[-2000:])
                temporary.replace(target)
        spec=importlib.util.spec_from_file_location('_callchain_frame_identity',target)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        class Probe:pass
        module.configure_dict_getter(vars(Probe)['__dict__'])
        import ast
        module.configure_dict_getter(vars(ast.AST)['__dict__'])
        import functools,types
        module.configure_dict_getter(vars(functools.partial)['__dict__'])
        descriptor=vars(types.SimpleNamespace).get('__dict__')
        if type(descriptor) is types.GetSetDescriptorType:module.configure_dict_getter(descriptor)
        _graph_signature=module.graph_signature
        _native_definition=module.native_definition
        _member_storage=module.member_storage
        _type_context=module.type_metadata_context
        _function=module.frame_function
        _flat_signature=module.flat_sequence_signature
        return _function


def flat_sequence_signature(value):
    if _flat_signature is None:adapter()
    return _flat_signature(value)


def graph_signature(values,budget,boundaries):
    if _graph_signature is None:adapter()
    return _graph_signature(values,budget,boundaries)


def native_definition(value):
    module=sys.modules.get('_relation_identity')
    if module is not None:return module.method_definition(value)
    if _native_definition is None:adapter()
    return _native_definition(value)


def member_storage(value,descriptor):
    if _member_storage is None:adapter()
    return _member_storage(value,descriptor)


def type_metadata_context(value):
    adapter()
    return _type_context(value)
