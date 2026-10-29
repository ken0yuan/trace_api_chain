import os
from pathlib import Path
import pytest
from trace_api_chain.source_entities import SourceEntities


def fixture(path):
    path.write_text('class Alpha:\n def one(self): pass\n def two(self): pass\n\nclass Beta:\n def other(self): pass\n')
    s=path.stat();return [s.st_size,s.st_mtime_ns]


def test_exact_class_proofs_reuse_resolution_without_losing_last_evidence(tmp_path):
    file=tmp_path/'backend.py';stamp=fixture(file)
    resolver=SourceEntities({'fixture':tmp_path},immutable_sources=True)
    record={'class_source_hints':[{'file':str(file),'first_line':line,'source_stamp':stamp} for line in (2,3)]}
    first=resolver.instance(record);assert first[1] is None
    resolver.named({'file':str(file),'source_stamp':stamp},'Alpha')
    assert resolver.entities[first[0]]['evidence']=='observed_namespace_binding'
    assert resolver.instance(record)==first
    assert resolver.entities[first[0]]['evidence']=='class_own_method_locations'
    mixed={'class_source_hints':record['class_source_hints']+[{'file':str(file),'first_line':6,'source_stamp':stamp}]}
    assert resolver.instance(mixed)==(None,'ambiguous_class_owner')
    beta=resolver.instance({'class_source_hints':[{'file':str(file),'first_line':6,'source_stamp':stamp}]})
    assert beta[0]!=first[0] and beta[1] is None
    resolver.verify_sources()


def test_immutable_resolution_refuses_changed_alias_even_with_identical_bytes(tmp_path):
    first=tmp_path/'a.py';stamp=fixture(first);second=tmp_path/'b.py';second.write_bytes(first.read_bytes())
    alias=tmp_path/'source.py';alias.symlink_to(first)
    resolver=SourceEntities(immutable_sources=True);source=resolver.load(alias)
    assert source['filename']==str(first)
    alias.unlink();alias.symlink_to(second)
    with pytest.raises(ValueError,match='source_alias_changed'):resolver.verify_sources()
    assert resolver.load(alias) is source  # No mixed source enters this snapshot.
    live=SourceEntities();assert live.load(alias)['filename']==str(second)


def test_source_bytes_are_rechecked_even_when_modification_time_is_restored(tmp_path):
    file=tmp_path/'source.py';fixture(file);before=file.stat()
    resolver=SourceEntities(immutable_sources=True);resolver.load(file)
    file.write_text(file.read_text().replace('Alpha','Gamma'))
    os.utime(file,ns=(before.st_atime_ns,before.st_mtime_ns))
    with pytest.raises(ValueError,match='normalization_source_changed'):resolver.verify_sources()


def test_missing_source_is_not_silently_replaced_during_normalization(tmp_path):
    file=tmp_path/'missing.py';resolver=SourceEntities(immutable_sources=True)
    assert resolver.load(file) is None
    fixture(file)
    with pytest.raises(ValueError,match='normalization_source_changed'):resolver.verify_sources()
