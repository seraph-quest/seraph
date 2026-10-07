"""Closed failure-only diagnostics; no native worker or vendor execution."""
import ast
import hashlib
import inspect
import json
from pathlib import Path
import sys

import pytest

from src.execution.repo_sandbox import RepoSandboxError
from tests import test_repo_repair_local_vertical as native
from tests import test_repo_publication_runtime as runtime


def receipt(reason):
    return {"profile": "repo-python-pytest-v1", "status": "blocked", "reason": reason}


def test_literal_allowlist_is_exact_original_worker_source():
    source = (Path(__file__).resolve().parents[1] / "src/execution/repo_worker.py").read_text()
    assert hashlib.sha256(source.encode()).hexdigest() == "2a49b14e4855adef27b300b2b55268b7b0f615e2023f45cdbe9efa0603e1f2e4"
    rows = []
    def visit(node, function=""):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "WorkerInputError"
            and len(node.exc.args) == 1 and isinstance(node.exc.args[0], ast.Constant)
            and type(node.exc.args[0].value) is str):
            rows.append((node.exc.args[0].value, f"worker_input_guard_{node.lineno}", function, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, function)
    visit(ast.parse(source))
    assert sorted(rows) == sorted(native._PUBLICATION_WORKER_GUARD_CANDIDATES)


def test_unique_literal_projects_only_static_candidate():
    rows = native._PUBLICATION_WORKER_GUARD_CANDIDATES
    selected = next(row for row in rows if sum(other[0] == row[0] for other in rows) == 1)
    result = native._publication_worker_receipt_projection(receipt(selected[0]), receipt(selected[0]))
    assert result == {"guard_candidate": selected[1], "file": "src/execution/repo_worker.py",
                      "function": selected[2], "line": selected[3]}
    assert selected[0] not in json.dumps(result)


@pytest.mark.parametrize("reason", ["private-secret/file: dynamic denial", "unrecognized", "allowed path: /secret"])
def test_nonliteral_reason_is_closed_unknown(reason):
    assert native._publication_worker_receipt_projection(receipt(reason), receipt(reason)) == {
        "guard_candidate": "worker_input_guard_unknown"}


def test_duplicate_literal_is_ambiguous():
    rows = native._PUBLICATION_WORKER_GUARD_CANDIDATES
    reason = next(row[0] for row in rows if sum(other[0] == row[0] for other in rows) > 1)
    assert native._publication_worker_receipt_projection(receipt(reason), receipt(reason)) == {
        "guard_candidate": "worker_input_guard_ambiguous"}


@pytest.mark.parametrize("malformed", [None, [], {}, {"reason": "secret"},
    {"profile": "repo-python-pytest-v1", "status": "blocked", "reason": "secret", "extra": "secret"},
    receipt(None), receipt(1), receipt(""), receipt("x" * 513), receipt("x\x00secret"),
    {"profile": "other", "status": "blocked", "reason": "secret"},
    {"profile": "repo-python-pytest-v1", "status": "success", "reason": "secret"}])
def test_malformed_receipt_is_unavailable(malformed):
    assert native._publication_worker_receipt_projection(malformed, receipt("secret")) == {
        "guard_candidate": "worker_input_diagnostic_unavailable"}


def test_mismatched_original_receipts_are_unavailable():
    assert native._publication_worker_receipt_projection(receipt("one"), receipt("two")) == {
        "guard_candidate": "worker_input_diagnostic_unavailable"}


def test_builtin_types_required_without_object_conversion():
    class Untrusted:
        def __str__(self):
            raise AssertionError("must not stringify locals")
    class Receipt(dict):
        pass
    for value in (receipt(Untrusted()), Receipt(receipt("secret"))):
        assert native._publication_worker_receipt_projection(value, value) == {
            "guard_candidate": "worker_input_diagnostic_unavailable"}


@pytest.mark.parametrize("kind", ["missing", "foreign", "wrong_message", "wrong_phase", "wrong_status", "subclass", "cycle"])
def test_only_original_failure_frame_can_be_read(kind):
    error = RepoSandboxError("local worker was blocked before terminal readback",
                             phase="output_exported", terminal_status="unknown_external_effect")
    if kind == "wrong_message": error.args = ("secret",)
    if kind == "wrong_phase": error.phase = "other"
    if kind == "wrong_status": error.terminal_status = "other"
    if kind == "subclass":
        class Derived(RepoSandboxError): pass
        error = Derived(*error.args, phase=error.phase, terminal_status=error.terminal_status)
    if kind == "cycle": error.__cause__ = error
    if kind != "missing":
        # Same-named foreign function and matching locals cannot supply authority.
        def execute_job():
            raw_manifest = receipt(native._PUBLICATION_WORKER_GUARD_CANDIDATES[0][0])
            raw_readback = dict(raw_manifest)
            raise error
        try: execute_job()
        except RepoSandboxError: pass
    assert native._publication_worker_blocked_diagnostic(error) == {
        "guard_candidate": "worker_input_diagnostic_unavailable"}


@pytest.mark.parametrize("failure", ["unavailable", "helper", "print"])
def test_actual_runtime_test_reraises_original_failure(tmp_path, monkeypatch, capsys, failure):
    original = RepoSandboxError("local worker was blocked before terminal readback",
                                phase="output_exported", terminal_status="unknown_external_effect")
    def original_execute(self, job):
        raise original
    runner = object.__new__(native.LocalRepoRepairExecutor)
    repository = tmp_path / "fixture-repository"
    (repository / "tests").mkdir(parents=True)
    monkeypatch.setattr(native, "_PUBLICATION_ORIGINAL_EXECUTE", original_execute)
    monkeypatch.setattr(runtime, "LocalRepoRepairExecutor", lambda *args, **kwargs: runner)
    monkeypatch.setattr(runtime, "_repo", lambda path: (repository, "unused", ()))
    monkeypatch.setattr(runtime, "_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "replace", lambda *args, **kwargs: None)
    if failure == "helper":
        def unavailable(error): raise RuntimeError("private diagnostic failure")
        monkeypatch.setattr(native, "_publication_worker_blocked_diagnostic", unavailable)
    if failure == "print":
        def broken_print(*args, **kwargs): raise OSError("private output failure")
        monkeypatch.setattr("builtins.print", broken_print)
    with pytest.raises(RepoSandboxError) as caught:
        runtime.test_actual_bounded_profile_repair_has_full_runtime_and_closed_environment(tmp_path, monkeypatch)
    assert caught.value is original
    output = capsys.readouterr().out
    expected = '' if failure == "print" else 'PUBLICATION_WORKER_BLOCKED_DIAGNOSTIC={"bootstrap_candidate": "copied_runtime_bootstrap_unknown", "guard_candidate": "worker_input_diagnostic_unavailable"}'
    assert output.strip() == expected
    assert "private" not in output


def test_existing_loaded_library_and_link_helpers_still_reject_unrelated_error():
    for helper, digest in (
        (native._publication_link_escape_diagnostic, "15130f3bf0890613c24c1e469b3a2aff483280f84ea91f30ed3eb2ba2d8c6255"),
        (native._publication_loaded_library_diagnostic, "5feb4ade3b99342cef17ffb46bba3f50dd27c83c164642c59d30115651dc8f0c"),
    ):
        assert hashlib.sha256(inspect.getsource(helper).rstrip().encode()).hexdigest() == digest
    assert native._publication_link_escape_diagnostic(ValueError("secret")) is None
    assert native._publication_loaded_library_diagnostic(ValueError("secret")) is None


def test_original_sandbox_method_and_bootstrap_are_source_pinned():
    root = Path(__file__).resolve().parents[1]
    sandbox = (root / 'src/execution/repo_sandbox.py').read_bytes()
    assert hashlib.sha256(sandbox).hexdigest() == '09d9e8f3c2397564aec218bf7c3b958a5a6622a2ed1380779277cb0087b53a36'
    assert native._PUBLICATION_ORIGINAL_EXECUTE_CODE is native._PUBLICATION_ORIGINAL_EXECUTE.__code__
    raises = [node for node in ast.walk(ast.parse(sandbox)) if isinstance(node, ast.Raise) and node.lineno == 3478]
    assert len(raises) == 1
    assert raises[0].exc.args[0].value == 'local worker was blocked before terminal readback'
    source = (root / 'src/execution/repo_publication_runtime.py').read_bytes()
    assert hashlib.sha256(source).hexdigest() == 'bd9fba2b083b04b9a726a7153ce36584265199b3502b2d6b55ca3469c75df7d6'
    bootstrap = native._PUBLICATION_BOOTSTRAP
    assert hashlib.sha256(bootstrap.encode()).hexdigest() == 'c58bb934c5f88476cc249415869f96a979ffbfe1124b3c508daef2d9f2a5c879'
    rows = []
    for node in ast.walk(ast.parse(bootstrap)):
        if (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name) and node.exc.func.id == 'SystemExit'
            and len(node.exc.args) == 1 and isinstance(node.exc.args[0], ast.Constant)
            and type(node.exc.args[0].value) is str):
            rows.append(((node.exc.args[0].value + '\n').encode(), f'copied_runtime_bootstrap_candidate_{node.lineno}'))
    assert sorted(rows) == sorted(native._PUBLICATION_BOOTSTRAP_CANDIDATES)


@pytest.mark.parametrize('row', native._PUBLICATION_BOOTSTRAP_CANDIDATES)
def test_bootstrap_exact_literal_projection(row):
    stderr, code = row
    assert native._publication_bootstrap_projection((1, b'private stdout', stderr, False)) == code


@pytest.mark.parametrize('stderr', [b'private-path/secret', b'Traceback: private', b'x' * 513,
    b'actual copied libpython binding unavailable\n\n', b'prefix: actual copied libpython binding unavailable\n'])
def test_bootstrap_nonliteral_is_unknown(stderr):
    assert native._publication_bootstrap_projection((1, b'private', stderr, False)) == 'copied_runtime_bootstrap_unknown'


@pytest.mark.parametrize('result', [[], (1,), (True, b'', b'', False), (1, '', b'', False),
    (1, b'', '', False), (1, b'', b'', 0), (1, b'', b'', True)])
def test_bootstrap_invalid_or_timeout_is_unavailable(result):
    assert native._publication_bootstrap_projection(result) == 'copied_runtime_bootstrap_unavailable'


def test_bootstrap_success_emits_no_candidate():
    assert native._publication_bootstrap_projection((0, b'private', native._PUBLICATION_BOOTSTRAP_CANDIDATES[0][0], False)) is None
    assert not native._publication_bootstrap_call_matches(sys._getframe(), ['/git', '-I', '-S', '-B', '-c', native._PUBLICATION_BOOTSTRAP, '{}', '/private'])


def test_common_context_preserves_calls_results_callbacks_and_cleanup(monkeypatch, capsys):
    runner = object.__new__(native.LocalRepoRepairExecutor)
    original_method = native.LocalRepoRepairExecutor.execute_job
    original_fixed = native._publication_worker_module._run_fixed
    job, callback, result = object(), lambda: None, {'status': 'fixture-success'}
    argv = ['/usr/bin/git', 'status']
    fixed_result = (0, b'private', b'private', False)
    seen = []
    def fixed(*args, **kwargs):
        assert args[0] is argv and kwargs['before_spawn'] is callback
        seen.append('fixed'); return fixed_result
    def execute(self, *args, **kwargs):
        assert self is runner and args[0] is job and kwargs['before_dispatch'] is callback
        assert native._publication_worker_module._run_fixed(argv, before_spawn=callback) is fixed_result
        seen.append('execute'); return result
    monkeypatch.setattr(native, '_PUBLICATION_ORIGINAL_EXECUTE', execute)
    monkeypatch.setattr(native, '_PUBLICATION_ORIGINAL_RUN_FIXED', fixed)
    with native._publication_worker_diagnostic_context(monkeypatch):
        assert runner.execute_job(job, before_dispatch=callback) is result
        assert native._PUBLICATION_ORIGINAL_EXECUTE_CODE is original_method.__code__
    assert seen == ['fixed', 'execute']
    assert native.LocalRepoRepairExecutor.execute_job is original_method
    assert native._publication_worker_module._run_fixed is original_fixed
    assert capsys.readouterr().out == ''


def test_original_wrong_site_frame_is_unavailable_without_native_execution():
    from types import SimpleNamespace
    runner = object.__new__(native.LocalRepoRepairExecutor)
    runner.limits = SimpleNamespace(max_wall_seconds=60)
    with pytest.raises(RepoSandboxError) as caught:
        native._PUBLICATION_ORIGINAL_EXECUTE(runner, SimpleNamespace(deadline_seconds=0))
    frames = []
    current = caught.value.__traceback__
    while current:
        frames.append(current.tb_frame.f_code); current = current.tb_next
    assert native._PUBLICATION_ORIGINAL_EXECUTE_CODE in frames
    assert native._publication_worker_blocked_diagnostic(caught.value) == {'guard_candidate': 'worker_input_diagnostic_unavailable'}


def test_unrelated_fixed_command_exception_identity_and_cleanup(monkeypatch, capsys):
    original = ValueError('private command failure')
    fixed = native._publication_worker_module._run_fixed
    argv, callback = ['/usr/bin/git', 'status'], lambda: None
    def failure(*args, **kwargs):
        assert args[0] is argv and kwargs['before_spawn'] is callback
        raise original
    monkeypatch.setattr(native, '_PUBLICATION_ORIGINAL_RUN_FIXED', failure)
    with native._publication_worker_diagnostic_context(monkeypatch):
        with pytest.raises(ValueError) as caught:
            native._publication_worker_module._run_fixed(argv, before_spawn=callback)
    assert caught.value is original
    assert native._publication_worker_module._run_fixed is fixed
    assert capsys.readouterr().out == ''


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
async def test_shared_publication_context_cleans_up_exception_or_cancellation(monkeypatch, cancelled):
    import asyncio
    from tests import test_repo_publication_vertical as publication
    method, fixed = native.LocalRepoRepairExecutor.execute_job, native._publication_worker_module._run_fixed
    error = asyncio.CancelledError() if cancelled else ValueError('private')
    async def fail(*args):
        assert native.LocalRepoRepairExecutor.execute_job is not method
        assert native._publication_worker_module._run_fixed is not fixed
        raise error
    monkeypatch.setattr(publication, '_actual_repair', fail)
    with pytest.raises(type(error)) as caught:
        await publication.actual_repair(None, None, None, monkeypatch)
    assert caught.value is error
    assert native.LocalRepoRepairExecutor.execute_job is method
    assert native._publication_worker_module._run_fixed is fixed
