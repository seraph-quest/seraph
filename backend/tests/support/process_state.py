"""Process-global state registry: source scan, snapshot and restore (#1223).

Every module-level mutable object in ``src`` is either reset between tests or
recorded as safe to share, in one registry
(``tests/support/process_state_registry.py``). The autouse fixture in the root
conftest snapshots every registered object before each test and restores it
afterwards, so no test can inherit state another test left behind, in any file
order.

``scan_source_state`` finds the objects with ``ast``; the guard test
``tests/test_process_state_registry.py`` fails when the scan and the registry
disagree, so a new module-level mutable cannot land unregistered.

What the scan treats as process state (module top level, including inside
module-level ``if``/``try``):

``container``
    A ``dict``/``list``/``set``/``deque``/... (literal, comprehension or
    constructor call) that src mutates: through a mutating method, an item
    assignment or deletion, from its own module or from a module that imports
    it. A container nothing in src mutates is a constant and is not state.
``rebound``
    A name some function in its module rebinds through ``global``.
``primitive``
    A lock, semaphore, event, condition, barrier, queue or executor (any
    constructor whose name ends in one of those words).
``singleton``
    An instance of a class defined in src, bound at module level.
``cache``
    A module-level function decorated with ``functools.cache`` or
    ``functools.lru_cache``.

Class attributes, closures and objects reachable only through another object
are outside the scan; a singleton's own attributes are covered by its entry.
Known gaps (none occur in src today): tuple-unpacked or ``with``/``for``
bindings at module level, mutation through ``heapq``/``bisect`` helpers,
stateful iterators such as ``itertools.count()``, ``module.Class()``
singletons, and modules imported as ``import src.x.y`` without an alias.
Restore resets an asyncio primitive's loop binding for registered objects
and singleton attributes, not for primitives held inside registered
containers.
"""
from __future__ import annotations

import ast
import asyncio
import collections
import importlib
import sys
import threading
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

CONTAINER_CALLS = frozenset({
    "dict", "list", "set", "bytearray", "defaultdict", "OrderedDict", "deque", "Counter",
    "WeakSet", "WeakValueDictionary", "WeakKeyDictionary",
})
MUTATING_METHODS = frozenset({
    "append", "appendleft", "add", "clear", "difference_update", "discard", "extend", "extendleft",
    "insert", "intersection_update", "move_to_end", "pop", "popitem", "popleft", "remove", "reverse",
    "rotate", "setdefault", "sort", "symmetric_difference_update", "update",
    "__setitem__", "__delitem__",
})
PRIMITIVE_SUFFIXES = ("Lock", "RLock", "Semaphore", "BoundedSemaphore", "Event", "Condition", "Queue", "Barrier",
                     "Executor")
KINDS = ("container", "rebound", "primitive", "singleton", "cache")


@dataclass(frozen=True)
class Finding:
    key: str  # "src.module:name"
    kind: str
    line: int


