from unittest.mock import MagicMock, patch

import pytest

from config.settings import settings
from src.agent.exceptions import ClarificationRequired
from src.agent.direct_chat import should_use_direct_local_chat
from src.approval.exceptions import ApprovalRequired
from src.audit.repository import audit_repository
from src.vault.repository import vault_repository


@pytest.mark.asyncio
class TestChatAPI:
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

    @patch("src.api.chat.should_use_direct_local_chat", return_value=True)
    @patch("src.api.chat.run_direct_local_chat", return_value="Hello. What should I call you?")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_chat_onboarding_hello_can_use_direct_local_path(
        self,
        mock_onboarding,
        mock_direct_chat,
        mock_should_use_direct,
        client,
    ):
        response = await client.post("/api/chat", json={"message": "Hello"})

        assert response.status_code == 200
        assert response.json()["response"] == "Hello. What should I call you?"
        mock_should_use_direct.assert_called_once()
        mock_direct_chat.assert_awaited_once()
        mock_onboarding.assert_not_called()

        events = await audit_repository.list_events(limit=10)
        assert any(
            event["event_type"] == "agent_run_succeeded"
            and event["tool_name"] == "onboarding_agent"
            and event["details"]["runtime"] == "direct-local-chat"
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

    async def test_chat_empty_message(self, client):
        response = await client.post("/api/chat", json={"message": ""})
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


@pytest.mark.asyncio
class TestHealthEndpoint:
    async def test_health(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
