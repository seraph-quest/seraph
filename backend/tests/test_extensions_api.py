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


def _write_installable_extension(
    root: Path,
    *,
    extension_id: str = "seraph.test-installable",
    version: str = "2026.3.21",
    display_name: str = "Test Installable",
    package_name: str = "installable-pack",
    skill_description: str = "Local installable skill",
    workflow_description: str = "Local installable workflow",
    workflow_content: str = "Use the local workflow.\n",
) -> Path:
    package_dir = root / package_name
    (package_dir / "skills").mkdir(parents=True)
    (package_dir / "workflows").mkdir()
    (package_dir / "runbooks").mkdir()
    (package_dir / "starter-packs").mkdir()
    (package_dir / "manifest.yaml").write_text(
        f"id: {extension_id}\n"
        f"version: {version}\n"
        f"display_name: {display_name}\n"
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
        f"description: {skill_description}\n"
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
        f"description: {workflow_description}\n"
        "requires:\n"
        "  tools: [read_file]\n"
        "steps:\n"
        "  - id: inspect\n"
        "    tool: read_file\n"
        "    arguments:\n"
        "      file_path: notes/test.md\n"
        "---\n\n"
        f"{workflow_content}",
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


def _write_high_risk_extension(root: Path) -> Path:
    package_dir = root / "high-risk-pack"
    (package_dir / "workflows").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.high-risk-pack\n"
        "version: 2026.3.21\n"
        "display_name: High Risk Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  workflows:\n"
        "    - workflows/write-note.md\n"
        "permissions:\n"
        "  tools: [write_file]\n"
        "  network: false\n",
        encoding="utf-8",
    )
    (package_dir / "workflows" / "write-note.md").write_text(
        "---\n"
        "name: write-note\n"
        "description: Write a note into the workspace\n"
        "requires:\n"
        "  tools: [write_file]\n"
        "steps:\n"
        "  - id: save\n"
        "    tool: write_file\n"
        "    arguments:\n"
        "      file_path: notes/high-risk.md\n"
        "      content: approved\n"
        "---\n\n"
        "Write a note.\n",
        encoding="utf-8",
    )
    return package_dir


def _write_mcp_connector_extension(
    root: Path,
    *,
    version: str = "2026.3.21",
    url: str = "https://example.test/mcp",
    description: str = "Packaged GitHub MCP",
    auth_hint: str = "Set GITHUB_TOKEN before enabling the connector",
    headers: str = '{"Authorization": "Bearer ${GITHUB_TOKEN}"}',
    package_name: str = "connector-pack",
) -> Path:
    package_dir = root / package_name
    (package_dir / "mcp").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.test-connector\n"
        f"version: {version}\n"
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
        f'  "url": "{url}",\n'
        f'  "description": "{description}",\n'
        f'  "headers": {headers},\n'
        f'  "auth_hint": "{auth_hint}",\n'
        '  "transport": "streamable-http"\n'
        "}\n",
        encoding="utf-8",
    )
    return package_dir


def _write_managed_connector_extension(root: Path) -> Path:
    package_dir = root / "managed-connector-pack"
    (package_dir / "connectors" / "managed").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.managed-github\n"
        "version: 2026.3.21\n"
        "display_name: Managed GitHub\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  managed_connectors:\n"
        "    - connectors/managed/github.yaml\n"
        "permissions:\n"
        "  network: true\n",
        encoding="utf-8",
    )
    (package_dir / "connectors" / "managed" / "github.yaml").write_text(
        "name: github-managed\n"
        "provider: github\n"
        "description: Curated GitHub connector\n"
        "auth_kind: oauth\n"
        "capabilities:\n"
        "  - pull_requests.read\n"
        "  - issues.write\n"
        "setup_steps:\n"
        "  - Authorize GitHub access\n"
        "config_fields:\n"
        "  - key: installation_id\n"
        "    label: Installation ID\n"
        "    required: true\n"
        "  - key: api_base_url\n"
        "    label: API Base URL\n"
        "    required: false\n"
        "    input: url\n",
        encoding="utf-8",
    )
    return package_dir


def _write_mixed_managed_connector_extension(root: Path) -> Path:
    package_dir = root / "mixed-managed-pack"
    (package_dir / "connectors" / "managed").mkdir(parents=True)
    (package_dir / "workflows").mkdir()
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.mixed-managed\n"
        "version: 2026.3.21\n"
        "display_name: Mixed Managed\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  workflows:\n"
        "    - workflows/local-workflow.md\n"
        "  managed_connectors:\n"
        "    - connectors/managed/github.yaml\n"
        "permissions:\n"
        "  tools: [read_file]\n"
        "  network: true\n",
        encoding="utf-8",
    )
    (package_dir / "workflows" / "local-workflow.md").write_text(
        "---\n"
        "name: local-workflow\n"
        "description: Local workflow for atomic toggle test\n"
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
    (package_dir / "connectors" / "managed" / "github.yaml").write_text(
        "name: github-managed\n"
        "provider: github\n"
        "description: Curated GitHub connector\n"
        "enabled: false\n"
        "auth_kind: oauth\n"
        "config_fields:\n"
        "  - key: installation_id\n"
        "    label: Installation ID\n"
        "    required: true\n",
        encoding="utf-8",
    )
    return package_dir


def _write_observer_extension(root: Path) -> Path:
    package_dir = root / "observer-pack"
    (package_dir / "observers" / "definitions").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.calendar-observer\n"
        "version: 2026.3.21\n"
        "display_name: Calendar Observer\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "permissions:\n"
        "  network: true\n"
        "contributes:\n"
        "  observer_definitions:\n"
        "    - observers/definitions/calendar.yaml\n",
        encoding="utf-8",
    )
    (package_dir / "observers" / "definitions" / "calendar.yaml").write_text(
        "name: calendar\n"
        "source_type: calendar\n"
        "description: Curated calendar observer\n"
        "enabled: true\n",
        encoding="utf-8",
    )
    return package_dir


