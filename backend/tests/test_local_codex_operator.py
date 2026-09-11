import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from fastapi import WebSocketDisconnect

from config.settings import settings
from src.app import create_app
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.llm_runtime import FallbackLiteLLMModel, completion_with_fallback_sync
from src.operators.local_codex import (
    EXTERNAL_AGENT_RUNTIME_REMOVED,
    ExternalAgentRuntimeRemovedError,
    is_legacy_external_agent_model,
    run_local_codex,
)
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal

_REMOVED_PROFILE_CONFIG = json.dumps(
    {
        "profiles": {
            "removed-agent": {
                "provider_kind": "openai_compatible",
                "model": "codex-local/custom",
                "api_base": "https://models.example.test/v1",
                "keyless": True,
                "enabled": True,
            }
        }
    }
)


@pytest.mark.parametrize(
    "selection",
    ["codex", "codex-local", "local-codex", "codex-local/gpt-5.5"],
)
def test_legacy_external_agent_aliases_are_recognized(selection):
    assert is_legacy_external_agent_model(selection)


@pytest.mark.asyncio
async def test_removed_adapter_never_invokes_subprocess():
    with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
        with pytest.raises(ExternalAgentRuntimeRemovedError) as exc_info:
            await run_local_codex("do work")

    assert exc_info.value.code == EXTERNAL_AGENT_RUNTIME_REMOVED
    run.assert_not_called()
    popen.assert_not_called()


def test_completion_rejects_legacy_primary_without_transport_or_fallback():
    tokens = set_runtime_context(
        "external-agent-migration-test",
        "high_risk",
        trust_principal=TrustPrincipal(
            principal_id="operator:external-agent-migration-test",
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            session_id="external-agent-migration-test",
        ),
    )
    try:
        with (
            patch.object(settings, "default_model", "codex-local"),
            patch.object(settings, "runtime_profile_preferences", ""),
            patch.object(settings, "runtime_model_overrides", ""),
            patch.object(settings, "fallback_model", "openrouter/openai/gpt-4.1-mini"),
            patch.object(settings, "fallback_models", ""),
            patch("litellm.completion") as completion,
        ):
            with pytest.raises(ExternalAgentRuntimeRemovedError) as exc_info:
                completion_with_fallback_sync(
                    messages=[{"role": "user", "content": "hello"}],
                    temperature=0.2,
                    max_tokens=64,
                    runtime_path="session_title_generation",
                )
    finally:
        reset_runtime_context(tokens)

    assert exc_info.value.code == EXTERNAL_AGENT_RUNTIME_REMOVED
    completion.assert_not_called()


def test_completion_rejects_legacy_fallback_before_any_transport():
    tokens = set_runtime_context(
        "external-agent-fallback-test",
        "high_risk",
        trust_principal=TrustPrincipal(
            principal_id="operator:external-agent-fallback-test",
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            session_id="external-agent-fallback-test",
        ),
    )
    try:
        with (
            patch.object(settings, "default_model", "openai/gpt-4.1-mini"),
            patch.object(settings, "llm_api_base", "https://api.openai.com/v1"),
            patch.object(settings, "runtime_profile_preferences", ""),
            patch.object(settings, "runtime_model_overrides", ""),
            patch.object(settings, "fallback_model", "codex-local"),
            patch.object(settings, "fallback_models", ""),
            patch("src.llm_runtime._log_llm_runtime_event_sync"),
            patch("litellm.completion", side_effect=RuntimeError("primary unavailable")) as completion,
        ):
            with pytest.raises(ExternalAgentRuntimeRemovedError):
                completion_with_fallback_sync(
                    messages=[{"role": "user", "content": "hello"}],
                    temperature=0.2,
                    max_tokens=64,
                    runtime_path="session_title_generation",
                )
    finally:
        reset_runtime_context(tokens)

    completion.assert_not_called()


