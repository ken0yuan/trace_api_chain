"""Actual compiler hooks: preserve branches and reuse paths across invocations."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import sysconfig

import pytest

C_SOURCE=r'''
#include <Python.h>
static int leaf_a(void) { return 1; }
static int leaf_b(void) { return 2; }
static long descend(int n) { return n ? descend(n-1) : 1; }
static long choose(int n,int branch) {
    if(branch==2) return descend(n);
    long result=0;
    for(int i=0;i<n;i++) result+=branch ? leaf_b() : leaf_a();
    return result;
}
static PyObject *run(PyObject *self,PyObject *args) {
    int n,branch;
    if(!PyArg_ParseTuple(args,"ii",&n,&branch)) return NULL;
    return PyLong_FromLong(choose(n,branch));
}
static PyMethodDef methods[]={{"run",run,METH_VARARGS,"run"},{NULL,NULL,0,NULL}};
static struct PyModuleDef module={PyModuleDef_HEAD_INIT,"native_fixture",NULL,-1,methods};
PyMODINIT_FUNC PyInit_native_fixture(void) { return PyModule_Create(&module); }
'''

def setup_fixture(root):
    source=root/'native_fixture.c';source.write_text(C_SOURCE)
    target=root/('native_fixture'+sysconfig.get_config_var('EXT_SUFFIX'))
    subprocess.run(['gcc','-shared','-fPIC','-O0','-g1','-finstrument-functions',
                    '-I'+sysconfig.get_path('include'),str(source),'-o',str(target)],check=True)
    (root/'fixture_api.py').write_text('import native_fixture\ndef api_a(n): return native_fixture.run(n,0)\ndef api_b(n): return native_fixture.run(n,1)\ndef unlisted(n): return n\n')
    (root/'test_case.py').write_text('import os,fixture_api\ndef test_one():\n for _ in range(int(os.environ.get("REPEATS","2"))):\n  fixture_api.api_a(int(os.environ.get("INNER","2")))\n  fixture_api.api_b(int(os.environ.get("INNER","2")))\n  fixture_api.unlisted(7)\n')
    ledger={'apis':[{'api_id':'python:'+name,'qualified_name':name,'language':'python','kind':'function'}
                    for name in ('fixture_api.api_a','fixture_api.api_b')]}
    (root/'ledger.json').write_text(json.dumps(ledger))

def capture(root,name,storage,inner=2,repeats=2,max_paths=262144):
    env=os.environ.copy();env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1',INNER=str(inner),REPEATS=str(repeats))
    env.pop('TRACE_API_CHAIN_INTERNAL',None);env.pop('SCENARIO_API_FILTER',None)
    cap=root/name
    cmd=[sys.executable,'-m','pytest','-p','trace_api_chain.pytest_plugin','test_case.py::test_one','-q',
         '--callchain-namespaces=fixture_api','--callchain-api-catalog='+str(root/'ledger.json'),
         '--callchain-out='+str(cap),'--callchain-native-storage='+storage,'--callchain-max-paths='+str(max_paths)]
    p=subprocess.run(cmd,cwd=root,env=env,capture_output=True,text=True,timeout=120)
    return p,cap

def import_graph(cap,root):
    from trace_api_chain.pipeline import prepare
    from trace_api_chain.database import refresh,query
    data=prepare(cap);db=cap/'graph.sqlite'
    refresh(db,data,roots={'fixture':str(root)},slot='fixture')
    connection=sqlite3.connect(db)
    paths=connection.execute('select * from paths order by id').fetchall();connection.close()
    docs={name:query(db,'fixture_api.'+name) for name in ('api_a','api_b')}
    return data,paths,docs

def test_paths_equal_raw_and_size_does_not_follow_repetitions(tmp_path):
    setup_fixture(tmp_path)
    raw,raw_cap=capture(tmp_path,'raw','events')
    small,small_cap=capture(tmp_path,'small','paths')
    large,large_cap=capture(tmp_path,'large','paths',inner=2000,repeats=50)
    for p in (raw,small,large):assert p.returncode==0,p.stdout+p.stderr
    raw_data,raw_paths,raw_docs=import_graph(raw_cap,tmp_path)
    small_data,small_paths,small_docs=import_graph(small_cap,tmp_path)
    large_data,large_paths,large_docs=import_graph(large_cap,tmp_path)
    assert raw_paths==small_paths==large_paths
    assert (small_cap/'native-paths.bin').stat().st_size==(large_cap/'native-paths.bin').stat().st_size
    assert large_data['graph']['native_activations']>small_data['graph']['native_activations']*1000
    assert large_data['graph']['native_path_nodes']==small_data['graph']['native_path_nodes']
    for docs in (raw_docs,small_docs,large_docs):
        a={e['name'] for e in docs['api_a']['entities'].values()}
        b={e['name'] for e in docs['api_b']['entities'].values()}
        assert 'leaf_a' in a and 'leaf_b' not in a
        assert 'leaf_b' in b and 'leaf_a' not in b
    catalog=json.loads((large_cap/'api-catalog.json').read_text())
    assert catalog['shared_ledger']['apis']==2
    assert set(catalog['api_ids'])=={'runtime:fixture_api.api_a','runtime:fixture_api.api_b'}
    before=(large_cap/'graph.json').read_bytes()
    from trace_api_chain.pipeline import prepare
    prepare(large_cap)
    assert before==(large_cap/'graph.json').read_bytes()

def test_compact_path_budget_failure_is_not_complete(tmp_path):
    setup_fixture(tmp_path)
    result,cap=capture(tmp_path,'limited','paths',max_paths=1)
    assert result.returncode!=0
    receipt=json.loads((cap/'capture-result.json').read_text())
    assert receipt['status']=='capture_incomplete' and receipt['gaps']
    assert 'native_path_node_budget_exceeded' in receipt['gaps']
    assert not any(reason.startswith('collector_error:') for reason in receipt['gaps'])
    from trace_api_chain.compact import decode_paths
    with pytest.raises(ValueError,match='native_path_capture_loss'):decode_paths(cap)


def test_preload_does_not_break_small_stack_threads(tmp_path):
    # Vendor runtimes create threads with explicit small stacks. Large static
    # TLS in a preloaded collector can make pthread_create fail with EINVAL.
    from trace_api_chain.native import build_hook
    source=tmp_path/'thread.c';binary=tmp_path/'thread'
    source.write_text('#include <pthread.h>\n#include <stdio.h>\n#include <unistd.h>\n'
                      'static void *run(void *p) { return p; }\n'
                      'int main(void) { pthread_attr_t a; pthread_t t; pthread_attr_init(&a); '
                      'size_t size=(size_t)sysconf(_SC_THREAD_STACK_MIN); if(size<65536) size=65536; '
                      'int r=pthread_attr_setstacksize(&a,size); if(r) return r; '
                      'r=pthread_create(&t,&a,run,0); printf("pthread_create=%d\\n",r); '
                      'if(r) return r; return pthread_join(t,0); }\n')
    subprocess.run(['gcc',str(source),'-pthread','-o',str(binary)],check=True)
    hook=build_hook(tmp_path)
    baseline_env=dict(os.environ);baseline_env.pop('LD_PRELOAD',None)
    baseline=subprocess.run([str(binary)],env=baseline_env,capture_output=True,text=True)
    assert baseline.returncode==0,baseline.stdout+baseline.stderr
    result=subprocess.run([str(binary)],env={**os.environ,'LD_PRELOAD':str(hook)},capture_output=True,text=True)
    assert result.returncode==0,result.stdout+result.stderr


def test_repeated_source_object_does_not_shorten_recursive_path(tmp_path):
    setup_fixture(tmp_path)
    # Same source object appears five times on one real call stack.
    (tmp_path/'fixture_api.py').write_text('import native_fixture\ndef api_a(n): return native_fixture.run(n,2)\ndef api_b(n): return n\n')
    (tmp_path/'test_case.py').write_text('import fixture_api\ndef test_one(): fixture_api.api_a(4)\n')
    results=[]
    from trace_api_chain.pipeline import prepare
    from trace_api_chain.database import refresh,query
    for storage in ('events','paths'):
        process,cap=capture(tmp_path,storage,storage)
        assert process.returncode==0,process.stdout+process.stderr
        db=cap/'graph.sqlite';refresh(db,prepare(cap),roots={'fixture':str(tmp_path)},slot='fixture')
        doc=query(db,'fixture_api.api_a')
        recursive=[n for n in doc['nodes'] if doc['entities'][n['entity_id']]['name']=='descend']
        assert len(recursive)==5
        assert len({n['entity_id'] for n in recursive})==1
        recursive.sort(key=lambda n:n['depth'])
        assert all(b['parent_id']==a['id'] for a,b in zip(recursive,recursive[1:]))
        results.append([(n['id'],n['depth']) for n in recursive])
    assert results[0]==results[1]


@pytest.mark.parametrize('storage',['events','paths'])
def test_native_number_slot_without_c_profile_event_is_inside_python_window(tmp_path,storage):
    setup_fixture(tmp_path)
    source=C_SOURCE.replace('static PyMethodDef methods[]=',"""static PyObject *slot_add(PyObject *left,PyObject *right) { return PyLong_FromLong(42); }
