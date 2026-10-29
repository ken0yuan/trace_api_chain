import hashlib
import importlib
import json
from pathlib import Path
import shutil
import sys
import types

import pytest
import trace_api_chain
from trace_api_chain.handoff_compat import FrozenHandoffLoader
from trace_api_chain.writer_session import WriterSession
from test_database import capture


def frozen_fixture(tmp_path):
    runtime=tmp_path/'frozen-runtime';package=runtime/'trace_api_chain'
    shutil.copytree(Path(trace_api_chain.__file__).parent,package,ignore=shutil.ignore_patterns('__pycache__'))
    replay=package/'replay.py';replay.write_bytes(replay.read_bytes()+b'\n# independently frozen implementation\n')
    source=package/'source_entities.py';source.write_bytes(source.read_bytes()+b'\n# independently pinned source resolver\n')
    namespace='fixture_frozen_'+tmp_path.name.replace('-','_')
    root=types.ModuleType(namespace);root.__path__=[str(package)];root.__version__='frozen-test';sys.modules[namespace]=root
    staged=importlib.import_module(namespace+'.staged')
    native_hash=hashlib.sha256((package/'native.py').read_bytes()).hexdigest()
    return runtime,staged,native_hash


def test_known_frozen_reader_keeps_all_strict_handoff_checks(tmp_path):
    data=capture(tmp_path,'def test_one():\n backend.run(1)\n')
    runtime,old,native_hash=frozen_fixture(tmp_path)
    target=tmp_path/'old-plan.json';old.prepare_file(data['output'],target,mode='objects',slot='one')
    loader=FrozenHandoffLoader(runtime,old.policy(),native_hash)
    session=WriterSession(tmp_path/'graph.sqlite')
    try:
        result=session.commit_file(target,loader=loader)
        assert result['gaps']==[] and result['status']=='refreshed'
        assert session.commit_file(target,loader=loader)['status']=='unchanged'
        plan=target.read_bytes();target.write_bytes(plan+b' ')
        with pytest.raises(ValueError,match='checksum_mismatch'):loader(target)
    finally:session.close()


@pytest.mark.parametrize('damage',['policy','native','reader'])
def test_unknown_or_modified_frozen_reader_is_never_executed(tmp_path,damage):
    data=capture(tmp_path,'def test_one():\n backend.run(1)\n')
    runtime,old,native_hash=frozen_fixture(tmp_path);target=tmp_path/'old-plan.json'
    old.prepare_file(data['output'],target,mode='objects',slot='one');loader=FrozenHandoffLoader(runtime,old.policy(),native_hash)
    marker=tmp_path/'initializer-executed'
    (runtime/'trace_api_chain/__init__.py').write_text('raise AssertionError("frozen initializer must never execute")')
    if damage=='policy':
        p=target.with_name(target.name+'.ready.json');receipt=json.loads(p.read_text());receipt['policy']={};p.write_text(json.dumps(receipt))
    else:
        name='native.py' if damage=='native' else 'staged.py';p=runtime/'trace_api_chain'/name;p.write_bytes(p.read_bytes()+b'\n# changed\n')
    with pytest.raises(ValueError,match='frozen_handoff_'):loader(target)
    assert not marker.exists()
