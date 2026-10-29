"""Deterministic evidence -> observed topology. No name-based topology inference."""
import argparse
import collections
import hashlib
import json
from pathlib import Path


class EvidenceError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise EvidenceError(code)


def canonical(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))+'\n').encode()


def rebuild(events, native, contract, *, reuse_reference_batches=False):
    from .reference_transport import normalize_events
    events=normalize_events(events)
    nodes = {}; edges = []; stacks = {}; definitions = {}; objects = {}
    observations = []; references = []; source_reads = []; gaps = set(); exclusions = []
    start = None; finished = False; live_views = {}; end_summary = None
    python_times = {}; reference_groups = {}; live_group_views = {}; reference_scopes = {}
    initial_batches=collections.OrderedDict();initial_batch_bytes=0
    excluded = set(contract['api_scope']['excluded_api_ids'])
    for seq, e in enumerate(events):
        kind = e['event']
        require(not finished, 'event_after_capture_end')
        require(start is not None or kind == 'start', 'event_before_capture_start')
        if kind == 'start':
            require(start is None, 'multiple_capture_starts'); start = e
            if e.get('scope_policy') != contract['version']:
                gaps.add('collector_scope_policy_unverified')
        elif kind == 'definition':
            definitions[e['id']] = e
        elif kind == 'object':
            # Observation identities are preserved; addresses are not promoted
            # into claims about lifetime. Later observations do not erase earlier ones.
            objects[e['id']] = e
            observations.append({'sequence': seq, 'object': e})
        elif kind == 'call':
            require(start is not None, 'call_before_start')
            require(e.get('api_id') not in excluded, 'excluded_api_was_captured')
            uid = 'p:'+str(e['uid']); stack = stacks.setdefault(e['tid'], [])
            require(e['time_ns'] >= python_times.get(e['tid'], 0), 'python_time_order')
            python_times[e['tid']] = e['time_ns']
            parent = 'p:'+str(e['parent']) if e['parent'] is not None else None
            require(uid not in nodes, 'duplicate_python_uid')
            require(parent == (stack[-1] if stack else None), 'python_parent_stack_mismatch')
            if parent is None and not e.get('api_id'):
                gaps.add('api_root_identity_missing:'+uid)
            if parent is None and e.get('api_id'):
                require(e['api_id'] in start.get('api_catalog', []), 'root_api_not_in_catalog')
            nodes[uid] = {'id': uid, 'kind': e['kind'], 'thread': e['tid'],
                          'parent': parent, 'entered': e['time_ns'], 'returned': None,
                          'definition': e.get('definition'), 'api_id': e.get('api_id'),
                          'label': e.get('name'), 'input_sequence': seq}
            if parent is not None:
                edges.append({'from': parent, 'to': uid, 'kind': 'observed_python_nesting'})
            stack.append(uid)
        elif kind == 'return':
            uid = 'p:'+str(e['uid']); require(uid in nodes, 'return_without_enter')
            node = nodes[uid]; stack = stacks[node['thread']]
            require(e['time_ns'] >= python_times.get(node['thread'], 0), 'python_time_order')
            python_times[node['thread']] = e['time_ns']
            require(stack and stack[-1] == uid, 'python_return_stack_mismatch')
            require(e['time_ns'] >= node['entered'], 'python_negative_duration')
            require(node['returned'] is None, 'duplicate_python_return')
            node['returned'] = e['time_ns']; stack.pop()
            for scope in reference_scopes.pop(uid,()):
                live_views.pop((uid,scope),None);live_group_views.pop((uid,scope),None)
        elif kind in ('reference_group','reference_group_delta'):
            from .reference_codec import expand_group
            reference_groups[e['id']]=expand_group(e,reference_groups,objects,require,start.get('reference_policy'))
        elif kind == 'references':
            uid = 'p:'+str(e['uid']); require(uid in nodes, 'references_without_call')
            require(nodes[uid]['returned'] is None, 'references_after_call_return')
            reference_scopes.setdefault(uid,set()).add(e['scope'])
            if 'added_groups' in e:
                require(start.get('reference_policy') in ('shared-adjacency-states/1','shared-adjacency-states/2'),'reference_group_policy_missing')
                view=live_group_views.setdefault((uid,e['scope']),{})
                # Only the pipeline's privately decoded immutable evidence opts
                # in. Public mutable in-memory callers retain every old check.
                batch=(tuple(e['added_groups']) if reuse_reference_batches and not view and not e['removed_groups'] and len(e['added_groups'])>=32 else None)
                cached=initial_batches.get(batch) if batch is not None else None
                if cached is not None:
                    view.update(cached[0]);initial_batches.move_to_end(batch)
                else:
                    added=set(e['added_groups']);removed=set(e['removed_groups'])
                    require(len(added)==len(e['added_groups']) and len(removed)==len(e['removed_groups']),'duplicate_reference_group_update')
                    require(added|removed <= reference_groups.keys(),'unknown_reference_group')
                    for state in removed:
                        require(view.get(reference_groups[state]['parent'])==state,'reference_remove_without_add')
                    new_parents=set()
                    for state in added:
                        parent=reference_groups[state]['parent'];previous=view.get(parent)
                        require(previous!=state or state in removed,'reference_group_already_active')
                        require(parent not in new_parents and (previous is None or previous in removed),'multiple_reference_states_for_parent')
                        new_parents.add(parent)
                    for state in removed:view.pop(reference_groups[state]['parent'])
                    for state in added:view[reference_groups[state]['parent']]=state
                    if batch is not None:
                        cost=512+160*len(batch)
                        if cost<=64*1024*1024:
                            initial_batches[batch]=(view.copy(),cost);initial_batch_bytes+=cost
                            while initial_batch_bytes>64*1024*1024 or len(initial_batches)>256:
                                _,old=initial_batches.popitem(last=False);initial_batch_bytes-=old[1]
            else:
                view = live_views.setdefault((uid, e['scope']), set())
                for parent, role, target in e['added'] + e['removed']:
                    require(target in objects and (parent is None or parent in objects), 'unknown_object_reference')
                removed = {tuple(r) for r in e['removed']}
                require(removed <= view, 'reference_remove_without_add')
                view.difference_update(removed); view.update(tuple(r) for r in e['added'])
            references.append({'sequence': seq, 'observation': e})
        elif kind == 'source_read':
            uid='p:'+str(e['uid']);require(uid in nodes,'source_read_without_call')
            node=nodes[uid]
            require(node['returned'] is None,'source_read_after_return')
            require(node['kind']=='python' and stacks[node['thread']][-1]==uid,'source_read_outside_active_python_call')
            require(e.get('scope') in ('global','module_attribute'),'unknown_source_read_scope')
            require(isinstance(e.get('name'),str) and bool(e['name']),'invalid_source_read_name')
            require(isinstance(e.get('namespace'),dict) and bool(e['namespace'].get('file')),'missing_source_read_namespace')
            definition=definitions.get(node['definition'],{})
            require(e.get('file')==definition.get('file'),'source_read_code_file_mismatch')
            source_reads.append(e)
        elif kind == 'excluded_call':
            require(e['api_id'] in excluded, 'undeclared_exclusion')
            exclusions.append(e)
        elif kind == 'gap':
            gaps.add(e.get('reason', 'capture_gap'))
        elif kind == 'end':
            require(not finished, 'multiple_capture_ends'); finished = True; end_summary = e
            gaps.update(e.get('gaps', []))
        else:
            raise EvidenceError('unknown_python_event:'+kind)
    require(start is not None and finished, 'capture_incomplete')
    require(all(not s for s in stacks.values()), 'python_unclosed_stack')
    if 'calls' in end_summary:
        require(end_summary['calls'] == len(nodes), 'python_call_count_mismatch')
    if 'roots' in end_summary:
        require(end_summary['roots'] == sum(n['parent'] is None for n in nodes.values()), 'python_root_count_mismatch')
    require(all(n['definition'] is None or n['definition'] in definitions for n in nodes.values()),
            'missing_python_definition')

    nstacks = {}; previous = {}; native_count = 0
    for seq, e in enumerate(native):
        window = 'p:'+str(e['py_uid']); require(window in nodes, 'native_window_missing')
        owner = nodes[window]
        require(owner['kind']=='native_boundary' or (owner['kind']=='python' and start.get('native_window_policy')=='python-and-c-boundaries/1'), 'native_window_not_c_boundary')
        require(e['pid']==start['pid'], 'native_pid_mismatch')
        require(e['tid']==owner['thread'], 'native_thread_mismatch')
        require(owner['entered'] <= e['time'] <= owner['returned'], 'native_event_outside_window')
        thread = (e['pid'],e['tid'])
        require(e['time'] >= previous.get(thread,0), 'native_time_order')
        previous[thread] = e['time']; stack = nstacks.setdefault(thread, [])
        # Raw absolute addresses are invocation identities inside this run.
        # ELF/source annotations are deliberately not used to choose parents.
        address = e['absolute_function']
        if e['event']=='enter':
            uid = 'n:'+str(seq); parent = stack[-1] if stack and nodes[stack[-1]]['window']==window else window
            relation = 'instrumented_nesting' if parent.startswith('n:') else 'window_contains'
            nodes[uid] = {'id':uid,'kind':'native_activation','thread':e['tid'],
                          'parent':parent,'window':window,'address':address,
                          'caller_address':e['absolute_caller'],'entered':e['time'],
                          'returned':None,'input_sequence':seq,
                          'module':e['path'],'offset':e['offset']}
            edges.append({'from':parent,'to':uid,'kind':relation})
            stack.append(uid); native_count+=1
        elif e['event']=='exit':
            require(bool(stack), 'native_exit_without_enter')
            node = nodes[stack[-1]]
            require(node['address']==address, 'native_return_stack_mismatch')
            require(node['window']==window, 'native_return_window_mismatch')
            # Optimized inline/outlined code can report different entry/exit
            # call sites. Function, top-of-stack, window, PID and TID remain strict.
            if node['caller_address']!=e['absolute_caller']:
                node['exit_caller_address']=e['absolute_caller']
            node['returned']=e['time']; stack.pop()
        else:
            raise EvidenceError('unknown_native_event')
    require(all(not s for s in nstacks.values()), 'native_unclosed_stack')
    return {'schema':'deterministic-callchain/1','contract_version':contract['version'],
            'run_id':start.get('run_id'),'test':start.get('nodeid'),
            'status':'evidence_scope_incomplete' if gaps else 'observed_scope_validated',
            'gaps':sorted(gaps),'nodes':nodes,'edges':edges,'definitions':definitions,
            'object_observations':observations,'reference_observations':references,'reference_groups':reference_groups,
            'source_reads':source_reads,'module_sources':end_summary.get('module_sources',{}),
            'source_read_policy':end_summary.get('source_read_policy'),
            'exclusions':exclusions,'native_activations':native_count,
            'direct_machine_call_edges_proved':False,
            'all_python_objects_claimed':False,
            'declared_boundaries':start.get('boundaries',[])}


