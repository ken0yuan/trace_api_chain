"""Bounded offline libdw subprocess; failure leaves explicit unmapped evidence."""
import json
from pathlib import Path
import subprocess
from .native import digest

def reader_source():
    return Path(__file__).with_name('dwarf_lookup.c')

def lookup(path, offsets, cache_dir):
    source=reader_source()
    executable=Path(cache_dir)/('dwarf-lookup-'+digest(source)[:20])
    if not executable.exists():
        subprocess.run(['cc','-O2',str(source),'-ldw','-lelf','-o',str(executable)],check=True,
                       capture_output=True,text=True,timeout=60)
    try:
        process=subprocess.run([str(executable),str(path)],input=''.join(f'{o:x}\n' for o in offsets),
                               capture_output=True,text=True,timeout=240)
        if process.returncode:raise RuntimeError(f'libdw_exit_{process.returncode}: {process.stderr[:500]}')
        rows={str(row['offset']):row for row in map(json.loads,process.stdout.splitlines())}
        if set(rows)!=set(map(str,offsets)):raise RuntimeError('incomplete_libdw_response')
        for row in rows.values():
            function=row.get('function')
            if function and function.get('file') and not Path(function['file']).is_absolute():
                if function.get('comp_dir'):function['file']=str(Path(function['comp_dir'])/function['file'])
                else:row['function']=None;row['reason']='relative_declaration_without_compilation_directory'
        return rows
    except (RuntimeError,subprocess.TimeoutExpired) as error:
        # Do not cache transient resource failures as successful resolutions.
        raise RuntimeError(f'DWARF lookup failed for {path}: {error}') from error
