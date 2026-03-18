from types import SimpleNamespace
from unittest.mock import patch

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
    assert research_pack["blocked_skills"][0]["name"] == "web-briefing"
    assert any(item["type"] == "skill" and item["name"] == "daily-standup" for item in payload["catalog_items"])
    assert any(item["type"] == "mcp_server" and item["name"] == "github" for item in payload["catalog_items"])
    assert payload["recommendations"]
    assert payload["runbooks"]


@pytest.mark.asyncio
async def test_activate_starter_pack_enables_seeded_assets(client):
    with (
        patch("src.api.capabilities.skill_manager.get_skill", return_value=None),
        patch("src.api.capabilities.workflow_manager.get_workflow", return_value=None),
        patch("src.api.capabilities._seed_bundled_skill", return_value=True),
        patch("src.api.capabilities._seed_bundled_workflow", return_value=True),
        patch("src.api.capabilities.skill_manager.reload", return_value=[]),
        patch("src.api.capabilities.workflow_manager.reload", return_value=[]),
        patch("src.api.capabilities.skill_manager.enable", return_value=True) as enable_skill,
        patch("src.api.capabilities.workflow_manager.enable", return_value=True) as enable_workflow,
        patch(
            "src.api.capabilities._build_capability_overview",
            return_value={"summary": {"starter_packs_ready": 1}},
        ),
    ):
        resp = await client.post("/api/capabilities/starter-packs/research-briefing/activate")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "activated"
    assert payload["name"] == "research-briefing"
    assert payload["enabled_skills"] == ["web-briefing"]
    assert payload["enabled_workflows"] == ["web-brief-to-file"]
    assert payload["overview"]["summary"]["starter_packs_ready"] == 1
    enable_skill.assert_called_with("web-briefing")
    enable_workflow.assert_called_with("web-brief-to-file")


@pytest.mark.asyncio
async def test_capabilities_overview_runbooks_only_include_ready_workflows(client):
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
        patch("src.api.capabilities._load_catalog_items", return_value={"skills": [], "mcp_servers": []}),
    ):
        resp = await client.get("/api/capabilities/overview")

    assert resp.status_code == 200
    payload = resp.json()
    runbook_ids = {item["id"] for item in payload["runbooks"]}
    assert "workflow:summarize-file" in runbook_ids
    assert "workflow:web-brief-to-file" not in runbook_ids


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
        patch("src.api.capabilities._load_catalog_items", return_value={"skills": [], "mcp_servers": []}),
    ):
        resp = await client.get("/api/capabilities/overview")

    assert resp.status_code == 200
    payload = resp.json()
    recommendation_ids = {item["id"] for item in payload["recommendations"]}
    assert "starter-pack:research-briefing" not in recommendation_ids
    assert "tool-policy:write_file:full" in recommendation_ids
