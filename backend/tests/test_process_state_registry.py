"""Guards for process-global state in src (#1223).

The registry in ``tests/support/process_state_registry.py`` must name exactly
the module-level mutable objects the ``ast`` scan finds, so a new one cannot
land without a reset or a recorded reason for sharing it. Background work must
not be spawned and dropped: a discarded ``create_task``/``ensure_future``
handle goes through ``src.utils.background.track_task`` instead.
"""
from __future__ import annotations

import ast
import asyncio
import importlib
import textwrap
from pathlib import Path

import pytest

from tests.support.process_state import (KINDS, Entry, ProcessStateSnapshot, Reset, Restore, Shared,
                                         resolve_reset, scan_source_state)
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
            assert callable(resolve_reset(entry.strategy.function)), key
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
        HANDED_TO_AN_INSTANCE = {}
        _flag = False
        _LOCK = threading.Lock()
        _ALOCK = asyncio.Lock()

        class Service:
            pass

        service = Service()
        service.tasks = HANDED_TO_AN_INSTANCE
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
        "HANDED_TO_AN_INSTANCE": "container",
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


async def test_a_leftover_dispatcher_worker_entry_is_caught_and_restored():
    """#1259: a worker entry a test leaves in ``_ACTIVE_WORKER_TASKS`` is
    reported (its still-running task fails the task leak check) and removed
    in place before the next test, which therefore sees no stale worker."""
    from src.work_board import dispatcher
    from tests.support import leaks

    key = "src.work_board.dispatcher:_ACTIVE_WORKER_TASKS"
    assert PROCESS_STATE[key] == PROCESS_STATE["src.work_board.channel_capture:_LIVE_SOURCE_FRAMES"]
    registry = dispatcher._ACTIVE_WORKER_TASKS
    assert dispatcher._dispatcher._active_worker_tasks is registry

    snapshot = ProcessStateSnapshot.take({key: PROCESS_STATE[key]})
    worker = asyncio.create_task(asyncio.Event().wait(), name="leftover-worker")
    registry[("leftover-task", "leftover-attempt")] = worker
    try:
        leaked = leaks.pending_tasks(asyncio.get_running_loop())
        assert worker in leaked
        assert "leftover-worker" in leaks.describe_task(worker)
        assert snapshot.restore() == []
        # Restored in place: the dispatcher's own reference sees the cleanup too.
        assert dispatcher._ACTIVE_WORKER_TASKS is registry
        assert registry == {} and dispatcher._dispatcher._active_worker_tasks == {}
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


# ── Background spawns ───────────────────────────────────

SPAWNS = {"create_task", "ensure_future"}


def _is_spawn(value: ast.AST) -> bool:
    if not isinstance(value, ast.Call):
        return False
    func = value.func
    return (isinstance(func, ast.Attribute) and func.attr in SPAWNS) or (isinstance(func, ast.Name) and func.id in SPAWNS)


def _discarded_spawns(tree: ast.AST) -> list[int]:
    """Spawns whose task handle is dropped: an expression statement, or a
    lambda (a callback) whose result nobody keeps, in either ``if`` branch."""
    lines = []
    for node in ast.walk(tree):
        values: list[ast.AST] = []
        if isinstance(node, ast.Expr):
            values = [node.value]
        elif isinstance(node, ast.Lambda):
            values = [node.body.body, node.body.orelse] if isinstance(node.body, ast.IfExp) else [node.body]
        if any(_is_spawn(value) for value in values):
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
            create_task(work())
            page.on("y", lambda p: None if p else ensure_future(p.close()))
            return kept
    """))
    assert sorted(_discarded_spawns(tree)) == [4, 5, 6, 8, 9]


# ── Bounded stream reads ────────────────────────────────

AITER_METHODS = {"aiter_bytes", "aiter_raw", "aiter_lines", "aiter_text"}


def _unclosed_body_iterators(tree: ast.AST) -> list[int]:
    """HTTP body iterators src opens without closing them explicitly.

    A bounded read raises mid-iteration; an iterator nobody closed is left to
    the collector, whose finalizer task outlives the request (and the test).
    Each ``aiter_*()`` (and Starlette's ``request.stream()``) must be the
    argument of ``contextlib.aclosing`` or be bound to a name whose
    ``aclose()`` the same function calls. Delegation between the inference
    broker's own streams is tracked separately (#1245).
    """
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def is_body_iterator(node: ast.AST) -> bool:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            return False
        if node.func.attr in AITER_METHODS:
            return True
        owner = node.func.value
        return node.func.attr == "stream" and isinstance(owner, ast.Name) and owner.id == "request"

    def closed_by_name(node: ast.AST) -> bool:
        parent = parents.get(node)
        if not (isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name)):
            return False
        name = parent.targets[0].id
        scope = parent
        while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            scope = parents.get(scope)
        return any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "aclose"
                   and isinstance(call.func.value, ast.Name) and call.func.value.id == name
                   for call in ast.walk(scope))

    lines = []
    for node in ast.walk(tree):
        if not is_body_iterator(node):
            continue
        parent = parents.get(node)
        wrapped = (isinstance(parent, ast.Call) and node in parent.args
                   and ((isinstance(parent.func, ast.Attribute) and parent.func.attr == "aclosing")
                        or (isinstance(parent.func, ast.Name) and parent.func.id == "aclosing")))
        # ``yield response.aiter_bytes()`` hands the iterator to the caller,
        # who owns closing it.
        if wrapped or closed_by_name(node) or isinstance(parent, ast.Yield):
            continue
        lines.append(node.lineno)
    return lines


def test_src_closes_http_body_iterators_explicitly():
    offenders = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        for line in _unclosed_body_iterators(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(SRC_ROOT.parent)}:{line}")
    assert not offenders, f"close the body iterator with contextlib.aclosing (or aclose()): {offenders}"


def test_body_iterator_guard_detects_unclosed_iterators():
    tree = ast.parse(textwrap.dedent("""
        import contextlib
        async def f(response, request):
            async for chunk in response.aiter_raw():
                pass
            async with contextlib.aclosing(response.aiter_bytes()) as chunks:
                pass
            held = response.aiter_lines()
            kept = response.aiter_text()
            await kept.aclose()
            async for chunk in request.stream():
                pass
    """))
    assert sorted(_unclosed_body_iterators(tree)) == [4, 8, 11]
