from trace_api_chain.changed_objects import index_python,source_identity
from trace_api_chain.source_entities import SourceEntities
import pytest


@pytest.mark.parametrize('imports,decorator',[
    ('from typing import overload','overload'),
    ('from typing import overload as declared','declared'),
    ('import typing as t','t.overload'),
    ('import typing_extensions as t','t.overload'),
])
def test_overload_declarations_share_unique_runtime_definition(tmp_path,imports,decorator):
    text=f'{imports}\n@{decorator}\ndef api(x: int) -> int: ...\n@{decorator}\ndef api(x: str) -> str: ...\ndef api(x): return x\n'
    defs=[d for d in index_python(text) if d['name']=='api']
    assert [d['declaration_only'] for d in defs]==[True,True,False]
    assert not any(d['ambiguous'] for d in defs)
    path=tmp_path/'backend.py';path.write_text(text)
    resolver=SourceEntities({'demo':tmp_path})
    eid,reason=resolver.python({'file':str(path),'first_line':6})
    assert not reason and eid==source_identity('demo/backend.py','function','api')
    assert resolver.entities[eid]['line']==6
    assert resolver.python({'file':str(path),'first_line':2})[0] is None


@pytest.mark.parametrize('prefix',[
    'def overload(fn): return fn\n',
    'from typing import overload\noverload=lambda fn: fn\n',
])
def test_custom_or_rebound_decorator_does_not_hide_duplicate_definition(prefix):
    text=prefix+'@overload\ndef api(x): ...\ndef api(x): return x\n'
    assert all(d['ambiguous'] for d in index_python(text) if d['name']=='api')


def test_class_overload_and_missing_implementation():
    text='from typing import overload\nclass A:\n    @overload\n    def run(self,x:int): ...\n    def run(self,x): return x\n'
    assert not any(d['ambiguous'] for d in index_python(text) if d['name']=='A.run')
    assert next(d for d in index_python('from typing import overload\n@overload\ndef api(x): ...\n') if d['name']=='api')['ambiguous']
