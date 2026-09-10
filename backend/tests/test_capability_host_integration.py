"""Focused local proof for the adopted capability execution host."""

from __future__ import annotations

import json
import inspect
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest

from config.settings import settings
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.extensions.capability_execution import (
    CapabilityExecutionError,
    CapabilityJournalError,
    CapabilityExecutionHost,
    CapabilityExecutionLimits,
    CapabilityExecutionRequest,
    _REGISTRY_TOKEN,
    current_capability_execution_host,
)
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.tools.approval import wrap_tools_for_approval
from src.tools.filesystem_tool import read_file, write_file
from src.tools.process_tools import run_command


def _request(**overrides) -> CapabilityExecutionRequest:
    payload = {
        "owner_principal_id": "operator:test",
        "capability_id": "test.echo",
        "capability_version": "v1",
        "destination": "local://workspace/notes",
        "arguments": {"message": "hello", "count": 1},
        "principal_authenticated": True,
        "authority_granted": True,
        "session_id": "session:test",
    }
    payload.update(overrides)
    return CapabilityExecutionRequest(**payload)


def _test_host(path: Path, **handlers):
    host = CapabilityExecutionHost(journal_path=path)
    for capability_id, handler in handlers.items():
        host._register_handler(capability_id, handler, _token=_REGISTRY_TOKEN)
    return host


def _native_write_worker(journal: str, workspace: str, barrier, results) -> None:
    from config.settings import settings as child_settings
    from src.extensions.capability_execution import CapabilityExecutionHost, CapabilityExecutionRequest

    child_settings.workspace_dir = workspace
    host = CapabilityExecutionHost(journal_path=journal)
    request = CapabilityExecutionRequest(
        owner_principal_id="operator:multi-process",
        capability_id="write_file",
        capability_version="native-v1",
        destination="workspace:multi-process.txt",
        arguments={"file_path": "multi-process.txt", "content": "one\n"},
        principal_authenticated=True,
        authority_granted=True,
        session_id="session:multi-process",
        idempotency_key="same-request-key",
    )
    barrier.wait(timeout=10)
    try:
        results.put(host.execute(request).state)
    except CapabilityExecutionError as exc:
        results.put(exc.reason_code)


def test_request_and_effect_identity_is_stable_and_owner_scoped(tmp_path):
    first = _request(arguments={"count": 1, "message": "hello"}, request_id="attempt-a")
    retry = _request(arguments={"message": "hello", "count": 1}, request_id="attempt-b")
    other_owner = _request(owner_principal_id="operator:other")

    assert first.request_digest == retry.request_digest
    assert first.effect_digest == retry.effect_digest
    assert first.duplicate_key == retry.duplicate_key
    assert first.duplicate_key != other_owner.duplicate_key
    assert first.journal_binding()["request_digest"] == first.request_digest
    assert first.journal_binding()["destination_digest"]


def test_public_host_has_no_arbitrary_callback_bypass(tmp_path):
    host = CapabilityExecutionHost(journal_path=tmp_path / "journal.json")
    request = _request()

    with pytest.raises(CapabilityExecutionError, match="handler_unregistered"):
        host.execute(request)
    with pytest.raises(TypeError):
        host.execute(request, lambda _arguments: "bypass")  # type: ignore[call-arg]
    with pytest.raises(CapabilityExecutionError, match="handler_injection_forbidden") as excinfo:
        CapabilityExecutionHost(
            journal_path=tmp_path / "injected.json",
            handlers={"test.echo": lambda _arguments: "bypass"},
        )
    assert excinfo.value.reason_code == "handler_injection_forbidden"


def test_execution_is_bounded_redacted_and_deduplicated(tmp_path):
    calls: list[dict] = []
    host = _test_host(
        tmp_path / "journal.json",
        **{"test.echo": lambda arguments: calls.append(dict(arguments)) or ("x" * 512)},
    )
    request = _request(
        arguments={"message": "x" * 512, "secret_token": "do-not-persist"},
        limits=CapabilityExecutionLimits(output_bytes=64),
    )

    first = host.execute(request)
    replay = host.execute(request)

    assert first.state == "succeeded"
    assert first.output_truncated is True
    assert isinstance(first.result, str)
    assert len(first.result.encode()) <= 64
    assert replay.result["replayed"] is True
    assert len(calls) == 1
    journal = (tmp_path / "journal.json").read_text(encoding="utf-8")
    assert "do-not-persist" not in journal
    assert "secret_token" not in journal
    assert (tmp_path / "journal.json").stat().st_mode & 0o077 == 0


