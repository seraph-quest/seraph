"""Tests for vector-store runtime audit coverage."""

from unittest.mock import MagicMock, patch

import pytest

from src.memory.embedder import EmbeddingMetadata
from src.memory import vector_store


_AUDIT_EVENTS: list[dict[str, object]] = []


def _record_audit(*, integration_type, name, outcome, details=None):
    _AUDIT_EVENTS.append(
        {
            "event_type": f"integration_{outcome}",
            "tool_name": f"{integration_type}:{name}",
            "details": details or {},
        }
    )


def _events(outcome: str) -> list[dict[str, object]]:
    return [event for event in _AUDIT_EVENTS if event["event_type"] == f"integration_{outcome}"]


@pytest.fixture(autouse=True)
def _capture_vector_store_audit_events():
    _AUDIT_EVENTS.clear()
    with patch.object(vector_store, "log_integration_event_sync", side_effect=_record_audit):
        yield
    _AUDIT_EVENTS.clear()


def test_add_memory_logs_runtime_audit_success():
    table = MagicMock()
    table.count_rows.return_value = 0
    metadata = EmbeddingMetadata(
        schema_version="seraph.memory.embedding.v1",
        provider="openrouter",
        model="openai/text-embedding-3-small",
        dimension=2,
    )

    with (
        patch("src.memory.vector_store._get_or_create_table", return_value=table),
        patch("src.memory.vector_store.embed", return_value=[0.1, 0.2]),
        patch("src.memory.vector_store.embedding_metadata", return_value=metadata),
    ):
        memory_id = vector_store.add_memory("remember this", category="fact", source_session_id="sess")

    assert memory_id

    events = _events("succeeded")
    assert events
    assert events[0]["tool_name"] == "vector_store:memories"
    assert events[0]["details"]["operation"] == "add"
    assert events[0]["details"]["category"] == "fact"


def test_openrouter_embedding_namespace_is_persisted_with_memory_row():
    table = MagicMock()
    table.count_rows.return_value = 0
    metadata = EmbeddingMetadata(
        schema_version="seraph.memory.embedding.v1",
        provider="openrouter",
        model="openai/text-embedding-3-small",
        dimension=2,
    )

    with (
        patch("src.memory.vector_store._get_or_create_table", return_value=table),
        patch("src.memory.vector_store.embed", return_value=[0.6, 0.8]),
        patch("src.memory.vector_store.embedding_metadata", return_value=metadata),
    ):
        memory_id = vector_store.add_memory("versioned memory")

    assert memory_id
    row = table.add.call_args.args[0][0]
    assert row["embedding_namespace"] == metadata.namespace
    assert row["embedding_model"] == metadata.model
    assert row["embedding_dimension"] == metadata.dimension


def test_embedding_model_or_dimension_change_selects_a_distinct_table():
    first = EmbeddingMetadata("seraph.memory.embedding.v1", "openrouter", "openai/a", 2)
    second = EmbeddingMetadata("seraph.memory.embedding.v1", "openrouter", "openai/b", 3)

    assert vector_store._table_name(first) != vector_store._table_name(second)
    assert vector_store._table_name(first).startswith("memories__memory-")
    assert vector_store._schema_for_embedding(first).field("vector").type.list_size == 2
    assert vector_store._schema_for_embedding(second).field("vector").type.list_size == 3


def test_active_vector_store_requires_embedding_namespace(monkeypatch):
    monkeypatch.setattr(vector_store.settings, "openrouter_provider_only", True)
    monkeypatch.setattr(vector_store, "embedding_metadata", lambda: None)
    with patch("src.memory.vector_store.embed", return_value=[0.1, 0.2]):
        assert vector_store.add_memory("cannot use legacy vector space") == ""


def test_search_logs_runtime_audit_empty_result():
    table = MagicMock()
    table.count_rows.return_value = 0
    metadata = EmbeddingMetadata(
        schema_version="seraph.memory.embedding.v1",
        provider="openrouter",
        model="openai/text-embedding-3-small",
        dimension=2,
    )

    with (
        patch("src.memory.vector_store._get_or_create_table", return_value=table),
        patch("src.memory.vector_store.embed", return_value=[0.1, 0.2]),
        patch("src.memory.vector_store.embedding_metadata", return_value=metadata),
    ):
        results = vector_store.search("missing memory", top_k=3)

    assert results == []

    events = _events("empty_result")
    assert events
    assert events[0]["tool_name"] == "vector_store:memories"
    assert events[0]["details"]["operation"] == "search"
    assert events[0]["details"]["reason"] == "empty_table"


def test_search_with_invalid_query_still_fails_open():
    table = MagicMock()
    table.count_rows.return_value = 0

    with patch("src.memory.vector_store._get_or_create_table", return_value=table):
        results = vector_store.search(None, top_k=3)

    assert results == []

    events = _events("empty_result")
    assert events
    assert events[0]["tool_name"] == "vector_store:memories"
    assert events[0]["details"]["query_length"] is None


def test_add_memory_logs_runtime_audit_failure():
    metadata = EmbeddingMetadata(
        schema_version="seraph.memory.embedding.v1",
        provider="openrouter",
        model="openai/text-embedding-3-small",
        dimension=2,
    )
    with (
        patch("src.memory.vector_store._get_or_create_table", side_effect=RuntimeError("db down")),
        patch("src.memory.vector_store.embed", return_value=[0.1, 0.2]),
        patch("src.memory.vector_store.embedding_metadata", return_value=metadata),
    ):
        memory_id = vector_store.add_memory("broken", category="fact", source_session_id="sess")

    assert memory_id == ""

    events = _events("failed")
    assert events
    assert events[0]["tool_name"] == "vector_store:memories"
    assert events[0]["details"]["operation"] == "add"
    assert events[0]["details"]["error"] == "db down"
