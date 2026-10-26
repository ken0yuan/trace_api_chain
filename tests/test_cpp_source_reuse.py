import json
import subprocess
from trace_api_chain.source_entities import SourceEntities
from trace_api_chain.dwarf_lookup import lookup


def test_real_compiler_template_and_overload_identity(tmp_path):
    source=tmp_path/'objects.cpp'
    source.write_text('template<class T> __attribute__((noinline)) T same(T x) { return x; }\n'
                      '__attribute__((noinline)) int other(int x) { return x; } __attribute__((noinline)) double other(double x) { return x; }\n'
                      'int main() { return same(1)+same(2.0)+other(1)+other(2.0); }\n')
    binary=tmp_path/'objects'
    subprocess.run(['g++','-O0','-g1',str(source),'-o',str(binary)],check=True)
    symbols=subprocess.check_output(['nm','-n',str(binary)],text=True)
    selected={int(row.split()[0],16):row.split()[-1] for row in symbols.splitlines()
              if len(row.split())==3 and ('_Z4same' in row or '_Z5other' in row)}
    assert len(selected)==4
    definitions=lookup(binary,list(selected),tmp_path)
    resolver=SourceEntities({'fixture':tmp_path});groups={}
    for offset,symbol in selected.items():
        eid=resolver.cpp({'path':str(binary),'sha256':'fixture','offset':offset},definitions[str(offset)],{})
        assert resolver.entities[eid]['status']=='mapped',definitions[str(offset)]
        groups.setdefault('template' if '_Z4same' in symbol else 'overload',set()).add(eid)
    assert len(groups['template'])==1
    assert len(groups['overload'])==2
    entity=resolver.entities[next(iter(groups['template']))]
    assert len(entity['linkage_names'])==2


def test_no_coordinate_or_macro_name_is_not_guessed(tmp_path):
    source=tmp_path/'generated.cpp';source.write_text('MAKE_FUNCTIONS(foo)\n')
    resolver=SourceEntities()
    for column in (0,1):
        eid=resolver.cpp({'path':'x.so','offset':column}, {'function':dict(file=str(source),line=1,column=column,name='foo',kind='definition')},{})
        assert resolver.entities[eid]['status']=='unresolved'


def test_installed_header_copy_reuses_verified_repository_source(tmp_path):
    root=tmp_path/'repo';header=root/'c10/fixture.h';header.parent.mkdir(parents=True)
    header.write_text('int same(int x) { return x; }\n')
    installed=tmp_path/'venv/site-packages/torch/include/c10/fixture.h'
    installed.parent.mkdir(parents=True);installed.write_bytes(header.read_bytes())
    resolver=SourceEntities({'pytorch':root})
    assert resolver.path(installed)==resolver.path(header)
    installed.write_text('int different(int x) { return x; }\n')
    resolver=SourceEntities({'pytorch':root})
    assert resolver.path(installed)!=resolver.path(header)
