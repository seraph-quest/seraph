"""Tests for the Discover catalog API."""

import json
import os
from pathlib import Path
import shutil

import pytest
import pytest_asyncio
from unittest.mock import patch, MagicMock

from config.settings import settings
from src.skills.manager import SkillManager
from src.api.catalog import install_catalog_item_by_name
from src.extensions.registry import default_manifest_roots_for_workspace
from src.runbooks.manager import runbook_manager
from src.skills.manager import skill_manager
from src.starter_packs.manager import starter_pack_manager
from src.tools.mcp_manager import mcp_manager
from src.workflows.manager import workflow_manager


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
def catalog_extension_runtime(tmp_path):
    original_workspace_dir = settings.workspace_dir
    original_skill_manager = (
        list(skill_manager._skills),
        list(skill_manager._load_errors),
        skill_manager._skills_dir,
        list(skill_manager._manifest_roots),
        skill_manager._config_path,
        set(skill_manager._disabled),
        skill_manager._registry,
    )
    original_workflow_manager = (
        list(workflow_manager._workflows),
        workflow_manager._workflows_dir,
        list(workflow_manager._manifest_roots),
        workflow_manager._config_path,
        set(workflow_manager._disabled),
        workflow_manager._registry,
    )
    original_runbook_manager = (
        list(runbook_manager._runbooks),
        runbook_manager._runbooks_dir,
        list(runbook_manager._manifest_roots),
        runbook_manager._registry,
    )
    original_starter_pack_manager = (
        list(starter_pack_manager._packs),
        starter_pack_manager._legacy_path,
        list(starter_pack_manager._manifest_roots),
        starter_pack_manager._registry,
    )
    original_mcp_manager = (
        dict(mcp_manager._config),
        dict(mcp_manager._status),
        dict(mcp_manager._clients),
        dict(mcp_manager._tools),
        mcp_manager._config_path,
    )

    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    (workspace / "workflows").mkdir()
    (workspace / "runbooks").mkdir()
    manifest_roots = default_manifest_roots_for_workspace(str(workspace))

    settings.workspace_dir = str(workspace)
    skill_manager.init(str(workspace / "skills"), manifest_roots=manifest_roots)
    workflow_manager.init(str(workspace / "workflows"), manifest_roots=manifest_roots)
    runbook_manager.init(str(workspace / "runbooks"), manifest_roots=manifest_roots)
    starter_pack_manager.init(str(workspace / "starter-packs.json"), manifest_roots=manifest_roots)
    mcp_manager.disconnect_all()
    mcp_manager._config = {}
    mcp_manager._status = {}
    mcp_manager._tools = {}
    mcp_manager._config_path = str(workspace / "mcp-servers.json")

    yield str(workspace)

    settings.workspace_dir = original_workspace_dir
    (
        skill_manager._skills,
        skill_manager._load_errors,
        skill_manager._skills_dir,
        skill_manager._manifest_roots,
        skill_manager._config_path,
        skill_manager._disabled,
        skill_manager._registry,
    ) = original_skill_manager
    (
        workflow_manager._workflows,
        workflow_manager._workflows_dir,
        workflow_manager._manifest_roots,
        workflow_manager._config_path,
        workflow_manager._disabled,
        workflow_manager._registry,
    ) = original_workflow_manager
    (
        runbook_manager._runbooks,
        runbook_manager._runbooks_dir,
        runbook_manager._manifest_roots,
        runbook_manager._registry,
    ) = original_runbook_manager
    (
        starter_pack_manager._packs,
        starter_pack_manager._legacy_path,
        starter_pack_manager._manifest_roots,
        starter_pack_manager._registry,
    ) = original_starter_pack_manager
    mcp_manager.disconnect_all()
    (
        mcp_manager._config,
        mcp_manager._status,
        mcp_manager._clients,
        mcp_manager._tools,
        mcp_manager._config_path,
    ) = original_mcp_manager


@pytest.fixture
def bundled_skills_dir(tmp_path):
    """Create a temp bundled extension root with a packaged skill."""
    package_dir = tmp_path / "bundled" / "catalog-pack"
    skills_dir = package_dir / "skills"
    skills_dir.mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.catalog-pack\n"
        "version: 2026.3.21\n"
        "display_name: Catalog Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: bundled\n"
        "contributes:\n"
        "  skills:\n"
        "    - skills/test-catalog-skill.md\n"
        "permissions:\n"
        "  tools: [web_search]\n"
        "  network: false\n",
        encoding="utf-8",
    )
    (skills_dir / "test-catalog-skill.md").write_text(
        "---\n"
        "name: test-catalog-skill\n"
        "description: A test skill\n"
        "requires:\n"
        "  tools: [web_search]\n"
        "user_invocable: true\n"
        "---\n\n"
        "Do something useful.",
        encoding="utf-8",
    )
    return str(tmp_path / "bundled")


