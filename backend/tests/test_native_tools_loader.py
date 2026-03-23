"""Tests for bundled native tool discovery and legacy plugin aliases."""

import pytest

import src.native_tools.loader as loader_mod
import src.plugins.loader as legacy_loader_mod
from src.native_tools.loader import discover_tools, reload_tools


@pytest.fixture(autouse=True)
def clear_cache():
    """Reset the tool cache before each test."""
    loader_mod._discovered_tools = None
    yield
    loader_mod._discovered_tools = None


class TestDiscoverTools:
    def test_returns_non_empty(self):
        tools = discover_tools()
        assert len(tools) > 0

    def test_has_expected_tools(self):
        tools = discover_tools()
        names = {t.name for t in tools}
        for expected in ["web_search", "read_file", "write_file", "fill_template"]:
            assert expected in names, f"Expected tool '{expected}' not found"

    def test_no_duplicates(self):
        tools = discover_tools()
        names = [t.name for t in tools]
        assert len(names) == len(set(names))

    def test_caching(self):
        t1 = discover_tools()
        t2 = discover_tools()
        assert t1 is t2

    def test_reload(self):
        t1 = discover_tools()
        t2 = reload_tools()
        assert t1 is not t2
        assert len(t2) > 0

    def test_legacy_loader_alias_shares_module_state(self):
        assert legacy_loader_mod is loader_mod

        legacy_loader_mod._discovered_tools = ["sentinel"]
        assert loader_mod._discovered_tools == ["sentinel"]

        loader_mod._discovered_tools = None
        assert legacy_loader_mod._discovered_tools is None
