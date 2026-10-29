"""Canonical source definitions. Runtime names are aliases, never identity keys."""
import hashlib
import os
import re
from pathlib import Path
from .changed_objects import key, source_identity, canonical_path, decode_python, index_python

class SourceEntities:
    def __init__(self,roots=None,immutable_sources=False):
        self.roots={k:Path(v).resolve() for k,v in (roots or {}).items()};self.root_snapshots={os.fspath(v):self.roots[k] for k,v in (roots or {}).items()};self.files={};self.entities={};self.cpp_files={};self.path_cache={}
        self.sources_by_path={};self.container_definitions={};self.entity_templates={}
        self.definition_indexes={};self.class_resolutions={}
        self.immutable_sources=immutable_sources;self.resolved_paths={};self.source_snapshots={}

    @staticmethod
    def file_state(stat):
        return (stat.st_dev,stat.st_ino,stat.st_mode,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)

    def resolve_source(self,filename):
        if not self.immutable_sources:return Path(filename).resolve()
        raw=os.fspath(filename)
        if not os.path.isabs(raw):raw=os.path.join(os.getcwd(),raw)
        resolved=self.resolved_paths.get(raw)
        if resolved is None:
            resolved=Path(raw).resolve();self.resolved_paths[raw]=resolved
        return resolved

    def source_bytes(self,filename):
        if not self.immutable_sources:return Path(filename).read_bytes()
        path=self.resolve_source(filename);name=str(path);before=None
        try:
            before=path.stat();data=path.read_bytes();after=path.stat()
        except OSError:
            self.source_snapshots.setdefault(name,(self.file_state(before) if before else None,None))
            raise
        if self.file_state(before)!=self.file_state(after):raise ValueError('normalization_source_changed_during_read:'+name)
        snapshot=(self.file_state(after),hashlib.sha256(data).hexdigest())
        old=self.source_snapshots.setdefault(name,snapshot)
        if old!=snapshot:raise ValueError('normalization_source_changed:'+name)
        return data

    def verify_sources(self):
        if not self.immutable_sources:return
        # The worker normalizes one immutable snapshot. Validate every alias and
        # byte source again before publishing its contribution; never mix a new
        # path/definition into results derived from an earlier source snapshot.
        for raw,resolved in self.root_snapshots.items():
            if Path(raw).resolve()!=resolved:raise ValueError('normalization_source_root_changed:'+raw)
        for raw,resolved in self.resolved_paths.items():
            if Path(raw).resolve()!=resolved:raise ValueError('normalization_source_alias_changed:'+raw)
        for name,(state,sha) in self.source_snapshots.items():
            path=Path(name)
            try:current=self.file_state(path.stat())
            except OSError:current=None
            if current!=state:raise ValueError('normalization_source_changed:'+name)
            if sha is not None:
                if hashlib.sha256(path.read_bytes()).hexdigest()!=sha or self.file_state(path.stat())!=state:
                    raise ValueError('normalization_source_changed:'+name)

    def path(self,filename):
        if str(filename) not in self.path_cache:self.path_cache[str(filename)]=self.canonical_source_path(filename)
        return self.path_cache[str(filename)]

    def canonical_source_path(self,filename):
        p=self.resolve_source(filename)
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
            if candidate!=p and candidate.is_file() and self.source_bytes(candidate)==self.source_bytes(p):
                return canonical_path(candidate,self.roots)
        return canonical_path(p,self.roots)

    def load(self,filename):
        filename=str(self.resolve_source(filename))
        if filename not in self.files:
            try:
                p=Path(filename);data=self.source_bytes(p);s=p.stat();text=decode_python(data)
                definitions=index_python(text)
                self.files[filename]={'path':self.path(filename),'sha256':hashlib.sha256(data).hexdigest(),
                                      'filename':filename,'content':text,'lines':text.splitlines(),
                                      'stamp':[s.st_size,s.st_mtime_ns],'definitions':definitions}
                self.sources_by_path.setdefault(self.files[filename]['path'],self.files[filename])
            except (OSError,UnicodeError,SyntaxError):self.files[filename]=None
        return self.files[filename]

    def definition_index(self,source):
        identity=(source['filename'],source['sha256'])
        index=self.definition_indexes.get(identity)
        if index is None:
            functions={};types={};names={}
            for definition in source['definitions']:
                names.setdefault(definition['name'],[]).append(definition)
                if definition['kind']=='function':functions.setdefault(definition['line'],[]).append(definition)
                if definition['kind']=='type':types.setdefault(definition['name'],[]).append(definition)
            index=(functions,types,names);self.definition_indexes[identity]=index
        return index

    def python(self,record):
        if not record.get('file'):return None,'no_code_location'
        source=self.load(record['file'])
        if source is None:return None,'source_unavailable'
        if record.get('source_stamp') and record['source_stamp']!=source['stamp']:return None,'source_changed_since_capture'
        matches=[d for d in self.definition_index(source)[0].get(record.get('first_line'),()) if not d.get('declaration_only')]
        if len(matches)!=1 or matches[0].get('ambiguous'):return None,'ambiguous_code_definition'
        return self.entity(source,matches[0],'python','runtime_code_first_line'),None

    def instance(self,record):
        # Different runtime instances commonly carry exactly the same class
        # proof. Validate every distinct path/stamp in that proof, then reuse
        # only its resolution; entity() still performs its original assignment.
        hints=record.get('class_source_hints',[]);sources={};signature=[];cacheable=True
        for hint in hints:
            filename=hint['file']
            if filename not in sources:sources[filename]=self.load(filename)
            source=sources[filename];stamp=hint.get('source_stamp')
            context=(source['filename'],source['sha256'],tuple(source['stamp'])) if source else None
            item=(filename,hint['first_line'],(tuple(stamp) if type(stamp) in (list,tuple) else stamp) if stamp else None,context)
            try:hash(item)
            except TypeError:cacheable=False
            signature.append(item)
        identity=tuple(signature) if cacheable else None
        if identity is not None and identity in self.class_resolutions:
            source,definition,reason=self.class_resolutions[identity]
            return (self.entity(source,definition,'python','class_own_method_locations'),None) if source else (None,reason)
        candidates=[]
        for hint in hints:
            source=sources[hint['file']]
            if source is None or (hint.get('source_stamp') and hint['source_stamp']!=source['stamp']):continue
            functions,types,_=self.definition_index(source)
            for method in functions.get(hint['first_line'],()):
                candidates.extend((source,d) for d in types.get(method['parent'],()) if not d.get('ambiguous'))
        unique={(source['path'],d['name'],d['ordinal']):(source,d) for source,d in candidates}
        if len(unique)==1:
            source,definition=next(iter(unique.values()))
            if identity is not None:self.class_resolutions[identity]=(source,definition,None)
            return self.entity(source,definition,'python','class_own_method_locations'),None
        reason='ambiguous_class_owner' if unique else 'class_definition_not_proven'
        if identity is not None:self.class_resolutions[identity]=(None,None,reason)
        return None,reason

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
        matches=[d for d in self.definition_index(source)[2].get(name or '<module>',()) if not d.get('declaration_only')]
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
        matches=self.definition_index(source)[2].get(name,[])
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
                    data=self.source_bytes(p);self.cpp_files[filename]=(data.decode('utf-8').splitlines(),hashlib.sha256(data).hexdigest())
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
