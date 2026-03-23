"""Tests for session HTTP endpoints (src/api/sessions.py)."""

import pytest

from src.agent.session import SessionManager


@pytest.fixture
def sm():
    return SessionManager()


class TestListSessions:
    async def test_empty(self, client):
        res = await client.get("/api/sessions")
        assert res.status_code == 200
        assert res.json() == []

    async def test_with_data(self, client, async_db):
        sm = SessionManager()
        await sm.get_or_create("s1")
        await sm.get_or_create("s2")
        res = await client.get("/api/sessions")
        assert res.status_code == 200
        assert len(res.json()) == 2


class TestSearchSessions:
    async def test_search_success(self, client, async_db):
        sm = SessionManager()
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Need a weather briefing")
        res = await client.get("/api/sessions/search", params={"q": "weather"})
        assert res.status_code == 200
        payload = res.json()
        assert len(payload) == 1
        assert payload[0]["session_id"] == "s1"

    async def test_search_excludes_current_session(self, client, async_db):
        sm = SessionManager()
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "Need a weather briefing")
        await sm.get_or_create("s2")
        await sm.add_message("s2", "user", "Need another weather briefing")

        res = await client.get(
            "/api/sessions/search",
            params={"q": "weather", "exclude_session_id": "s2"},
        )
        assert res.status_code == 200
        payload = res.json()
        assert [item["session_id"] for item in payload] == ["s1"]


class TestGetMessages:
    async def test_success(self, client, async_db):
        sm = SessionManager()
        await sm.get_or_create("s1")
        await sm.add_message("s1", "user", "hello")
        res = await client.get("/api/sessions/s1/messages")
        assert res.status_code == 200
        msgs = res.json()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello"

    async def test_not_found(self, client):
        res = await client.get("/api/sessions/nonexistent/messages")
        assert res.status_code == 404


class TestGetTodos:
    async def test_success(self, client, async_db):
        sm = SessionManager()
        await sm.get_or_create("s1")
        await sm.replace_todos("s1", [{"content": "Ship Wave 1", "completed": False}])
        res = await client.get("/api/sessions/s1/todos")
        assert res.status_code == 200
        todos = res.json()
        assert len(todos) == 1
        assert todos[0]["content"] == "Ship Wave 1"

    async def test_not_found(self, client):
        res = await client.get("/api/sessions/nonexistent/todos")
        assert res.status_code == 404


class TestUpdateTitle:
    async def test_success(self, client, async_db):
        sm = SessionManager()
        await sm.get_or_create("s1")
        res = await client.patch(
            "/api/sessions/s1",
            json={"title": "New Title"},
        )
        assert res.status_code == 200

    async def test_not_found(self, client):
        res = await client.patch(
            "/api/sessions/nonexistent",
            json={"title": "Nope"},
        )
        assert res.status_code == 404


class TestDeleteSession:
    async def test_success(self, client, async_db):
        sm = SessionManager()
        await sm.get_or_create("s1")
        res = await client.delete("/api/sessions/s1")
        assert res.status_code == 200

    async def test_not_found(self, client):
        res = await client.delete("/api/sessions/nonexistent")
        assert res.status_code == 404
