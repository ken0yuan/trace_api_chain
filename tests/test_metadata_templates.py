import json
import sys
import types

from trace_api_chain.object_capture import Recorder


def test_templates_preserve_complete_object_and_reference_events(tmp_path):
    class Shared:
        def method(self):return 1
    optimized=Recorder(tmp_path/'fast.jsonl','test_one',tmp_path/'test.py')
    reference=Recorder(tmp_path/'old.jsonl','test_one',tmp_path/'test.py')
    reference.metadata_template_limit=0
    optimized.stack=reference.stack=[{'uid':1}]
    values={'objects':[Shared() for _ in range(100)],'leaves':[str(i) for i in range(100)],
            'builtins':[{}.get,[].append], 'functions':[Shared.method]}
    def snapshot():
        for recorder in [optimized,reference]:
            recorder.metadata_cache.clear()
            recorder.snapshot(sys._getframe(),'locals',values)
    snapshot()
    values['objects'][0].new_branch={'new_entity':Shared()}
    values['methods']=[obj.method for obj in values['objects']]
    snapshot()
    def replacement(self):return 2
    Shared.method.__code__=replacement.__code__;snapshot()
    Shared.added=staticmethod(replacement);snapshot()
    for recorder in [optimized,reference]:recorder.file.close()
    def events(path):
        return [line for line in path.read_text().splitlines() if json.loads(line)['event']!='start']
    assert events(tmp_path/'fast.jsonl')==events(tmp_path/'old.jsonl')
    assert optimized.metadata_template_hits>100 and not optimized.gaps


def test_modules_with_same_name_still_observe_current_registered_owner_file(tmp_path,monkeypatch):
    recorder=Recorder(tmp_path/'events.jsonl','one',tmp_path/'test.py')
    registered=types.ModuleType('_callchain_template_module');registered.__file__='first.py'
    monkeypatch.setitem(sys.modules,registered.__name__,registered)
    first=types.ModuleType(registered.__name__);first.__file__='alias.py'
    before=recorder.metadata(first)[1]
    registered.__file__='second.py'
    second=types.ModuleType(registered.__name__);second.__file__='alias.py'
    after=recorder.metadata(second)[1]
    assert before['owner_file']=='first.py' and after['owner_file']=='second.py'
    assert recorder.metadata_template_hits==0
    recorder.file.close()
