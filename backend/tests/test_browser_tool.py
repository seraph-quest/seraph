"""Tests for browse_webpage tool runtime integration coverage."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from config.settings import settings
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


@pytest.fixture(autouse=True)
def reset_browser_site_policy():
    original_allowlist = settings.browser_site_allowlist
    original_blocklist = settings.browser_site_blocklist
    settings.browser_site_allowlist = ""
    settings.browser_site_blocklist = ""
    yield
    settings.browser_site_allowlist = original_allowlist
    settings.browser_site_blocklist = original_blocklist


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


def test_browse_webpage_blocks_site_policy_domain(async_db):
    settings.browser_site_blocklist = "example.com"

    result = browse_webpage("https://docs.example.com/guide")

    assert "blocked by site policy" in result.lower()

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "integration_blocked"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["tool_name"] == "browser:playwright"
    assert events[0]["details"]["hostname"] == "docs.example.com"
    assert events[0]["details"]["site_policy_reason"] == "blocklisted_domain"
    assert events[0]["details"]["site_policy_rule"] == "example.com"


def test_browse_webpage_logs_timeout_runtime_audit(async_db):
    with (
        patch("src.tools.browser_tool._browse", new=AsyncMock(side_effect=TimeoutError("Timed out"))),
        patch("concurrent.futures.ThreadPoolExecutor", return_value=_ImmediateExecutor()),
    ):
        result = browse_webpage("https://example.com/slow")

    assert "timed out after" in result.lower()

    async def _fetch():
        events = await audit_repository.list_events(limit=5)
        return [e for e in events if e["event_type"] == "integration_timed_out"]

    events = asyncio.run(_fetch())
    assert events
    assert events[0]["tool_name"] == "browser:playwright"
    assert events[0]["details"]["hostname"] == "example.com"
    assert events[0]["details"]["timeout_seconds"] == 30
