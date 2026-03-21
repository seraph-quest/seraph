"""Tests for startup config seeding and bundled manifest roots."""

import json
import os
import shutil

import pytest

from src.extensions.registry import bundled_manifest_root, default_manifest_roots_for_workspace


class TestSeedConfig:
    def test_first_run_creates_config_from_default(self, tmp_path):
        """When mcp-servers.json doesn't exist, it should be copied from default."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        default_config = tmp_path / "default.json"
        default_data = {
            "mcpServers": {
                "http-request": {
                    "url": "http://http-mcp:9200/mcp",
                    "enabled": False,
                    "description": "Make HTTP requests to external APIs",
                },
                "github": {
                    "url": "https://api.githubcopilot.com/mcp/",
                    "enabled": False,
                    "description": "GitHub integration",
                },
            }
        }
        default_config.write_text(json.dumps(default_data, indent=2))

        mcp_config = str(workspace / "mcp-servers.json")

        # Simulate the seed logic from app.py
        if not os.path.exists(mcp_config):
            if os.path.isfile(str(default_config)):
                os.makedirs(os.path.dirname(mcp_config), exist_ok=True)
                shutil.copy2(str(default_config), mcp_config)

        assert os.path.isfile(mcp_config)
        with open(mcp_config) as f:
            data = json.load(f)
        assert "http-request" in data["mcpServers"]
        assert "github" in data["mcpServers"]

    def test_existing_config_not_overwritten(self, tmp_path):
        """Existing mcp-servers.json should not be touched by seed logic."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        existing_data = {
            "mcpServers": {
                "my-server": {
                    "url": "http://custom:9999/mcp",
                    "enabled": True,
                    "description": "My custom server",
                }
            }
        }
        mcp_config = workspace / "mcp-servers.json"
        mcp_config.write_text(json.dumps(existing_data, indent=2))

        default_config = tmp_path / "default.json"
        default_config.write_text(json.dumps({"mcpServers": {"new-server": {}}}))

        # Simulate the seed logic — should NOT copy
        if not os.path.exists(str(mcp_config)):
            shutil.copy2(str(default_config), str(mcp_config))

        with open(str(mcp_config)) as f:
            data = json.load(f)
        assert "my-server" in data["mcpServers"]
        assert "new-server" not in data["mcpServers"]

    def test_default_entries_all_disabled(self):
        """All entries in the default config should have enabled: false."""
        default_path = os.path.join(
            os.path.dirname(__file__),
            "../src/defaults/mcp-servers.default.json",
        )
        with open(default_path) as f:
            data = json.load(f)

        for name, server in data["mcpServers"].items():
            assert server.get("enabled") is False, (
                f"Default MCP server '{name}' should be disabled"
            )

    def test_default_github_has_auth_headers(self):
        """GitHub entry in default config should have auth headers with env var template."""
        default_path = os.path.join(
            os.path.dirname(__file__),
            "../src/defaults/mcp-servers.default.json",
        )
        with open(default_path) as f:
            data = json.load(f)

        github = data["mcpServers"]["github"]
        assert "headers" in github
        assert "Authorization" in github["headers"]
        assert "${GITHUB_TOKEN}" in github["headers"]["Authorization"]

    def test_default_github_has_auth_hint(self):
        """GitHub entry in default config should have an auth_hint."""
        default_path = os.path.join(
            os.path.dirname(__file__),
            "../src/defaults/mcp-servers.default.json",
        )
        with open(default_path) as f:
            data = json.load(f)

        github = data["mcpServers"]["github"]
        assert "auth_hint" in github
        assert "github.com" in github["auth_hint"].lower()

    def test_default_manifest_roots_include_workspace_and_bundled_extensions(self, tmp_path):
        workspace = tmp_path / "workspace"

        roots = default_manifest_roots_for_workspace(str(workspace))

        assert roots == [str(workspace / "extensions"), bundled_manifest_root()]

    def test_bundled_extensions_root_exists(self):
        assert os.path.isdir(bundled_manifest_root())
