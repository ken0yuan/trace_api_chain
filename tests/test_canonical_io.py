import pytest
from trace_api_chain.canonical_io import write_canonical,iter_canonical_bytes
from trace_api_chain.replay import canonical


@pytest.mark.parametrize('value',[
    {'nodes':{'p:10':{'name':'中文😀\n"\\','number':-0.0},'p:2':{'truth':True,'nil':None}},'edges':[['p:10','p:2']], 'empty':{}},
    {'reference_groups':{'b':{'edges':[['x','child','z']]*10000},'a':{'edges':[['x','child','y']]*10000}},'observations':[{'uid':n,'scope':'frame','added':['a','b']} for n in range(200)]},
    {'non_string_keys':{2:'two',1:'one'},'tuple':(1,2,{'a':3}),'floating':[float('inf'),float('-inf'),float('nan'),1.25]},
    [[],{},('a','b'),{'scalar':42}],
])
def test_complete_canonical_bytes_match_without_changing_values(tmp_path,value):
    expected=canonical(value);out=tmp_path/'graph.json';write_canonical(out,value)
    assert out.read_bytes()==expected and canonical(value)==expected


def test_subtree_encoding_does_not_require_a_graph_sized_write(tmp_path):
    graph={'objects':[{'id':str(n),'label':'payload'*200} for n in range(1000)]}
    chunks=list(iter_canonical_bytes(graph));assert b''.join(chunks)==canonical(graph)
    assert max(map(len,chunks))<sum(map(len,chunks))//100


def test_invalid_values_and_mixed_keys_still_fail(tmp_path):
    for value in [{'bad':object()},{1:'one','a':'string'}]:
        with pytest.raises(TypeError):canonical(value)
        target=tmp_path/'invalid.json';target.write_bytes(b'previous_valid_graph')
        with pytest.raises(TypeError):write_canonical(target,value)
        assert target.read_bytes()==b'previous_valid_graph'
