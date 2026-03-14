from unittest.mock import MagicMock, patch

import pytest

from src.approval.exceptions import ApprovalRequired
from src.audit.repository import audit_repository
from src.vault.repository import vault_repository


@pytest.mark.asyncio
class TestChatAPI:
    @patch("src.memory.vector_store.search_formatted", return_value="")
    @patch("src.api.chat.build_agent")
    @patch("src.api.chat.create_onboarding_agent")
    async def test_chat_success(self, mock_onboarding, mock_create_agent, mock_search, client):
        mock_agent = MagicMock()
        mock_agent.run.return_value = "Hello! I'm Seraph."
        mock_onboarding.return_value = mock_agent

        response = await client.post("/api/chat", json={"message": "Hello"})
        assert response.status_code == 200
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
