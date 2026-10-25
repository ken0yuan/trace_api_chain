import json
from pathlib import Path
import sqlite3
import pytest
from trace_api_chain.source_entities import SourceEntities
from trace_api_chain.database import refresh,query,render_api
from trace_api_chain.pipeline import prepare
from test_pytest_entry import launch,PRELUDE


def capture(tmp_path,body):
    out=tmp_path/'capture'
    r=launch(tmp_path,PRELUDE+body,'--callchain-out='+str(out),'--callchain-mode=objects','--callchain-namespaces=fixture_api')
    assert r.returncode==0,r.stdout+r.stderr
    return prepare(out,'objects')


def test_aliases_instances_merge_to_source_and_sql_only_html(tmp_path):
    data=capture(tmp_path,"m.Backend.__qualname__='RuntimeBackend'\nm.exported=m.Backend.run\ndef test_one():\n backend.run(1)\n m.Backend().run(2)\n")
    db=tmp_path/'all.sqlite'
    first=refresh(db,data);second=refresh(db,data)
    assert first['status']=='refreshed' and second['status']=='unchanged'
    conn=sqlite3.connect(db)
    assert conn.execute("SELECT count(*) FROM entities WHERE kind='type' AND name='Backend'").fetchone()[0]==1
    assert conn.execute("SELECT count(*) FROM entities WHERE kind='function' AND name='Backend.run'").fetchone()[0]==1
    assert conn.execute("SELECT count(*) FROM aliases WHERE alias='RuntimeBackend'").fetchone()[0]>=1
    aliases=conn.execute("SELECT DISTINCT api_id FROM api_aliases WHERE alias IN ('fixture_api.exported','fixture_api.Backend.run')").fetchall()
    assert len(aliases)==1
    conn.close()
    # Rendering must survive removal of every source/capture input.
    data['output'].rename(tmp_path/'raw-moved')
    (tmp_path/'backend.py').unlink()
    report=render_api(db,'fixture_api.Backend.run',tmp_path/'html')
    assert report['paths']>=3
    assert 'Backend.run' in (tmp_path/'html/C03.html').read_text()


def test_same_name_different_source_does_not_merge(tmp_path):
    resolver=SourceEntities()
    ids=[]
    for name in ('one.py','two.py'):
        p=tmp_path/name;p.write_text('def api(): return 1\n')
        eid,reason=resolver.python({'file':str(p),'first_line':1,'qualname':'changed_runtime_name'})
        assert not reason;ids.append(eid)
    assert len(set(ids))==2


def test_changed_source_not_silently_rebound(tmp_path):
    p=tmp_path/'api.py';p.write_text('def api(): return 1\n');s=p.stat()
    p.write_text('def different(): return 999\n')
    eid,reason=SourceEntities().python({'file':str(p),'first_line':1,'source_stamp':[s.st_size,s.st_mtime_ns]})
    assert eid is None and reason=='source_changed_since_capture'


def test_api_context_does_not_mix_shared_helper_branches(tmp_path):
    extra='''source2="""def a(): return 1
def b(): return 2
def helper(which): return a() if which else b()
def api_a(): return helper(True)
def api_b(): return helper(False)
"""
p2=Path(__file__).with_name('branches.py');p2.write_text(source2)
exec(compile(source2,str(p2),'exec'),m.__dict__)
def test_one():
    m.api_a()
    m.api_b()
'''
    data=capture(tmp_path,extra);db=tmp_path/'all.sqlite';refresh(db,data)
    qa=query(db,'fixture_api.api_a');qb=query(db,'fixture_api.api_b')
    assert 'a' in {e['name'] for e in qa['entities'].values()}
    assert 'b' not in {e['name'] for e in qa['entities'].values()}
    assert 'b' in {e['name'] for e in qb['entities'].values()}
    assert 'a' not in {e['name'] for e in qb['entities'].values()}


def test_failed_refresh_rolls_back_previous_graph(tmp_path):
    data=capture(tmp_path,'def test_one():\n backend.run(1)\n');db=tmp_path/'all.sqlite';refresh(db,data)
    before=query(db,'fixture_api.Backend.run')
    # Cause an insertion error after slot replacement has begun.
    conn=sqlite3.connect(db);conn.execute("CREATE TRIGGER reject_new BEFORE INSERT ON path_support BEGIN SELECT RAISE(ABORT,'forced'); END");conn.commit();conn.close()
    data['manifest']['sha256']['test-change']='forces-new-transaction'
    with pytest.raises(sqlite3.IntegrityError):refresh(db,data)
    assert query(db,'fixture_api.Backend.run')==before


def test_cpp_same_declaration_merges_but_overloads_do_not(tmp_path):
    p=tmp_path/'api.cpp';p.write_text('void api(int) {} void api(double) {}\n')
    s=SourceEntities();ident={'path':'/libx.so','sha256':'a','offset':1}
    definition={'function':{'file':str(p),'line':1,'kind':'definition','name':'api','linkage_name':'_Z3apii'}}
    a=s.cpp(ident,definition,{'frames':[{'symbol':'runtime_alias_a'}]})
    b=s.cpp({**ident,'offset':2},definition,{'frames':[{'symbol':'runtime_alias_b'}]})
    assert a==b
    definition['function']['linkage_name']='_Z3apid'
    assert s.cpp(ident,definition,{})!=a


def test_refresh_retires_alias_and_preserves_other_slot(tmp_path):
    data=capture(tmp_path,"m.old_alias=m.Backend.run\ndef test_one():\n backend.run(1)\n")
    db=tmp_path/'all.sqlite'
    refresh(db,data,slot='first');refresh(db,data,slot='second')
    for event in data['events']:
        if event.get('api_aliases'):event['api_aliases']=[a for a in event['api_aliases'] if a.removeprefix('runtime:')!='fixture_api.old_alias']
    data['manifest']['sha256']['revision']='alias-removed'
    refresh(db,data,slot='first')
    assert query(db,'fixture_api.old_alias')['nodes']  # Other slot still supports the alias.
    refresh(db,data,slot='second')
    with pytest.raises(ValueError,match='API not present'):query(db,'fixture_api.old_alias')
    assert query(db,'fixture_api.Backend.run')['nodes']


def test_source_change_marks_other_test_evidence_stale(tmp_path):
    data=capture(tmp_path,'def test_one():\n backend.run(1)\n')
    db=tmp_path/'all.sqlite'
    refresh(db,data,slot='first');refresh(db,data,slot='second')
    data['output'].rename(tmp_path/'old-raw')
    out=tmp_path/'capture'
    changed=PRELUDE.replace('return x+1','return x+20')+'def test_one():\n backend.run(1)\n'
    result=launch(tmp_path,changed,'--callchain-out='+str(out),'--callchain-mode=objects','--callchain-namespaces=fixture_api')
    assert result.returncode==0,result.stdout+result.stderr
    refresh(db,prepare(out,'objects'),slot='first')
    conn=sqlite3.connect(db)
    assert dict(conn.execute('SELECT id,status FROM capture_slots'))=={'first':'current','second':'stale'}
    conn.close()
    assert [e['id'] for e in query(db,'fixture_api.Backend.run')['evidence']]==['first']
