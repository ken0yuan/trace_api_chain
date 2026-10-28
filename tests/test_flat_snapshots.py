import json
import sys
from types import SimpleNamespace
import pytest

from trace_api_chain.object_capture import Recorder
import trace_api_chain.frame_identity as identity


def test_flat_sequence_deltas_equal_uncached_observations_through_mutations(tmp_path,monkeypatch):
    values=list(range(80));box=SimpleNamespace(values=values,alias=values)
    optimized=Recorder(tmp_path/'optimized.jsonl','test::one',tmp_path/'test.py')
    reference=Recorder(tmp_path/'reference.jsonl','test::one',tmp_path/'test.py')
    optimized.stack=[{'uid':1}];reference.stack=[{'uid':1}]
    signature=identity.flat_sequence_signature
    def snapshot():
        optimized.snapshot(sys._getframe(),'locals',{'box':box})
        with monkeypatch.context() as patch:
            patch.setattr(identity,'flat_sequence_signature',lambda value:None)
            patch.setattr(identity,'graph_signature',lambda *args:None)
            reference.snapshot(sys._getframe(),'locals',{'box':box})
    snapshot();snapshot()
    values[30]='replacement';snapshot()
    values.append(None);snapshot()
    nested={'child':[1,2]};values[0]=nested;snapshot()
    nested['child'].append(3);snapshot()
    values[0]=0;snapshot()
    values[:]=[];snapshot()
    box.alias=tuple(range(90));snapshot()
    for recorder in (optimized,reference):recorder.file.close()
    def deltas(path):
        return [row for line in path.read_text().splitlines() if (row:=json.loads(line))['event']=='references']
    assert deltas(tmp_path/'optimized.jsonl')==deltas(tmp_path/'reference.jsonl')
    assert not optimized.gaps


def test_signature_rejects_subclasses_and_mutable_descendants():
    class L(list):pass
    assert identity.flat_sequence_signature(L(range(80))) is None
    assert identity.flat_sequence_signature([[]]*80) is None
    values=list(range(80));first=identity.flat_sequence_signature(values)
    values[20]=None
    assert identity.flat_sequence_signature(values)!=first


def test_graph_validation_reuses_only_identical_nested_reference_states(tmp_path,monkeypatch):
    class Box:
        __slots__=('slot','__dict__')
        def method(self):pass
    class Other:pass
    box=Box();box.slot=[];box.mapping={'child':[1,2]};box.alias=box.mapping;box.cycle=box
    captured=[]
    def factory():
        def function(default=1):return captured,default
        return function
    function=factory()
    values={'box':box,'function':function,'method':box.method,'native':len,'type':Box}
    optimized=Recorder(tmp_path/'optimized-graph.jsonl','test::one',tmp_path/'test.py')
    reference=Recorder(tmp_path/'reference-graph.jsonl','test::one',tmp_path/'test.py')
    optimized.stack=[{'uid':1}];reference.stack=[{'uid':1}]
    first=identity.graph_signature(values,100000,{})
    assert isinstance(first,bytes)
    def snapshot():
        optimized.snapshot(sys._getframe(),'locals',values)
        with monkeypatch.context() as patch:
            patch.setattr(identity,'flat_sequence_signature',lambda value:None)
            patch.setattr(identity,'graph_signature',lambda *args:None)
            reference.snapshot(sys._getframe(),'locals',values)
    snapshot();snapshot()
    box.mapping['child'].append(3);snapshot()
    assert identity.graph_signature(values,100000,{})!=first
    box.slot.append(Other());snapshot()
    del box.slot;snapshot()
    box.mapping={'rebound':4};snapshot()
    captured.append([5]);snapshot()
    function.__defaults__=(9,);snapshot()
    Box.__qualname__='RenamedBox';snapshot()
    del box.alias;snapshot()
    for recorder in (optimized,reference):recorder.file.close()
    def references(path):
        return [row for line in path.read_text().splitlines() if (row:=json.loads(line))['event']=='references']
    assert references(tmp_path/'optimized-graph.jsonl')==references(tmp_path/'reference-graph.jsonl')
    assert not optimized.gaps


