from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient
from fastapi import HTTPException
import pytest

from src.api.capabilities import _explicit_runbook_entries, _runbook_labels_by_starter_pack
from src.app import create_app
from src.observer.context import CurrentContext
from src.extensions.registry import bundled_manifest_root, default_manifest_roots_for_workspace


@pytest.fixture
def _setup_manifest_pack_and_runbook_managers(tmp_path):
    from src.runbooks.manager import runbook_manager
    from src.starter_packs.manager import starter_pack_manager

    legacy_starter_packs = tmp_path / "starter-packs.json"
    legacy_starter_packs.write_text('{"packs": []}\n', encoding="utf-8")
    runbooks_dir = tmp_path / "runbooks"
    runbooks_dir.mkdir()

    package_dir = tmp_path / "extensions" / "research-pack"
    (package_dir / "starter-packs").mkdir(parents=True)
    (package_dir / "runbooks").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.research-pack\n"
        "version: 2026.3.21\n"
        "display_name: Research Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  starter_packs:\n"
        "    - starter-packs/research.json\n"
        "  runbooks:\n"
        "    - runbooks/research.yaml\n"
        "permissions:\n"
        "  tools: []\n"
        "  network: false\n",
        encoding="utf-8",
    )
    (package_dir / "starter-packs" / "research.json").write_text(
        "{\n"
        '  "name": "research-briefing",\n'
        '  "label": "Research Briefing",\n'
        '  "description": "Manifest starter pack.",\n'
        '  "skills": ["web-briefing"],\n'
        '  "workflows": ["web-brief-to-file"],\n'
        '  "install_items": ["http-request"],\n'
        '  "sample_prompt": "Search the web and save a concise brief."\n'
        "}\n",
        encoding="utf-8",
    )
    (package_dir / "runbooks" / "research.yaml").write_text(
        "id: runbook:research-briefing\n"
        "title: Research Briefing\n"
        "summary: Run the packaged research workflow.\n"
        "workflow: web-brief-to-file\n",
        encoding="utf-8",
    )

    starter_pack_manager.init(str(legacy_starter_packs), manifest_roots=[str(tmp_path / "extensions")])
    runbook_manager.init(str(runbooks_dir), manifest_roots=[str(tmp_path / "extensions")])
    yield
    starter_pack_manager._packs = []
    starter_pack_manager._load_errors = []
    starter_pack_manager._shared_manifest_errors = []
    starter_pack_manager._legacy_path = ""
    starter_pack_manager._manifest_roots = []
    starter_pack_manager._registry = None
    runbook_manager._runbooks = []
    runbook_manager._load_errors = []
    runbook_manager._shared_manifest_errors = []
    runbook_manager._runbooks_dir = ""
    runbook_manager._manifest_roots = []
    runbook_manager._registry = None


@pytest.fixture
def _setup_bundled_core_capabilities_managers(tmp_path):
    from src.extensions.registry import default_manifest_roots_for_workspace
    from src.runbooks.manager import runbook_manager
    from src.skills.manager import skill_manager
    from src.starter_packs.manager import starter_pack_manager
    from src.workflows.manager import workflow_manager

    workspace_dir = tmp_path / "workspace"
    skills_dir = workspace_dir / "skills"
    workflows_dir = workspace_dir / "workflows"
    runbooks_dir = workspace_dir / "runbooks"
    starter_packs_path = workspace_dir / "starter-packs.json"
    skills_dir.mkdir(parents=True)
    workflows_dir.mkdir()
    runbooks_dir.mkdir()
    manifest_roots = default_manifest_roots_for_workspace(str(workspace_dir))

    skill_manager.init(str(skills_dir), manifest_roots=manifest_roots)
    workflow_manager.init(str(workflows_dir), manifest_roots=manifest_roots)
    runbook_manager.init(str(runbooks_dir), manifest_roots=manifest_roots)
    starter_pack_manager.init(str(starter_packs_path), manifest_roots=manifest_roots)

    yield workspace_dir

    skill_manager._skills = []
    skill_manager._load_errors = []
    skill_manager._skills_dir = ""
    skill_manager._manifest_roots = []
    skill_manager._config_path = ""
    skill_manager._disabled = set()
    skill_manager._registry = None
    workflow_manager._workflows = []
    workflow_manager._load_errors = []
    workflow_manager._shared_manifest_errors = []
    workflow_manager._workflows_dir = ""
    workflow_manager._manifest_roots = []
    workflow_manager._config_path = ""
    workflow_manager._disabled = set()
    workflow_manager._registry = None
    runbook_manager._runbooks = []
    runbook_manager._load_errors = []
    runbook_manager._shared_manifest_errors = []
    runbook_manager._runbooks_dir = ""
    runbook_manager._manifest_roots = []
    runbook_manager._registry = None
    starter_pack_manager._packs = []
    starter_pack_manager._load_errors = []
    starter_pack_manager._shared_manifest_errors = []
    starter_pack_manager._legacy_path = ""
    starter_pack_manager._manifest_roots = []
    starter_pack_manager._registry = None


def test_load_starter_packs_does_not_fallback_when_manager_is_initialized():
    with (
        patch("src.api.capabilities.starter_pack_manager.list_packs", return_value=[]),
        patch("src.api.capabilities.starter_pack_manager.is_initialized", return_value=True),
        patch("src.api.capabilities.StarterPackManager") as fallback_manager_cls,
    ):
        from src.api.capabilities import _load_starter_packs

        assert _load_starter_packs() == []

    fallback_manager_cls.assert_not_called()


def test_attach_skill_actions_uses_extension_enable_for_packaged_disabled_skills():
    from src.api.capabilities import _attach_skill_actions

    skills = [
        {
            "name": "web-briefing",
            "enabled": False,
            "extension_id": "seraph.research-pack",
            "missing_tools": [],
        }
    ]

    with patch(
        "src.api.capabilities.get_extension",
        return_value={
            "id": "seraph.research-pack",
            "display_name": "Research Pack",
            "enable_supported": True,
        },
    ):
        _attach_skill_actions(skills, native_tools=[], tool_mode="balanced")

    assert skills[0]["recommended_actions"] == [
        {
            "type": "enable_extension",
            "label": "Enable Research Pack",
            "name": "seraph.research-pack",
            "target": "Research Pack",
        }
    ]


def test_attach_workflow_actions_uses_extension_enable_for_packaged_disabled_workflows():
    from src.api.capabilities import _attach_workflow_actions

    workflows = [
        {
            "name": "web-brief-to-file",
            "enabled": False,
            "extension_id": "seraph.research-pack",
            "missing_skills": ["web-briefing"],
            "missing_tools": [],
        }
    ]
    skills_by_name = {
        "web-briefing": {
            "name": "web-briefing",
            "enabled": False,
            "extension_id": "seraph.research-pack",
            "missing_tools": [],
        }
    }

    with patch(
        "src.api.capabilities.get_extension",
        return_value={
            "id": "seraph.research-pack",
            "display_name": "Research Pack",
            "enable_supported": True,
        },
    ):
        _attach_workflow_actions(
            workflows,
            native_tools=[],
            skills_by_name=skills_by_name,
            tool_mode="balanced",
        )

    assert workflows[0]["recommended_actions"] == [
        {
            "type": "enable_extension",
            "label": "Enable Research Pack",
            "name": "seraph.research-pack",
            "target": "Research Pack",
        }
    ]


