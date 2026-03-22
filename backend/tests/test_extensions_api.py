from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from config.settings import settings
from src.extensions.registry import default_manifest_roots_for_workspace
from src.runbooks.manager import runbook_manager
from src.skills.manager import skill_manager
from src.starter_packs.manager import starter_pack_manager
from src.tools.mcp_manager import mcp_manager
from src.workflows.manager import workflow_manager


def _write_installable_extension(root: Path) -> Path:
    package_dir = root / "installable-pack"
    (package_dir / "skills").mkdir(parents=True)
    (package_dir / "workflows").mkdir()
    (package_dir / "runbooks").mkdir()
    (package_dir / "starter-packs").mkdir()
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.test-installable\n"
        "version: 2026.3.21\n"
        "display_name: Test Installable\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  skills:\n"
        "    - skills/local-skill.md\n"
        "  workflows:\n"
        "    - workflows/local-workflow.md\n"
        "  runbooks:\n"
        "    - runbooks/local-runbook.yaml\n"
        "  starter_packs:\n"
        "    - starter-packs/local-pack.json\n"
        "permissions:\n"
        "  tools: [read_file]\n"
        "  network: false\n",
        encoding="utf-8",
    )
    (package_dir / "skills" / "local-skill.md").write_text(
        "---\n"
        "name: local-skill\n"
        "description: Local installable skill\n"
        "requires:\n"
        "  tools: []\n"
        "user_invocable: true\n"
        "---\n\n"
        "Use the local skill.\n",
        encoding="utf-8",
    )
    (package_dir / "workflows" / "local-workflow.md").write_text(
        "---\n"
        "name: local-workflow\n"
        "description: Local installable workflow\n"
        "requires:\n"
        "  tools: [read_file]\n"
        "steps:\n"
        "  - id: inspect\n"
        "    tool: read_file\n"
        "    arguments:\n"
        "      file_path: notes/test.md\n"
        "---\n\n"
        "Use the local workflow.\n",
        encoding="utf-8",
    )
    (package_dir / "runbooks" / "local-runbook.yaml").write_text(
        "id: runbook:local-runbook\n"
        "title: Local Runbook\n"
        "summary: Run the local workflow.\n"
        "workflow: local-workflow\n",
        encoding="utf-8",
    )
    (package_dir / "starter-packs" / "local-pack.json").write_text(
        "{\n"
        '  "name": "local-pack",\n'
        '  "label": "Local Pack",\n'
        '  "description": "Enable local installable capability.",\n'
        '  "skills": ["local-skill"],\n'
        '  "workflows": ["local-workflow"],\n'
        '  "install_items": []\n'
        "}\n",
        encoding="utf-8",
    )
    return package_dir


def _write_mcp_connector_extension(root: Path) -> Path:
    package_dir = root / "connector-pack"
    (package_dir / "mcp").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.test-connector\n"
        "version: 2026.3.21\n"
        "display_name: Test Connector\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  mcp_servers:\n"
        "    - mcp/github.json\n"
        "permissions:\n"
        "  network: true\n",
        encoding="utf-8",
    )
    (package_dir / "mcp" / "github.json").write_text(
        "{\n"
        '  "name": "github-packaged",\n'
        '  "url": "https://example.test/mcp",\n'
        '  "description": "Packaged GitHub MCP",\n'
        '  "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},\n'
        '  "auth_hint": "Set GITHUB_TOKEN before enabling the connector",\n'
        '  "transport": "streamable-http"\n'
        "}\n",
        encoding="utf-8",
    )
    return package_dir


