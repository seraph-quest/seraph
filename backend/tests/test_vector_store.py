"""Tests for vector-store runtime audit coverage."""

import asyncio
from unittest.mock import MagicMock, patch

from src.audit.repository import audit_repository
from src.memory import vector_store


def test_add_memory_logs_runtime_audit_success(async_db):
    table = MagicMock()
    table.count_rows.return_value = 0

    with (
        patch("src.memory.vector_store._get_or_create_table", return_value=table),
        patch("src.memory.vector_store.embed", return_value=[0.1, 0.2]),
    ):
        memory_id = vector_store.add_memory("remember this", category="fact", source_session_id="sess")

    assert memory_id

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "integration_succeeded"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["tool_name"] == "vector_store:memories"
    assert events[0]["details"]["operation"] == "add"
    assert events[0]["details"]["category"] == "fact"


def test_search_logs_runtime_audit_empty_result(async_db):
    table = MagicMock()
    table.count_rows.return_value = 0

    with patch("src.memory.vector_store._get_or_create_table", return_value=table):
        results = vector_store.search("missing memory", top_k=3)

    assert results == []

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "integration_empty_result"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["tool_name"] == "vector_store:memories"
    assert events[0]["details"]["operation"] == "search"
    assert events[0]["details"]["reason"] == "empty_table"


def test_search_with_invalid_query_still_fails_open(async_db):
    table = MagicMock()
    table.count_rows.return_value = 0

    with patch("src.memory.vector_store._get_or_create_table", return_value=table):
        results = vector_store.search(None, top_k=3)

    assert results == []

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "integration_empty_result"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["tool_name"] == "vector_store:memories"
    assert events[0]["details"]["query_length"] is None


def test_add_memory_logs_runtime_audit_failure(async_db):
    with patch("src.memory.vector_store._get_or_create_table", side_effect=RuntimeError("db down")):
        memory_id = vector_store.add_memory("broken", category="fact", source_session_id="sess")

    assert memory_id == ""

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "integration_failed"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["tool_name"] == "vector_store:memories"
    assert events[0]["details"]["operation"] == "add"
    assert events[0]["details"]["error"] == "db down"
