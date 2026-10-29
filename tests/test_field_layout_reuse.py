import gc
import weakref
from trace_api_chain.frame_identity import graph_signature,new_type_context_cache,field_plan_cache_stats


def checked(value,cache):
    old=graph_signature({'root':value},100000,{})
    new=graph_signature({'root':value},100000,{},cache)
    assert old is not None and new==old
    return new


def test_one_namespace_scan_for_repeated_unchanged_type_layout():
    Wide=type('Wide',(),{'ordinary_'+str(i):i for i in range(1500)})
    obj=Wide();obj.values=[1,2,3];cache=new_type_context_cache()
    initial=checked(obj,cache)
    for _ in range(100):assert graph_signature({'root':obj},100000,{},cache)==initial
    stats=field_plan_cache_stats(cache)
    assert stats['namespace_scans']==1 and stats['hits']==100
    assert stats['entries']==1


def test_instance_changes_are_read_despite_reusing_layout():
    class Value:pass
    obj=Value();obj.nested={'values':[1]};cache=new_type_context_cache();prior=checked(obj,cache)
    for change in (lambda:obj.nested['values'].append(object()),lambda:setattr(obj,'new',[object()]),lambda:obj.nested.pop('values'),lambda:delattr(obj,'new')):
        change();current=checked(obj,cache);assert current!=prior;prior=current
    assert field_plan_cache_stats(cache)['hits']>=4


def test_slot_descriptor_removal_and_restoration_invalidate_layout():
    class Slot:__slots__=('value',)
    obj=Slot();obj.value=[1,2];cache=new_type_context_cache();prior=checked(obj,cache)
    descriptor=Slot.value;del Slot.value
    removed=checked(obj,cache);assert removed!=prior
    Slot.value=descriptor
    assert checked(obj,cache)!=removed
    assert field_plan_cache_stats(cache)['invalidations']>=2


def test_mro_change_and_shadowed_descriptor_are_not_hidden():
    class First:pass
    class Second:pass
    class Item(First):pass
    obj=Item();obj.values=[1];cache=new_type_context_cache();prior=checked(obj,cache)
    Item.__bases__=(Second,)
    assert checked(obj,cache)!=prior
    assert field_plan_cache_stats(cache)['invalidations']>=1


def test_weak_plan_never_retains_workload_class_or_instance():
    cache=new_type_context_cache()
    def create():
        class Temporary:
            __slots__=('value','__weakref__')
        value=Temporary();value.value=[];checked(value,cache)
        return weakref.ref(Temporary),weakref.ref(value)
    cls,obj=create();gc.collect()
    assert obj() is None and cls() is None


def test_metaclass_hash_and_properties_are_never_called():
    class Meta(type):
        def __hash__(self):raise AssertionError('metaclass hash called')
    class Value(metaclass=Meta):
        @property
        def hidden(self):raise AssertionError('property evaluated')
    obj=Value();obj.value=[1];cache=new_type_context_cache()
    checked(obj,cache);checked(obj,cache)
    assert field_plan_cache_stats(cache)['hits']>0


def test_persistent_field_plan_cache_is_bounded():
    cache=new_type_context_cache();classes=[]
    for i in range(4200):
        cls=type('Temporary'+str(i),(),{'__slots__':('value',)})
        classes.append(cls);obj=cls();obj.value=None
        assert graph_signature({'root':obj},100000,{},cache) is not None
    stats=field_plan_cache_stats(cache)
    assert stats['entries']==stats['limit']==4096


def test_old_capsule_layout_is_rejected_before_accessing_memory():
    import ctypes
    import pytest
    factory=ctypes.pythonapi.PyCapsule_New
    factory.argtypes=(ctypes.c_void_p,ctypes.c_char_p,ctypes.c_void_p);factory.restype=ctypes.py_object
    backing=ctypes.c_int(0);legacy_name=b'callchain.exact_type_context/1'
    old=factory(ctypes.addressof(backing),legacy_name,None)
    with pytest.raises(ValueError):field_plan_cache_stats(old)
    with pytest.raises(ValueError):graph_signature({'root':[]},100000,{},old)
