"""Export recorded invocations as source-backed analysis trees, without recollection.

No operator names, source lines, binary addresses or backend paths are built in.
Analyst documents are produced separately by an agent, not by this exporter.
"""
from __future__ import annotations
import argparse
import copy
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def sha(path):
    with open(path, 'rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def parse_locations(text):
    """Parse eu-addr2line -a -A -f -C -i; retain inlining as source scopes."""
    result, current, pending = {}, None, None
    for line in text.splitlines():
        if re.fullmatch(r'0x[0-9a-fA-F]+', line):
            if pending is not None:
                raise ValueError('incomplete symbolizer frame')
            current = int(line, 16)
            result[current] = []
            pending = None
        elif current is not None:
            if pending is None:
                pending = line
            else:
                match = re.match(r'^(.*?):(\d+)(?::(\d+))?(?:\s.*)?$', line)
                result[current].append({'symbol': pending,
                    'file': match[1] if match and match[1] != '??' else None,
                    'line': int(match[2]) if match else None,
                    'column': int(match[3]) if match and match[3] else None,
                    'raw_location': line})
                pending = None
    if pending is not None:
        raise ValueError('incomplete symbolizer frame')
    return result


def snapshot(frame, files):
    result = dict(frame)
    filename, line = frame.get('file'), frame.get('line')
    if not filename or not line:
        return {**result, 'status': 'unresolved', 'reason': 'no_debug_execution_line'}
    if filename not in files:
        p = Path(filename)
        files[filename] = (p.read_text(errors='replace').splitlines(), sha(p)) if p.is_file() else None
    value = files[filename]
    if value is None:
        return {**result, 'status': 'source_missing'}
    lines, digest = value
    return {**result, 'status': 'source_snapshot', 'source_sha256': digest,
            'build_source_match_verified': False,
            'context': [{'line': i+1, 'text': lines[i]}
                        for i in range(max(0, line-4), min(len(lines), line+3))]}


def read_symbol_cache(path):
    """Read a complete hash-checked derived cache; corrupt caches are misses."""
    try:
        saved=json.loads(Path(path).read_text())
        payload=saved.get('payload',{})
        if not isinstance(payload,dict):return {}
        expected=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        return payload if saved.get('sha256')==expected else {}
    except (OSError,ValueError,TypeError):return {}


def publish_symbol_index(cache_dirs,destination):
    """Publish a new immutable seed, never updating an index used by workers."""
    destination=Path(destination)
    destination.mkdir(parents=True,exist_ok=False)
    entries={}
    for directory in cache_dirs:
        for path in sorted(Path(directory).glob('*.json')):
            if re.fullmatch(r'[0-9a-f]{64}',path.stem) is None:continue
            payload=read_symbol_cache(path)
            if not payload:continue
            if path.stem in entries:
                previous=read_symbol_cache(destination/path.name)
                if any(offset in previous and previous[offset]!=frames for offset,frames in payload.items()):
                    raise ValueError('symbol_index_conflicting_entries:'+path.stem)
                payload={**previous,**payload}
            # Publish the validated spelling, not bytes that could change
            # between validation and copying from a private producer cache.
            data=json.dumps({'payload':payload,'sha256':hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()}).encode()
            (destination/path.name).write_bytes(data)
            entries[path.stem]={'file':path.name,'sha256':hashlib.sha256(data).hexdigest()}
    tool=shutil.which('eu-addr2line')
    if not tool:raise RuntimeError('eu-addr2line is required')
    manifest={'schema':'readonly-symbol-index/1','parser_sha256':sha(__file__),
              'tool_version':subprocess.check_output([tool,'--version'],text=True).splitlines()[0],
              'entries':entries}
    # A missing manifest is an incomplete publication and is never consumed.
    from .cache_io import atomic_json
    atomic_json(destination/'manifest.json',manifest)
    return manifest


def indexed_symbols(index_dir,key,parser,version):
    if index_dir is None:return {}
    try:
        root=Path(index_dir);manifest=json.loads((root/'manifest.json').read_text())
        if (manifest.get('schema')!='readonly-symbol-index/1' or
            manifest.get('parser_sha256')!=parser or manifest.get('tool_version')!=version):return {}
        entry=manifest.get('entries',{}).get(key)
        if not entry or entry.get('file')!=key+'.json':return {}
        path=root/entry['file'];data=path.read_bytes()
        if hashlib.sha256(data).hexdigest()!=entry.get('sha256'):return {}
        saved=json.loads(data);payload=saved.get('payload',{})
        if not isinstance(payload,dict):return {}
        expected=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        return payload if saved.get('sha256')==expected else {}
    except (OSError,ValueError,TypeError,KeyError):return {}


def resolve(identities, cache_dir, timeout=600, index_dir=None):
    tool = shutil.which('eu-addr2line')
    if not tool:
        raise RuntimeError('Install elfutils: eu-addr2line is required')
    version = subprocess.check_output([tool, '--version'], text=True).splitlines()[0]
    parser = sha(__file__)
    groups, answer, files = {}, {}, {}
    for identity in identities:
        path, offset = identity.get('path'), identity.get('offset')
        if path and isinstance(offset, int):
            groups.setdefault(path, {}).setdefault(offset, []).append(identity)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    for path, addresses in groups.items():
        print('Source mapping: '+Path(path).name+' ('+str(len(addresses))+' addresses)', flush=True)
        try:
            digest = sha(path)
            before = os.stat(path)
            key = hashlib.sha256((digest+version+parser).encode()).hexdigest()
            cache_path = cache_dir/(key+'.json')
            cache = indexed_symbols(index_dir,key,parser,version)
            private=read_symbol_cache(cache_path)
            if any(offset in cache and cache[offset]!=frames for offset,frames in private.items()):
                cache={}  # Conflicting derived evidence requires fresh resolution.
            else:cache.update(private)
            todo = []
            for offset, observations in addresses.items():
                if not all(i.get('sha256') == digest for i in observations):
                    answer[path, offset] = {'status': 'unresolved', 'reason': 'binary_identity_changed'}
                elif any('<ELF PLT stub>' in (i.get('function') or {}).get('symbols', []) for i in observations):
                    answer[path, offset] = {'status': 'machine_code', 'reason': 'linker_stub', 'frames': []}
                elif str(offset) not in cache:
                    todo.append(offset)
            if todo:
                proc = subprocess.run([tool, '-a', '-A', '-f', '-C', '-i', '-e', path,
                                       *map(hex, sorted(todo))], capture_output=True, text=True,
                                      timeout=timeout, check=True, env={**os.environ, 'DEBUGINFOD_URLS': ''})
                parsed = parse_locations(proc.stdout)
                if set(parsed) != set(todo):
                    raise ValueError('symbolizer address response mismatch')
                cache.update({str(k): v for k, v in parsed.items()})
            after = os.stat(path)
            if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('binary changed during source lookup')
            cache_path.write_text(json.dumps({'payload': cache,
                'sha256': hashlib.sha256(json.dumps(cache, sort_keys=True).encode()).hexdigest()}))
            for offset in addresses:
                if (path, offset) not in answer:
                    frames = [snapshot(f, files) for f in cache.get(str(offset), [])]
                    answer[path, offset] = {'status': 'mapped' if any(f.get('line') for f in frames) else 'unresolved',
                        'evidence': 'matching_ELF_DWARF_execution_location', 'frames': frames,
                        'inline_scopes_are_runtime_calls': False, 'module_sha256': digest, 'tool': version}
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            for offset in addresses:
                answer[path, offset] = {'status': 'unresolved', 'reason': str(error), 'frames': []}
    return answer
