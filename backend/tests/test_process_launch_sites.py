"""Inventory of every subprocess launch site under ``backend/src`` (#1103).

Agent commands must reach the OS only through the process sandbox. A new
launcher fails this test until it is reviewed and added below, with the
reason it is not an agent-controlled command (or that it is sandboxed).
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import sys

from src.execution.repo_isolation import (
    CommandIsolation,
    IsolationLimits,
    IsolationPosture,
    IsolationRuntime,
)
from src.workspace import AgentAccessIndex, AgentWorkspaceAccess

SOURCE = Path(__file__).resolve().parents[1] / "src"
_LAUNCHERS = {
    "subprocess": {"Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"},
    "os": {"system", "popen", "execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe",
           "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle", "spawnlp", "spawnlpe",
           "posix_spawn", "posix_spawnp", "fork", "forkpty"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
    "pty": {"spawn", "fork"},
}

# (file, enclosing function, call) -> count.  Grouped by reason.
REVIEWED = {
    # Agent process tools: the single launch site, always the OS sandbox.
    ("tools/process_tools.py", "_spawn_process", "subprocess.Popen"): 1,
    ("tools/process_limit_bootstrap.py", "main", "os.execvpe"): 1,
    # Sandbox self-tests (fixed probe programs, no agent input).
    ("execution/process_isolation.py", "_run_probe", "subprocess.Popen"): 1,
    ("execution/process_isolation.py", "bwrap_flags", "subprocess.run"): 1,
    ("execution/repo_isolation.py", "_disable_userns_supported", "subprocess.run"): 1,
    ("execution/repo_isolation.py", "probe", "subprocess.run"): 1,
    # Repository repair/publication (#1065 sandbox, reviewed snapshots, git).
    ("execution/repo_node.py", "NodeRepoRepairExecutor.execute_job", "subprocess.Popen"): 1,
    ("execution/repo_node.py", "runtime_identity", "subprocess.run"): 2,
    ("execution/repo_original_producer.py", "run_original_producer", "subprocess.Popen"): 1,
    ("execution/repo_publication.py", "_bounded_git", "subprocess.Popen"): 1,
    ("execution/repo_publication_supervisor.py", "run", "subprocess.Popen"): 1,
    ("execution/repo_sandbox.py", "LocalRepoRepairExecutor._run_supervised_python", "subprocess.Popen"): 1,
    ("execution/repo_sandbox.py", "LocalRepoRepairExecutor.iterative_preflight", "subprocess.run"): 1,
    ("execution/repo_sandbox.py", "_git_numstat_paths", "subprocess.run"): 1,
    ("execution/repo_supervisor.py", "run_command", "subprocess.Popen"): 1,
    ("execution/repo_worker.py", "_run_fixed", "subprocess.Popen"): 1,
    # Reviewed tool packages (own bwrap rootfs; see the bind check below).
    ("execution/tool_package_runner.py", "execute", "subprocess.Popen"): 1,
    ("execution/tool_package_supervisor.py", "run_isolated", "subprocess.Popen"): 1,
    # Runtime plugin host (reviewed Cordis package and toolchain).
    ("runtime_plugins/bridge.py", "CordisHost.start", "asyncio.create_subprocess_exec"): 1,
    ("runtime_plugins/cli.py", "build", "subprocess.run"): 1,
    ("runtime_plugins/cli.py", "reviewed_npm", "subprocess.run"): 1,
    ("runtime_plugins/composition.py", "reviewed_node", "subprocess.run"): 1,
    # Document and audio workers (fixed reviewed converters).
    ("work_board/document_build_native.py", "invoke", "asyncio.create_subprocess_exec"): 1,
    ("work_board/document_compare_native.py", "execute", "asyncio.create_subprocess_exec"): 1,
    ("work_board/document_compare_supervisor.py", "main", "subprocess.Popen"): 1,
    ("work_board/document_pairs.py", "probe_upload_profile", "asyncio.create_subprocess_exec"): 1,
    ("work_board/documents.py", "DocumentService.parse", "asyncio.create_subprocess_exec"): 1,
    ("guardian/audio_worker.py", "_run_process", "asyncio.create_subprocess_exec"): 1,
    # Operator-host helpers and fixed repository claim scans.
    ("api/settings.py", "_choose_screenshot_folder_with_native_dialog", "asyncio.create_subprocess_exec"): 1,
    ("cockpit/post_dp_operator_debugging_recovery.py", "_strategy_claim_scan_command_evidence", "subprocess.run"): 1,
    ("extensions/post_dp_browser_computer_use_reliability.py", "_run_strategy_claim_scan", "subprocess.run"): 1,
    ("extensions/post_dp_marketplace_lifecycle_gap_closure.py", "_run_strategy_claim_scan", "subprocess.run"): 1,
    ("guardian/post_dp_guardian_memory_gap_closure.py", "guardian_memory_false_claim_scan_v2_receipts", "subprocess.run"): 1,
}


class _Inventory(ast.NodeVisitor):
    def __init__(self, relative: str):
        self.relative = relative
        self.scope: list[str] = []
        self.sites: Counter = Counter()
        self.aliases: list[str] = []

    def _scoped(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Import(self, node):
        for alias in node.names:
            if alias.name in _LAUNCHERS and alias.asname or alias.name.split(".")[0] == "multiprocessing":
                self.aliases.append(f"{self.relative}: import {alias.name} as {alias.asname}")

    def visit_ImportFrom(self, node):
        if node.module in _LAUNCHERS and any(alias.name in _LAUNCHERS[node.module] for alias in node.names):
            self.aliases.append(f"{self.relative}: from {node.module} import ...")
        if (node.module or "").split(".")[0] in {"multiprocessing", "concurrent"} and any(
                "Process" in alias.name for alias in node.names):
            self.aliases.append(f"{self.relative}: from {node.module} import ...")

    def visit_Call(self, node):
        function = node.func
        if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            if function.attr in _LAUNCHERS.get(function.value.id, ()):
                self.sites[(self.relative, ".".join(self.scope) or "<module>",
                            f"{function.value.id}.{function.attr}")] += 1
        self.generic_visit(node)


def _inventory() -> tuple[Counter, list[str]]:
    sites: Counter = Counter()
    aliases: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        visitor = _Inventory(path.relative_to(SOURCE).as_posix())
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), str(path)))
        sites.update(visitor.sites)
        aliases += visitor.aliases
    return sites, aliases


def test_every_subprocess_launch_site_is_reviewed():
    sites, aliases = _inventory()
    assert aliases == [], "aliased process launchers bypass this inventory"
    unreviewed = {site: count for site, count in sites.items() if REVIEWED.get(site) != count}
    stale = {site: count for site, count in REVIEWED.items() if sites.get(site) != count}
    assert not unreviewed, f"new or changed launch sites need review (#1103): {unreviewed}"
    assert not stale, f"reviewed launch sites no longer exist; update the inventory: {stale}"


def test_process_tools_launch_only_through_the_sandbox_seam():
    tree = ast.parse((SOURCE / "tools" / "process_tools.py").read_text(encoding="utf-8"))
    callers = set()
    for function in ast.walk(tree):
        if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_spawn_process":
                    callers.add(function.name)
                    keywords = {keyword.arg for keyword in node.keywords}
                    # Every launch passes the sandbox's fds and argv.
                    assert "pass_fds" in keywords and "start_new_session" in keywords
                    assert ast.unparse(node.args[0]) == "sandbox.launch.argv"
    assert callers == {"run_command", "start_process"}


def _binds(argv: list[str]) -> list[tuple[str, str]]:
    pairs = []
    for index, item in enumerate(argv):
        if item in {"--bind", "--ro-bind", "--bind-try", "--ro-bind-try", "--dev-bind"}:
            pairs.append((item, argv[index + 1]))
    return pairs


def test_tool_package_supervisor_binds_no_runtime_owned_state_writable(tmp_path):
    tree = ast.parse((SOURCE / "execution" / "tool_package_supervisor.py").read_text(encoding="utf-8"))
    argv = next(node for node in ast.walk(tree) if isinstance(node, ast.List)
                and any(isinstance(item, ast.Constant) and item.value == "--unshare-all" for item in node.elts))
    items = [ast.unparse(item) if not isinstance(item, ast.Constant) else item.value for item in argv.elts]
    binds = _binds(items)
    assert ("--bind", "str(output / 'result.json')") in binds
    writable = [source for flag, source in binds if flag == "--bind"]
    assert writable == ["str(output / 'result.json')"], "only the precreated result file may be writable"
    assert {source for flag, source in binds if flag == "--ro-bind"} == {
        "str(runtime_root / 'rootfs')", "str(stage / 'input.json')", "str(stage / 'package.py')"}
    # The concrete roots: the reviewed runtime is READ_ONLY registry state
    # (bound read-only) and the stage is ordinary artifact space.
    index = AgentAccessIndex(tmp_path)
    runtime = tmp_path / "artifacts" / "tool-package-runtime" / "profile" / "rootfs"
    stage = tmp_path / "artifacts" / "tool-package-runs" / "job-1"
    assert index.access(runtime) is AgentWorkspaceAccess.READ_ONLY
    assert index.access(stage / "out" / "result.json") is AgentWorkspaceAccess.READ_WRITE


def test_repo_sandbox_binds_no_runtime_owned_workspace_state(tmp_path):
    workspace = tmp_path.resolve()
    stage = workspace / "artifacts" / "repo-sandbox" / "staging" / "token"
    for path in (stage / "repo", stage / "home", stage / "tmp"):
        path.mkdir(parents=True)
    limits = IsolationLimits()
    posture = IsolationPosture("isolated", "bwrap_userns", "netns_loopback_only_verified",
                               "rlimit_cpu_as_fsize_nproc_and_wall_timeout", "snapshot_bind_only",
                               bwrap_path="/usr/bin/bwrap", prlimit_path="/usr/bin/prlimit",
                               limits_digest=limits.digest)
    isolation = CommandIsolation(posture, IsolationRuntime(Path(sys.executable)), limits, stage,
                                 (stage / "home", stage / "tmp"))
    argv, _env = isolation.command(["git", "status"], stage / "repo", {})
    index = AgentAccessIndex(workspace)
    for flag, source in _binds(argv):
        path = Path(source)
        if workspace not in path.parents and path != workspace:
            continue
        access = index.access(path)
        assert access is not AgentWorkspaceAccess.DENIED, source
        if flag == "--bind":
            assert access is AgentWorkspaceAccess.READ_WRITE, source