static PyType_Slot number_slots[]={{Py_tp_new,PyType_GenericNew},{Py_nb_add,slot_add},{0,NULL}};
static PyType_Spec number_spec={"native_fixture.Number",sizeof(PyObject),0,Py_TPFLAGS_DEFAULT,number_slots};
static PyMethodDef methods[]=""").replace('return PyModule_Create(&module);','PyObject *m=PyModule_Create(&module); PyObject *type=PyType_FromSpec(&number_spec); if(!m || !type) return NULL; PyModule_AddObject(m,"Number",type);return m;')
    c=tmp_path/'native_fixture.c';c.write_text(source)
    target=tmp_path/('native_fixture'+sysconfig.get_config_var('EXT_SUFFIX'))
    subprocess.run(['gcc','-shared','-fPIC','-O0','-g1','-finstrument-functions','-I'+sysconfig.get_path('include'),str(c),'-o',str(target)],check=True)
    (tmp_path/'fixture_api.py').write_text('import native_fixture\ndef api_a(n):\n left=native_fixture.Number();right=native_fixture.Number()\n return left+right\ndef api_b(n):return n\n')
    (tmp_path/'test_case.py').write_text('import fixture_api\ndef test_one():\n assert fixture_api.api_a(1)==42\n assert fixture_api.api_b(2)==2\n')
    process,cap=capture(tmp_path,'slot',storage)
    assert process.returncode==0,process.stdout+process.stderr
    rows=[json.loads(line) for line in (cap/'events.jsonl').read_text().splitlines()]
    assert not any(r['event']=='call' and r['kind']=='native_boundary' for r in rows)
    data,paths,docs=import_graph(cap,tmp_path)
    assert data['graph']['native_activations']>0 and not data['graph']['gaps']
    assert any(e['name']=='slot_add' for e in docs['api_a']['entities'].values())


def test_cpython_only_native_boundaries_are_valid_empty_compiler_trace(tmp_path):
    setup_fixture(tmp_path)
    (tmp_path/'fixture_api.py').write_text('def api_a(n):return len([n])\ndef api_b(n):return n\n')
    (tmp_path/'test_case.py').write_text('import fixture_api\ndef test_one():assert fixture_api.api_a(1)==1\n')
    process,cap=capture(tmp_path,'python-only','paths')
    assert process.returncode==0,process.stdout+process.stderr
    from trace_api_chain.pipeline import prepare
    data=prepare(cap)
    assert not data['graph']['gaps'] and data['graph']['native_activations']==0
