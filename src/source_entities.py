"""Canonical source definitions. Runtime names are aliases, never identity keys."""
import ast
import hashlib
import json
from pathlib import Path

def key(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

class SourceEntities:
    def __init__(self,roots=None):
        self.roots={k:Path(v).resolve() for k,v in (roots or {}).items()};self.files={};self.entities={};self.cpp_files={}

    def path(self,filename):
        p=Path(filename).resolve();candidates=[]
        for label,root in self.roots.items():
            if p.is_relative_to(root):
                relative=p.relative_to(root).as_posix()
                prefix='torch_npu/' if label=='torch_npu' else 'torch/' if label=='pytorch' else label+'/'
                candidates.append((len(str(root)),int(relative.startswith(prefix)),label+'/'+relative))
        return max(candidates)[2] if candidates else p.as_posix()

    def load(self,filename):
        filename=str(Path(filename).resolve())
        if filename not in self.files:
            try:
                p=Path(filename);data=p.read_bytes();s=p.stat();text=data.decode('utf-8')
                tree=ast.parse(text);definitions=[];ordinals={}
                def walk(node,parents):
                    for child in ast.iter_child_nodes(node):
                        if isinstance(child,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):
                            name='.'.join(parents+[child.name]);kind='type' if isinstance(child,ast.ClassDef) else 'function'
                            first=min([child.lineno]+[d.lineno for d in child.decorator_list])
                            ordinal=ordinals.get((kind,name),0);ordinals[kind,name]=ordinal+1
                            definitions.append({'name':name,'kind':kind,'line':first,'end_line':child.end_lineno,
                                                'ordinal':ordinal,'parent':'.'.join(parents)})
                            walk(child,parents+[child.name])
                        else:walk(child,parents)
                walk(tree,[])
                self.files[filename]={'path':self.path(filename),'sha256':hashlib.sha256(data).hexdigest(),
                                      'content':text,'stamp':[s.st_size,s.st_mtime_ns],'definitions':definitions}
            except (OSError,UnicodeError,SyntaxError):self.files[filename]=None
        return self.files[filename]

    def python(self,record):
        if not record.get('file'):return None,'no_code_location'
        source=self.load(record['file'])
        if source is None:return None,'source_unavailable'
        if record.get('source_stamp') and record['source_stamp']!=source['stamp']:return None,'source_changed_since_capture'
        matches=[d for d in source['definitions'] if d['kind']=='function' and d['line']==record.get('first_line')]
        if len(matches)!=1:return None,'ambiguous_code_definition'
        return self.entity(source,matches[0],'python','runtime_code_first_line'),None

    def instance(self,record):
        candidates=[]
        for hint in record.get('class_source_hints',[]):
            source=self.load(hint['file'])
            if source is None or (hint.get('source_stamp') and hint['source_stamp']!=source['stamp']):continue
            methods=[d for d in source['definitions'] if d['kind']=='function' and d['line']==hint['first_line']]
            for method in methods:
                candidates.extend((source,d) for d in source['definitions'] if d['kind']=='type' and d['name']==method['parent'])
        unique={(s['path'],d['name'],d['ordinal']):(s,d) for s,d in candidates}
        if len(unique)==1:
            s,d=next(iter(unique.values()));return self.entity(s,d,'python','class_own_method_locations'),None
        return None,'ambiguous_class_owner' if unique else 'class_definition_not_proven'

    def entity(self,source,definition,language,evidence):
        eid=key([source['path'],definition['kind'],definition['name'],definition.get('ordinal',0)])
        self.entities[eid]={'id':eid,'path':source['path'],'kind':definition['kind'],'name':definition['name'],
                            'ordinal':definition.get('ordinal',0),'line':definition['line'],'end_line':definition['end_line'],
                            'source_sha256':source['sha256'],'language':language,'status':'mapped','evidence':evidence,
                            'source_text':'\n'.join(source['content'].splitlines()[definition['line']-1:definition['end_line']])}
        return eid

    def unresolved(self,identity,label,language,reason,annotation=None):
        eid=key(['unresolved',identity])
        self.entities.setdefault(eid,{'id':eid,'path':None,'kind':'unresolved','name':label or '<unresolved>',
                                      'ordinal':0,'line':None,'end_line':None,'source_sha256':None,'language':language,
                                      'status':'unresolved','evidence':reason,'source_text':None,'annotation':annotation})
        return eid

    def cpp(self,identity,definition,annotation):
        f=(definition or {}).get('function') or {}
        if f.get('file') and f.get('line') and f.get('kind') in ('definition','inline_definition','declaration_only'):
            filename=f['file'];line=f['line'];p=Path(filename)
            if filename not in self.cpp_files:
                try:
                    data=p.read_bytes();self.cpp_files[filename]=(data.decode('utf-8').splitlines(),hashlib.sha256(data).hexdigest())
                except (OSError,UnicodeError):self.cpp_files[filename]=([],None)
            lines,sha=self.cpp_files[filename]
            name=f.get('declaration_name') or f.get('name')
            if name:
                path=self.path(filename);eid=key([path,'function',name,line,f.get('linkage_name')])
                self.entities[eid]={'id':eid,'path':path,'kind':'function','name':name,'ordinal':line,
                                    'line':line,'end_line':line,'source_sha256':sha,'language':'cpp','status':'mapped',
                                    'evidence':'DWARF_declaration_identity','source_text':lines[line-1] if 0<line<=len(lines) else None,
                                    'annotation':annotation,'dwarf':f,'build_source_match_verified':False}
                return eid
        label=next((f.get('symbol') for f in reversed((annotation or {}).get('frames',[])) if f.get('symbol') not in (None,'??','')),None)
        return self.unresolved([identity.get('sha256'),identity.get('path'),identity.get('offset')],label,'cpp','no_unique_DWARF_definition',annotation)
