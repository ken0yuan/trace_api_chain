import sys

from trace_api_chain import frame_identity
from trace_api_chain.object_capture import Recorder


def test_shared_base_metadata_preserves_mutations_aliases_and_new_branches(tmp_path, monkeypatch):
    class Base:
        def method(self):
            return 1
    class Left(Base):
        pass
    class Right(Base):
        pass
    left, right = Left(), Right()
    left.child = []
    right.alias = left.child
    values = {'left': left, 'right': right, 'type': Base}
    optimized = Recorder(tmp_path / 'optimized.jsonl', 'test::one', tmp_path / 'test.py')
    reference = Recorder(tmp_path / 'reference.jsonl', 'test::one', tmp_path / 'test.py')
    optimized.stack = reference.stack = [{'uid': 1}]

    def snapshot():
        optimized.snapshot(sys._getframe(), 'locals', values)
        with monkeypatch.context() as patch:
            patch.setattr(frame_identity, 'graph_signature', lambda *args: None)
            patch.setattr(frame_identity, 'flat_sequence_signature', lambda value: None)
            reference.snapshot(sys._getframe(), 'locals', values)

    first = frame_identity.graph_signature(values, 10000, {})
    snapshot()
    snapshot()
    left.child.append({'new_entity': Right()})
    assert frame_identity.graph_signature(values, 10000, {}) != first
    snapshot()
    before = frame_identity.graph_signature(values, 10000, {})
    def replacement(self):
        return 2
    Base.method.__code__ = replacement.__code__
    assert frame_identity.graph_signature(values, 10000, {}) != before
    snapshot()
    right.alias = {'another_branch': left.child}
    snapshot()
    Base.extra = replacement
    snapshot()
    for recorder in [optimized, reference]:
        recorder.file.close()
    import json
    def events(path):
        return [row for line in path.read_text().splitlines()
                if (row := json.loads(line))['event'] in ['object', 'references']]
    assert events(tmp_path / 'optimized.jsonl') == events(tmp_path / 'reference.jsonl')
    assert not optimized.gaps


def test_native_identity_reuses_loader_but_resolves_each_callable(monkeypatch):
    from trace_api_chain import native
    original = native.ctypes.CDLL
    loaded = []
    def load(path):
        loaded.append(path)
        return original(path)
    monkeypatch.setattr(native, '_identity_api', None)
    monkeypatch.setattr(native.ctypes, 'CDLL', load)
    first = native.native_identity(len)
    second = native.native_identity(len)
    other = native.native_identity({}.get)
    assert first == second
    assert first['func_ptr'] != other['func_ptr']
    assert first['module'] and other['module']
    assert loaded == [None]
