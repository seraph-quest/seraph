import subprocess
from unittest.mock import AsyncMock, patch

import pytest

from config.settings import REPO_ROOT, settings
from src.audit.repository import audit_repository
from src.operators.local_codex import (
    LocalCodexConfigurationError,
    _truncate_output,
    local_codex_command,
    local_codex_status,
    run_local_codex,
)


def test_local_codex_command_uses_local_exec_contract():
    with (
        patch.object(settings, "codex_local_command", "codex"),
        patch.object(settings, "codex_local_model", "gpt-5.5"),
        patch.object(settings, "codex_local_sandbox", "workspace-write"),
        patch.object(settings, "codex_local_approval_policy", "never"),
        patch.object(settings, "codex_local_timeout_seconds", 42),
    ):
        command = local_codex_command("fix the thing")

    assert command.argv == [
        "codex",
        "--ask-for-approval",
        "never",
        "exec",
        "-C",
        str(REPO_ROOT),
        "--sandbox",
        "workspace-write",
        "--model",
        "gpt-5.5",
        "fix the thing",
    ]
    assert command.cwd == REPO_ROOT
    assert command.timeout_seconds == 42


@pytest.mark.parametrize("approval_policy", ["bogus", "always"])
def test_local_codex_command_rejects_unknown_approval_policy(approval_policy):
    with pytest.raises(LocalCodexConfigurationError):
        local_codex_command("do it", approval_policy=approval_policy)


def test_local_codex_command_rejects_danger_full_access_sandbox():
    with pytest.raises(LocalCodexConfigurationError, match="read-only or workspace-write"):
        local_codex_command("do it", sandbox="danger-full-access")


def test_local_codex_command_rejects_shell_metacharacters():
    with patch.object(settings, "codex_local_command", "codex; rm -rf /"):
        with pytest.raises(LocalCodexConfigurationError):
            local_codex_command("do it")


def test_local_codex_command_rejects_cwd_outside_repo(tmp_path):
    with pytest.raises(LocalCodexConfigurationError, match="inside the Seraph repo"):
        local_codex_command("do it", cwd=str(tmp_path))


def test_local_codex_status_fails_closed_when_binary_missing():
    with (
        patch.object(settings, "codex_local_enabled", True),
        patch.object(settings, "codex_local_command", "missing-codex"),
        patch("src.operators.local_codex.shutil.which", return_value=None),
    ):
        status = local_codex_status()

    assert status["id"] == "codex-local"
    assert status["operator_kind"] == "local_command"
    assert status["ready"] is False
    assert status["unavailable_reason"] == "codex command was not found on PATH"
    assert status["requires_api_key"] is False


def test_local_codex_status_fails_closed_when_sandbox_config_is_unsafe():
    with (
        patch.object(settings, "codex_local_enabled", True),
        patch.object(settings, "codex_local_sandbox", "danger-full-access"),
    ):
        status = local_codex_status()

    assert status["ready"] is False
    assert status["unavailable_reason"] == "Local Codex sandbox mode must be read-only or workspace-write."


def test_local_codex_status_reports_version_without_api_key_requirement():
    completed = subprocess.CompletedProcess(["codex", "--version"], 0, stdout="codex 1.2.3\n", stderr="")
    with (
        patch.object(settings, "codex_local_enabled", True),
        patch.object(settings, "codex_local_command", "codex"),
        patch("src.operators.local_codex.shutil.which", return_value="/usr/local/bin/codex"),
        patch("src.operators.local_codex.subprocess.run", return_value=completed) as mock_run,
    ):
        status = local_codex_status()

    assert status["ready"] is True
    assert status["command_path"] == "/usr/local/bin/codex"
    assert status["version"] == "codex 1.2.3"
    assert status["requires_api_key"] is False
    assert mock_run.call_args.args[0] == ["codex", "--version"]
    assert "OPENAI_API_KEY" not in mock_run.call_args.kwargs["env"]


