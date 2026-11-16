"""Autouse process-state isolation plugin (#1223).

Registered by the root ``tests/conftest.py``. Before every test it snapshots
every registered object whose module is already imported; after the test,
once every other function-scoped fixture has been finalized, it restores
them. A registered lock left held, or a semaphore permit left taken, fails
the test as a leak.

A registered module first imported *during* a test is snapshotted by an
import hook the moment its module body finishes, and restored to that
import-time state after the test. So no test inherits another test's changes
whichever test happens to import a module first, and the registry's modules
need not all be imported up front.

Fixtures from a registered plugin are set up before the conftest's own
autouse fixtures, so the snapshot sees the state module-, class- and
session-scoped fixtures established and the restore runs last.
"""
from __future__ import annotations

import importlib.abc
import sys
from typing import Any, Callable

import pytest

from tests.support.process_state import Entry, ProcessStateSnapshot


def _default_registry() -> dict[str, Entry]:
    from tests.support.process_state_registry import PROCESS_STATE
    return PROCESS_STATE


# Replaced only by the plugin's own meta-tests, which run it against a
# synthetic registry in a pytester subprocess.
registry_provider: Callable[[], dict[str, Entry]] = _default_registry
_by_module: dict[str, dict[str, Entry]] = {}
_import_snapshots: dict[str, ProcessStateSnapshot] = {}


def _registry_by_module() -> dict[str, dict[str, Entry]]:
    if not _by_module:
        for key, entry in registry_provider().items():
            _by_module.setdefault(key.split(":", 1)[0], {})[key] = entry
    return _by_module


class _SnapshotLoader:
    """Delegates to the real loader; snapshots the module's state after exec."""

    def __init__(self, loader: Any, name: str) -> None:
        self._loader = loader
        self._name = name

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module) -> None:
        self._loader.exec_module(module)
        entries = _registry_by_module().get(self._name)
        if entries:
            _import_snapshots[self._name] = ProcessStateSnapshot.take(entries)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._loader, name)


class _SnapshotOnImport(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        # Loaded before this finder is installed, so a lookup never imports.
        if fullname not in _by_module:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                if spec.loader is not None and hasattr(spec.loader, "exec_module"):
                    spec.loader = _SnapshotLoader(spec.loader, fullname)
                return spec
        return None


_FINDER = _SnapshotOnImport()


def pytest_configure(config) -> None:
    _registry_by_module()
    if _FINDER not in sys.meta_path:
        sys.meta_path.insert(0, _FINDER)


def pytest_unconfigure(config) -> None:
    if _FINDER in sys.meta_path:
        sys.meta_path.remove(_FINDER)


@pytest.fixture(autouse=True)
def isolated_process_state():
    by_module = _registry_by_module()
    imported = {name for name in by_module if name in sys.modules}
    snapshot = ProcessStateSnapshot.take({key: entry for name in imported for key, entry in by_module[name].items()})
    yield
    problems = snapshot.restore()
    for name in by_module:
        if name not in imported and name in sys.modules and name in _import_snapshots:
            # First imported during this test: back to its import-time state.
            problems += _import_snapshots.pop(name).restore()
    if problems:
        pytest.fail("process state leaked by the test (#1223):\n- " + "\n- ".join(problems), pytrace=False)
