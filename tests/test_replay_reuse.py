"""Replay reuse must preserve source evidence and every stored SQL row."""
import json
import sqlite3

from trace_api_chain import database
from trace_api_chain.source_entities import SourceEntities,source_identity,key


class UncachedSourceEntities(SourceEntities):
    """Independent pre-reuse source projection, retained as an output oracle."""
    def entity(self,source,definition,language,evidence):
        eid=source_identity(source['path'],definition['kind'],definition['name'])
        self.entities[eid]={'id':eid,'path':source['path'],'kind':definition['kind'],'name':definition['name'],
                            'ordinal':definition.get('ordinal',0),'line':definition['line'],'end_line':definition['end_line'],
                            'source_sha256':source['sha256'],'language':language,'status':'mapped','evidence':evidence,
                            'source_text':'\n'.join(source['content'].splitlines()[definition['line']-1:definition['end_line']])}
        return eid

    def containers(self,eid):
        entity=self.entities[eid]
        if entity['language']!='python' or not entity['path']:return []
        source=next((s for s in self.files.values() if s and s['path']==entity['path']),None)
        if source is None:return []
        return [self.entity(source,d,'python','lexical_source_container') for d in source['definitions']
                if (d['kind']=='module' and entity['kind']!='module') or
                (d['kind'] in ('type','function') and entity['name'].startswith(d['name']+'.') and not d.get('ambiguous') and not d.get('declaration_only'))]


def test_container_reuse_restores_latest_evidence_after_binding_reads(tmp_path):
    path=tmp_path/'backend.py'
    path.write_text('class Backend:\n def api(self):\n  return 1\n',encoding='utf-8')
    resolver=SourceEntities();source=resolver.load(path)
    child,reason=resolver.python({'file':str(path),'first_line':2})
    assert reason is None
    first=resolver.containers(child)
    owner,_=resolver.named({'file':str(path),'source_stamp':source['stamp']},'Backend')
    assert resolver.entities[owner]['evidence']=='observed_namespace_binding'
    second=resolver.containers(child)
    assert first==second and first is not second
    assert resolver.entities[owner]['evidence']=='lexical_source_container'
    second.clear()
    assert resolver.containers(child)==first


def prepared_graph(tmp_path,repeats=300):
    path=tmp_path/'backend.py'
    path.write_text('class Backend:\n def api(self):\n  return self.helper()\n\n def helper(self):\n  return 1\n',encoding='utf-8')
    stat=path.stat();stamp=[stat.st_size,stat.st_mtime_ns]
    namespace={'file':str(path),'source_stamp':stamp,'module':'fixture_api','package':''}
    events=[{'event':'start','test_file':str(tmp_path/'test.py'),'namespaces':['fixture_api']},
            {'event':'definition','id':'api','file':str(path),'first_line':2,'source_stamp':stamp},
            {'event':'definition','id':'helper','file':str(path),'first_line':5,'source_stamp':stamp},
            {'event':'object','id':'function','kind':'function','definition':'helper'},
            {'event':'object','id':'receiver','kind':'instance','class_source_hints':[{'file':str(path),'first_line':2,'source_stamp':stamp}],
             'static_owner':{'module':'fixture_api','qualname':'Backend'}}]
    nodes={};edges=[];observations=[]
    for uid in range(1,repeats+2):
        api=uid==1;parent=None if api else 'p:1'
        call={'event':'call','uid':uid,'definition':'api' if api else 'helper',
              'name':'Backend.api' if api else 'Backend.helper',
              'api_aliases':['runtime:fixture_api.Backend.api'] if api else (['runtime:fixture_api.exported_helper'] if uid%7==0 else [])}
        events.append(call)
        node='p:'+str(uid);nodes[node]={'id':node,'kind':'python','parent':parent}
        if parent is not None:edges.append({'from':parent,'to':node,'kind':'observed_python_nesting'})
        observations.append({'observation':{'uid':uid,'scope':'frame','phase':'entry',
            'added':[[None,'root:receiver','receiver'],[None,'root:function','function']], 'removed':[]}})
    graph={'test':'test::api','nodes':nodes,'edges':edges,'gaps':[],
           'reference_observations':observations,'source_reads':[{'uid':1,'namespace':namespace,'name':'Backend',
               'scope':'global','file':str(path),'line':2,'offset':0}],
           'module_sources':{'fixture_api':namespace},'source_read_policy':'namespace-load/1'}
    output=tmp_path/'capture';output.mkdir();(output/'graph.json').write_text(json.dumps(graph),encoding='utf-8')
    return {'output':output,'graph':graph,'events':events,'native':[],
            'manifest':{'sha256':{'fixture':'unchanged'}},'mode':'objects',
            'receipt':{'source_roots':{'fixture':str(tmp_path)}}}


def sql_rows(path):
    with sqlite3.connect(path) as connection:
        tables=[row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table:sorted(connection.execute('SELECT * FROM "'+table+'"').fetchall(),key=repr) for table in tables}


def test_replay_reuse_keeps_all_database_rows_and_identity_formulas(tmp_path,monkeypatch):
    prepared=prepared_graph(tmp_path)
    legacy=tmp_path/'legacy.sqlite';optimized=tmp_path/'optimized.sqlite'
    with monkeypatch.context() as patch:
        patch.setattr(database,'SourceEntities',UncachedSourceEntities)
        before=database.refresh(legacy,prepared,cache_mib=1)
    after=database.refresh(optimized,prepared,cache_mib=1)
    assert before==after
    assert sql_rows(legacy)==sql_rows(optimized)
    assert database.query(legacy,'fixture_api.Backend.api')==database.query(optimized,'fixture_api.Backend.api')
    with sqlite3.connect(optimized) as connection:
        for api,entity in connection.execute('SELECT id,entity_id FROM apis'):
            assert api==key(['api',entity])
        for identity,api,parent,entity,relation in connection.execute('SELECT id,api_id,parent_id,entity_id,edge_kind FROM paths'):
            assert identity==key([api,parent,entity,relation])
        assert connection.execute('SELECT count(*) FROM source_dependencies').fetchone()[0]>0
        assert not connection.execute('PRAGMA foreign_key_check').fetchall()
