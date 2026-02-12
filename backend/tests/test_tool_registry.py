"""Tests for tool metadata registry (src/plugins/registry.py)."""

from src.plugins.registry import get_tool_metadata, get_all_metadata


class TestToolRegistry:
    def test_known_tool(self):
        meta = get_tool_metadata("web_search")
        assert meta is not None
        assert meta["description"] == "Search the web for information"

    def test_unknown_tool(self):
        assert get_tool_metadata("nonexistent_tool") is None

    def test_all_entries_have_required_fields(self):
        all_meta = get_all_metadata()
        required = {"description"}
        for name, meta in all_meta.items():
            missing = required - set(meta.keys())
            assert not missing, f"Tool '{name}' missing fields: {missing}"

    def test_returns_copy(self):
        m1 = get_all_metadata()
        m2 = get_all_metadata()
        assert m1 is not m2
