"""Tests for embedding-model runtime audit coverage."""

import asyncio
import types
from unittest.mock import patch

import pytest

from config.settings import settings
from src.audit.repository import audit_repository
from src.memory import embedder


class _Vector:
    def __init__(self, payload):
        self._payload = payload

    def tolist(self):
        return self._payload


class _FakeSentenceTransformer:
    def __init__(self, model_name: str):
        self.model_name = model_name

    def encode(self, value, normalize_embeddings: bool = True):
        if value == "fail":
            raise RuntimeError("encode crashed")
        if isinstance(value, list):
            return _Vector([[0.1, 0.2] for _ in value])
        return _Vector([0.1, 0.2])


@pytest.fixture(autouse=True)
def _reset_embedder_state():
    embedder._reset_embedder_state()
    yield
    embedder._reset_embedder_state()


def test_embed_logs_model_load(async_db):
    fake_module = types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer)

    with patch.dict("sys.modules", {"sentence_transformers": fake_module}):
        vector = embedder.embed("hello")

    assert vector == [0.1, 0.2]

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "integration_loaded"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["tool_name"] == f"embedding_model:{settings.embedding_model}"
    assert events[0]["details"]["integration_type"] == "embedding_model"
    assert events[0]["details"]["name"] == settings.embedding_model


def test_embed_logs_encode_failure(async_db):
    fake_module = types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer)

    with patch.dict("sys.modules", {"sentence_transformers": fake_module}):
        with pytest.raises(RuntimeError, match="encode crashed"):
            embedder.embed("fail")

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "integration_failed"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["tool_name"] == f"embedding_model:{settings.embedding_model}"
    assert events[0]["details"]["stage"] == "encode"
    assert events[0]["details"]["batch_size"] == 1


def test_embed_logs_model_load_failure(async_db):
    class _BrokenSentenceTransformer:
        def __init__(self, model_name: str):
            raise RuntimeError("model missing")

    fake_module = types.SimpleNamespace(SentenceTransformer=_BrokenSentenceTransformer)

    with patch.dict("sys.modules", {"sentence_transformers": fake_module}):
        with pytest.raises(RuntimeError, match="model missing"):
            embedder.embed("hello")

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "integration_failed"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["tool_name"] == f"embedding_model:{settings.embedding_model}"
    assert events[0]["details"]["stage"] == "load"
    assert events[0]["details"]["error"] == "model missing"