def test_mcp_status_list_exposes_packaged_toolset_presets(tmp_path):
    workspace_dir = tmp_path / "workspace"
    package_dir = workspace_dir / "extensions" / "github-pack"
    (package_dir / "presets" / "toolset").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.github-pack\n"
        "version: 2026.3.23\n"
        "display_name: GitHub Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  toolset_presets:\n"
        "    - presets/toolset/github-operator.yaml\n"
        "permissions:\n"
        "  network: true\n",
        encoding="utf-8",
    )
    (package_dir / "presets" / "toolset" / "github-operator.yaml").write_text(
        "name: github-operator\n"
        "description: GitHub MCP operator preset\n"
        "include_mcp_servers:\n"
        "  - github\n"
        "execution_boundaries:\n"
        "  - external_mcp\n",
        encoding="utf-8",
    )

    with (
        patch("src.api.capabilities.settings.workspace_dir", str(workspace_dir)),
        patch(
            "src.api.capabilities.mcp_manager.get_config",
            return_value=[
                {
                    "name": "github",
                    "enabled": True,
                    "status": "connected",
                    "tool_count": 2,
                    "description": "GitHub MCP",
                }
            ],
        ),
        patch(
            "src.api.capabilities.mcp_manager.get_server_tools",
            return_value=[
                SimpleNamespace(name="github_list_issues"),
                SimpleNamespace(name="github_get_pull_request"),
            ],
        ),
    ):
        from src.api.capabilities import _mcp_status_list

        servers = _mcp_status_list("full")

    assert servers[0]["tool_names"] == ["github_get_pull_request", "github_list_issues"]
    assert servers[0]["toolset_presets"] == [
        {
            "name": "github-operator",
            "description": "GitHub MCP operator preset",
            "reference": "presets/toolset/github-operator.yaml",
            "extension_id": "seraph.github-pack",
            "extension_display_name": "GitHub Pack",
        }
    ]


def test_doctor_reports_missing_mcp_server_reference_for_toolset_preset(tmp_path):
    package_dir = tmp_path / "extensions" / "github-pack"
    (package_dir / "presets" / "toolset").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.github-pack\n"
        "version: 2026.3.23\n"
        "display_name: GitHub Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  toolset_presets:\n"
        "    - presets/toolset/github-operator.yaml\n"
        "permissions:\n"
        "  network: true\n",
        encoding="utf-8",
    )
    (package_dir / "presets" / "toolset" / "github-operator.yaml").write_text(
        "name: github-operator\n"
        "description: GitHub MCP operator preset\n"
        "include_mcp_servers:\n"
        "  - github-typo\n"
        "execution_boundaries:\n"
        "  - external_mcp\n",
        encoding="utf-8",
    )

    from src.extensions.doctor import doctor_snapshot
    from src.extensions.registry import ExtensionRegistry

    snapshot = ExtensionRegistry(
        manifest_roots=[str(tmp_path / "extensions")],
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    report = doctor_snapshot(snapshot)

    result = next(item for item in report.results if item.extension_id == "seraph.github-pack")
    assert any(issue.code == "missing_mcp_server_reference" for issue in result.issues)


@pytest.mark.asyncio
async def test_capabilities_overview_includes_catalog_extension_packs():
    catalog_payload = {
        "skills": [],
        "mcp_servers": [],
        "extension_packages": [
            {
                "name": "Hermes Session Memory",
                "catalog_id": "seraph.hermes-session-memory",
                "type": "extension_pack",
                "description": "Optional Hermes-style session recall and checklist skills.",
                "category": "capability-pack",
                "installed": False,
                "bundled": True,
                "trust": "bundled",
                "version": "2026.3.23",
                "installed_version": None,
                "update_available": False,
                "contribution_types": ["context_packs", "skills"],
            }
        ],
    }

    with (
        patch("src.api.capabilities.load_catalog_items", return_value=catalog_payload),
        patch("src.api.capabilities.catalog_skill_by_name", return_value={}),
        patch("src.api.capabilities._tool_status_list", return_value=[]),
        patch("src.api.capabilities._skill_status_map", return_value=([], {})),
        patch("src.api.capabilities._workflow_status_map", return_value=([], {})),
        patch("src.api.capabilities._mcp_status_list", return_value=[]),
        patch("src.api.capabilities._starter_pack_statuses", return_value=[]),
        patch("src.api.capabilities._load_explicit_runbooks", return_value=[]),
        patch("src.api.capabilities.get_base_tools_and_active_skills", return_value=([], [], "disabled")),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=create_app()),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/capabilities/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["catalog_items"] == [
        {
            "name": "Hermes Session Memory",
            "catalog_id": "seraph.hermes-session-memory",
            "type": "extension_pack",
            "description": "Optional Hermes-style session recall and checklist skills.",
            "category": "capability-pack",
            "bundled": True,
            "installed": False,
            "update_available": False,
            "version": "2026.3.23",
            "version_line": None,
            "installed_version": None,
            "compatibility": None,
            "publisher": None,
            "trust": "bundled",
            "contribution_types": ["context_packs", "skills"],
            "status": "ready",
            "doctor_ok": True,
            "issues": [],
            "load_errors": [],
            "diagnostics_summary": None,
            "recommended_actions": [
                {
                    "type": "install_catalog_item",
                    "label": "Install pack",
                    "name": "seraph.hermes-session-memory",
                }
            ],
        }
    ]
    assert payload["recommendations"] == [
        {
            "id": "catalog-extension:seraph.hermes-session-memory",
            "label": "Install pack Hermes Session Memory",
            "description": "Optional Hermes-style session recall and checklist skills.",
            "action": {
                "type": "install_catalog_item",
                "label": "Install pack",
                "name": "seraph.hermes-session-memory",
            },
        }
    ]
    assert payload["marketplace_flows"] == [
        {
            "id": "extension-pack:seraph.hermes-session-memory",
            "label": "Hermes Session Memory",
            "kind": "extension_pack",
            "availability": "ready",
            "summary": "Optional Hermes-style session recall and checklist skills.",
            "detail": "trust bundled · 2 contribution types",
            "ready_count": 1,
            "total_count": 1,
            "primary_action": {
                "type": "install_catalog_item",
                "label": "Install pack",
                "name": "seraph.hermes-session-memory",
            },
            "recommended_actions": [
                {
                    "type": "install_catalog_item",
                    "label": "Install pack",
                    "name": "seraph.hermes-session-memory",
                }
            ],
            "draft_command": None,
            "blocking_reasons": [],
            "install_items": [],
            "skills": [],
            "workflows": [],
            "related_runbooks": [],
            "catalog_id": "seraph.hermes-session-memory",
            "installed": False,
            "update_available": False,
            "version": "2026.3.23",
            "version_line": None,
            "installed_version": None,
            "contribution_types": ["context_packs", "skills"],
            "trust": "bundled",
            "publisher": None,
            "compatibility": None,
            "diagnostics_summary": None,
            "status": "ready",
        }
    ]


def test_explicit_runbook_entries_preserve_starter_pack_linkage():
    entries, _, _ = _explicit_runbook_entries(
        [
            {
                "id": "runbook:research-briefing",
                "title": "Research Briefing",
                "summary": "Run the packaged research workflow.",
                "starter_pack": "research-briefing",
            }
        ],
        workflows_by_name={},
        starter_packs_by_name={
            "research-briefing": {
                "name": "research-briefing",
                "availability": "ready",
                "recommended_actions": [],
                "sample_prompt": "Research the latest release notes",
            }
        },
    )

    assert entries[0]["starter_pack_name"] == "research-briefing"
    assert _runbook_labels_by_starter_pack(entries) == {
        "research-briefing": ["Research Briefing"],
    }


@pytest.mark.asyncio
async def test_capabilities_overview_surfaces_extension_pack_update_by_catalog_id():
    catalog_payload = {
        "skills": [],
        "mcp_servers": [],
        "extension_packages": [
            {
                "name": "Hermes Session Memory",
                "catalog_id": "seraph.hermes-session-memory",
                "type": "extension_pack",
                "description": "Optional Hermes-style session recall and checklist skills.",
                "category": "capability-pack",
                "installed": True,
                "bundled": True,
                "trust": "bundled",
                "version": "2026.3.23",
                "installed_version": "2026.3.20",
                "update_available": True,
                "contribution_types": ["context_packs", "skills"],
                "status": "ready",
                "doctor_ok": True,
                "issues": [],
                "load_errors": [],
            }
        ],
    }

    with (
        patch("src.api.capabilities.load_catalog_items", return_value=catalog_payload),
        patch("src.api.capabilities.catalog_skill_by_name", return_value={}),
        patch("src.api.capabilities._tool_status_list", return_value=[]),
        patch("src.api.capabilities._skill_status_map", return_value=([], {})),
        patch("src.api.capabilities._workflow_status_map", return_value=([], {})),
        patch("src.api.capabilities._mcp_status_list", return_value=[]),
        patch("src.api.capabilities._starter_pack_statuses", return_value=[]),
        patch("src.api.capabilities._load_explicit_runbooks", return_value=[]),
        patch("src.api.capabilities.get_base_tools_and_active_skills", return_value=([], [], "disabled")),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=create_app()),
            base_url="http://test",
        ) as client:
            response = await client.get("/api/capabilities/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["catalog_items"][0]["recommended_actions"] == [
        {
            "type": "install_catalog_item",
            "label": "Update pack",
            "name": "seraph.hermes-session-memory",
        }
    ]
    assert payload["recommendations"] == [
        {
            "id": "catalog-extension:seraph.hermes-session-memory",
            "label": "Update pack Hermes Session Memory",
            "description": "Optional Hermes-style session recall and checklist skills.",
            "action": {
                "type": "install_catalog_item",
                "label": "Update pack",
                "name": "seraph.hermes-session-memory",
            },
        }
    ]


