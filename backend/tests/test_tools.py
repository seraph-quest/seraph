import pytest

from src.tools.filesystem_tool import _safe_resolve, read_file, write_file
from src.tools.template_tool import fill_template
from src.tools.web_search_tool import web_search


class TestFilesystemTool:
    def test_safe_resolve_blocks_traversal(self):
        with pytest.raises(ValueError, match="Path traversal blocked"):
            _safe_resolve("../../etc/passwd")

    def test_read_file_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        result = read_file.forward("nonexistent.txt")
        assert "Error: File not found" in result

    def test_write_and_read_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        write_result = write_file.forward("test.txt", "Hello, Seraph!")
        assert "Successfully wrote" in write_result

        read_result = read_file.forward("test.txt")
        assert read_result == "Hello, Seraph!"

    def test_write_creates_subdirectories(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        write_file.forward("sub/dir/file.txt", "nested content")
        assert (tmp_path / "sub" / "dir" / "file.txt").read_text() == "nested content"

    def test_read_file_not_a_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.tools.filesystem_tool.settings.workspace_dir", str(tmp_path))
        (tmp_path / "adir").mkdir()
        result = read_file.forward("adir")
        assert "Error: Not a file" in result


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
        from unittest.mock import patch

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
        from unittest.mock import patch

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
