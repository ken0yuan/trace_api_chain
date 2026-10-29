import json
import sys
from types import SimpleNamespace

from trace_api_chain import frame_identity
from trace_api_chain.object_capture import Recorder,NativeStorage


def test_native_children_preserve_all_object_and_reference_bytes(tmp_path,monkeypatch):
    class Value:
        def method(self):return 1
    value=Value();value.child=[1,2]
    closure={'new_reference':value}
    def function(arg=value,*,keyword=closure):return closure
    values={'sequence':[value,closure],'mapping':{1:value,'two':closure},'tuple':(value,closure),
            'set':{1,2,3},'function':function,'method':value.method,'builtin':{}.get,'class':Value}
    a=Recorder(tmp_path/'fast.jsonl','one',tmp_path/'test.py')
    b=Recorder(tmp_path/'reference.jsonl','one',tmp_path/'test.py')
    a.stack=b.stack=[{'uid':1}]
    for index in range(4):
        a.snapshot(sys._getframe(),'locals',values)
        with monkeypatch.context() as patch:
            patch.setattr(frame_identity,'reference_children',lambda *args:None)
            b.snapshot(sys._getframe(),'locals',values)
        value.child.append({'new_branch':Value()})
        closure['new_reference']=Value()
    for recorder in [a,b]:recorder.file.close()
    def records(path):
        return [line for line in path.read_text().splitlines() if json.loads(line)['event']!='start']
    assert records(tmp_path/'fast.jsonl')==records(tmp_path/'reference.jsonl')
    assert not a.gaps and not b.gaps


def test_native_children_leave_unsupported_storage_and_properties_to_legacy():
    observed=[]
    class Custom:
        @property
        def danger(self):observed.append(True);raise AssertionError('property executed')
    assert frame_identity.reference_children(Custom(),'parent',NativeStorage) is None
    assert not observed
    def function(*,keyword=1):return keyword
    function.__kwdefaults__={object():1}
    assert frame_identity.reference_children(function,'parent',NativeStorage) is None
    assert frame_identity.reference_children([1,2],'parent',NativeStorage)==[('parent','item:0',1),('parent','item:1',2)]