@pytest.mark.asyncio
async def test_capabilities_overview_aggregates_blocked_states_and_starter_packs():
    ctx = CurrentContext(tool_policy_mode="balanced", mcp_policy_mode="approval", approval_mode="high_risk")
    with (
        patch(
            "src.api.capabilities.get_base_tools_and_active_skills",
            return_value=(
                [SimpleNamespace(name="web_search"), SimpleNamespace(name="get_goals")],
                ["goal-reflection"],
                "approval",
            ),
        ),
        patch("src.api.capabilities.context_manager.get_context", return_value=ctx),
        patch(
            "src.api.capabilities.skill_manager.list_skills",
            return_value=[
                {
                    "name": "goal-reflection",
                    "description": "Reflect on goals",
                    "requires_tools": ["get_goals"],
                    "user_invocable": True,
                    "enabled": True,
                    "file_path": "/tmp/goal-reflection.md",
                },
                {
                    "name": "web-briefing",
                    "description": "Web briefing",
                    "requires_tools": ["web_search", "write_file"],
                    "user_invocable": True,
                    "enabled": True,
                    "file_path": "/tmp/web-briefing.md",
                },
                {
                    "name": "daily-standup",
                    "description": "Standup",
                    "requires_tools": ["execute_code"],
                    "user_invocable": True,
                    "enabled": False,
                    "file_path": "/tmp/daily-standup.md",
                },
            ],
        ),
        patch(
            "src.api.capabilities.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "goal-snapshot-to-file",
                    "tool_name": "workflow_goal_snapshot_to_file",
                    "description": "Save goals",
                    "requires_tools": ["get_goals", "write_file"],
                    "requires_skills": ["goal-reflection"],
                    "user_invocable": True,
                    "enabled": True,
                    "step_count": 2,
                    "file_path": "/tmp/goal-snapshot-to-file.md",
                    "policy_modes": ["balanced", "full"],
                    "execution_boundaries": ["guardian_state_read", "workspace_write"],
                    "risk_level": "medium",
                    "accepts_secret_refs": False,
                    "is_available": False,
                    "missing_tools": ["write_file"],
                    "missing_skills": [],
                },
                {
                    "name": "web-brief-to-file",
                    "tool_name": "workflow_web_brief_to_file",
                    "description": "Research and save",
                    "requires_tools": ["web_search", "write_file"],
                    "requires_skills": [],
                    "user_invocable": True,
                    "enabled": True,
                    "step_count": 2,
                    "file_path": "/tmp/web-brief-to-file.md",
                    "policy_modes": ["balanced", "full"],
                    "execution_boundaries": ["external_read", "workspace_write"],
                    "risk_level": "medium",
                    "accepts_secret_refs": False,
                    "is_available": False,
                    "missing_tools": ["write_file"],
                    "missing_skills": [],
                },
            ],
        ),
        patch(
            "src.api.capabilities.mcp_manager.get_config",
            return_value=[
                {
                    "name": "github",
                    "enabled": True,
                    "status": "auth_required",
                    "status_message": "Missing token",
                    "tool_count": 0,
                    "connected": False,
                }
            ],
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=create_app()),
            base_url="http://test",
        ) as client:
            resp = await client.get("/api/capabilities/overview")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["tool_policy_mode"] == "balanced"
    assert payload["mcp_policy_mode"] == "approval"
    assert payload["approval_mode"] == "high_risk"

    execute_code_tool = next(tool for tool in payload["native_tools"] if tool["name"] == "execute_code")
    assert execute_code_tool["availability"] == "blocked"
    assert execute_code_tool["blocked_reason"] == "tool_policy_balanced"
    assert execute_code_tool["recommended_actions"][0]["type"] == "set_tool_policy"

    web_briefing = next(skill for skill in payload["skills"] if skill["name"] == "web-briefing")
    assert web_briefing["availability"] == "blocked"
    assert web_briefing["missing_tools"] == ["write_file"]
    assert web_briefing["recommended_actions"][0]["type"] == "set_tool_policy"

    goal_snapshot = next(workflow for workflow in payload["workflows"] if workflow["name"] == "goal-snapshot-to-file")
    assert goal_snapshot["availability"] == "blocked"
    assert goal_snapshot["missing_tools"] == ["write_file"]
    assert goal_snapshot["recommended_actions"][0]["type"] == "set_tool_policy"

    github = payload["mcp_servers"][0]
    assert github["availability"] == "blocked"
    assert github["blocked_reason"] == "auth_required"
    assert github["recommended_actions"][0]["type"] == "test_mcp_server"

    research_pack = next(pack for pack in payload["starter_packs"] if pack["name"] == "research-briefing")
    assert research_pack["availability"] == "blocked"
    assert research_pack["install_items"] == ["http-request"]
    assert research_pack["missing_install_items"] == ["http-request"]
    assert research_pack["blocked_skills"][0]["name"] == "web-briefing"
    assert not any(action["type"] == "activate_starter_pack" for action in research_pack["recommended_actions"])
    assert any(action["type"] == "install_catalog_item" for action in research_pack["recommended_actions"])
    assert any(action["type"] == "set_tool_policy" for action in research_pack["recommended_actions"])
    assert any(item["type"] == "skill" and item["name"] == "daily-standup" for item in payload["catalog_items"])
    assert any(item["type"] == "mcp_server" and item["name"] == "github" for item in payload["catalog_items"])
    assert payload["recommendations"]
    assert payload["runbooks"]


@pytest.mark.asyncio
async def test_capabilities_overview_includes_manifest_starter_pack_and_explicit_runbook(
    _setup_manifest_pack_and_runbook_managers,
):
    ctx = CurrentContext(tool_policy_mode="balanced", mcp_policy_mode="approval", approval_mode="high_risk")
    with (
        patch(
            "src.api.capabilities.get_base_tools_and_active_skills",
            return_value=(
                [SimpleNamespace(name="web_search"), SimpleNamespace(name="write_file")],
                ["web-briefing"],
                "approval",
            ),
        ),
        patch("src.api.capabilities.context_manager.get_context", return_value=ctx),
        patch(
            "src.api.capabilities.skill_manager.list_skills",
            return_value=[
                {
                    "name": "web-briefing",
                    "description": "Web briefing",
                    "requires_tools": ["web_search", "write_file"],
                    "user_invocable": True,
                    "enabled": True,
                    "file_path": "/tmp/web-briefing.md",
                    "source": "manifest",
                    "extension_id": "seraph.research-pack",
                },
            ],
        ),
        patch(
            "src.api.capabilities.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "web-brief-to-file",
                    "tool_name": "workflow_web_brief_to_file",
                    "description": "Research and save",
                    "requires_tools": ["web_search", "write_file"],
                    "requires_skills": [],
                    "user_invocable": True,
                    "enabled": True,
                    "is_available": True,
                    "missing_tools": [],
                    "missing_skills": [],
                    "inputs": {
                        "query": {"type": "string", "required": True},
                        "file_path": {"type": "string", "required": True},
                    },
                    "risk_level": "medium",
                    "execution_boundaries": ["external_read", "workspace_write"],
                    "source": "manifest",
                    "extension_id": "seraph.research-pack",
                },
            ],
        ),
        patch("src.api.capabilities.mcp_manager.get_config", return_value=[]),
        patch("src.api.capabilities.load_catalog_items", return_value={"skills": [], "mcp_servers": []}),
        patch("src.api.capabilities.catalog_skill_by_name", return_value={}),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=create_app()),
            base_url="http://test",
        ) as client:
            resp = await client.get("/api/capabilities/overview")

    assert resp.status_code == 200
    payload = resp.json()
    pack = next(pack for pack in payload["starter_packs"] if pack["name"] == "research-briefing")
    assert pack["source"] == "manifest"
    assert pack["extension_id"] == "seraph.research-pack"
    assert pack["file_path"].endswith("starter-packs/research.json")

    runbooks = {item["id"]: item for item in payload["runbooks"]}
    explicit = runbooks["runbook:research-briefing"]
    assert explicit["source"] == "extension_runbook"
    assert explicit["extension_id"] == "seraph.research-pack"
    assert explicit["availability"] == "ready"
    assert explicit["action"]["type"] == "draft_workflow"
    assert explicit["command"].startswith('Run workflow "web-brief-to-file"')
    assert "workflow:web-brief-to-file" not in runbooks


