import difflib
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from trace_api_chain.changed_objects import identify_changes, source_identity, match_callchains
from trace_api_chain.source_entities import SourceEntities
from trace_api_chain.database import refresh
from trace_api_chain.pipeline import prepare
from trace_api_chain.replay import rebuild, EvidenceError
from test_pytest_entry import launch


def diff(old,new,path='backend.py'):
    return ''.join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),'a/'+path,'b/'+path))


def identify(tmp_path,old,new,path='backend.py'):
    file=tmp_path/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(old,encoding='utf-8')
    return identify_changes(diff(old,new,path),repo=tmp_path,before_root=tmp_path,label='demo')


@pytest.mark.parametrize('old,new,name,kind',[
    ('GPU_TYPES={"cuda"}\n','GPU_TYPES={"cuda","npu"}\n','GPU_TYPES','binding'),
    ('def api():\n    return 1\n','def api():\n    x=1\n    return 1\n','api','function'),
    ('class Backend:\n    def run(self):\n        return 1\n','class Backend:\n    def run(self):\n        return 2\n','Backend.run','function'),
    ('class Backend:\n    MODE=1\n','class Backend:\n    MODE=2\n','Backend.MODE','binding'),
    ('class Backend:\n    pass\n','class Backend(object):\n    pass\n','Backend','type'),
    ('print(1)\n','print(2)\n','<module>','module'),
    ('def api():\n    x=1\n    return 1\n','def api():\n    return 1\n','api','function'),
    ('async def api():\n    return 1\n','async def api():\n    return 2\n','api','function'),
    ('class Backend:\n    @staticmethod\n    def run():\n        return 1\n','class Backend:\n    @classmethod\n    def run():\n        return 1\n','Backend.run','function'),
])
def test_diff_owner_is_source_identity(tmp_path,old,new,name,kind):
    result=identify(tmp_path,old,new)
    assert not result['unresolved']
    assert len(result['objects'])==1
    obj=result['objects'][0]
    assert (obj['qualname'],obj['kind'],obj['change'])==(name,kind,'modified')
    assert obj['source_id']==source_identity('demo/backend.py',kind,name)
    assert set(obj['sides'])=={'old','new'}


def test_source_identity_ignores_runtime_name_and_line_shift(tmp_path):
    file=tmp_path/'backend.py';file.write_text('def api(): return 1\n')
    a,_=SourceEntities({'demo':tmp_path}).python({'file':str(file),'first_line':1,'qualname':'alias'})
    result=identify(tmp_path,file.read_text(),'def api(): return 2\n')
    assert a==result['objects'][0]['source_id']
    file.write_text('\n\ndef api(): return 1\n')
    b,_=SourceEntities({'demo':tmp_path}).python({'file':str(file),'first_line':3,'qualname':'different'})
    assert a==b
    assert a!=source_identity('demo/other.py','function','api')


def test_renamed_definition_has_two_identities(tmp_path):
    result=identify(tmp_path,'def old(): return 1\n','def new(): return 1\n')
    assert {(o['qualname'],o['change']) for o in result['objects']}=={('old','deleted'),('new','added')}


def test_duplicate_definitions_are_ambiguous(tmp_path):
    old='def api(): return 1\ndef api(): return 2\n'
    result=identify(tmp_path,old,old.replace('return 2','return 3'))
    assert result['objects'][0]['status']=='ambiguous'
    assert SourceEntities().python({'file':str(tmp_path/'backend.py'),'first_line':2})==(None,'ambiguous_code_definition')
    result=identify(tmp_path,'def api(): return 1\n','def api(): return 2\ndef api(): return 3\n')
    assert result['objects'][0]['status']=='ambiguous'