@pytest.mark.asyncio
async def test_run_local_codex_audits_without_prompt_or_raw_output(async_db):
    secret_value = "secret-value"
    completed = subprocess.CompletedProcess(
        ["codex", "exec"],
        7,
        stdout="done",
        stderr=f"token: {secret_value}",
    )
    with (
        patch.object(settings, "openai_api_key", secret_value),
        patch("src.operators.local_codex.shutil.which", return_value="/usr/local/bin/codex"),
        patch(
            "src.operators.local_codex.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess(["codex", "--version"], 0, stdout="codex 1.2.3\n", stderr=""),
                completed,
            ],
        ),
    ):
        result = await run_local_codex("private task prompt")

    assert result["ok"] is False
    assert result["exit_code"] == 7
    assert result["stdout"] == "done"
    assert result["stderr"] == "[redacted]"

    events = await audit_repository.list_events(limit=5)
    local_events = [event for event in events if event["tool_name"] == "codex-local"]
    assert [event["event_type"] for event in reversed(local_events)] == [
        "local_operator_started",
        "local_operator_completed",
    ]
    serialized = str(local_events)
    assert "private task prompt" not in serialized
    assert "[redacted]" not in serialized
    assert "prompt_sha256" in serialized
    assert "prompt_chars" in serialized


@pytest.mark.asyncio
async def test_run_local_codex_subprocess_env_strips_provider_api_keys(monkeypatch, async_db):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("LLM_API_KEY", "llm-secret")
    completed = subprocess.CompletedProcess(["codex", "exec"], 0, stdout="ok", stderr="")
    with (
        patch("src.operators.local_codex.shutil.which", return_value="/usr/local/bin/codex"),
        patch(
            "src.operators.local_codex.subprocess.run",
            side_effect=[
                subprocess.CompletedProcess(["codex", "--version"], 0, stdout="codex 1.2.3\n", stderr=""),
                completed,
            ],
        ) as mock_run,
    ):
        result = await run_local_codex("inspect local state")

    assert result["ok"] is True
    exec_env = mock_run.call_args_list[1].kwargs["env"]
    assert "OPENAI_API_KEY" not in exec_env
    assert "OPENROUTER_API_KEY" not in exec_env
    assert "ANTHROPIC_API_KEY" not in exec_env
    assert "LLM_API_KEY" not in exec_env
    assert exec_env["SERAPH_LOCAL_OPERATOR"] == "codex-local"


def test_local_codex_output_redaction_covers_env_configured_and_pattern_secrets(monkeypatch):
    monkeypatch.setenv("CUSTOM_TOKEN", "env-token-value")
    with patch.object(settings, "openai_api_key", "configured-secret"):
        output, truncated = _truncate_output(
            "api_key=inline-secret bearer abc.def configured-secret env-token-value"
        )

    assert truncated is False
    assert "inline-secret" not in output
    assert "abc.def" not in output
    assert "configured-secret" not in output
    assert "env-token-value" not in output
    assert output.count("[redacted]") >= 4


@pytest.mark.asyncio
async def test_runtime_status_exposes_local_operator_separately_from_provider_profiles(client):
    with patch("src.operators.local_codex.shutil.which", return_value=None):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert "local_operators" in payload
    assert any(item["id"] == "codex-local" for item in payload["local_operators"])
    assert all(item["id"] != "codex-local" for item in payload["provider_profiles"])


@pytest.mark.asyncio
async def test_operator_local_codex_status_endpoint_reports_blocked(client):
    with (
        patch.object(settings, "codex_local_command", "missing-codex"),
        patch("src.operators.local_codex.shutil.which", return_value=None),
    ):
        response = await client.get("/api/operator/local-codex/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["operator_kind"] == "local_command"
    assert payload["ready"] is False
    assert payload["unavailable_reason"] == "codex command was not found on PATH"


@pytest.mark.asyncio
async def test_operator_local_codex_exec_endpoint_fails_closed_when_binary_missing(client):
    with (
        patch.object(settings, "codex_local_command", "missing-codex"),
        patch("src.operators.local_codex.shutil.which", return_value=None),
        patch("src.operators.local_codex.audit_repository.log_event", AsyncMock()) as mock_audit,
    ):
        response = await client.post(
            "/api/operator/local-codex/exec",
            json={"prompt": "hello"},
        )

    assert response.status_code == 503
    payload = response.json()
    assert payload["detail"]["adapter"] == "codex-local"
    assert payload["detail"]["status"] == "blocked"
    assert mock_audit.await_count == 1


@pytest.mark.asyncio
async def test_operator_local_codex_exec_endpoint_does_not_accept_sandbox_or_approval_overrides(client):
    with patch(
        "src.api.operator.run_local_codex",
        AsyncMock(return_value={"ok": True, "operator_id": "codex-local"}),
    ) as mock_run:
        response = await client.post(
            "/api/operator/local-codex/exec",
            json={
                "prompt": "hello",
                "sandbox": "danger-full-access",
                "approval_policy": "on-request",
            },
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    _, kwargs = mock_run.call_args
    assert kwargs == {
        "cwd": None,
        "model": None,
        "timeout_seconds": None,
        "session_id": None,
    }
