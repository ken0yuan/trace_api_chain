"""Collect runtime references and call ownership, without source parsing or GDB.

Reference snapshots are observations, not a census of the Python heap. In
particular, addresses of non-weak-referenceable objects may be reused.
"""
import collections
import dis
import hashlib
import json
import struct
import os
from pathlib import Path
import sys
import threading
import time
import types
import inspect
import traceback
import weakref

_ACTIVE_RECORDERS=weakref.WeakSet()
_FORK_CALLBACK_READY=False
_PROCESS_EXIT=os._exit

def _redirect_inherited_recorders():
    for recorder in list(_ACTIVE_RECORDERS):
        if recorder.started and not recorder.closed:recorder.redirect_fork_fragment()

from . import __version__
from .reference_codec import edge_order
from .reference_state import ReferencePublisher


def test_body_code(item):
    """Resolve the actual selected test through wrappers; never arm a policy wrapper."""
    target=os.path.realpath(str(item.path))
    function=inspect.unwrap(item.obj)
    if type(function) is types.MethodType:function=function.__func__
    if type(function) is types.FunctionType and os.path.realpath(function.__code__.co_filename)==target:
        return function.__code__
    # Some parametrization adapters use closures without __wrapped__. Accept
    # only a unique source-test code object, not a name/line heuristic.
    queue=[function];seen=set();codes=set()
    while queue:
        function=queue.pop()
        if type(function) is types.MethodType:function=function.__func__
        if type(function) is not types.FunctionType or id(function) in seen:continue
        seen.add(id(function))
        if os.path.realpath(function.__code__.co_filename)==target:codes.add(function.__code__)
        wrapped=vars(function).get('__wrapped__')
        if wrapped is not None:queue.append(wrapped)
        for cell in function.__closure__ or ():
            try:queue.append(cell.cell_contents)
            except ValueError:pass
    if len(codes)!=1:raise RuntimeError('selected test body is not uniquely resolvable; refusing wrapper-wide capture')
    return codes.pop()


def class_label(cls):
    # Use type's own descriptors, not user-defined attribute access.
    def field(name):
        try:
            return type.__dict__[name].__get__(cls, type(cls))
        except Exception:
            return None
    return {'module': field('__module__'), 'qualname': field('__qualname__')}


def class_namespace(cls):
    return type.__dict__['__dict__'].__get__(cls, type(cls))


def add_reference(groups,parent,edge):
    """Borrow frozen adjacency without copying it until an edge really changes."""
    edges=groups.get(parent)
    if edges is None:groups[parent]={edge}
    elif type(edges) is frozenset:
        if edge not in edges:
            edges=set(edges);edges.add(edge);groups[parent]=edges
    else:edges.add(edge)


class NativeStorage:
    __slots__=('descriptor','offset','kind','value_type')
    def __init__(self,descriptor,spec):self.descriptor=descriptor;self.offset,self.kind,self.value_type=spec


def instance_fields(value, plans=None, project_storage=False):
    """Only builtin storage descriptors; never invoke a property or repr."""
    result=[]; notes=[]
    cls=type(value)
    try:
        plan=plans.get(cls) if plans is not None else None
        if plan is None:
            mro=type.__dict__['__mro__'].__get__(cls, type(cls))
            seen=set();plan=[]
            for base in mro:
                for name,descriptor in class_namespace(base).items():
                    if name in seen:continue
                    seen.add(name)
                    if (name=='__dict__' and type(descriptor) is types.GetSetDescriptorType) or type(descriptor) in (types.MemberDescriptorType,property):
                        plan.append((name,descriptor))
            if plans is not None:plans[cls]=plan
        for name, descriptor in plan:
                if name == '__dict__' and type(descriptor) is types.GetSetDescriptorType:
                    try:
                        fields=descriptor.__get__(value,cls)
                        if type(fields) is dict:
                            result.extend(('attribute:'+str(k),v) for k,v in fields.copy().items() if type(k) is str)
                    except Exception as exc: notes.append(type(exc).__name__+':__dict__')
                elif type(descriptor) is types.MemberDescriptorType:
                    try:
                        from .frame_identity import member_storage
                        storage=member_storage(value,descriptor) if project_storage else None
                        result.append(('slot:'+name,NativeStorage(descriptor,storage) if storage is not None else descriptor.__get__(value,cls)))
                    except AttributeError: pass
                    except Exception as exc: notes.append(type(exc).__name__+':'+name)
                elif type(descriptor) is property:
                    notes.append('property_not_evaluated:'+name)
    except Exception as exc: notes.append('storage_unavailable:'+type(exc).__name__)
    return result,notes


