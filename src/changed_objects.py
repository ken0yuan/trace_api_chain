"""Standalone Python diff -> source objects, also shared by runtime replay.

No imports of the target project and no evaluation of its code.
Run: python changed_objects.py change.diff --repo REPO --base HEAD --label pytorch
"""
import argparse
import ast
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import tokenize


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source_identity(path, kind, qualname):
    """Names are lexical source names; lines, runtime names and values are not keys.

The final zero preserves IDs of existing, uniquely named Python definitions.
Repeated function/class definitions are reported ambiguous instead of numbered.
"""
    return key([path, kind, qualname, 0])


def canonical_path(filename, roots):
    path = Path(filename).resolve()
    matches = []
    for label, directory in roots.items():
        root = Path(directory).resolve()
        if path.is_relative_to(root):
            relative = path.relative_to(root).as_posix()
            prefix = 'torch_npu/' if label == 'torch_npu' else 'torch/' if label == 'pytorch' else label + '/'
            matches.append((len(str(root)), int(relative.startswith(prefix)), label + '/' + relative))
    return max(matches)[2] if matches else path.as_posix()


def decode_python(data):
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return data.decode(encoding)


def index_python(text):
    """Index definitions and module/class bindings, never invent local variables."""
    tree = ast.parse(text)
    definitions = [{'kind': 'module', 'name': '<module>', 'parent': '', 'line': 1,
                    'end_line': max(1, len(text.splitlines())), 'ordinal': 0, 'object_type': 'module'}]

    def binding_names(node):
        if isinstance(node, ast.Name): return [node.id]
        if isinstance(node, (ast.Tuple, ast.List)):
            return [name for child in node.elts for name in binding_names(child)]
        return []

    def add_binding(name, node, parents, imported=None):
        definitions.append({'kind': 'binding', 'name': '.'.join(parents + [name]),
                            'parent': '.'.join(parents), 'line': node.lineno,
                            'end_line': node.end_lineno, 'ordinal': 0,
                            'object_type': 'class_attribute' if parents else 'global',
                            'import': imported})

    def dotted(node):
        if isinstance(node,ast.Name):return [node.id]
        if isinstance(node,ast.Attribute):
            base=dotted(node.value)
            return base+[node.attr] if base else []
        return []

    def walk(node, parents, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                is_class = isinstance(child, ast.ClassDef)
                name = '.'.join(parents + [child.name])
                first = min([child.lineno] + [d.lineno for d in child.decorator_list])
                definitions.append({'kind': 'type' if is_class else 'function', 'name': name,
                                    'parent': '.'.join(parents), 'line': first, 'end_line': child.end_lineno,
                                    'ordinal': 0, 'object_type': 'class' if is_class else 'method' if scope == 'class' else 'function',
                                    '_decorators':[dotted(d) for d in child.decorator_list]})
                walk(child, parents + [child.name], 'class' if is_class else 'function')
            elif isinstance(child, ast.Lambda):
                continue
            else:
                if scope in ('module', 'class'):
                    targets = child.targets if isinstance(child, ast.Assign) else [child.target] if isinstance(child, (ast.AnnAssign, ast.AugAssign)) else []
                    for target in targets:
                        for name in binding_names(target): add_binding(name, child, parents)
                    if isinstance(child, ast.ImportFrom):
                        for alias in child.names:
                            if alias.name != '*':
                                add_binding(alias.asname or alias.name, child, parents,
                                            {'module': child.module or '', 'level': child.level, 'name': alias.name})
                    elif isinstance(child, ast.Import):
                        for alias in child.names:
                            add_binding(alias.asname or alias.name.split('.')[0], child, parents,
                                        {'module': alias.name if alias.asname else alias.name.split('.')[0], 'level': 0, 'name': None})
                walk(child, parents, scope)
    walk(tree, [], 'module')
    # typing.overload declarations share the implementation's source identity.
    # Resolve explicit imports; a decorator merely named "overload" is not proof.
    by_name={}
    for d in definitions:by_name.setdefault(d['name'],[]).append(d)
    for d in definitions:
        d['declaration_only']=False
        parent=d['parent'];scopes=[];safe=True
        while parent:
            owners=by_name.get(parent,[])
            if len(owners)!=1 or owners[0]['kind']!='type':safe=False;break
            scopes.append(parent+'.');parent=owners[0]['parent']
        if safe:
            scopes.append('')
            for parts in d.pop('_decorators',[]):
                if not parts:continue
                bindings=next((by_name[prefix+parts[0]] for prefix in scopes if prefix+parts[0] in by_name),[])
                if len(bindings)!=1:continue
                imp=bindings[0].get('import')
                if not imp or imp['level'] or imp['module'] not in ('typing','typing_extensions'):continue
                resolved=([imp['name']] if imp['name'] else [])+parts[1:]
                if resolved==['overload'] and d['kind']=='function':d['declaration_only']=True
        else:d.pop('_decorators',None)
    counts = {}
    for definition in definitions:
        ident = definition['kind'], definition['name']
        if not definition['declaration_only']:counts[ident] = counts.get(ident, 0) + 1
    for definition in definitions:
        definition['ambiguous'] = definition['kind'] in ('function', 'type') and counts.get((definition['kind'], definition['name']),0)!=1
    return definitions


def _diff_path(raw,strip_prefix=True):
    raw = raw.split('\t', 1)[0].strip()
    if raw.startswith('"'):
        raw = ast.literal_eval(raw)
        try: raw = raw.encode('latin1').decode('utf-8')
        except (UnicodeEncodeError, UnicodeDecodeError): pass
    if raw == '/dev/null': return None
    if strip_prefix and raw.startswith(('a/', 'b/')): raw = raw[2:]
    path = PurePosixPath(raw)
    if path.is_absolute() or '..' in path.parts or '\\' in raw or ':' in raw:
        raise ValueError('unsafe_diff_path: ' + raw)
    return path.as_posix()


def parse_diff(text):
    lines = text.splitlines(keepends=True)
    patches = []; current = None; index = 0
    while index < len(lines):
        line = lines[index]; index += 1
        if line.startswith('diff --git '):
            match = re.match(r'diff --git ("(?:\\.|[^"])*"|a/.*?) ("(?:\\.|[^"])*"|b/.*)\r?\n?$', line.rstrip('\r\n'))
            if not match: raise ValueError('unsupported_git_diff_header')
            current = {'old_path': _diff_path(match[1]), 'new_path': _diff_path(match[2]), 'hunks': []}
            patches.append(current)
        elif line.startswith('--- '):
            if current is None or current.get('headers_seen'):
                current = {'hunks': []}; patches.append(current)
            current['old_path'] = _diff_path(line[4:]); current['headers_seen'] = True
        elif line.startswith('+++ ') and current is not None:
            current['new_path'] = _diff_path(line[4:])
        elif line.startswith('new file mode ') and current is not None:
            current['old_path'] = None
        elif line.startswith('deleted file mode ') and current is not None:
            current['new_path'] = None
        elif line.startswith(('copy from ','copy to ')) and current is not None:
            current['copy'] = True
        elif line.startswith(('rename from ', 'rename to ')) and current is not None:
            side = 'old_path' if line.startswith('rename from ') else 'new_path'
            # Rename metadata does not have a/ or b/ prefixes; headers, when
            # present, already contain the authoritative paths.
            raw = line.removeprefix('rename from ').removeprefix('rename to ').rstrip('\r\n')
            current[side] = _diff_path(raw,strip_prefix=False)
        elif line.startswith('@@'):
            match = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if current is None or not match: raise ValueError('unsupported_or_combined_diff_hunk')
            old_start, old_count, new_start, new_count = [int(value) if value is not None else 1 for value in match.groups()]
            body = []; old_seen = new_seen = 0
            while old_seen < old_count or new_seen < new_count:
                if index >= len(lines): raise ValueError('truncated_diff_hunk')
                item = lines[index]; index += 1
                if not item or item[0] not in ' +-': raise ValueError('invalid_diff_hunk_line')
                body.append(item)
                old_seen += item[0] in ' -'; new_seen += item[0] in ' +'
                if index < len(lines) and lines[index].startswith('\\ No newline at end of file'):
                    body[-1] = body[-1].rstrip('\r\n'); index += 1
            if (old_seen, new_seen) != (old_count, new_count): raise ValueError('diff_hunk_count_mismatch')
            current['hunks'].append((old_start, old_count, new_start, new_count, body))
        elif current is not None and (line.startswith('GIT binary patch') or line.startswith('Binary files ')):
            current['binary'] = True
    if text.strip() and not patches:raise ValueError('not_a_unified_diff')
    return patches


def apply_diff(old_text, hunks):
    old = old_text.splitlines(keepends=True); result = []; cursor = 0
    changed_old = set(); changed_new = set()
    for old_start, old_count, new_start, new_count, body in hunks:
        anchor = old_start - 1 if old_count else old_start
        new_anchor = new_start - 1 if new_count else new_start
        if anchor < cursor or anchor > len(old): raise ValueError('diff_old_range_mismatch')
        result.extend(old[cursor:anchor]); cursor = anchor
        if len(result) != new_anchor: raise ValueError('diff_new_range_mismatch')
        for item in body:
            marker, content = item[0], item[1:]
            if marker in ' -':
                if cursor >= len(old) or old[cursor] != content: raise ValueError('diff_context_mismatch')
                if marker == '-': changed_old.add(cursor + 1)
                cursor += 1
            if marker in ' +':
                if marker == '+': changed_new.add(len(result) + 1)
                result.append(content)
    result.extend(old[cursor:])
    return ''.join(result), changed_old, changed_new


def _owners(text, changed_lines):
    definitions = index_python(text); found = {}
    for line in sorted(changed_lines):
        candidates = [d for d in definitions if d['line'] <= line <= d['end_line']]
        if not candidates: candidates = definitions[:1]
        # Module is a fallback. Prefer the deepest lexical scope, then the
        # narrowest declaration; multi-target assignments may own two bindings.
        score = lambda d: (d['kind'] != 'module', d['name'].count('.'), -(d['end_line'] - d['line']), d['kind'] == 'binding')
        best = max(map(score, candidates))
        for definition in candidates:
            if score(definition) == best:
                ident = definition['kind'], definition['name']
                found.setdefault(ident, {'definition': definition, 'changed_lines': []})['changed_lines'].append(line)
    return list(found.values())


def identify_changes(diff_text, *, repo, label, base='HEAD', before_root=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', label) or label in ('.', '..'):
        raise ValueError('invalid_source_root_label')
    repo = Path(repo).resolve()
    objects = {}; unresolved = []
    for patch in parse_diff(diff_text):
        old_path = patch.get('old_path'); new_path = patch.get('new_path')
        if patch.get('binary') or any(p and not p.endswith('.py') for p in (old_path, new_path)):
            unresolved.append({'old_path': old_path, 'new_path': new_path, 'reason': 'unsupported_non_python_or_binary_file'}); continue
        if patch.get('copy'):
            unresolved.append({'old_path':old_path,'new_path':new_path,'reason':'copy_metadata_requires_diff_without_copy_detection'});continue
        try:
            if old_path is None: old_data = b''
            elif before_root is not None:
                root = Path(before_root).resolve(); file = (root / old_path).resolve()
                if not file.is_relative_to(root): raise ValueError('source_symlink_outside_root')
                old_data = file.read_bytes()
            else:
                revision = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', '--end-of-options', base + '^{commit}'], capture_output=True, text=True, check=True).stdout.strip()
                old_data = subprocess.run(['git', '-C', str(repo), 'show', revision + ':' + old_path], capture_output=True, check=True).stdout
            old_text=decode_python(old_data)
            new_text, old_lines, new_lines = apply_diff(old_text, patch['hunks'])
            new_encoding,_=tokenize.detect_encoding(io.BytesIO(new_text.encode('utf-8')).readline)
            if old_data.startswith(b'\xef\xbb\xbf') and new_encoding=='utf-8':new_encoding='utf-8-sig'
            hashes={'old':hashlib.sha256(old_data).hexdigest(),'new':hashlib.sha256(new_text.encode(new_encoding)).hexdigest()}
            indexes={'old':index_python(old_text),'new':index_python(new_text)}
            selected={}
            for side, path, text, changed_lines in (('old', old_path, old_text, old_lines), ('new', new_path, new_text, new_lines)):
                if path is None: continue
                owners = _owners(text, changed_lines)
                if old_path != new_path:
                    owners.append({'definition': indexes[side][0], 'changed_lines': []})
                    # Renames change the path component of every named identity.
                    if old_path is not None and new_path is not None:
                        owners.extend({'definition': d, 'changed_lines': []} for d in indexes[side][1:])
                for owner in owners:
                    ident=owner['definition']['kind'],owner['definition']['name']
                    previous=selected.setdefault(side,{}).setdefault(ident,owner)
                    previous['changed_lines']=sorted(set(previous['changed_lines']+owner['changed_lines']))
            # A pure insertion/deletion inside a definition still modifies that
            # definition: recover the unchanged counterpart from the other AST.
            if old_path==new_path:
                identities=set(selected.get('old',{}))|set(selected.get('new',{}))
                for side in ('old','new'):
                    for d in indexes[side]:
                        ident=d['kind'],d['name']
                        if ident in identities:selected.setdefault(side,{}).setdefault(ident,{'definition':d,'changed_lines':[]})
            patch_objects={}
            for side,owners in selected.items():
                path=old_path if side=='old' else new_path
                for owner in owners.values():
                    definition = owner['definition']; canonical = label + '/' + path
                    eid = source_identity(canonical, definition['kind'], definition['name'])
                    row = patch_objects.setdefault(eid, {'source_id': eid, 'path': canonical,
                        'kind': definition['kind'], 'qualname': definition['name'], 'object_type': definition['object_type'],
                        'language': 'python', 'status': 'ambiguous' if definition.get('ambiguous') else 'resolved', 'sides': {}})
                    if definition.get('ambiguous'):row['status']='ambiguous'
                    row['sides'][side] = {'source_sha256': hashes[side],
                        'definition_line': definition['line'], 'end_line': definition['end_line'],
                        'changed_lines': sorted(set(owner['changed_lines']))}
            objects.update(patch_objects)
        except (OSError, ValueError, SyntaxError, UnicodeError, subprocess.CalledProcessError) as error:
            unresolved.append({'old_path': old_path, 'new_path': new_path, 'reason': type(error).__name__ + ': ' + str(error)})
    for row in objects.values():
        row['change'] = 'modified' if len(row['sides']) == 2 else 'added' if 'new' in row['sides'] else 'deleted'
    return {'schema': 'changed-source-objects/1', 'identity': 'canonical source path + kind + lexical qualified name',
            'objects': sorted(objects.values(), key=lambda r: (r['path'], r['qualname'], r['kind'])), 'unresolved': unresolved}


def match_callchains(db_path, changes):
    """Read only. Never infer a call edge from a global dependency edge."""
    db = sqlite3.connect(Path(db_path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        results = []
        for obj in changes['objects']:
            eid = obj['source_id']; matches = []
            if obj['status'] == 'resolved':
                selectors = ["SELECT p.id AS path_id,s.slot_id,'called_definition' AS relation,'{}' AS evidence FROM paths p JOIN path_support s ON s.path_id=p.id WHERE p.entity_id=?",
                             "SELECT path_id,slot_id,'observed_object' AS relation,'{}' AS evidence FROM observed_objects WHERE entity_id=?"]
                if 'source_dependencies' in tables:
                    selectors.append('SELECT path_id,slot_id,relation,evidence FROM source_dependencies WHERE entity_id=?')
                sql = ' UNION ALL '.join(selectors)
                for row in db.execute(f'''SELECT DISTINCT q.*,p.api_id,c.test,se.source_sha256 FROM ({sql}) q
                    JOIN paths p ON p.id=q.path_id JOIN capture_slots c ON c.id=q.slot_id
                    LEFT JOIN slot_entities se ON se.slot_id=q.slot_id AND se.entity_id=?
                    WHERE c.status IN ('current','partial') ORDER BY p.api_id,q.path_id,q.relation''', [eid] * (len(selectors) + 1)):
                    value = dict(row); value['evidence'] = json.loads(value['evidence'])
                    value['matching_diff_sides'] = [side for side, version in obj['sides'].items() if version['source_sha256'] == value['source_sha256']]
                    value['version_status'] = 'matched' if value['matching_diff_sides'] else 'different_or_unavailable_source_version'
                    value['api_aliases'] = [r[0] for r in db.execute('SELECT alias FROM api_alias_support WHERE api_id=? AND slot_id=? ORDER BY alias', (value['api_id'], value['slot_id']))]
                    chain = []; path_id = value['path_id'];seen=set()
                    while path_id:
                        if path_id in seen:raise ValueError('cyclic_database_path')
                        seen.add(path_id)
                        node = db.execute('SELECT p.id,p.parent_id,e.id AS source_id,e.path,e.name,e.kind FROM paths p JOIN entities e ON e.id=p.entity_id WHERE p.id=?', (path_id,)).fetchone()
                        if node is None: raise ValueError('broken_database_path')
                        chain.append(dict(node)); path_id = node['parent_id']
                    value['call_path'] = list(reversed(chain)); matches.append(value)
            results.append({'object': obj, 'matches': matches,
                            'status': 'matched' if matches else 'ambiguous_source_object' if obj['status'] != 'resolved' else 'not_observed_in_current_database'})
        return {'schema': 'source-object-callchains/1', 'results': results,
                'dependency_table_present': 'source_dependencies' in tables,
                'active_capture_coverage':[{'slot':r['id'],'source_read_policy':json.loads(r['summary']).get('source_read_policy')}
                    for r in db.execute("SELECT id,summary FROM capture_slots WHERE status IN ('current','partial') ORDER BY id")],
                'semantics': 'Observed paths and labelled source dependencies; not proof that behavior changes.'}
    finally: db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('diff', type=Path)
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--base', default='HEAD', help='Commit corresponding to the old side of the diff')
    parser.add_argument('--before-root', type=Path, help='Old source snapshot directory instead of Git')
    parser.add_argument('--label', required=True, help='Same source-root label used during SQL replay')
    parser.add_argument('--db', type=Path, help='Optionally match objects against an existing call-chain database')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    try:
        result = identify_changes(args.diff.read_bytes().decode('utf-8'), repo=args.repo, label=args.label, base=args.base, before_root=args.before_root)
    except (OSError,ValueError) as error:
        result={'schema':'changed-source-objects/1','objects':[],
                'unresolved':[{'reason':type(error).__name__+': '+str(error)}]}
    if args.db: result['callchains'] = match_callchains(args.db, result)
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True); args.out.write_text(text, encoding='utf-8')
    else: print(text, end='')
    return 2 if result['unresolved'] or any(o['status']!='resolved' for o in result['objects']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
