"""Tests for the Discover catalog API."""

import json
import os
import shutil

import pytest
import pytest_asyncio
from unittest.mock import patch, MagicMock

from src.skills.manager import SkillManager


# ── Fixtures ─────────────────────────────────────────────


@pytest.fixture
def catalog_data():
    """Sample catalog data for testing."""
    return {
        "skills": [
            {
                "name": "test-catalog-skill",
                "description": "A test skill",
                "category": "test",
                "requires_tools": ["web_search"],
                "bundled": True,
            },
        ],
        "mcp_servers": [
            {
                "name": "test-mcp",
                "description": "A test MCP server",
                "category": "test",
                "url": "http://test-mcp:9200/mcp",
                "bundled": True,
            },
        ],
    }


@pytest.fixture
def workspace_dir(tmp_path):
    """Create a temporary workspace directory."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "skills").mkdir()
    return str(ws)


@pytest.fixture
def bundled_skills_dir(tmp_path):
    """Create a temp directory with a bundled skill file."""
    d = tmp_path / "bundled"
    d.mkdir()
    (d / "test-catalog-skill.md").write_text(
        "---\n"
        "name: test-catalog-skill\n"
        "description: A test skill\n"
        "requires:\n"
        "  tools: [web_search]\n"
        "user_invocable: true\n"
        "---\n\n"
        "Do something useful."
    )
    return str(d)


# ── TestCatalogAPI ───────────────────────────────────────


class TestCatalogAPI:
    @pytest.mark.asyncio
    async def test_get_catalog_returns_items(self, client, catalog_data, workspace_dir):
        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog.mcp_manager") as mock_mcp:
            mock_settings.workspace_dir = workspace_dir
            mock_mcp._config = {}

            resp = await client.get("/api/catalog")
            assert resp.status_code == 200
            data = resp.json()
            assert "items" in data
            assert len(data["items"]) == 2

            skill_item = next(i for i in data["items"] if i["type"] == "skill")
            assert skill_item["name"] == "test-catalog-skill"
            assert skill_item["installed"] is False

            mcp_item = next(i for i in data["items"] if i["type"] == "mcp_server")
            assert mcp_item["name"] == "test-mcp"
            assert mcp_item["installed"] is False

    @pytest.mark.asyncio
    async def test_get_catalog_shows_installed_status(self, client, catalog_data, workspace_dir):
        # Create the skill file to simulate installation
        skills_dir = os.path.join(workspace_dir, "skills")
        with open(os.path.join(skills_dir, "test-catalog-skill.md"), "w") as f:
            f.write("---\nname: test-catalog-skill\ndescription: test\n---\nBody")

        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog.mcp_manager") as mock_mcp:
            mock_settings.workspace_dir = workspace_dir
            mock_mcp._config = {"test-mcp": {"url": "http://test:9200"}}

            resp = await client.get("/api/catalog")
            data = resp.json()
            skill_item = next(i for i in data["items"] if i["type"] == "skill")
            assert skill_item["installed"] is True
            mcp_item = next(i for i in data["items"] if i["type"] == "mcp_server")
            assert mcp_item["installed"] is True

    @pytest.mark.asyncio
    async def test_get_catalog_handles_missing_file(self, client):
        with patch("src.api.catalog._load_catalog", return_value={"skills": [], "mcp_servers": []}):
            resp = await client.get("/api/catalog")
            assert resp.status_code == 200
            assert resp.json()["items"] == []

    @pytest.mark.asyncio
    async def test_install_skill_success(
        self, client, catalog_data, workspace_dir, bundled_skills_dir
    ):
        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog._BUNDLED_SKILLS_DIR", bundled_skills_dir), \
             patch("src.api.catalog.skill_manager") as mock_skill_mgr:
            mock_settings.workspace_dir = workspace_dir

            resp = await client.post("/api/catalog/install/test-catalog-skill")
            assert resp.status_code == 201
            data = resp.json()
            assert data["status"] == "installed"
            assert data["type"] == "skill"

            # Verify the file was copied
            installed_path = os.path.join(workspace_dir, "skills", "test-catalog-skill.md")
            assert os.path.isfile(installed_path)
            mock_skill_mgr.reload.assert_called_once()

    @pytest.mark.asyncio
    async def test_install_mcp_success(self, client, catalog_data, workspace_dir):
        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog.mcp_manager") as mock_mcp:
            mock_settings.workspace_dir = workspace_dir
            mock_mcp._config = {}

            resp = await client.post("/api/catalog/install/test-mcp")
            assert resp.status_code == 201
            data = resp.json()
            assert data["status"] == "installed"
            assert data["type"] == "mcp_server"
            mock_mcp.add_server.assert_called_once_with(
                name="test-mcp",
                url="http://test-mcp:9200/mcp",
                description="A test MCP server",
                enabled=False,
                headers=None,
                auth_hint="",
            )

    @pytest.mark.asyncio
    async def test_install_mcp_passes_headers_and_auth_hint(self, client, workspace_dir):
        catalog_with_headers = {
            "skills": [],
            "mcp_servers": [
                {
                    "name": "toggl",
                    "description": "Toggl Track",
                    "category": "productivity",
                    "url": "http://toggl-mcp:9300/mcp",
                    "bundled": True,
                    "headers": {"Authorization": "Bearer ${TOGGL_API_KEY}"},
                    "auth_hint": "Get your token from toggl.com",
                },
            ],
        }
        with patch("src.api.catalog._load_catalog", return_value=catalog_with_headers), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog.mcp_manager") as mock_mcp:
            mock_settings.workspace_dir = workspace_dir
            mock_mcp._config = {}

            resp = await client.post("/api/catalog/install/toggl")
            assert resp.status_code == 201
            mock_mcp.add_server.assert_called_once_with(
                name="toggl",
                url="http://toggl-mcp:9300/mcp",
                description="Toggl Track",
                enabled=False,
                headers={"Authorization": "Bearer ${TOGGL_API_KEY}"},
                auth_hint="Get your token from toggl.com",
            )

    @pytest.mark.asyncio
    async def test_install_not_found(self, client, catalog_data):
        with patch("src.api.catalog._load_catalog", return_value=catalog_data):
            resp = await client.post("/api/catalog/install/nonexistent")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_install_already_installed_skill(
        self, client, catalog_data, workspace_dir
    ):
        # Pre-install the skill
        skills_dir = os.path.join(workspace_dir, "skills")
        with open(os.path.join(skills_dir, "test-catalog-skill.md"), "w") as f:
            f.write("---\nname: test-catalog-skill\ndescription: test\n---\nBody")

        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings:
            mock_settings.workspace_dir = workspace_dir

            resp = await client.post("/api/catalog/install/test-catalog-skill")
            assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_install_already_installed_mcp(self, client, catalog_data, workspace_dir):
        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog.mcp_manager") as mock_mcp:
            mock_settings.workspace_dir = workspace_dir
            mock_mcp._config = {"test-mcp": {"url": "http://test:9200"}}

            resp = await client.post("/api/catalog/install/test-mcp")
            assert resp.status_code == 409