def test_agent_model_rejects_legacy_primary_without_transport():
    with patch("litellm.completion") as completion:
        with pytest.raises(ExternalAgentRuntimeRemovedError):
            FallbackLiteLLMModel(model_id="local-codex").generate(
                [{"role": "user", "content": "hello"}]
            )

    completion.assert_not_called()


def test_agent_model_blocks_unregistered_route_before_any_transport():
    with (
        patch.object(settings, "fallback_model", "CoDeX-LoCaL"),
        patch.object(settings, "fallback_models", ""),
        patch("litellm.completion") as completion,
    ):
        model = FallbackLiteLLMModel(model_id="openrouter/openai/gpt-4.1-mini")
        with pytest.raises(PermissionError, match="registered canonical runtime path"):
            model.generate([{"role": "user", "content": "hello"}])

    completion.assert_not_called()


def test_completion_blocks_unregistered_legacy_override_before_any_transport():
    with (
        patch.object(settings, "default_model", "openrouter/openai/gpt-4.1-mini"),
        patch.object(settings, "runtime_profile_preferences", ""),
        patch.object(settings, "runtime_model_overrides", ""),
        patch.object(settings, "runtime_fallback_overrides", "migration_test=local-codex"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("litellm.completion") as completion,
    ):
        with pytest.raises(PermissionError, match="registered canonical runtime path"):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "hello"}],
                temperature=0.2,
                max_tokens=64,
                runtime_path="migration_test",
            )

    completion.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_operator_endpoints_return_migration_response(client):
    for method, path, kwargs in (
        (client.get, "/api/operator/local-codex/status", {}),
        (client.post, "/api/operator/local-codex/exec", {"json": {"prompt": "hello"}}),
    ):
        response = await method(path, **kwargs)
        assert response.status_code == 410
        assert response.json()["detail"]["code"] == EXTERNAL_AGENT_RUNTIME_REMOVED


