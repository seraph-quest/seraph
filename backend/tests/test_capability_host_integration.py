"""Focused local proof for the adopted capability execution host."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.settings import settings
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.extensions.capability_execution import (
    CapabilityExecutionError,
    CapabilityExecutionHost,
    CapabilityExecutionLimits,
    CapabilityExecutionRequest,
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


def test_execution_is_bounded_redacted_and_deduplicated(tmp_path):
    calls: list[dict] = []
    host = CapabilityExecutionHost(
        journal_path=tmp_path / "journal.json",
        handlers={"test.echo": lambda arguments: calls.append(dict(arguments)) or ("x" * 512)},
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
    host = CapabilityExecutionHost(
        journal_path=journal,
        handlers={"test.echo": lambda _arguments: (_ for _ in ()).throw(KeyboardInterrupt())},
    )
    request = _request()
    with pytest.raises(KeyboardInterrupt):
        host.execute(request)
    assert host.journal_records()[0]["state"] == "uncertain"

    restarted_calls: list[dict] = []
    restarted = CapabilityExecutionHost(
        journal_path=journal,
        handlers={"test.echo": lambda arguments: restarted_calls.append(dict(arguments)) or "replayed"},
    )
    recovered = restarted.recover()
    assert recovered[0].state == "uncertain"
    with pytest.raises(CapabilityExecutionError, match="effect_uncertain"):
        restarted.execute(request)
    assert restarted_calls == []


def test_failed_effect_is_durable_and_explicit_key_cannot_change_owner(tmp_path):
    journal = tmp_path / "journal.json"
    host = CapabilityExecutionHost(
        journal_path=journal,
        handlers={"test.echo": lambda _arguments: (_ for _ in ()).throw(RuntimeError("private detail"))},
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
    with pytest.raises(CapabilityExecutionError, match="duplicate_key_conflict"):
        host.execute(other)


def test_authority_and_approval_expiry_fail_closed(tmp_path):
    host = CapabilityExecutionHost(journal_path=tmp_path / "journal.json", handlers={"test.echo": lambda _: "ok"})
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
