"""Exact JSON collection channel; never parse pytest's terminal nodeids."""
import json, os
from pathlib import Path
import torch
import torch_npu

EXPECTED_SITE = Path('/home/workspace-ycy/compile-performance/cases/instrumented-r9-lld22-exclude-std/venv311/lib/python3.11/site-packages')
assert Path(torch.__file__).is_relative_to(EXPECTED_SITE), torch.__file__
assert Path(torch_npu.__file__).is_relative_to(EXPECTED_SITE), torch_npu.__file__


def pytest_configure(config):
    capture = config.getoption('--callchain-out', default=None)
    if not capture:
        return
    import api_test_relations.pytest_plugin as relations
    import api_test_relations
    assert api_test_relations.__version__=='0.1.12'
    import trace_api_chain.pytest_plugin as chain
    import trace_api_chain
    assert trace_api_chain.__version__=='0.3.5'
    from importlib.metadata import version
    assert version('api-test-relations')=='0.1.12'
    assert version('trace-api-chain')=='0.3.5'
    root = Path(__file__).resolve().parent/'plugin-runtime-035-private'
    assert root == Path('/home/workspace-ycy/compile-performance/pipeline-whitelist-dual-plugins-032-2026-10-28/plugin-runtime-035-private')
    assert root.is_dir() and not root.is_symlink(), root
    assert root.resolve() == root, root
    assert Path(relations.__file__).resolve().is_relative_to(root), relations.__file__
    assert Path(chain.__file__).resolve().is_relative_to(root), chain.__file__
    assert config.pluginmanager.hasplugin('api_test_relations.pytest_plugin')
    assert config.pluginmanager.hasplugin('trace_api_chain.pytest_plugin')
    assert config.getoption('--api-relations-out')
    Path(capture, 'dual-plugin-load.json').write_text(json.dumps({
        'relations':relations.__file__, 'callchain':chain.__file__,
        'torch':torch.__file__, 'torch_npu':torch_npu.__file__,
        'relations_catalog':config.getoption('--api-relations-catalog'),
        'callchain_catalog':config.getoption('--callchain-api-catalog'),
        'pid':os.getpid(),
    }, indent=2), encoding='utf-8')


def pytest_collection_finish(session):
    target = os.environ.get('TRACE_WHITELIST_COLLECTION')
    if target:
        Path(target).write_text(json.dumps([
            {'nodeid': i.nodeid, 'markers': sorted(set(m.name for m in i.iter_markers()))}
            for i in session.items
        ], ensure_ascii=False), encoding='utf-8')
