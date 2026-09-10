import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from config.settings import settings
from src.agent.exceptions import ClarificationRequired
from src.agent.direct_chat import should_use_direct_local_chat
from src.approval.exceptions import ApprovalRequired
from src.api.chat import _bind_chat_principal
from src.audit.repository import audit_repository
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.vault.repository import vault_repository


@pytest.mark.asyncio
class TestChatAPI:
    @pytest.fixture(autouse=True)
    def _bind_test_operator_principal(self, monkeypatch):
        monkeypatch.setattr(
            "src.api.chat.get_current_trust_principal",
            lambda: TrustPrincipal(
                principal_id="operator:test",
                principal_type=PrincipalType.OPERATOR,
                grants=(AuthorityGrant.MODEL_INFERENCE,),
            ),
        )

    @pytest.fixture(autouse=True)
    def _disable_direct_local_chat_by_default(self, monkeypatch):
        monkeypatch.setattr(
            "src.api.chat.should_use_direct_local_chat",
            lambda *args, **kwargs: False,
        )

    @patch("src.memory.vector_store.search_formatted", return_value="")
    @patch("src.api.chat.build_agent")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_chat_success(self, mock_onboarding, mock_create_agent, mock_search, client):
        mock_agent = MagicMock()
        mock_agent.run.return_value = "Hello! I'm Seraph."
        mock_onboarding.return_value = mock_agent

        response = await client.post("/api/chat", json={"message": "Hello"})
        assert response.status_code == 200
        mock_onboarding.assert_called_once_with("Hello")
        data = response.json()
        assert data["response"] == "Hello! I'm Seraph."
        assert "session_id" in data

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "agent_run_succeeded"
            and event["tool_name"] == "onboarding_agent"
            and event["details"]["transport"] == "rest"
            for event in events
        )

    @patch("src.api.chat.run_direct_local_chat")
    @patch("src.memory.vector_store.search_formatted", return_value="")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_chat_onboarding_bare_domain_uses_agent_path(
        self,
        mock_onboarding,
        mock_search,
        mock_direct_chat,
        monkeypatch,
        client,
    ):
        monkeypatch.setattr("src.api.chat.should_use_direct_local_chat", should_use_direct_local_chat)
        mock_agent = MagicMock()
        mock_agent.run.return_value = "I reviewed your site and saved the relevant priorities."
        mock_onboarding.return_value = mock_agent

        with (
            patch.object(settings, "local_model", "openai/local-gemma"),
            patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
            patch.object(settings, "runtime_profile_preferences", "onboarding_agent=local-gemma-chat-thinking"),
        ):
            response = await client.post("/api/chat", json={"message": "natgurlain.com"})

        assert response.status_code == 200
        mock_onboarding.assert_called_once_with("natgurlain.com")
        mock_agent.run.assert_called_once_with("natgurlain.com")
        mock_direct_chat.assert_not_called()

    @patch("src.api.chat.run_direct_local_chat")
    @patch("src.memory.vector_store.search_formatted", return_value="")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_chat_onboarding_website_intent_uses_agent_path(
        self,
        mock_onboarding,
        mock_search,
        mock_direct_chat,
        monkeypatch,
        client,
    ):
        monkeypatch.setattr("src.api.chat.should_use_direct_local_chat", should_use_direct_local_chat)
        mock_agent = MagicMock()
        mock_agent.run.return_value = "Which exact page should I inspect?"
        mock_onboarding.return_value = mock_agent

        with (
            patch.object(settings, "local_model", "openai/local-gemma"),
            patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
            patch.object(settings, "runtime_profile_preferences", "onboarding_agent=local-gemma-chat-thinking"),
        ):
            response = await client.post(
                "/api/chat",
                json={"message": "Check the website and get the goals from it"},
            )

        assert response.status_code == 200
        mock_onboarding.assert_called_once_with("Check the website and get the goals from it")
        mock_agent.run.assert_called_once_with("Check the website and get the goals from it")
        mock_direct_chat.assert_not_called()

    @patch("src.api.chat.direct_local_chat_route_error", new_callable=AsyncMock)
    @patch("src.api.chat.should_use_direct_local_chat", return_value=True)
    @patch("src.api.chat.run_direct_local_chat", return_value="Hello. What should I call you?")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_chat_onboarding_hello_can_use_direct_openrouter_path(
        self,
        mock_onboarding,
        mock_direct_chat,
        mock_should_use_direct,
        mock_route_error,
        client,
    ):
        mock_route_error.return_value = None
        response = await client.post("/api/chat", json={"message": "Hello"})

        assert response.status_code == 200
        assert response.json()["response"] == "Hello. What should I call you?"
        mock_should_use_direct.assert_called_once()
        mock_route_error.assert_awaited_once()
        mock_direct_chat.assert_awaited_once()
        mock_onboarding.assert_not_called()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "agent_run_succeeded"
            and event["tool_name"] == "onboarding_agent"
            and event["details"]["runtime"] == "direct-openrouter-chat"
            for event in events
        )

    @patch("src.api.chat.direct_local_chat_route_error", new_callable=AsyncMock)
    @patch("src.api.chat.should_use_direct_local_chat", return_value=True)
    @patch("src.api.chat.run_direct_local_chat")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_chat_direct_openrouter_preflight_failure_returns_operator_error(
        self,
        mock_onboarding,
        mock_direct_chat,
        mock_should_use_direct,
        mock_route_error,
        client,
    ):
        mock_route_error.return_value = (
            "Local chat runtime is unreachable from the Seraph backend at http://192.168.1.26:8001. "
            "Health endpoint http://192.168.1.26:8001/health/chat reported chat proxy connect_error."
        )

        response = await client.post("/api/chat", json={"message": "Hello"})

        assert response.status_code == 503
        detail = response.json()["detail"]
        assert "Local chat runtime is unreachable" in detail
        assert "health/chat" in detail
        assert "LiteLLM" not in detail
        mock_should_use_direct.assert_called_once()
        mock_route_error.assert_awaited_once()
        mock_direct_chat.assert_not_called()
        mock_onboarding.assert_not_called()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "agent_run_failed"
            and event["tool_name"] == "onboarding_agent"
            and event["details"]["runtime"] == "direct-openrouter-chat"
            and event["details"]["failure_stage"] == "route_preflight"
            for event in events
        )

    @patch("src.memory.vector_store.search_formatted", return_value="")
    @patch("src.api.chat.build_agent")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_chat_with_session(self, mock_onboarding, mock_create_agent, mock_search, client):
        mock_agent = MagicMock()
        mock_agent.run.return_value = "Response 1"
        mock_onboarding.return_value = mock_agent

        r1 = await client.post("/api/chat", json={"message": "Hi"})
        session_id = r1.json()["session_id"]

        mock_agent.run.return_value = "Response 2"
        r2 = await client.post(
            "/api/chat", json={"message": "Follow up", "session_id": session_id}
        )
        assert r2.json()["session_id"] == session_id

    @patch("src.memory.vector_store.search_formatted", return_value="")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_chat_ingress_rejects_duplicate_before_model_dispatch(
        self,
        mock_onboarding,
        mock_search,
        client,
    ):
        mock_agent = MagicMock()
        mock_agent.run.return_value = "One response"
        mock_onboarding.return_value = mock_agent
        payload = {
            "message": "Retry-safe message",
            "message_id": "rest-retry-1",
            "idempotency_key": "rest-retry-1",
        }

        first = await client.post("/api/chat", json=payload)
        assert first.status_code == 200
        payload["session_id"] = first.json()["session_id"]
        second = await client.post("/api/chat", json=payload)

        assert second.status_code == 409
        duplicate_detail = second.json()["detail"]
        assert duplicate_detail["code"] == "chat_message_duplicate"
        assert duplicate_detail["message_id"]
        assert duplicate_detail["session_id"] == duplicate_detail["conversation_id"] == duplicate_detail["thread_id"] == payload["session_id"]
        assert duplicate_detail["continuity"]["message_id"] == duplicate_detail["message_id"]
        assert duplicate_detail["continuity"]["content_digest"]
        assert "Retry-safe message" not in json.dumps(duplicate_detail)
        mock_agent.run.assert_called_once_with("Retry-safe message")

        session_id = first.json()["session_id"]
        history = await client.get(f"/api/sessions/{session_id}/messages")
        assert history.status_code == 200
        user_messages = [item for item in history.json() if item["role"] == "user"]
        assert len(user_messages) == 1
        ingress = user_messages[0]["metadata"]["ingress"]
        assert ingress["schema_version"] == "seraph.chat.message.v1"
        assert ingress["message_id"] == duplicate_detail["message_id"]
        assert ingress["client_message_id"] == "rest-retry-1"
        assert ingress["idempotency_key"].startswith("sha256:")
        assert ingress["idempotency_key_digest"] != "rest-retry-1"
        assert ingress["principal_id"]
        assert ingress["device_id"].startswith("web-operator-session:")
        assert "Retry-safe message" not in json.dumps(ingress)

        events = await audit_repository.list_events(limit=20, session_id=session_id)
        statuses = {
            event["details"]["status"]
            for event in events
            if event["event_type"] == "chat_message_ingress"
        }
        assert {"accepted", "duplicate_rejected"}.issubset(statuses)

    @patch("src.memory.vector_store.search_formatted", return_value="")
    @patch("src.api.chat.create_onboarding_agent")
    @patch("src.api.chat.log_chat_ingress_event", new_callable=AsyncMock)
    async def test_chat_ingress_rejects_ambiguous_dual_identity_before_effects(
        self,
        mock_log_ingress,
        mock_onboarding,
        mock_search,
        client,
    ):
        response = await client.post(
            "/api/chat",
            json={
                "message": "Do not reserve this",
                "message_id": "client-message-1",
                "idempotency_key": "rest-retry-1",
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "chat_message_identity_conflict"
        mock_onboarding.assert_not_called()
        mock_log_ingress.assert_not_awaited()

    @patch("src.memory.vector_store.search_formatted", return_value="")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_chat_ingress_rejects_identity_conflict_and_unknown_session(
        self,
        mock_onboarding,
        mock_search,
        client,
    ):
        mock_agent = MagicMock()
        mock_agent.run.return_value = "One response"
        mock_onboarding.return_value = mock_agent
        first = await client.post(
            "/api/chat",
            json={"message": "Original", "idempotency_key": "same-key"},
        )
        assert first.status_code == 200
        conflict = await client.post(
            "/api/chat",
            json={
                "message": "Changed",
                "session_id": first.json()["session_id"],
                "idempotency_key": "same-key",
            },
        )
        unknown = await client.post(
            "/api/chat",
            json={
                "message": "Unknown session must fail",
                "session_id": "unknown-ingress-session",
                "idempotency_key": "unknown-key",
            },
        )

        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "chat_message_identity_conflict"
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == "chat_session_not_found"
        mock_agent.run.assert_called_once_with("Original")

    @pytest.mark.parametrize("message", ["", " \t "])
    async def test_chat_empty_message(self, message, client):
        response = await client.post("/api/chat", json={"message": message})
        assert response.status_code == 422

    @patch("src.memory.vector_store.search_formatted", return_value="")
    @patch("src.api.chat.build_agent")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_chat_agent_error(self, mock_onboarding, mock_create_agent, mock_search, client):
        mock_agent = MagicMock()
        mock_agent.run.side_effect = RuntimeError("LLM failure")
        mock_onboarding.return_value = mock_agent

        response = await client.post("/api/chat", json={"message": "Hello"})
        assert response.status_code == 500

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "agent_run_failed"
            and event["tool_name"] == "onboarding_agent"
            and event["details"]["transport"] == "rest"
            for event in events
        )

    @patch("src.memory.vector_store.search_formatted", return_value="")
    @patch("src.api.chat.build_agent")
    @patch("src.api.chat.create_onboarding_agent")
    @patch("src.api.chat.approval_repository.merge_details")
    async def test_chat_approval_required(self, mock_merge_details, mock_onboarding, mock_create_agent, mock_search, client):
        mock_agent = MagicMock()
        mock_agent.run.side_effect = ApprovalRequired(
            approval_id="approval-123",
            session_id="s1",
            tool_name="shell_execute",
            risk_level="high",
            summary="Calling tool: shell_execute({\"code\": \"[redacted]\"})",
        )
        mock_onboarding.return_value = mock_agent

        response = await client.post("/api/chat", json={"message": "Run this"})
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["type"] == "approval_required"
        assert detail["approval_id"] == "approval-123"
        assert detail["tool_name"] == "shell_execute"
        mock_merge_details.assert_awaited_once_with("approval-123", {"resume_message": "Run this"})

    @patch("src.memory.vector_store.search_formatted", return_value="")
    @patch("src.api.chat.build_agent")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_chat_clarification_required(self, mock_onboarding, mock_create_agent, mock_search, client):
        mock_agent = MagicMock()
        mock_agent.run.side_effect = ClarificationRequired(
            question="Which city should I check?",
            reason="Weather depends on location.",
            options=["Wroclaw", "Warsaw"],
        )
        mock_onboarding.return_value = mock_agent

        response = await client.post("/api/chat", json={"message": "What is the weather?"})
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["type"] == "clarification_required"
        assert isinstance(detail["session_id"], str)
        assert detail["question"] == "Which city should I check?"
        assert detail["reason"] == "Weather depends on location."
        assert detail["options"] == ["Wroclaw", "Warsaw"]
        assert "Which city should I check?" in detail["message"]

        session_id = detail["session_id"]
        history = await client.get(f"/api/sessions/{session_id}/messages")
        assert history.status_code == 200
        messages = history.json()
        assert messages[-1]["metadata"]["display_role"] == "clarification"
        assert messages[-1]["metadata"]["options"] == ["Wroclaw", "Warsaw"]

    @patch("src.memory.vector_store.search_formatted", return_value="")
    @patch("src.api.chat.build_agent")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_chat_redacts_secrets_in_response(self, mock_onboarding, mock_create_agent, mock_search, client):
        await vault_repository.store("github_token", "super-secret-token")
        mock_agent = MagicMock()
        mock_agent.run.return_value = "The token is super-secret-token"
        mock_onboarding.return_value = mock_agent

        response = await client.post("/api/chat", json={"message": "Hello"})
        assert response.status_code == 200
        assert response.json()["response"] == "The token is [redacted secret]"

    async def test_chat_principal_is_bound_to_server_session(self):
        principal = TrustPrincipal(
            principal_id="operator:test",
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
        )
        with patch("src.api.chat.get_current_trust_principal", return_value=principal):
            bound = _bind_chat_principal("session-from-server")

        assert bound.principal_id == principal.principal_id
        assert bound.authenticated is True
        assert bound.session_id == "session-from-server"
        assert bound.grants == (AuthorityGrant.MODEL_INFERENCE,)

    async def test_chat_principal_missing_or_forged_fails_closed(self):
        with patch("src.api.chat.get_current_trust_principal", return_value=None):
            with pytest.raises(Exception, match="authenticated operator"):
                _bind_chat_principal("session-from-server")

        forged = TrustPrincipal(
            principal_id="operator:forged",
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            session_id="different-session",
        )
        with patch("src.api.chat.get_current_trust_principal", return_value=forged):
            with pytest.raises(Exception, match="not bound to this session"):
                _bind_chat_principal("session-from-server")


@pytest.mark.asyncio
class TestHealthEndpoint:
    async def test_health(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