def test_ast_storage_is_observed_and_reused_independently_of_other_locals(tmp_path,monkeypatch):
    import ast
    tree=ast.parse('def function(value):\n return value+1\n')
    assert isinstance(identity.graph_signature({'tree':tree},100000,{}),bytes)
    optimized=Recorder(tmp_path/'optimized-ast.jsonl','test::one',tmp_path/'test.py')
    reference=Recorder(tmp_path/'reference-ast.jsonl','test::one',tmp_path/'test.py')
    optimized.stack=[{'uid':1}];reference.stack=[{'uid':1}]
    original=optimized.snapshot_root;visits=[]
    def observe(value):
        if value is tree:visits.append(1)
        return original(value)
    optimized.snapshot_root=observe
    def snapshot(counter):
        values={'tree':tree,'counter':counter}
        optimized.snapshot(sys._getframe(),'locals',values)
        with monkeypatch.context() as patch:
            patch.setattr(identity,'flat_sequence_signature',lambda value:None)
            patch.setattr(identity,'graph_signature',lambda *args:None)
            reference.snapshot(sys._getframe(),'locals',values)
    for counter in range(8):snapshot(counter)
    assert len(visits)==1
    tree.body[0].body.append(ast.Pass());snapshot(8)
    assert len(visits)==2
    for recorder in (optimized,reference):recorder.file.close()
    def references(path):
        return [row for line in path.read_text().splitlines() if (row:=json.loads(line))['event']=='references']
    assert references(tmp_path/'optimized-ast.jsonl')==references(tmp_path/'reference-ast.jsonl')


def test_primitive_metadata_source_change_invalidates_reused_graph(tmp_path,monkeypatch):
    import builtins
    values=list(range(80))
    first=identity.flat_sequence_signature(values)
    recorder=Recorder(tmp_path/'primitive-owner.jsonl','test::one',tmp_path/'test.py');recorder.stack=[{'uid':1}]
    recorder.snapshot(sys._getframe(),'locals',{'values':values})
    monkeypatch.setattr(builtins,'__file__',str(tmp_path/'builtin-source.py'),raising=False)
    assert identity.flat_sequence_signature(values)!=first
    recorder.snapshot(sys._getframe(),'locals',{'values':values});recorder.file.close()
    rows=[json.loads(line) for line in (tmp_path/'primitive-owner.jsonl').read_text().splitlines()]
    assert any(r['event']=='object' and r.get('owner_file')==str(tmp_path/'builtin-source.py') for r in rows)
    assert not recorder.gaps


def test_partial_and_builtin_numeric_storage_preserve_references(tmp_path,monkeypatch):
    import functools,re
    values=[1,2];partial=functools.partial(len,values);pattern=re.compile('value')
    roots={'partial':partial,'pattern':pattern}
    assert isinstance(identity.graph_signature(roots,100000,{}),bytes)
    optimized=Recorder(tmp_path/'optimized-storage.jsonl','test::one',tmp_path/'test.py')
    reference=Recorder(tmp_path/'reference-storage.jsonl','test::one',tmp_path/'test.py')
    optimized.stack=[{'uid':1}];reference.stack=[{'uid':1}]
    for change in (False,False,True):
        if change:values.append(3)
        optimized.snapshot(sys._getframe(),'locals',roots)
        with monkeypatch.context() as patch:
            patch.setattr(identity,'flat_sequence_signature',lambda value:None)
            patch.setattr(identity,'graph_signature',lambda *args:None)
            reference.snapshot(sys._getframe(),'locals',roots)
    for recorder in (optimized,reference):recorder.file.close()
    def references(path):
        return [row for line in path.read_text().splitlines() if (row:=json.loads(line))['event']=='references']
    assert references(tmp_path/'optimized-storage.jsonl')==references(tmp_path/'reference-storage.jsonl')
    assert not optimized.gaps


