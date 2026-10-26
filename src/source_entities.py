"""Canonical source definitions. Runtime names are aliases, never identity keys."""
import hashlib
from pathlib import Path
from .changed_objects import key, source_identity, canonical_path, decode_python, index_python

class SourceEntities:
    def __init__(self,roots=None):
        self.roots={k:Path(v).resolve() for k,v in (roots or {}).items()};self.files={};self.entities={};self.cpp_files={}

    def path(self,filename):
        return canonical_path(filename,self.roots)

    def load(self,filename):
        filename=str(Path(filename).resolve())
        if filename not in self.files:
            try:
                p=Path(filename);data=p.read_bytes();s=p.stat();text=decode_python(data)
                definitions=index_python(text)
                self.files[filename]={'path':self.path(filename),'sha256':hashlib.sha256(data).hexdigest(),
                                      'filename':filename,'content':text,'stamp':[s.st_size,s.st_mtime_ns],'definitions':definitions}
            except (OSError,UnicodeError,SyntaxError):self.files[filename]=None
        return self.files[filename]

    def python(self,record):
        if not record.get('file'):return None,'no_code_location'
        source=self.load(record['file'])
        if source is None:return None,'source_unavailable'
        if record.get('source_stamp') and record['source_stamp']!=source['stamp']:return None,'source_changed_since_capture'
        matches=[d for d in source['definitions'] if d['kind']=='function' and d['line']==record.get('first_line') and not d.get('declaration_only')]
        if len(matches)!=1 or matches[0].get('ambiguous'):return None,'ambiguous_code_definition'
        return self.entity(source,matches[0],'python','runtime_code_first_line'),None

    def instance(self,record):
        candidates=[]
        for hint in record.get('class_source_hints',[]):
            source=self.load(hint['file'])
            if source is None or (hint.get('source_stamp') and hint['source_stamp']!=source['stamp']):continue
            methods=[d for d in source['definitions'] if d['kind']=='function' and d['line']==hint['first_line']]
            for method in methods:
                candidates.extend((source,d) for d in source['definitions'] if d['kind']=='type' and d['name']==method['parent'] and not d.get('ambiguous'))
        unique={(s['path'],d['name'],d['ordinal']):(s,d) for s,d in candidates}
        if len(unique)==1:
            s,d=next(iter(unique.values()));return self.entity(s,d,'python','class_own_method_locations'),None
        return None,'ambiguous_class_owner' if unique else 'class_definition_not_proven'

    def entity(self,source,definition,language,evidence):
        eid=source_identity(source['path'],definition['kind'],definition['name'])
        self.entities[eid]={'id':eid,'path':source['path'],'kind':definition['kind'],'name':definition['name'],
                            'ordinal':definition.get('ordinal',0),'line':definition['line'],'end_line':definition['end_line'],
                            'source_sha256':source['sha256'],'language':language,'status':'mapped','evidence':evidence,
                            'source_text':'\n'.join(source['content'].splitlines()[definition['line']-1:definition['end_line']])}
        return eid

    def named(self,record,name=None):
        if not record.get('file'):return None,'namespace_source_unavailable'
        source=self.load(record['file'])
        if source is None:return None,'source_unavailable'
        if not record.get('source_stamp') or record['source_stamp']!=source['stamp']:
            return None,'namespace_source_unverified_or_changed'
        matches=[d for d in source['definitions'] if d['name']==(name or '<module>') and not d.get('declaration_only')]
        kinds={d['kind'] for d in matches}
        if not matches:return None,'binding_not_declared_in_source'
        if len(kinds)!=1 or any(d.get('ambiguous') for d in matches):return None,'ambiguous_source_binding'
        return self.entity(source,matches[0],'python','observed_namespace_binding' if name else 'module_source_file'),None

    def containers(self,eid):
        entity=self.entities[eid]
        if entity['language']!='python' or not entity['path']:return []
        source=next((s for s in self.files.values() if s and s['path']==entity['path']),None)
        if source is None:return []
        return [self.entity(source,d,'python','lexical_source_container') for d in source['definitions']
                if (d['kind']=='module' and entity['kind']!='module') or
                (d['kind'] in ('type','function') and entity['name'].startswith(d['name']+'.') and not d.get('ambiguous') and not d.get('declaration_only'))]

    def import_origins(self,record,name,modules,seen=None):
        """Static provenance candidates only; never claim imports prove runtime aliasing."""
        seen=set() if seen is None else seen
        marker=(record.get('file'),name)
        if marker in seen:return []
        seen.add(marker)
        source=self.load(record['file']) if record.get('file') else None
        if not source or source['stamp']!=record.get('source_stamp'):return []
        matches=[d for d in source['definitions'] if d['name']==name]
        if len(matches)!=1 or not matches[0].get('import'):return []
        spec=matches[0]['import'];module=spec['module']
        if spec['level']:
            package=record.get('package') or ''
            parts=package.split('.') if package else []
            if spec['level']>len(parts):return []
            module='.'.join(parts[:len(parts)-spec['level']+1]+([module] if module else []))
        origin=modules.get(module)
        if not origin:return []
        eid,reason=self.named(origin,spec['name'])
        if not eid:return []
        result=[eid]
        if spec['name']:result+=self.import_origins(origin,spec['name'],modules,seen)
        return result

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
