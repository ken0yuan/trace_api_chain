import gc
import sys
import types
import weakref

from trace_api_chain import frame_identity as identity


def test_type_reuse_revalidates_code_names_bases_and_owner_file(monkeypatch):
    cache=identity.new_type_context_cache()
    module=types.ModuleType('_callchain_type_cache_test');module.__file__='first.py'
    monkeypatch.setitem(sys.modules,module.__name__,module)
    class Base:
        def method(self):return 1
    class Alternative:
        pass
    class Child(Base):
        pass
    Child.__module__=module.__name__
    def exact():
        context=identity.type_metadata_context(Child,cache)
        assert context==identity.type_metadata_context(Child)
        return context
    original=exact();assert exact()==original
    def replacement(self):return 2
    Base.method.__code__=replacement.__code__
    assert exact()!=original
    previous=exact();module.__file__='second.py';assert exact()!=previous
    previous=exact();Child.__qualname__='Renamed';assert exact()!=previous
    previous=exact();Child.__bases__=(Alternative,);assert exact()!=previous
    Child.extra=staticmethod(replacement);exact()
    stats=identity.type_context_cache_stats(cache)
    assert stats['hits']>0 and stats['invalidations']>=4


def test_type_cache_does_not_keep_classes_functions_or_code_alive():
    cache=identity.new_type_context_cache()
    def observe():
        class Temporary:
            def method(self):return __class__
        function=Temporary.method
        identity.type_metadata_context(Temporary,cache)
        identity.type_metadata_context(Temporary,cache)
        return weakref.ref(Temporary),weakref.ref(function),weakref.ref(function.__code__)
    cls,fn,code=observe();gc.collect()
    assert cls() is None and fn() is None
    # The lexical code may remain in observe.co_consts; the cache owns no strong code.
    assert identity.type_context_cache_stats(cache)['hits']==1


def test_cached_graph_still_detects_new_objects_and_mutated_references():
    cache=identity.new_type_context_cache()
    class Value:
        def method(self):return 1
    value=Value();value.child=[];roots={'value':value}
    first=identity.graph_signature(roots,10000,{},cache)
    assert first==identity.graph_signature(roots,10000,{})
    assert first==identity.graph_signature(roots,10000,{},cache)
    value.child.append(Value())
    second=identity.graph_signature(roots,10000,{},cache)
    assert second!=first
    value.child[0].new_branch={'another_entity':Value()}
    assert identity.graph_signature(roots,10000,{},cache)!=second
    assert identity.type_context_cache_stats(cache)['hits']>0


def test_type_cache_is_bounded_without_hiding_new_type_descriptions():
    cache=identity.new_type_context_cache()
    for index in range(4200):
        cls=type('New'+str(index),(),{})
        assert identity.type_metadata_context(cls,cache)==identity.type_metadata_context(cls)
    assert identity.type_context_cache_stats(cache)['entries']<=4096


def test_cached_signatures_keep_shared_mro_encoding_byte_identical():
    cache=identity.new_type_context_cache()
    class Base:
        def method(self):return 1
    derived=[type('Derived'+str(i),(Base,),{}) for i in range(40)]
    values={'objects':[cls() for cls in derived]}
    for index in range(5):
        assert identity.graph_signature(values,10000,{},cache)==identity.graph_signature(values,10000,{})
        values['objects'][index].new_branch=[derived[-1]()]
    assert identity.type_context_cache_stats(cache)['hits']>100


def test_namespace_cache_detects_in_place_code_change_without_dict_mutation():
    cache=identity.new_type_context_cache()
    class Base:
        def method(self):return 1
    class Child(Base):pass
    values={'object':Child()}
    before=identity.graph_signature(values,10000,{},cache)
    def replacement(self):return 2
    Base.method.__code__=replacement.__code__
    after=identity.graph_signature(values,10000,{},cache)
    assert after!=before
    assert after==identity.graph_signature(values,10000,{})