def test_unboxed_file_fields_are_retained_without_manufacturing_object_changes(tmp_path,monkeypatch):
    import io
    with io.FileIO(tmp_path/'file.bin','w+') as stream:
        roots={'first':[stream],'second':stream,'third':stream.read}
        optimized=Recorder(tmp_path/'optimized-file.jsonl','test::one',tmp_path/'test.py')
        reference=Recorder(tmp_path/'reference-file.jsonl','test::one',tmp_path/'test.py')
        optimized.stack=[{'uid':1}];reference.stack=[{'uid':1}]
        for repeat in range(3):
            optimized.snapshot(sys._getframe(),'locals',roots)
            with monkeypatch.context() as patch:
                patch.setattr(identity,'flat_sequence_signature',lambda value:None)
                patch.setattr(identity,'graph_signature',lambda *args:None)
                reference.snapshot(sys._getframe(),'locals',roots)
        for recorder in (optimized,reference):recorder.file.close()
        def rows(path):return [json.loads(line) for line in path.read_text().splitlines()]
        left=rows(tmp_path/'optimized-file.jsonl');right=rows(tmp_path/'reference-file.jsonl')
        assert [r for r in left if r['event']=='references']==[r for r in right if r['event']=='references']
        fields=[r for r in left if r['event']=='object' and r.get('kind')=='storage_member']
        assert any(r['declaration']['name']=='_blksize' and r['type']['qualname']=='int' for r in fields)
        assert not optimized.gaps and not reference.gaps


def test_metadata_reuse_validates_type_module_and_exact_callable_changes(tmp_path,monkeypatch):
    import types
    recorder=Recorder(tmp_path/'metadata-cache.jsonl','test::one',tmp_path/'test.py')
    module=types.ModuleType('metadata_fixture');module.__file__='first.py'
    monkeypatch.setitem(sys.modules,'metadata_fixture',module)
    class Box:pass
    Box.__module__='metadata_fixture';value=Box()
    first=recorder.metadata(value)
    assert recorder.metadata(value) is first
    Box.__qualname__='Renamed'
    second=recorder.metadata(value)
    assert second[0]!=first[0] and second[1]['type']['qualname']=='Renamed'
    module.__file__='second.py'
    assert recorder.metadata(value)[1]['owner_file']=='second.py'
    def one():pass
    def two():pass
    original=recorder.metadata(one)
    one.__code__=two.__code__
    assert recorder.metadata(one)[1]['definition']!=original[1]['definition']
    recorder.file.close()


def test_alias_roots_reuse_complete_first_visit_state(tmp_path,monkeypatch):
    child={'values':[1,2]};outer={'child':child}
    recorder=Recorder(tmp_path/'aliases.jsonl','test::one',tmp_path/'test.py');recorder.stack=[{'uid':1}]
    original=recorder.snapshot_root;visits=[]
    def observe(value):
        visits.append(id(value));return original(value)
    recorder.snapshot_root=observe
    roots={'first':child,'outer':outer}
    recorder.snapshot(sys._getframe(),'locals',roots)
    first=len(visits)
    recorder.snapshot(sys._getframe(),'locals',roots)
    assert len(visits)==first
    child['values'].append(3)
    recorder.snapshot(sys._getframe(),'locals',roots)
    assert len(visits)>first and not recorder.gaps
    recorder.file.close()


def test_root_reuse_cache_is_bounded_and_eviction_keeps_reference_deltas(tmp_path):
    recorder=Recorder(tmp_path/'bounded.jsonl','test::one',tmp_path/'test.py');recorder.stack=[{'uid':1}]
    values=list(range(4200))
    for value in values:recorder.snapshot(sys._getframe(),'locals',{'value':value})
    assert len(recorder.snapshot_cache)<=4096
    assert recorder.snapshot_cache_bytes==sum(recorder.snapshot_cache_costs.values())
    recorder.snapshot(sys._getframe(),'locals',{'value':values[0]})
    assert not recorder.gaps
    recorder.file.close()


