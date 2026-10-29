import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from trace_api_chain import symbols


def tool(monkeypatch,tmp_path):
    binary=tmp_path/'library.so';binary.write_bytes(b'fixed ELF fixture')
    monkeypatch.setattr(symbols.shutil,'which',lambda name:'/fixture/eu-addr2line')
    monkeypatch.setattr(symbols.subprocess,'check_output',lambda *a,**k:'fixture-symbolizer 1\n')
    calls=[]
    def run(command,**kwargs):
        offsets=[int(arg,16) for arg in command[command.index('-e')+2:]]
        calls.append(offsets)
        text=''.join(hex(n)+'\nfunction_'+str(n)+'\n'+str(tmp_path/'code.cpp')+':1\n' for n in offsets)
        return SimpleNamespace(stdout=text)
    monkeypatch.setattr(symbols.subprocess,'run',run)
    (tmp_path/'code.cpp').write_text('int original;\n')
    identities=lambda offsets:[{'path':str(binary),'offset':n,'sha256':symbols.sha(binary)} for n in offsets]
    return binary,identities,calls


def contents(root):return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_readonly_index_retains_all_offsets_inline_frames_and_current_source(tmp_path,monkeypatch):
    binary,identities,calls=tool(monkeypatch,tmp_path)
    old=symbols.resolve(identities([1,2]),tmp_path/'one');assert calls==[[1,2]]
    index=tmp_path/'index';symbols.publish_symbol_index([tmp_path/'one'],index)
    before=contents(index);calls.clear()
    assert symbols.resolve(identities([1,2]),tmp_path/'two',index_dir=index)==old
    assert calls==[] and contents(index)==before
    result=symbols.resolve(identities([1,2,3]),tmp_path/'three',index_dir=index)
    assert calls==[[3]] and len(result)==3 and contents(index)==before
    (tmp_path/'code.cpp').write_text('int changed;\n')
    result=symbols.resolve(identities([1]),tmp_path/'four',index_dir=index)
    assert result[str(binary),1]['frames'][0]['context'][0]['text']=='int changed;'
    assert contents(index)==before


@pytest.mark.parametrize('corrupt',['manifest_parser','tool_version','entry_hash','cache_hash','binary'])
def test_stale_or_corrupt_index_falls_back_to_full_private_resolution(tmp_path,monkeypatch,corrupt):
    binary,identities,calls=tool(monkeypatch,tmp_path)
    symbols.resolve(identities([1,2]),tmp_path/'one');index=tmp_path/'index'
    manifest=symbols.publish_symbol_index([tmp_path/'one'],index);key=next(iter(manifest['entries']))
    if corrupt=='manifest_parser':manifest['parser_sha256']='wrong'
    elif corrupt=='tool_version':manifest['tool_version']='a different symbolizer'
    elif corrupt=='entry_hash':manifest['entries'][key]['sha256']='wrong'
    elif corrupt=='cache_hash':
        path=index/(key+'.json');cache=json.loads(path.read_text());cache['sha256']='wrong';path.write_text(json.dumps(cache))
        manifest['entries'][key]['sha256']=symbols.sha(path)
    else:binary.write_bytes(b'a different binary')
    (index/'manifest.json').write_text(json.dumps(manifest));before=contents(index);calls.clear()
    result=symbols.resolve(identities([1,2]),tmp_path/'two',index_dir=index)
    assert calls==[[1,2]] and len(result)==2 and contents(index)==before
    assert all(value['status']=='mapped' for value in result.values())


def test_publication_unions_compatible_private_results_and_rejects_conflicts(tmp_path,monkeypatch):
    binary,identities,calls=tool(monkeypatch,tmp_path)
    symbols.resolve(identities([1,2]),tmp_path/'one');symbols.resolve(identities([2,3]),tmp_path/'two')
    index=tmp_path/'index';symbols.publish_symbol_index([tmp_path/'one',tmp_path/'two'],index);calls.clear()
    assert len(symbols.resolve(identities([1,2,3]),tmp_path/'three',index_dir=index))==3 and calls==[]
    with pytest.raises(FileExistsError):symbols.publish_symbol_index([tmp_path/'one'],index)
    p=next((tmp_path/'two').glob('*.json'));saved=json.loads(p.read_text());saved['payload']['2'][0]['symbol']='conflict'
    saved['sha256']=hashlib.sha256(json.dumps(saved['payload'],sort_keys=True).encode()).hexdigest();p.write_text(json.dumps(saved))
    with pytest.raises(ValueError,match='conflicting_entries'):
        symbols.publish_symbol_index([tmp_path/'one',tmp_path/'two'],tmp_path/'conflict')
    assert not (tmp_path/'conflict/manifest.json').exists()


def test_parallel_consumers_only_write_their_private_caches(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    binary,identities,calls=tool(monkeypatch,tmp_path)
    expected=symbols.resolve(identities([1,2]),tmp_path/'producer');index=tmp_path/'index'
    symbols.publish_symbol_index([tmp_path/'producer'],index);before=contents(index);calls.clear()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda n:symbols.resolve(identities([1,2]),tmp_path/('private'+str(n)),index_dir=index),range(8)))
    assert all(result==expected for result in results) and calls==[]
    assert contents(index)==before
    assert all(len(list((tmp_path/('private'+str(n))).glob('*.json')))==1 for n in range(8))
