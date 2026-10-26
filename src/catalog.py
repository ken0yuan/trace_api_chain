"""Runtime identity catalog of loaded public bindings; no source-name guessing."""
import inspect
import hashlib
import json
from pathlib import Path
import sys
import types


class Registry:
    def __init__(self, namespaces=('torch', 'torch_npu'),catalog_path=None):
        self.namespaces=tuple(namespaces)
        self.ledger=None;self.allowed=None
        if catalog_path:
            raw=Path(catalog_path).read_bytes();document=json.loads(raw)
            if not isinstance(document.get('apis'),list):raise ValueError('API ledger must contain apis list')
            apis=document['apis'];ids=[a['api_id'] for a in apis]
            if len(set(ids))!=len(ids):raise ValueError('API ledger contains duplicate api_id')
            self.allowed={'runtime:'+a['qualified_name'] for a in apis if a['language']=='python'}
            self.ledger={'path':str(Path(catalog_path).resolve()),'sha256':hashlib.sha256(raw).hexdigest(),
                         'apis':len(apis),'python':sum(a['language']=='python' for a in apis),
                         'cpp':sum(a['language']=='cpp' for a in apis),
                         'policy':'Every test uses this entire ledger. Resolvable Python identities are entries; all descendants including native functions are captured. Non-callable and unloaded entries are not claimed observed.'}
        self.catalog=set(); self.python={}; self.native={}; self.descriptors={}
        self.excluded_bodies={}; self.keep=[]
        for module_name,module in sorted(list(sys.modules.items())):
            if type(module) is not types.ModuleType or not self.public_module(module_name):continue
            for name,value in list(vars(module).items()):
                if name.startswith('_'):continue
                path=module_name+'.'+name
                self.add(path,value)
                if issubclass(type(value),type):
                    members={}
                    for base in reversed(type.__dict__['__mro__'].__get__(value,type(value))):
                        members.update(type.__dict__['__dict__'].__get__(base,type(base)))
                    for member,descriptor in members.items():
                        if member.startswith('_') and member not in ('__init__','__call__'):continue
                        if type(descriptor) in (staticmethod,classmethod):descriptor=descriptor.__func__
                        self.add(path+'.'+member,descriptor,owner=value)
                        if member=='__init__' and self.allowed is not None and 'runtime:'+path in self.allowed:
                            self.add(path,descriptor,owner=value)
        torch=sys.modules.get('torch')
        compile_fn=vars(torch).get('compile') if torch is not None else None
        if type(compile_fn) is types.FunctionType:
            self.excluded_bodies[(compile_fn.__code__,id(compile_fn.__globals__))]='python:torch.compile'

    def public_module(self,name):
        return (any(name==n or name.startswith(n+'.') for n in self.namespaces)
                and not any(p.startswith('_') for p in name.split('.'))
                and not name.startswith('torch.testing'))

    def add(self,path,value,owner=None):
        api='runtime:'+path
        if self.allowed is not None and api not in self.allowed:return
        if type(value) is types.FunctionType:
            self.python.setdefault((value.__code__,id(value.__globals__)),[]).append((api,owner))
        elif type(value) in (types.BuiltinFunctionType,types.BuiltinMethodType):
            self.native.setdefault(id(value),set()).add(api);self.keep.append(value)
        elif type(value) in (types.MethodDescriptorType,types.WrapperDescriptorType):
            self.descriptors.setdefault(id(value),set()).add(api);self.keep.append(value)
        else:return
        self.catalog.add(api)

    def match(self,frame,event,arg):
        if event=='call':
            candidates=self.python.get((frame.f_code,id(frame.f_globals)),[])
            owner=type(frame.f_locals.get('self'))
            matching=[(api,cls) for api,cls in candidates if cls is None or issubclass(owner,cls)]
            return sorted({api for api,cls in matching})
        aliases=set(self.native.get(id(arg),()))
        receiver=getattr(arg,'__self__',None)
        if receiver is not None:
            descriptor=inspect.getattr_static(type(receiver),getattr(arg,'__name__',''),None)
            aliases.update(self.descriptors.get(id(descriptor),()))
        return sorted(aliases)

    def excluded(self,frame):
        found=self.excluded_bodies.get((frame.f_code,id(frame.f_globals)))
        if found:return found
        module=frame.f_globals.get('__name__','')
        # Compiled wrappers created before capture live in these subsystems.
        # This is an explicit broad scope exclusion, not an inferred call edge.
        if any(module==prefix or module.startswith(prefix+'.') for prefix in ('torch._dynamo','torch._inductor')):
            return 'python:torch.compile_runtime'
        return None

    def exclude_returned(self,value):
        if type(value) is types.FunctionType:
            self.excluded_bodies[(value.__code__,id(value.__globals__))]='python:torch.compile_runtime'

    def describe(self):
        return {'schema':'runtime-api-catalog/1','namespaces':self.namespaces,
                'shared_ledger':self.ledger,
                'ledger_python_entries_not_resolved':sorted(self.allowed-self.catalog) if self.allowed is not None else [],
                'api_ids':sorted(self.catalog),
                'python_identity':'code object and globals identity; compatible receiver type for methods',
                'native_identity':'callable identity or receiver type static descriptor identity',
                'alias_policy':'preserve all matching catalog paths; canonical id is lexicographically first',
                'scope':'loaded public module bindings and public class methods; not a full external API ledger',
                'exclusion':'torch.compile body, its returned callable bodies, torch._dynamo and torch._inductor subtrees'}
