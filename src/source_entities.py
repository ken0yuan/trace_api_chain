"""Canonical source definitions. Runtime names are aliases, never identity keys."""
import hashlib
import re
from pathlib import Path
from .changed_objects import key, source_identity, canonical_path, decode_python, index_python

class SourceEntities:
    def __init__(self,roots=None):
        self.roots={k:Path(v).resolve() for k,v in (roots or {}).items()};self.files={};self.entities={};self.cpp_files={};self.path_cache={}
        self.sources_by_path={};self.container_definitions={};self.entity_templates={}

    def path(self,filename):
        if str(filename) not in self.path_cache:self.path_cache[str(filename)]=self.canonical_source_path(filename)
        return self.path_cache[str(filename)]

    def canonical_source_path(self,filename):
        p=Path(filename).resolve()
        # Installed source/header copies can refer to the same repository file.
        # Only an explicit layout correspondence AND identical bytes prove this.
        text=p.as_posix();candidates=[]
        for package,label in (('torch','pytorch'),('torch_npu','torch_npu')):
            marker='/site-packages/'+package+'/'
            if marker in text and label in self.roots:
                relative=text.split(marker,1)[1];root=self.roots[label]
                candidates.append(root/package/relative)
                if package=='torch' and relative.startswith('include/'):
                    tail=relative[len('include/'):]
                    candidates.extend((root/tail,root/'aten/src'/tail,root/'build/aten/src'/tail))
        for candidate in candidates:
            if candidate!=p and candidate.is_file() and candidate.read_bytes()==p.read_bytes():
                return canonical_path(candidate,self.roots)
        return canonical_path(p,self.roots)

    def load(self,filename):
        filename=str(Path(filename).resolve())
        if filename not in self.files:
            try:
                p=Path(filename);data=p.read_bytes();s=p.stat();text=decode_python(data)
                definitions=index_python(text)
                self.files[filename]={'path':self.path(filename),'sha256':hashlib.sha256(data).hexdigest(),
                                      'filename':filename,'content':text,'lines':text.splitlines(),
                                      'stamp':[s.st_size,s.st_mtime_ns],'definitions':definitions}
                self.sources_by_path.setdefault(self.files[filename]['path'],self.files[filename])
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
        identity=(source['path'],source['sha256'],definition['kind'],definition['name'],
                  definition.get('ordinal',0),definition['line'],definition['end_line'],language,evidence)
        template=self.entity_templates.get(identity)
        if template is None:
            eid=source_identity(source['path'],definition['kind'],definition['name'])
            lines=source.get('lines')
            if lines is None:lines=source['content'].splitlines()
            template={'id':eid,'path':source['path'],'kind':definition['kind'],'name':definition['name'],
                      'ordinal':definition.get('ordinal',0),'line':definition['line'],'end_line':definition['end_line'],
                      'source_sha256':source['sha256'],'language':language,'status':'mapped','evidence':evidence,
                      'source_text':'\n'.join(lines[definition['line']-1:definition['end_line']])}
            self.entity_templates[identity]=template
        eid=template['id']
        # Keep the original last-observation evidence assignment: a later named
        # binding may have overwritten the same lexical container meanwhile.
        self.entities[eid]=dict(template)
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
        source=self.sources_by_path.get(entity['path'])
        if source is None:return []
        identity=(source['path'],source['sha256'],entity['kind'],entity['name'])
        definitions=self.container_definitions.get(identity)
        if definitions is None:
            definitions=tuple(d for d in source['definitions']
                    if (d['kind']=='module' and entity['kind']!='module') or
                    (d['kind'] in ('type','function') and entity['name'].startswith(d['name']+'.') and not d.get('ambiguous') and not d.get('declaration_only')))
            self.container_definitions[identity]=definitions
        return [self.entity(source,d,'python','lexical_source_container') for d in definitions]

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
                path=self.path(filename)
                anchor=cpp_source_anchor(f,lines)
                if not anchor:
                    return self.unresolved([identity.get('sha256'),identity.get('path'),identity.get('offset')],name,'cpp','ambiguous_DWARF_source_anchor',annotation)
                column,source_name=anchor
                eid=key([path,'function',line,column,source_name])
                self.entities.setdefault(eid,{'id':eid,'path':path,'kind':'function','name':source_name,'ordinal':line,
                                    'line':line,'end_line':line,'source_sha256':sha,'language':'cpp','status':'mapped',
                                    'column':column,'evidence':'DWARF_source_definition_anchor','source_text':lines[line-1],
                                    'annotation':annotation,'dwarf':f,'runtime_names':[], 'linkage_names':[],
                                    'build_source_match_verified':False})
                entity=self.entities[eid]
                entity['runtime_names']=sorted(set(entity['runtime_names']+[name]))
                entity['linkage_names']=sorted(set(entity['linkage_names']+([f['linkage_name']] if f.get('linkage_name') else [])))
                return eid
        label=next((f.get('symbol') for f in reversed((annotation or {}).get('frames',[])) if f.get('symbol') not in (None,'??','')),None)
        return self.unresolved([identity.get('sha256'),identity.get('path'),identity.get('offset')],label,'cpp','no_unique_DWARF_definition',annotation)


def cpp_source_anchor(function,lines):
    """Verify the DWARF declaration token; never equate symbols by demangling.

    Template instantiations share this source token, whereas overloads and
    explicit specializations have their own line/column. Macro/generated or
    missing coordinates remain unresolved rather than inventing a definition.
    """
    line=function.get('line',0);column=function.get('column',0)
    if not (0<line<=len(lines) and column>0):return None
    # GCC's declaration column is a byte column, including tabs as one byte.
    tail=lines[line-1].encode('utf-8')[column-1:].decode('utf-8',errors='replace')
    name=function.get('declaration_name') or function.get('name') or ''
    if name.startswith('operator'):
        if not tail.startswith('operator'):return None
        # An operator's actual source spelling is required, not its instantiated
        # conversion type. Keep unusual conversion operators unresolved.
        m=re.match(r'operator\s*(\(\)|\[\]|new\[\]|delete\[\]|new|delete|[-+*/%=!<>&|^~,]+)\s*\(',tail)
        if not m:return None
        source_name='operator'+m[1]
        if re.sub(r'\s+','',name).split('<',1)[0]!=source_name and re.sub(r'\s+','',name)!=source_name:return None
        return column,source_name
    m=re.match(r'~?[A-Za-z_][A-Za-z_0-9]*',tail)
    if not m:return None
    source_name=m[0]
    if name!=source_name and not name.startswith(source_name+'<'):return None
    rest=tail[m.end():].lstrip()
    if not rest.startswith(('(', '<')):return None
    return column,source_name
