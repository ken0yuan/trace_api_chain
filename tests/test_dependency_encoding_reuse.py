import json
import math
from trace_api_chain import database


def reference(values):
    return [(*key,json.dumps([json.loads(v) for v in sorted(items)],ensure_ascii=False))
            for key,items in sorted(values.items())]


def test_exact_evidence_spelling_and_distinct_path_support():
    payloads=[{'文字':'a\n\t\\"','list':[1,None,True,-0.0,1e100]},
              {'escaped':'\u2028😀'}, {'nested':{'z':2,'a':1}}, {'nan':math.nan}]
    strings={json.dumps(p,sort_keys=True,ensure_ascii=ascii) for p in payloads for ascii in (True,False)}
    values={('path:'+str(i),'entity','read'):strings for i in range(25)}
    values[('empty','entity','read')]=set()
    result=database.encode_dependency_rows(values)
    assert result==reference(values)
    assert len(result)==26


def test_each_distinct_value_is_decoded_once(monkeypatch):
    original=json.loads;calls=[]
    def counted(value,*args,**kwargs):
        calls.append(value);return original(value,*args,**kwargs)
    monkeypatch.setattr(database.json,'loads',counted)
    strings={'{"line": 1}','{"line": 2}'}
    values={('p'+str(i),'e','r'):strings for i in range(100)}
    result=database.encode_dependency_rows(values)
    assert len(result)==100 and len(calls)==2
