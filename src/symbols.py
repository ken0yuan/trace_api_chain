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


def resolve(identities, cache_dir, timeout=600):
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
            cache = {}
            if cache_path.exists():
                saved = json.loads(cache_path.read_text())
                payload = saved.get('payload', {})
                payload_sha = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                if saved.get('sha256') == payload_sha:
                    cache = payload
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
