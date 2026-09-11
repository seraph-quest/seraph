"""Focused local proof for the adopted capability execution host."""

from __future__ import annotations

import json
import inspect
import multiprocessing
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.sql.dml import Update

from config.settings import settings
from src.approval.repository import approval_repository, fingerprint_tool_call
from src.approval.runtime import (
    _seal_capability_approval,
    reset_runtime_context,
    seal_capability_approval,
    set_runtime_context,
)
from src.extensions.capability_execution import (
    CapabilityExecutionError,
    CapabilityJournalError,
    CapabilityExecutionHost,
    CapabilityExecutionLimits,
    CapabilityExecutionRequest,
    _REGISTRY_TOKEN,
    build_capability_request,
    current_capability_execution_host,
)
from src.db.models import ApprovalRequest
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
        "session_id": "session:test",
    }
    payload.update(overrides)
    return CapabilityExecutionRequest(**payload)


@pytest.fixture(autouse=True)
def _authenticated_capability_runtime(monkeypatch):
    monkeypatch.setattr(settings, "capability_journal_secret", "test-capability-journal-secret")
    monkeypatch.setattr(settings, "capability_journal_secret_hash", "")
    principal = TrustPrincipal(
        principal_id="operator:test",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id="session:test",
    )
    tokens = set_runtime_context("session:test", "off", trust_principal=principal)
    try:
        yield
    finally:
        reset_runtime_context(tokens)


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
    principal = TrustPrincipal(
        principal_id="operator:multi-process",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id="session:multi-process",
    )
    tokens = set_runtime_context(
        "session:multi-process",
        "off",
        trust_principal=principal,
    )
    request = CapabilityExecutionRequest(
        owner_principal_id="operator:multi-process",
        capability_id="write_file",
        capability_version="native-v1",
        destination="workspace:multi-process.txt",
        arguments={"file_path": "multi-process.txt", "content": "one\n"},
        session_id="session:multi-process",
        idempotency_key="same-request-key",
    )
    barrier.wait(timeout=10)
    try:
        results.put(host.execute(request).state)
    except CapabilityExecutionError as exc:
        results.put(exc.reason_code)
    finally:
        reset_runtime_context(tokens)


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


def test_internal_raw_result_path_still_returns_bounded_data(tmp_path):
    host = _test_host(tmp_path / "journal.json", **{"test.echo": lambda _: {"payload": "x" * 4096}})
    request = _request(limits=CapabilityExecutionLimits(output_bytes=64))

    result, receipt = host._execute(
        request,
        lambda _: {"payload": "x" * 4096},
        return_raw_result=True,
    )

    assert result == receipt.result
    assert result["output_truncated"] is True
    assert result["output_bytes"] > 64
    assert len(json.dumps(result).encode("utf-8")) <= 256


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
    other_tokens = set_runtime_context(
        "session:test",
        "off",
        trust_principal=TrustPrincipal(
            principal_id="operator:other",
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
            session_id="session:test",
        ),
    )
    try:
        assert host.execute(other).state == "failed"
    finally:
        reset_runtime_context(other_tokens)