@pytest.mark.asyncio
async def test_legacy_operator_handlers_raise_structured_migration_tombstones():
    from fastapi import HTTPException
    from src.api.operator import (
        LocalCodexExecRequest,
        operator_local_codex_exec,
        operator_local_codex_status,
    )

    for call in (
        operator_local_codex_status(),
        operator_local_codex_exec(LocalCodexExecRequest(prompt="hello")),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await call
        assert exc_info.value.status_code == 410
        assert exc_info.value.detail["code"] == EXTERNAL_AGENT_RUNTIME_REMOVED


@pytest.mark.asyncio
async def test_runtime_status_rejects_legacy_default_without_advertising_operator(client):
    with patch.object(settings, "default_model", "codex-local"):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 410
    assert response.json()["detail"]["code"] == EXTERNAL_AGENT_RUNTIME_REMOVED


@pytest.mark.asyncio
async def test_runtime_status_has_no_external_operator_inventory(client):
    response = await client.get("/api/runtime/status")
    assert response.status_code == 200
    assert "local_operators" not in response.json()


@pytest.mark.asyncio
async def test_runtime_status_ignores_legacy_runtime_override_on_canonical_route():
    with patch.object(settings, "runtime_model_overrides", "chat_agent=CoDeX-LoCaL"):
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
            response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "openrouter"
    assert payload["active_profile"] == "openrouter"


@pytest.mark.asyncio
async def test_runtime_status_ignores_legacy_custom_profile_on_canonical_route():
    with (
        patch.object(settings, "llm_provider_profiles", _REMOVED_PROFILE_CONFIG),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=removed-agent"),
        patch.object(settings, "runtime_model_overrides", ""),
    ):
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
            response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "openrouter"
    assert payload["active_profile"] == "openrouter"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_override", "profile_preference", "profile_config"),
    [
        ("chat_agent=local-codex", "", ""),
        ("", "chat_agent=removed-agent", _REMOVED_PROFILE_CONFIG),
    ],
)
async def test_rest_chat_ignores_legacy_route_selection_and_uses_governed_path(
    runtime_override,
    profile_preference,
    profile_config,
):
    session = SimpleNamespace(id="legacy-session")
    profile = SimpleNamespace(onboarding_completed=False)
    mock_agent = MagicMock()
    mock_agent.run.return_value = ""
    with (
        patch.object(settings, "runtime_model_overrides", runtime_override),
        patch.object(settings, "runtime_profile_preferences", profile_preference),
        patch.object(settings, "llm_provider_profiles", profile_config),
        patch("src.api.chat.session_manager.get_or_create", new=AsyncMock(return_value=session)),
        patch("src.api.chat.session_manager.add_message", new=AsyncMock()),
        patch("src.api.chat.session_manager.count_messages", new=AsyncMock(return_value=0)),
        patch("src.api.chat.get_or_create_profile", new=AsyncMock(return_value=profile)),
        patch("src.api.chat.should_use_direct_local_chat", return_value=False),
        patch("src.api.chat.create_onboarding_agent", return_value=mock_agent),
        patch("src.api.chat.log_agent_run_event", new=AsyncMock()),
        patch("litellm.completion") as completion,
    ):
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
            response = await client.post("/api/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert response.json()["response"] == ""
    mock_agent.run.assert_called_once_with("hello")
    completion.assert_not_called()


def test_operator_runtime_payload_ignores_effective_legacy_override():
    from src.api.operator import _runtime_status_payload

    with patch.object(settings, "runtime_model_overrides", "chat_agent=codex"):
        payload = _runtime_status_payload()

    assert payload["provider"] == "openrouter"
    assert payload["active_profile"] == "openrouter"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runtime_override", "profile_preference", "profile_config"),
    [
        ("chat_agent=codex-local", "", ""),
        ("", "chat_agent=removed-agent", _REMOVED_PROFILE_CONFIG),
    ],
)
async def test_websocket_handler_ignores_legacy_route_selection_without_transport(
    runtime_override,
    profile_preference,
    profile_config,
):
    from src.api.ws import websocket_chat

    async def _fake_stream(*args, **kwargs):
        yield "OpenRouter ready."

    class FakeWebSocket:
        def __init__(self):
            self.sent: list[dict[str, object]] = []
            self.received = False

        async def accept(self):
            return None

        async def receive_text(self):
            if self.received:
                raise WebSocketDisconnect()
            self.received = True
            return json.dumps({"type": "message", "message": "hello"})

        async def send_text(self, payload: str):
            self.sent.append(json.loads(payload))

    websocket = FakeWebSocket()
    session = SimpleNamespace(id="legacy-ws-session")
    profile = SimpleNamespace(onboarding_completed=True)
    with (
        patch.object(settings, "runtime_model_overrides", runtime_override),
        patch.object(settings, "runtime_profile_preferences", profile_preference),
        patch.object(settings, "llm_provider_profiles", profile_config),
        patch("src.api.ws.get_or_create_profile", new=AsyncMock(return_value=profile)),
        patch("src.api.ws.session_manager.get_or_create", new=AsyncMock(return_value=session)),
        patch("src.api.ws.session_manager.add_message", new=AsyncMock()),
        patch("src.api.ws.ws_manager.connect"),
        patch("src.api.ws.ws_manager.disconnect"),
        patch("src.api.ws.should_use_direct_local_chat", return_value=True),
        patch("src.api.ws.direct_local_chat_route_error", new=AsyncMock(return_value=None)),
        patch("src.api.ws.stream_direct_local_chat", _fake_stream),
        patch(
            "src.api.ws.redact_secrets_for_streaming_snapshot",
            new=AsyncMock(side_effect=lambda text, emitted: (text, len(text))),
        ),
        patch("src.api.ws.redact_secrets_in_text", new=AsyncMock(side_effect=lambda text, **kwargs: text)),
        patch("litellm.completion") as completion,
    ):
        await websocket_chat(websocket)  # type: ignore[arg-type]

    final = next(item for item in websocket.sent if item["type"] == "final")
    assert final["content"] == "OpenRouter ready."
    completion.assert_not_called()
