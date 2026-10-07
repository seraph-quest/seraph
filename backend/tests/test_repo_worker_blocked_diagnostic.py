"""Closed failure-only diagnostics; no native worker or vendor execution."""
import ast
import hashlib
import inspect
import json
from pathlib import Path

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
    class Runner:
        def execute_job(self, job):
            raise original
    repository = tmp_path / "fixture-repository"
    (repository / "tests").mkdir(parents=True)
    monkeypatch.setattr(runtime, "LocalRepoRepairExecutor", lambda *args, **kwargs: Runner())
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
    expected = '' if failure == "print" else 'PUBLICATION_WORKER_BLOCKED_DIAGNOSTIC={"guard_candidate": "worker_input_diagnostic_unavailable"}'
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
