"""Experimental object-depth budget; actual call/native hooks stay unchanged."""
import collections
import types


def snapshot(recorder,frame,phase,values):
    from .object_capture import NativeStorage,add_reference,instance_fields,label_is_extension
    from .frame_identity import reference_children,flat_sequence_signature
    r=recorder;limit=r.reference_depth_limit
    queue=collections.deque();roots=[];groups={};notes=set();seen=set();metadata={};labels={};plans={}
    count=0;flat_roots=[]
    # All roots enter before any descendant: the first visit has the shortest
    # distance from any observed root, including a later shallow alias.
    for name,value in values.items():
        identity=(id(value),id(type(value)));flat=flat_sequence_signature(value)
        signature=(identity,flat) if flat is not None else None
        cached=r.snapshot_cache.get(signature) if signature is not None else None
        if cached is None and flat is not None:
            cached=r.incremental_flat_snapshot(value,identity,flat)
        if cached is not None:
            root,adjacency,unread,nodes,complete=cached
            roots.append((None,'root:'+name,root));groups.update(adjacency);notes.update(unread)
            count+=1+(sum(len(edges) for edges in adjacency.values()) if root not in seen else 0);seen.add(root)
            r.remember_snapshot(identity,signature,cached)
        else:
            queue.append((None,'root:'+name,value,0))
        if flat is not None:flat_roots.append((identity,signature,value))
    while queue:
        parent,role,value,depth=queue.popleft();count+=1
        if count>r.max_references:
            notes.add('reference_budget_exceeded');r.gaps.add('reference_budget_exceeded');break
        identity=(id(value),id(type(value)))
        if identity not in metadata:metadata[identity]=r.metadata(value,labels)
        token,meta=metadata[identity]
        if parent is None:roots.append((None,role,token))
        else:add_reference(groups,parent,(parent,role,token))
        if r.object_metadata.get(token)!=meta:r.object_metadata[token]=meta;r.emit('object',**meta)
        if token in seen:continue
        seen.add(token);typ=type(value)
        if id(value) in r.observer_roots and r.observer_roots[id(value)] is value:
            notes.add('observer_state_not_traversed');continue
        terminal=typ in (str,bytes,int,float,complex,bool,type(None),types.CodeType,types.ModuleType) or issubclass(typ,type)
        if terminal:continue
        if depth>=limit:
            notes.add('reference_depth_limit:'+str(limit))
            # Boundary means "not inspected at this observation", even if the
            # same object is observed as a shallower root in another snapshot.
            if token not in r.depth_boundary_objects:
                r.depth_boundary_objects.add(token)
                r.emit('reference_boundary',object=token,depth=depth,limit=limit,
                       uid=r.stack[-1]['uid'],phase=phase,reason='experimental_depth_limit')
            continue
        fast=reference_children(value,token,NativeStorage)
        if fast is not None:
            queue.extend((p,name,child,depth+1) for p,name,child in fast);continue
        children=[]
        if typ in (set,frozenset):children=[('member:'+str(id(v)),v) for v in value.copy()]
        else:
            children,unread=instance_fields(value,plans,project_storage=True);notes.update(unread)
            if not children and label_is_extension(meta):notes.add('opaque_storage:'+meta['type'].get('qualname','unknown'))
        for name,child in children:
            if type(child) is NativeStorage:
                count+=1
                if count>r.max_references:
                    notes.add('reference_budget_exceeded');r.gaps.add('reference_budget_exceeded');break
                field=r.storage_metadata(token,child);add_reference(groups,token,(token,name,field))
            else:queue.append((token,name,child,depth+1))
    if count>r.max_references:
        notes.add('reference_budget_exceeded');r.gaps.add('reference_budget_exceeded')
    frozen={parent:frozenset(edges) for parent,edges in groups.items()}
    if 'reference_budget_exceeded' not in notes:
        # A flat root contains immutable leaves at depth one. Its exact identity
        # vector remains valid under this budget; no recursive full signature is
        # computed anywhere in the bounded observer.
        for identity,signature,value in flat_roots:
            if id(value) in r.observer_roots:continue
            root=r.metadata(value,labels)[0]
            if root in frozen:
                chunk=(root,{root:frozen[root]},frozenset(),frozenset({root}),True)
                r.remember_snapshot(identity,signature,chunk)
    r.publish_snapshot(r.stack[-1]['uid'],phase,values,frozenset(roots),frozen,notes)