def test_restart_marks_started_effect_uncertain_and_refuses_replay(tmp_path):
    journal = tmp_path / "journal.json"
    host = _test_host(
        journal,
        **{"test.echo": lambda _arguments: (_ for _ in ()).throw(KeyboardInterrupt())},
    )
    request = _request()
    with pytest.raises(KeyboardInterrupt):
        host.execute(request)
    assert host.journal_records()[0]["state"] == "uncertain"

    restarted_calls: list[dict] = []
    restarted = _test_host(
        journal,
        **{"test.echo": lambda arguments: restarted_calls.append(dict(arguments)) or "replayed"},
    )
    recovered = restarted.recover()
    assert recovered[0].state == "uncertain"
    with pytest.raises(CapabilityExecutionError, match="effect_uncertain"):
        restarted.execute(request)
    assert restarted_calls == []


def test_failed_effect_is_durable_and_explicit_key_cannot_change_owner(tmp_path):
    journal = tmp_path / "journal.json"
    host = _test_host(
        journal,
        **{"test.echo": lambda _arguments: (_ for _ in ()).throw(RuntimeError("private detail"))},
    )
    request = _request(idempotency_key="shared-key")
    result = host.execute(request)
    assert result.state == "failed"
    assert result.recoverable is True
    assert result.error_code == "RuntimeError"
    assert "private detail" not in journal.read_text(encoding="utf-8")
    with pytest.raises(CapabilityExecutionError, match="effect_failed"):
        host.execute(request)

    other = _request(owner_principal_id="operator:other", idempotency_key="shared-key")
    assert other.duplicate_key != request.duplicate_key
    assert host.execute(other).state == "failed"