def test_functions_without_module_metadata_keep_exact_cache_validation(tmp_path):
    namespace={};exec('def function(value=1): return value',namespace)
    function=namespace['function'];assert function.__module__ is None
    first=identity.graph_signature({'function':function},100000,{})
    assert isinstance(first,bytes)
    function.__module__='fixture'
    assert identity.graph_signature({'function':function},100000,{})!=first


def test_changing_outer_registry_reuses_validated_inner_roots(tmp_path,monkeypatch):
    child=list(range(80));outer={'child':child}
    optimized=Recorder(tmp_path/'optimized-inner.jsonl','test::one',tmp_path/'test.py')
    reference=Recorder(tmp_path/'reference-inner.jsonl','test::one',tmp_path/'test.py')
    optimized.stack=[{'uid':1}];reference.stack=[{'uid':1}]
    def snapshot(values):
        optimized.snapshot(sys._getframe(),'locals',values)
        with monkeypatch.context() as patch:
            patch.setattr(identity,'flat_sequence_signature',lambda value:None)
            patch.setattr(identity,'graph_signature',lambda *args:None)
            reference.snapshot(sys._getframe(),'locals',values)
    snapshot({'child':child})
    for repeat in range(4):
        outer[str(repeat)]=repeat;snapshot({'outer':outer})
    child.append(99);snapshot({'outer':outer})
    outer['alias']=child;snapshot({'outer':outer})
    for recorder in (optimized,reference):recorder.file.close()
    def rows(path):return [row for line in path.read_text().splitlines() if (row:=json.loads(line))['event']=='references']
    assert rows(tmp_path/'optimized-inner.jsonl')==rows(tmp_path/'reference-inner.jsonl')
    assert not optimized.gaps


def test_shared_descendant_aliases_do_not_exhaust_a_sufficient_budget(tmp_path,monkeypatch):
    chain=[[]]
    for _ in range(15):chain.append([chain[-1]])
    # 15 chain edges + 16 alias-list edges + 2 named roots = 33.
    # Reaching each depth must not recount all its already observed descendants.
    values={'first':chain[-1],'second':list(reversed(chain))}
    optimized=Recorder(tmp_path/'optimized-deep-aliases.jsonl','test::one',tmp_path/'test.py',max_references=80)
    reference=Recorder(tmp_path/'reference-deep-aliases.jsonl','test::one',tmp_path/'test.py',max_references=80)
    optimized.stack=[{'uid':1}];reference.stack=[{'uid':1}]
    original=reference.snapshot_root
    def fresh_root(value):
        # Independent full per-root traversal, without borrowing shared state
        # from an earlier root in this snapshot.
        reference._snapshot_known_nodes=set()
        return original(value)
    reference.snapshot_root=fresh_root
    optimized.snapshot(sys._getframe(),'locals',values)
    with monkeypatch.context() as patch:
        patch.setattr(identity,'flat_sequence_signature',lambda value:None)
        patch.setattr(identity,'graph_signature',lambda *args:None)
        reference.snapshot(sys._getframe(),'locals',values)
    for recorder in (optimized,reference):recorder.file.close()
    def rows(path):return [row for line in path.read_text().splitlines() if (row:=json.loads(line))['event']=='references']
    actual=rows(tmp_path/'optimized-deep-aliases.jsonl')
    assert actual==rows(tmp_path/'reference-deep-aliases.jsonl')
    assert sum(len(row['added']) for row in actual)==33
    assert not optimized.gaps and not reference.gaps


def test_unsupported_class_context_never_reuses_stale_type_metadata(tmp_path):
    class Box:pass
    # CPython allows non-string __module__ metadata. The validator deliberately
    # rejects it, so the ordinary observer must still detect later class changes.
    Box.__module__=42
    Box.__qualname__='OriginalBox'
    assert identity.type_metadata_context(Box) is None
    recorder=Recorder(tmp_path/'unsupported-class.jsonl','test::one',tmp_path/'test.py')
    before=recorder.metadata(Box)
    Box.__qualname__='Renamed'
    after=recorder.metadata(Box)
    recorder.file.close()
    assert before[1]['static_owner']['qualname']=='OriginalBox'
    assert after[1]['static_owner']['qualname']=='Renamed'
    assert before[0]!=after[0]


