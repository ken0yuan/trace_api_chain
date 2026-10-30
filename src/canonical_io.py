"""Write the same canonical JSON bytes without one graph-sized string/bytes pair."""
import json
import os
import tempfile


def iter_canonical_bytes(value):
    encoder=json.JSONEncoder(sort_keys=True,ensure_ascii=False,separators=(',',':'))
    def chunks(item,levels):
        # Graph keys are strings. Let the standard encoder handle other key
        # types, including its original conversion/errors, without inventing
        # different JSON spelling. C encoding remains in use for each subtree.
        if levels and type(item) is dict and all(type(key) is str for key in item):
            yield '{'
            for index,key in enumerate(sorted(item)):
                if index:yield ','
                yield encoder.encode(key);yield ':'
                yield from chunks(item[key],levels-1)
            yield '}'
        elif levels and type(item) in (list,tuple):
            yield '['
            for index,child in enumerate(item):
                if index:yield ','
                yield from chunks(child,levels-1)
            yield ']'
        else:yield encoder.encode(item)
    for chunk in chunks(value,2):yield chunk.encode('utf-8')
    yield b'\n'


def write_canonical(path,value):
    # Keep a previously valid destination intact if encoding fails. Retain a
    # failed temporary artifact for diagnosis, never discard source evidence.
    with tempfile.NamedTemporaryFile(mode='wb',buffering=1024*1024,dir=path.parent,
                                    prefix=path.name+'.',suffix='.tmp',delete=False) as stream:
        for chunk in iter_canonical_bytes(value):stream.write(chunk)
        temporary=stream.name
    os.replace(temporary,path)