# ── TestCatalogAPI ───────────────────────────────────────


class TestCatalogAPI:
    @pytest.mark.asyncio
    async def test_get_catalog_includes_extension_packages(self, client):
        resp = await client.get("/api/catalog")

        assert resp.status_code == 200
        items = resp.json()["items"]
        extension_ids = {
            item["catalog_id"]
            for item in items
            if item["type"] == "extension_pack"
        }
        assert "seraph.hermes-session-memory" in extension_ids
        session_memory = next(item for item in items if item.get("catalog_id") == "seraph.hermes-session-memory")
        assert session_memory["name"] == "Hermes Session Memory"
        assert session_memory["bundled"] is True
        assert "skills" in session_memory["contribution_types"]
        assert session_memory["version_line"] == "2026.3"
        assert session_memory["compatibility"]["compatible"] is True
        assert session_memory["diagnostics_summary"]["issue_count"] == 0
        assert isinstance(session_memory["publisher"], dict)
        assert session_memory["publisher"]["name"]
        browserbase = next(item for item in items if item.get("catalog_id") == "seraph.hermes-browserbase")
        assert browserbase["name"] == "Hermes Browserbase"
        assert "browser_providers" in browserbase["contribution_types"]
        browser_ops = next(item for item in items if item.get("catalog_id") == "seraph.hermes-browser-ops")
        assert browser_ops["name"] == "Hermes Browser Ops"
        assert "toolset_presets" in browser_ops["contribution_types"]
        telegram = next(item for item in items if item.get("catalog_id") == "seraph.hermes-telegram-relay")
        assert telegram["name"] == "Hermes Telegram Relay"
        assert telegram["kind"] == "connector-pack"
        assert "messaging_connectors" in telegram["contribution_types"]
        discord = next(item for item in items if item.get("catalog_id") == "seraph.hermes-discord-relay")
        assert discord["name"] == "Hermes Discord Relay"
        slack = next(item for item in items if item.get("catalog_id") == "seraph.hermes-slack-relay")
        assert slack["name"] == "Hermes Slack Relay"
        speech = next(item for item in items if item.get("catalog_id") == "seraph.hermes-speech-ops")
        assert speech["name"] == "Hermes Speech Ops"
        assert "speech_profiles" in speech["contribution_types"]
        multimodal = next(item for item in items if item.get("catalog_id") == "seraph.hermes-multimodal-review")
        assert multimodal["name"] == "Hermes Multimodal Review"
        assert "prompt_packs" in multimodal["contribution_types"]
        assert "context_packs" in multimodal["contribution_types"]

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
        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog.mcp_manager") as mock_mcp, \
             patch("src.api.catalog._skill_loaded", return_value=True):
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
        captured_package: dict[str, str] = {}

        def capture_package(path: str) -> None:
            install_path = Path(path)
            captured_package["manifest"] = (install_path / "manifest.yaml").read_text(encoding="utf-8")
            captured_package["skill_payload"] = (install_path / "skills" / "test-catalog-skill.md").read_text(
                encoding="utf-8"
            )

        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog.bundled_manifest_root", return_value=bundled_skills_dir), \
             patch("src.api.catalog._skill_installed", return_value=False), \
             patch("src.api.catalog._skill_loaded", side_effect=[False, False, False, True]), \
             patch("src.api.catalog.install_extension_path", side_effect=capture_package):
            mock_settings.workspace_dir = workspace_dir

            resp = await client.post("/api/catalog/install/test-catalog-skill")
            assert resp.status_code == 201
            data = resp.json()
            assert data["status"] == "installed"
            assert data["type"] == "skill"
            assert data["extension_id"] == "seraph.catalog-skill-test-catalog-skill"
            assert "id: seraph.catalog-skill-test-catalog-skill" in captured_package["manifest"]
            assert "name: test-catalog-skill" in captured_package["skill_payload"]

    @pytest.mark.asyncio
    async def test_install_mcp_success(self, client, catalog_data, workspace_dir):
        captured_package: dict[str, str] = {}

        def capture_package(path: str) -> None:
            install_path = Path(path)
            captured_package["manifest"] = (install_path / "manifest.yaml").read_text(encoding="utf-8")
            captured_package["connector_payload"] = (install_path / "mcp" / "test-mcp.json").read_text(encoding="utf-8")

        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog.mcp_manager") as mock_mcp, \
             patch("src.api.catalog.install_extension_path", side_effect=capture_package):
            mock_settings.workspace_dir = workspace_dir
            mock_mcp._config = {}

            resp = await client.post("/api/catalog/install/test-mcp")
            assert resp.status_code == 409
            approval_detail = resp.json()["detail"]
            assert approval_detail["type"] == "approval_required"

            approve = await client.post(f"/api/approvals/{approval_detail['approval_id']}/approve")
            assert approve.status_code == 200

            resp = await client.post("/api/catalog/install/test-mcp")
            assert resp.status_code == 201
            data = resp.json()
            assert data["status"] == "installed"
            assert data["type"] == "mcp_server"
            assert data["extension_id"] == "seraph.catalog-mcp-test-mcp"
            connector_payload = json.loads(captured_package["connector_payload"])
            assert 'id: seraph.catalog-mcp-test-mcp' in captured_package["manifest"]
            assert connector_payload["name"] == "test-mcp"
            assert connector_payload["url"] == "http://test-mcp:9200/mcp"
            assert connector_payload["enabled"] is False

    @pytest.mark.asyncio
    async def test_install_mcp_passes_headers_and_auth_hint(self, client, workspace_dir):
        captured_package: dict[str, str] = {}

        def capture_package(path: str) -> None:
            install_path = Path(path)
            captured_package["connector_payload"] = (install_path / "mcp" / "toggl.json").read_text(encoding="utf-8")

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
             patch("src.api.catalog.mcp_manager") as mock_mcp, \
             patch("src.api.catalog.install_extension_path", side_effect=capture_package):
            mock_settings.workspace_dir = workspace_dir
            mock_mcp._config = {}

            resp = await client.post("/api/catalog/install/toggl")
            assert resp.status_code == 409
            approval_detail = resp.json()["detail"]
            assert approval_detail["type"] == "approval_required"

            approve = await client.post(f"/api/approvals/{approval_detail['approval_id']}/approve")
            assert approve.status_code == 200

            resp = await client.post("/api/catalog/install/toggl")
            assert resp.status_code == 201
            connector_payload = json.loads(captured_package["connector_payload"])
            assert connector_payload["headers"] == {"Authorization": "Bearer ${TOGGL_API_KEY}"}
            assert connector_payload["auth_hint"] == "Get your token from toggl.com"
            assert connector_payload["transport"] == "streamable-http"

    @pytest.mark.asyncio
    async def test_install_mcp_success_registers_runtime_connector(
        self, client, catalog_data, catalog_extension_runtime
    ):
        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch.object(mcp_manager, "connect") as connect_mock:
            resp = await client.post("/api/catalog/install/test-mcp")

        assert resp.status_code == 409
        approval_detail = resp.json()["detail"]
        assert approval_detail["type"] == "approval_required"

        approve = await client.post(f"/api/approvals/{approval_detail['approval_id']}/approve")
        assert approve.status_code == 200

        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch.object(mcp_manager, "connect") as connect_mock:
            resp = await client.post("/api/catalog/install/test-mcp")

        assert resp.status_code == 201
        data = resp.json()
        assert data["extension_id"] == "seraph.catalog-mcp-test-mcp"
        assert mcp_manager._config["test-mcp"]["url"] == "http://test-mcp:9200/mcp"
        assert mcp_manager._config["test-mcp"]["enabled"] is False
        assert mcp_manager._config["test-mcp"]["description"] == "A test MCP server"
        assert not connect_mock.called
        assert Path(catalog_extension_runtime, "extensions", "seraph-catalog-mcp-test-mcp", "manifest.yaml").is_file()

    @pytest.mark.asyncio
    async def test_install_mcp_returns_422_for_unsupported_transport(
        self, client, catalog_extension_runtime
    ):
        catalog_with_unsupported_transport = {
            "skills": [],
            "mcp_servers": [
                {
                    "name": "unsupported-mcp",
                    "description": "Bad transport",
                    "category": "test",
                    "url": "http://unsupported-mcp:9200/mcp",
                    "bundled": True,
                    "transport": "sse",
                },
            ],
        }

        with patch("src.api.catalog._load_catalog", return_value=catalog_with_unsupported_transport):
            resp = await client.post("/api/catalog/install/unsupported-mcp")

        assert resp.status_code == 422
        assert "streamable-http" in resp.json()["detail"]
        assert "unsupported-mcp" not in mcp_manager._config

    @pytest.mark.asyncio
    async def test_install_mcp_returns_409_when_extension_install_conflicts(
        self, client, catalog_data, workspace_dir
    ):
        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog.mcp_manager") as mock_mcp, \
             patch("src.api.catalog.require_catalog_install_approval", return_value=None), \
             patch("src.api.catalog.install_extension_path", side_effect=FileExistsError("duplicate")):
            mock_settings.workspace_dir = workspace_dir
            mock_mcp._config = {}

            resp = await client.post("/api/catalog/install/test-mcp")

        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_install_not_found(self, client, catalog_data):
        with patch("src.api.catalog._load_catalog", return_value=catalog_data):
            resp = await client.post("/api/catalog/install/nonexistent")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_install_catalog_extension_pack_success(self, client, catalog_extension_runtime):
        resp = await client.post("/api/catalog/install/Hermes Session Memory")

        assert resp.status_code == 201
        payload = resp.json()
        assert payload["status"] == "installed"
        assert payload["type"] == "extension_pack"
        assert payload["extension_id"] == "seraph.hermes-session-memory"
        assert Path(
            catalog_extension_runtime,
            "extensions",
            "seraph-hermes-session-memory",
            "manifest.yaml",
        ).is_file()

    @pytest.mark.asyncio
    async def test_get_catalog_marks_invalid_extension_pack_as_degraded(self, client, tmp_path):
        bad_root = tmp_path / "bad-catalog"
        package_dir = bad_root / "broken-pack"
        package_dir.mkdir(parents=True)
        (package_dir / "manifest.yaml").write_text(
            "id: seraph.broken-pack\n"
            "version: 2026.3.23\n"
            "display_name: Broken Pack\n"
            "kind: capability-pack\n"
            "compatibility:\n"
            "  seraph: \">=2026.4.10\"\n"
            "publisher:\n"
            "  name: Seraph\n"
            "trust: bundled\n"
            "contributes:\n"
            "  skills:\n"
            "    - skills/missing.md\n",
            encoding="utf-8",
        )

        with patch("src.api.catalog._CATALOG_EXTENSION_ROOT", str(bad_root)):
            resp = await client.get("/api/catalog")

        assert resp.status_code == 200
        broken = next(item for item in resp.json()["items"] if item.get("catalog_id") == "seraph.broken-pack")
        assert broken["status"] == "degraded"
        assert broken["doctor_ok"] is False
        assert broken["issues"]

    @pytest.mark.asyncio
    async def test_install_catalog_extension_pack_updates_existing_workspace_install(self, client, catalog_extension_runtime):
        first = await client.post("/api/catalog/install/seraph.hermes-session-memory")
        assert first.status_code == 201

        installed_manifest = Path(
            catalog_extension_runtime,
            "extensions",
            "seraph-hermes-session-memory",
            "manifest.yaml",
        )
        manifest_text = installed_manifest.read_text(encoding="utf-8").replace("version: 2026.3.23", "version: 2026.4.10")
        installed_manifest.write_text(manifest_text, encoding="utf-8")

        update = await client.post("/api/catalog/install/seraph.hermes-session-memory")

        assert update.status_code == 201
        payload = update.json()
        assert payload["status"] == "updated"
        assert payload["type"] == "extension_pack"
        assert "version: 2026.3.23" in installed_manifest.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_install_catalog_messaging_connector_pack_can_be_configured_and_enabled(self, client, catalog_extension_runtime):
        install = await client.post("/api/catalog/install/seraph.hermes-telegram-relay")

        assert install.status_code == 409
        approval_detail = install.json()["detail"]
        assert approval_detail["type"] == "approval_required"

        approve = await client.post(f"/api/approvals/{approval_detail['approval_id']}/approve")
        assert approve.status_code == 200

        install = await client.post("/api/catalog/install/seraph.hermes-telegram-relay")
        assert install.status_code == 201
        payload = install.json()
        assert payload["status"] == "installed"
        assert payload["extension_id"] == "seraph.hermes-telegram-relay"

        connectors = await client.get("/api/extensions/seraph.hermes-telegram-relay/connectors")
        assert connectors.status_code == 200
        connector = connectors.json()["connectors"][0]
        assert connector["type"] == "messaging_connectors"
        assert connector["status"] == "requires_config"

        configure = await client.post(
            "/api/extensions/seraph.hermes-telegram-relay/configure",
            json={
                "config": {
                    "messaging_connectors": {
                        "telegram": {
                            "bot_token": "secret",
                            "default_chat_id": "12345",
                        }
                    }
                }
            },
        )
        assert configure.status_code == 409
        configure_approval_id = configure.json()["detail"]["approval_id"]
        approve_configure = await client.post(f"/api/approvals/{configure_approval_id}/approve")
        assert approve_configure.status_code == 200

        configure = await client.post(
            "/api/extensions/seraph.hermes-telegram-relay/configure",
            json={
                "config": {
                    "messaging_connectors": {
                        "telegram": {
                            "bot_token": "secret",
                            "default_chat_id": "12345",
                        }
                    }
                }
            },
        )
        assert configure.status_code == 200
        configured_extension = configure.json()["extension"]
        configured_connector_config = configured_extension["config"]["messaging_connectors"]["telegram"]
        assert configured_connector_config["bot_token"] == "__SERAPH_STORED_SECRET__"
        assert configured_connector_config["default_chat_id"] == "12345"

        reconfigure = await client.post(
            "/api/extensions/seraph.hermes-telegram-relay/configure",
            json={"config": configured_extension["config"]},
        )
        assert reconfigure.status_code == 200

        enable_connector = await client.post(
            "/api/extensions/seraph.hermes-telegram-relay/connectors/enabled",
            json={"reference": "connectors/messaging/telegram.yaml", "enabled": True},
        )
        assert enable_connector.status_code == 200
        assert enable_connector.json()["connector"]["status"] == "planned"

        enable_extension = await client.post("/api/extensions/seraph.hermes-telegram-relay/enable")
        assert enable_extension.status_code == 200
        extension_payload = enable_extension.json()["extension"]
        messaging_connector = next(
            contribution
            for contribution in extension_payload["contributions"]
            if contribution["type"] == "messaging_connectors"
        )
        assert messaging_connector["enabled"] is True
        assert messaging_connector["status"] == "planned"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("catalog_id", "connector_name", "reference", "config"),
        [
            (
                "seraph.hermes-discord-relay",
                "discord",
                "connectors/messaging/discord.yaml",
                {"bot_token": "secret", "guild_id": "guild-1"},
            ),
            (
                "seraph.hermes-slack-relay",
                "slack",
                "connectors/messaging/slack.yaml",
                {"bot_token": "secret", "app_token": "app-secret"},
            ),
        ],
    )
    async def test_install_catalog_additional_messaging_connector_packs(
        self,
        client,
        catalog_extension_runtime,
        catalog_id,
        connector_name,
        reference,
        config,
    ):
        install = await client.post(f"/api/catalog/install/{catalog_id}")

        assert install.status_code == 409
        approval_detail = install.json()["detail"]
        assert approval_detail["type"] == "approval_required"

        approve = await client.post(f"/api/approvals/{approval_detail['approval_id']}/approve")
        assert approve.status_code == 200

        install = await client.post(f"/api/catalog/install/{catalog_id}")
        assert install.status_code == 201

        configure = await client.post(
            f"/api/extensions/{catalog_id}/configure",
            json={"config": {"messaging_connectors": {connector_name: config}}},
        )
        assert configure.status_code == 409
        configure_approval_id = configure.json()["detail"]["approval_id"]
        approve_configure = await client.post(f"/api/approvals/{configure_approval_id}/approve")
        assert approve_configure.status_code == 200

        configure = await client.post(
            f"/api/extensions/{catalog_id}/configure",
            json={"config": {"messaging_connectors": {connector_name: config}}},
        )
        assert configure.status_code == 200

        enable_connector = await client.post(
            f"/api/extensions/{catalog_id}/connectors/enabled",
            json={"reference": reference, "enabled": True},
        )
        assert enable_connector.status_code == 200
        assert enable_connector.json()["connector"]["status"] == "planned"

    @pytest.mark.asyncio
    async def test_install_catalog_browserbase_pack_surfaces_browser_provider_metadata(self, client, catalog_extension_runtime):
        install = await client.post("/api/catalog/install/seraph.hermes-browserbase")

        assert install.status_code == 409
        approval_detail = install.json()["detail"]
        assert approval_detail["type"] == "approval_required"

        approve = await client.post(f"/api/approvals/{approval_detail['approval_id']}/approve")
        assert approve.status_code == 200

        install = await client.post("/api/catalog/install/seraph.hermes-browserbase")
        assert install.status_code == 201
        assert install.json()["extension_id"] == "seraph.hermes-browserbase"

        extension = await client.get("/api/extensions/seraph.hermes-browserbase")
        assert extension.status_code == 200
        contributions = extension.json()["extension"]["contributions"]
        provider = next(item for item in contributions if item["type"] == "browser_providers")
        preset = next(item for item in contributions if item["type"] == "toolset_presets")
        assert provider["name"] == "browserbase"
        assert provider["provider_kind"] == "browserbase"
        assert provider["status"] == "requires_config"
        assert preset["name"] == "browserbase-ops"
        assert "browser_session" in preset["include_tools"]

    @pytest.mark.asyncio
    async def test_catalog_install_preflight_does_not_consume_approved_lifecycle_request(self, client, catalog_extension_runtime):
        from src.api.catalog import require_catalog_install_approval

        install = await client.post("/api/catalog/install/seraph.hermes-browserbase")

        assert install.status_code == 409
        approval_detail = install.json()["detail"]
        assert approval_detail["type"] == "approval_required"

        approve = await client.post(f"/api/approvals/{approval_detail['approval_id']}/approve")
        assert approve.status_code == 200

        await require_catalog_install_approval("seraph.hermes-browserbase", consume=False)

        install = await client.post("/api/catalog/install/seraph.hermes-browserbase")
        assert install.status_code == 201
        assert install.json()["extension_id"] == "seraph.hermes-browserbase"

    @pytest.mark.asyncio
    async def test_install_catalog_remote_cdp_pack_surfaces_staged_browser_provider_metadata(self, client, catalog_extension_runtime):
        install = await client.post("/api/catalog/install/seraph.openclaw-remote-cdp")

        assert install.status_code == 201
        assert install.json()["extension_id"] == "seraph.openclaw-remote-cdp"

        extension = await client.get("/api/extensions/seraph.openclaw-remote-cdp")
        assert extension.status_code == 200
        contributions = extension.json()["extension"]["contributions"]
        provider = next(item for item in contributions if item["type"] == "browser_providers")
        preset = next(item for item in contributions if item["type"] == "toolset_presets")
        assert provider["name"] == "remote-cdp"
        assert provider["provider_kind"] == "remote_cdp"
        assert provider["status"] == "requires_config"
        assert preset["name"] == "remote-cdp-ops"
        assert "browser_session" in preset["include_tools"]

    @pytest.mark.asyncio
    async def test_install_catalog_extension_relay_pack_surfaces_staged_browser_provider_metadata(self, client, catalog_extension_runtime):
        install = await client.post("/api/catalog/install/seraph.openclaw-extension-relay")

        assert install.status_code == 409
        approval_detail = install.json()["detail"]
        assert approval_detail["type"] == "approval_required"

        approve = await client.post(f"/api/approvals/{approval_detail['approval_id']}/approve")
        assert approve.status_code == 200

        install = await client.post("/api/catalog/install/seraph.openclaw-extension-relay")
        assert install.status_code == 201
        assert install.json()["extension_id"] == "seraph.openclaw-extension-relay"

        extension = await client.get("/api/extensions/seraph.openclaw-extension-relay")
        assert extension.status_code == 200
        contributions = extension.json()["extension"]["contributions"]
        provider = next(item for item in contributions if item["type"] == "browser_providers")
        preset = next(item for item in contributions if item["type"] == "toolset_presets")
        assert provider["name"] == "extension-relay"
        assert provider["provider_kind"] == "extension_relay"
        assert provider["status"] == "requires_config"
        assert preset["name"] == "extension-relay-ops"
        assert "browser_session" in preset["include_tools"]

    @pytest.mark.asyncio
    async def test_install_catalog_browser_ops_pack_surfaces_browser_skills_and_toolset(self, client, catalog_extension_runtime):
        install = await client.post("/api/catalog/install/seraph.hermes-browser-ops")

        assert install.status_code == 201
        assert install.json()["extension_id"] == "seraph.hermes-browser-ops"

        extension = await client.get("/api/extensions/seraph.hermes-browser-ops")
        assert extension.status_code == 200
        contributions = extension.json()["extension"]["contributions"]
        skill_names = {
            item["name"]
            for item in contributions
            if item["type"] == "skills"
        }
        preset = next(item for item in contributions if item["type"] == "toolset_presets")
        assert skill_names == {"browser-session-review", "browser-snapshot"}
        assert preset["name"] == "browser-session-ops"
        assert "browser_session" in preset["include_tools"]

    @pytest.mark.asyncio
    async def test_install_catalog_webhook_gateway_pack_surfaces_automation_trigger_metadata(self, client, catalog_extension_runtime):
        install = await client.post("/api/catalog/install/seraph.openclaw-webhook-gateway")

        assert install.status_code == 409
        approval_detail = install.json()["detail"]
        assert approval_detail["type"] == "approval_required"

        approve = await client.post(f"/api/approvals/{approval_detail['approval_id']}/approve")
        assert approve.status_code == 200

        install = await client.post("/api/catalog/install/seraph.openclaw-webhook-gateway")
        assert install.status_code == 201
        assert install.json()["extension_id"] == "seraph.openclaw-webhook-gateway"

        extension = await client.get("/api/extensions/seraph.openclaw-webhook-gateway")
        assert extension.status_code == 200
        contributions = extension.json()["extension"]["contributions"]
        trigger = next(item for item in contributions if item["type"] == "automation_triggers")
        assert trigger["name"] == "openclaw-webhook"
        assert trigger["trigger_type"] == "webhook"
        assert trigger["status"] == "requires_config"

    @pytest.mark.asyncio
    async def test_install_catalog_poll_and_pubsub_packs_surface_staged_trigger_metadata(self, client, catalog_extension_runtime):
        poll_install = await client.post("/api/catalog/install/seraph.openclaw-poll-watch")
        pubsub_install = await client.post("/api/catalog/install/seraph.openclaw-pubsub-relay")

        assert poll_install.status_code == 201
        assert pubsub_install.status_code == 201

        poll_extension = await client.get("/api/extensions/seraph.openclaw-poll-watch")
        pubsub_extension = await client.get("/api/extensions/seraph.openclaw-pubsub-relay")
        assert poll_extension.status_code == 200
        assert pubsub_extension.status_code == 200

        poll_trigger = next(
            item for item in poll_extension.json()["extension"]["contributions"] if item["type"] == "automation_triggers"
        )
        pubsub_trigger = next(
            item for item in pubsub_extension.json()["extension"]["contributions"] if item["type"] == "automation_triggers"
        )
        assert poll_trigger["trigger_type"] == "poll"
        assert poll_trigger["status"] == "disabled"
        assert pubsub_trigger["trigger_type"] == "pubsub"
        assert pubsub_trigger["status"] == "disabled"

    @pytest.mark.asyncio
    async def test_install_catalog_node_packs_surface_node_adapter_metadata(self, client, catalog_extension_runtime):
        companion_install = await client.post("/api/catalog/install/seraph.openclaw-companion-node")
        device_install = await client.post("/api/catalog/install/seraph.openclaw-device-bridge")

        assert companion_install.status_code == 201
        assert device_install.status_code == 201

        companion_extension = await client.get("/api/extensions/seraph.openclaw-companion-node")
        device_extension = await client.get("/api/extensions/seraph.openclaw-device-bridge")
        assert companion_extension.status_code == 200
        assert device_extension.status_code == 200

        companion_adapter = next(
            item for item in companion_extension.json()["extension"]["contributions"] if item["type"] == "node_adapters"
        )
        device_adapter = next(
            item for item in device_extension.json()["extension"]["contributions"] if item["type"] == "node_adapters"
        )
        assert companion_adapter["name"] == "openclaw-companion"
        assert companion_adapter["adapter_kind"] == "companion"
        assert companion_adapter["status"] == "requires_config"
        assert device_adapter["name"] == "openclaw-device"
        assert device_adapter["adapter_kind"] == "device"
        assert device_adapter["status"] == "requires_config"

    @pytest.mark.asyncio
    async def test_install_catalog_canvas_pack_surfaces_canvas_output_metadata(self, client, catalog_extension_runtime):
        install = await client.post("/api/catalog/install/seraph.openclaw-canvas-board")

        assert install.status_code == 201
        assert install.json()["extension_id"] == "seraph.openclaw-canvas-board"

        extension = await client.get("/api/extensions/seraph.openclaw-canvas-board")
        assert extension.status_code == 200
        contributions = extension.json()["extension"]["contributions"]
        canvas_output = next(item for item in contributions if item["type"] == "canvas_outputs")
        assert canvas_output["name"] == "guardian-board"
        assert canvas_output["surface_kind"] == "board"
        assert canvas_output["sections"] == ["Summary", "Steps", "Artifacts"]

    @pytest.mark.asyncio
    async def test_install_catalog_workflow_runtimes_pack_surfaces_runtime_and_workflow_metadata(self, client, catalog_extension_runtime):
        install = await client.post("/api/catalog/install/seraph.openclaw-workflow-runtimes")

        assert install.status_code == 201
        assert install.json()["extension_id"] == "seraph.openclaw-workflow-runtimes"

        extension = await client.get("/api/extensions/seraph.openclaw-workflow-runtimes")
        assert extension.status_code == 200
        contributions = extension.json()["extension"]["contributions"]
        runtime_profile = next(item for item in contributions if item["type"] == "workflow_runtimes" and item["name"] == "openprose")
        workflow = next(item for item in contributions if item["type"] == "workflows" and item["name"] == "openprose-brief")
        assert runtime_profile["engine_kind"] == "openprose"
        assert runtime_profile["structured_output"] is True
        assert workflow["runtime_profile"] == "openprose"
        assert workflow["output_surface"] == "guardian-board"

    @pytest.mark.asyncio
    async def test_install_catalog_speech_pack_surfaces_speech_profile_metadata(self, client, catalog_extension_runtime):
        install = await client.post("/api/catalog/install/seraph.hermes-speech-ops")

        assert install.status_code == 201
        payload = install.json()
        assert payload["extension_id"] == "seraph.hermes-speech-ops"

        extension = await client.get("/api/extensions/seraph.hermes-speech-ops")
        assert extension.status_code == 200
        contributions = extension.json()["extension"]["contributions"]
        speech_profile = next(item for item in contributions if item["type"] == "speech_profiles")
        prompt_pack = next(item for item in contributions if item["type"] == "prompt_packs")
        assert speech_profile["name"] == "openai-realtime-voice"
        assert speech_profile["supports_tts"] is True
        assert speech_profile["supports_stt"] is True
        assert prompt_pack["name"] == "voice-relay"
        assert prompt_pack["title"] == "Voice Relay"

    @pytest.mark.asyncio
    async def test_install_catalog_multimodal_pack_surfaces_provider_preset_metadata(self, client, catalog_extension_runtime):
        install = await client.post("/api/catalog/install/seraph.hermes-multimodal-review")

        assert install.status_code == 201
        payload = install.json()
        assert payload["extension_id"] == "seraph.hermes-multimodal-review"

        extension = await client.get("/api/extensions/seraph.hermes-multimodal-review")
        assert extension.status_code == 200
        contributions = extension.json()["extension"]["contributions"]
        context_pack = next(item for item in contributions if item["type"] == "context_packs")
        prompt_pack = next(item for item in contributions if item["type"] == "prompt_packs")
        provider_preset = next(item for item in contributions if item["type"] == "provider_presets")
        assert context_pack["name"] == "multimodal-review"
        assert "review" in context_pack["domains"]
        assert prompt_pack["name"] == "vision-triage"
        assert prompt_pack["title"] == "Vision Triage"
        assert provider_preset["name"] == "multimodal-review"
        assert provider_preset["default_model"] == "multimodal-primary"

    @pytest.mark.asyncio
    async def test_install_already_installed_skill(
        self, client, catalog_data, workspace_dir
    ):
        # Pre-install the skill
        skills_dir = os.path.join(workspace_dir, "skills")
        with open(os.path.join(skills_dir, "test-catalog-skill.md"), "w") as f:
            f.write("---\nname: test-catalog-skill\ndescription: test\n---\nBody")

        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog.skill_manager") as mock_skill_mgr:
            mock_settings.workspace_dir = workspace_dir
            mock_skill_mgr.get_skill.return_value = object()

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

    def test_install_catalog_skill_ignores_stale_legacy_workspace_file(
        self, catalog_data, workspace_dir, bundled_skills_dir
    ):
        skills_dir = os.path.join(workspace_dir, "skills")
        with open(os.path.join(skills_dir, "test-catalog-skill.md"), "w", encoding="utf-8") as handle:
            handle.write("not valid frontmatter")
        loaded_checks = {"count": 0}

        def skill_loaded_side_effect(_name: str) -> bool:
            loaded_checks["count"] += 1
            return loaded_checks["count"] >= 4

        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog.bundled_manifest_root", return_value=bundled_skills_dir), \
             patch("src.api.catalog._skill_loaded", side_effect=skill_loaded_side_effect), \
             patch("src.api.catalog.install_extension_path") as install_extension:
            mock_settings.workspace_dir = workspace_dir

            result = install_catalog_item_by_name("test-catalog-skill")

        assert result["ok"] is True
        assert result["status"] == "installed"
        install_extension.assert_called_once()

    def test_install_catalog_skill_is_already_installed_when_manifest_loaded(
        self, catalog_data, workspace_dir, bundled_skills_dir
    ):
        manifest_roots = [os.path.join(workspace_dir, "extensions"), bundled_skills_dir]
        manager = SkillManager()
        manager.init(os.path.join(workspace_dir, "skills"), manifest_roots=manifest_roots)

        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog.skill_manager", manager):
            mock_settings.workspace_dir = workspace_dir

            result = install_catalog_item_by_name("test-catalog-skill")

        assert result["ok"] is False
        assert result["status"] == "already_installed"
        assert not os.path.exists(os.path.join(workspace_dir, "skills", "test-catalog-skill.md"))

    @pytest.mark.asyncio
    async def test_install_skill_returns_422_when_bundled_file_cannot_load(
        self, client, catalog_data, workspace_dir, bundled_skills_dir
    ):
        with patch("src.api.catalog._load_catalog", return_value=catalog_data), \
             patch("src.api.catalog.settings") as mock_settings, \
             patch("src.api.catalog.bundled_manifest_root", return_value=bundled_skills_dir), \
             patch("src.api.catalog._skill_installed", return_value=False), \
             patch("src.api.catalog._skill_loaded", side_effect=[False, False, False, False]), \
             patch("src.api.catalog.install_extension_path"):
            mock_settings.workspace_dir = workspace_dir

            resp = await client.post("/api/catalog/install/test-catalog-skill")

        assert resp.status_code == 422
        assert "could not be loaded" in resp.json()["detail"]