def test_foreign_member_descriptors_never_read_an_unrelated_instance_layout():
    import subprocess
    # Keep potential native regressions in a child process: a foreign slot's
    # offset is outside the empty-layout instance, and has unrelated meaning in
    # the same-sized layout. Neither is a valid storage read for this descriptor.
    script='''
from trace_api_chain import frame_identity as identity
from trace_api_chain.object_capture import instance_fields
class Owner:
    __slots__=('value',)
for slots in ((),('actual',)):
    Other=type('Other',(),{'__slots__':slots,'foreign':vars(Owner)['value']})
    value=Other()
    if slots:value.actual=[]
    assert identity.graph_signature({'value':value},100000,{}) is None
    fields,notes=instance_fields(value,project_storage=True)
    assert 'TypeError:foreign' in notes
    assert all(name!='slot:foreign' for name,child in fields)
    if slots:assert fields==[('slot:actual',value.actual)]
'''
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr


def test_foreign_builtin_dict_descriptor_uses_ordinary_observation_boundary():
    from trace_api_chain.object_capture import instance_fields
    class Owner:pass
    class Other:
        __slots__=()
        __dict__=vars(Owner)['__dict__']
    value=Other()
    assert identity.graph_signature({'value':value},100000,{}) is None
    fields,notes=instance_fields(value,project_storage=True)
    assert fields==[] and 'TypeError:__dict__' in notes


@pytest.mark.parametrize('binding',['method','staticmethod','classmethod'])
@pytest.mark.parametrize('module_name',['method_source_fixture',42])
def test_in_place_method_code_replacement_refreshes_class_and_instance_sources(tmp_path,monkeypatch,binding,module_name):
    original_file=tmp_path/'original_method_source.py'
    replacement_file=tmp_path/'replacement_method_source.py'
    original_file.write_text('class Box:\n def method(self):return 1\n')
    replacement_file.write_text('def replacement(self):return 2\n')
    namespace={'__name__':'method_source_fixture'}
    exec(compile(original_file.read_text(),str(original_file),'exec'),namespace)
    Box=namespace['Box'];Box.__module__=module_name
    function=vars(Box)['method']
    if binding=='staticmethod':Box.method=staticmethod(function)
    elif binding=='classmethod':Box.method=classmethod(function)
    value=Box();values={'class':Box,'instance':value}
    before_context=identity.type_metadata_context(Box)
    before_signature=identity.graph_signature(values,100000,{})
    optimized=Recorder(tmp_path/'optimized-method-code.jsonl','test::one',tmp_path/'test.py')
    reference=Recorder(tmp_path/'reference-method-code.jsonl','test::one',tmp_path/'test.py')
    optimized.stack=[{'uid':1}];reference.stack=[{'uid':1}]
    def snapshot():
        optimized.snapshot(sys._getframe(),'locals',values)
        with monkeypatch.context() as patch:
            patch.setattr(identity,'flat_sequence_signature',lambda value:None)
            patch.setattr(identity,'graph_signature',lambda *args:None)
            reference.metadata_cache.clear();reference.class_sources.clear()
            reference.snapshot(sys._getframe(),'locals',values)
    snapshot()
    before_class=optimized.metadata(Box);before_instance=optimized.metadata(value)
    replacement={};exec(compile(replacement_file.read_text(),str(replacement_file),'exec'),replacement)
    # The class binding and function identity stay exactly the same. Only the
    # function's immutable code object changes, without a type-dictionary write.
    function.__code__=replacement['replacement'].__code__
    after_context=identity.type_metadata_context(Box)
    if type(module_name) is str:
        assert before_context!=after_context
        assert before_signature!=identity.graph_signature(values,100000,{})
    else:assert before_context is None and after_context is None
    snapshot()
    after_class=optimized.metadata(Box);after_instance=optimized.metadata(value)
    for before,after in ((before_class,after_class),(before_instance,after_instance)):
        assert before[1]['owner_file']==str(original_file)
        assert after[1]['owner_file']==str(replacement_file)
        assert before[0]!=after[0]
        assert after[1]['class_source_hints'][0]['file']==str(replacement_file)
    for recorder in (optimized,reference):recorder.file.close()
    def rows(path):return [row for line in path.read_text().splitlines() if (row:=json.loads(line))['event'] in ('object','references')]
    assert rows(tmp_path/'optimized-method-code.jsonl')==rows(tmp_path/'reference-method-code.jsonl')
    assert not optimized.gaps and not reference.gaps


