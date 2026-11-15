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

Quarantine (#1220): ``@pytest.mark.quarantine(issue=NNNN)`` must name an issue
number and decorate a single test function (other placements are errors), and
at most ``QUARANTINE_CAP`` quarantine markers may exist. A
quarantined test still runs, in the non-blocking quarantine lane; it leaves
quarantine only through a fix that removes the marker after a 50-of-50 stress
pass. There is no calendar expiry. ``--quarantined-files`` and
``--realtime-files`` list the files those lanes run.

The per-file, per-category counts must equal the baseline exactly. A count
above the baseline fails, and so does a count below it until the baseline is
lowered with ``--update``, so the numbers only go down. ``--update`` never
raises a count, and refuses to write while any file fails to scan. The only
exemption is ``@pytest.mark.realtime(reason="...")`` (keyword ``reason`` is
required) as a decorator on the enclosing test function or class, or in the
module's ``pytestmark``.
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
)
SUPPORT_EXEMPT = frozenset({"get-session-patch", "popen"})
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


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    category: str
    snippet: str


def dotted(node: ast.AST) -> str | None:
    """Return ``a.b.c`` for a Name/Attribute chain, else ``None``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


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


def realtime_reason(decorator: ast.AST) -> str | None | bool:
    """Return the reason for a realtime marker, ``False`` if not one, ``None`` if reasonless."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if dotted(target) not in {"pytest.mark.realtime", "mark.realtime"}:
        return False
    if not isinstance(decorator, ast.Call):
        return None
    for keyword in decorator.keywords:
        if keyword.arg == "reason" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            if isinstance(value, str) and value.strip():
                return value
    return None


def quarantine_issue(decorator: ast.AST) -> str | None | bool:
    """Return the issue for a quarantine marker, ``False`` if not one, ``None`` if it names none."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if dotted(target) not in {"pytest.mark.quarantine", "mark.quarantine"}:
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
        self.quarantined: list[tuple[int, str]] = []
        self.accepted_quarantine: set[int] = set()
        self.realtime_markers = 0
        self.handler_stack: list[bool] = []
        self.tainted_stack: list[set[str]] = [self._tainted(tree.body)]
        self.module_exempt = self._module_exempt(tree)

    # -- exemption -----------------------------------------------------------------
    def _check_quarantine(self, node: ast.AST, decorator: ast.AST) -> None:
        issue = quarantine_issue(decorator)
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
            if (isinstance(node, ast.Attribute) and dotted(node) in {"pytest.mark.quarantine", "mark.quarantine"}
                    and id(node) not in self.accepted_quarantine):
                self.errors.append(
                    f"{self.display_path}:{node.lineno}: pytest.mark.quarantine is only supported as a "
                    "decorator on a test function (not pytest.param marks, an alias or an annotated pytestmark)"
                )

    def _check_marker(self, node: ast.AST, decorator: ast.AST) -> bool:
        self._check_quarantine(node, decorator)
        reason = realtime_reason(decorator)
        if reason is False:
            return False
        if reason is None:
            self.errors.append(
                f"{self.display_path}:{node.lineno}: pytest.mark.realtime requires a non-empty reason=..."
            )
            return False
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
        if self.module_exempt or self.exempt_depth:
            self.exempt.append(finding)
        else:
            self.findings.append(finding)

    # -- clock taint ---------------------------------------------------------------
    def is_clock_read(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        name = self.imports.resolve(dotted(node.func))
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
        name = self.imports.resolve(dotted(node.func))
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
                and self.imports.resolve(dotted(node.func.value)) == "os.environ"):
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
        base = self.imports.resolve(dotted(node)) or ""
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
            if self.imports.resolve(dotted(target)) == "os.environ":
                # ``os.environ |= {...}`` or rebinding the mapping itself.
                self.record(node, "environ-assign")
                continue
            for element in ast.walk(target):
                if isinstance(element, ast.Attribute):
                    if self._is_settings(element.value):
                        self.record(node, "settings-assign")
                elif isinstance(element, ast.Subscript):
                    base = self.imports.resolve(dotted(element.value))
                    if base == "os.environ":
                        self.record(node, "environ-assign")

    def visit_Assign(self, node: ast.Assign) -> None:
        self._check_targets(node.targets, node)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            if isinstance(target, ast.Subscript) and self.imports.resolve(dotted(target.value)) == "os.environ":
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
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--baseline", type=Path, default=BASELINE, help="baseline JSON path")
    parser.add_argument(
        "--update",
        action="store_true",
        help="lower baseline counts to the current counts (never raises one); creates a missing baseline",
    )
    parser.add_argument("--list", action="store_true", help="print every counted finding")
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

    for path, category, have, allowed in below:
        print(
            f"{path}: {category} count {have} is below the baseline {allowed}. "
            f"Lower the baseline: python3 scripts/check_test_determinism.py --update",
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
