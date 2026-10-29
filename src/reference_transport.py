"""Lossless transport of complete snapshots; reference interning runs offline.

No workload object is retained here. Blocks contain already observed identity
tokens and frozen edges. Normalization reproduces the legacy reference events,
including their ordering and counters, before graph construction.
"""
import collections

from .reference_codec import edge_order

POLICY='deferred-adjacency/1'


class SnapshotTransport:
    def __init__(self, emit, limit=128*1024*1024):
        self.emit=emit;self.blocks=collections.OrderedDict();self.bytes=0
        self.limit=limit;self.next_block=0;self.latest={}

    def snapshot(self,uid,phase,names,roots,parts,notes):
        encoded=[]
        for groups in parts:
            if not groups:continue
            key=id(groups);entry=self.blocks.get(key)
            if entry is not None and entry[0] is groups:
                self.blocks.move_to_end(key);encoded.append({'block':entry[1]});continue
            if len(groups)<32:
                rows=[[parent,[list(edge) for edge in sorted(edges,key=edge_order)]]
                      for parent,edges in groups.items()]
                encoded.append({'rows':rows});continue
            self.next_block+=1;block='adjacency:'+str(self.next_block)
            first=next(iter(groups));previous=self.blocks.get(self.latest.get(first))
            changed=None
            if previous is not None:
                changed={parent:edges for parent,edges in groups.items()
                         if parent not in previous[0] or (previous[0][parent] is not edges and previous[0][parent]!=edges)}
            # Preserve the complete parent order. Only already equal adjacency
            # is borrowed; all changed/new rows and every removed parent remain
            # explicit through this exact order and the base-relative update.
            delta=(changed is not None and len(changed)*2+32<len(groups))
            rows=[[parent,[list(edge) for edge in sorted(edges,key=edge_order)]]
                  for parent,edges in (changed if delta else groups).items()]
            if delta:self.emit('reference_block_delta',id=block,base=previous[1],order=list(groups),rows=rows)
            else:self.emit('reference_block',id=block,rows=rows)
            cost=128+sum(128+96*len(edges) for edges in groups.values())
            if cost<=self.limit:
                self.blocks[key]=(groups,block,cost);self.bytes+=cost
                self.latest[first]=key
                while self.bytes>self.limit:
                    old_key,old=self.blocks.popitem(last=False);self.bytes-=old[2]
                    old_first=next(iter(old[0]))
                    if self.latest.get(old_first)==old_key:self.latest.pop(old_first,None)
            encoded.append({'block':block})
        self.emit('reference_snapshot',uid=uid,phase=phase,names=list(names),
                  roots=[list(edge) for edge in sorted(roots,key=edge_order)],
                  parts=encoded,notes=sorted(notes))


def normalize_events(events):
    """Leave legacy evidence untouched; expand declared transport before replay."""
    if not isinstance(events,(list,tuple)):events=list(events)
    if not events or not events[0].get('reference_transport'):return events
    return list(expand_events(events))


def expand_events(events):
    from .replay import require
    from .reference_state import ReferencePublisher
    pending=[]
    def emit(kind,**row):
        for key in ('edges','added','removed'):
            if key in row:row[key]=[list(edge) for edge in row[key]]
        pending.append({'event':kind,**row})
    publisher=ReferencePublisher(emit,False)
    blocks={};active=set();started=False;finished=False
    def checked_rows(rows):
        require(type(rows) is list,'invalid_reference_block_rows');groups={}
        for row in rows:
            require(type(row) is list and len(row)==2,'invalid_reference_block_row')
            parent,edges=row;require(type(parent) is str and parent not in groups,'invalid_reference_block_parent')
            require(type(edges) is list,'invalid_reference_block_edges');found=set()
            for edge in edges:
                require(type(edge) is list and len(edge)==3 and edge[0]==parent and
                        type(edge[1]) is str and type(edge[2]) is str,'invalid_reference_block_edge')
                item=tuple(edge);require(item not in found,'duplicate_reference_block_edge');found.add(item)
            groups[parent]=frozenset(found)
        return groups
    for event in events:
        kind=event['event'];require(not finished,'event_after_capture_end')
        if kind=='start':
            require(not started and event.get('reference_transport')==POLICY,'invalid_reference_transport')
            require(event.get('reference_policy') in ('shared-adjacency-states/1','shared-adjacency-states/2'),'reference_group_policy_missing')
            publisher.reference_delta_storage=event['reference_policy']=='shared-adjacency-states/2'
            started=True;row=dict(event);row.pop('reference_transport');yield row;continue
        require(started,'event_before_capture_start')
        if kind in ('reference_block','reference_block_delta'):
            block=event.get('id');require(type(block) is str and block and block not in blocks,'duplicate_reference_block')
            changed=checked_rows(event.get('rows'))
            if kind=='reference_block_delta':
                require(event.get('base') in blocks,'unknown_reference_block_base');base=blocks[event['base']]
                order=event.get('order');require(type(order) is list and all(type(x) is str for x in order) and len(set(order))==len(order),'invalid_reference_block_order')
                require(changed.keys()<=set(order) and set(order)<=base.keys()|changed.keys(),'invalid_reference_block_delta_members')
                blocks[block]={parent:changed[parent] if parent in changed else base[parent] for parent in order}
            else:blocks[block]=changed
            continue
        if kind=='reference_snapshot':
            uid=event.get('uid');require(type(uid) is int and uid in active,'reference_snapshot_without_active_call')
            require(type(event.get('names')) is list and all(type(x) is str for x in event['names']),'invalid_reference_snapshot_names')
            require(len(set(event['names']))==len(event['names']),'duplicate_reference_snapshot_name')
            require(type(event.get('phase')) is str and type(event.get('notes')) is list and all(type(x) is str for x in event['notes']),'invalid_reference_snapshot_scope')
            groups={}
            for part in event['parts']:
                if set(part)=={'block'}:
                    require(part['block'] in blocks,'unknown_reference_block');current=blocks[part['block']]
                else:
                    require(set(part)=={'rows'},'invalid_reference_snapshot_part');current=checked_rows(part['rows'])
                for parent,edges in current.items():
                    require(parent not in groups or groups[parent]==edges,'conflicting_reference_snapshot_parts')
                    groups[parent]=edges
            roots=event.get('roots');require(type(roots) is list,'invalid_reference_snapshot_roots')
            require(all(type(edge) is list and len(edge)==3 and edge[0] is None and
                        type(edge[1]) is str and type(edge[2]) is str for edge in roots),'invalid_reference_snapshot_root')
            frozen=frozenset(map(tuple,roots));require(len(frozen)==len(roots),'duplicate_reference_snapshot_root')
            publisher.publish_grouped_snapshot(uid,event['phase'],dict.fromkeys(event['names']),frozen,groups,event['notes'])
            yield from pending;pending.clear();continue
        require(kind not in ('reference_group','reference_group_delta','references'),'mixed_reference_transport')
        if kind=='call':active.add(event['uid'])
        if kind=='return':
            active.discard(event['uid']);publisher.retire_views(event['uid'])
        if kind=='end':
            event=dict(event);event['reference_storage']=dict(publisher.reference_storage);finished=True
        yield event