def _write_wave2_extension(root: Path) -> Path:
    package_dir = root / "wave2-pack"
    (package_dir / "presets" / "toolset").mkdir(parents=True)
    (package_dir / "context").mkdir()
    (package_dir / "speech").mkdir()
    (package_dir / "automation").mkdir()
    (package_dir / "connectors" / "browser").mkdir(parents=True)
    (package_dir / "connectors" / "messaging").mkdir()
    (package_dir / "connectors" / "nodes").mkdir()
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.wave2-pack\n"
        "version: 2026.3.23\n"
        "display_name: Wave 2 Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "permissions:\n"
        "  tools: [read_file]\n"
        "  network: true\n"
        "contributes:\n"
        "  toolset_presets:\n"
        "    - presets/toolset/research.yaml\n"
        "  context_packs:\n"
        "    - context/research.yaml\n"
        "  speech_profiles:\n"
        "    - speech/voice.yaml\n"
        "  automation_triggers:\n"
        "    - automation/daily-brief.yaml\n"
        "  browser_providers:\n"
        "    - connectors/browser/browserbase.yaml\n"
        "  messaging_connectors:\n"
        "    - connectors/messaging/telegram.yaml\n"
        "  node_adapters:\n"
        "    - connectors/nodes/companion.yaml\n",
        encoding="utf-8",
    )
    (package_dir / "presets" / "toolset" / "research.yaml").write_text(
        "name: research\ninclude_tools:\n  - read_file\ncapabilities:\n  - analysis\n",
        encoding="utf-8",
    )
    (package_dir / "context" / "research.yaml").write_text(
        "name: research\ninstructions: Keep context tight.\ndomains:\n  - research\n",
        encoding="utf-8",
    )
    (package_dir / "speech" / "voice.yaml").write_text(
        "name: narrator\nprovider: openai\nsupports_tts: true\nvoice: alloy\n",
        encoding="utf-8",
    )
    (package_dir / "automation" / "daily-brief.yaml").write_text(
        "name: daily-brief\ntrigger_type: webhook\nendpoint: https://example.test/hooks/daily\nconfig_fields:\n  - key: signing_secret\n    label: Signing Secret\n    input: password\n",
        encoding="utf-8",
    )
    (package_dir / "connectors" / "browser" / "browserbase.yaml").write_text(
        "name: browserbase\nprovider_kind: browserbase\nconfig_fields:\n  - key: api_key\n    label: API Key\n    input: password\n",
        encoding="utf-8",
    )
    (package_dir / "connectors" / "messaging" / "telegram.yaml").write_text(
        "name: telegram\nplatform: telegram\ndelivery_modes:\n  - dm\nconfig_fields:\n  - key: bot_token\n    label: Bot Token\n    input: password\n",
        encoding="utf-8",
    )
    (package_dir / "connectors" / "nodes" / "companion.yaml").write_text(
        "name: companion\nadapter_kind: companion\nconfig_fields:\n  - key: node_url\n    label: Node URL\n    input: url\n",
        encoding="utf-8",
    )
    return package_dir


def _write_toolset_boundary_extension(root: Path, *, boundary: str, network: bool = False) -> Path:
    package_dir = root / f"toolset-{boundary.replace('_', '-')}"
    (package_dir / "presets" / "toolset").mkdir(parents=True)
    network_literal = "true" if network else "false"
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.toolset-boundary-pack\n"
        "version: 2026.3.23\n"
        "display_name: Toolset Boundary Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "permissions:\n"
        f"  network: {network_literal}\n"
        "contributes:\n"
        "  toolset_presets:\n"
        "    - presets/toolset/boundary.yaml\n",
        encoding="utf-8",
    )
    (package_dir / "presets" / "toolset" / "boundary.yaml").write_text(
        f"name: boundary\nexecution_boundaries:\n  - {boundary}\n",
        encoding="utf-8",
    )
    return package_dir


def _write_invalid_observer_extension(root: Path) -> Path:
    package_dir = root / "invalid-observer-pack"
    (package_dir / "observers" / "definitions").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.invalid-observer\n"
        "version: 2026.3.21\n"
        "display_name: Invalid Observer\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "permissions:\n"
        "  network: true\n"
        "contributes:\n"
        "  observer_definitions:\n"
        "    - observers/definitions/calendar.yaml\n",
        encoding="utf-8",
    )
    (package_dir / "observers" / "definitions" / "calendar.yaml").write_text(
        "name: invalid-calendar\n"
        "description: Missing source type\n"
        "enabled: true\n",
        encoding="utf-8",
    )
    return package_dir


def _write_channel_adapter_extension(root: Path) -> Path:
    package_dir = root / "channel-adapter-pack"
    (package_dir / "channels").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.native-channel\n"
        "version: 2026.3.21\n"
        "display_name: Native Channel\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  channel_adapters:\n"
        "    - channels/native.yaml\n",
        encoding="utf-8",
    )
    (package_dir / "channels" / "native.yaml").write_text(
        "name: workspace-native\n"
        "transport: native_notification\n"
        "description: Workspace native adapter\n"
        "enabled: true\n",
        encoding="utf-8",
    )
    return package_dir


