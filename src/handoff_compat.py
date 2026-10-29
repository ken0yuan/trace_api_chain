"""Read an existing handoff with its exact frozen, hash-pinned implementation."""
import hashlib
import importlib
import json
from pathlib import Path
import sys
import types


class FrozenHandoffLoader:
    def __init__(self,runtime,expected_policy,expected_native_sha256):
        self.runtime=Path(runtime).resolve()
        self.package=self.runtime/'trace_api_chain'
        self.expected_policy=dict(expected_policy)
        self.expected_native_sha256=expected_native_sha256
        identity=str(self.package)+json.dumps(self.expected_policy,sort_keys=True)
        self.namespace='_callchain_frozen_'+hashlib.sha256(identity.encode()).hexdigest()

    def __call__(self,target):
        target=Path(target)
        receipt=json.loads(target.with_name(target.name+'.ready.json').read_text())
        if receipt.get('policy')!=self.expected_policy:raise ValueError('frozen_handoff_policy_mismatch')
        expected={**self.expected_policy,'native.py':self.expected_native_sha256}
        for name,sha in expected.items():
            if Path(name).name!=name:raise ValueError('frozen_source_name_invalid')
            source=self.package/name
            if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest()!=sha:
                raise ValueError('frozen_handoff_source_changed:'+name)
        if self.namespace not in sys.modules:
            # A private package namespace avoids replacing the active collector
            # or executing a package initializer. Only pinned staged/native code
            # loads; its original strict file, manifest and graph checks still run.
            module=types.ModuleType(self.namespace)
            module.__path__=[str(self.package)]
            sys.modules[self.namespace]=module
        staged=importlib.import_module(self.namespace+'.staged')
        return staged.load_file(target)

    def importer(self,plan):
        # An indexed source resolver can have a different implementation hash
        # even when its output is identical. Commit an old contribution through
        # its exact pinned importer, rather than weakening either policy check.
        policy=plan.get('policy')
        if not isinstance(policy,list) or len(policy)!=3:
            raise ValueError('frozen_importer_policy_missing')
        if policy[:2]!=[self.expected_policy.get('database.py'),self.expected_policy.get('source_entities.py')]:
            raise ValueError('frozen_importer_policy_mismatch')
        for name,sha in zip(('database.py','source_entities.py','changed_objects.py'),policy):
            source=self.package/name
            if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest()!=sha:
                raise ValueError('frozen_importer_source_changed:'+name)
        if self.namespace not in sys.modules:raise ValueError('frozen_importer_not_verified')
        return importlib.import_module(self.namespace+'.database').commit_refresh