@pytest.mark.asyncio
async def test_activate_starter_pack_enables_seeded_assets(client):
    def install_side_effect(name: str):
        if name == "http-request":
            return {"ok": True, "status": "installed", "name": name, "type": "mcp_server", "bundled": True}
        if name == "web-briefing":
            return {"ok": True, "status": "installed", "name": name, "type": "skill", "bundled": True}
        return {"ok": False, "status": "not_found", "name": name, "type": "unknown", "bundled": False}

    with (
        patch("src.api.capabilities.skill_manager.get_skill", return_value=None),
        patch("src.api.capabilities.workflow_manager.get_workflow", return_value=None),
        patch("src.api.capabilities.require_catalog_install_approval", AsyncMock()),
        patch("src.api.capabilities.install_catalog_item_by_name", side_effect=install_side_effect),
        patch("src.api.capabilities._ensure_bundled_workflow_available", return_value=True),
        patch("src.api.capabilities.workflow_manager.reload", return_value=[]),
        patch("src.api.capabilities.skill_manager.enable", return_value=True) as enable_skill,
        patch("src.api.capabilities.workflow_manager.enable", return_value=True) as enable_workflow,
        patch(
            "src.api.capabilities._build_capability_overview",
            side_effect=[
                {"starter_packs": [{"name": "research-briefing", "label": "Research Briefing", "description": "", "sample_prompt": "Search the web.", "availability": "blocked", "recommended_actions": [{"type": "install_catalog_item", "name": "http-request"}], "install_items": ["http-request"]}], "summary": {"starter_packs_ready": 0}},
                {"starter_packs": [{"name": "research-briefing", "label": "Research Briefing", "description": "", "sample_prompt": "Search the web.", "availability": "ready", "recommended_actions": [], "install_items": ["http-request"]}], "summary": {"starter_packs_ready": 1}},
                {"starter_packs": [{"name": "research-briefing", "label": "Research Briefing", "description": "", "sample_prompt": "Search the web.", "availability": "ready", "recommended_actions": [], "install_items": ["http-request"]}], "summary": {"starter_packs_ready": 1}},
            ],
        ),
    ):
        resp = await client.post("/api/capabilities/starter-packs/research-briefing/activate")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "activated"
    assert payload["name"] == "research-briefing"
    assert payload["installed_catalog_items"] == [{"name": "http-request", "type": "mcp_server", "status": "installed"}]
    assert payload["enabled_skills"] == ["web-briefing"]
    assert payload["enabled_workflows"] == ["web-brief-to-file"]
    assert payload["overview"]["summary"]["starter_packs_ready"] == 1
    assert payload["doctor_plan_before"]["install_actions"][0]["type"] == "install_catalog_item"
    assert payload["doctor_plan_after"]["ready"] is True
    enable_skill.assert_called_with("web-briefing")
    enable_workflow.assert_called_with("web-brief-to-file")


@pytest.mark.asyncio
async def test_activate_manifest_backed_starter_pack_works(client, _setup_manifest_pack_and_runbook_managers):
    def install_side_effect(name: str):
        if name == "http-request":
            return {"ok": True, "status": "installed", "name": name, "type": "mcp_server", "bundled": False}
        if name == "web-briefing":
            return {"ok": True, "status": "installed", "name": name, "type": "skill", "bundled": False}
        return {"ok": False, "status": "not_found", "name": name, "type": "unknown", "bundled": False}

    with (
        patch("src.api.capabilities.skill_manager.get_skill", return_value=None),
        patch("src.api.capabilities.workflow_manager.get_workflow", return_value=SimpleNamespace(name="web-brief-to-file")),
        patch("src.api.capabilities.require_catalog_install_approval", AsyncMock()),
        patch("src.api.capabilities.install_catalog_item_by_name", side_effect=install_side_effect) as install_item,
        patch("src.api.capabilities.skill_manager.enable", return_value=True) as enable_skill,
        patch("src.api.capabilities.workflow_manager.enable", return_value=True) as enable_workflow,
        patch(
            "src.api.capabilities._build_capability_overview",
            side_effect=[
                {
                    "starter_packs": [
                        {
                            "name": "research-briefing",
                            "label": "Research Briefing",
                            "description": "Manifest starter pack.",
                            "sample_prompt": "Search the web and save a concise brief.",
                            "availability": "blocked",
                            "recommended_actions": [{"type": "install_catalog_item", "name": "http-request"}],
                            "install_items": ["http-request"],
                        }
                    ],
                    "summary": {"starter_packs_ready": 0},
                },
                {
                    "starter_packs": [
                        {
                            "name": "research-briefing",
                            "label": "Research Briefing",
                            "description": "Manifest starter pack.",
                            "sample_prompt": "Search the web and save a concise brief.",
                            "availability": "ready",
                            "recommended_actions": [],
                            "install_items": ["http-request"],
                        }
                    ],
                    "summary": {"starter_packs_ready": 1},
                },
                {
                    "starter_packs": [
                        {
                            "name": "research-briefing",
                            "label": "Research Briefing",
                            "description": "Manifest starter pack.",
                            "sample_prompt": "Search the web and save a concise brief.",
                            "availability": "ready",
                            "recommended_actions": [],
                            "install_items": ["http-request"],
                        }
                    ],
                    "summary": {"starter_packs_ready": 1},
                },
            ],
        ),
    ):
        resp = await client.post("/api/capabilities/starter-packs/research-briefing/activate")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "activated"
    assert payload["name"] == "research-briefing"
    assert payload["installed_catalog_items"] == [{"name": "http-request", "type": "mcp_server", "status": "installed"}]
    assert payload["enabled_skills"] == ["web-briefing"]
    assert payload["enabled_workflows"] == ["web-brief-to-file"]
    assert payload["overview"]["summary"]["starter_packs_ready"] == 1
    assert payload["doctor_plan_before"]["install_actions"][0]["type"] == "install_catalog_item"
    assert payload["doctor_plan_after"]["ready"] is True
    assert [call.args[0] for call in install_item.call_args_list] == ["http-request", "web-briefing"]
    enable_skill.assert_called_with("web-briefing")
    enable_workflow.assert_called_with("web-brief-to-file")


@pytest.mark.asyncio
async def test_activate_bundled_core_capability_pack_uses_manifest_runtime(_setup_bundled_core_capabilities_managers):
    from src.api.capabilities import _activate_starter_pack_by_name

    workspace_dir = _setup_bundled_core_capabilities_managers
    install_calls: list[str] = []

    def install_side_effect(name: str):
        install_calls.append(name)
        if name == "http-request":
            return {"ok": True, "status": "installed", "name": name, "type": "mcp_server", "bundled": True}
        return {"ok": False, "status": "not_found", "name": name, "type": "unknown", "bundled": False}

    with (
        patch("src.api.capabilities.require_catalog_install_approval", AsyncMock()),
        patch("src.api.capabilities.install_catalog_item_by_name", side_effect=install_side_effect),
        patch(
            "src.api.capabilities.get_base_tools_and_active_skills",
            return_value=(
                [
                    SimpleNamespace(name="web_search"),
                    SimpleNamespace(name="write_file"),
                    SimpleNamespace(name="http_request"),
                ],
                ["web-briefing"],
                "approval",
            ),
        ),
        patch(
            "src.api.capabilities._mcp_status_list",
            return_value=[{"name": "http-request", "availability": "ready"}],
        ),
        patch("src.api.capabilities.log_integration_event", AsyncMock()),
    ):
        payload = await _activate_starter_pack_by_name("research-briefing")

    assert payload["status"] == "activated"
    assert payload["enabled_skills"] == ["web-briefing"]
    assert payload["enabled_workflows"] == ["web-brief-to-file"]
    assert install_calls == ["http-request"]
    assert not (workspace_dir / "workflows" / "web-brief-to-file.md").exists()


