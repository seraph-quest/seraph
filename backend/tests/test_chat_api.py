from unittest.mock import MagicMock, patch

import pytest


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


@pytest.mark.asyncio
class TestHealthEndpoint:
    async def test_health(self, client):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
