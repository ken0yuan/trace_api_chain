"""Shared deterministic reference interning, usable by the offline reader."""
import collections
from .reference_codec import edge_order

class ReferencePublisher:
    def __init__(self,emit,delta):
        self.emit=emit;self.reference_delta_storage=delta
        self.views={};self.view_keys={}
        self.reference_groups=collections.OrderedDict();self.reference_group_bytes=0
        self.reference_group_costs={};self.next_reference_group=0
        self.reference_parent_states=collections.OrderedDict();self.reference_parent_bytes=0
        self.reference_parent_costs={}
        self.reference_storage={'full_groups':0,'delta_groups':0,'logical_edges':0,'written_edges':0}

    def remember_view(self,key,state):
        self.views[key]=state
        self.view_keys.setdefault(key[0],set()).add(key)

    def retire_views(self,uid):
        # Retire only the scopes owned by this actual returned frame. Scanning
        # every other live frame's global/locals views on each c_return is
        # quadratic during deeply nested imports; their evidence is unchanged.
        for key in self.view_keys.pop(uid,()):self.views.pop(key,None)

    def emit_reference_state(self,state,parent,edges):
        prior=self.reference_parent_states.get(parent)
        self.reference_storage['logical_edges']+=len(edges)
        delta=None
        if self.reference_delta_storage and prior is not None and len(edges)>=16:
            added=edges-prior[1];removed=prior[1]-edges
            if (len(added)+len(removed))*2+4<len(edges):delta=(added,removed)
        if delta is not None:
            added,removed=delta
            self.emit('reference_group_delta',id=state,parent=parent,base=prior[0],
                      added=sorted(added,key=edge_order),removed=sorted(removed,key=edge_order))
            self.reference_storage['delta_groups']+=1
            self.reference_storage['written_edges']+=len(added)+len(removed)
        else:
            self.emit('reference_group',id=state,parent=parent,edges=sorted(edges,key=edge_order))
            self.reference_storage['full_groups']+=1;self.reference_storage['written_edges']+=len(edges)

    def remember_reference_state(self,state,parent,edges):
        if parent in self.reference_parent_states:
            self.reference_parent_bytes-=self.reference_parent_costs.pop(parent)
            del self.reference_parent_states[parent]
        self.reference_parent_states[parent]=(state,edges)
        size=128+len(edges)*96;self.reference_parent_costs[parent]=size;self.reference_parent_bytes+=size
        while len(self.reference_parent_states)>100000 or self.reference_parent_bytes>128*1024*1024:
            previous,_=self.reference_parent_states.popitem(last=False)
            self.reference_parent_bytes-=self.reference_parent_costs.pop(previous)

    def publish_grouped_snapshot(self,uid,phase,values,current,groups,notes):
        scope=('global:'+','.join(values)) if phase=='global_read' else 'return' if phase=='return' else 'native' if phase=='native_entry' else 'frame'
        key=(uid,scope);previous,old_groups,old_ids,old_notes=self.views.get(key,(frozenset(),{}, {},None))
        if (current is previous or current==previous) and (groups is old_groups or groups==old_groups):
            if frozenset(notes)!=old_notes:self.emit('references',uid=uid,phase=phase,scope=scope,added_groups=[],removed_groups=[],notes=sorted(notes))
            self.remember_view(key,(current,groups,old_ids,frozenset(notes)));return
        states={};source={None:current,**groups}
        for parent,edges in source.items():
            before=previous if parent is None else old_groups.get(parent)
            if parent in old_ids and (before is edges or (before is not None and before==edges)):
                states[parent]=old_ids[parent];continue
            identity=(parent,edges);state=self.reference_groups.get(identity)
            if state is None:
                self.next_reference_group+=1;state='references:'+str(self.next_reference_group)
                self.emit_reference_state(state,parent,edges)
                self.reference_groups[identity]=state
                size=128+len(edges)*96;self.reference_group_bytes+=size;self.reference_group_costs[identity]=size
                while len(self.reference_groups)>100000 or self.reference_group_bytes>128*1024*1024:
                    expired,_=self.reference_groups.popitem(last=False)
                    self.reference_group_bytes-=self.reference_group_costs.pop(expired)
            else:self.reference_groups.move_to_end(identity)
            if self.reference_delta_storage:self.remember_reference_state(state,parent,edges)
            states[parent]=state
        added=sorted(set(states.values())-set(old_ids.values()));removed=sorted(set(old_ids.values())-set(states.values()))
        if added or removed or frozenset(notes)!=old_notes:
            self.emit('references',uid=uid,phase=phase,scope=scope,added_groups=added,removed_groups=removed,notes=sorted(notes))
        self.remember_view(key,(current,groups,states,frozenset(notes)))

