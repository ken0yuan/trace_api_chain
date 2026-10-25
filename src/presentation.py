"""Offline static-owner mapping and HTML; never executes the recorded test."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path


class Sources:
    def __init__(self):self.files={}

    def load(self, filename):
        if not filename or filename.startswith('<'):return None
        filename=os.path.realpath(filename)
        if filename not in self.files:
            try:
                import tokenize
                with tokenize.open(filename) as f:text=f.read()
                tree=ast.parse(text,filename);definitions=[]
                def walk(node,parents):
                    for child in ast.iter_child_nodes(node):
                        if isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                            name='.'.join(parents+[child.name])
                            first=min([child.lineno]+[x.lineno for x in child.decorator_list])
                            definitions.append({'qualname':name,'kind':'class' if isinstance(child,ast.ClassDef) else 'function',
                                                'first_line':first,'end_line':child.end_lineno})
                            walk(child,parents+[child.name])
                        else:walk(child,parents)
                walk(tree,[])
                stat=os.stat(filename)
                self.files[filename]={'file':filename,'sha256':hashlib.sha256(Path(filename).read_bytes()).hexdigest(),
                                      'stamp':[stat.st_size,stat.st_mtime_ns],'text':text.splitlines(),'definitions':definitions}
            except (OSError,SyntaxError,UnicodeError):self.files[filename]=None
        return self.files[filename]

    def locate(self, record):
        source=self.load(record.get('file'))
        if not source:return {'status':'source_unavailable'}
        if record.get('source_stamp') and record['source_stamp']!=source['stamp']:
            return {'status':'source_changed_since_observation'}
        name=(record.get('qualname') or '').replace('.<locals>.','.')
        matches=[d for d in source['definitions'] if d['qualname']==name and
                 (not record.get('first_line') or d['first_line']<=record['first_line']<=d['end_line'])]
        if len(matches)!=1:return {'status':'definition_unresolved','file':source['file'],'qualname':name}
        found=matches[0]
        return {'status':'mapped',**found,'file':source['file'],'source_sha256':source['sha256'],
                'text':'\n'.join(source['text'][found['first_line']-1:found['end_line']]),
                'evidence':'runtime_code_location_to_static_definition; no line coverage required'}


def build(events_path, resolve_native=True):
    calls={};definitions={};objects={};roots=[];detached=[];gaps=[];start={};end={};stack=[]
    sources=Sources()
    with open(events_path,encoding='utf-8') as stream:
        for line in stream:
            try:e=json.loads(line)
            except ValueError:gaps.append('truncated_event');break
            kind=e['event']
            if kind=='start':start=e
            elif kind=='definition':definitions[e['id']]={**e,'static':sources.locate(e)}
            elif kind=='object':objects[e['id']]=e
            elif kind=='call':
                uid=e['uid'];parent=e['parent'];node={**e,'children':[],'objects':set(),'reference_observations':[],'returned':False}
                calls[uid]=node
                if parent is None:roots.append(uid)
                elif parent in calls and stack and stack[-1]==parent:calls[parent]['children'].append(uid)
                else:detached.append(uid);gaps.append('unverified_parent:'+str(uid))
                stack.append(uid)
            elif kind=='return':
                uid=e['uid']
                if stack and stack[-1]==uid:
                    stack.pop();calls[uid]['returned']=True;calls[uid]['return_time_ns']=e.get('time_ns')
                else:gaps.append('unmatched_return:'+str(uid))
            elif kind=='references' and e['uid'] in calls:
                node=calls[e['uid']]
                node['objects'].update(r[2] for r in e['added'])
                node['reference_observations'].append({k:v for k,v in e.items() if k not in ('event','uid')})
            elif kind=='gap':gaps.append(e['reason'])
            elif kind=='end':end=e;gaps.extend(e['gaps'])
    if not end:gaps.append('missing_collection_end')
    if stack:gaps.append('unclosed_calls')
    for obj in objects.values():
        if obj.get('definition') in definitions:obj['static']=definitions[obj['definition']]['static']
        elif obj.get('owner_file') and (obj.get('static_owner') or {}).get('qualname'):
            obj['static']=sources.locate({'file':obj['owner_file'],**obj['static_owner']})
    for node in calls.values():node['objects']=sorted(node['objects'])
    native=[];binary_hashes={}
    for node in calls.values():
        identity=node.get('native_identity') or {}
        if identity.get('module') and identity.get('module_offset') is not None:
            path=identity['module']
            try:
                stat=os.stat(path)
                if identity.get('module_stamp')!=[stat.st_size,stat.st_mtime_ns]:
                    node['native_source']={'status':'binary_changed_since_observation'};continue
                if path not in binary_hashes:
                    digest=hashlib.sha256()
                    with open(path,'rb') as f:
                        for block in iter(lambda:f.read(1<<20),b''):digest.update(block)
                    binary_hashes[path]=digest.hexdigest()
                native.append({'path':path,'offset':identity['module_offset'],'sha256':binary_hashes[path]})
            except OSError:node['native_source']={'status':'binary_unavailable'}
    if resolve_native and native:
        try:
            from .symbols import resolve
            locations=resolve(native,Path(events_path).parent/'source-cache',timeout=120)
            for node in calls.values():
                identity=node.get('native_identity') or {}
                location=locations.get((identity.get('module'),identity.get('module_offset')))
                if location:node['native_source']=location
        except Exception as exc:gaps.append('native_source_lookup:'+type(exc).__name__)
    static_definitions={}
    for record in list(definitions.values())+list(objects.values()):
        definition=record.pop('static',None)
        if definition:
            key='static:'+hashlib.sha256(json.dumps(definition,sort_keys=True).encode()).hexdigest()[:20]
            static_definitions[key]=definition;record['static_definition']=key
    return {'schema':'object_calltree/1','test':start.get('nodeid'),'run_id':start.get('run_id'),'calls':calls,'roots':roots,'detached':detached,
            'definitions':definitions,'objects':objects,'gaps':sorted(set(gaps)),
            'static_definitions':static_definitions,
            'boundaries':start.get('boundaries',[]),'stats':end,
            'criterion':'change intersects static owner of an observed object/call; changed line execution is not required'}


def affected(tree, filename, line):
    """Coarse ownership matching: an unexecuted branch still belongs to its function."""
    filename=os.path.realpath(filename);hits=[]
    for uid,call in tree['calls'].items():
        candidates=[]
        definition=tree['definitions'].get(call.get('definition'),{})
        if definition:candidates.append(('called_function',tree['static_definitions'].get(definition.get('static_definition'),{})))
        for object_id in call['objects']:
            obj=tree['objects'].get(object_id,{})
            candidates.append(('observed_object',tree['static_definitions'].get(obj.get('static_definition'),{})))
        reasons={kind for kind,s in candidates if s.get('status')=='mapped' and
                 s.get('file')==filename and s['first_line']<=line<=s['end_line']}
        if reasons:hits.append({'test':tree['test'],'call':uid,'reasons':sorted(reasons)})
    return hits


PAGE=r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>调用树</title>
<style>body{font:15px system-ui;background:#f5f6f8;color:#233047;margin:28px}header{position:sticky;top:0;background:#f5f6f8;padding:12px 0;z-index:1}h1{font-size:24px;margin:0 0 8px}.note{color:#64748b;font-size:13px}input,button{font:inherit;padding:7px;border:1px solid #ccd3df;border-radius:5px}li{list-style:none;border-left:1px solid #ccd3df;padding:5px 0 5px 17px}ul{padding-left:20px}summary{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:12px;font-size:12px}.detail{margin:5px 0;color:#526078}.fn{font-family:monospace}.warning{color:#9c4318}</style>
<header><h1>真实测试 · 调用树</h1><div id="test" class="note"></div><p id="stats"></p><p id="capture-status" class="warning"></p><input id="search" placeholder="查找函数，例如 fill_"><button id="find">筛选</button><button id="reset">全部调用</button><p class="note">主线只显示实际函数调用；点击“源码与对象”查看静态定义、调用位置和对象引用。C++ 分支来自编译插桩的实际嵌套和窗口归属；这不等同于直接机器 call 指令。</p></header>
<details><summary>采集范围与未解析项</summary><pre id="limits"></pre></details><ul id="tree"></ul>
<script id="data" type="application/json">__DATA__</script><script>
const data=JSON.parse(document.getElementById('data').textContent),calls=data.calls;
document.getElementById('test').textContent=data.test;
document.getElementById('stats').textContent=`${Object.keys(calls).length} 次调用 · ${data.roots.length} 个入口 · ${data.object_count} 个对象地址/类型记录`;
document.getElementById('capture-status').textContent=data.gaps.length?'采集或解析未完整：'+data.gaps.length+' 项缺口，请展开采集范围查看。':'这是已观测到的调用；适用范围和未覆盖边界见下方说明。';
document.getElementById('limits').textContent=JSON.stringify({gaps:data.gaps,boundaries:data.boundaries},null,2);
const chunks=new Map(),pending=new Map();
window.__callchainChunk=(name,value)=>{chunks.set(name,value);if(pending.has(name))pending.get(name).resolve(value)};
function loadChunk(name){if(chunks.has(name))return Promise.resolve(chunks.get(name));if(pending.has(name))return pending.get(name).promise;let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});pending.set(name,{promise,resolve,reject});const script=document.createElement('script');script.src=name;script.onerror=()=>reject(new Error('无法加载对象证据：'+name));document.head.append(script);return promise}
function detail(node){const d=document.createElement('details');d.className='detail';const s=document.createElement('summary');s.textContent='源码与对象';d.append(s);d.addEventListener('toggle',async()=>{if(!d.open||d.dataset.done)return;d.dataset.done='1';const p=document.createElement('pre');p.textContent='正在加载对象证据…';d.append(p);try{const chunk=await loadChunk(node.chunk),full=chunk.calls[node.uid],def=data.definitions[node.definition];p.textContent=JSON.stringify({call_id:node.uid,returned:node.returned,definition:def,static_definition:def?data.static_definitions[def.static_definition]:null,callsite:{file:node.caller_file,line:node.caller_line},native_binding:full.native_identity,native_source:full.native_source,native_callsite:full.native_callsite,transfer:full.transfer_evidence,relation:full.relation,objects:full.objects.map(id=>chunk.objects[id]),reference_observations:full.reference_observations},null,2);const owners=new Set(full.objects.map(id=>chunk.objects[id]?.static_definition).filter(Boolean));for(const id of owners){const source=data.static_definitions[id],fold=document.createElement('details'),label=document.createElement('summary'),body=document.createElement('pre');label.textContent='对象定义：'+(source.qualname||source.status);body.textContent=JSON.stringify(source,null,2);fold.append(label,body);d.append(fold)}}catch(error){p.textContent=String(error)}});return d}
function render(){const q=document.getElementById('search').value.toLowerCase();const keep=new Set();function mark(id){const n=calls[id];let yes=n.name.toLowerCase().includes(q);for(const child of n.children)if(mark(child))yes=true;if(yes)keep.add(id);return yes}for(const id of [...data.roots,...data.detached])mark(id);const host=document.getElementById('tree');host.replaceChildren();function show(id,parent,depth){if(!keep.has(id))return;const n=calls[id],li=document.createElement('li'),d=document.createElement('details'),s=document.createElement('summary');s.className='fn';s.textContent=n.name+(n.kind==='native_boundary'?(n.native_expanded?' [C++ 已展开]':' [原生入口]'):n.kind==='native_external'?' [外部边界]':'')+(n.returned?'':' [未闭合]');d.open=!!q||depth<1;d.append(s,detail(n));li.append(d);parent.append(li);let filled=false;function children(){if(filled||!d.open)return;filled=true;const ul=document.createElement('ul');d.append(ul);for(const child of n.children)show(child,ul,depth+1)}d.addEventListener('toggle',children);children()}for(const id of data.roots)show(id,host,0);if(data.detached.length){const p=document.createElement('p');p.className='warning';p.textContent='以下片段缺少已验证父调用，不接入主树';host.append(p);for(const id of data.detached)show(id,host,0)}}
document.getElementById('find').onclick=render;document.getElementById('reset').onclick=()=>{document.getElementById('search').value='';render()};document.getElementById('search').onkeydown=e=>{if(e.key==='Enter')render()};document.getElementById('search').value=new URLSearchParams(location.search).get('q')||'';render();</script></html>'''


def render(tree, output, page='C03'):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    if Path(page).name!=page or page in ('','.','..'):raise ValueError('invalid page name')
    serialized=json.dumps(tree,ensure_ascii=False,separators=(',',':'))
    (output/'object-tree.json').write_text(serialized,encoding='utf-8')
    # The human-facing tree need not load every reference observation up front.
    # Small JS data chunks work both on a local server and as adjacent files.
    folder=output/(page+'.data');folder.mkdir(exist_ok=True)
    lightweight={**tree,'calls':{},'objects':{},'object_count':len(tree['objects'])}
    rows=list(tree['calls'].items())
    for offset in range(0,len(rows),128):
        chunk_name=page+'.data/'+str(offset//128)+'.js'
        batch=rows[offset:offset+128];object_ids={oid for _,node in batch for oid in node['objects']}
        chunk={'calls':dict(batch),'objects':{oid:tree['objects'][oid] for oid in object_ids}}
        encoded=json.dumps(chunk,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
        (output/chunk_name).write_text('window.__callchainChunk('+json.dumps(chunk_name)+','+encoded+');',encoding='utf-8')
        for uid,node in batch:
            lightweight['calls'][uid]={k:v for k,v in node.items() if k not in ('objects','reference_observations','native_source','native_identity','native_caller','native_callsite','transfer_evidence')}
            lightweight['calls'][uid]['chunk']=chunk_name
    encoded=json.dumps(lightweight,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    (output/(page+'.html')).write_text(PAGE.replace('__DATA__',encoded),encoding='utf-8')
