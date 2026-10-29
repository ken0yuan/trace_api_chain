def restore_captured_graph_order(graph,events):
    # Canonical graph JSON sorts dictionary keys. Restore only insertion order
    # known from retained input_sequence and actual raw source-read records.
    # Every graph value must still compare equal before replacing a container.
    nodes=dict(sorted(graph['nodes'].items(),key=lambda item:(0 if item[0].startswith('p:') else 1,item[1]['input_sequence'])))
    assert nodes==graph['nodes']
    representatives={};contexts={}
    for row in events:
        if row['event']=='call':contexts[str(row['uid'])]=representatives.setdefault(row['path_context'],str(row['uid']))
    reads=[dict(row,uid=int(contexts[str(row['uid'])])) for row in events if row['event']=='source_read']
    assert reads==graph['source_reads'],'Captured source-read values/order may not change'
    graph['nodes']=nodes;graph['source_reads']=reads
