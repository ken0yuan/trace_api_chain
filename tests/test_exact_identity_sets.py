import gc
import weakref

from trace_api_chain.frame_identity import graph_signature,new_type_context_cache


def test_many_distinct_nodes_cycle_and_mutation():
    values=[{'child':i} for i in range(16000)]
    values[0]['cycle']=values
    cache=new_type_context_cache()
    first=graph_signature({'value':values},100000,{},cache)
    assert first is not None
    assert graph_signature({'value':values},100000,{},cache)==first
    values[-1]['new_deep_reference']={'child':object()}
    assert graph_signature({'value':values},100000,{},cache)!=first


def test_duplicate_edges_still_consume_the_original_reference_budget():
    child={'value':object()};values=[child]*1000
    assert graph_signature({'value':values},32,{}) is None
    assert graph_signature({'value':values},10000,{}) is not None


def test_membership_sets_do_not_retain_objects():
    class Node:pass
    value=Node();value.self=value;ref=weakref.ref(value)
    cache=new_type_context_cache()
    assert graph_signature({'value':value},1000,{},cache) is not None
    del value;gc.collect()
    assert ref() is None
