"""Validated observed native paths; no synthetic chronological event history."""
import collections
import json
import struct
from .native import Modules
from .replay import rebuild,require

def decode_paths(output):
    windows=json.loads((output/'windows.json').read_text())
    modules=Modules((output/'maps.txt').read_text(),windows['module_stamps'])
    raw=(output/'native-paths.bin').read_bytes()
    require(len(raw)>=48,'native_path_header_truncated')
    magic,n,s,events,pid,tid,failed,reserved=struct.unpack_from('<8sQQQIIII',raw)
    require(magic==b'CCPATH01' and reserved==0,'native_path_format')
    require(failed==0,'native_path_capture_loss:'+str(failed))
    require(len(raw)==48+n*24+s*32,'native_path_size_mismatch')
    nodes=list(struct.iter_unpack('<QQII',raw[48:48+n*24]))
    for index,(fn,caller,parent,pad) in enumerate(nodes,1):
        require(fn>0 and parent<index and pad==0,'native_path_invalid_parent')
    records=[];seen=set();by_window=collections.Counter()
    representatives={}
    for segment in windows['segments']:representatives.setdefault(segment['context'],segment['uid'])
    for window,enters,exits,node,pad in struct.iter_unpack('<QQQII',raw[48+n*24:]):
        require(0<node<=n and enters>0 and enters==exits and pad==0,'native_path_count_or_node_invalid')
        require((window,node) not in seen,'native_path_duplicate_support')
        fn,caller,parent,_=nodes[node-1]
        require(parent==0 or (window,parent) in seen,'native_path_window_parent_missing')
        seen.add((window,node));by_window[window]+=enters+exits
        target=modules.identify(fn);callsite=modules.identify(max(0,caller-1))
        require(window in representatives,'native_path_context_unknown')
        records.append({'event':'path','pid':pid,'tid':tid,'py_uid':representatives[window],'context':window,
            'node_id':node,'parent_node_id':parent,'entries':enters,'exits':exits,
            'absolute_function':fn,'absolute_caller':caller,'path':target['path'],
            'offset':target['offset'],'source_identity':target,'caller_identity':callsite})
    require(sum(by_window.values())==events,'native_path_event_count_mismatch')
    receipts=collections.Counter()
    for index,segment in enumerate(windows['segments']):
        require(segment['ordinal']==index and segment['stop_status']==0,'native_path_segment_invalid')
        receipts[int(segment['context'])]+=segment['events']
    require(+receipts==+by_window,'native_path_window_count_mismatch')
    return records,modules,list(windows['gaps'])

def collapse_python_contexts(graph,events):
    contexts={};representatives={};signatures={};by_uid={};counts=collections.Counter()
    for row in events:
        if row['event']!='call':continue
        uid=str(row['uid']);parent=str(row['parent']) if row['parent'] is not None else None
        require('path_context' in row,'python_path_context_missing')
        context=row['path_context'];identity=row.get('native_identity') or {}
        signature=(by_uid[parent] if parent is not None else None,row['kind'],row.get('definition'),
                   identity.get('module'),identity.get('module_offset'),tuple(row.get('api_aliases',[])))
        require(context not in signatures or signatures[context]==signature,'python_path_context_collision')
        signatures[context]=signature;by_uid[uid]=context;counts[context]+=1
        representatives.setdefault(context,uid);contexts[uid]=representatives[context]
    old=graph['nodes'];nodes={};edges=[]
    for context,uid in representatives.items():
        key='p:'+uid;node=dict(old[key]);parent=node['parent']
        node.update(parent='p:'+contexts[parent[2:]] if parent is not None else None,
                    entered=None,returned=None,aggregated=True,call_count=counts[context],path_context=context)
        nodes[key]=node
        if node['parent'] is not None:edges.append({'from':node['parent'],'to':key,'kind':'observed_python_nesting'})
    for item in graph['reference_observations']:
        row=dict(item['observation']);row['uid']=int(contexts[str(row['uid'])]);item['observation']=row
    graph['source_reads']=[dict(row,uid=int(contexts[str(row['uid'])])) for row in graph['source_reads']]
    graph.update(nodes=nodes,edges=edges,python_invocations=len(old),python_path_nodes=len(nodes))
    return contexts

def rebuild_paths(events,native,contract,*,reuse_reference_batches=False):
    from .reference_transport import normalize_events
    events=normalize_events(events)
    graph=rebuild(events,[],contract,reuse_reference_batches=reuse_reference_batches)
    collapse_python_contexts(graph,events)
    first=events[0];support={};total=0
    for index,row in enumerate(native):
        require(row['event']=='path','native_path_row_kind')
        window='p:'+str(row['py_uid']);require(window in graph['nodes'],'native_window_missing')
        owner=graph['nodes'][window]
        require(owner['path_context']==row['context'],'native_path_context_owner_mismatch')
        require(owner['kind']=='native_boundary' or (owner['kind']=='python' and first.get('native_window_policy')=='python-and-c-boundaries/1'),'native_window_not_c_boundary')
        require(row['pid']==first['pid'] and row['tid']==owner['thread'],'native_path_thread_mismatch')
        require(row['entries']>0 and row['entries']==row['exits'],'native_path_unbalanced_counts')
        key=(row['py_uid'],row['node_id']);require(key not in support,'native_path_duplicate_support')
        parent_key=(row['py_uid'],row['parent_node_id'])
        require(row['parent_node_id']==0 or parent_key in support,'native_path_window_parent_missing')
        parent=support[parent_key] if row['parent_node_id'] else window
        uid='n:'+str(index);support[key]=uid;total+=row['entries']
        graph['nodes'][uid]={'id':uid,'kind':'native_aggregated_path','thread':row['tid'],
            'parent':parent,'window':window,'address':row['absolute_function'],
            'caller_address':row['absolute_caller'],'input_sequence':index,
            'module':row['path'],'offset':row['offset'],'call_count':row['entries']}
        graph['edges'].append({'from':parent,'to':uid,'kind':'instrumented_nesting' if parent.startswith('n:') else 'window_contains'})
    graph.update(schema='deterministic-native-paths/1',native_activations=total,native_path_nodes=len(native),
        native_storage='paths',runtime_event_history_preserved=False)
    graph['declared_boundaries']=list(graph['declared_boundaries'])+['Python and native paths aggregate repeated source-call contexts and counts; sibling order and per-invocation timestamps are not retained. Context-specific support preserves observed branches without inventing cross-API ancestry.']
    return graph