def _write_invalid_channel_adapter_extension(root: Path) -> Path:
    package_dir = root / "invalid-channel-pack"
    (package_dir / "channels").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.invalid-channel\n"
        "version: 2026.3.21\n"
        "display_name: Invalid Channel\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.3.19\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  channel_adapters:\n"
        "    - channels/native.yaml\n",
        encoding="utf-8",
    )
    (package_dir / "channels" / "native.yaml").write_text(
        "name: invalid-native\n"
        "description: Missing transport\n"
        "enabled: true\n",
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
    managed = next(item for item in payload["extensions"] if item["id"] == "seraph.core-managed-connectors")
    assert managed["location"] == "bundled"
    assert managed["enable_supported"] is True
    assert managed["toggleable_contribution_types"] == ["managed_connectors"]


@pytest.mark.asyncio
async def test_validate_extension_package_path_returns_manifest_report(client, tmp_path):
    package_dir = _write_installable_extension(tmp_path)
    with patch("src.api.extensions.log_integration_event", AsyncMock()) as log_event:
        response = await client.post("/api/extensions/validate", json={"path": str(package_dir)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["extension_id"] == "seraph.test-installable"
    assert payload["ok"] is True
    assert isinstance(payload["package_digest"], str)
    assert payload["permissions"]["tools"] == ["read_file"]
    assert payload["permission_summary"]["required"]["tools"] == ["read_file"]
    assert payload["approval_profile"]["requires_lifecycle_approval"] is False
    assert log_event.await_count == 1
    assert log_event.await_args.kwargs["integration_type"] == "extension"
    assert log_event.await_args.kwargs["outcome"] == "succeeded"
    assert log_event.await_args.kwargs["details"]["status"] == "validated"


@pytest.mark.asyncio
async def test_validate_extension_reports_lifecycle_approval_for_boundary_only_toolset(client, tmp_path):
    package_dir = _write_toolset_boundary_extension(tmp_path, boundary="secret_read")

    with patch("src.api.extensions.log_integration_event", AsyncMock()):
        response = await client.post("/api/extensions/validate", json={"path": str(package_dir)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["approval_profile"]["requires_lifecycle_approval"] is True
    assert "secret_read" in payload["approval_profile"]["lifecycle_boundaries"]

    with patch("src.api.extensions.log_integration_event", AsyncMock()):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})

    assert install_response.status_code == 409
    assert install_response.json()["detail"]["type"] == "approval_required"


@pytest.mark.asyncio
async def test_validate_extension_reports_network_for_boundary_only_toolset(client, tmp_path):
    package_dir = _write_toolset_boundary_extension(tmp_path, boundary="external_read", network=False)

    with patch("src.api.extensions.log_integration_event", AsyncMock()):
        response = await client.post("/api/extensions/validate", json={"path": str(package_dir)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["permission_summary"]["required"]["network"] is True
    assert payload["permission_summary"]["missing"]["network"] is True


@pytest.mark.asyncio
async def test_install_logs_failure_for_invalid_extension_package(client, extension_runtime, tmp_path):
    package_dir = _write_invalid_observer_extension(tmp_path)

    with (
        patch(
            "src.extensions.lifecycle.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], [], "approval"),
        ),
        patch("src.api.extensions.log_integration_event", AsyncMock()) as log_event,
    ):
        response = await client.post("/api/extensions/install", json={"path": str(package_dir)})

    assert response.status_code == 422
    assert log_event.await_count == 1
    assert log_event.await_args.kwargs["outcome"] == "failed"
    assert log_event.await_args.kwargs["details"]["status"] == "install_failed"
    assert log_event.await_args.kwargs["details"]["issue_count"] == 1


@pytest.mark.asyncio
async def test_install_and_enable_high_risk_extension_require_approval(client, extension_runtime, tmp_path):
    package_dir = _write_high_risk_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="write_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 409
        detail = install_response.json()["detail"]
        assert detail["type"] == "approval_required"
        install_approval_id = detail["approval_id"]

        approve_install = await client.post(f"/api/approvals/{install_approval_id}/approve")
        assert approve_install.status_code == 200

        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201
        installed = install_response.json()["extension"]
        assert installed["approval_profile"]["requires_lifecycle_approval"] is True
        assert "workspace_write" in installed["approval_profile"]["lifecycle_boundaries"]

        disable_response = await client.post("/api/extensions/seraph.high-risk-pack/disable")
        assert disable_response.status_code == 200

        enable_response = await client.post("/api/extensions/seraph.high-risk-pack/enable")
        assert enable_response.status_code == 409
        enable_detail = enable_response.json()["detail"]
        assert enable_detail["type"] == "approval_required"

        approve_enable = await client.post(f"/api/approvals/{enable_detail['approval_id']}/approve")
        assert approve_enable.status_code == 200

        enable_response = await client.post("/api/extensions/seraph.high-risk-pack/enable")
        assert enable_response.status_code == 200
        assert enable_response.json()["extension"]["enabled"] is True


@pytest.mark.asyncio
async def test_install_high_risk_extension_requires_new_approval_if_package_changes(client, extension_runtime, tmp_path):
    package_dir = _write_high_risk_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="write_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 409
        first_approval_id = install_response.json()["detail"]["approval_id"]

        approve_install = await client.post(f"/api/approvals/{first_approval_id}/approve")
        assert approve_install.status_code == 200

        (package_dir / "workflows" / "write-note.md").write_text(
            "---\n"
            "name: write-note\n"
            "description: Write a changed note into the workspace\n"
            "requires:\n"
            "  tools: [write_file]\n"
            "steps:\n"
            "  - id: save\n"
            "    tool: write_file\n"
            "    arguments:\n"
            "      file_path: notes/high-risk.md\n"
            "      content: changed-after-approval\n"
            "---\n\n"
            "Write a changed note.\n",
            encoding="utf-8",
        )

        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 409
        second_approval_id = install_response.json()["detail"]["approval_id"]
        assert second_approval_id != first_approval_id


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
        assert installed["permissions"]["tools"] == ["read_file"]
        assert installed["permission_summary"]["status"] == "granted"
        assert installed["approval_profile"]["requires_lifecycle_approval"] is False
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
async def test_validate_extension_path_reports_workspace_update_plan(client, extension_runtime, tmp_path):
    package_dir = _write_installable_extension(tmp_path)
    updated_package_dir = _write_installable_extension(
        tmp_path,
        package_name="installable-pack-update",
        version="2026.4.01",
        workflow_description="Updated local installable workflow",
        workflow_content="Use the updated local workflow.\n",
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

        validate_response = await client.post("/api/extensions/validate", json={"path": str(updated_package_dir)})
        assert validate_response.status_code == 200
        payload = validate_response.json()
        assert payload["display_name"] == "Test Installable"
        assert payload["version"] == "2026.4.01"
        assert payload["lifecycle_plan"]["mode"] == "update_workspace"
        assert payload["lifecycle_plan"]["recommended_action"] == "update"
        assert payload["lifecycle_plan"]["current_location"] == "workspace"
        assert payload["lifecycle_plan"]["current_version"] == "2026.3.21"
        assert payload["lifecycle_plan"]["candidate_version"] == "2026.4.01"
        assert payload["lifecycle_plan"]["version_relation"] == "upgrade"
        assert payload["lifecycle_plan"]["package_changed"] is True


@pytest.mark.asyncio
async def test_install_rejects_workspace_replacement_and_update_replaces_package(client, extension_runtime, tmp_path):
    package_dir = _write_installable_extension(tmp_path)
    updated_package_dir = _write_installable_extension(
        tmp_path,
        package_name="installable-pack-update",
        version="2026.4.01",
        workflow_description="Updated local installable workflow",
        workflow_content="Use the updated local workflow.\n",
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

        reinstall_response = await client.post("/api/extensions/install", json={"path": str(updated_package_dir)})
        assert reinstall_response.status_code == 422
        assert "use update to replace the workspace package" in reinstall_response.json()["detail"]

        update_response = await client.post("/api/extensions/update", json={"path": str(updated_package_dir)})
        assert update_response.status_code == 200
        updated = update_response.json()["extension"]
        assert updated["version"] == "2026.4.01"

        source_response = await client.get(
            "/api/extensions/seraph.test-installable/source",
            params={"reference": "workflows/local-workflow.md"},
        )
        assert source_response.status_code == 200
        assert "Updated local installable workflow" in source_response.json()["content"]


@pytest.mark.asyncio
async def test_validate_and_install_allow_workspace_override_for_bundled_extension(client, extension_runtime, tmp_path):
    package_dir = _write_installable_extension(
        tmp_path,
        package_name="bundled-override-pack",
        extension_id="seraph.core-capabilities",
        version="2026.4.01",
        display_name="Core Capabilities Override",
    )

    with (
        patch(
            "src.extensions.lifecycle.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], [], "approval"),
        ),
        patch("src.api.extensions.log_integration_event", AsyncMock()),
    ):
        validate_response = await client.post("/api/extensions/validate", json={"path": str(package_dir)})
        assert validate_response.status_code == 200
        preview = validate_response.json()
        assert preview["lifecycle_plan"]["mode"] == "workspace_override"
        assert preview["lifecycle_plan"]["recommended_action"] == "install"
        assert preview["lifecycle_plan"]["current_location"] == "bundled"

        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201
        installed = install_response.json()["extension"]
        assert installed["id"] == "seraph.core-capabilities"
        assert installed["location"] == "workspace"
        assert installed["version"] == "2026.4.01"

        list_response = await client.get("/api/extensions")
        assert list_response.status_code == 200
        matching = [
            item
            for item in list_response.json()["extensions"]
            if item["id"] == "seraph.core-capabilities"
        ]
        assert len(matching) == 1
        assert matching[0]["location"] == "workspace"


@pytest.mark.asyncio
async def test_enable_rejects_degraded_extension_with_permission_mismatch(client, extension_runtime, tmp_path):
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
        (installed_root / "manifest.yaml").write_text(
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
            "  tools: []\n"
            "  network: false\n",
            encoding="utf-8",
        )

        disable_response = await client.post("/api/extensions/seraph.test-installable/disable")
        assert disable_response.status_code == 200

        enable_response = await client.post("/api/extensions/seraph.test-installable/enable")
        assert enable_response.status_code == 422
        assert "degraded" in enable_response.json()["detail"]


@pytest.mark.asyncio
async def test_enable_rejects_degraded_extension_with_invalid_workflow_before_approval(client, extension_runtime, tmp_path):
    package_dir = _write_high_risk_extension(tmp_path)

    with (
        patch(
            "src.extensions.lifecycle.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="write_file")], [], "approval"),
        ),
        patch("src.api.extensions.log_integration_event", AsyncMock()) as log_event,
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 409
        approval_id = install_response.json()["detail"]["approval_id"]

        approve_install = await client.post(f"/api/approvals/{approval_id}/approve")
        assert approve_install.status_code == 200

        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        installed_root = extension_runtime / "extensions" / "seraph-high-risk-pack"
        (installed_root / "workflows" / "write-note.md").write_text(
            "---\n"
            "name: write-note\n"
            "description: broken workflow\n"
            "steps:\n"
            "  - id: save\n"
            "    tool:\n"
            "---\n",
            encoding="utf-8",
        )

        disable_response = await client.post("/api/extensions/seraph.high-risk-pack/disable")
        assert disable_response.status_code == 200

        enable_response = await client.post("/api/extensions/seraph.high-risk-pack/enable")
        assert enable_response.status_code == 422
        assert "degraded" in enable_response.json()["detail"]
        assert log_event.await_args.kwargs["outcome"] == "failed"
        assert log_event.await_args.kwargs["details"]["status"] == "enable_failed"
        assert log_event.await_args.kwargs["details"]["issue_count"] >= 1


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
        assert install_response.status_code == 409
        install_approval_id = install_response.json()["detail"]["approval_id"]

        approve_install = await client.post(f"/api/approvals/{install_approval_id}/approve")
        assert approve_install.status_code == 200

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
        assert enable_response.status_code == 409
        enable_approval_id = enable_response.json()["detail"]["approval_id"]

        approve_enable = await client.post(f"/api/approvals/{enable_approval_id}/approve")
        assert approve_enable.status_code == 200

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
async def test_update_workspace_connector_refreshes_packaged_mcp_server(client, extension_runtime, tmp_path):
    package_dir = _write_mcp_connector_extension(tmp_path)
    updated_package_dir = _write_mcp_connector_extension(
        tmp_path,
        package_name="connector-pack-update",
        version="2026.4.01",
        url="https://example.test/mcp/v2",
        description="Updated packaged GitHub MCP",
        auth_hint="",
        headers='{"Authorization": "Bearer ${UPDATED_GITHUB_TOKEN}"}',
    )

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
        assert install_response.status_code == 409
        install_approval_id = install_response.json()["detail"]["approval_id"]

        approve_install = await client.post(f"/api/approvals/{install_approval_id}/approve")
        assert approve_install.status_code == 200

        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201
        assert mcp_manager._config["github-packaged"]["url"] == "https://example.test/mcp"

        enable_response = await client.post("/api/extensions/seraph.test-connector/enable")
        assert enable_response.status_code == 409
        enable_approval_id = enable_response.json()["detail"]["approval_id"]

        approve_enable = await client.post(f"/api/approvals/{enable_approval_id}/approve")
        assert approve_enable.status_code == 200

        enable_response = await client.post("/api/extensions/seraph.test-connector/enable")
        assert enable_response.status_code == 200
        assert mcp_manager._config["github-packaged"]["enabled"] is True
        assert connect_mock.call_count == 1

        update_response = await client.post("/api/extensions/update", json={"path": str(updated_package_dir)})
        assert update_response.status_code == 409
        update_approval_id = update_response.json()["detail"]["approval_id"]

        approve_update = await client.post(f"/api/approvals/{update_approval_id}/approve")
        assert approve_update.status_code == 200

        update_response = await client.post("/api/extensions/update", json={"path": str(updated_package_dir)})
        assert update_response.status_code == 200
        updated = update_response.json()["extension"]
        assert updated["version"] == "2026.4.01"
        assert mcp_manager._config["github-packaged"]["url"] == "https://example.test/mcp/v2"
        assert mcp_manager._config["github-packaged"]["description"] == "Updated packaged GitHub MCP"
        assert "auth_hint" not in mcp_manager._config["github-packaged"]
        assert mcp_manager._config["github-packaged"]["headers"] == {
            "Authorization": "Bearer ${UPDATED_GITHUB_TOKEN}"
        }
        assert mcp_manager._config["github-packaged"]["enabled"] is True
        assert connect_mock.call_count == 2
        assert disconnect_mock.call_count == 1


@pytest.mark.asyncio
async def test_install_and_configure_workspace_managed_connector_extension(client, extension_runtime, tmp_path):
    package_dir = _write_managed_connector_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201
        installed = install_response.json()["extension"]
        assert installed["id"] == "seraph.managed-github"
        assert installed["enabled"] is False
        assert installed["toggleable_contribution_types"] == ["managed_connectors"]
        assert installed["passive_contribution_types"] == []
        assert installed["configurable"] is True
        assert installed["config_scope"] == "metadata_and_managed_connectors"
        connector = next(item for item in installed["contributions"] if item["type"] == "managed_connectors")
        assert connector["name"] == "github-managed"
        assert connector["provider"] == "github"
        assert connector["loaded"] is False
        assert connector["enabled"] is False
        assert connector["status"] == "requires_config"

        configure_response = await client.post(
            "/api/extensions/seraph.managed-github/configure",
            json={
                "config": {
                    "managed_connectors": {
                        "github-managed": {
                            "installation_id": "12345",
                            "api_base_url": "https://api.github.com",
                        }
                    }
                }
            },
        )
        assert configure_response.status_code == 200
        configured = configure_response.json()["extension"]
        connector = next(item for item in configured["contributions"] if item["type"] == "managed_connectors")
        assert configured["enabled"] is False
        assert connector["configured"] is True
        assert connector["enabled"] is False
        assert connector["config_keys"] == ["api_base_url", "installation_id"]
        assert connector["status"] == "disabled"

        enable_response = await client.post("/api/extensions/seraph.managed-github/enable")
        assert enable_response.status_code == 200
        enabled_extension = enable_response.json()["extension"]
        enabled_connector = next(
            item for item in enabled_extension["contributions"] if item["type"] == "managed_connectors"
        )
        assert enabled_extension["enabled"] is True
        assert enabled_connector["enabled"] is True
        assert enabled_connector["status"] == "ready"

        disable_response = await client.post("/api/extensions/seraph.managed-github/disable")
        assert disable_response.status_code == 200
        disabled_extension = disable_response.json()["extension"]
        disabled_connector = next(
            item for item in disabled_extension["contributions"] if item["type"] == "managed_connectors"
        )
        assert disabled_extension["enabled"] is False
        assert disabled_connector["enabled"] is False
        assert disabled_connector["status"] == "disabled"

        invalid_config_response = await client.post(
            "/api/extensions/seraph.managed-github/configure",
            json={
                "config": {
                    "managed_connectors": {
                        "github-managed": {
                            "api_base_url": "https://api.github.com",
                        }
                    }
                }
            },
        )
        assert invalid_config_response.status_code == 422
        assert "installation_id" in invalid_config_response.json()["detail"]

        invalid_url_response = await client.post(
            "/api/extensions/seraph.managed-github/configure",
            json={
                "config": {
                    "managed_connectors": {
                        "github-managed": {
                            "installation_id": "12345",
                            "api_base_url": "not-a-url",
                        }
                    }
                }
            },
        )
        assert invalid_url_response.status_code == 422
        assert "valid http or https URL" in invalid_url_response.json()["detail"]

        unknown_connector_response = await client.post(
            "/api/extensions/seraph.managed-github/configure",
            json={
                "config": {
                    "managed_connectors": {
                        "github-shadow": {
                            "installation_id": "12345",
                        }
                    }
                }
            },
        )
        assert unknown_connector_response.status_code == 422
        assert "github-shadow" in unknown_connector_response.json()["detail"]

        remove_response = await client.delete("/api/extensions/seraph.managed-github")
        assert remove_response.status_code == 200
        assert not (extension_runtime / "extensions" / "seraph-managed-github").exists()


@pytest.mark.asyncio
async def test_install_configure_and_toggle_wave2_contribution_surfaces(client, extension_runtime, tmp_path):
    package_dir = _write_wave2_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201
        installed = install_response.json()["extension"]
        assert installed["id"] == "seraph.wave2-pack"
        assert installed["configurable"] is True
        assert installed["config_scope"] == "metadata_and_connector_configs"
        assert installed["toggleable_contribution_types"] == [
            "automation_triggers",
            "browser_providers",
            "messaging_connectors",
            "node_adapters",
        ]
        assert installed["passive_contribution_types"] == [
            "toolset_presets",
            "context_packs",
            "speech_profiles",
        ]

        toolset = next(item for item in installed["contributions"] if item["type"] == "toolset_presets")
        context_pack = next(item for item in installed["contributions"] if item["type"] == "context_packs")
        speech_profile = next(item for item in installed["contributions"] if item["type"] == "speech_profiles")
        assert toolset["include_tools"] == ["read_file"]
        assert context_pack["domains"] == ["research"]
        assert speech_profile["supports_tts"] is True

        connectors_response = await client.get("/api/extensions/seraph.wave2-pack/connectors")
        assert connectors_response.status_code == 200
        connectors = {item["type"]: item for item in connectors_response.json()["connectors"]}
        assert connectors["browser_providers"]["status"] == "requires_config"
        assert connectors["automation_triggers"]["status"] == "requires_config"
        assert connectors["messaging_connectors"]["status"] == "requires_config"
        assert connectors["node_adapters"]["status"] == "requires_config"

        enable_before_config = await client.post(
            "/api/extensions/seraph.wave2-pack/connectors/enabled",
            json={"reference": "connectors/browser/browserbase.yaml", "enabled": True},
        )
        assert enable_before_config.status_code == 422
        assert "requires valid configuration" in enable_before_config.json()["detail"]

        configure_response = await client.post(
            "/api/extensions/seraph.wave2-pack/configure",
            json={
                "config": {
                    "browser_providers": {"browserbase": {"api_key": "secret"}},
                    "messaging_connectors": {"telegram": {"bot_token": "secret"}},
                    "automation_triggers": {"daily-brief": {"signing_secret": "secret"}},
                    "node_adapters": {"companion": {"node_url": "https://nodes.example.test"}},
                }
            },
        )
        assert configure_response.status_code == 200
        configured = configure_response.json()["extension"]
        configured_connectors = {
            item["type"]: item
            for item in configured["contributions"]
            if item["type"] in connectors
        }
        assert configured_connectors["browser_providers"]["configured"] is True
        assert configured_connectors["browser_providers"]["status"] == "disabled"
        assert configured_connectors["messaging_connectors"]["config_keys"] == ["bot_token"]
        assert configured_connectors["automation_triggers"]["config_keys"] == ["signing_secret"]
        assert configured_connectors["node_adapters"]["config_keys"] == ["node_url"]

        enable_browser = await client.post(
            "/api/extensions/seraph.wave2-pack/connectors/enabled",
            json={"reference": "connectors/browser/browserbase.yaml", "enabled": True},
        )
        assert enable_browser.status_code == 200
        browser_connector = enable_browser.json()["connector"]
        assert browser_connector["status"] == "planned"
        assert browser_connector["health"]["supports_configure"] is True
        assert browser_connector["health"]["supports_enable"] is True

        enable_extension = await client.post("/api/extensions/seraph.wave2-pack/enable")
        assert enable_extension.status_code == 200
        enabled_extension = enable_extension.json()["extension"]
        enabled_connectors = {
            item["type"]: item
            for item in enabled_extension["contributions"]
            if item["type"] in connectors
        }
        assert all(enabled_connectors[item]["enabled"] is True for item in enabled_connectors)
        assert enabled_connectors["automation_triggers"]["status"] == "planned"
        assert enabled_connectors["node_adapters"]["status"] == "planned"


@pytest.mark.asyncio
async def test_enable_extension_with_default_disabled_managed_connector_keeps_other_targets_active(client, extension_runtime, tmp_path):
    package_dir = _write_mixed_managed_connector_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        disable_response = await client.post("/api/extensions/seraph.mixed-managed/disable")
        assert disable_response.status_code == 200
        assert workflow_manager.get_workflow("local-workflow") is not None
        assert workflow_manager.get_workflow("local-workflow").enabled is False

        enable_response = await client.post("/api/extensions/seraph.mixed-managed/enable")
        assert enable_response.status_code == 200
        enabled_extension = enable_response.json()["extension"]
        assert workflow_manager.get_workflow("local-workflow") is not None
        assert workflow_manager.get_workflow("local-workflow").enabled is True
        workflow = next(item for item in enabled_extension["contributions"] if item["type"] == "workflows")
        connector = next(item for item in enabled_extension["contributions"] if item["type"] == "managed_connectors")
        assert enabled_extension["enabled"] is True
        assert workflow["enabled"] is True
        assert connector["enabled"] is False


@pytest.mark.asyncio
async def test_mixed_extension_enabled_state_tracks_non_connector_targets_when_managed_connector_defaults_disabled(
    client,
    extension_runtime,
    tmp_path,
):
    package_dir = _write_mixed_managed_connector_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201
        installed = install_response.json()["extension"]

        assert installed["enabled"] is True
        workflow = next(item for item in installed["contributions"] if item["type"] == "workflows")
        connector = next(item for item in installed["contributions"] if item["type"] == "managed_connectors")
        assert workflow["enabled"] is True
        assert connector["enabled"] is False


@pytest.mark.asyncio
async def test_extension_connector_listing_exposes_generic_health_contract(client, extension_runtime, tmp_path):
    mcp_package = _write_mcp_connector_extension(tmp_path)
    managed_package = _write_managed_connector_extension(tmp_path)
    observer_package = _write_observer_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(mcp_package)})
        assert install_response.status_code == 409
        approval_id = install_response.json()["detail"]["approval_id"]
        approve_response = await client.post(f"/api/approvals/{approval_id}/approve")
        assert approve_response.status_code == 200
        install_response = await client.post("/api/extensions/install", json={"path": str(mcp_package)})
        assert install_response.status_code == 201

        managed_install = await client.post("/api/extensions/install", json={"path": str(managed_package)})
        assert managed_install.status_code == 201

        observer_install = await client.post("/api/extensions/install", json={"path": str(observer_package)})
        assert observer_install.status_code == 201

        mcp_connectors = await client.get("/api/extensions/seraph.test-connector/connectors")
        assert mcp_connectors.status_code == 200
        mcp_payload = mcp_connectors.json()
        assert mcp_payload["summary"]["total"] == 1
        assert mcp_payload["summary"]["states"]["disabled"] == 1
        connector = mcp_payload["connectors"][0]
        assert connector["type"] == "mcp_servers"
        assert connector["health"]["state"] == "disabled"
        assert connector["health"]["supports_test"] is True
        assert connector["health"]["supports_enable"] is True

        managed_connectors = await client.get("/api/extensions/seraph.managed-github/connectors")
        assert managed_connectors.status_code == 200
        managed_payload = managed_connectors.json()
        assert managed_payload["summary"]["total"] == 1
        managed_connector = managed_payload["connectors"][0]
        assert managed_connector["type"] == "managed_connectors"
        assert managed_connector["health"]["state"] == "requires_config"
        assert managed_connector["health"]["supports_configure"] is True
        assert managed_connector["health"]["supports_test"] is True
        assert managed_connector["health"]["supports_enable"] is True
        assert managed_connector["health"]["enabled"] is False

        observer_connectors = await client.get("/api/extensions/seraph.calendar-observer/connectors")
        assert observer_connectors.status_code == 200
        observer_payload = observer_connectors.json()
        assert observer_payload["summary"]["total"] == 1
        observer_connector = observer_payload["connectors"][0]
        assert observer_connector["type"] == "observer_definitions"
        assert observer_connector["health"]["state"] == "ready"
        assert observer_connector["health"]["supports_test"] is True
        assert observer_connector["health"]["supports_enable"] is True
        assert observer_connector["health"]["enabled"] is True


@pytest.mark.asyncio
async def test_extension_connector_test_endpoint_uses_packaged_mcp_runtime(client, extension_runtime, tmp_path):
    package_dir = _write_mcp_connector_extension(tmp_path)
    mock_client = SimpleNamespace(
        get_tools=lambda: [SimpleNamespace(name="fetch_repo"), SimpleNamespace(name="list_issues")],
        disconnect=lambda: None,
    )

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ), patch(
        "src.api.extensions.MCPClient",
        return_value=mock_client,
    ) as client_factory, patch.object(
        mcp_manager,
        "connect",
    ) as connect_mock:
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 409
        approval_id = install_response.json()["detail"]["approval_id"]
        approve_response = await client.post(f"/api/approvals/{approval_id}/approve")
        assert approve_response.status_code == 200
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201
        assert mcp_manager.set_token("github-packaged", "secret-token") is True

        enable_response = await client.post("/api/extensions/seraph.test-connector/enable")
        assert enable_response.status_code == 409
        enable_approval_id = enable_response.json()["detail"]["approval_id"]
        approve_enable = await client.post(f"/api/approvals/{enable_approval_id}/approve")
        assert approve_enable.status_code == 200
        enable_response = await client.post("/api/extensions/seraph.test-connector/enable")
        assert enable_response.status_code == 200

        test_response = await client.post(
            "/api/extensions/seraph.test-connector/connectors/test",
            json={"reference": "mcp/github.json"},
        )

        assert test_response.status_code == 200
        payload = test_response.json()
        assert payload["status"] == "ok"
        assert payload["tool_count"] == 2
        assert payload["tools"] == ["fetch_repo", "list_issues"]
        assert payload["health"]["supports_test"] is True
        connect_mock.assert_called_once()
        client_factory.assert_called_once()


@pytest.mark.asyncio
async def test_extension_connector_enable_endpoint_controls_packaged_mcp_runtime(client, extension_runtime, tmp_path):
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
        assert install_response.status_code == 409
        approval_id = install_response.json()["detail"]["approval_id"]
        approve_response = await client.post(f"/api/approvals/{approval_id}/approve")
        assert approve_response.status_code == 200
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        enable_response = await client.post(
            "/api/extensions/seraph.test-connector/connectors/enabled",
            json={"reference": "mcp/github.json", "enabled": True},
        )
        assert enable_response.status_code == 409
        enable_approval_id = enable_response.json()["detail"]["approval_id"]
        approve_enable = await client.post(f"/api/approvals/{enable_approval_id}/approve")
        assert approve_enable.status_code == 200

        enable_response = await client.post(
            "/api/extensions/seraph.test-connector/connectors/enabled",
            json={"reference": "mcp/github.json", "enabled": True},
        )
        assert enable_response.status_code == 200
        payload = enable_response.json()
        assert payload["status"] == "enabled"
        assert payload["connector"]["name"] == "github-packaged"
        assert payload["connector"]["enabled"] is True
        assert payload["changed"]["reference"] == "mcp/github.json"
        assert mcp_manager._config["github-packaged"]["enabled"] is True
        assert mcp_manager._config["github-packaged"]["source"] == "extension"
        assert connect_mock.call_count == 1

        disable_response = await client.post(
            "/api/extensions/seraph.test-connector/connectors/enabled",
            json={"reference": "mcp/github.json", "enabled": False},
        )
        assert disable_response.status_code == 200
        assert disable_response.json()["status"] == "disabled"
        assert mcp_manager._config["github-packaged"]["enabled"] is False
        assert disconnect_mock.call_count == 1


@pytest.mark.asyncio
async def test_whole_extension_enable_repairs_packaged_mcp_ownership_metadata(client, extension_runtime, tmp_path):
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
    ) as connect_mock:
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 409
        approval_id = install_response.json()["detail"]["approval_id"]
        approve_response = await client.post(f"/api/approvals/{approval_id}/approve")
        assert approve_response.status_code == 200
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        for key in ("source", "extension_id", "extension_reference", "extension_display_name"):
            mcp_manager._config["github-packaged"].pop(key, None)

        enable_response = await client.post("/api/extensions/seraph.test-connector/enable")
        assert enable_response.status_code == 409
        enable_approval_id = enable_response.json()["detail"]["approval_id"]
        approve_enable = await client.post(f"/api/approvals/{enable_approval_id}/approve")
        assert approve_enable.status_code == 200

        enable_response = await client.post("/api/extensions/seraph.test-connector/enable")
        assert enable_response.status_code == 200
        assert mcp_manager._config["github-packaged"]["source"] == "extension"
        assert mcp_manager._config["github-packaged"]["extension_id"] == "seraph.test-connector"
        assert mcp_manager._config["github-packaged"]["extension_reference"] == "mcp/github.json"
        assert mcp_manager._config["github-packaged"]["extension_display_name"] == "Test Connector"
        assert connect_mock.call_count == 1


@pytest.mark.asyncio
async def test_extension_connector_test_endpoint_returns_managed_connector_health(client, extension_runtime, tmp_path):
    package_dir = _write_managed_connector_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        first_test = await client.post(
            "/api/extensions/seraph.managed-github/connectors/test",
            json={"reference": "connectors/managed/github.yaml"},
        )
        assert first_test.status_code == 200
        assert first_test.json()["status"] == "requires_config"

        invalid_enable = await client.post(
            "/api/extensions/seraph.managed-github/connectors/enabled",
            json={"reference": "connectors/managed/github.yaml", "enabled": True},
        )
        assert invalid_enable.status_code == 422
        assert "requires valid configuration before enable" in invalid_enable.json()["detail"]

        configure_response = await client.post(
            "/api/extensions/seraph.managed-github/configure",
            json={
                "config": {
                    "managed_connectors": {
                        "github-managed": {
                            "installation_id": "12345",
                            "api_base_url": "https://api.github.com",
                        }
                    }
                }
            },
        )
        assert configure_response.status_code == 200

        second_test = await client.post(
            "/api/extensions/seraph.managed-github/connectors/test",
            json={"reference": "connectors/managed/github.yaml"},
        )
        assert second_test.status_code == 200
        assert second_test.json()["status"] == "disabled"
        assert second_test.json()["health"]["summary"] == "Managed connector is configured but disabled."

        enable_response = await client.post(
            "/api/extensions/seraph.managed-github/connectors/enabled",
            json={"reference": "connectors/managed/github.yaml", "enabled": True},
        )
        assert enable_response.status_code == 200
        assert enable_response.json()["connector"]["enabled"] is True

        third_test = await client.post(
            "/api/extensions/seraph.managed-github/connectors/test",
            json={"reference": "connectors/managed/github.yaml"},
        )
        assert third_test.status_code == 200
        assert third_test.json()["status"] == "ready"
        assert third_test.json()["health"]["summary"] == "Managed connector configuration is valid and ready."


@pytest.mark.asyncio
async def test_install_workspace_observer_extension_exposes_typed_observer_metadata(client, extension_runtime, tmp_path):
    package_dir = _write_observer_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201
        installed = install_response.json()["extension"]
        assert installed["enabled"] is True
        assert installed["toggleable_contribution_types"] == ["observer_definitions"]
        assert installed["passive_contribution_types"] == []
        observer = next(item for item in installed["contributions"] if item["type"] == "observer_definitions")
        assert observer["name"] == "calendar"
        assert observer["source_type"] == "calendar"
        assert observer["default_enabled"] is True
        assert observer["enabled"] is True
        assert observer["requires_network"] is True
        assert observer["loaded"] is True
        assert observer["status"] == "active"
        assert observer["health"]["supports_enable"] is True


@pytest.mark.asyncio
async def test_observer_connector_toggle_updates_extension_and_runtime_selection(client, extension_runtime, tmp_path):
    package_dir = _write_observer_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        disable_response = await client.post(
            "/api/extensions/seraph.calendar-observer/connectors/enabled",
            json={"reference": "observers/definitions/calendar.yaml", "enabled": False},
        )
        assert disable_response.status_code == 200
        disabled_extension = disable_response.json()["extension"]
        disabled_connector = disable_response.json()["connector"]
        assert disabled_extension["enabled"] is False
        assert disabled_connector["enabled"] is False
        assert disabled_connector["status"] == "disabled"

        list_response = await client.get("/api/extensions")
        assert list_response.status_code == 200
        extensions = {item["id"]: item for item in list_response.json()["extensions"]}
        workspace_extension = extensions["seraph.calendar-observer"]
        bundled_extension = extensions["seraph.core-observer-sources"]

        workspace_observer = next(
            item
            for item in workspace_extension["contributions"]
            if item["type"] == "observer_definitions" and item["source_type"] == "calendar"
        )
        bundled_observer = next(
            item
            for item in bundled_extension["contributions"]
            if item["type"] == "observer_definitions" and item["source_type"] == "calendar"
        )
        assert workspace_observer["enabled"] is False
        assert workspace_observer["loaded"] is False
        assert workspace_observer["status"] == "disabled"
        assert bundled_observer["loaded"] is True
        assert bundled_observer["status"] == "active"

        observer_test = await client.post(
            "/api/extensions/seraph.calendar-observer/connectors/test",
            json={"reference": "observers/definitions/calendar.yaml"},
        )
        assert observer_test.status_code == 200
        assert observer_test.json()["status"] == "disabled"
        assert observer_test.json()["message"] == "Observer source is disabled in extension lifecycle state."

        enable_response = await client.post(
            "/api/extensions/seraph.calendar-observer/connectors/enabled",
            json={"reference": "observers/definitions/calendar.yaml", "enabled": True},
        )
        assert enable_response.status_code == 200
        enabled_extension = enable_response.json()["extension"]
        enabled_connector = enable_response.json()["connector"]
        assert enabled_extension["enabled"] is True
        assert enabled_connector["enabled"] is True
        assert enabled_connector["status"] == "active"


@pytest.mark.asyncio
async def test_workspace_observer_extension_overrides_bundled_source_of_same_type(client, extension_runtime, tmp_path):
    package_dir = _write_observer_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        list_response = await client.get("/api/extensions")
        assert list_response.status_code == 200
        extensions = {item["id"]: item for item in list_response.json()["extensions"]}

        workspace_extension = extensions["seraph.calendar-observer"]
        bundled_extension = extensions["seraph.core-observer-sources"]

        workspace_observer = next(
            item
            for item in workspace_extension["contributions"]
            if item["type"] == "observer_definitions" and item["source_type"] == "calendar"
        )
        bundled_observer = next(
            item
            for item in bundled_extension["contributions"]
            if item["type"] == "observer_definitions" and item["source_type"] == "calendar"
        )

        assert workspace_observer["loaded"] is True
        assert workspace_observer["status"] == "active"
        assert bundled_observer["loaded"] is False
        assert bundled_observer["status"] == "overridden"


@pytest.mark.asyncio
async def test_invalid_observer_definition_surfaces_invalid_status_in_extension_payload(client, extension_runtime):
    package_dir = _write_invalid_observer_extension(extension_runtime / "extensions")

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ):
        list_response = await client.get("/api/extensions")
        assert list_response.status_code == 200
        extensions = {item["id"]: item for item in list_response.json()["extensions"]}

    invalid_extension = extensions["seraph.invalid-observer"]
    observer = next(item for item in invalid_extension["contributions"] if item["type"] == "observer_definitions")
    assert observer["loaded"] is False
    assert observer["status"] == "invalid"
    assert invalid_extension["doctor_ok"] is False


@pytest.mark.asyncio
async def test_workspace_channel_adapter_overrides_bundled_transport(client, extension_runtime, tmp_path):
    package_dir = _write_channel_adapter_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        list_response = await client.get("/api/extensions")
        assert list_response.status_code == 200
        extensions = {item["id"]: item for item in list_response.json()["extensions"]}

    workspace_extension = extensions["seraph.native-channel"]
    bundled_extension = extensions["seraph.core-channel-adapters"]

    workspace_adapter = next(
        item
        for item in workspace_extension["contributions"]
        if item["type"] == "channel_adapters" and item["transport"] == "native_notification"
    )
    bundled_adapter = next(
        item
        for item in bundled_extension["contributions"]
        if item["type"] == "channel_adapters" and item["transport"] == "native_notification"
    )

    assert workspace_adapter["loaded"] is True
    assert workspace_adapter["status"] == "degraded"
    assert bundled_adapter["loaded"] is False
    assert bundled_adapter["status"] == "overridden"


@pytest.mark.asyncio
async def test_channel_adapter_toggle_updates_extension_and_runtime_selection(client, extension_runtime, tmp_path):
    package_dir = _write_channel_adapter_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        disable_response = await client.post(
            "/api/extensions/seraph.native-channel/connectors/enabled",
            json={"reference": "channels/native.yaml", "enabled": False},
        )
        assert disable_response.status_code == 200
        disabled_extension = disable_response.json()["extension"]
        disabled_connector = disable_response.json()["connector"]
        assert disabled_extension["enabled"] is False
        assert disabled_connector["enabled"] is False
        assert disabled_connector["status"] == "disabled"

        list_response = await client.get("/api/extensions")
        assert list_response.status_code == 200
        extensions = {item["id"]: item for item in list_response.json()["extensions"]}
        workspace_extension = extensions["seraph.native-channel"]
        bundled_extension = extensions["seraph.core-channel-adapters"]

        workspace_adapter = next(
            item
            for item in workspace_extension["contributions"]
            if item["type"] == "channel_adapters" and item["transport"] == "native_notification"
        )
        bundled_adapter = next(
            item
            for item in bundled_extension["contributions"]
            if item["type"] == "channel_adapters" and item["transport"] == "native_notification"
        )
        assert workspace_adapter["enabled"] is False
        assert workspace_adapter["loaded"] is False
        assert workspace_adapter["status"] == "disabled"
        assert bundled_adapter["loaded"] is True
        assert bundled_adapter["status"] == "degraded"

        channel_test = await client.post(
            "/api/extensions/seraph.native-channel/connectors/test",
            json={"reference": "channels/native.yaml"},
        )
        assert channel_test.status_code == 200
        assert channel_test.json()["status"] == "disabled"
        assert channel_test.json()["message"] == "Channel adapter is disabled in extension lifecycle state."

        enable_response = await client.post(
            "/api/extensions/seraph.native-channel/connectors/enabled",
            json={"reference": "channels/native.yaml", "enabled": True},
        )
        assert enable_response.status_code == 200
        enabled_extension = enable_response.json()["extension"]
        enabled_connector = enable_response.json()["connector"]
        assert enabled_extension["enabled"] is True
        assert enabled_connector["enabled"] is True
        assert enabled_connector["status"] == "degraded"


@pytest.mark.asyncio
async def test_channel_adapter_package_toggle_updates_runtime_selection(client, extension_runtime, tmp_path):
    package_dir = _write_channel_adapter_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        install_response = await client.post("/api/extensions/install", json={"path": str(package_dir)})
        assert install_response.status_code == 201

        disable_response = await client.post("/api/extensions/seraph.native-channel/disable")
        assert disable_response.status_code == 200
        disabled_extension = disable_response.json()["extension"]
        disabled_adapter = next(
            item for item in disabled_extension["contributions"] if item["type"] == "channel_adapters"
        )
        assert disabled_extension["enabled"] is False
        assert disabled_adapter["enabled"] is False
        assert disabled_adapter["status"] == "disabled"

        list_response = await client.get("/api/extensions")
        assert list_response.status_code == 200
        extensions = {item["id"]: item for item in list_response.json()["extensions"]}
        workspace_extension = extensions["seraph.native-channel"]
        bundled_extension = extensions["seraph.core-channel-adapters"]

        workspace_adapter = next(
            item
            for item in workspace_extension["contributions"]
            if item["type"] == "channel_adapters" and item["transport"] == "native_notification"
        )
        bundled_adapter = next(
            item
            for item in bundled_extension["contributions"]
            if item["type"] == "channel_adapters" and item["transport"] == "native_notification"
        )
        assert workspace_adapter["loaded"] is False
        assert workspace_adapter["status"] == "disabled"
        assert bundled_adapter["loaded"] is True
        assert bundled_adapter["status"] == "degraded"

        enable_response = await client.post("/api/extensions/seraph.native-channel/enable")
        assert enable_response.status_code == 200
        enabled_extension = enable_response.json()["extension"]
        enabled_adapter = next(
            item for item in enabled_extension["contributions"] if item["type"] == "channel_adapters"
        )
        assert enabled_extension["enabled"] is True
        assert enabled_adapter["enabled"] is True
        assert enabled_adapter["status"] == "degraded"


@pytest.mark.asyncio
async def test_invalid_channel_adapter_surfaces_invalid_status_in_extension_payload(client, extension_runtime):
    _write_invalid_channel_adapter_extension(extension_runtime / "extensions")

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ):
        list_response = await client.get("/api/extensions")
        assert list_response.status_code == 200
        extensions = {item["id"]: item for item in list_response.json()["extensions"]}

    invalid_extension = extensions["seraph.invalid-channel"]
    adapter = next(item for item in invalid_extension["contributions"] if item["type"] == "channel_adapters")
    assert adapter["loaded"] is False
    assert adapter["status"] == "invalid"
    assert invalid_extension["doctor_ok"] is False


@pytest.mark.asyncio
async def test_extension_connector_test_endpoint_returns_observer_and_channel_health(client, extension_runtime, tmp_path):
    observer_package = _write_observer_extension(tmp_path)
    channel_package = _write_channel_adapter_extension(tmp_path)

    with patch(
        "src.extensions.lifecycle.get_base_tools_and_active_skills",
        return_value=([SimpleNamespace(name="read_file")], [], "approval"),
    ), patch(
        "src.api.extensions.log_integration_event",
        AsyncMock(),
    ):
        observer_install = await client.post("/api/extensions/install", json={"path": str(observer_package)})
        assert observer_install.status_code == 201
        channel_install = await client.post("/api/extensions/install", json={"path": str(channel_package)})
        assert channel_install.status_code == 201

        observer_test = await client.post(
            "/api/extensions/seraph.calendar-observer/connectors/test",
            json={"reference": "observers/definitions/calendar.yaml"},
        )
        assert observer_test.status_code == 200
        assert observer_test.json()["status"] == "ready"
        assert observer_test.json()["message"] == "Observer source is active in the runtime selection."
        assert observer_test.json()["health"]["supports_test"] is True

        channel_test = await client.post(
            "/api/extensions/seraph.native-channel/connectors/test",
            json={"reference": "channels/native.yaml"},
        )
        assert channel_test.status_code == 200
        assert channel_test.json()["status"] == "degraded"
        assert channel_test.json()["message"] == "Channel adapter owns the transport, but the native daemon is offline."
        assert channel_test.json()["health"]["supports_test"] is True
        assert channel_test.json()["health"]["supports_enable"] is True
        assert channel_test.json()["health"]["connected"] is False


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
