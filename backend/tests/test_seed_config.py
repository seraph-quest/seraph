"""Tests for MCP seed config logic in app lifespan."""

import json
import os
import shutil

import pytest


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

    def test_seed_default_skills(self, tmp_path):
        """Default skills should be seeded to empty workspace skills dir."""
        from src.app import _seed_default_skills

        defaults_dir = os.path.join(
            os.path.dirname(__file__), "../src/defaults"
        )
        skills_dir = str(tmp_path / "skills")
        os.makedirs(skills_dir)

        _seed_default_skills(defaults_dir, skills_dir)

        seeded = os.listdir(skills_dir)
        assert len(seeded) >= 8
        assert "daily-standup.md" in seeded
        assert "weekly-planner.md" in seeded

    def test_seed_does_not_overwrite_existing(self, tmp_path):
        """Seeding should not overwrite skills that already exist."""
        from src.app import _seed_default_skills

        defaults_dir = os.path.join(
            os.path.dirname(__file__), "../src/defaults"
        )
        skills_dir = str(tmp_path / "skills")
        os.makedirs(skills_dir)

        # Create a custom version of a skill
        custom_content = "---\nname: daily-standup\ndescription: My custom version\n---\nCustom."
        with open(os.path.join(skills_dir, "daily-standup.md"), "w") as f:
            f.write(custom_content)

        _seed_default_skills(defaults_dir, skills_dir)

        # Custom version should be preserved
        with open(os.path.join(skills_dir, "daily-standup.md")) as f:
            assert f.read() == custom_content