@pytest.mark.asyncio
async def test_activate_bundled_core_capability_pack_uses_real_catalog_install(_setup_bundled_core_capabilities_managers):
    from src.api.capabilities import _activate_starter_pack_by_name
    from src.api.catalog import install_catalog_item_by_name as real_install_catalog_item_by_name
    from src.tools.mcp_manager import mcp_manager

    original_config = dict(mcp_manager._config)
    original_status = dict(mcp_manager._status)
    original_config_path = mcp_manager._config_path
    mcp_manager._config = {}
    mcp_manager._status = {}
    mcp_manager._config_path = None

    try:
        with (
            patch("src.api.capabilities.require_catalog_install_approval", AsyncMock()),
            patch(
                "src.api.capabilities.install_catalog_item_by_name",
                side_effect=real_install_catalog_item_by_name,
            ) as install_item,
            patch(
                "src.api.capabilities.get_base_tools_and_active_skills",
                return_value=(
                    [
                        SimpleNamespace(name="web_search"),
                        SimpleNamespace(name="write_file"),
                        SimpleNamespace(name="http_request"),
                    ],
                    ["web-briefing"],
                    "approval",
                ),
            ),
            patch(
                "src.api.capabilities._mcp_status_list",
                return_value=[{"name": "http-request", "availability": "ready"}],
            ),
            patch("src.api.capabilities.log_integration_event", AsyncMock()),
        ):
            payload = await _activate_starter_pack_by_name("research-briefing")
    finally:
        mcp_manager._config = original_config
        mcp_manager._status = original_status
        mcp_manager._config_path = original_config_path

    assert payload["status"] == "activated"
    assert payload["installed_catalog_items"] == [
        {"name": "http-request", "type": "mcp_server", "status": "installed"}
    ]
    assert payload["enabled_skills"] == ["web-briefing"]
    assert payload["enabled_workflows"] == ["web-brief-to-file"]
    assert [call.args[0] for call in install_item.call_args_list] == ["http-request"]


@pytest.mark.asyncio
async def test_activate_starter_pack_requires_catalog_install_approval(client):
    with (
        patch(
            "src.api.capabilities.require_catalog_install_approval",
            AsyncMock(side_effect=HTTPException(status_code=409, detail={"type": "approval_required", "approval_id": "approval-catalog-install"})),
        ),
        patch("src.api.capabilities.install_catalog_item_by_name") as install_item,
    ):
        resp = await client.post("/api/capabilities/starter-packs/research-briefing/activate")

    assert resp.status_code == 409
    assert resp.json()["detail"]["type"] == "approval_required"
    install_item.assert_not_called()


@pytest.mark.asyncio
async def test_activate_starter_pack_preflights_all_approvals_without_consuming_them(client):
    from src.api.capabilities import _activate_starter_pack_by_name

    events: list[tuple[str, str, bool | None]] = []

    async def approval_side_effect(name: str, *, consume: bool = True):
        events.append(("approval", name, consume))

    def install_side_effect(name: str):
        events.append(("install", name, None))
        return {"ok": True, "status": "installed", "name": name, "type": "mcp_server", "bundled": True}

    with (
        patch(
            "src.api.capabilities._load_starter_packs",
            return_value=[
                {
                    "name": "privileged-pack",
                    "label": "Privileged Pack",
                    "description": "",
                    "skills": [],
                    "workflows": [],
                    "install_items": ["alpha", "beta"],
                }
            ],
        ),
        patch(
            "src.api.capabilities.require_catalog_install_approval",
            AsyncMock(side_effect=approval_side_effect),
        ),
        patch("src.api.capabilities.install_catalog_item_by_name", side_effect=install_side_effect),
        patch(
            "src.api.capabilities._build_capability_overview",
            return_value={
                "starter_packs": [
                    {
                        "name": "privileged-pack",
                        "availability": "ready",
                        "missing_install_items": [],
                        "blocked_skills": [],
                        "blocked_workflows": [],
                    }
                ]
            },
        ),
        patch("src.api.capabilities.log_integration_event", AsyncMock()),
    ):
        payload = await _activate_starter_pack_by_name("privileged-pack")

    assert payload["status"] == "activated"
    assert payload["installed_catalog_items"] == [
        {"name": "alpha", "type": "mcp_server", "status": "installed"},
        {"name": "beta", "type": "mcp_server", "status": "installed"},
    ]
    assert events == [
        ("approval", "alpha", False),
        ("approval", "beta", False),
        ("approval", "alpha", True),
        ("install", "alpha", None),
        ("approval", "beta", True),
        ("install", "beta", None),
    ]


def test_ensure_bundled_workflow_available_preserves_existing_manager_roots(tmp_path):
    from src.api.capabilities import _ensure_bundled_workflow_available
    from src.workflows.manager import workflow_manager

    custom_workspace = tmp_path / "custom-workspace"
    workflows_dir = custom_workspace / "workflows"
    extra_root = tmp_path / "extra-extensions"
    package_dir = extra_root / "local-pack"
    workflows_dir.mkdir(parents=True)
    (package_dir / "workflows").mkdir(parents=True)
    (package_dir / "manifest.yaml").write_text(
        "id: seraph.local-pack\n"
        "version: 2026.3.21\n"
        "display_name: Local Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  workflows:\n"
        "    - workflows/local-only.md\n"
        "permissions:\n"
        "  tools: [read_file]\n"
        "  network: false\n",
        encoding="utf-8",
    )
    (package_dir / "workflows" / "local-only.md").write_text(
        "---\n"
        "name: local-only\n"
        "description: Local-only workflow\n"
        "requires:\n"
        "  tools: [read_file]\n"
        "steps:\n"
        "  - id: inspect\n"
        "    tool: read_file\n"
        "    arguments:\n"
        "      file_path: notes/source.md\n"
        "---\n\n"
        "Local-only workflow.\n",
        encoding="utf-8",
    )

    workflow_manager.init(str(workflows_dir), manifest_roots=[str(extra_root)])
    workflow_manager._workflows = [
        workflow for workflow in workflow_manager._workflows if workflow.name != "web-brief-to-file"
    ]

    with patch("src.api.capabilities.settings") as mock_settings:
        mock_settings.workspace_dir = str(tmp_path / "other-workspace")

        assert _ensure_bundled_workflow_available("web-brief-to-file") is True

    assert workflow_manager._workflows_dir == str(workflows_dir)
    assert workflow_manager._manifest_roots == [str(extra_root), bundled_manifest_root()]
    assert workflow_manager.get_workflow("local-only") is not None
    assert workflow_manager.get_workflow("web-brief-to-file") is not None

    workflow_manager._workflows = []
    workflow_manager._load_errors = []
    workflow_manager._shared_manifest_errors = []
    workflow_manager._workflows_dir = ""
    workflow_manager._manifest_roots = []
    workflow_manager._config_path = ""
    workflow_manager._disabled = set()
    workflow_manager._registry = None


@pytest.mark.asyncio
async def test_activate_starter_pack_reports_degraded_when_enable_fails(client):
    with (
        patch("src.api.capabilities.skill_manager.get_skill", return_value=SimpleNamespace(name="web-briefing")),
        patch("src.api.capabilities.workflow_manager.get_workflow", return_value=SimpleNamespace(name="web-brief-to-file")),
        patch("src.api.capabilities.require_catalog_install_approval", AsyncMock()),
        patch("src.api.capabilities.skill_manager.enable", return_value=False),
        patch("src.api.capabilities.workflow_manager.enable", return_value=True),
        patch(
            "src.api.capabilities._build_capability_overview",
            return_value={
                "starter_packs": [
                    {
                        "name": "research-briefing",
                        "availability": "blocked",
                        "missing_install_items": [],
                        "blocked_skills": [{"name": "web-briefing"}],
                        "blocked_workflows": [],
                    }
                ],
            },
        ),
    ):
        resp = await client.post("/api/capabilities/starter-packs/research-briefing/activate")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "degraded"
    assert "skill:web-briefing" in payload["missing_entries"]