def test_journal_tamper_is_rejected(tmp_path):
    host = _test_host(tmp_path / "journal.json", **{"test.echo": lambda _: "ok"})
    host.execute(_request())
    payload = json.loads((tmp_path / "journal.json").read_text(encoding="utf-8"))
    payload["records"][0]["state"] = "uncertain"
    (tmp_path / "journal.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CapabilityJournalError, match="integrity"):
        host.journal_records()


def test_journal_mac_uses_server_secret_and_rejects_wrong_key(tmp_path, monkeypatch):
    journal = tmp_path / "journal.json"
    host = _test_host(journal, **{"test.echo": lambda _: "ok"})
    host.execute(_request())

    monkeypatch.setattr(settings, "capability_journal_secret", "wrong-server-secret")
    restarted = CapabilityExecutionHost(journal_path=journal)
    assert restarted.recovery_status()["status"] == "blocked"
    with pytest.raises(CapabilityJournalError, match="integrity"):
        restarted.journal_records()


def test_journal_mac_fails_closed_without_server_secret(tmp_path, monkeypatch):
    for name in (
        "capability_journal_secret",
        "capability_journal_secret_hash",
        "operator_auth_secret",
        "operator_auth_secret_hash",
    ):
        monkeypatch.setattr(settings, name, "")
    host = CapabilityExecutionHost(journal_path=tmp_path / "journal.json")
    assert host.recovery_status()["error_code"] == "journal_mac_key_unavailable"
    with pytest.raises(CapabilityExecutionError, match="journal_mac_key_unavailable"):
        host.execute(_request())


def test_public_capability_builder_rejects_raw_result_escape_hatch():
    with pytest.raises(CapabilityExecutionError, match="raw_result_internal_only"):
        build_capability_request(
            capability_id="run_command",
            arguments={"command": "pwd", "__seraph_raw_result": True},
        )


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
        def _execute_adopted_internal_result(self, request, *, _token):
            captured.append(request)
            return None, SimpleNamespace(
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
    assert "__seraph_raw_result" not in captured[0].arguments


def test_authority_and_approval_expiry_fail_closed(tmp_path):
    host = _test_host(tmp_path / "journal.json", **{"test.echo": lambda _: "ok"})
    with pytest.raises(CapabilityExecutionError, match="authority_expired"):
        host.execute(_request(expires_at=0))
    with pytest.raises(CapabilityExecutionError, match="approval_binding_missing"):
        host.execute(
            _request(
                approval_id="approval:forged",
                approval_digest="forged",
            )
        )


def test_public_request_cannot_supply_authority_booleans():
    with pytest.raises(TypeError, match="principal_authenticated"):
        CapabilityExecutionRequest(
            owner_principal_id="operator:test",
            capability_id="test.echo",
            capability_version="v1",
            destination="local://workspace/notes",
            principal_authenticated=True,  # type: ignore[call-arg]
        )


def test_approval_binding_must_be_repository_sealed(tmp_path):
    calls: list[dict] = []
    host = _test_host(
        tmp_path / "journal.json",
        **{"test.echo": lambda arguments: calls.append(dict(arguments)) or "ok"},
    )
    forged = {
        "approval_id": "approval:forged",
        "status": "consumed",
        "session_id": "session:test",
        "tool_name": "test.echo",
        "fingerprint": "fingerprint",
        "owner_operator_session_id": "session:test",
    }
    with pytest.raises(CapabilityExecutionError, match="approval_binding_missing"):
        host.execute(
            _request(
                approval_id="approval:forged",
                approval_digest="fingerprint",
                approval_binding=forged,
            )
        )

    with pytest.raises(RuntimeError, match="approval_seal_internal_only"):
        seal_capability_approval(forged)

    with pytest.raises(RuntimeError, match="approval_seal_proof_missing"):
        _seal_capability_approval(forged)
    assert calls == []


def test_forged_consumed_approval_model_cannot_mint_receipt_or_effect(tmp_path):
    calls: list[dict] = []
    host = _test_host(
        tmp_path / "journal.json",
        **{"test.echo": lambda arguments: calls.append(dict(arguments)) or "ok"},
    )
    arguments = {"message": "hello", "count": 1}
    fingerprint = fingerprint_tool_call("test.echo", arguments)
    forged = ApprovalRequest(
        id="approval:forged-model",
        status="consumed",
        session_id="session:test",
        tool_name="test.echo",
        fingerprint=fingerprint,
        details_json=None,
    )
    payload = {
        "approval_id": forged.id,
        "status": forged.status,
        "session_id": forged.session_id,
        "tool_name": forged.tool_name,
        "fingerprint": forged.fingerprint,
        "owner_operator_session_id": "session:test",
        "approval_resolved_at": "2026-09-11T00:00:00+00:00",
        "consumed_at": "2026-09-11T00:00:00+00:00",
    }

    with pytest.raises(RuntimeError, match="approval_seal_proof_missing"):
        _seal_capability_approval(payload, repository_proof=forged)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="approval_seal_proof_invalid"):
        _seal_capability_approval(payload, repository_proof="caller-forged-proof")
    assert calls == []
    assert not (tmp_path / "journal.json").exists()


@pytest.mark.asyncio
async def test_repository_consumed_approval_issues_one_use_host_binding(monkeypatch, tmp_path):
    calls: list[dict] = []
    host = _test_host(
        tmp_path / "journal.json",
        **{"test.echo": lambda arguments: calls.append(dict(arguments)) or "ok"},
    )
    arguments = {"message": "hello", "count": 1}
    fingerprint = fingerprint_tool_call("test.echo", arguments)
    request = ApprovalRequest(
        id="approval:repository",
        status="approved",
        session_id="session:test",
        tool_name="test.echo",
        fingerprint=fingerprint,
        details_json=None,
    )

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [request] if request.status == "approved" else []

    class _Db:
        async def execute(self, statement):
            if isinstance(statement, Update):
                if request.status != "approved":
                    return SimpleNamespace(rowcount=0)
                request.status = "consumed"
                return SimpleNamespace(rowcount=1)
            return _Result()

    db = _Db()

    @asynccontextmanager
    async def _get_session():
        yield db

    monkeypatch.setattr("src.approval.repository.get_session", _get_session)

    binding = await approval_repository.consume_approved(
        session_id="session:test",
        tool_name="test.echo",
        fingerprint=fingerprint,
    )
    assert isinstance(binding, dict)
    assert binding["approval_id"] == request.id
    assert binding["status"] == "consumed"
    assert binding["tool_name"] == "test.echo"
    assert await approval_repository.consume_approved(
        session_id="session:test",
        tool_name="test.echo",
        fingerprint=fingerprint,
    ) is False

    result = host.execute(
        _request(
            approval_id=request.id,
            approval_digest=fingerprint,
            approval_binding=binding,
            arguments=arguments,
        )
    )
    assert result.state == "succeeded"
    assert calls == [{"message": "hello", "count": 1}]

    with pytest.raises(CapabilityExecutionError, match="approval_binding_mismatch"):
        host.execute(
            _request(
                approval_id=request.id,
                approval_digest=fingerprint,
                approval_binding=binding,
                arguments={"message": "altered", "count": 1},
            )
        )
    assert calls == [{"message": "hello", "count": 1}]

    with pytest.raises(CapabilityExecutionError, match="approval_binding_mismatch"):
        host.execute(
            _request(
                approval_id=request.id,
                approval_digest="fingerprint",
                approval_binding=binding,
            )
        )
    assert calls == [{"message": "hello", "count": 1}]


@pytest.mark.asyncio
async def test_repository_selector_ignores_other_owner_and_rejects_duplicate_exact_rows(monkeypatch):
    common = {
        "session_id": "conversation-selector",
        "conversation_id": "conversation-selector",
        "tool_name": "selector-tool",
        "fingerprint": "selector-fingerprint",
        "status": "approved",
        "summary": "selector approval",
    }
    requests = [
        ApprovalRequest(
            id="selector-wrong-owner",
            **common,
            owner_principal_id="operator:other",
            operator_session_id="operator-session-other",
            details_json=json.dumps({
                "owner_principal_id": "operator:other",
                "approval_owner_operator_session_id": "operator-session-other",
                "conversation_id": "conversation-selector",
            }),
        ),
        ApprovalRequest(
            id="selector-right-owner",
            **common,
            owner_principal_id="operator:selector",
            operator_session_id="operator-session-selector",
            details_json=json.dumps({
                "owner_principal_id": "operator:selector",
                "approval_owner_operator_session_id": "operator-session-selector",
                "conversation_id": "conversation-selector",
            }),
        ),
    ]

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return list(requests)

    class _Db:
        async def execute(self, statement):
            return _Result()

    db = _Db()

    @asynccontextmanager
    async def _get_session():
        yield db

    monkeypatch.setattr("src.approval.repository.get_session", _get_session)

    assert await approval_repository.has_approved(
        session_id="conversation-selector",
        tool_name="selector-tool",
        fingerprint="selector-fingerprint",
        owner_operator_session_id="operator-session-selector",
        owner_principal_id="operator:selector",
    )

    requests.append(
        ApprovalRequest(
            id="selector-right-duplicate",
            **common,
            owner_principal_id="operator:selector",
            operator_session_id="operator-session-selector",
            details_json=json.dumps({
                "owner_principal_id": "operator:selector",
                "approval_owner_operator_session_id": "operator-session-selector",
                "conversation_id": "conversation-selector",
            }),
        )
    )
    assert not await approval_repository.has_approved(
        session_id="conversation-selector",
        tool_name="selector-tool",
        fingerprint="selector-fingerprint",
        owner_operator_session_id="operator-session-selector",
        owner_principal_id="operator:selector",
    )
    assert await approval_repository.consume_approved(
        session_id="conversation-selector",
        tool_name="selector-tool",
        fingerprint="selector-fingerprint",
        owner_operator_session_id="operator-session-selector",
        owner_principal_id="operator:selector",
    ) is False


def test_corrupt_journal_is_operator_visible_and_blocks_restart_execution(tmp_path):
    journal = tmp_path / "journal.json"
    journal.write_text("{not-json", encoding="utf-8")
    host = _test_host(journal, **{"test.echo": lambda _: "must-not-run"})
    status = host.recovery_status()
    assert status["status"] == "blocked"
    assert status["recovery_required"] is True
    assert status["error_code"] == "journal_recovery_required"
    with pytest.raises(CapabilityExecutionError, match="journal_recovery_required"):
        host.execute(_request())


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


def test_allowlisted_pytest_uses_active_interpreter_environment(tmp_path, monkeypatch):
    """The adopted test command must use the runtime's own pytest install."""
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    test_file = tmp_path / "test_active_runtime.py"
    test_file.write_text("def test_active_runtime():\n    assert True\n", encoding="utf-8")
    principal = TrustPrincipal(
        principal_id="operator:test",
        principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id="session:pytest-runtime",
    )
    tokens = set_runtime_context("session:pytest-runtime", "off", trust_principal=principal)
    try:
        wrapped = wrap_tools_for_approval([run_command])[0]
        result = wrapped(
            command="pytest",
            args_json=json.dumps(["-q", test_file.name]),
            timeout_seconds=30,
        )
    finally:
        reset_runtime_context(tokens)

    assert "1 passed" in result


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
