"""Lossless adjacency-state deltas. A base is storage reuse, never ancestry."""

POLICY='shared-adjacency-states/2'
SUPPORTED=('shared-adjacency-states/1',POLICY)


def edge_order(edge):
    return (edge[0] or '',edge[1],edge[2])


def expand_group(row,groups,objects,require,policy):
    require(policy in SUPPORTED,'reference_group_policy_missing')
    require(row['id'] not in groups,'duplicate_reference_group')
    parent=row['parent']
    require(parent is None or parent in objects,'unknown_reference_group_parent')
    def checked(rows):
        found=set()
        for value in rows:
            require(isinstance(value,(list,tuple)) and len(value)==3,'invalid_reference_group_edge')
            source,role,target=value
            require(source==parent and target in objects,'unknown_object_reference')
            edge=(source,role,target)
            require(edge not in found,'duplicate_reference_group_edge');found.add(edge)
        return found
    if row['event']=='reference_group':
        checked(row['edges'])
        return row
    require(policy==POLICY,'reference_delta_policy_missing')
    require(row.get('base') in groups,'unknown_reference_delta_base')
    base=groups[row['base']]
    require(base['parent']==parent,'reference_delta_parent_mismatch')
    previous=set(map(tuple,base['edges']));added=checked(row['added']);removed=checked(row['removed'])
    require(not added.intersection(removed),'reference_delta_added_and_removed')
    require(removed<=previous,'reference_delta_remove_without_add')
    require(not added.intersection(previous),'reference_delta_duplicate_add')
    current=(previous-removed)|added
    return {'event':'reference_group','id':row['id'],'parent':parent,
            'edges':[list(edge) for edge in sorted(current,key=edge_order)]}