def _call_name(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _module_name(path: Path, src_root: Path) -> str:
    parts = list(path.relative_to(src_root.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_from(module: str, node: ast.ImportFrom, is_package: bool) -> str | None:
    if node.level == 0:
        return node.module
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    return ".".join(base + ([node.module] if node.module else []))


def _top_level_bindings(body: list[ast.stmt]) -> Iterator[tuple[str, ast.AST, int]]:
    for statement in body:
        if isinstance(statement, ast.If):
            yield from _top_level_bindings(statement.body)
            yield from _top_level_bindings(statement.orelse)
        elif isinstance(statement, ast.Try):
            yield from _top_level_bindings(statement.body)
            for handler in statement.handlers:
                yield from _top_level_bindings(handler.body)
            yield from _top_level_bindings(statement.orelse)
            yield from _top_level_bindings(statement.finalbody)
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    yield target.id, statement.value, statement.lineno
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value is not None:
            yield statement.target.id, statement.value, statement.lineno
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in statement.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
                if name in {"cache", "lru_cache"}:
                    yield statement.name, decorator, statement.lineno


def _is_container(value: ast.AST) -> bool:
    if isinstance(value, (ast.Dict, ast.List, ast.Set, ast.DictComp, ast.ListComp, ast.SetComp)):
        return True
    return _call_name(value) in CONTAINER_CALLS


def _mutated_names(node: ast.AST) -> Iterator[ast.expr]:
    """Expressions that ``node`` mutates in place."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in MUTATING_METHODS:
        yield node.func.value
    elif isinstance(node, (ast.Assign, ast.Delete)):
        for target in node.targets:
            if isinstance(target, ast.Subscript):
                yield target.value
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        if isinstance(node.target, ast.Subscript):
            yield node.target.value
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            # ``NAME |= {...}`` on a set or dict mutates in place.
            yield node.target


def scan_source_state(src_root: Path = SRC_ROOT) -> dict[str, Finding]:
    """Every module-level mutable object in ``src``, keyed ``module:name``."""

    parsed: dict[str, tuple[ast.Module, bool]] = {}
    for path in sorted(src_root.rglob("*.py")):
        relative = path.relative_to(src_root.parent).with_suffix("").parts
        if not all(part.isidentifier() for part in relative):
            # Not importable (e.g. a tool-package script under src/defaults/
            # that runs in its own child process): no state in this process.
            continue
        parsed[_module_name(path, src_root)] = (ast.parse(path.read_text(encoding="utf-8"), str(path)),
                                                path.name == "__init__.py")

    mutated: set[tuple[str, str]] = set()  # (module, name) mutated somewhere in src
    rebound: set[tuple[str, str]] = set()
    for module, (tree, is_package) in parsed.items():
        # Names this module imports from other src modules, and module aliases.
        imported: dict[str, tuple[str, str]] = {}
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                source = _resolve_from(module, node, is_package)
                if not source or not source.startswith("src"):
                    continue
                for alias in node.names:
                    if f"{source}.{alias.name}" in parsed:
                        aliases[alias.asname or alias.name] = f"{source}.{alias.name}"
                    else:
                        imported[alias.asname or alias.name] = (source, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("src") and alias.asname:
                        aliases[alias.asname] = alias.name
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                rebound.update((module, name) for name in node.names)
            for target in _mutated_names(node):
                # ``NAME[key].append(...)`` mutates a value NAME holds: NAME is state.
                while isinstance(target, ast.Subscript):
                    target = target.value
                if isinstance(target, ast.Name):
                    mutated.add(imported.get(target.id, (module, target.id)))
                elif (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                      and target.value.id in aliases):
                    mutated.add((aliases[target.value.id], target.attr))

    findings: dict[str, Finding] = {}
    for module, (tree, is_package) in parsed.items():
        classes = {node.name for node in tree.body if isinstance(node, ast.ClassDef)}
        src_names: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                source = _resolve_from(module, node, is_package)
                if source and source.startswith("src"):
                    src_names.update(alias.asname or alias.name for alias in node.names)
        for name, value, line in _top_level_bindings(tree.body):
            key = f"{module}:{name}"
            if key in findings or (name.startswith("__") and name.endswith("__")):
                continue
            kind = None
            call = _call_name(value)
            if (module, name) in rebound:
                kind = "rebound"
            elif isinstance(value, (ast.Call, ast.Name, ast.Attribute)) and _is_cache_decorator(value) and _is_function(tree, name):
                kind = "cache"
            elif _is_container(value):
                kind = "container" if (module, name) in mutated else None
            elif call is not None and call.endswith(PRIMITIVE_SUFFIXES):
                kind = "primitive"
            elif (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                  and value.func.id[:1].isupper() and (value.func.id in classes or value.func.id in src_names)):
                kind = "singleton"
            if kind is not None:
                findings[key] = Finding(key, kind, line)
    return findings


def _is_cache_decorator(value: ast.AST) -> bool:
    target = value.func if isinstance(value, ast.Call) else value
    name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
    return name in {"cache", "lru_cache"}


def _is_function(tree: ast.Module, name: str) -> bool:
    return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
               for node in ast.walk(tree))


# ── Registry entries ────────────────────────────────────


@dataclass(frozen=True)
class Restore:
    """Snapshot before the test and restore after it.

    A rebound name gets its binding back. A container also gets its contents
    back, in place, so modules holding the same object see the restore. A
    singleton gets its instance attributes back, and each container-valued
    attribute its contents. A lock or semaphore must be released by the end
    of the test (a held one fails the test as a leak); an event gets its flag
    back; an asyncio primitive is unbound from the closed test loop.
    """

    note: str = ""


@dataclass(frozen=True)
class Reset:
    """Call ``function`` (``module:attr``) after the test instead of restoring."""

    function: str
    note: str = ""


@dataclass(frozen=True)
class Shared:
    """Deliberately shared across tests; ``reason`` says why that is safe."""

    reason: str

    def __post_init__(self) -> None:
        if len(self.reason.strip()) < 20:
            raise ValueError("a Shared entry needs a real reason")


Strategy = Restore | Reset | Shared


@dataclass(frozen=True)
class Entry:
    kind: str
    strategy: Strategy


# ── Snapshot and restore ────────────────────────────────

_RESTORABLE_CONTAINERS = (dict, list, set, bytearray, collections.deque)
_WEAK_CONTAINERS = (weakref.WeakSet, weakref.WeakValueDictionary, weakref.WeakKeyDictionary)


def resolve_reset(target: str) -> Callable[[], Any]:
    """``module:attr[.attr...]`` to the reset callable it names."""

    module, path = target.split(":", 1)
    value: Any = importlib.import_module(module)
    for part in path.split("."):
        value = getattr(value, part)
    if not callable(value):
        raise TypeError(f"{target} is not callable")
    return value


def _resolve(key: str) -> tuple[Any, str]:
    module_name, attr = key.split(":", 1)
    module = sys.modules.get(module_name)
    if module is None:
        module = importlib.import_module(module_name)
    return module, attr


def _copy_contents(value: Any) -> Any:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (list, collections.deque, bytearray)):
        return list(value)
    if isinstance(value, set):
        return set(value)
    if isinstance(value, _WEAK_CONTAINERS):
        return None  # entries vanish with their owners; the binding is restored
    return None


def _restore_contents(target: Any, contents: Any) -> None:
    if contents is None:
        return
    if isinstance(target, dict):
        if target != contents or any(target.get(k) is not v for k, v in contents.items()):
            target.clear()
            target.update(contents)
    elif isinstance(target, (list, collections.deque)):
        if list(target) != contents or any(a is not b for a, b in zip(target, contents)):
            target.clear()
            target.extend(contents)
    elif isinstance(target, bytearray):
        target[:] = bytes(contents)
    elif isinstance(target, set):
        if target != contents:
            target.clear()
            target.update(contents)


def _lock_held(value: Any) -> bool | None:
    """Whether a lock-like primitive is held; None if it is not a lock."""

    if isinstance(value, (asyncio.Lock,)):
        return value.locked()
    if isinstance(value, asyncio.Semaphore):
        return None  # compared by value instead
    if isinstance(value, type(threading.Lock())):
        return value.locked()
    if isinstance(value, type(threading.RLock())):
        return "unlocked" not in repr(value)
    locked = getattr(value, "locked", None)
    if callable(locked):
        try:
            return bool(locked())
        except Exception:  # pragma: no cover - diagnostics only
            return None
    return None


def _unbind_loop(value: Any) -> None:
    # asyncio primitives bind to the first loop that contends on them; each
    # test has its own loop, so a primitive bound to a closed loop would raise
    # "bound to a different event loop" in the next test that contends on it.
    if isinstance(value, (asyncio.Lock, asyncio.Event, asyncio.Condition, asyncio.Semaphore,
                          asyncio.Queue, asyncio.Barrier)):
        loop = getattr(value, "_loop", None)
        waiters = getattr(value, "_waiters", None) or getattr(value, "_getters", None)
        if loop is not None and loop.is_closed() and not waiters:
            value._loop = None  # noqa: SLF001 - see comment above
        if isinstance(value, asyncio.Condition):
            _unbind_loop(value._lock)  # noqa: SLF001


@dataclass
class _Saved:
    key: str
    entry: Entry
    owner: Any
    attr: str
    binding: Any
    contents: Any = None
    attributes: dict[str, tuple[Any, Any]] | None = None
    flag: bool | None = None
    semaphore_value: int | None = None


@dataclass
class ProcessStateSnapshot:
    """The registered process state at one point in time."""

    saved: list[_Saved] = field(default_factory=list)
    resets: list[Callable[[], Any]] = field(default_factory=list)

    @classmethod
    def take(cls, registry: dict[str, Entry]) -> "ProcessStateSnapshot":
        snapshot = cls()
        for key, entry in registry.items():
            if isinstance(entry.strategy, Shared):
                continue
            owner, attr = _resolve(key)
            if isinstance(entry.strategy, Reset):
                snapshot.resets.append(resolve_reset(entry.strategy.function))
                continue
            binding = getattr(owner, attr)
            saved = _Saved(key, entry, owner, attr, binding)
            if entry.kind == "singleton" and hasattr(binding, "__dict__") and not isinstance(binding, type):
                saved.attributes = {name: (value, _copy_contents(value)) for name, value in vars(binding).items()}
            elif entry.kind == "primitive":
                if isinstance(binding, (threading.Event, asyncio.Event)):
                    saved.flag = binding.is_set()
                if isinstance(binding, (threading.Semaphore, asyncio.Semaphore)):
                    saved.semaphore_value = binding._value  # noqa: SLF001
            else:
                saved.contents = _copy_contents(binding)
            snapshot.saved.append(saved)
        return snapshot

    def restore(self) -> list[str]:
        """Restore every saved object; return the leaks found (held locks)."""

        problems: list[str] = []
        for saved in self.saved:
            current = getattr(saved.owner, saved.attr, None)
            if saved.entry.kind == "primitive":
                held = _lock_held(current)
                if held:
                    problems.append(f"{saved.key} is still held after the test")
                if saved.semaphore_value is not None and current is saved.binding:
                    if current._value != saved.semaphore_value:  # noqa: SLF001
                        problems.append(f"{saved.key} has {current._value} free permit(s), "  # noqa: SLF001
                                        f"expected {saved.semaphore_value}: a permit was not released")
                if saved.flag is not None and current is saved.binding and current.is_set() != saved.flag:
                    current.set() if saved.flag else current.clear()
                _unbind_loop(saved.binding)
            if current is not saved.binding:
                setattr(saved.owner, saved.attr, saved.binding)
            if saved.attributes is not None:
                attributes = vars(saved.binding)
                for name in [name for name in attributes if name not in saved.attributes]:
                    del attributes[name]
                for name, (value, contents) in saved.attributes.items():
                    if attributes.get(name, _MISSING) is not value:
                        attributes[name] = value
                    _restore_contents(value, contents)
                    if isinstance(value, (asyncio.Lock, asyncio.Event, asyncio.Condition, asyncio.Semaphore,
                                          asyncio.Queue)):
                        _unbind_loop(value)
            else:
                _restore_contents(saved.binding, saved.contents)
        for reset in self.resets:
            reset()
        return problems


_MISSING = object()


__all__ = [
    "Entry",
    "Finding",
    "KINDS",
    "ProcessStateSnapshot",
    "Reset",
    "Restore",
    "Shared",
    "scan_source_state",
]