def main():
    p=argparse.ArgumentParser();p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    contract=json.loads(Path(__file__).with_name('contract.json').read_text())
    manifest=json.loads((a.evidence/'manifest.json').read_text())
    for name,expected in manifest['sha256'].items():
        data=(a.evidence/name).read_bytes()
        require(hashlib.sha256(data).hexdigest()==expected,'evidence_hash_mismatch:'+name)
    require({'python.jsonl','native.jsonl'} <= set(manifest['sha256']), 'unhashed_evidence_input')
    events=[json.loads(x) for x in (a.evidence/'python.jsonl').read_text().splitlines()]
    native=[json.loads(x) for x in (a.evidence/'native.jsonl').read_text().splitlines()]
    graph=rebuild(events,native,contract)
    a.out.parent.mkdir(parents=True,exist_ok=True);a.out.write_bytes(canonical(graph))
    print(json.dumps({'status':graph['status'],'nodes':len(graph['nodes']),
                     'native_activations':graph['native_activations'],'gaps':graph['gaps'],
                     'sha256':hashlib.sha256(a.out.read_bytes()).hexdigest()}))

if __name__=='__main__':main()


def added_reference_rows(graph,unique_groups=False):
    """Read legacy deltas and shared states without expanding the saved graph."""
    seen=set()
    for item in graph['reference_observations']:
        row=item['observation']
        if 'added_groups' not in row:
            yield row,row['added'];continue
        for state in row['added_groups']:
            identity=(row['uid'],state)
            if unique_groups and identity in seen:continue
            seen.add(identity)
            yield row,graph['reference_groups'][state]['edges']
