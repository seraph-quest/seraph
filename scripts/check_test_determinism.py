#!/usr/bin/env python3
"""Ratchet backend test-determinism hazards against a checked-in per-file baseline.

The check parses every Python file under ``backend/tests`` with ``ast`` (so
code inside child-script strings is not counted) and counts these hazards:

``sleep``
    ``time.sleep(...)`` and ``asyncio.sleep(x)`` where ``x`` is not a literal 0.
``clock-compare``
    A comparison with an operand that reads ``time.time()``, ``time.monotonic()``,
    ``time.perf_counter()`` (or ``*_ns``) or an event loop's ``time()``,
    directly or through a name assigned from such a read in the same scope.
    This covers elapsed-time asserts and ``while now < deadline`` polls, and
    ``self.assertLess``-style methods or ``math.isclose`` given a clock value.
    ``is`` / ``is not`` checks are not comparisons of time.
``timeout-skip``
    ``pytest.skip(...)`` inside an ``except`` handler for a timeout.
``settings-assign``
    Direct ``settings.x = ...`` (or ``setattr(settings, ...)``) instead of
    ``monkeypatch``.
``environ-assign``
    Direct ``os.environ[...] = ...``, ``del os.environ[...]``,
    ``os.environ.update/setdefault/pop/popitem/clear`` or ``os.environ |= ...``
    instead of ``monkeypatch``.
``get-session-patch``
    ``patch(...)`` / ``patch.object(...)`` / ``monkeypatch.setattr(...)`` of a
    ``get_session`` target (a string or f-string), outside ``backend/tests/support/``.
``popen``
    Raw ``subprocess.Popen(...)``, outside ``backend/tests/support/``.
``realtime``
    Each ``@pytest.mark.realtime(reason=...)`` marker. The marker is ratcheted
    too: adding one takes a reviewed hand edit of the baseline (``--update``
    only lowers counts).

Quarantine (#1220): ``@pytest.mark.quarantine(issue=NNNN)`` must name an issue
number and decorate a single test function (other placements are errors), and
at most ``QUARANTINE_CAP`` quarantine markers may exist. A
quarantined test still runs, in the non-blocking quarantine lane; it leaves
quarantine only through a fix that removes the marker after a 50-of-50 stress
pass. There is no calendar expiry. ``--quarantined-files`` and
``--realtime-files`` list the files those lanes run.

Names resolve through imports and aliases, and through dynamic imports
(#1257): ``__import__("time").sleep`` and ``importlib.import_module("asyncio")``
count as the named module for every rule, as does a name assigned from one;
the import table never rewrites such a chain. An attribute read on a dynamic
import with a non-literal or relative module name, directly or through a name
bound only to one in the same scope, is an error, since it could hide any
hazard.

The per-file, per-category counts must equal the baseline exactly. A count
above the baseline fails, and so does a count below it until the baseline is
lowered with ``--update``, so the numbers only go down. ``--update`` never
raises a count, and refuses to write while any file fails to scan. A renamed
file keeps its entry with ``--rename OLD NEW``; a split file needs its counts
moved by hand. The only exemption is ``@pytest.mark.realtime(reason="...")``
(keyword ``reason`` is required) as a decorator on the enclosing test function
or class, or in the module's ``pytestmark``. It exempts only ``sleep`` and
``clock-compare``: a real-time test still may not mutate ambient state, patch
``get_session`` or spawn unowned processes.

Clock seam (#1225): the production modules in ``CLOCK_SEAM_MODULES`` read time
and wait on timers only through ``src.utils.clock.get_clock()``. In them, any
direct ``time.time/monotonic/perf_counter`` (or ``*_ns``), ``datetime.now/
utcnow/today``, ``date.today``, event-loop ``time()``, ``asyncio.wait_for``,
``asyncio.timeout``/``timeout_at``, ``time.sleep``, nonzero ``asyncio.sleep``,
``asyncio.wait(..., timeout=...)`` (or ``**kwargs``), ``asyncio.sleep``/``wait``
passed as a value, or an event loop's ``time``/``call_later``/``call_at`` is
an error. There is no baseline for these:
the count is zero, and a module leaves the list only through a reviewed edit.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "backend/tests"
SRC_DIR = ROOT / "backend/src"
BASELINE = ROOT / "scripts/test_determinism_baseline.json"
SUPPORT_PACKAGE = "support"

CATEGORIES = (
    "sleep",
    "clock-compare",
    "timeout-skip",
    "settings-assign",
    "environ-assign",
    "get-session-patch",
    "popen",
    "realtime",
)
SUPPORT_EXEMPT = frozenset({"get-session-patch", "popen"})
REALTIME_EXEMPT = frozenset({"sleep", "clock-compare"})
_REAL_TIME = ("Synchronise on a cause (event, gate, notification) instead, or mark a test that must "
              "use real time with @pytest.mark.realtime(reason=...).")
REMEDY = {
    "sleep": _REAL_TIME,
    "clock-compare": "Assert the outcome or causal order, not elapsed time. " + _REAL_TIME,
    "timeout-skip": "A slow host must not turn a proof into a skip; fail with a diagnostic instead.",
    "settings-assign": "Use monkeypatch.setattr so the setting is restored.",
    "environ-assign": "Use monkeypatch.setenv / delenv so the environment is restored.",
    "get-session-patch": "Route the test through the session-factory seam (tests/support/) instead.",
    "popen": "Spawn through an owning helper in tests/support/ that reaps the child.",
    "realtime": ("A new realtime marker needs a reviewed baseline edit: raise this file's realtime count "
                 "in scripts/test_determinism_baseline.json in the same change, with the reason."),
}

CLOCK_FUNCTIONS = frozenset(
    {"time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns"}
)
CLOCK_WRAPPERS = frozenset({"min", "max", "round", "abs", "int", "float"})
LOOP_ACCESSORS = frozenset({"get_running_loop", "get_event_loop"})
PATCH_CALLS = frozenset(
    {
        "patch",
        "mock.patch",
        "unittest.mock.patch",
        "patch.object",
        "mock.patch.object",
        "unittest.mock.patch.object",
        "monkeypatch.setattr",
        "setattr",
    }
)
TIMEOUT_EXCEPTIONS = ("TimeoutExpired", "TimeoutError")
# unittest-style comparisons; a clock-valued argument makes them elapsed-time asserts.
COMPARE_METHODS = frozenset(
    {"assertLess", "assertLessEqual", "assertGreater", "assertGreaterEqual",
     "assertAlmostEqual", "assertNotAlmostEqual"}
)
ENVIRON_MUTATORS = frozenset({"update", "setdefault", "pop", "popitem", "clear"})
QUARANTINE_CAP = 10
ISSUE_REFERENCE = re.compile(r"#?[1-9][0-9]*")


# Production modules adopted onto the clock seam (#1225), relative to backend/src.
CLOCK_SEAM_MODULES = (
    "api/extension_metadata.py",
    "api/settings.py",
    "api/ws.py",
    "approval/repository.py",
    "browser/task_runner.py",
    "conversation/identity.py",
    "integrations/connection_sync.py",
    "integrations/github_activity.py",
    "integrations/gmail_read.py",
    "model_fabric/near_tee_catalog.py",
    "observer/manager.py",
    "observer/native_notification_queue.py",
    "observer/sources/time_source.py",
    "runtime_plugins/bridge.py",
    "runtime_plugins/lifecycle.py",
    "scheduler/job_status.py",
    "security/http_transport.py",
    "work_board/dispatcher.py",
    "work_board/pipelines.py",
    "workflows/repo_repair_source.py",
)
CLOCK_SEAM_READS = frozenset(
    {f"time.{name}" for name in CLOCK_FUNCTIONS}
    | {"datetime.datetime.now", "datetime.datetime.utcnow", "datetime.datetime.today", "datetime.date.today"}
)
CLOCK_SEAM_TIMERS = frozenset({"asyncio.wait_for", "asyncio.timeout", "asyncio.timeout_at", "time.sleep",
                               "asyncio.tasks.wait_for", "asyncio.timeouts.timeout", "asyncio.timeouts.timeout_at"})
# Referenced without being called (passed as a value), these timers escape the call checks.
CLOCK_SEAM_TIMER_VALUES = frozenset({"asyncio.sleep", "asyncio.wait"})
LOOP_TIMER_METHODS = frozenset({"call_later", "call_at"})


def scan_clock_seam(src_dir: Path, root: Path, modules=CLOCK_SEAM_MODULES) -> list[str]:
    """Direct clock reads and timers in modules adopted onto ``get_clock()``; each is an error."""
    errors: list[str] = []
    for module in modules:
        path = src_dir / module
        try:
            display = path.relative_to(root).as_posix()
        except ValueError:
            display = path.as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except FileNotFoundError:
            errors.append(f"{display}: listed in CLOCK_SEAM_MODULES but missing; update the list")
            continue
        except SyntaxError as error:
            errors.append(f"{display}:{error.lineno}: cannot parse: {error.msg}")
            continue
        imports = Imports(tree)
        lines = source.split("\n")

        def resolved(node: ast.AST) -> str | None:
            return imports.name_of(node)

        found: list[tuple[int, str]] = []

        def report(node: ast.AST, what: str) -> None:
            snippet = lines[node.lineno - 1].strip()[:120] if node.lineno <= len(lines) else ""
            found.append((node.lineno, f"{display}:{node.lineno}: direct {what}; "
                                       f"use src.utils.clock.get_clock(): {snippet}"))

        calls: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = resolved(node.func)
                if name in {"datetime.now", "datetime.utcnow", "datetime.today", "date.today"}:
                    # ``from datetime import datetime`` resolves to datetime.datetime; a bare
                    # unresolved ``datetime.now`` is the same read.
                    name = "datetime." + name
                func = node.func
                if name == "asyncio.sleep":
                    calls.add(id(func))
                    delay = node.args[0] if node.args else next(
                        (keyword.value for keyword in node.keywords if keyword.arg == "delay"), None)
                    if delay is None or not is_zero(delay):
                        report(node, "asyncio.sleep timer")
                elif name == "asyncio.wait":
                    calls.add(id(func))
                    if any(keyword.arg in {"timeout", None} for keyword in node.keywords) or len(node.args) > 1:
                        report(node, "asyncio.wait timeout")
                elif isinstance(func, ast.Attribute) and func.attr == "time" and not node.args and (
                        (isinstance(func.value, ast.Call)
                         and (dotted(func.value.func) or "").rpartition(".")[2] in LOOP_ACCESSORS)
                        or (dotted(func.value) or "").endswith("loop")):
                    report(node, "event-loop clock read")
                elif isinstance(func, ast.Attribute) and func.attr in LOOP_TIMER_METHODS and (
                        (isinstance(func.value, ast.Call)
                         and (dotted(func.value.func) or "").rpartition(".")[2] in LOOP_ACCESSORS)
                        or (dotted(func.value) or "").endswith("loop")):
                    report(node, "event-loop timer " + func.attr)
            elif isinstance(node, (ast.Attribute, ast.Name)):
                if id(node) in calls:
                    continue
                name = resolved(node)
                if name in {"datetime.now", "datetime.utcnow", "datetime.today", "date.today"}:
                    name = "datetime." + name
                if name in CLOCK_SEAM_READS:
                    report(node, "clock read " + name)
                elif name in CLOCK_SEAM_TIMERS:
                    report(node, "timer " + name)
                elif name in CLOCK_SEAM_TIMER_VALUES:
                    report(node, "timer " + name + " used as a value")
        for node, unresolvable in unresolvable_dynamic_imports(tree):
            report(node, f"dynamic import with {unresolvable.why} ({unresolvable.label})")
        errors.extend(message for _, message in sorted(found))
    return errors


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    category: str
    snippet: str


class _Unresolvable:
    """A dynamic import whose module the checker cannot name."""

    def __init__(self, why: str, label: str) -> None:
        self.why = why
        self.label = label


_NON_LITERAL = _Unresolvable("a non-literal module name", "non-literal/unresolvable")
_RELATIVE = _Unresolvable("a relative module name", "relative/unresolvable")


def dynamic_import(node: ast.AST) -> str | _Unresolvable | None:
    """The module a ``__import__("m")`` / ``importlib.import_module("m")`` call yields (#1257).

    ``None`` when ``node`` is not such a call, an ``_Unresolvable`` when it is
    one whose module name is not a string literal or is relative. ``__import__("a.b")`` yields the
    top-level package ``a`` unless it is given a ``fromlist``, as at runtime.
    The function is matched by name (``import_module`` under any module alias)
    because ``dotted`` has no import table.
    """
    if not isinstance(node, ast.Call):
        return None
    function = dotted(node.func)
    if function in {"__import__", "builtins.__import__"}:
        builtin = True
    elif function is not None and function.rpartition(".")[2] == "import_module":
        builtin = False
    else:
        return None
    names = node.args[:1] or [keyword.value for keyword in node.keywords if keyword.arg == "name"]
    if (len(names) != 1 or not isinstance(names[0], ast.Constant) or not isinstance(names[0].value, str)
            or not names[0].value):
        return _NON_LITERAL
    if names[0].value.startswith("."):
        return _RELATIVE
    module = names[0].value
    if builtin:
        fromlist = node.args[3] if len(node.args) > 3 else next(
            (keyword.value for keyword in node.keywords if keyword.arg == "fromlist"), None)
        if fromlist is None or (isinstance(fromlist, ast.Constant) and not fromlist.value) or (
                isinstance(fromlist, (ast.List, ast.Tuple)) and not fromlist.elts):
            module = module.partition(".")[0]
    return module


def dotted(node: ast.AST) -> str | None:
    """Return ``a.b.c`` for a Name/Attribute chain, else ``None``.

    A chain rooted at a literal dynamic import (``__import__("time").sleep``)
    is named after the imported module (``time.sleep``).
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    module = dynamic_import(node)
    if isinstance(module, str):
        parts.append(module)
        return ".".join(reversed(parts))
    return None


def chain_root(node: ast.AST) -> ast.AST:
    """The node an Attribute chain hangs off (``x`` in ``x.a.b``)."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node


def _scope_nodes(body: list[ast.stmt]):
    """Walk ``body`` without descending into nested function, class or lambda scopes."""
    scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    stack = list(body)
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, scopes):
            # The definition binds its name here and its body is its own
            # scope; everything else (decorators, defaults, annotations, class
            # bases and keywords) is evaluated here.
            for name, value in ast.iter_fields(current):
                if name == "body":
                    continue
                for child in value if isinstance(value, list) else [value]:
                    if isinstance(child, ast.AST):
                        stack.append(child)
            continue
        stack.extend(ast.iter_child_nodes(current))


def unresolvable_dynamic_imports(tree: ast.Module) -> list[tuple[ast.Attribute, _Unresolvable]]:
    """Attribute reads on a dynamic import the checker cannot name.

    ``importlib.import_module(name).sleep(1)``, and ``m.sleep(1)`` after
    ``m = importlib.import_module(name)`` in the same scope, could hide any
    hazard, so they are reported instead of ignored. A bare non-literal import
    (its result unused or passed on, e.g. to ``getattr``) is left alone. A
    name counts only in a scope where every binding of it is such an import.
    """
    found: list[tuple[ast.Attribute, _Unresolvable]] = []
    scopes: list[tuple[ast.AST | None, list[ast.stmt]]] = [(None, tree.body)]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scopes.append((node, node.body))
        elif isinstance(node, ast.Lambda):
            scopes.append((node, [ast.Expr(value=node.body)]))
    for owner, body in scopes:
        nodes = list(_scope_nodes(body))
        dynamic: dict[str, _Unresolvable] = {}
        dynamic_targets: set[int] = set()
        other: set[str] = set()
        if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # The scope's own parameters bind their names too.
            arguments = owner.args
            parameters = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
                          arguments.vararg, arguments.kwarg]
            other.update(parameter.arg for parameter in parameters if parameter is not None)
        for node in nodes:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                result = dynamic_import(node.value)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(result, _Unresolvable) and isinstance(target, ast.Name):
                        dynamic[target.id] = result
                        dynamic_targets.add(id(target))
        for node in nodes:
            # Any other binding of the name (assignment, loop, ``with``, import,
            # def) makes it ambiguous in this scope; it is then not reported.
            if (isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
                    and id(node) not in dynamic_targets):
                other.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                other.update((alias.asname or alias.name).partition(".")[0] for alias in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                other.add(node.name)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                other.add(node.name)
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                other.add(node.name)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                other.add(node.rest)
        unresolved_names = {name: why for name, why in dynamic.items() if name not in other}
        for node in nodes:
            if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
                # ``module.FLAG = True`` stores; it does not read a hazard.
                continue
            result = dynamic_import(node.value)
            if result is None and isinstance(node.value, ast.Name):
                result = unresolved_names.get(node.value.id)
            if isinstance(result, _Unresolvable):
                found.append((node, result))
    return sorted(found, key=lambda item: (item[0].lineno, item[0].col_offset))


class Imports:
    """Names bound by ``from time import sleep`` and similar imports."""

    def __init__(self, tree: ast.Module) -> None:
        self.aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.aliases[alias.asname] = alias.name
            elif (isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
                  and isinstance(module := dynamic_import(node.value), str)):
                # ``clock = __import__("time")`` binds the module like ``import time as clock``.
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        self.aliases[target.id] = module

    def name_of(self, node: ast.AST) -> str | None:
        """The resolved dotted name of ``node``.

        A chain rooted at a literal dynamic import already names its module
        (``__import__("datetime").datetime.now``) and skips the alias table, so
        ``from datetime import datetime`` cannot rewrite its head.
        """
        name = dotted(node)
        if isinstance(dynamic_import(chain_root(node)), str):
            return name
        return self.resolve(name)

    def resolve(self, name: str | None) -> str | None:
        if name is None:
            return None
        head, _, rest = name.partition(".")
        target = self.aliases.get(head)
        if target is None:
            return name
        return f"{target}.{rest}" if rest else target


def is_zero(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and not isinstance(node.value, bool) and node.value == 0


def realtime_reason(decorator: ast.AST, name_of=dotted) -> str | None | bool:
    """Return the reason for a realtime marker, ``False`` if not one, ``None`` if reasonless.

    ``name_of`` names a node through import aliases (``import pytest as pt``,
    ``from pytest import mark``).
    """
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if name_of(target) not in {"pytest.mark.realtime", "mark.realtime"}:
        return False
    if not isinstance(decorator, ast.Call):
        return None
    for keyword in decorator.keywords:
        if keyword.arg == "reason" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            if isinstance(value, str) and value.strip():
                return value
    return None


def quarantine_issue(decorator: ast.AST, name_of=dotted) -> str | None | bool:
    """Return the issue for a quarantine marker, ``False`` if not one, ``None`` if it names none."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if name_of(target) not in {"pytest.mark.quarantine", "mark.quarantine"}:
        return False
    if not isinstance(decorator, ast.Call):
        return None
    values = [keyword.value for keyword in decorator.keywords if keyword.arg == "issue"]
    if not values and len(decorator.args) == 1:
        values = list(decorator.args)
    if len(values) != 1 or not isinstance(values[0], ast.Constant):
        return None
    value = values[0].value
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    return text.lstrip("#") if ISSUE_REFERENCE.fullmatch(text) else None


class Scanner(ast.NodeVisitor):
    def __init__(self, display_path: str, tree: ast.Module, source: str, *, support: bool) -> None:
        self.display_path = display_path
        # split("\n"), not splitlines(): U+2028 and form feeds are not line breaks to ast.
        self.lines = source.split("\n")
        self.imports = Imports(tree)
        self.support = support
        self.findings: list[Finding] = []
        self.exempt: list[Finding] = []
        self.errors: list[str] = []
        self.exempt_depth = 0
        self.module_exempt = False
        self.quarantined: list[tuple[int, str]] = []
        self.accepted_quarantine: set[int] = set()
        self.realtime_markers = 0
        self.handler_stack: list[bool] = []
        self.tainted_stack: list[set[str]] = [self._tainted(tree.body)]
        self.module_exempt = self._module_exempt(tree)

    # -- exemption -----------------------------------------------------------------
    def _check_quarantine(self, node: ast.AST, decorator: ast.AST) -> None:
        issue = quarantine_issue(decorator, self.imports.name_of)
        if issue is False:
            return
        self.accepted_quarantine.add(id(decorator.func if isinstance(decorator, ast.Call) else decorator))
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.errors.append(
                f"{self.display_path}:{node.lineno}: quarantine one test function at a time, "
                "not a class or module, so the cap counts tests"
            )
            return
        if issue is None:
            self.errors.append(
                f"{self.display_path}:{node.lineno}: pytest.mark.quarantine requires issue=<issue number>"
            )
            return
        self.quarantined.append((node.lineno, issue))

    def check_stray_quarantine(self, tree: ast.Module) -> None:
        """A quarantine marker anywhere but a test-function decorator escapes the
        issue check, the cap and the quarantine lane, so it is an error."""
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and self.imports.name_of(node) in {"pytest.mark.quarantine", "mark.quarantine"}
                    and id(node) not in self.accepted_quarantine):
                self.errors.append(
                    f"{self.display_path}:{node.lineno}: pytest.mark.quarantine is only supported as a "
                    "decorator on a test function (not pytest.param marks, an alias or an annotated pytestmark)"
                )

    def _check_marker(self, node: ast.AST, decorator: ast.AST) -> bool:
        self._check_quarantine(node, decorator)
        reason = realtime_reason(decorator, self.imports.name_of)
        if reason is False:
            return False
        if reason is None:
            self.errors.append(
                f"{self.display_path}:{node.lineno}: pytest.mark.realtime requires a non-empty reason=..."
            )
            return False
        self.record(node, "realtime")
        self.realtime_markers += 1
        return True

    def _module_exempt(self, tree: ast.Module) -> bool:
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
                continue
            values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            # Evaluate every marker (no short-circuit) so each one is validated.
            if any([self._check_marker(node, value) for value in values]):
                return True
        return False

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        exempt = any([self._check_marker(node, decorator) for decorator in node.decorator_list])
        if exempt:
            self.exempt_depth += 1
        is_function = not isinstance(node, ast.ClassDef)
        if is_function:
            inherited = self.tainted_stack[-1]
            self.tainted_stack.append(inherited | self._tainted(node.body, inherited))
        self.handler_stack.append(False)
        self.generic_visit(node)
        self.handler_stack.pop()
        if is_function:
            self.tainted_stack.pop()
        if exempt:
            self.exempt_depth -= 1

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope
    visit_ClassDef = _visit_scope

    # -- recording -----------------------------------------------------------------
    def record(self, node: ast.AST, category: str) -> None:
        if self.support and category in SUPPORT_EXEMPT:
            return
        snippet = self.lines[node.lineno - 1].strip()[:120] if node.lineno <= len(self.lines) else ""
        finding = Finding(self.display_path, node.lineno, category, snippet)
        if (self.module_exempt or self.exempt_depth) and category in REALTIME_EXEMPT:
            self.exempt.append(finding)
        else:
            self.findings.append(finding)

    # -- clock taint ---------------------------------------------------------------
    def is_clock_read(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        name = self.imports.name_of(node.func)
        if name is not None:
            module, _, function = name.rpartition(".")
            if module == "time" and function in CLOCK_FUNCTIONS:
                return True
            if name.endswith("loop.time"):
                return True
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "time" and isinstance(func.value, ast.Call):
            accessor = dotted(func.value.func) or ""
            return accessor.rpartition(".")[2] in LOOP_ACCESSORS
        return False

    def reads_clock(self, node: ast.AST, tainted: set[str]) -> bool:
        """Whether ``node`` is a clock-valued expression.

        A clock read, a name assigned from one, or arithmetic / ``min`` /
        ``max`` / ``round`` / numeric conversion over such values. Containers
        and arbitrary calls that merely receive a clock value are not.
        """
        if self.is_clock_read(node):
            return True
        if isinstance(node, (ast.Name, ast.Attribute)):
            return dotted(node) in tainted
        if isinstance(node, ast.BinOp):
            return self.reads_clock(node.left, tainted) or self.reads_clock(node.right, tainted)
        if isinstance(node, ast.UnaryOp):
            return self.reads_clock(node.operand, tainted)
        if isinstance(node, ast.IfExp):
            return self.reads_clock(node.body, tainted) or self.reads_clock(node.orelse, tainted)
        if isinstance(node, ast.Call) and dotted(node.func) in CLOCK_WRAPPERS:
            return any(self.reads_clock(argument, tainted) for argument in node.args)
        return False

    def _tainted(self, body: list[ast.stmt], inherited: set[str] | None = None) -> set[str]:
        """Names assigned (directly or transitively) from a clock read in this scope."""
        assignments: list[tuple[list[ast.AST], ast.AST]] = []
        for statement in body:
            for node in self._scope_walk(statement):
                if isinstance(node, ast.Assign):
                    assignments.append((node.targets, node.value))
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
                    assignments.append(([node.target], node.value))
                elif isinstance(node, ast.NamedExpr):
                    assignments.append(([node.target], node.value))
        tainted: set[str] = set(inherited or ())
        changed = True
        while changed:
            changed = False
            for targets, value in assignments:
                if not self.reads_clock(value, tainted):
                    continue
                for target in targets:
                    elements = target.elts if isinstance(target, (ast.Tuple, ast.List)) else [target]
                    for element in elements:
                        name = dotted(element) if isinstance(element, (ast.Name, ast.Attribute)) else None
                        if name and name not in tainted:
                            tainted.add(name)
                            changed = True
        return tainted

    @staticmethod
    def _scope_walk(node: ast.AST):
        """Walk a statement without descending into nested function or class scopes."""
        stack = [node]
        while stack:
            current = stack.pop()
            yield current
            for child in ast.iter_child_nodes(current):
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                    stack.append(child)

    # -- visitors ------------------------------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        name = self.imports.name_of(node.func)
        if name == "time.sleep":
            self.record(node, "sleep")
        elif name == "asyncio.sleep":
            delay = node.args[0] if node.args else next(
                (keyword.value for keyword in node.keywords if keyword.arg == "delay"), None
            )
            if delay is None or not is_zero(delay):
                self.record(node, "sleep")
        elif name == "subprocess.Popen":
            self.record(node, "popen")
        elif name == "pytest.skip" and any(self.handler_stack):
            self.record(node, "timeout-skip")
        elif name == "math.isclose" or (
            isinstance(node.func, ast.Attribute) and node.func.attr in COMPARE_METHODS
        ):
            if any(self.reads_clock(argument, self.tainted_stack[-1]) for argument in node.args):
                self.record(node, "clock-compare")
        if (isinstance(node.func, ast.Attribute) and node.func.attr in ENVIRON_MUTATORS
                and self.imports.name_of(node.func.value) == "os.environ"):
            self.record(node, "environ-assign")
        if dotted(node.func) == "setattr" and node.args and self._is_settings(node.args[0]):
            self.record(node, "settings-assign")
        if dotted(node.func) in PATCH_CALLS or name in PATCH_CALLS:
            values = [*node.args, *(keyword.value for keyword in node.keywords)]
            if any(self._names_get_session(value) for value in values):
                self.record(node, "get-session-patch")
        self.generic_visit(node)

    @staticmethod
    def _names_get_session(value: ast.AST) -> bool:
        """A string (or f-string) patch target that ends in ``get_session``."""
        if isinstance(value, ast.JoinedStr) and value.values:
            value = value.values[-1]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value == "get_session" or value.value.endswith(".get_session")
        return False

    def _is_settings(self, node: ast.AST) -> bool:
        base = self.imports.name_of(node) or ""
        return base == "settings" or base.endswith(".settings")

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        names = [dotted(t) or "" for t in ast.walk(node.type)] if node.type is not None else []
        is_timeout = any(name.endswith(TIMEOUT_EXCEPTIONS) for name in names)
        self.handler_stack.append(is_timeout)
        self.generic_visit(node)
        self.handler_stack.pop()

    def visit_Compare(self, node: ast.Compare) -> None:
        # ``started is None`` checks presence, not elapsed time.
        if all(isinstance(op, (ast.Is, ast.IsNot)) for op in node.ops):
            self.generic_visit(node)
            return
        if any(self.reads_clock(operand, self.tainted_stack[-1]) for operand in (node.left, *node.comparators)):
            self.record(node, "clock-compare")
        self.generic_visit(node)

    def _check_targets(self, targets: list[ast.AST], node: ast.AST) -> None:
        for target in targets:
            if self.imports.name_of(target) == "os.environ":
                # ``os.environ |= {...}`` or rebinding the mapping itself.
                self.record(node, "environ-assign")
                continue
            for element in ast.walk(target):
                if isinstance(element, ast.Attribute):
                    if self._is_settings(element.value):
                        self.record(node, "settings-assign")
                elif isinstance(element, ast.Subscript):
                    base = self.imports.name_of(element.value)
                    if base == "os.environ":
                        self.record(node, "environ-assign")

    def visit_Assign(self, node: ast.Assign) -> None:
        self._check_targets(node.targets, node)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            if isinstance(target, ast.Subscript) and self.imports.name_of(target.value) == "os.environ":
                self.record(node, "environ-assign")
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._check_targets([node.target], node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._check_targets([node.target], node)
        self.generic_visit(node)


@dataclass
class ScanResult:
    findings: list[Finding]
    exempt: list[Finding]
    errors: list[str]
    quarantined: list[tuple[str, int, str]] = field(default_factory=list)
    realtime_files: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, dict[str, int]]:
        table: dict[str, Counter[str]] = {}
        for finding in self.findings:
            table.setdefault(finding.path, Counter())[finding.category] += 1
        return {
            path: {category: counter[category] for category in CATEGORIES if counter[category]}
            for path, counter in sorted(table.items())
        }


def scan(tests_dir: Path, root: Path) -> ScanResult:
    findings: list[Finding] = []
    exempt: list[Finding] = []
    errors: list[str] = []
    quarantined: list[tuple[str, int, str]] = []
    realtime_files: list[str] = []
    for path in sorted(tests_dir.rglob("*.py")):
        display_path = path.relative_to(root).as_posix()
        relative = path.relative_to(tests_dir).parts
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as error:
            errors.append(f"{display_path}:{error.lineno}: cannot parse: {error.msg}")
            continue
        scanner = Scanner(display_path, tree, source, support=relative[:1] == (SUPPORT_PACKAGE,))
        scanner.visit(tree)
        scanner.check_stray_quarantine(tree)
        for node, unresolvable in unresolvable_dynamic_imports(tree):
            errors.append(
                f"{display_path}:{node.lineno}: dynamic import with {unresolvable.why} is "
                f"{unresolvable.label} and cannot be checked for determinism hazards; import the module by name"
            )
        findings.extend(scanner.findings)
        exempt.extend(scanner.exempt)
        errors.extend(scanner.errors)
        quarantined.extend((display_path, line, issue) for line, issue in scanner.quarantined)
        if scanner.realtime_markers:
            realtime_files.append(display_path)
    if len(quarantined) > QUARANTINE_CAP:
        errors.append(
            f"{len(quarantined)} quarantine markers exceed the cap of {QUARANTINE_CAP}; "
            "fix a quarantined test (50-of-50 stress pass) before quarantining another"
        )
    return ScanResult(findings, exempt, errors, quarantined, realtime_files)


def load_baseline(path: Path) -> dict[str, dict[str, int]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    files = data.get("files")
    if not isinstance(files, dict):
        raise ValueError(f"{path}: missing 'files' mapping")
    for file_path, counts in files.items():
        if not isinstance(counts, dict) or any(
            category not in CATEGORIES or not isinstance(value, int) or value <= 0
            for category, value in counts.items()
        ):
            raise ValueError(f"{path}: invalid entry for {file_path!r}")
    return files


def write_baseline(path: Path, counts: dict[str, dict[str, int]]) -> None:
    payload = {
        "description": (
            "Per-file test-determinism hazard counts for scripts/check_test_determinism.py (#1218). "
            "Counts may only go down: lower them with --update after removing a hazard. "
            "The only exemption is @pytest.mark.realtime(reason=...)."
        ),
        "categories": list(CATEGORIES),
        "files": counts,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def compare(
    current: dict[str, dict[str, int]], baseline: dict[str, dict[str, int]]
) -> tuple[list[tuple[str, str, int, int]], list[tuple[str, str, int, int]]]:
    above: list[tuple[str, str, int, int]] = []
    below: list[tuple[str, str, int, int]] = []
    for path in sorted(set(current) | set(baseline)):
        for category in CATEGORIES:
            have = current.get(path, {}).get(category, 0)
            allowed = baseline.get(path, {}).get(category, 0)
            if have > allowed:
                above.append((path, category, have, allowed))
            elif have < allowed:
                below.append((path, category, have, allowed))
    return above, below


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tests-dir", type=Path, default=TESTS_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--src-dir", type=Path, default=SRC_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--baseline", type=Path, default=BASELINE, help="baseline JSON path")
    parser.add_argument(
        "--update",
        action="store_true",
        help="lower baseline counts to the current counts (never raises one); creates a missing baseline",
    )
    parser.add_argument("--list", action="store_true", help="print every counted finding")
    parser.add_argument(
        "--rename", nargs=2, metavar=("OLD", "NEW"), action="append", default=[],
        help="move a renamed file's baseline entry (repo-relative paths, e.g. backend/tests/test_a.py); repeatable",
    )
    parser.add_argument(
        "--quarantined-files", action="store_true",
        help="print backend-relative test files that hold a quarantine marker, one per line, and exit",
    )
    parser.add_argument(
        "--realtime-files", action="store_true",
        help="print backend-relative test files that hold a realtime marker, one per line, and exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    if sys.version_info < (3, 12):
        print("check_test_determinism.py needs Python 3.12+ to parse the backend tests", file=sys.stderr)
        return 2
    args = parse_args(argv)
    root = args.root.resolve()
    result = scan(args.tests_dir.resolve(), root)
    src_dir = args.src_dir.resolve()
    if src_dir.is_dir():
        result.errors.extend(scan_clock_seam(src_dir, root))
    for error in result.errors:
        print(error, file=sys.stderr)
    current = result.counts()

    if args.quarantined_files or args.realtime_files:
        listed = sorted({path for path, _, _ in result.quarantined}) if args.quarantined_files else result.realtime_files
        for path in listed:
            print(Path(path).relative_to("backend").as_posix() if path.startswith("backend/") else path)
        return 1 if result.errors else 0

    if args.list:
        for finding in result.findings:
            print(f"{finding.path}:{finding.line}: {finding.category}: {finding.snippet}")

    if args.update and not args.baseline.exists():
        if result.errors:
            print("test determinism: --update refuses to write while there are scan errors", file=sys.stderr)
            return 1
        write_baseline(args.baseline, current)
        print(f"test determinism: wrote new baseline {args.baseline} ({len(result.findings)} findings)")
        return 1 if result.errors else 0

    try:
        baseline = load_baseline(args.baseline)
    except (OSError, ValueError) as error:
        print(f"test determinism: cannot read baseline: {error}", file=sys.stderr)
        return 1
    if args.rename:
        if result.errors:
            print("test determinism: --rename refuses to write while there are scan errors", file=sys.stderr)
            return 1
        for old, new in args.rename:
            if not (root / new).is_file():
                print(f"test determinism: --rename: {new} does not exist", file=sys.stderr)
                return 1
            if old not in baseline:
                print(f"test determinism: --rename: {old} has no baseline entry", file=sys.stderr)
                return 1
            if new in baseline:
                print(f"test determinism: --rename: {new} already has a baseline entry; merge by hand",
                      file=sys.stderr)
                return 1
            baseline[new] = baseline.pop(old)
        write_baseline(args.baseline, dict(sorted(baseline.items())))
        print(f"test determinism: moved {len(args.rename)} baseline entr{'y' if len(args.rename) == 1 else 'ies'}")
    above, below = compare(current, baseline)

    by_location: dict[tuple[str, str], list[Finding]] = {}
    for finding in result.findings:
        by_location.setdefault((finding.path, finding.category), []).append(finding)
    for path, category, have, allowed in above:
        print(f"{path}: {category} count {have} is above the baseline {allowed}. {REMEDY[category]}",
              file=sys.stderr)
        for finding in by_location.get((path, category), []):
            print(f"  {finding.path}:{finding.line}: {finding.snippet}", file=sys.stderr)

    if args.update:
        if result.errors:
            print("test determinism: --update refuses to write while there are scan errors", file=sys.stderr)
            return 1
        if above:
            print("test determinism: --update never raises a count; fix the findings above first", file=sys.stderr)
            return 1
        write_baseline(args.baseline, current)
        print(f"test determinism: baseline lowered for {len(below)} entr{'y' if len(below) == 1 else 'ies'}")
        return 1 if result.errors else 0

    missing = sorted({path for path, *_ in below if not (root / path).exists()})
    for path, category, have, allowed in below:
        if path in missing:
            continue
        print(
            f"{path}: {category} count {have} is below the baseline {allowed}. "
            f"Lower the baseline: python3 scripts/check_test_determinism.py --update",
            file=sys.stderr,
        )
    for path in missing:
        print(
            f"{path}: has a baseline entry but no longer exists. If it was renamed: "
            f"python3 scripts/check_test_determinism.py --rename {path} <new path>. If it was split, "
            "move its counts to the new files by hand in scripts/test_determinism_baseline.json. "
            "If it was deleted: --update.",
            file=sys.stderr,
        )
    if above or below or result.errors:
        return 1
    totals = Counter(finding.category for finding in result.findings)
    summary = ", ".join(f"{category}={totals[category]}" for category in CATEGORIES)
    print(
        f"test determinism: ok ({summary}; realtime-exempt={len(result.exempt)}; "
        f"quarantined={len(result.quarantined)}/{QUARANTINE_CAP})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