class Recorder(ReferencePublisher):
    def __init__(self, path, nodeid, test_file, namespaces=('torch','torch_npu'), max_references=100000, run_id=None, native=None, registry=None, observer_roots=(), reference_deltas=False, defer_reference_states=None):
        if registry is None:
            from .catalog import Registry
            registry=Registry(namespaces)
        self.capture_path=Path(path).resolve();self.owner_pid=os.getpid();self.fragment_pid=None
        self.fork_inherited_protected=False
        self.file=open(path,'w',encoding='utf-8',buffering=1024*1024)
        self.nodeid=nodeid; self.test_file=os.path.realpath(test_file)
        self.namespaces=tuple(namespaces); self.max_references=max_references
        self.thread=threading.get_ident(); self.stack=[]; self.next_uid=0
        self.code_ids={}; self.object_metadata={}; self.views={}; self.view_keys={}; self.busy=False
        self.class_sources={};self.object_semantics={};self.global_loads={};self.metadata_cache=collections.OrderedDict();self.metadata_cache_limit=max(100000,max_references)
        self.metadata_cache_hits=0;self.metadata_cache_misses=0;self.metadata_cache_evictions=0
        from .frame_identity import new_type_context_cache
        self.type_context_cache=new_type_context_cache()
        self.metadata_templates=collections.OrderedDict();self.metadata_template_limit=100000;self.metadata_template_hits=0
        self.module_sources={};self.read_sites={};self.module_operands={}
        for name,module in list(sys.modules.items()):
            if type(module) is types.ModuleType:self.module_source(vars(module))
        self.started=False; self.closed=False; self.gaps=set(); self.calls=0; self.roots=0
        self.path_contexts={}
        self.reference_groups=collections.OrderedDict();self.reference_group_bytes=0;self.reference_group_costs={};self.next_reference_group=0
        self.reference_parent_states=collections.OrderedDict();self.reference_parent_costs={};self.reference_parent_bytes=0
        self.reference_storage={'full_groups':0,'delta_groups':0,'logical_edges':0,'written_edges':0}
        self.grouped_references=getattr(native,"storage",None)=="paths"
        self.reference_delta_storage=bool(reference_deltas and self.grouped_references)
        if defer_reference_states is None:defer_reference_states=os.environ.get('CALLCHAIN_DEFER_REFERENCE_STATES')=='1'
        self.reference_transport=None
        if defer_reference_states:
            if not self.grouped_references:raise ValueError('deferred reference states require grouped path storage')
            from .reference_transport import SnapshotTransport
            self.reference_transport=SnapshotTransport(self.emit)
        self.snapshot_cache=collections.OrderedDict();self.snapshot_cache_bytes=0;self.snapshot_cache_costs={};self.snapshot_cache_reference_counts={};self.root_states={};self.snapshot_cache_roots={}
        self.snapshot_root_alias_reuses=0
        self.snapshot_native_root_stubs=0
        self.flat_incremental_updates=0;self.flat_unchanged_leaves_reused=0
        self.snapshot_component_limit=8;self.component_cache_promotions=0;self.component_cache_reuses=0;self.snapshot_cache_limit_bytes=128*1024*1024
        self.body_code=None;self.body_depth=0;self.body_entries=0
        self.native=native; self.registry=registry; self.suppressed=0
        self.protected_subscription=None
        # Exact instrumentation-owned objects only. Retain incoming workload
        # references but never recursively observe our growing evidence store.
        self.observer_roots={id(value):value for value in (self,native,registry,_ACTIVE_RECORDERS,*observer_roots) if value is not None}
        self.emit('start',schema='object_callchain/1',collector_version=__version__,identity_policy='exact-function-and-native-receiver/1',storage_policy='unboxed-members-are-storage-projections/1',reference_policy=('shared-adjacency-states/2' if self.reference_delta_storage else 'shared-adjacency-states/1') if self.grouped_references else 'reference-deltas/1',nodeid=nodeid,test_file=self.test_file,namespaces=self.namespaces,pid=os.getpid(),run_id=run_id,
                  python=sys.version,scope_policy='callchain-contract/1',
                  native_window_policy='python-and-c-boundaries/1' if native is not None else 'c-boundaries/1',
                   api_catalog=sorted(registry.catalog) if registry else [],scope='API subtrees on the selected test thread',
                  boundaries=['native function bodies require native instrumentation',
                              'other threads/processes are not attributed by this collector',
                              'unbound temporaries and opaque extension storage are not enumerated',
                              'address identity is local to observations; reuse is not lifetime proof'],
                  **({'reference_transport':'deferred-adjacency/1'} if self.reference_transport is not None else {}))

    def redirect_fork_fragment(self):
        """Keep inherited child observations separate; never overwrite parent proof."""
        pid=os.getpid()
        if pid==self.owner_pid or pid==self.fragment_pid:return
        parent_path=self.capture_path;parent_pid=self.fragment_pid or self.owner_pid
        output=parent_path.parent/'fork-fragments'/str(pid)
        output.mkdir(parents=True,exist_ok=True)
        if not output.resolve().is_relative_to(parent_path.parent):raise RuntimeError('fork fragment path escapes parent capture')
        target=output/'events.jsonl'
        descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            # The pending bytes are an inherited copy, not new parent evidence.
            # Redirect before close/flush, then line-buffer child-only records so
            # os._exit cannot flush them into or discard the parent's stream.
            os.dup2(descriptor,self.file.fileno());self.file.close()
        finally:os.close(descriptor)
        self.file=open(target,'a',encoding='utf-8',buffering=1)
        self.capture_path=target;self.fragment_pid=pid
        self.fork_inherited_protected=self.protected_subscription is not None
        self.protected_subscription=None
        if self.native is not None:
            self.native.redirect_fork_fragment(output)
        self.gaps.add('fork_continuation_not_validated')
        manifest={'state':'retained_unvalidated_fork_continuation','writer_pid':pid,'parent_writer_pid':parent_pid,
            'selected_owner_pid':self.owner_pid,'parent_evidence':str(parent_path),
            'inherited_uids':[row['uid'] for row in self.stack],
            'inherited_buffers_and_context_retained':True,'parent_outputs_modified':False,
            'scope':'separate raw fragment, never a validated parent or child callchain; other processes remain outside parent calling-thread scope'}
        (output/'fork-fragment.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        self.emit('gap',reason='fork_continuation_not_validated',writer_pid=pid,parent_writer_pid=parent_pid)

    def emit(self, event_kind, **data):
        self.file.write(json.dumps({'event':event_kind,**data},ensure_ascii=False,separators=(',',':'))+'\n')

    def observe(self, phase, action, *args, **kwargs):
        """Observation failures mark a gap; they must not consume a stack event."""
        try:
            return action(*args, **kwargs)
        except Exception as exc:
            self.collector_error(exc, phase)
            return None

    def collector_error(self, exc, phase):
        reason='collector_error:'+type(exc).__name__
        first=reason not in self.gaps
        self.gaps.add(reason)
        if first:
            try:
                self.emit('gap',reason=reason,phase=phase,
                          exception_args=[x if type(x) is str else type(x).__name__ for x in exc.args],
                          traceback=traceback.format_exc())
            except Exception as write_error:
                self.gaps.add('evidence_write_error:'+type(write_error).__name__)

    def module_source(self,namespace):
        name=namespace.get('__name__');filename=namespace.get('__file__')
        if type(name) is not str or type(filename) is not str:return {}
        if name not in self.module_sources or self.module_sources[name]['file']!=filename:
            try:
                stat=os.stat(filename);stamp=[stat.st_size,stat.st_mtime_ns]
            except OSError:stamp=None
            package=namespace.get('__package__')
            self.module_sources[name]={'module':name,'file':filename,'source_stamp':stamp,
                                       'package':package if type(package) is str else None}
        return self.module_sources[name]

    def source_read(self,frame,namespace,name,scope):
        record=self.module_source(namespace)
        if not record:return
        uid=self.stack[-1]['uid'];site=(record['file'],name,scope,frame.f_lasti)
        seen=self.read_sites.setdefault(uid,set())
        if site in seen:return
        seen.add(site)
        self.emit('source_read',uid=uid,namespace=record,name=name,scope=scope,
                  file=frame.f_code.co_filename,line=frame.f_lineno,offset=frame.f_lasti)

    def framework(self, module):
        return (type(module) is str and not module.startswith('torch.testing._internal') and
                any(module==n or module.startswith(n+'.') for n in self.namespaces))

    def code(self, code, module):
        key=id(code)
        if key not in self.code_ids:
            token='code:'+str(len(self.code_ids)+1)
            self.code_ids[key]=(code,token)  # Code objects only, not test instances.
            filename=code.co_filename
            try:
                stat=os.stat(filename); stamp=[stat.st_size,stat.st_mtime_ns]
            except OSError: stamp=None
            self.emit('definition',id=token,kind='function',module=module,
                      qualname=getattr(code,'co_qualname',code.co_name),file=filename,
                      first_line=code.co_firstlineno,source_stamp=stamp)
        return self.code_ids[key][1]

    def metadata(self, value, labels=None):
        cls=type(value)
        from .frame_identity import type_metadata_context
        if labels is None:labels={}
        contexts=labels.setdefault('metadata_contexts',{})
        if cls not in contexts:contexts[cls]=type_metadata_context(cls,self.type_context_cache)
        context=contexts[cls]
        extra=None
        if cls is types.FunctionType:extra=(value.__code__,value.__module__)
        elif cls is types.MethodType:extra=(value.__func__.__code__,value.__func__.__module__)
        elif issubclass(cls,type):extra=type_metadata_context(value,self.type_context_cache)
        elif cls is types.ModuleType:extra=(vars(value).get('__name__'),vars(value).get('__file__'))
        elif cls in (types.BuiltinFunctionType,types.BuiltinMethodType):extra=(value.__name__,value.__module__)
        cacheable=context is not None and not (issubclass(cls,type) and extra is None)
        identity=(id(value),id(cls));key=(context,extra)
        cached=self.metadata_cache.get(identity)
        if cacheable and cached is not None and cached[0]==key:
            self.metadata_cache_hits+=1;self.metadata_cache.move_to_end(identity);return cached[1]
        self.metadata_cache_misses+=1
        template_key=None
        if cacheable and self.metadata_template_limit and cls is not types.ModuleType:
            if cls in (types.FunctionType,types.MethodType):
                if type(extra[1]) in (str,type(None)):template_key=(id(cls),context,id(extra[0]),extra[1])
            elif cls in (types.BuiltinFunctionType,types.BuiltinMethodType):
                if type(extra[0]) is str and type(extra[1]) in (str,type(None)):template_key=(id(cls),context,*extra)
            else:template_key=(id(cls),context,extra)
        template=self.metadata_templates.get(template_key) if template_key is not None else None
        if template is not None:
            semantic,suffix=template
            base='object:'+str(id(value))+':'+str(id(cls));token=base+':'+suffix
            result={'id':token,'address':id(value),'type':semantic['type'],'identity':'observation_address'}
            result.update((name,item) for name,item in semantic.items() if name!='type')
            self.object_semantics[base]=(semantic,token)
            answer=(token,result);self.metadata_template_hits+=1;self.metadata_templates.move_to_end(template_key)
            self.remember_metadata(identity,key,answer)
            return answer
        if labels is None:label=class_label(cls)
        else:
            if cls not in labels:labels[cls]=class_label(cls)
            label=labels[cls]
        token='object:'+str(id(value))+':'+str(id(cls))
        result={'id':token,'address':id(value),'type':label,'identity':'observation_address'}
        if type(value) is types.FunctionType:
            result.update(kind='function',definition=self.code(value.__code__,value.__module__))
        elif type(value) is types.MethodType:
            result.update(kind='bound_method',definition=self.code(value.__func__.__code__,value.__func__.__module__))
        elif issubclass(type(value),type):
            result.update(kind='class',static_owner=class_label(value))
        elif type(value) is types.ModuleType:
            result.update(kind='module',static_owner={'module':vars(value).get('__name__'),'qualname':None})
        elif type(value) in (types.BuiltinFunctionType,types.BuiltinMethodType):
            result.update(kind='native_callable',name=value.__name__,module=value.__module__)
        else:
            result.update(kind='instance',static_owner=label)
        owner=result.get('static_owner') or {}
        module=sys.modules.get(owner.get('module'))
        if type(module) is types.ModuleType:
            result['owner_file']=vars(module).get('__file__')
        # Exported classes can have __module__ rewritten (e.g. torch.Tensor).
        # Their own Python methods retain the actual defining source path.
        owner_class=value if result['kind']=='class' else cls if result['kind']=='instance' else None
        source_context=extra if result['kind']=='class' else context
        class_key=(id(owner_class),owner.get('module'),owner.get('qualname'),source_context) if owner_class is not None else None
        if source_context is not None and class_key in self.class_sources:
            result.update(self.class_sources[class_key])
        elif owner_class is not None and owner.get('module')!='builtins':
            candidates=set()
            hints=[]
            for member in class_namespace(owner_class).values():
                if type(member) in (staticmethod,classmethod):member=member.__func__
                if type(member) is types.FunctionType:
                    candidates.add(member.__code__.co_filename)
                    try:
                        stat=os.stat(member.__code__.co_filename)
                        stamp=[stat.st_size,stat.st_mtime_ns]
                    except OSError:stamp=None
                    hints.append({'file':member.__code__.co_filename,'first_line':member.__code__.co_firstlineno,
                                  'source_stamp':stamp,'evidence':'class_own_method_code_location'})
            if hints:result['class_source_hints']=hints
            if len(candidates)==1:result['owner_file']=next(iter(candidates))
            elif len(candidates)>1:result['owner_candidates']=sorted(candidates)
            if source_context is not None:self.class_sources[class_key]={k:result[k] for k in ('owner_file','owner_candidates','class_source_hints') if k in result}
        # CPython reuses short-lived builtin-method addresses. Never overwrite
        # a previous fill_ object with append merely because the address matches.
        semantic={k:v for k,v in result.items() if k not in ('id','address','identity')}
        previous=self.object_semantics.get(token)
        if previous and previous[0]==semantic:
            result['id']=previous[1]
        else:
            suffix=hashlib.sha256(json.dumps(semantic,sort_keys=True).encode()).hexdigest()[:16]
            result['id']=token+':'+suffix
            self.object_semantics[token]=(semantic,result['id'])
        if template_key is not None:
            self.metadata_templates[template_key]=(semantic,result['id'].rsplit(':',1)[1])
            self.metadata_templates.move_to_end(template_key)
            while len(self.metadata_templates)>self.metadata_template_limit:self.metadata_templates.popitem(last=False)
        token=result['id']
        answer=(token,result)
        if cacheable:self.remember_metadata(identity,key,answer)
        return answer

    def remember_metadata(self,identity,key,answer):
        self.metadata_cache[identity]=(key,answer);self.metadata_cache.move_to_end(identity)
        while len(self.metadata_cache)>self.metadata_cache_limit:
            self.metadata_cache.popitem(last=False);self.metadata_cache_evictions+=1

    def snapshot(self, frame, phase, values=None):
        if not self.stack:return
        if values is None:
            if os.path.realpath(frame.f_code.co_filename)==self.test_file:return
            values=frame.f_locals.copy()
        from .frame_identity import graph_signature,flat_sequence_signature
        roots=[];groups={};notes=set();local={};count=len(values)
        self._snapshot_known_nodes=set();self._snapshot_current_groups=groups;self._snapshot_current_notes=notes
        # Future roots are already held alive by values. Store only their
        # identities/tokens; never retain workload objects across observations.
        self._snapshot_root_targets={(id(value),id(type(value))) for value in values.values()}
        self._snapshot_root_tokens={}
        self._snapshot_stub_nodes=set()
        names=list(values)
        for name,value in values.items():
            identity=(id(value),id(type(value)))
            token=self._snapshot_root_tokens.get(identity)
            if token is None:
                # A validated cached first root can already contain this root
                # without running snapshot_root. Reuse its exact observed token,
                # only when that token is present in this snapshot's node set.
                base='object:'+str(identity[0])+':'+str(identity[1])
                semantic=self.object_semantics.get(base)
                if semantic is not None and semantic[1] in self._snapshot_known_nodes:token=semantic[1]
            if token is not None:
                roots.append((None,'root:'+name,token));self.snapshot_root_alias_reuses+=1
                continue
            chunk=local.get(identity)
            if chunk is None:
                # Exact builtin callables have only their receiver reference.
                # If that receiver is an explicit root, record this edge now
                # and let its own root do the one complete subtree validation.
                chunk=self.snapshot_native_root(value)
                flat=None;signature=None
                if chunk is None:
                    flat=flat_sequence_signature(value)
                    signature=(identity,flat) if flat is not None else graph_signature({'value':value},self.max_references,self.observer_roots,self.type_context_cache)
                    chunk=self.snapshot_cache.get(signature) if signature is not None else None
                if groups and chunk is not None and any(parent in groups and groups[parent]!=edges for parent,edges in chunk[1].items()):
                    chunk=None  # Earlier first-visit observations own this snapshot's state.
                if chunk is None:
                    chunk=self.incremental_flat_snapshot(value,identity,flat) if flat is not None else None
                    if chunk is None:chunk=self.snapshot_root(value)
                    if signature is not None and chunk[4] and 'reference_budget_exceeded' not in chunk[2]:
                        self.remember_snapshot(identity,signature,chunk)
                elif signature is not None:self.snapshot_cache.move_to_end(signature)
                if signature is not None and signature in self.snapshot_cache:self.root_states[identity]=signature
                local[identity]=chunk
            root,adjacency,unread,nodes,complete=chunk
            if (self.reference_transport is not None and name==names[-1] and len(groups)<=1 and complete
                    and signature is not None and signature in self.snapshot_cache):
                references=self.snapshot_cache_reference_counts.get(signature)
                if references is None:references=sum(len(edges) for edges in adjacency.values())
                shared_edges=sum(len(edges) for parent,edges in groups.items() if parent in adjacency)
                if count+references-shared_edges<=self.max_references:
                    self.reference_transport.snapshot(self.stack[-1]['uid'],phase,values,
                        frozenset([*roots,(None,'root:'+name,root)]),(groups,adjacency),notes|unread)
                    return
            if len(values)==1 and complete and signature is not None and signature in self.snapshot_cache:
                # A complete current graph was just validated. A sole root has
                # no later roots to coordinate or merge, so borrow its immutable
                # adjacency rather than copying every unchanged node and edge.
                # Preserve the original partial-observation path near the budget.
                references=self.snapshot_cache_reference_counts.get(signature)
                if references is None:references=sum(len(edges) for edges in adjacency.values())
                if 1+references<=self.max_references:
                    self.publish_snapshot(self.stack[-1]['uid'],phase,values,
                                          frozenset({(None,'root:'+name,root)}),adjacency,unread)
                    return
            self._snapshot_known_nodes.update(nodes)
            roots.append((None,'root:'+name,root));notes.update(unread)
            for parent,edges in adjacency.items():
                if parent not in groups:
                    count+=len(edges)
                    if count>self.max_references:
                        notes.add('reference_budget_exceeded');self.gaps.add('reference_budget_exceeded');break
                    groups[parent]=edges
                elif groups[parent] is not edges and groups[parent]!=edges:
                    notes.add('snapshot_inconsistent_reference_state');self.gaps.add('snapshot_inconsistent_reference_state')
            if 'reference_budget_exceeded' in notes:break
        self.publish_snapshot(self.stack[-1]['uid'],phase,values,frozenset(roots),groups,notes)

    def snapshot_native_root(self,value):
        if type(value) not in (types.BuiltinFunctionType,types.BuiltinMethodType):return None
        receiver=value.__self__
        if receiver is None or (id(receiver),id(type(receiver))) not in self._snapshot_root_targets:return None
        if id(value) in self.observer_roots and self.observer_roots[id(value)] is value:return None
        labels={};token,meta=self.metadata(value,labels);target,target_meta=self.metadata(receiver,labels)
        for key,row in ((token,meta),(target,target_meta)):
            if self.object_metadata.get(key)!=row:self.object_metadata[key]=row;self.emit('object',**row)
        self.snapshot_native_root_stubs+=1
        self._snapshot_stub_nodes.add(token)
        # Do not mark the receiver expanded and never cache this incomplete
        # subtree as a complete standalone observation. Its explicit root is
        # still pending, or has already contributed its complete adjacency.
        return token,{token:frozenset({(token,'receiver',target)})},frozenset(),frozenset({token}),False

    def remember_snapshot(self,identity,signature,chunk):
        size=len(signature) if type(signature) is bytes else len(signature[1][0])+len(signature[1][1])+64
        references=sum(len(edges) for edges in chunk[1].values())
        size+=references*96+len(chunk[1])*128+len(chunk[3])*40+256
        # An uncacheable giant must not evict every reusable interior first.
        if size>self.snapshot_cache_limit_bytes:return
        old_signature=self.root_states.get(identity)
        if old_signature is not None and old_signature!=signature:
            self.snapshot_cache.pop(old_signature,None)
            self.snapshot_cache_bytes-=self.snapshot_cache_costs.pop(old_signature,0)
            self.snapshot_cache_roots.pop(old_signature,None)
            self.snapshot_cache_reference_counts.pop(old_signature,None)
        self.snapshot_cache[signature]=chunk;self.snapshot_cache.move_to_end(signature)
        self.snapshot_cache_roots[signature]=identity;self.root_states[identity]=signature
        self.snapshot_cache_reference_counts[signature]=references
        self.snapshot_cache_bytes-=self.snapshot_cache_costs.get(signature,0)
        self.snapshot_cache_costs[signature]=size;self.snapshot_cache_bytes+=size
        while len(self.snapshot_cache)>4096 or self.snapshot_cache_bytes>self.snapshot_cache_limit_bytes:
            previous,_=self.snapshot_cache.popitem(last=False)
            self.snapshot_cache_bytes-=self.snapshot_cache_costs.pop(previous)
            self.snapshot_cache_reference_counts.pop(previous,None)
            old_identity=self.snapshot_cache_roots.pop(previous,None)
            if old_identity is not None and self.root_states.get(old_identity)==previous:self.root_states.pop(old_identity,None)

    def promote_snapshot_components(self,candidates,groups,nodes,node_notes):
        # No second Python observation and no new events. Slice the complete
        # observation only after a second exact validator confirms its state.
        from .frame_identity import graph_signature,flat_sequence_signature
        candidates.sort(key=lambda row:len(row[2]) if type(row[2]) is bytes else len(row[2][1][1]))
        for value,token,signature in candidates:
            identity=(id(value),id(type(value)))
            flat=flat_sequence_signature(value)
            current=(identity,flat) if flat is not None else graph_signature({'value':value},self.max_references,self.observer_roots,self.type_context_cache)
            if current!=signature:continue
            pending=[token];observed=set();adjacency={};unread=set();references=0
            while pending:
                parent=pending.pop()
                if parent in observed or parent not in nodes:continue
                observed.add(parent);unread.update(node_notes.get(parent,()))
                edges=groups.get(parent)
                if edges is not None:
                    adjacency[parent]=edges;references+=len(edges)
                    pending.extend(edge[2] for edge in edges)
            if references<32:continue
            chunk=(token,adjacency,frozenset(unread),frozenset(observed),True)
            self.remember_snapshot(identity,signature,chunk);self.component_cache_promotions+=1

    def incremental_flat_snapshot(self,value,identity,flat):
        """Reuse only unchanged immutable-leaf identities; observe every new edge.

        The C validator has inspected every current position and rejected mutable
        descendants. A changed owner-file context, budget/boundary, or unsupported
        prior observation takes the original complete traversal.
        """
        from .frame_identity import flat_sequence_changes
        prior_signature=self.root_states.get(identity)
        if type(prior_signature) is not tuple or prior_signature[0]!=identity:return None
        previous=prior_signature[1]
        if type(previous) is not tuple or len(previous)!=2 or previous[0]!=flat[0]:return None
        prior=self.snapshot_cache.get(prior_signature)
        if prior is None or prior[2] or not prior[4] or len(value)+1>self.max_references:return None
        if id(value) in self.observer_roots and self.observer_roots[id(value)] is value:return None
        root,meta=self.metadata(value)
        if root!=prior[0] or set(prior[1])!={root}:return None
        old_edges=prior[1][root];old_count=len(previous[1])//(2*struct.calcsize('P'))
        if len(old_edges)!=old_count:return None
        changed=flat_sequence_changes(previous[1],flat[1])
        # Appending immutable leaves changes only new positions. Rebuilding a
        # Python role->edge dictionary for every prior item on every append is
        # quadratic bookkeeping; the exact C identity comparison above has
        # already proved that all old positions remain unchanged.
        replaced=any(index<old_count for index in changed)
        by_role={edge[1]:edge for edge in old_edges} if len(value)<old_count or replaced else {}
        removed=set();added=set()
        for index in range(len(value),old_count):
            removed.add(by_role['item:'+str(index)])
        for index in changed:
            role='item:'+str(index);old=by_role.get(role)
            if old is not None:removed.add(old)
            token,child_meta=self.metadata(value[index])
            if self.object_metadata.get(token)!=child_meta:
                self.object_metadata[token]=child_meta;self.emit('object',**child_meta)
            added.add((root,role,token))
        # Keep the validated old adjacency immutable. In the common append
        # case a frozenset union builds the next state once, instead of copying
        # every old edge into a mutable set and then copying it back again.
        edges=old_edges.difference(removed).union(added) if removed else old_edges.union(added)
        if self.object_metadata.get(root)!=meta:
            self.object_metadata[root]=meta;self.emit('object',**meta)
        self.flat_incremental_updates+=1;self.flat_unchanged_leaves_reused+=min(len(value),old_count)-sum(index<old_count for index in changed)
        return root,{root:edges},frozenset(),frozenset({root}),True

    def snapshot_root(self,value):
        # Cache by reference root, not the whole frame: a changing loop counter
        # must not invalidate an unchanged registry reachable from another local.
        from .frame_identity import reference_children
        queue=collections.deque([(None,None,value)]);groups={};seen=set();notes=set();count=0
        metadata={};labels={};plans={};root=None;nodes=set();complete=True;reused_components={}
        shared=getattr(self,'_snapshot_known_nodes',set())
        # A coordinated builtin root contributes only its receiver edge and no
        # opaque/property notes. It must not disable interior cache promotion
        # for the complete receiver root that follows it.
        promote=(not shared or shared.issubset(getattr(self,'_snapshot_stub_nodes',set()))) and self.snapshot_component_limit>0;candidates=[];node_notes={}
        while queue:
            parent,name,value=queue.popleft();count+=1
            if count>self.max_references:
                notes.add('reference_budget_exceeded');self.gaps.add('reference_budget_exceeded');break
            identity=(id(value),id(type(value)))
            candidate=self.root_states.get(identity) if parent is not None else None
            prior=self.snapshot_cache.get(candidate) if candidate is not None else None
            prior_references=self.snapshot_cache_reference_counts.get(candidate) if prior is not None else None
            if prior is not None and (prior_references if prior_references is not None else sum(len(edges) for edges in prior[1].values()))>=32:
                # First-visit ownership already applies below. Honor it before
                # recursively validating a cached component again for an alias
                # or cycle. Incoming edges remain; uncached leaves pay no extra
                # identity/seen lookups for this optimization.
                previous=metadata.get(identity)
                if previous is not None and previous[0] in seen:
                    add_reference(groups,parent,(parent,name,previous[0]));continue
                reused=reused_components.get(identity)
                if reused is not None and reused[0] is value:
                    add_reference(groups,parent,(parent,name,reused[1]));continue
                from .frame_identity import graph_signature,flat_sequence_signature
                flat=flat_sequence_signature(value)
                signature=(identity,flat) if flat is not None else graph_signature({'value':value},self.max_references,self.observer_roots,self.type_context_cache)
                overlap=prior[3].intersection(shared)
                previous_groups=getattr(self,'_snapshot_current_groups',{})
                compatible=all(prior[1].get(node,frozenset())==groups.get(node,previous_groups.get(node,frozenset())) for node in overlap)
                if signature==candidate and compatible and not any(target in groups and groups[target]!=edges for target,edges in prior[1].items()):
                    self.snapshot_cache.move_to_end(candidate)
                    target,adjacency,unread,observed,complete=prior
                    if identity in getattr(self,'_snapshot_root_targets',()):self._snapshot_root_tokens[identity]=target
                    new_edges=sum(len(edges) for node,edges in adjacency.items() if node not in shared)
                    add_reference(groups,parent,(parent,name,target));groups.update(adjacency)
                    seen.update(observed);nodes.update(observed);shared.update(observed);notes.update(unread)
                    node_notes.setdefault(target,set()).update(unread);self.component_cache_reuses+=1
                    reused_components[identity]=(value,target)
                    count+=new_edges
                    if count>self.max_references:
                        notes.add('reference_budget_exceeded');self.gaps.add('reference_budget_exceeded');break
                    continue
            if identity not in metadata:metadata[identity]=self.metadata(value,labels)
            token,meta=metadata[identity]
            if identity in getattr(self,'_snapshot_root_targets',()):self._snapshot_root_tokens[identity]=token
            if parent is None:root=token
            else:add_reference(groups,parent,(parent,name,token))
            if self.object_metadata.get(token)!=meta:
                self.object_metadata[token]=meta;self.emit('object',**meta)
            if token in seen:continue
            seen.add(token);typ=type(value);children=[]
            if promote and parent is not None:
                worthwhile=(typ in (dict,list,tuple,set,frozenset) and len(value)>=64) or (typ is not types.ModuleType and issubclass(typ,types.ModuleType))
                if worthwhile and not (id(value) in self.observer_roots and self.observer_roots[id(value)] is value):
                    from .frame_identity import graph_signature,flat_sequence_signature
                    flat=flat_sequence_signature(value)
                    component_signature=(identity,flat) if flat is not None else graph_signature({'value':value},self.max_references,self.observer_roots,self.type_context_cache)
                    if component_signature is not None:
                        candidates.append((value,token,component_signature))
                        candidates.sort(key=lambda row:len(row[2]) if type(row[2]) is bytes else len(row[2][1][1]),reverse=True)
                        del candidates[self.snapshot_component_limit:]
            if typ not in (str,bytes,int,float,complex,bool,type(None),types.CodeType):
                if token in shared:
                    # Borrow the first observed state of shared descendants from
                    # this same snapshot, rather than rereading builtin getters
                    # or discarding a reusable root merely because it aliases.
                    pending=[token];borrowed=set()
                    current=getattr(self,'_snapshot_current_groups',{})
                    while pending:
                        target=pending.pop()
                        if target in borrowed:continue
                        borrowed.add(target);nodes.add(target)
                        edges=current.get(target)
                        if edges is not None:
                            groups[target]=edges;count+=len(edges)
                            pending.extend(edge[2] for edge in edges)
                    # Every borrowed descendant was already observed, including
                    # those reachable through another alias queued below. Keep
                    # those alias edges, but do not traverse/count the subtree
                    # again merely because its interior node is another root.
                    seen.update(borrowed)
                    notes.update(getattr(self,'_snapshot_current_notes',()))
                    if count>self.max_references:
                        notes.add('reference_budget_exceeded');self.gaps.add('reference_budget_exceeded')
                    continue
                shared.add(token);nodes.add(token)
            if id(value) in self.observer_roots and self.observer_roots[id(value)] is value:
                notes.add('observer_state_not_traversed');node_notes.setdefault(token,set()).add('observer_state_not_traversed');continue
            fast=reference_children(value,token,NativeStorage)
            if fast is not None:
                if fast:queue.extend(fast)
                del fast
                continue
            if typ in (list,tuple):children=[('item:'+str(i),v) for i,v in enumerate(value)]
            elif typ is dict:
                for i,(k,v) in enumerate(value.copy().items()):children.extend((('key:'+str(i),k),('value:'+str(i),v)))
            elif typ in (set,frozenset):children=[('member:'+str(id(v)),v) for v in value.copy()]
            elif typ is types.MethodType:children=[('receiver',value.__self__),('function',value.__func__)]
            elif typ is types.FunctionType:
                children=[('default:'+str(i),v) for i,v in enumerate(value.__defaults__ or ())]
                children.extend(('keyword_default:'+k,v) for k,v in (value.__kwdefaults__ or {}).items())
                for name,cell in zip(value.__code__.co_freevars,value.__closure__ or ()):
                    try:children.append(('closure:'+name,cell.cell_contents))
                    except ValueError:pass
            elif typ in (types.BuiltinFunctionType,types.BuiltinMethodType):
                if value.__self__ is not None:children=[('receiver',value.__self__)]
            elif typ not in (str,bytes,int,float,complex,bool,type(None),types.ModuleType,types.CodeType) and not issubclass(type(value),type):
                children,unread=instance_fields(value,plans,project_storage=True);notes.update(unread)
                node_notes.setdefault(token,set()).update(unread)
                if not children and label_is_extension(meta):
                    opaque='opaque_storage:'+meta['type'].get('qualname','unknown');notes.add(opaque);node_notes.setdefault(token,set()).add(opaque)
            for name,child in children:
                if type(child) is NativeStorage:
                    count+=1
                    if count>self.max_references:
                        notes.add('reference_budget_exceeded');self.gaps.add('reference_budget_exceeded');break
                    field=self.storage_metadata(token,child)
                    add_reference(groups,token,(token,name,field))
                else:queue.append((token,name,child))
        frozen={parent:frozenset(edges) for parent,edges in groups.items()}
        if candidates and complete and 'reference_budget_exceeded' not in notes:
            self.promote_snapshot_components(candidates,frozen,nodes,node_notes)
        return root,frozen,frozenset(notes),frozenset(nodes),complete

    def storage_metadata(self,owner,storage):
        descriptor=storage.descriptor
        declaration={'owner':class_label(descriptor.__objclass__),'name':descriptor.__name__,
                     'byte_offset':storage.offset,'c_member_type':storage.kind}
        semantic={'kind':'storage_member','type':class_label(storage.value_type),'owner':owner,
                  'identity':'native_storage_projection','declaration':declaration}
        token='member:'+owner+':'+str(id(descriptor))+':'+hashlib.sha256(json.dumps(semantic,sort_keys=True).encode()).hexdigest()[:16]
        meta={'id':token,**semantic}
        if self.object_metadata.get(token)!=meta:self.object_metadata[token]=meta;self.emit('object',**meta)
        return token



    def publish_snapshot(self,uid,phase,values,current,groups,notes):
        if self.reference_transport is not None:
            return self.reference_transport.snapshot(uid,phase,values,current,(groups,),notes)
        if self.grouped_references:return self.publish_grouped_snapshot(uid,phase,values,current,groups,notes)
        scope=('global:'+','.join(values)) if phase=='global_read' else 'return' if phase=='return' else 'native' if phase=='native_entry' else 'frame'
        key=(uid,scope)
        previous,old_groups=self.views.get(key,(set(),{}))
        if current is previous and groups is old_groups and not notes:return
        added=set(current-previous);removed=set(previous-current)
        for token in groups.keys() | old_groups.keys():
            now=groups.get(token,frozenset());old=old_groups.get(token,frozenset())
            if now is old:continue
            added.update(now-old);removed.update(old-now)
        # An edge can move between the ordinary and flat-sequence representation.
        unchanged=added & removed;added.difference_update(unchanged);removed.difference_update(unchanged)
        if added or removed or notes:
            self.emit('references',uid=uid,phase=phase,scope=scope,
                      added=sorted(added,key=lambda r:(r[0] or '',r[1],r[2])),
                      removed=sorted(removed,key=lambda r:(r[0] or '',r[1],r[2])),notes=sorted(notes))
        self.remember_view(key,(current,groups))




    def profile(self, frame, event, arg, external_uid=None):
        if self.busy or threading.get_ident()!=self.thread:return
        if os.getpid()!=self.owner_pid:self.redirect_fork_fragment()
        if self.fragment_pid==os.getpid() and event=='c_call' and arg is _PROCESS_EXIT:
            if self.native is not None:self.native.close()
            self.file.flush()
        quiet_enter=getattr(self.native,'observer_enter',None) if self.protected_subscription is None else None
        quiet_leave=getattr(self.native,'observer_leave',None) if quiet_enter else None
        if quiet_enter:quiet_enter()
        self.busy=True
        try:
            if self.body_code is not None:
                if frame.f_code is self.body_code and event in ('call','return'):
                    self.body_depth+=1 if event=='call' else -1
                    if event=='call':self.body_entries+=1
                    return
                if not self.body_depth and not self.stack:return
            if self.suppressed:
                if event=='call':self.suppressed+=1
                elif event=='return':
                    self.suppressed-=1
                    if not self.suppressed:
                        if self.registry:self.observe('excluded_return',self.registry.exclude_returned,arg)
                        if self.native:self.observe('native_resume',self.native.resume_current)
                return
            if event=='call' and self.registry:
                excluded=self.observe('api_exclusion',self.registry.excluded,frame)
                if excluded:
                    if self.native:self.observe('native_pause',self.native.pause)
                    self.suppressed=1
                    self.emit('excluded_call',api_id=excluded,tid=threading.get_native_id(),
                              parent=self.stack[-1]['uid'] if self.stack else None,
                              time_ns=time.monotonic_ns(),reason='excluded_runtime_body')
                    return
            if event not in ('call','return','c_call','c_return','c_exception'):return
            if event in ('call','c_call'):
                module=frame.f_globals.get('__name__') if event=='call' else getattr(arg,'__module__',None)
                if event=='c_call' and not self.framework(module):
                    module=class_label(type(getattr(arg,'__self__',None))).get('module')
                aliases=(self.observe('api_match',self.registry.match,frame,event,arg) or []) if self.registry else []
                if external_uid is None and not self.stack and not aliases:return
                self.next_uid+=1;uid=external_uid if external_uid is not None else self.next_uid
                parent=self.stack[-1]['uid'] if self.stack else None
                definition=self.observe('code_definition',self.code,frame.f_code,module) if event=='call' else None
                name=getattr(frame.f_code,'co_qualname',frame.f_code.co_name) if event=='call' else getattr(arg,'__qualname__',getattr(arg,'__name__','native'))
                row={'uid':uid,'parent':parent,'kind':'python' if event=='call' else 'native_boundary',
                     'tid':threading.get_native_id(),'time_ns':time.monotonic_ns(),
                     'definition':definition,'name':name,'module':module,
                     'api_id':aliases[0] if aliases else None,'api_aliases':aliases,
                     'caller_file':frame.f_back.f_code.co_filename if event=='call' and frame.f_back else frame.f_code.co_filename,
                     'caller_line':frame.f_back.f_lineno if event=='call' and frame.f_back else frame.f_lineno}
                if event=='c_call':row['native_identity']=native_identity(arg)
                native_key=row.get('native_identity') or {}
                context_key=(self.stack[-1]['path_context'] if self.stack else None,row['kind'],definition,
                             native_key.get('module'),native_key.get('module_offset'),tuple(aliases))
                if context_key not in self.path_contexts:self.path_contexts[context_key]=len(self.path_contexts)+1
                row['path_context']=self.path_contexts[context_key]
                self.emit('call',**row)
                self.stack.append({**row,'frame':id(frame),'callable':id(arg) if event=='c_call' else None})
                self.calls+=1;self.roots+=int(parent is None)
                self.observe('entry_snapshot',self.snapshot,frame,'entry' if event=='call' else 'native_entry',
                             None if event=='call' else {'callable':arg,'receiver':getattr(arg,'__self__',None)})
                if self.native:
                    if getattr(self.native,'storage',None)=='paths':self.observe('native_begin',self.native.begin,uid,row.get('native_identity'),context=row['path_context'])
                    else:self.observe('native_begin',self.native.begin,uid,row.get('native_identity'))
            elif self.stack:
                top=self.stack[-1]
                matches=(top['kind']=='python' and event=='return' and top['frame']==id(frame)) or (
                    top['kind']=='native_boundary' and event in ('c_return','c_exception') and top['frame']==id(frame) and top['callable']==id(arg))
                if not matches:
                    self.gaps.add('call_return_mismatch')
                    self.emit('gap',reason='call_return_mismatch',uid=top['uid'],observed_event=event,
                              expected_kind=top['kind'],expected_frame=top['frame'],observed_frame=id(frame),
                              expected_callable=top['callable'],observed_callable=id(arg) if event!='return' else None)
                    return
                if event=='return':
                    self.observe('exit_snapshot',self.snapshot,frame,'exit')
                    self.observe('return_snapshot',self.snapshot,frame,'return',{'return':arg})
                if self.native:self.observe('native_end',self.native.end,top['uid'])
                # A real return always retires its matching frame, including when
                # optional snapshots or native bookkeeping reported an error.
                try:self.emit('return',uid=top['uid'],kind=event,time_ns=time.monotonic_ns())
                finally:
                    self.stack.pop()
                    self.read_sites.pop(top['uid'],None)
                    self.module_operands.pop(top['uid'],None)
                    self.retire_views(top['uid'])
                if not self.stack:self.file.flush()  # Preserve each completed API window, not each object event.
        except Exception as exc:
            self.collector_error(exc,'profile_'+event)
        finally:
            self.busy=False
            if quiet_leave:quiet_leave()

    def trace(self,frame,event,arg):
        if os.getpid()!=self.owner_pid:self.redirect_fork_fragment()
        quiet_enter=getattr(self.native,'observer_enter',None) if self.protected_subscription is None else None
        quiet_leave=getattr(self.native,'observer_leave',None) if quiet_enter else None
        if quiet_enter:quiet_enter()
        try:return self.trace_event(frame,event,arg)
        finally:
            if quiet_leave:quiet_leave()

    def trace_event(self, frame, event, arg):
        if self.suppressed or threading.get_ident()!=self.thread:return None
        if self.body_code is not None and not self.body_depth and not self.stack:return None
        if event=='call' and not self.stack and not self.framework(frame.f_globals.get('__name__')):return None
        if event=='call' and (self.stack or self.framework(frame.f_globals.get('__name__'))):
            key=id(frame.f_code)
            if key not in self.global_loads:
                instructions=list(dis.get_instructions(frame.f_code))
                self.global_loads[key]={i.offset:(i.opname,i.argval,instructions[n+1].offset if n+1<len(instructions) else None)
                    for n,i in enumerate(instructions) if i.opname in ('LOAD_GLOBAL','LOAD_NAME','LOAD_ATTR')}
            frame.f_trace_opcodes=bool(self.global_loads[key])
        if event=='opcode' and self.stack and not self.busy and self.stack[-1]['kind']=='python' and self.stack[-1]['frame']==id(frame):
            instruction=self.global_loads.get(id(frame.f_code),{}).get(frame.f_lasti)
            uid=self.stack[-1]['uid'];operand=self.module_operands.pop(uid,None)
            if instruction:
                opname,name,next_offset=instruction
                self.busy=True
                try:
                    if opname=='LOAD_ATTR':
                        # Only a straight LOAD_GLOBAL/LOAD_NAME -> LOAD_ATTR chain
                        # over exact modules. Never evaluate arbitrary getattr.
                        if operand and operand[0]==frame.f_lasti:
                            namespace=vars(operand[1])
                            if name in namespace:
                                self.source_read(frame,namespace,name,'module_attribute')
                                value=namespace[name]
                                if type(value) is types.ModuleType:self.module_operands[uid]=(next_offset,value)
                    else:
                        scopes=([frame.f_locals] if opname=='LOAD_NAME' else [])+[frame.f_globals,frame.f_builtins]
                        for scope in scopes:
                            if name in scope:
                                value=scope[name]
                                if scope is frame.f_globals:self.source_read(frame,scope,name,'global')
                                self.snapshot(frame,'global_read',{'global:'+name:value})
                                if type(value) is types.ModuleType:self.module_operands[uid]=(next_offset,value)
                                break
                except Exception as exc:self.gaps.add('global_observation_error:'+type(exc).__name__)
                finally:self.busy=False
        if event=='line' and self.stack and not self.busy and self.stack[-1]['kind']=='python' and self.stack[-1]['frame']==id(frame):
            self.busy=True
            try:self.snapshot(frame,'locals')
            except Exception as exc:self.gaps.add('object_observation_error:'+type(exc).__name__)
            finally:self.busy=False
        return self.trace

    def begin(self):
        global _FORK_CALLBACK_READY
        _ACTIVE_RECORDERS.add(self)
        if hasattr(os,'register_at_fork') and not _FORK_CALLBACK_READY:
            os.register_at_fork(after_in_child=_redirect_inherited_recorders);_FORK_CALLBACK_READY=True
        self.old_profile=sys.getprofile();self.old_trace=sys.gettrace()
        identity=sys.modules.get('_relation_identity')
        if identity is not None and getattr(identity,'protected_observer_available',lambda:False)():
            self.protected_subscription=(identity,identity.attach_callchain_observer(self.profile,self.trace))
            self.started=True
            return
        if self.old_profile or self.old_trace:raise RuntimeError('existing Python profiling/tracing hook; cannot silently replace it')
        self.started=True
        sys.setprofile(self.profile);sys.settrace(self.trace)

    def end(self, external_hooks=False):
        if os.getpid()!=self.owner_pid:self.redirect_fork_fragment()
        if self.fragment_pid==os.getpid():
            # A parent guard token is not valid against a newly bootstrapped
            # relation observer in the child. Never detach somebody else's hook.
            if not self.fork_inherited_protected:
                if sys.getprofile()==self.profile:sys.setprofile(self.old_profile)
                if sys.gettrace()==self.trace:sys.settrace(self.old_trace)
            self.protected_subscription=None
        elif self.protected_subscription is not None:
            identity,token=self.protected_subscription
            try:
                if not identity.guard_intact():self.gaps.add('hook_replaced')
                identity.detach_callchain_observer(token)
            except RuntimeError:self.gaps.add('hook_replaced')
            self.protected_subscription=None
        elif not external_hooks:
            if sys.getprofile()!=self.profile or sys.gettrace()!=self.trace:self.gaps.add('hook_replaced')
            sys.setprofile(self.old_profile);sys.settrace(self.old_trace)
        if self.stack:self.gaps.add('unclosed_calls')
        if self.body_code is not None and not self.body_entries:self.gaps.add('test_body_not_entered')
        self.closed=True
        from .frame_identity import type_context_cache_stats
        self.emit('end',reference_storage=self.reference_storage,type_metadata_reuse=type_context_cache_stats(self.type_context_cache),calls=self.calls,roots=self.roots,objects=len(self.object_metadata),
                  gaps=sorted(self.gaps),all_objects_complete=False,module_sources=self.module_sources,
                  source_read_policy='namespace-load/1',
                  metadata_cache={'limit':self.metadata_cache_limit,'entries':len(self.metadata_cache),
                                  'hits':self.metadata_cache_hits,'misses':self.metadata_cache_misses,
                                  'evictions':self.metadata_cache_evictions,'template_hits':self.metadata_template_hits,
                                  'templates':len(self.metadata_templates)})
        self.file.close()
        self.observer_roots.clear();_ACTIVE_RECORDERS.discard(self)


def label_is_extension(meta):
    module=meta['type'].get('module') or ''
    return type(module) is str and module.startswith(('torch','numpy'))


from .native import native_identity
