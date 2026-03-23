import asyncio
from unittest.mock import patch

import pytest

from config.settings import settings
from src.audit.repository import audit_repository
from src.tools.filesystem_tool import _safe_resolve, read_file, write_file
from src.tools.template_tool import fill_template
from src.tools.web_search_tool import web_search


@pytest.fixture(autouse=True)
def reset_site_policy():
    original_allowlist = settings.browser_site_allowlist
    original_blocklist = settings.browser_site_blocklist
    settings.browser_site_allowlist = ""
    settings.browser_site_blocklist = ""
    yield
    settings.browser_site_allowlist = original_allowlist
    settings.browser_site_blocklist = original_blocklist


class TestFilesystemTool:
    def test_safe_resolve_blocks_traversal(self):
        with pytest.raises(ValueError, match="Path traversal blocked"):
            _safe_resolve("../../etc/passwd")

    def test_read_file_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        result = read_file.forward("nonexistent.txt")
        assert "Error: File not found" in result

    def test_read_file_not_found_logs_runtime_audit(self, tmp_path, monkeypatch, async_db):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        result = read_file.forward("nonexistent.txt")
        assert "Error: File not found" in result

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_empty_result"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["tool_name"] == "filesystem:workspace"
        assert events[0]["details"]["operation"] == "read"
        assert events[0]["details"]["reason"] == "missing_file"

    def test_write_and_read_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        write_result = write_file.forward("test.txt", "Hello, Seraph!")
        assert "Successfully wrote" in write_result

        read_result = read_file.forward("test.txt")
        assert read_result == "Hello, Seraph!"

    def test_write_and_read_file_log_runtime_audit(self, tmp_path, monkeypatch, async_db):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        write_result = write_file.forward("test.txt", "Hello, Seraph!")
        read_result = read_file.forward("test.txt")

        assert "Successfully wrote" in write_result
        assert read_result == "Hello, Seraph!"

        async def _fetch():
            events = await audit_repository.list_events(limit=10)
            return [e for e in events if e["event_type"] == "integration_succeeded"]

        events = asyncio.run(_fetch())
        assert len(events) >= 2
        write_event = next(e for e in events if e["details"]["operation"] == "write")
        read_event = next(e for e in events if e["details"]["operation"] == "read")
        assert write_event["tool_name"] == "filesystem:workspace"
        assert write_event["details"]["length"] == len("Hello, Seraph!")
        assert read_event["details"]["length"] == len("Hello, Seraph!")

    def test_write_creates_subdirectories(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        write_file.forward("sub/dir/file.txt", "nested content")
        assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "nested content"

    def test_read_file_not_a_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        (tmp_path / "adir").mkdir()
        result = read_file.forward("adir")
        assert "Error: Not a file" in result

    def test_read_file_not_a_file_logs_runtime_audit(self, tmp_path, monkeypatch, async_db):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        (tmp_path / "adir").mkdir()
        result = read_file.forward("adir")
        assert "Error: Not a file" in result

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_failed"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["tool_name"] == "filesystem:workspace"
        assert events[0]["details"]["operation"] == "read"
        assert events[0]["details"]["reason"] == "not_a_file"

    def test_read_file_traversal_logs_blocked_runtime_audit(self, tmp_path, monkeypatch, async_db):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        with pytest.raises(ValueError, match="Path traversal blocked"):
            read_file.forward("../../etc/passwd")

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_blocked"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["tool_name"] == "filesystem:workspace"
        assert events[0]["details"]["operation"] == "read"

    def test_write_file_failure_logs_runtime_audit(self, tmp_path, monkeypatch, async_db):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        with patch("pathlib.Path.write_text", side_effect=PermissionError("denied")):
            result = write_file.forward("blocked.txt", "secret")

        assert "Failed to write file" in result

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_failed"]

        events = asyncio.run(_fetch())
        write_event = next(e for e in events if e["details"]["operation"] == "write")
        assert write_event["tool_name"] == "filesystem:workspace"
        assert write_event["details"]["error"] == "denied"

    def test_read_file_failure_logs_runtime_audit(self, tmp_path, monkeypatch, async_db):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        (tmp_path / "broken.txt").write_text("hello", encoding="utf-8")

        with patch("pathlib.Path.read_text", side_effect=OSError("boom")):
            result = read_file.forward("broken.txt")

        assert "Failed to read file" in result

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_failed"]

        events = asyncio.run(_fetch())
        read_event = next(e for e in events if e["details"]["operation"] == "read")
        assert read_event["tool_name"] == "filesystem:workspace"
        assert read_event["details"]["error"] == "boom"


class TestTemplateTool:
    def test_fill_template_basic(self):
        result = fill_template.forward("Hello, {name}!", {"name": "World"})
        assert result == "Hello, World!"

    def test_fill_template_multiple(self):
        result = fill_template.forward("{greeting}, {name}!", {"greeting": "Hi", "name": "Alice"})
        assert result == "Hi, Alice!"

    def test_fill_template_missing_variable(self):
        result = fill_template.forward("Hello, {name}!", {})
        assert "Error: Missing template variable" in result

    def test_fill_template_empty(self):
        result = fill_template.forward("No placeholders here", {})
        assert result == "No placeholders here"


class TestWebSearch:
    def test_web_search_returns_string(self, monkeypatch):
        """Test with mocked DuckDuckGo results."""
        class MockDDGS:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def text(self, query, max_results=5):
                return [
                    {"title": "Result 1", "href": "https://example.com", "body": "A test result"},
                ]

        with patch("src.tools.web_search_tool.DDGS", MockDDGS):
            result = web_search.forward("test query", max_results=1)
        assert "Result 1" in result
        assert "https://example.com" in result

    def test_web_search_no_results(self):
        class MockDDGS:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def text(self, query, max_results=5):
                return []

        with patch("src.tools.web_search_tool.DDGS", MockDDGS):
            result = web_search.forward("empty query")
        assert "No results found" in result

    def test_web_search_no_results_logs_runtime_audit(self, async_db):
        class MockDDGS:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def text(self, query, max_results=5):
                return []

        with patch("src.tools.web_search_tool.DDGS", MockDDGS):
            result = web_search.forward("empty query")

        assert "No results found" in result

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_empty_result"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["tool_name"] == "web_search:duckduckgo"
        assert events[0]["details"]["query_length"] == len("empty query")
        assert events[0]["details"]["result_count"] == 0

    def test_web_search_logs_runtime_audit(self, async_db):
        class MockDDGS:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def text(self, query, max_results=5):
                return [
                    {"title": "Result 1", "href": "https://example.com", "body": "A test result"},
                    {"title": "Result 2", "href": "https://example.org", "body": "Another test result"},
                ]

        with patch("src.tools.web_search_tool.DDGS", MockDDGS):
            result = web_search.forward("search reliability", max_results=2)

        assert "Result 1" in result

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_succeeded"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["tool_name"] == "web_search:duckduckgo"
        assert events[0]["details"]["query_length"] == len("search reliability")
        assert events[0]["details"]["result_count"] == 2

    def test_web_search_timeout_logs_runtime_audit(self, async_db):
        class MockDDGS:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def text(self, query, max_results=5):
                raise TimeoutError("Timed out")

        with patch("src.tools.web_search_tool.DDGS", MockDDGS):
            result = web_search.forward("slow query")

        assert "timed out" in result.lower()

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_timed_out"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["tool_name"] == "web_search:duckduckgo"
        assert events[0]["details"]["timeout_seconds"] == 15

    def test_web_search_filters_site_policy_blocked_results(self, async_db):
        class MockDDGS:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def text(self, query, max_results=5):
                return [
                    {"title": "Blocked", "href": "https://blocked.example.com", "body": "Should not surface"},
                    {"title": "Allowed", "href": "https://allowed.example.org", "body": "Safe result"},
                ]

        settings.browser_site_blocklist = "example.com"
        with patch("src.tools.web_search_tool.DDGS", MockDDGS):
            result = web_search.forward("search policy")

        assert "Allowed" in result
        assert "Blocked" not in result

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_succeeded"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["details"]["filtered_result_count"] == 1
        assert events[0]["details"]["blocked_hostnames"] == ["blocked.example.com"]

    def test_web_search_returns_no_allowed_results_when_allowlist_blocks_everything(self, async_db):
        class MockDDGS:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def text(self, query, max_results=5):
                return [
                    {"title": "Other", "href": "https://elsewhere.example.net", "body": "Nope"},
                ]

        settings.browser_site_allowlist = "allowed.example.org"
        with patch("src.tools.web_search_tool.DDGS", MockDDGS):
            result = web_search.forward("allowlisted search")

        assert "No allowed results found" in result

        async def _fetch():
            events = await audit_repository.list_events(limit=5)
            return [e for e in events if e["event_type"] == "integration_blocked"]

        events = asyncio.run(_fetch())
        assert events
        assert events[0]["tool_name"] == "web_search:duckduckgo"
        assert events[0]["details"]["filtered_result_count"] == 1
        assert events[0]["details"]["blocked_reasons"] == ["not_allowlisted"]

    def test_web_search_filters_missing_href_when_allowlist_active(self, async_db):
        class MockDDGS:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def text(self, query, max_results=5):
                return [
                    {"title": "Missing URL", "href": "", "body": "Should not bypass policy"},
                    {"title": "Allowed", "href": "https://allowed.example.org", "body": "Safe result"},
                ]

        settings.browser_site_allowlist = "allowed.example.org"
        with patch("src.tools.web_search_tool.DDGS", MockDDGS):
            result = web_search.forward("missing href")

        assert "Allowed" in result
        assert "Missing URL" not in result