@pytest.fixture
def extension_runtime(tmp_path):
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
        list(workflow_manager._load_errors),
        list(workflow_manager._shared_manifest_errors),
        workflow_manager._workflows_dir,
        list(workflow_manager._manifest_roots),
        workflow_manager._config_path,
        set(workflow_manager._disabled),
        workflow_manager._registry,
    )
    original_runbook_manager = (
        list(runbook_manager._runbooks),
        list(runbook_manager._load_errors),
        list(runbook_manager._shared_manifest_errors),
        runbook_manager._runbooks_dir,
        list(runbook_manager._manifest_roots),
        runbook_manager._registry,
    )
    original_starter_pack_manager = (
        list(starter_pack_manager._packs),
        list(starter_pack_manager._load_errors),
        list(starter_pack_manager._shared_manifest_errors),
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

    workspace_dir = tmp_path / "workspace"
    skills_dir = workspace_dir / "skills"
    workflows_dir = workspace_dir / "workflows"
    runbooks_dir = workspace_dir / "runbooks"
    starter_packs_path = workspace_dir / "starter-packs.json"
    skills_dir.mkdir(parents=True)
    workflows_dir.mkdir()
    runbooks_dir.mkdir()

    settings.workspace_dir = str(workspace_dir)
    manifest_roots = default_manifest_roots_for_workspace(str(workspace_dir))
    skill_manager.init(str(skills_dir), manifest_roots=manifest_roots)
    workflow_manager.init(str(workflows_dir), manifest_roots=manifest_roots)
    runbook_manager.init(str(runbooks_dir), manifest_roots=manifest_roots)
    starter_pack_manager.init(str(starter_packs_path), manifest_roots=manifest_roots)
    mcp_manager.disconnect_all()
    mcp_manager._config = {}
    mcp_manager._status = {}
    mcp_manager._tools = {}
    mcp_manager._config_path = str(workspace_dir / "mcp-servers.json")

    yield workspace_dir

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
        workflow_manager._load_errors,
        workflow_manager._shared_manifest_errors,
        workflow_manager._workflows_dir,
        workflow_manager._manifest_roots,
        workflow_manager._config_path,
        workflow_manager._disabled,
        workflow_manager._registry,
    ) = original_workflow_manager
    (
        runbook_manager._runbooks,
        runbook_manager._load_errors,
        runbook_manager._shared_manifest_errors,
        runbook_manager._runbooks_dir,
        runbook_manager._manifest_roots,
        runbook_manager._registry,
    ) = original_runbook_manager
    (
        starter_pack_manager._packs,
        starter_pack_manager._load_errors,
        starter_pack_manager._shared_manifest_errors,
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


@pytest.mark.asyncio
async def test_list_extensions_includes_bundled_core_capabilities(client, extension_runtime):
    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file"), SimpleNamespace(name="web_search"), SimpleNamespace(name="write_file"), SimpleNamespace(name="http_request")], ["web-briefing"], "approval"),
    ):
        response = await client.get("/api/extensions")

    assert response.status_code == 200
    payload = response.json()
    bundled = next(item for item in payload["extensions"] if item["id"] == "seraph.core-capabilities")
    assert bundled["location"] == "bundled"
    assert bundled["removable"] is False
    assert bundled["enable_supported"] is True