def test_journal_tamper_is_rejected(tmp_path):
    host = _test_host(tmp_path / "journal.json", **{"test.echo": lambda _: "ok"})
    host.execute(_request())
    payload = json.loads((tmp_path / "journal.json").read_text(encoding="utf-8"))
    payload["records"][0]["state"] = "uncertain"
    (tmp_path / "journal.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CapabilityJournalError, match="integrity"):
        host.journal_records()


def test_network_destination_is_rejected_at_request_boundary():
    with pytest.raises(ValueError, match="network destinations"):
        _request(destination="https://example.invalid/connector")


def test_post_effect_receipt_failure_is_uncertain_and_denies_retry(tmp_path, monkeypatch):
    calls: list[dict] = []
    host = _test_host(
        tmp_path / "journal.json",
        **{"test.echo": lambda arguments: calls.append(dict(arguments)) or "ok"},
    )
    original = host._write_records_unlocked
    writes = 0

    def fail_completion(records):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("simulated receipt write failure")
        return original(records)

    monkeypatch.setattr(host, "_write_records_unlocked", fail_completion)
    with pytest.raises(CapabilityExecutionError, match="effect_uncertain"):
        host.execute(_request())
    assert host.journal_records()[0]["state"] == "uncertain"
    with pytest.raises(CapabilityExecutionError, match="effect_uncertain"):
        host.execute(_request())
    assert len(calls) == 1


def test_multi_process_journal_claim_is_atomic(tmp_path, monkeypatch):
    from config.settings import settings as parent_settings

    monkeypatch.setattr(parent_settings, "workspace_dir", str(tmp_path))
    ctx = multiprocessing.get_context("fork")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    journal = tmp_path / "journal.json"
    workers = [
        ctx.Process(target=_native_write_worker, args=(str(journal), str(tmp_path), barrier, results))
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    states = [results.get(timeout=15) for _ in workers]
    for worker in workers:
        worker.join(timeout=15)
    assert all(worker.exitcode == 0 for worker in workers)
    assert sorted(states) == ["effect_uncertain", "succeeded"]
    assert (tmp_path / "multi-process.txt").read_text(encoding="utf-8") == "one\n"
    assert len(json.loads(journal.read_text(encoding="utf-8"))["records"]) == 1


def test_native_swe_effect_paths_use_the_governed_host(monkeypatch):
    import src.workflows.native_software_engineering as native_swe

    source = inspect.getsource(native_swe)
    assert "process_runtime_manager.run_command" not in source
    assert "preview_workspace_patch(" not in source
    assert "apply_workspace_patch(" not in source

    captured = []

    class FakeHost:
        def execute(self, request):
            captured.append(request)
            return SimpleNamespace(
                state="succeeded",
                result={
                    "ok": True,
                    "blocked": False,
                    "cancelled": False,
                    "stdout": "",
                    "stderr": "",
                    "stdout_sha256": "",
                    "stderr_sha256": "",
                    "stdout_chars": 0,
                    "stderr_chars": 0,
                    "exit_code": 0,
                    "timed_out": False,
                    "cleanup_status": "stopped",
                    "remaining_descendants": 0,
                    "worker_root": None,
                    "display_command": "pwd",
                    "cwd": ".",
                    "timeout_seconds": 1,
                },
            )

    principal = TrustPrincipal(
        principal_id="service:native-software-engineering",
        principal_type=PrincipalType.SERVICE,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id="session:swe-host",
    )
    tokens = set_runtime_context("session:swe-host", "off", trust_principal=principal)
    monkeypatch.setattr(native_swe, "current_capability_execution_host", lambda: FakeHost())
    try:
        result = native_swe._process_result("pwd", [], ".", timeout_seconds=1)
    finally:
        reset_runtime_context(tokens)
    assert result["ok"] is True
    assert captured and captured[0].capability_id == "run_command"
    assert captured[0].arguments["__seraph_raw_result"] is True


def test_authority_and_approval_expiry_fail_closed(tmp_path):
    host = _test_host(tmp_path / "journal.json", **{"test.echo": lambda _: "ok"})
    with pytest.raises(CapabilityExecutionError, match="authority_expired"):
        host.execute(_request(expires_at=0))
    with pytest.raises(CapabilityExecutionError, match="approval_required"):
        host.execute(_request(requires_approval=True, approved=False))
    with pytest.raises(CapabilityExecutionError, match="approval_expired"):
        host.execute(_request(requires_approval=True, approved=True, approval_expires_at=0))


def test_run_command_uses_adopted_host_after_authority_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    script = tmp_path / "echo_local.py"
    script.write_text("print('local capability host')\n", encoding="utf-8")
    principal = TrustPrincipal(
        principal_id="operator:test",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id="session:host",
    )
    tokens = set_runtime_context("session:host", "off", trust_principal=principal)
    journal: Path
    try:
        wrapped = wrap_tools_for_approval([run_command])[0]
        result = wrapped(command="python3", args_json=json.dumps([script.name]), timeout_seconds=5)
        journal = current_capability_execution_host().journal_path
    finally:
        reset_runtime_context(tokens)

    assert result == "local capability host\n"
    assert any(
        item.get("binding", {}).get("capability_id") == "run_command"
        for item in json.loads(journal.read_text(encoding="utf-8"))["records"]
    )


def test_filesystem_builtins_use_the_same_adopted_host(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    principal = TrustPrincipal(
        principal_id="operator:test",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id="session:filesystem",
    )
    tokens = set_runtime_context("session:filesystem", "off", trust_principal=principal)
    journal: Path
    try:
        write_tool, read_tool = wrap_tools_for_approval([write_file, read_file])
        assert "Successfully wrote" in write_tool.forward(file_path="notes.txt", content="local")
        assert read_tool.forward(file_path="notes.txt") == "local"
        journal = current_capability_execution_host().journal_path
    finally:
        reset_runtime_context(tokens)

    records = json.loads(journal.read_text(encoding="utf-8"))["records"]
    capabilities = {item.get("binding", {}).get("capability_id") for item in records}
    assert {"write_file", "read_file"}.issubset(capabilities)


def test_adopted_process_output_is_bounded_before_return_and_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    script = tmp_path / "large_output.py"
    script.write_text("print('x' * 30000)\n", encoding="utf-8")
    principal = TrustPrincipal(
        principal_id="operator:output",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id="session:output",
    )
    tokens = set_runtime_context("session:output", "off", trust_principal=principal)
    journal: Path
    try:
        wrapped = wrap_tools_for_approval([run_command])[0]
        result = wrapped(command="python3", args_json=json.dumps([script.name]), timeout_seconds=5)
        journal = current_capability_execution_host().journal_path
    finally:
        reset_runtime_context(tokens)

    assert len(result.encode("utf-8")) <= 13_000
    records = json.loads(journal.read_text(encoding="utf-8"))["records"]
    run_record = next(item for item in records if item.get("binding", {}).get("capability_id") == "run_command")
    assert run_record["output_bytes"] <= 13_000
    assert "x" * 100 not in journal.read_text(encoding="utf-8")