def test_wrong_base_invalid_syntax_and_unsupported_are_explicit(tmp_path):
    (tmp_path/'backend.py').write_text('def api(): return 99\n')
    result=identify_changes(diff('def api(): return 1\n','def api(): return 2\n'),repo=tmp_path,before_root=tmp_path,label='demo')
    assert not result['objects'] and 'diff_context_mismatch' in result['unresolved'][0]['reason']
    result=identify(tmp_path,'def api(): return 1\n','def api(: return 2\n')
    assert not result['objects'] and result['unresolved']
    result=identify_changes(diff('a\n','b\n','file.cpp'),repo=tmp_path,before_root=tmp_path,label='demo')
    assert not result['objects'] and result['unresolved']
    with pytest.raises(ValueError,match='unsafe_diff_path'):
        identify_changes('--- a/../secret.py\n+++ b/../secret.py\n',repo=tmp_path,label='demo')


def test_added_deleted_and_renamed_files(tmp_path):
    text='def api(): return 1\n';(tmp_path/'backend.py').write_text(text)
    add='--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+'+text
    result=identify_changes(add,repo=tmp_path,before_root=tmp_path,label='demo')
    assert {o['qualname'] for o in result['objects']}=={'<module>','api'}
    assert all(o['change']=='added' for o in result['objects'])
    delete='--- a/backend.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-'+text
    result=identify_changes(delete,repo=tmp_path,before_root=tmp_path,label='demo')
    assert all(o['change']=='deleted' for o in result['objects'])
    rename='diff --git a/backend.py b/moved.py\nsimilarity index 100%\nrename from backend.py\nrename to moved.py\n'
    result=identify_changes(rename,repo=tmp_path,before_root=tmp_path,label='demo')
    assert len(result['objects'])==4
    assert len({o['source_id'] for o in result['objects']})==4
    copy=rename.replace('rename from','copy from').replace('rename to','copy to')
    result=identify_changes(copy,repo=tmp_path,before_root=tmp_path,label='demo')
    assert result['unresolved'] and not result['objects']


def test_git_base_and_standalone_cli(tmp_path):
    for args in (['init'],['config','user.email','test@example.invalid'],['config','user.name','Test']):
        subprocess.run(['git','-C',str(tmp_path),*args],check=True,capture_output=True)
    old='def api(): return 1\n';(tmp_path/'backend.py').write_text(old)
    subprocess.run(['git','-C',str(tmp_path),'add','.'],check=True)
    subprocess.run(['git','-C',str(tmp_path),'-c','commit.gpgsign=false','commit','-m','base'],check=True,capture_output=True)
    patch=tmp_path/'change.diff';patch.write_text(diff(old,old.replace('1','2')))
    import trace_api_chain.changed_objects as tool
    run=subprocess.run([sys.executable,tool.__file__,str(patch),'--repo',str(tmp_path),'--base','HEAD','--label','demo'],text=True,capture_output=True)
    assert run.returncode==0,run.stderr
    assert json.loads(run.stdout)['objects'][0]['qualname']=='api'


def test_relative_import_origins_are_candidates_and_reassignments_not_guessed(tmp_path):
    config=tmp_path/'config.py';config.write_text('GPU_TYPES={"cuda"}\n')
    consumer=tmp_path/'backend.py';consumer.write_text('from .config import GPU_TYPES as SUPPORTED\n')
    def record(file):
        stat=file.stat()
        return {'file':str(file),'source_stamp':[stat.st_size,stat.st_mtime_ns],'package':'pkg'}
    modules={'pkg.config':record(config)};resolver=SourceEntities({'demo':tmp_path})
    origin=resolver.import_origins(record(consumer),'SUPPORTED',modules)
    assert origin==[source_identity('demo/config.py','binding','GPU_TYPES')]
    old_record=record(config);config.write_text('GPU_TYPES={"cuda","npu"}\n')
    assert SourceEntities({'demo':tmp_path}).named(old_record,'GPU_TYPES')==(None,'namespace_source_unverified_or_changed')
    consumer.write_text('from .config import GPU_TYPES as SUPPORTED\nSUPPORTED=set()\n')
    assert SourceEntities({'demo':tmp_path}).import_origins(record(consumer),'SUPPORTED',{'pkg.config':record(config)})==[]


