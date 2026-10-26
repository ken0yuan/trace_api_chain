"""Execute selected upstream methods without changing their bodies."""
import ast
import contextlib
import hashlib
import json
import os
import pathlib
import random
import re
import sys
import types
import unittest

import torch
DEVICE = os.environ.get('SCENARIO_DEVICE', 'cpu')
if DEVICE == 'npu':
    import torch_npu
import torch.nn as nn
import torch.nn.init as init
from torch.testing._internal.common_utils import TestCase, HardwareClassification
from torch.testing._internal.common_nn import NNTestCase
from torch.testing._internal.common_device_type import (
    dtypes, skipMeta, skipMPS, instantiate_device_type_tests,
)
from torch.testing._internal.common_dtype import (
    all_types_complex_float8_and, all_types_and_complex_and,
)

ROOT = pathlib.Path(__file__).resolve().parent
REFERENCE = ROOT / 'upstream-reference'
SPECS = [
    ('C03_fill', 'test/test_tensor_creation_ops.py', 'TestTensorCreation', 'test_fill_all_dtypes_and_devices', True, ['Tensor.fill_']),
    ('C03_diagonal', 'test/test_torch.py', 'TestTorch', 'test_fill_diagonal', False, ['Tensor.fill_diagonal_']),
    ('C03_constant', 'test/nn/test_init.py', 'TestNNInit', 'test_constant', False, ['nn.init.constant_', 'Tensor.fill_']),
    ('C03_ones_zeros', 'test/nn/test_init.py', 'TestNNInit', 'test_ones_and_zeros', False, ['nn.init.ones_', 'nn.init.zeros_']),
    ('C04_clone', 'test/test_torch.py', 'TestTorchDeviceType', 'test_clone_all_dtypes_and_devices', True, ['Tensor.clone']),
    ('C04_broadcast', 'test/test_torch.py', 'TestDevicePrecision', 'test_copy_broadcast', True, ['Tensor.copy_']),
    ('C04_transpose', 'test/test_torch.py', 'TestTorch', 'test_copy_transpose', False, ['Tensor.copy_']),
    ('C05_to', 'test/test_nn.py', 'TestNNDeviceType', 'test_to', True, ['nn.Module.to']),
    ('C05_empty', 'test/test_nn.py', 'TestNNDeviceType', 'test_module_to_empty', True, ['nn.Module.to_empty', 'nn.Module.to']),
    ('C05_empty_nonrecursive', 'test/test_nn.py', 'TestNNDeviceType', 'test_module_to_empty_non_recursive', True, ['nn.Module.to_empty']),
]

def build_cases():
    modules = {}
    provenance = {}
    for filename in dict.fromkeys(s[1] for s in SPECS):
        path = REFERENCE / filename
        source = path.read_text()
        tree = ast.parse(source, filename=str(path))
        specs = [s for s in SPECS if s[1] == filename]
        body = []
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef) or cls.name not in {s[2] for s in specs}:
                continue
            methods = {s[3] for s in specs if s[2] == cls.name}
            helpers = {'setUp', 'tearDown', 'setUpClass', 'tearDownClass'}
            if cls.name == 'TestNNInit':
                helpers |= {'_create_random_nd_tensor', '_random_float'}
            kept = []
            for node in cls.body:
                if isinstance(node, ast.FunctionDef):
                    if node.name not in methods | helpers:
                        continue
                    if node.name in methods:
                        segment = ast.get_source_segment(source, node)
                        provenance[(filename, cls.name, node.name)] = {
                            'line': node.lineno,
                            'method_sha256': hashlib.sha256(segment.encode()).hexdigest(),
                        }
                    kept.append(node)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    kept.append(node)
            cls.body = kept
            body.append(cls)
        module = types.ModuleType('selected_' + filename.replace('/', '_').replace('.', '_'))
        module.__file__ = str(path)
        module.__dict__.update({k: v for k, v in globals().items() if not k.startswith('__')})
        sys.modules[module.__name__] = module
        exec(compile(ast.Module(body=body, type_ignores=[]), str(path), 'exec'), module.__dict__)
        for clsname in dict.fromkeys(s[2] for s in specs if s[4]):
            instantiate_device_type_tests(module.__dict__[clsname], module.__dict__, only_for=('cpu',) if DEVICE == 'cpu' else ('privateuse1',))
        modules[filename] = module
    cases = {}
    for label, filename, clsname, method, parametrized, apis in SPECS:
        module = modules[filename]
        if parametrized:
            candidates = [(n, c) for n, c in module.__dict__.items()
                          if isinstance(c, type) and n.startswith(clsname) and n != clsname]
            for actual_cls, cls in candidates:
                for actual_method in sorted(dir(cls)):
                    if not any(actual_method.startswith(method + '_' + device) for device in ('cpu', 'npu')):
                        continue
                    device = 'cpu' if actual_cls.endswith('CPU') else 'npu'
                    ident = label + '__' + actual_method.removeprefix(method + '_')
                    cases[ident] = {'id': ident, 'class': cls, 'method': actual_method,
                        'device': device, 'adapted_default_device': False, 'spec': label,
                        'source': filename, 'original_class': clsname, 'original_method': method,
                        'apis': apis, **provenance[(filename, clsname, method)]}
        else:
            for device in (DEVICE,):
                ident = label + '__' + device
                cases[ident] = {'id': ident, 'class': module.__dict__[clsname], 'method': method,
                    'device': device, 'adapted_default_device': device == 'npu', 'spec': label,
                    'source': filename, 'original_class': clsname, 'original_method': method,
                    'apis': apis, **provenance[(filename, clsname, method)]}
    return cases

CASES = build_cases()
_READY_CLASSES = set()

def manifest():
    return [{k: v for k, v in row.items() if k != 'class'} for row in CASES.values()]

def synchronize(row):
    if row['device'] == 'npu':
        torch.npu.synchronize()

@contextlib.contextmanager
def prepared_case(ident):
    row = CASES[ident]
    cls = row['class']
    if cls not in _READY_CLASSES:
        cls.setUpClass()
        _READY_CLASSES.add(cls)
    instance = cls(row['method'])
    device_context = torch.device('npu:0') if row['adapted_default_device'] else contextlib.nullcontext()
    with device_context:
        instance.setUp()
        try:
            synchronize(row)
            yield row, getattr(instance, row['method'])
        finally:
            try:
                instance.tearDown()
            finally:
                instance.doCleanups()