@pytest.mark.asyncio
async def test_validate_extension_package_path_returns_manifest_report(client, tmp_path):
    package_dir = _write_installable_extension(tmp_path)

    response = await client.post("/api/extensions/validate", json={"path": str(package_dir)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["extension_id"] == "seraph.test-installable"
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_install_configure_toggle_and_remove_workspace_extension(client, extension_runtime, tmp_path):
    package_dir = _write_installable_extension(tmp_path)

    with (
        patch(
            "src.extensions.lifecycle.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file"), SimpleNamespace(name="web_search"), SimpleNamespace(name="write_file"), SimpleNamespace(name="http_request")], [], "approval"),
        ),
        patch("src.api.extensions.log_integration_event", AsyncMock()),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201
        installed = install_response.json()["extension"]
        assert installed["id"] == "seraph.test-installable"
        assert installed["location"] == "workspace"
        assert installed["enabled_scope"] == "toggleable_contributions"
        assert installed["configurable"] is False
        assert installed["metadata_supported"] is True
        assert (extension_runtime / "extensions" / "seraph-test-installable").is_dir()

        configure_response = await client.post(
            "/api/extensions/seraph.test-installable/configure",
            json={"config": {"mode": "focus", "budget": "low"}},
        )
        assert configure_response.status_code == 200
        configured = configure_response.json()["extension"]
        assert configured["config"] == {"mode": "focus", "budget": "low"}
        assert configured["config_scope"] == "metadata_only"

        disable_response = await client.post("/api/extensions/seraph.test-installable/disable")
        assert disable_response.status_code == 200
        assert disable_response.json()["extension"]["enabled"] is False
        assert skill_manager.get_skill("local-skill") is not None
        assert skill_manager.get_skill("local-skill").enabled is False
        assert workflow_manager.get_workflow("local-workflow") is not None
        assert workflow_manager.get_workflow("local-workflow").enabled is False
        assert runbook_manager.get_runbook("runbook:local-runbook") is not None
        assert starter_pack_manager.get_pack("local-pack") is not None

        enable_response = await client.post("/api/extensions/seraph.test-installable/enable")
        assert enable_response.status_code == 200
        assert enable_response.json()["extension"]["enabled"] is True
        assert skill_manager.get_skill("local-skill") is not None
        assert skill_manager.get_skill("local-skill").enabled is True
        assert workflow_manager.get_workflow("local-workflow") is not None
        assert workflow_manager.get_workflow("local-workflow").enabled is True

        remove_response = await client.delete("/api/extensions/seraph.test-installable")
        assert remove_response.status_code == 200
        assert not (extension_runtime / "extensions" / "seraph-test-installable").exists()

        reinstall_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert reinstall_response.status_code == 201
        reinstalled = reinstall_response.json()["extension"]
        assert reinstalled["enabled"] is True
        assert skill_manager.get_skill("local-skill") is not None
        assert skill_manager.get_skill("local-skill").enabled is True
        assert workflow_manager.get_workflow("local-workflow") is not None
        assert workflow_manager.get_workflow("local-workflow").enabled is True


@pytest.mark.asyncio
async def test_install_toggle_and_remove_workspace_connector_extension(client, extension_runtime, tmp_path):
    package_dir = _write_mcp_connector_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ), patch.object(
        mcp_manager,
        "connect",
    ) as connect_mock, patch.object(
        mcp_manager,
        "disconnect",
    ) as disconnect_mock:
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201
        installed = install_response.json()["extension"]
        assert installed["id"] == "seraph.test-connector"
        assert installed["enabled"] is False
        assert installed["toggleable_contribution_types"] == ["mcp_servers"]
        assert installed["studio_files"][1]["reference"] == "mcp/github.json"
        assert installed["studio_files"][1]["loaded"] is True
        assert mcp_manager._config["github-packaged"]["enabled"] is False
        assert mcp_manager._config["github-packaged"]["auth_hint"] == "Set GITHUB_TOKEN before enabling the connector"
        connect_mock.assert_not_called()

        enable_response = await client.post("/api/extensions/seraph.test-connector/enable")
        assert enable_response.status_code == 200
        assert enable_response.json()["extension"]["enabled"] is True
        assert mcp_manager._config["github-packaged"]["enabled"] is True
        connect_mock.assert_called_once()

        disable_response = await client.post("/api/extensions/seraph.test-connector/disable")
        assert disable_response.status_code == 200
        assert disable_response.json()["extension"]["enabled"] is False
        assert mcp_manager._config["github-packaged"]["enabled"] is False
        disconnect_mock.assert_called_once()

        remove_response = await client.delete("/api/extensions/seraph.test-connector")
        assert remove_response.status_code == 200
        assert "github-packaged" not in mcp_manager._config
        assert not (extension_runtime / "extensions" / "seraph-test-connector").exists()