def test_multiple_files_private_names_and_non_utf8_version(tmp_path):
    old='class Backend:\n    def __consume(self): return 1\n'
    (tmp_path/'backend.py').write_text(old)
    (tmp_path/'other.py').write_text('LIMIT=1\n')
    patch=diff(old,old.replace('return 1','return 2'))+diff('LIMIT=1\n','LIMIT=2\n','other.py')
    result=identify_changes(patch,repo=tmp_path,before_root=tmp_path,label='demo')
    assert {o['qualname'] for o in result['objects']}=={'Backend.__consume','LIMIT'}
    source='# coding: latin-1\nVALUE="é"\n';data=source.encode('latin-1')
    (tmp_path/'latin.py').write_bytes(data)
    result=identify_changes(diff(source,source.replace('é','è'),'latin.py'),repo=tmp_path,before_root=tmp_path,label='demo')
    obj=result['objects'][0]
    assert obj['sides']['old']['source_sha256']==hashlib.sha256(data).hexdigest()
    assert obj['sides']['new']['source_sha256']==hashlib.sha256(source.replace('é','è').encode('latin-1')).hexdigest()


def test_observed_globals_alias_module_attribute_and_method_match_sql(tmp_path):
    config='GPU_TYPES={"cuda"}\nUNUSED_TYPES={"never"}\n'
    backend='''from fixture_config import GPU_TYPES as SUPPORTED_DEVICES
import fixture_config as config
class Backend:
    def run(self,device):
        if device=='never':
            ignored=config.UNUSED_TYPES
        return device in SUPPORTED_DEVICES and device in config.GPU_TYPES
def api(device):
    return Backend().run(device)
'''
    (tmp_path/'config.py').write_text(config);(tmp_path/'backend.py').write_text(backend)
    script='''import sys, types
from pathlib import Path
for name,file in [('fixture_config','config.py'),('fixture_api','backend.py')]:
    module=types.ModuleType(name)
    module.__file__=str(Path(__file__).with_name(file));module.__package__=''
    sys.modules[name]=module
    exec(compile(Path(module.__file__).read_text(),module.__file__,'exec'),module.__dict__)
m=sys.modules['fixture_api'];m.Backend.__qualname__='RuntimeName'
def test_one():
    assert m.api('cuda')
'''
    out=tmp_path/'capture'
    result=launch(tmp_path,script,'--callchain-out='+str(out),'--callchain-mode=objects','--callchain-namespaces=fixture_api')
    assert result.returncode==0,result.stdout+result.stderr
    data=prepare(out,'objects');db=tmp_path/'chains.sqlite'
    summary=refresh(db,data,roots={'demo':tmp_path})
    assert summary['source_dependencies']>0
    changes=identify_changes(diff(config,config.replace('"cuda"','"cuda","npu"'),'config.py'),repo=tmp_path,before_root=tmp_path,label='demo')
    matched=match_callchains(db,changes)['results'][0]
    assert matched['status']=='matched'
    assert {'observed_binding_read','static_import_origin'}<={m['relation'] for m in matched['matches']}
    assert all(m['matching_diff_sides']==['old'] for m in matched['matches'])
    assert any('fixture_api.api' in m['api_aliases'] and m['call_path'][-1]['name']=='Backend.run' for m in matched['matches'])
    unused=identify_changes(diff(config,config.replace('never','other'),'config.py'),repo=tmp_path,before_root=tmp_path,label='demo')
    assert match_callchains(db,unused)['results'][0]['status']=='not_observed_in_current_database'
    methods=identify_changes(diff(backend,backend.replace('return device in','return not device in')),repo=tmp_path,before_root=tmp_path,label='demo')
    assert match_callchains(db,methods)['results'][0]['status']=='matched'
    # Replaying the same evidence is idempotent and malformed reads are rejected.
    assert refresh(db,data,roots={'demo':tmp_path})['status']=='unchanged'
    events=[dict(e) for e in data['events']]
    read=next(e for e in events if e['event']=='source_read');read['uid']=999999
    contract=json.loads(Path(__import__('trace_api_chain').__file__).with_name('contract.json').read_text())
    with pytest.raises(EvidenceError,match='source_read_without_call'):rebuild(events,[],contract)