@pytest.mark.asyncio
async def test_capabilities_overview_runbooks_publish_preflight_for_blocked_workflows():
    ctx = CurrentContext(tool_policy_mode="balanced", mcp_policy_mode="approval", approval_mode="high_risk")
    with (
        patch(
            "src.api.capabilities.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], [], "approval"),
        ),
        patch("src.api.capabilities.context_manager.get_context", return_value=ctx),
        patch("src.api.capabilities.skill_manager.list_skills", return_value=[]),
        patch(
            "src.api.capabilities.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "summarize-file",
                    "tool_name": "workflow_summarize_file",
                    "description": "Summarize file",
                    "requires_tools": ["read_file"],
                    "requires_skills": [],
                    "user_invocable": True,
                    "enabled": True,
                    "is_available": True,
                    "missing_tools": [],
                    "missing_skills": [],
                },
                {
                    "name": "web-brief-to-file",
                    "tool_name": "workflow_web_brief_to_file",
                    "description": "Research and save",
                    "requires_tools": ["web_search", "write_file"],
                    "requires_skills": [],
                    "user_invocable": True,
                    "enabled": True,
                    "is_available": False,
                    "missing_tools": ["write_file"],
                    "missing_skills": [],
                },
            ],
        ),
        patch("src.api.capabilities.mcp_manager.get_config", return_value=[]),
        patch("src.api.capabilities.load_catalog_items", return_value={"skills": [], "mcp_servers": []}),
        patch("src.api.capabilities.catalog_skill_by_name", return_value={}),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=create_app()),
            base_url="http://test",
        ) as client:
            resp = await client.get("/api/capabilities/overview")

    assert resp.status_code == 200
    payload = resp.json()
    runbooks = {item["id"]: item for item in payload["runbooks"]}
    assert runbooks["workflow:summarize-file"]["availability"] == "ready"
    assert any(
        action["type"] == "draft_workflow"
        for action in runbooks["workflow:summarize-file"]["recommended_actions"]
    )
    assert runbooks["workflow:web-brief-to-file"]["availability"] == "blocked"
    assert runbooks["workflow:web-brief-to-file"]["blocking_reasons"] == ["missing tool: write_file"]
    assert any(
        action["type"] == "set_tool_policy"
        for action in runbooks["workflow:web-brief-to-file"]["recommended_actions"]
    )


@pytest.mark.asyncio
async def test_capabilities_overview_counts_missing_install_items_in_pack_availability():
    ctx = CurrentContext(tool_policy_mode="full", mcp_policy_mode="approval", approval_mode="high_risk")
    with (
        patch(
            "src.api.capabilities.get_base_tools_and_active_skills",
            return_value=(
                [SimpleNamespace(name="web_search"), SimpleNamespace(name="write_file")],
                ["web-briefing"],
                "approval",
            ),
        ),
        patch("src.api.capabilities.context_manager.get_context", return_value=ctx),
        patch(
            "src.api.capabilities.skill_manager.list_skills",
            return_value=[
                {
                    "name": "web-briefing",
                    "description": "Web briefing",
                    "requires_tools": ["web_search", "write_file"],
                    "user_invocable": True,
                    "enabled": True,
                    "file_path": "/tmp/web-briefing.md",
                },
            ],
        ),
        patch(
            "src.api.capabilities.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "web-brief-to-file",
                    "tool_name": "workflow_web_brief_to_file",
                    "description": "Research and save",
                    "requires_tools": ["web_search", "write_file"],
                    "requires_skills": [],
                    "user_invocable": True,
                    "enabled": True,
                    "step_count": 2,
                    "file_path": "/tmp/web-brief-to-file.md",
                    "policy_modes": ["balanced", "full"],
                    "execution_boundaries": ["external_read", "workspace_write"],
                    "risk_level": "medium",
                    "accepts_secret_refs": False,
                    "is_available": True,
                    "missing_tools": [],
                    "missing_skills": [],
                },
            ],
        ),
        patch("src.api.capabilities.mcp_manager.get_config", return_value=[]),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=create_app()),
            base_url="http://test",
        ) as client:
            resp = await client.get("/api/capabilities/overview")

    assert resp.status_code == 200
    payload = resp.json()
    research_pack = next(pack for pack in payload["starter_packs"] if pack["name"] == "research-briefing")
    assert research_pack["ready_skills"] == ["web-briefing"]
    assert research_pack["ready_workflows"] == ["web-brief-to-file"]
    assert research_pack["missing_install_items"] == ["http-request"]
    assert research_pack["availability"] == "partial"
    assert any(action["type"] == "install_catalog_item" for action in research_pack["recommended_actions"])
    research_flow = next(flow for flow in payload["marketplace_flows"] if flow["id"] == "starter-pack:research-briefing")
    assert research_flow["kind"] == "starter_pack"
    assert research_flow["availability"] == "partial"
    assert research_flow["ready_count"] == 2
    assert research_flow["total_count"] == 3
    assert research_flow["related_runbooks"] == ["Research Briefing"]
    assert research_flow["blocking_reasons"] == ["missing install item: http-request"]


@pytest.mark.asyncio
async def test_capability_preflight_returns_workflow_and_runbook_repair_metadata(client):
    ctx = CurrentContext(tool_policy_mode="balanced", mcp_policy_mode="approval", approval_mode="high_risk")
    with (
        patch(
            "src.api.capabilities.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], [], "approval"),
        ),
        patch("src.api.capabilities.context_manager.get_context", return_value=ctx),
        patch("src.api.capabilities.skill_manager.list_skills", return_value=[]),
        patch(
            "src.api.capabilities.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "web-brief-to-file",
                    "tool_name": "workflow_web_brief_to_file",
                    "description": "Research and save",
                    "inputs": {
                        "query": {"type": "string", "description": "Search query", "required": True},
                        "file_path": {"type": "string", "description": "Output path", "required": True},
                    },
                    "requires_tools": ["web_search", "write_file"],
                    "requires_skills": [],
                    "user_invocable": True,
                    "enabled": True,
                    "is_available": False,
                    "missing_tools": ["write_file"],
                    "missing_skills": [],
                    "execution_boundaries": ["external_read", "workspace_write"],
                    "risk_level": "medium",
                },
            ],
        ),
        patch("src.api.capabilities.mcp_manager.get_config", return_value=[]),
        patch("src.api.capabilities.load_catalog_items", return_value={"skills": [], "mcp_servers": []}),
        patch("src.api.capabilities.catalog_skill_by_name", return_value={}),
    ):
        workflow_resp = await client.get("/api/capabilities/preflight", params={"target_type": "workflow", "name": "web-brief-to-file"})
        runbook_resp = await client.get("/api/capabilities/preflight", params={"target_type": "runbook", "name": "workflow:web-brief-to-file"})

    assert workflow_resp.status_code == 200
    workflow_payload = workflow_resp.json()
    assert workflow_payload["availability"] == "blocked"
    assert workflow_payload["blocking_reasons"] == ["missing tool: write_file"]
    assert workflow_payload["can_autorepair"] is False
    assert workflow_payload["autorepair_actions"] == []
    assert workflow_payload["parameter_schema"]["file_path"]["type"] == "string"
    assert workflow_payload["doctor_plan"]["repair_actions"][0]["type"] == "set_tool_policy"

    assert runbook_resp.status_code == 200
    runbook_payload = runbook_resp.json()
    assert runbook_payload["availability"] == "blocked"
    assert runbook_payload["blocking_reasons"] == ["missing tool: write_file"]
    assert runbook_payload["risk_level"] == "medium"
    assert runbook_payload["execution_boundaries"] == ["external_read", "workspace_write"]
    assert runbook_payload["can_autorepair"] is False
    assert runbook_payload["autorepair_actions"] == []
    assert runbook_payload["doctor_plan"]["command_preview"]


