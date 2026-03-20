from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.observer.context import CurrentContext


@pytest.mark.asyncio
async def test_capabilities_overview_aggregates_blocked_states_and_starter_packs(client):
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
                    "requires_tools": ["shell_execute"],
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
        resp = await client.get("/api/capabilities/overview")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["tool_policy_mode"] == "balanced"
    assert payload["mcp_policy_mode"] == "approval"
    assert payload["approval_mode"] == "high_risk"

    shell_tool = next(tool for tool in payload["native_tools"] if tool["name"] == "shell_execute")
    assert shell_tool["availability"] == "blocked"
    assert shell_tool["blocked_reason"] == "tool_policy_balanced"
    assert shell_tool["recommended_actions"][0]["type"] == "set_tool_policy"

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
        patch("src.api.capabilities.install_catalog_item_by_name", side_effect=install_side_effect),
        patch("src.api.capabilities._seed_bundled_workflow", return_value=True),
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
async def test_activate_starter_pack_reports_degraded_when_enable_fails(client):
    with (
        patch("src.api.capabilities.skill_manager.get_skill", return_value=SimpleNamespace(name="web-briefing")),
        patch("src.api.capabilities.workflow_manager.get_workflow", return_value=SimpleNamespace(name="web-brief-to-file")),
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
async def test_capabilities_overview_runbooks_publish_preflight_for_blocked_workflows(client):
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
async def test_capabilities_overview_counts_missing_install_items_in_pack_availability(client):
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
        resp = await client.get("/api/capabilities/overview")

    assert resp.status_code == 200
    payload = resp.json()
    research_pack = next(pack for pack in payload["starter_packs"] if pack["name"] == "research-briefing")
    assert research_pack["ready_skills"] == ["web-briefing"]
    assert research_pack["ready_workflows"] == ["web-brief-to-file"]
    assert research_pack["missing_install_items"] == ["http-request"]
    assert research_pack["availability"] == "partial"
    assert any(action["type"] == "install_catalog_item" for action in research_pack["recommended_actions"])


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
    assert workflow_payload["can_autorepair"] is True
    assert workflow_payload["autorepair_actions"][0]["type"] == "set_tool_policy"
    assert workflow_payload["parameter_schema"]["file_path"]["type"] == "string"
    assert workflow_payload["doctor_plan"]["repair_actions"][0]["type"] == "set_tool_policy"

    assert runbook_resp.status_code == 200
    runbook_payload = runbook_resp.json()
    assert runbook_payload["availability"] == "blocked"
    assert runbook_payload["blocking_reasons"] == ["missing tool: write_file"]
    assert runbook_payload["risk_level"] == "medium"
    assert runbook_payload["execution_boundaries"] == ["external_read", "workspace_write"]
    assert runbook_payload["autorepair_actions"][0]["type"] == "set_tool_policy"
    assert runbook_payload["doctor_plan"]["command_preview"]


@pytest.mark.asyncio
async def test_capabilities_overview_skips_noop_starter_pack_recommendation_for_tool_policy_blocks(client):
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
        resp = await client.get("/api/capabilities/overview")

    assert resp.status_code == 200
    payload = resp.json()
    recommendation_ids = {item["id"] for item in payload["recommendations"]}
    assert "starter-pack:research-briefing" not in recommendation_ids
    assert "tool-policy:write_file:full" in recommendation_ids


@pytest.mark.asyncio
async def test_capabilities_overview_repairs_starter_pack_skill_only_tool_blocks(client):
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
                    "requires_tools": ["shell_execute"],
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
        resp = await client.get("/api/capabilities/overview")

    assert resp.status_code == 200
    payload = resp.json()
    pack = payload["starter_packs"][0]
    assert pack["name"] == "daily-operator-rhythm"
    assert pack["blocked_skills"][0]["name"] == "daily-standup"
    assert pack["blocked_skills"][0]["missing_tools"] == ["shell_execute"]
    assert any(action["type"] == "set_tool_policy" and action["mode"] == "full" for action in pack["recommended_actions"])
    assert not any(action["type"] == "activate_starter_pack" for action in pack["recommended_actions"])


@pytest.mark.asyncio
async def test_capability_bootstrap_applies_safe_actions_until_ready(client):
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
        "autorepair_actions": [{"type": "set_tool_policy", "label": "Set tool policy to full", "mode": "full"}],
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
                {"summary": {"workflows_ready": 1}},
                {"summary": {"workflows_ready": 2}},
                {"summary": {"workflows_ready": 2}},
            ],
        ),
        patch(
            "src.api.capabilities._capability_preflight_payload",
            side_effect=[blocked_preflight, ready_preflight],
        ),
        patch(
            "src.api.capabilities._apply_safe_capability_action",
            return_value={"type": "set_tool_policy", "mode": "full", "status": "applied"},
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
    assert payload["applied_actions"] == [{"type": "set_tool_policy", "mode": "full", "status": "applied"}]
    assert payload["manual_actions"] == []
    assert payload["command"] == blocked_preflight["command"]
    assert payload["doctor_plan"]["command_ready"] is True
    assert payload["doctor_plan"]["applied_actions"][0]["type"] == "set_tool_policy"
    assert payload["overview"]["summary"]["workflows_ready"] == 2
    apply_action.assert_awaited_once()
    log_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_capability_bootstrap_can_apply_mcp_toggle_actions(client):
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
        "autorepair_actions": [{"type": "toggle_mcp_server", "label": "Enable server", "name": "browser", "enabled": True}],
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
                {"summary": {"mcp_servers_ready": 0}},
                {"summary": {"mcp_servers_ready": 1}},
                {"summary": {"mcp_servers_ready": 1}},
            ],
        ),
        patch(
            "src.api.capabilities._capability_preflight_payload",
            side_effect=[blocked_preflight, ready_preflight],
        ),
        patch(
            "src.api.capabilities._apply_safe_capability_action",
            return_value={"type": "toggle_mcp_server", "name": "browser", "enabled": True, "status": "applied"},
        ) as apply_action,
        patch("src.api.capabilities.log_integration_event", AsyncMock()),
    ):
        resp = await client.post(
            "/api/capabilities/bootstrap",
            json={"target_type": "runbook", "name": "starter-pack:research-briefing"},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ready"
    assert payload["applied_actions"][0]["type"] == "toggle_mcp_server"
    assert payload["doctor_plan"]["applied_actions"][0]["type"] == "toggle_mcp_server"
    apply_action.assert_awaited_once()


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
    assert save_payload["file_path"].endswith("web_brief_to_file.md")
