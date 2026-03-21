from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from config.settings import settings
from src.extensions.registry import default_manifest_roots_for_workspace
from src.runbooks.manager import runbook_manager
from src.skills.manager import skill_manager
from src.starter_packs.manager import starter_pack_manager
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
async def test_remove_bundled_extension_is_rejected(client, extension_runtime):
    with patch("src.api.extensions.log_integration_event", AsyncMock()):
        response = await client.delete("/api/extensions/seraph.core-capabilities")

    assert response.status_code == 409
