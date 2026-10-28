"""Runtime identity catalog of loaded public bindings; no source-name guessing."""
import inspect
import hashlib
import json
from pathlib import Path
import sys
import types


class Registry:
    def __init__(self, namespaces=('torch', 'torch_npu'),catalog_path=None):
        from .frame_identity import adapter,native_definition
        self.frame_function=adapter();self.native_definition=native_definition
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
                        method_owner=value
                        if type(descriptor) is staticmethod:descriptor=descriptor.__func__;method_owner=None
                        elif type(descriptor) is classmethod:descriptor=descriptor.__func__
                        self.add(path+'.'+member,descriptor,owner=method_owner)
                        if member=='__init__' and self.allowed is not None and 'runtime:'+path in self.allowed:
                            self.add(path,descriptor,owner=value)
        torch=sys.modules.get('torch')
        compile_fn=vars(torch).get('compile') if torch is not None else None
        if type(compile_fn) is types.FunctionType:
            self.excluded_bodies[compile_fn]='python:torch.compile'

    def public_module(self,name):
        return (any(name==n or name.startswith(n+'.') for n in self.namespaces)
                and not any(p.startswith('_') for p in name.split('.'))
                and not name.startswith('torch.testing'))

    def add(self,path,value,owner=None):
        api='runtime:'+path
        if self.allowed is not None and api not in self.allowed:return
        if type(value) is types.FunctionType:
            self.python.setdefault(value,[]).append((api,owner))
        elif type(value) in (types.BuiltinFunctionType,types.BuiltinMethodType):
            self.native.setdefault(id(value),[]).append((api,owner));self.keep.append(value)
        elif type(value) in (types.MethodDescriptorType,types.WrapperDescriptorType):
            self.descriptors.setdefault(self.native_definition(value),[]).append((api,owner));self.keep.append(value)
        else:return
        self.catalog.add(api)

    @staticmethod
    def compatible(owner,receiver):
        if owner is None:return True
        classes=[type(receiver)]
        if issubclass(type(receiver),type):classes.append(receiver)
        return any(any(base is owner for base in type.__dict__['__mro__'].__get__(cls,type(cls))) for cls in classes)

    def match(self,frame,event,arg):
        if event=='call':
            candidates=self.python.get(self.frame_function(frame),[])
            code=frame.f_code;receiver=None
            if code.co_argcount:receiver=frame.f_locals.get(code.co_varnames[0])
            elif code.co_flags & inspect.CO_VARARGS:
                args=frame.f_locals.get(code.co_varnames[code.co_kwonlyargcount])
                if type(args) is tuple and args:receiver=args[0]
            return sorted({api for api,owner in candidates if self.compatible(owner,receiver)})
        receiver=getattr(arg,'__self__',None)
        candidates=list(self.native.get(id(arg),()))
        definition=self.native_definition(arg)
        if definition is not None:candidates.extend(self.descriptors.get(definition,()))
        return sorted({api for api,owner in candidates if self.compatible(owner,receiver)})

    def excluded(self,frame):
        found=self.excluded_bodies.get(self.frame_function(frame))
        if found:return found
        module=frame.f_globals.get('__name__','')
        # Compiled wrappers created before capture live in these subsystems.
        # This is an explicit broad scope exclusion, not an inferred call edge.
        if any(module==prefix or module.startswith(prefix+'.') for prefix in ('torch._dynamo','torch._inductor')):
            return 'python:torch.compile_runtime'
        return None

    def exclude_returned(self,value):
        if type(value) is types.FunctionType:
            self.excluded_bodies[value]='python:torch.compile_runtime'

    def describe(self):
        return {'schema':'runtime-api-catalog/1','namespaces':self.namespaces,
                'shared_ledger':self.ledger,
                'ledger_python_entries_not_resolved':sorted(self.allowed-self.catalog) if self.allowed is not None else [],
                'api_ids':sorted(self.catalog),
                'python_identity':'exact executing function identity; compatible receiver type for methods',
                'native_identity':'exact callable/method-definition identity and nominal receiver ownership; saved methods retain their original definition',
                'alias_policy':'preserve all matching catalog paths; canonical id is lexicographically first',
                'scope':'loaded public module bindings and public class methods; not a full external API ledger',
                'exclusion':'torch.compile body, its returned callable bodies, torch._dynamo and torch._inductor subtrees'}