@pytest.mark.asyncio
async def test_capabilities_overview_skips_noop_starter_pack_recommendation_for_tool_policy_blocks():
    ctx = CurrentContext(tool_policy_mode="balanced", mcp_policy_mode="approval", approval_mode="high_risk")
    with (
        patch(
            "src.api.capabilities.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="web_search")], ["web-briefing"], "approval"),
        ),
        patch("src.api.capabilities.context_manager.get_context", return_value=ctx),
        patch(
            "src.api.capabilities.skill_manager.list_skills",
            return_value=[
                {
                    "name": "web-briefing",
                    "description": "Web briefing",
                    "requires_tools": ["web_search", "write_file"],
                    "user_invocable": True,
                    "enabled": True,
                    "file_path": "/tmp/web-briefing.md",
                },
            ],
        ),
        patch(
            "src.api.capabilities.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "web-brief-to-file",
                    "tool_name": "workflow_web_brief_to_file",
                    "description": "Research and save",
                    "requires_tools": ["web_search", "write_file"],
                    "requires_skills": ["web-briefing"],
                    "user_invocable": True,
                    "enabled": True,
                    "step_count": 2,
                    "file_path": "/tmp/web-brief-to-file.md",
                    "policy_modes": ["balanced", "full"],
                    "execution_boundaries": ["external_read", "workspace_write"],
                    "risk_level": "medium",
                    "accepts_secret_refs": False,
                    "is_available": False,
                    "missing_tools": ["write_file"],
                    "missing_skills": [],
                },
            ],
        ),
        patch("src.api.capabilities.mcp_manager.get_config", return_value=[]),
        patch("src.api.capabilities.load_catalog_items", return_value={"skills": [], "mcp_servers": []}),
        patch("src.api.capabilities.catalog_skill_by_name", return_value={}),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=create_app()),
            base_url="http://test",
        ) as client:
            resp = await client.get("/api/capabilities/overview")

    assert resp.status_code == 200
    payload = resp.json()
    recommendation_ids = {item["id"] for item in payload["recommendations"]}
    assert "starter-pack:research-briefing" not in recommendation_ids
    assert "tool-policy:write_file:full" in recommendation_ids


@pytest.mark.asyncio
async def test_capabilities_overview_repairs_starter_pack_skill_only_tool_blocks():
    ctx = CurrentContext(tool_policy_mode="balanced", mcp_policy_mode="approval", approval_mode="high_risk")
    with (
        patch(
            "src.api.capabilities.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="read_file")], ["daily-standup"], "approval"),
        ),
        patch("src.api.capabilities.context_manager.get_context", return_value=ctx),
        patch(
            "src.api.capabilities.skill_manager.list_skills",
            return_value=[
                {
                    "name": "daily-standup",
                    "description": "Generate a quick standup",
                    "requires_tools": ["execute_code"],
                    "user_invocable": True,
                    "enabled": True,
                    "file_path": "/tmp/daily-standup.md",
                },
            ],
        ),
        patch(
            "src.api.capabilities.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "summarize-file",
                    "tool_name": "workflow_summarize_file",
                    "description": "Summarize an existing file",
                    "requires_tools": ["read_file"],
                    "requires_skills": [],
                    "user_invocable": True,
                    "enabled": True,
                    "step_count": 1,
                    "file_path": "/tmp/summarize-file.md",
                    "policy_modes": ["balanced", "full"],
                    "execution_boundaries": ["workspace_read"],
                    "risk_level": "low",
                    "accepts_secret_refs": False,
                    "is_available": True,
                    "missing_tools": [],
                    "missing_skills": [],
                },
            ],
        ),
        patch("src.api.capabilities.mcp_manager.get_config", return_value=[]),
        patch(
            "src.api.capabilities._load_starter_packs",
            return_value=[
                {
                    "name": "daily-operator-rhythm",
                    "label": "Daily Operator Rhythm",
                    "description": "Standup plus file summary",
                    "sample_prompt": "Run workflow \"summarize-file\" with file_path=\"notes/today.md\".",
                    "skills": ["daily-standup"],
                    "workflows": ["summarize-file"],
                }
            ],
        ),
        patch("src.api.capabilities.load_catalog_items", return_value={"skills": [], "mcp_servers": []}),
        patch("src.api.capabilities.catalog_skill_by_name", return_value={}),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=create_app()),
            base_url="http://test",
        ) as client:
            resp = await client.get("/api/capabilities/overview")

    assert resp.status_code == 200
    payload = resp.json()
    pack = payload["starter_packs"][0]
    assert pack["name"] == "daily-operator-rhythm"
    assert pack["blocked_skills"][0]["name"] == "daily-standup"
    assert pack["blocked_skills"][0]["missing_tools"] == ["execute_code"]
    assert any(action["type"] == "set_tool_policy" and action["mode"] == "full" for action in pack["recommended_actions"])
    assert not any(action["type"] == "activate_starter_pack" for action in pack["recommended_actions"])