@pytest.mark.asyncio
async def test_workspace_extension_exposes_studio_files_and_source_endpoints(client, extension_runtime, tmp_path):
    package_dir = _write_installable_extension(tmp_path)

    with (
        patch(
            "src.extensions.lifecycle.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], [], "approval"),
        ),
        patch("src.api.extensions.log_integration_event", AsyncMock()),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        detail_response = await client.get("/api/extensions/seraph.test-installable")
        assert detail_response.status_code == 200
        extension = detail_response.json()["extension"]
        studio_files = extension["studio_files"]
        references = [item["reference"] for item in studio_files]
        assert references[:3] == ["manifest.yaml", "skills/local-skill.md", "workflows/local-workflow.md"]
        assert any(item["reference"] == "runbooks/local-runbook.yaml" for item in studio_files)
        assert any(item["reference"] == "starter-packs/local-pack.json" for item in studio_files)

        source_response = await client.get(
            "/api/extensions/seraph.test-installable/source",
            params={"reference": "skills/local-skill.md"},
        )
        assert source_response.status_code == 200
        source_payload = source_response.json()
        assert source_payload["editable"] is True
        assert source_payload["validation"]["skill"]["name"] == "local-skill"


@pytest.mark.asyncio
async def test_workspace_extension_source_save_updates_package_members(client, extension_runtime, tmp_path):
    package_dir = _write_installable_extension(tmp_path)

    with (
        patch(
            "src.extensions.lifecycle.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], [], "approval"),
        ),
        patch("src.api.extensions.log_integration_event", AsyncMock()),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        save_response = await client.post(
            "/api/extensions/seraph.test-installable/source",
            json={
                "reference": "workflows/local-workflow.md",
                "content": (
                    "---\n"
                    "name: local-workflow\n"
                    "description: Updated installable workflow\n"
                    "requires:\n"
                    "  tools: [read_file]\n"
                    "steps:\n"
                    "  - id: inspect\n"
                    "    tool: read_file\n"
                    "    arguments:\n"
                    "      file_path: notes/updated.md\n"
                    "---\n\n"
                    "Use the updated workflow.\n"
                ),
            },
        )
        assert save_response.status_code == 200
        payload = save_response.json()
        assert payload["validation"]["workflow"]["name"] == "local-workflow"
        assert workflow_manager.get_workflow("local-workflow") is not None

        manifest_response = await client.post(
            "/api/extensions/seraph.test-installable/source",
            json={
                "reference": "manifest.yaml",
                "content": (
                    "id: seraph.test-installable\n"
                    "version: 2026.3.22\n"
                    "display_name: Test Installable Updated\n"
                    "kind: capability-pack\n"
                    "compatibility:\n"
                    "  seraph: \">=2026.3.19\"\n"
                    "publisher:\n"
                    "  name: Seraph\n"
                    "trust: local\n"
                    "summary: Updated installable package.\n"
                    "contributes:\n"
                    "  skills:\n"
                    "    - skills/local-skill.md\n"
                    "  workflows:\n"
                    "    - workflows/local-workflow.md\n"
                    "  runbooks:\n"
                    "    - runbooks/local-runbook.yaml\n"
                    "  starter_packs:\n"
                    "    - starter-packs/local-pack.json\n"
                    "permissions:\n"
                    "  tools: [read_file]\n"
                    "  network: false\n"
                ),
            },
        )
        assert manifest_response.status_code == 200
        manifest_payload = manifest_response.json()
        assert manifest_payload["extension"]["display_name"] == "Test Installable Updated"
        assert manifest_payload["validation"]["manifest"]["version"] == "2026.3.22"


@pytest.mark.asyncio
async def test_manifest_source_save_rejects_unloadable_package_updates(client, extension_runtime, tmp_path):
    package_dir = _write_installable_extension(tmp_path)

    with (
        patch(
            "src.extensions.lifecycle.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], [], "approval"),
        ),
        patch("src.api.extensions.log_integration_event", AsyncMock()),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        installed_root = extension_runtime / "extensions" / "seraph-test-installable"
        original_manifest = (installed_root / "manifest.yaml").read_text(encoding="utf-8")

        response = await client.post(
            "/api/extensions/seraph.test-installable/source",
            json={
                "reference": "manifest.yaml",
                "content": (
                    "id: seraph.test-installable\n"
                    "version: 2026.3.22\n"
                    "display_name: Broken Installable\n"
                    "kind: capability-pack\n"
                    "compatibility:\n"
                    "  seraph: \">=9999.1.1\"\n"
                    "publisher:\n"
                    "  name: Seraph\n"
                    "trust: local\n"
                    "contributes:\n"
                    "  skills:\n"
                    "    - skills/local-skill.md\n"
                    "permissions:\n"
                    "  tools: [read_file]\n"
                    "  network: false\n"
                ),
            },
        )

        assert response.status_code == 422
        assert "invalid" in response.json()["detail"]
        assert (installed_root / "manifest.yaml").read_text(encoding="utf-8") == original_manifest


@pytest.mark.asyncio
async def test_manifest_source_save_validates_manifest_yml_aliases(client, extension_runtime, tmp_path):
    package_dir = _write_installable_extension(tmp_path)
    manifest_yaml = package_dir / "manifest.yaml"
    manifest_yml = package_dir / "manifest.yml"
    manifest_yml.write_text(manifest_yaml.read_text(encoding="utf-8"), encoding="utf-8")
    manifest_yaml.unlink()

    with (
        patch(
            "src.extensions.lifecycle.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], [], "approval"),
        ),
        patch("src.api.extensions.log_integration_event", AsyncMock()),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        installed_root = extension_runtime / "extensions" / "seraph-test-installable"
        original_manifest = (installed_root / "manifest.yml").read_text(encoding="utf-8")

        response = await client.post(
            "/api/extensions/seraph.test-installable/source",
            json={
                "reference": "manifest.yaml",
                "content": (
                    "id: seraph.test-installable\n"
                    "version: 2026.3.22\n"
                    "display_name: Broken Installable\n"
                    "kind: capability-pack\n"
                    "compatibility:\n"
                    "  seraph: \">=9999.1.1\"\n"
                    "publisher:\n"
                    "  name: Seraph\n"
                    "trust: local\n"
                    "contributes:\n"
                    "  skills:\n"
                    "    - skills/local-skill.md\n"
                    "permissions:\n"
                    "  tools: [read_file]\n"
                    "  network: false\n"
                ),
            },
        )

        assert response.status_code == 422
        assert "invalid" in response.json()["detail"]
        assert (installed_root / "manifest.yml").read_text(encoding="utf-8") == original_manifest


@pytest.mark.asyncio
async def test_broken_workspace_manifest_can_be_loaded_and_repaired_via_source_api(client, extension_runtime, tmp_path):
    package_dir = _write_installable_extension(tmp_path)

    with (
        patch(
            "src.extensions.lifecycle.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], [], "approval"),
        ),
        patch("src.api.extensions.log_integration_event", AsyncMock()),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        installed_root = extension_runtime / "extensions" / "seraph-test-installable"
        broken_manifest = "id: seraph.test-installable\nversion:\n  - [broken\n"
        (installed_root / "manifest.yaml").write_text(broken_manifest, encoding="utf-8")

        source_response = await client.get(
            "/api/extensions/seraph.test-installable/source",
            params={"reference": "manifest.yaml"},
        )
        assert source_response.status_code == 200
        source_payload = source_response.json()
        assert source_payload["content"] == broken_manifest
        assert source_payload["validation"]["valid"] is False

        repair_response = await client.post(
            "/api/extensions/seraph.test-installable/source",
            json={
                "reference": "manifest.yaml",
                "content": (
                    "id: seraph.test-installable\n"
                    "version: 2026.3.22\n"
                    "display_name: Repaired Installable\n"
                    "kind: capability-pack\n"
                    "compatibility:\n"
                    "  seraph: \">=2026.3.19\"\n"
                    "publisher:\n"
                    "  name: Seraph\n"
                    "trust: local\n"
                    "contributes:\n"
                    "  skills:\n"
                    "    - skills/local-skill.md\n"
                    "  workflows:\n"
                    "    - workflows/local-workflow.md\n"
                    "  runbooks:\n"
                    "    - runbooks/local-runbook.yaml\n"
                    "  starter_packs:\n"
                    "    - starter-packs/local-pack.json\n"
                    "permissions:\n"
                    "  tools: [read_file]\n"
                    "  network: false\n"
                ),
            },
        )
        assert repair_response.status_code == 200
        repair_payload = repair_response.json()
        assert repair_payload["validation"]["valid"] is True
        assert repair_payload["extension"]["display_name"] == "Repaired Installable"


@pytest.mark.asyncio
async def test_workflow_source_save_rejects_cross_reference_breakage(client, extension_runtime, tmp_path):
    package_dir = _write_installable_extension(tmp_path)

    with (
        patch(
            "src.extensions.lifecycle.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], [], "approval"),
        ),
        patch("src.api.extensions.log_integration_event", AsyncMock()),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        response = await client.post(
            "/api/extensions/seraph.test-installable/source",
            json={
                "reference": "workflows/local-workflow.md",
                "content": (
                    "---\n"
                    "name: local-workflow-updated\n"
                    "description: Updated installable workflow\n"
                    "requires:\n"
                    "  tools: [read_file]\n"
                    "steps:\n"
                    "  - id: inspect\n"
                    "    tool: read_file\n"
                    "    arguments:\n"
                    "      file_path: notes/updated.md\n"
                    "---\n\n"
                    "Use the updated workflow.\n"
                ),
            },
        )

        assert response.status_code == 422
        assert "unknown workflow" in response.json()["detail"]
        assert workflow_manager.get_workflow("local-workflow-updated") is None
        assert workflow_manager.get_workflow("local-workflow") is not None


@pytest.mark.asyncio
async def test_skill_source_save_rejects_workflow_reference_breakage(client, extension_runtime, tmp_path):
    package_dir = _write_installable_extension(tmp_path)
    (package_dir / "workflows" / "local-workflow.md").write_text(
        "---\n"
        "name: local-workflow\n"
        "description: Local installable workflow\n"
        "requires:\n"
        "  tools: [read_file]\n"
        "  skills: [local-skill]\n"
        "steps:\n"
        "  - id: inspect\n"
        "    tool: read_file\n"
        "    arguments:\n"
        "      file_path: notes/test.md\n"
        "---\n\n"
        "Use the local workflow.\n",
        encoding="utf-8",
    )

    with (
        patch(
            "src.extensions.lifecycle.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], [], "approval"),
        ),
        patch("src.api.extensions.log_integration_event", AsyncMock()),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        response = await client.post(
            "/api/extensions/seraph.test-installable/source",
            json={
                "reference": "skills/local-skill.md",
                "content": (
                    "---\n"
                    "name: local-skill-updated\n"
                    "description: Local installable skill\n"
                    "requires:\n"
                    "  tools: []\n"
                    "user_invocable: true\n"
                    "---\n\n"
                    "Use the local skill.\n"
                ),
            },
        )

        assert response.status_code == 422
        assert "unknown skills" in response.json()["detail"]
        assert skill_manager.get_skill("local-skill-updated") is None
        assert skill_manager.get_skill("local-skill") is not None


@pytest.mark.asyncio
async def test_skill_source_save_rejects_external_reverse_dependencies(client, extension_runtime, tmp_path):
    package_dir = _write_installable_extension(tmp_path)
    (package_dir / "starter-packs" / "local-pack.json").write_text(
        "{\n"
        '  "name": "local-pack",\n'
        '  "label": "Local Pack",\n'
        '  "description": "Local pack without direct skill references.",\n'
        '  "skills": [],\n'
        '  "workflows": [],\n'
        '  "install_items": []\n'
        "}\n",
        encoding="utf-8",
    )

    with (
        patch(
            "src.extensions.lifecycle.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], [], "approval"),
        ),
        patch("src.api.extensions.log_integration_event", AsyncMock()),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        (extension_runtime / "workflows" / "external-dependent.md").write_text(
            "---\n"
            "name: external-dependent\n"
            "description: External workflow that depends on the packaged skill\n"
            "requires:\n"
            "  tools: [read_file]\n"
            "  skills: [local-skill]\n"
            "steps:\n"
            "  - id: inspect\n"
            "    tool: read_file\n"
            "    arguments:\n"
            "      file_path: notes/external.md\n"
            "---\n\n"
            "Use the external workflow.\n",
            encoding="utf-8",
        )
        workflow_manager.reload()

        response = await client.post(
            "/api/extensions/seraph.test-installable/source",
            json={
                "reference": "skills/local-skill.md",
                "content": (
                    "---\n"
                    "name: local-skill-updated\n"
                    "description: Local installable skill\n"
                    "requires:\n"
                    "  tools: []\n"
                    "user_invocable: true\n"
                    "---\n\n"
                    "Use the local skill.\n"
                ),
            },
        )

        assert response.status_code == 422
        assert "depends on skills removed" in response.json()["detail"]
        assert skill_manager.get_skill("local-skill-updated") is None
        assert skill_manager.get_skill("local-skill") is not None


@pytest.mark.asyncio
async def test_workflow_source_save_allows_cross_package_references(client, extension_runtime, tmp_path):
    (extension_runtime / "skills" / "external-skill.md").write_text(
        "---\n"
        "name: external-skill\n"
        "description: External skill\n"
        "requires:\n"
        "  tools: []\n"
        "user_invocable: true\n"
        "---\n\n"
        "Use the external skill.\n",
        encoding="utf-8",
    )
    (extension_runtime / "workflows" / "external-workflow.md").write_text(
        "---\n"
        "name: external-workflow\n"
        "description: External workflow\n"
        "requires:\n"
        "  tools: [read_file]\n"
        "steps:\n"
        "  - id: inspect\n"
        "    tool: read_file\n"
        "    arguments:\n"
        "      file_path: notes/external.md\n"
        "---\n\n"
        "Use the external workflow.\n",
        encoding="utf-8",
    )
    (extension_runtime / "starter-packs.json").write_text(
        "{\n"
        '  "packs": [\n'
        "    {\n"
        '      "name": "external-pack",\n'
        '      "label": "External Pack",\n'
        '      "description": "Shared external pack.",\n'
        '      "skills": ["external-skill"],\n'
        '      "workflows": ["external-workflow"],\n'
        '      "install_items": []\n'
        "    }\n"
        "  ]\n"
        "}\n",
        encoding="utf-8",
    )
    manifest_roots = default_manifest_roots_for_workspace(str(extension_runtime))
    skill_manager.reload()
    workflow_manager.reload()
    starter_pack_manager.init(str(extension_runtime / "starter-packs.json"), manifest_roots=manifest_roots)

    package_dir = _write_installable_extension(tmp_path)
    (package_dir / "runbooks" / "local-runbook.yaml").write_text(
        "id: runbook:local-runbook\n"
        "title: Local Runbook\n"
        "summary: Run the external workflow.\n"
        "workflow: external-workflow\n",
        encoding="utf-8",
    )
    (package_dir / "starter-packs" / "local-pack.json").write_text(
        "{\n"
        '  "name": "local-pack",\n'
        '  "label": "Local Pack",\n'
        '  "description": "Enable shared external capability.",\n'
        '  "skills": ["external-skill"],\n'
        '  "workflows": ["external-workflow"],\n'
        '  "install_items": []\n'
        "}\n",
        encoding="utf-8",
    )

    with (
        patch(
            "src.extensions.lifecycle.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], [], "approval"),
        ),
        patch("src.api.extensions.log_integration_event", AsyncMock()),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        response = await client.post(
            "/api/extensions/seraph.test-installable/source",
            json={
                "reference": "workflows/local-workflow.md",
                "content": (
                    "---\n"
                    "name: local-workflow\n"
                    "description: Updated installable workflow\n"
                    "requires:\n"
                    "  tools: [read_file]\n"
                    "  skills: [external-skill]\n"
                    "steps:\n"
                    "  - id: inspect\n"
                    "    tool: read_file\n"
                    "    arguments:\n"
                    "      file_path: notes/updated.md\n"
                    "---\n\n"
                    "Use the updated workflow.\n"
                ),
            },
        )

        assert response.status_code == 200
        assert response.json()["validation"]["workflow"]["name"] == "local-workflow"


@pytest.mark.asyncio
async def test_remove_bundled_extension_is_rejected(client, extension_runtime):
    with patch("src.api.extensions.log_integration_event", AsyncMock()):
        response = await client.delete("/api/extensions/seraph.core-capabilities")

    assert response.status_code == 409
