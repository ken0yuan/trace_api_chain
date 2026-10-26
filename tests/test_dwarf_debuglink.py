import subprocess
import pytest
from trace_api_chain.dwarf_lookup import lookup


@pytest.mark.parametrize('debug_file_present', [False, True])
def test_libdw_split_debug_file(tmp_path, debug_file_present):
    source = tmp_path / 'sample.c'
    source.write_text('int sample_api(int x) { return x + 1; }\n')
    binary = tmp_path / 'sample.so'
    debug = tmp_path / 'sample.so.debug'
    subprocess.run(['cc', '-g1', '-O0', '-shared', '-fPIC', str(source), '-o', str(binary)], check=True)
    symbols = subprocess.check_output(['nm', '--defined-only', str(binary)], text=True)
    pc = next(int(parts[0], 16) for line in symbols.splitlines()
              if len(parts := line.split()) == 3 and parts[2] == 'sample_api')
    subprocess.run(['objcopy', '--only-keep-debug', str(binary), str(debug)], check=True)
    subprocess.run(['objcopy', '--strip-debug', '--add-gnu-debuglink=' + str(debug), str(binary)], check=True)
    if not debug_file_present:
        debug.unlink()
    rows = lookup(binary, [0, pc], tmp_path)
    assert rows['0']['function'] is None
    function = rows[str(pc)]['function']
    if debug_file_present:
        assert function['file'] == str(source)
        assert function['line'] == 1
        assert function['name'] == 'sample_api'
    else:
        assert function is None