def test_storage_plans_keep_each_instances_fields_and_rebuild_after_type_changes(tmp_path,monkeypatch):
    class Base:
        __slots__=('slot','hidden','__dict__')
    class Item(Base):
        __slots__=('extra',)
        @property
        def hidden(self):raise AssertionError('a shadowing property was evaluated')
    # Most Tensor type attributes are not instance storage descriptors. Reusing
    # their selection must not reuse any of the 686 instances' actual values.
    for index in range(256):setattr(Item,'unused_'+str(index),index)
    shared={'children':[1]};items=[]
    for index in range(686):
        value=Item();value.slot=[shared,index];value.extra=None;value.link=shared
        vars(Base)['hidden'].__set__(value,object());items.append(value)
    values={'jacobians':[items,list(reversed(items))]}
    optimized=Recorder(tmp_path/'optimized-field-plans.jsonl','test::one',tmp_path/'test.py')
    reference=Recorder(tmp_path/'reference-field-plans.jsonl','test::one',tmp_path/'test.py')
    optimized.stack=[{'uid':1}];reference.stack=[{'uid':1}]
    previous=None
    def snapshot():
        nonlocal previous
        current=identity.graph_signature(values,100000,{})
        assert isinstance(current,bytes)
        if previous is not None:assert current!=previous
        previous=current
        optimized.snapshot(sys._getframe(),'locals',values)
        with monkeypatch.context() as patch:
            patch.setattr(identity,'flat_sequence_signature',lambda value:None)
            patch.setattr(identity,'graph_signature',lambda *args:None)
            reference.snapshot(sys._getframe(),'locals',values)
    snapshot()
    items[321].slot={'new_instance_field':[object()]};snapshot()
    shared['children'].append(object());snapshot()
    del items[10].extra;snapshot()
    items[20].new_attribute=items[321];snapshot()
    Item.alias=vars(Base)['slot'];snapshot()
    del Item.alias;snapshot()
    for recorder in (optimized,reference):recorder.file.close()
    def rows(path):return [row for line in path.read_text().splitlines() if (row:=json.loads(line))['event']=='references']
    assert rows(tmp_path/'optimized-field-plans.jsonl')==rows(tmp_path/'reference-field-plans.jsonl')
    assert not optimized.gaps and not reference.gaps


def test_storage_plan_keys_do_not_invoke_user_metaclass_hash_or_equality():
    class Meta(type):
        def __hash__(cls):raise AssertionError('metaclass hash was invoked')
        def __eq__(cls,other):raise AssertionError('metaclass equality was invoked')
    class Item(metaclass=Meta):
        __slots__=('slot','__dict__')
    items=[]
    for index in range(686):
        item=Item();item.slot=[index];item.attribute=None;items.append(item)
    first=identity.graph_signature({'items':items},100000,{})
    assert isinstance(first,bytes)
    items[685].attribute=object()
    assert first!=identity.graph_signature({'items':items},100000,{})


def test_storage_plan_descriptors_do_not_keep_types_alive_after_validation():
    import gc,weakref
    def validate_temporary_type():
        class Temporary:
            __slots__=('slot','__dict__')
        values=[Temporary() for _ in range(8)]
        for value in values:value.slot=None;value.attribute=[]
        assert isinstance(identity.graph_signature({'values':values},100000,{}),bytes)
        return weakref.ref(Temporary)
    reference=validate_temporary_type();gc.collect()
    assert reference() is None
