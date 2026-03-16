"""Tests for browse_webpage tool runtime integration coverage."""

import asyncio
from unittest.mock import AsyncMock, patch

from src.audit.repository import audit_repository
from src.tools.browser_tool import browse_webpage


class _ImmediateFuture:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


class _ImmediateExecutor:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def submit(self, fn, *args, **kwargs):
        return _ImmediateFuture(fn(*args, **kwargs))


def test_browse_webpage_logs_blocked_runtime_audit(async_db):
    result = browse_webpage("http://localhost:3000/secret")

    assert "internal/private" in result.lower()

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "integration_blocked"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["tool_name"] == "browser:playwright"
    assert events[0]["details"]["hostname"] == "localhost"


def test_browse_webpage_logs_success_runtime_audit(async_db):
    with (
        patch("src.tools.browser_tool._browse", new=AsyncMock(return_value="hello from page")),
        patch("concurrent.futures.ThreadPoolExecutor", return_value=_ImmediateExecutor()),
    ):
        result = browse_webpage("https://example.com/docs")

    assert result == "hello from page"

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "integration_succeeded"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["tool_name"] == "browser:playwright"
    assert events[0]["details"]["hostname"] == "example.com"
    assert events[0]["details"]["action"] == "extract"
