"""Guards for process-global state in src (#1223).

The registry in ``tests/support/process_state_registry.py`` must name exactly
the module-level mutable objects the ``ast`` scan finds, so a new one cannot
land without a reset or a recorded reason for sharing it. Background work must
not be spawned and dropped: a discarded ``create_task``/``ensure_future``
handle goes through ``src.utils.background.track_task`` instead.
"""
from __future__ import annotations

import ast
import importlib
import textwrap
from pathlib import Path

import pytest

from tests.support.process_state import (KINDS, Entry, ProcessStateSnapshot, Reset, Restore, Shared,
                                         scan_source_state)
from tests.support.process_state_registry import PROCESS_STATE

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"


def test_registry_names_exactly_the_scanned_module_state():
    scanned = scan_source_state()
    unregistered = sorted(set(scanned) - set(PROCESS_STATE))
    stale = sorted(set(PROCESS_STATE) - set(scanned))
    assert not unregistered, (
        "module-level mutable state in src is not in tests/support/process_state_registry.py; "
        f"add a RESTORE, Reset or Shared entry for: {unregistered}")
    assert not stale, f"registry entries no longer found in src (delete them): {stale}"
    wrong_kind = {key: (PROCESS_STATE[key].kind, finding.kind) for key, finding in scanned.items()
                  if PROCESS_STATE[key].kind != finding.kind}
    assert not wrong_kind, f"registry kind differs from the scan (registered, scanned): {wrong_kind}"


def test_every_registry_entry_resolves():
    for key, entry in PROCESS_STATE.items():
        assert entry.kind in KINDS, key
        assert isinstance(entry.strategy, (Restore, Reset, Shared)), key
        module_name, attr = key.split(":", 1)
        module = importlib.import_module(module_name)
        assert hasattr(module, attr), key
        if isinstance(entry.strategy, Reset):
            reset_module, function = entry.strategy.function.split(":", 1)
            assert callable(getattr(importlib.import_module(reset_module), function)), key
        if entry.kind == "cache":
            # A cache is either cleared per test or shared with a recorded reason.
            assert not isinstance(entry.strategy, Restore), key


def test_shared_entries_need_a_reason():
    with pytest.raises(ValueError):
        Shared("ok")


def _scan(source: str, tmp_path: Path) -> dict:
    root = tmp_path / "src"
    (root / "pkg").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "subject.py").write_text(textwrap.dedent(source))
    (root / "pkg" / "user.py").write_text(textwrap.dedent("""
        from src.pkg.subject import IMPORTED_AND_MUTATED
        from src.pkg import subject as alias

        def touch():
            IMPORTED_AND_MUTATED.append(1)
            alias.ALIAS_MUTATED["k"] = 1
    """))
    return {key.split(":", 1)[1]: finding.kind for key, finding in scan_source_state(root).items()}


def test_scan_classifies_module_state(tmp_path):
    found = _scan("""
        import asyncio
        import threading
        from functools import lru_cache
        from collections import defaultdict

        CONSTANT_TABLE = {"a": 1}
        CONSTANT_LIST = [1, 2]
        _cache = {}
        _seen = set()
        _by_key = defaultdict(list)
        IMPORTED_AND_MUTATED = []
        ALIAS_MUTATED = {}
        _flag = False
        _LOCK = threading.Lock()
        _ALOCK = asyncio.Lock()

        class Service:
            pass

        service = Service()
        __all__ = ["Service"]

        @lru_cache(maxsize=1)
        def load():
            return 1

        def remember(key, value):
            global _flag
            _flag = True
            _cache[key] = value
            _seen.add(key)
            _by_key[key].append(value)
    """, tmp_path)
    assert found == {
        "_cache": "container",
        "_seen": "container",
        "_by_key": "container",
        "IMPORTED_AND_MUTATED": "container",
        "ALIAS_MUTATED": "container",
        "_flag": "rebound",
        "_LOCK": "primitive",
        "_ALOCK": "primitive",
        "service": "singleton",
        "load": "cache",
    }


def test_snapshot_restores_in_place_so_other_holders_see_it(monkeypatch):
    import types

    module = types.ModuleType("isolation_subject")
    module.TABLE = {"seed": 1}
    module.ITEMS = [1]
    monkeypatch.setitem(__import__("sys").modules, "isolation_subject", module)
    held_elsewhere = module.TABLE
    registry = {
        "isolation_subject:TABLE": Entry("container", Restore()),
        "isolation_subject:ITEMS": Entry("container", Restore()),
    }
    snapshot = ProcessStateSnapshot.take(registry)
    module.TABLE["leak"] = 2
    module.ITEMS = ["rebound"]
    assert snapshot.restore() == []
    assert held_elsewhere == {"seed": 1} and module.TABLE is held_elsewhere
    assert module.ITEMS == [1]


# ── Background spawns ───────────────────────────────────

SPAWNS = {"create_task", "ensure_future"}


def _discarded_spawns(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        value = None
        if isinstance(node, ast.Expr):
            value = node.value
        elif isinstance(node, ast.Lambda):
            value = node.body
            if isinstance(value, ast.IfExp):
                value = value.body if isinstance(value.body, ast.Call) else value.orelse
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                and value.func.attr in SPAWNS):
            lines.append(node.lineno)
    return lines


def test_src_does_not_spawn_and_drop_background_tasks():
    offenders = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        for line in _discarded_spawns(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(SRC_ROOT.parent)}:{line}")
    assert not offenders, (
        "a background task whose handle is dropped is invisible to shutdown and to the tests' "
        f"drain; spawn it with src.utils.background.track_task: {offenders}")


def test_spawn_guard_detects_dropped_handles():
    tree = ast.parse(textwrap.dedent("""
        import asyncio
        def f(loop, page):
            asyncio.create_task(work())
            loop.create_task(work())
            page.on("x", lambda p: asyncio.create_task(p.close()) if p else None)
            kept = asyncio.create_task(work())
            return kept
    """))
    assert _discarded_spawns(tree) == [4, 5, 6]
