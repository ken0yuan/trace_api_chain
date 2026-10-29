from trace_api_chain.object_capture import Recorder


def test_metadata_lru_keeps_valid_entries_and_still_detects_source_changes(tmp_path):
    recorder=Recorder(tmp_path/'events.jsonl','one',tmp_path/'test.py')
    recorder.metadata_cache_limit=2
    def first():return 1
    def second():return 2
    def replacement():return 3
    initial=recorder.metadata(first)
    recorder.metadata(second)
    assert recorder.metadata(first)==initial
    recorder.metadata(replacement)
    assert (id(first),id(type(first))) in recorder.metadata_cache
    assert (id(second),id(type(second))) not in recorder.metadata_cache
    first.__code__=replacement.__code__
    changed=recorder.metadata(first)
    assert changed!=initial
    assert recorder.metadata_cache_hits==1
    assert recorder.metadata_cache_evictions==1
    assert len(recorder.metadata_cache)==2
    recorder.file.close()


def test_metadata_reuse_capacity_matches_existing_observation_budget(tmp_path):
    recorder=Recorder(tmp_path/'events.jsonl','one',tmp_path/'test.py',max_references=1000000)
    assert recorder.metadata_cache_limit==1000000
    assert recorder.max_references==1000000
    recorder.file.close()