@pytest.mark.asyncio
async def test_capability_bootstrap_leaves_policy_changes_manual(client):
    blocked_preflight = {
        "target_type": "workflow",
        "name": "web-brief-to-file",
        "label": "Run web-brief-to-file",
        "description": "Research and save",
        "availability": "blocked",
        "blocking_reasons": ["missing tool: write_file"],
        "recommended_actions": [{"type": "set_tool_policy", "label": "Set tool policy to full", "mode": "full"}],
        "command": 'Run workflow "web-brief-to-file" with query="seraph", file_path="notes/brief.md".',
        "parameter_schema": {"query": {"type": "string"}},
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "autorepair_actions": [],
        "can_autorepair": False,
        "ready": False,
    }

    with (
        patch(
            "src.api.capabilities._build_capability_overview",
            side_effect=[
                {"summary": {"workflows_ready": 1}},
                {"summary": {"workflows_ready": 1}},
            ],
        ),
        patch(
            "src.api.capabilities._capability_preflight_payload",
            side_effect=[blocked_preflight, blocked_preflight],
        ),
        patch("src.api.capabilities._apply_safe_capability_action", AsyncMock()) as apply_action,
        patch("src.api.capabilities.log_integration_event", AsyncMock()) as log_event,
    ):
        resp = await client.post(
            "/api/capabilities/bootstrap",
            json={"target_type": "workflow", "name": "web-brief-to-file"},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "blocked"
    assert payload["ready"] is False
    assert payload["availability"] == "blocked"
    assert payload["applied_actions"] == []
    assert payload["manual_actions"] == blocked_preflight["recommended_actions"]
    assert payload["command"] is None
    assert payload["doctor_plan"]["command_ready"] is False
    assert payload["doctor_plan"]["manual_actions"] == blocked_preflight["recommended_actions"]
    assert payload["overview"]["summary"]["workflows_ready"] == 1
    apply_action.assert_not_awaited()
    log_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_capability_bootstrap_leaves_mcp_enable_actions_manual(client):
    blocked_preflight = {
        "target_type": "runbook",
        "name": "starter-pack:research-briefing",
        "label": "Research briefing",
        "description": "Repair MCP dependency",
        "availability": "blocked",
        "blocking_reasons": ["mcp server browser disabled"],
        "recommended_actions": [{"type": "toggle_mcp_server", "label": "Enable server", "name": "browser", "enabled": True}],
        "command": 'Run workflow "web-brief-to-file" with query="seraph".',
        "parameter_schema": {},
        "risk_level": "medium",
        "execution_boundaries": ["capability_activation"],
        "autorepair_actions": [],
        "can_autorepair": False,
        "ready": False,
    }

    with (
        patch(
            "src.api.capabilities._build_capability_overview",
            side_effect=[
                {"summary": {"mcp_servers_ready": 0}},
                {"summary": {"mcp_servers_ready": 0}},
            ],
        ),
        patch(
            "src.api.capabilities._capability_preflight_payload",
            side_effect=[blocked_preflight, blocked_preflight],
        ),
        patch("src.api.capabilities._apply_safe_capability_action", AsyncMock()) as apply_action,
        patch("src.api.capabilities.log_integration_event", AsyncMock()),
    ):
        resp = await client.post(
            "/api/capabilities/bootstrap",
            json={"target_type": "runbook", "name": "starter-pack:research-briefing"},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "blocked"
    assert payload["applied_actions"] == []
    assert payload["manual_actions"] == blocked_preflight["recommended_actions"]
    assert payload["doctor_plan"]["manual_actions"] == blocked_preflight["recommended_actions"]
    apply_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_capability_bootstrap_can_apply_low_risk_toggle_actions(client):
    blocked_preflight = {
        "target_type": "workflow",
        "name": "web-brief-to-file",
        "label": "Run web-brief-to-file",
        "description": "Enable a disabled workflow",
        "availability": "disabled",
        "blocking_reasons": ["workflow disabled"],
        "recommended_actions": [{"type": "toggle_workflow", "label": "Enable workflow", "name": "web-brief-to-file", "enabled": True}],
        "command": 'Run workflow "web-brief-to-file" with query="seraph", file_path="notes/brief.md".',
        "parameter_schema": {"query": {"type": "string"}},
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "autorepair_actions": [{"type": "toggle_workflow", "label": "Enable workflow", "name": "web-brief-to-file", "enabled": True}],
        "can_autorepair": True,
        "ready": False,
    }
    ready_preflight = {
        **blocked_preflight,
        "availability": "ready",
        "blocking_reasons": [],
        "recommended_actions": [],
        "autorepair_actions": [],
        "can_autorepair": False,
        "ready": True,
    }

    with (
        patch(
            "src.api.capabilities._build_capability_overview",
            side_effect=[
                {"summary": {"workflows_ready": 0}},
                {"summary": {"workflows_ready": 1}},
                {"summary": {"workflows_ready": 1}},
            ],
        ),
        patch(
            "src.api.capabilities._capability_preflight_payload",
            side_effect=[blocked_preflight, ready_preflight],
        ),
        patch(
            "src.api.capabilities._apply_safe_capability_action",
            return_value={"type": "toggle_workflow", "name": "web-brief-to-file", "enabled": True, "status": "applied"},
        ) as apply_action,
        patch("src.api.capabilities.log_integration_event", AsyncMock()) as log_event,
    ):
        resp = await client.post(
            "/api/capabilities/bootstrap",
            json={"target_type": "workflow", "name": "web-brief-to-file"},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ready"
    assert payload["ready"] is True
    assert payload["availability"] == "ready"
    assert payload["applied_actions"] == [{"type": "toggle_workflow", "name": "web-brief-to-file", "enabled": True, "status": "applied"}]
    assert payload["manual_actions"] == []
    assert payload["command"] == blocked_preflight["command"]
    assert payload["doctor_plan"]["command_ready"] is True
    assert payload["doctor_plan"]["applied_actions"][0]["type"] == "toggle_workflow"
    assert payload["overview"]["summary"]["workflows_ready"] == 1
    apply_action.assert_awaited_once()
    log_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_capability_bootstrap_does_not_reclassify_low_risk_actions_as_manual_after_failed_apply(client):
    blocked_preflight = {
        "target_type": "workflow",
        "name": "web-brief-to-file",
        "label": "Run web-brief-to-file",
        "description": "Enable a disabled workflow",
        "availability": "disabled",
        "blocking_reasons": ["workflow disabled"],
        "recommended_actions": [{"type": "toggle_workflow", "label": "Enable workflow", "name": "web-brief-to-file", "enabled": True}],
        "command": 'Run workflow "web-brief-to-file" with query="seraph", file_path="notes/brief.md".',
        "parameter_schema": {"query": {"type": "string"}},
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "autorepair_actions": [{"type": "toggle_workflow", "label": "Enable workflow", "name": "web-brief-to-file", "enabled": True}],
        "can_autorepair": True,
        "ready": False,
    }

    with (
        patch(
            "src.api.capabilities._build_capability_overview",
            side_effect=[
                {"summary": {"workflows_ready": 0}},
                {"summary": {"workflows_ready": 0}},
                {"summary": {"workflows_ready": 0}},
            ],
        ),
        patch(
            "src.api.capabilities._capability_preflight_payload",
            side_effect=[blocked_preflight, blocked_preflight],
        ),
        patch(
            "src.api.capabilities._apply_safe_capability_action",
            return_value={"type": "toggle_workflow", "name": "web-brief-to-file", "enabled": True, "status": "failed"},
        ) as apply_action,
        patch("src.api.capabilities.log_integration_event", AsyncMock()),
    ):
        resp = await client.post(
            "/api/capabilities/bootstrap",
            json={"target_type": "workflow", "name": "web-brief-to-file"},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "blocked"
    assert payload["applied_actions"] == [{"type": "toggle_workflow", "name": "web-brief-to-file", "enabled": True, "status": "failed"}]
    assert payload["manual_actions"] == []
    assert payload["doctor_plan"]["manual_actions"] == []
    assert payload["doctor_plan"]["applied_actions"][0]["status"] == "failed"
    apply_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_capability_bootstrap_leaves_extension_enable_actions_manual(client):
    blocked_preflight = {
        "target_type": "workflow",
        "name": "web-brief-to-file",
        "label": "Run web-brief-to-file",
        "description": "Research and save",
        "availability": "blocked",
        "blocking_reasons": ["workflow packaged in a disabled extension"],
        "recommended_actions": [
            {
                "type": "enable_extension",
                "label": "Enable Research Pack",
                "name": "seraph.research-pack",
                "target": "Research Pack",
            }
        ],
        "command": 'Run workflow "web-brief-to-file" with query="seraph", file_path="notes/brief.md".',
        "parameter_schema": {"query": {"type": "string"}},
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "autorepair_actions": [
            {
                "type": "enable_extension",
                "label": "Enable Research Pack",
                "name": "seraph.research-pack",
                "target": "Research Pack",
            }
        ],
        "can_autorepair": False,
        "ready": False,
    }

    with (
        patch(
            "src.api.capabilities._build_capability_overview",
            side_effect=[
                {"summary": {"workflows_ready": 0}},
                {"summary": {"workflows_ready": 0}},
            ],
        ),
        patch(
            "src.api.capabilities._capability_preflight_payload",
            side_effect=[blocked_preflight, blocked_preflight],
        ),
        patch("src.api.capabilities._apply_safe_capability_action", AsyncMock()) as apply_action,
        patch("src.api.capabilities.log_integration_event", AsyncMock()) as log_event,
    ):
        resp = await client.post(
            "/api/capabilities/bootstrap",
            json={"target_type": "workflow", "name": "web-brief-to-file"},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "blocked"
    assert payload["ready"] is False
    assert payload["applied_actions"] == []
    assert payload["manual_actions"] == blocked_preflight["recommended_actions"]
    assert payload["doctor_plan"]["manual_actions"] == blocked_preflight["recommended_actions"]
    apply_action.assert_not_awaited()
    log_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_workflow_draft_validation_and_save(client, tmp_path):
    workflow_content = (
        "---\n"
        "name: Web Brief To File\n"
        "description: Research and save\n"
        "user_invocable: true\n"
        "requires:\n"
        "  tools: [web_search, write_file]\n"
        "steps:\n"
        "  - id: search\n"
        "    tool: web_search\n"
        "    arguments:\n"
        "      query: \"{{ inputs.query }}\"\n"
        "  - id: save\n"
        "    tool: write_file\n"
        "    arguments:\n"
        "      file_path: \"{{ inputs.file_path }}\"\n"
        "      content: \"{{ steps.search.result }}\"\n"
        "inputs:\n"
        "  query:\n"
        "    type: string\n"
        "    required: true\n"
        "  file_path:\n"
        "    type: string\n"
        "    required: true\n"
        "---\n\n"
        "Workflow body.\n"
    )
    with (
        patch("src.api.capabilities.settings.workspace_dir", str(tmp_path)),
        patch("src.api.capabilities.workflow_manager._workflows_dir", str(tmp_path / "workflows")),
        patch("src.api.capabilities.workflow_manager._manifest_roots", []),
        patch("src.api.capabilities.workflow_manager.init") as init_manager,
        patch("src.api.capabilities.workflow_manager.reload", return_value=[]),
    ):
        validate_resp = await client.post(
            "/api/capabilities/workflow-drafts/validate",
            json={"content": workflow_content},
        )
        save_resp = await client.post(
            "/api/capabilities/workflow-drafts/save",
            json={"content": workflow_content},
        )

    assert validate_resp.status_code == 200
    validate_payload = validate_resp.json()
    assert validate_payload["valid"] is True
    assert validate_payload["workflow"]["step_count"] == 2
    assert validate_payload["workflow"]["tool_name"] == "workflow_web_brief_to_file"

    assert save_resp.status_code == 200
    save_payload = save_resp.json()
    assert save_payload["status"] == "saved"
    assert save_payload["name"] == "Web Brief To File"
    init_manager.assert_called_once_with(
        str(tmp_path / "workflows"),
        manifest_roots=default_manifest_roots_for_workspace(str(tmp_path)),
    )
    assert save_payload["file_path"].endswith(
        "extensions/workspace-capabilities/workflows/web_brief_to_file.md"
    )
