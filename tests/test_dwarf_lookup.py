import subprocess
from trace_api_chain.dwarf_lookup import lookup

def test_libdw_declarations_overloads_and_unmapped_pc(tmp_path):
    source=tmp_path/'sample.cpp'
    source.write_text('int api(int x) { return x+1; }\nint api(double x) { return (int)x+2; }\n')
    binary=tmp_path/'sample.so'
    subprocess.run(['c++','-g1','-O0','-shared','-fPIC',str(source),'-o',str(binary)],check=True)
    symbols=subprocess.check_output(['nm','--defined-only',str(binary)],text=True)
    offsets={parts[2]:int(parts[0],16) for line in symbols.splitlines() if len(parts:=line.split())==3 and parts[2].startswith('_Z3api')}
    rows=lookup(binary,list(offsets.values())+[0],tmp_path)
    assert rows['0']['function'] is None
    for name,pc in offsets.items():
        f=rows[str(pc)]['function']
        assert f['file']==str(source)
        assert f['name']=='api' and f['linkage_name']==name
        assert f['line']==(1 if name.endswith('i') else 2)
    assert len(offsets)==2
