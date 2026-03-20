"""Tests for reusable workflow composition."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.workflows.loader import Workflow, _parse_workflow_file, load_workflows
from src.workflows.manager import WorkflowManager
from src.approval.exceptions import ApprovalRequired
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.agent.factory import get_tools
from src.tools.approval import ApprovalTool


class DummyTool:
    def __init__(self, name: str, responder):
        self.name = name
        self.description = f"{name} description"
        self.inputs = {}
        self.output_type = "string"
        self.calls: list[dict] = []
        self._responder = responder

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        self.calls.append(kwargs)
        return self._responder(**kwargs)


@pytest.fixture
def workflows_dir(tmp_path):
    d = tmp_path / "workflows"
    d.mkdir()

    (d / "web-brief.md").write_text(
        "---\n"
        "name: web-brief-to-file\n"
        "description: Search the web and save a note\n"
        "requires:\n"
        "  tools: [web_search, write_file]\n"
        "inputs:\n"
        "  query:\n"
        "    type: string\n"
        "    description: Query to search\n"
        "  file_path:\n"
        "    type: string\n"
        "    description: Output path\n"
        "steps:\n"
        "  - id: search\n"
        "    tool: web_search\n"
        "    arguments:\n"
        "      query: \"{{ query }}\"\n"
        "  - id: save\n"
        "    tool: write_file\n"
        "    arguments:\n"
        "      file_path: \"{{ file_path }}\"\n"
        "      content: |\n"
        "        Search results\n"
        "\n"
        "        {{ steps.search.result }}\n"
        "result: Saved search results for {{ query }} to {{ file_path }}.\n"
        "---\n\n"
        "Search and save.\n"
    )

    (d / "goal-snapshot.md").write_text(
        "---\n"
        "name: goal-snapshot-to-file\n"
        "description: Export goals into a note\n"
        "requires:\n"
        "  tools: [get_goals, write_file]\n"
        "  skills: [goal-reflection]\n"
        "inputs:\n"
        "  file_path:\n"
        "    type: string\n"
        "    description: Output path\n"
        "steps:\n"
        "  - id: goals\n"
        "    tool: get_goals\n"
        "    arguments: {}\n"
        "  - id: save\n"
        "    tool: write_file\n"
        "    arguments:\n"
        "      file_path: \"{{ file_path }}\"\n"
        "      content: \"{{ steps.goals.result }}\"\n"
        "---\n\n"
        "Export current goals.\n"
    )

    return str(d)


@pytest.fixture
def invalid_workflows_dir(tmp_path):
    d = tmp_path / "bad_workflows"
    d.mkdir()
    (d / "no-frontmatter.md").write_text("missing yaml")
    (d / "missing-steps.md").write_text(
        "---\n"
        "name: broken\n"
        "description: missing steps\n"
        "---\n\n"
        "No steps.\n"
    )
    (d / "undeclared-tool.md").write_text(
        "---\n"
        "name: underdeclared\n"
        "description: workflow with an undeclared step tool\n"
        "requires:\n"
        "  tools: [web_search]\n"
        "steps:\n"
        "  - id: save\n"
        "    tool: write_file\n"
        "    arguments:\n"
        "      file_path: notes/test.md\n"
        "      content: hi\n"
        "---\n\n"
        "Underdeclared.\n"
    )
    return str(d)


class TestWorkflowLoader:
    def test_parse_valid_workflow(self, workflows_dir):
        workflow = _parse_workflow_file(os.path.join(workflows_dir, "web-brief.md"))
        assert isinstance(workflow, Workflow)
        assert workflow.name == "web-brief-to-file"
        assert workflow.tool_name == "workflow_web_brief_to_file"
        assert workflow.requires_tools == ["web_search", "write_file"]
        assert workflow.inputs["query"]["type"] == "string"
        assert len(workflow.steps) == 2

    def test_load_workflows(self, workflows_dir):
        workflows = load_workflows(workflows_dir)
        assert {workflow.name for workflow in workflows} == {
            "web-brief-to-file",
            "goal-snapshot-to-file",
        }

    def test_invalid_workflows_are_skipped(self, invalid_workflows_dir):
        assert load_workflows(invalid_workflows_dir) == []

    def test_step_tools_property_tracks_runtime_tools(self, workflows_dir):
        workflow = _parse_workflow_file(os.path.join(workflows_dir, "web-brief.md"))
        assert workflow is not None
        assert workflow.step_tools == ["web_search", "write_file"]


class TestWorkflowManager:
    def test_active_workflow_gating_uses_tools_and_skills(self, workflows_dir):
        mgr = WorkflowManager()
        mgr.init(workflows_dir)

        active = mgr.get_active_workflows(
            ["web_search", "write_file", "get_goals"],
            [],
        )
        assert [workflow.name for workflow in active] == ["web-brief-to-file"]

        active = mgr.get_active_workflows(
            ["web_search", "write_file", "get_goals"],
            ["goal-reflection"],
        )
        assert {workflow.name for workflow in active} == {
            "web-brief-to-file",
            "goal-snapshot-to-file",
        }

    def test_enable_disable_persists(self, workflows_dir):
        mgr = WorkflowManager()
        mgr.init(workflows_dir)
        assert mgr.disable("web-brief-to-file") is True
        assert mgr.get_workflow("web-brief-to-file").enabled is False

        config_path = os.path.join(os.path.dirname(workflows_dir), "workflows-config.json")
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["disabled"] == ["web-brief-to-file"]

        mgr2 = WorkflowManager()
        mgr2.init(workflows_dir)
        assert mgr2.get_workflow("web-brief-to-file").enabled is False
        assert mgr2.enable("web-brief-to-file") is True
        assert mgr2.get_workflow("web-brief-to-file").enabled is True

    def test_build_tools_executes_steps_sequentially(self, workflows_dir):
        mgr = WorkflowManager()
        mgr.init(workflows_dir)
        search = DummyTool("web_search", lambda query: f"SEARCH<{query}>")
        writes: list[dict] = []

        def _write_file(file_path: str, content: str):
            writes.append({"file_path": file_path, "content": content})
            return f"WROTE<{file_path}>"

        write = DummyTool("write_file", _write_file)
        workflow_tools = mgr.build_workflow_tools([search, write], active_skill_names=[])
        workflow_tool = next(tool for tool in workflow_tools if tool.name == "workflow_web_brief_to_file")

        result = workflow_tool(query="seraph", file_path="notes/brief.md")

        assert search.calls == [{"query": "seraph"}]
        assert writes == [{
            "file_path": "notes/brief.md",
            "content": "Search results\n\nSEARCH<seraph>\n",
        }]
        assert result == "Saved search results for seraph to notes/brief.md."
        audit_payload = workflow_tool.get_audit_result_payload({}, result)
        assert audit_payload[0] == "workflow_web_brief_to_file succeeded (2 steps)"
        assert audit_payload[1]["workflow_name"] == "web-brief-to-file"
        assert audit_payload[1]["step_count"] == 2
        assert audit_payload[1]["step_tools"] == ["web_search", "write_file"]
        assert audit_payload[1]["artifact_paths"] == ["notes/brief.md"]
        assert audit_payload[1]["continued_error_steps"] == []
        assert audit_payload[1]["failed_step_ids"] == []
        assert audit_payload[1]["content_redacted"] is True
        assert isinstance(audit_payload[1]["run_fingerprint"], str)
        assert len(audit_payload[1]["step_records"]) == 2
        assert audit_payload[1]["step_records"][0]["tool"] == "web_search"
        assert audit_payload[1]["step_records"][1]["artifact_paths"] == ["notes/brief.md"]

    def test_build_tools_supports_mcp_requirements(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        (workflows_dir / "mcp.md").write_text(
            "---\n"
            "name: mcp-export\n"
            "description: Use MCP and save output\n"
            "requires:\n"
            "  tools: [mcp_list_tasks, write_file]\n"
            "inputs:\n"
            "  file_path:\n"
            "    type: string\n"
            "steps:\n"
            "  - id: fetch\n"
            "    tool: mcp_list_tasks\n"
            "    arguments: {}\n"
            "  - id: save\n"
            "    tool: write_file\n"
            "    arguments:\n"
            "      file_path: \"{{ file_path }}\"\n"
            "      content: \"{{ steps.fetch.result }}\"\n"
            "---\n"
        )

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir))
        mcp_tool = DummyTool("mcp_list_tasks", lambda: "task-a\ntask-b")
        write = DummyTool("write_file", lambda file_path, content: f"saved {file_path}: {content}")
        workflow_tools = mgr.build_workflow_tools([mcp_tool, write], active_skill_names=[])
        tool = workflow_tools[0]

        result = tool(file_path="tasks.md")

        assert tool.name == "workflow_mcp_export"
        assert "saved tasks.md: task-a" in result
        assert mgr.get_tool_metadata(tool.name)["policy_modes"] == ["full"]
        assert mgr.get_tool_metadata(tool.name)["execution_boundaries"] == ["external_mcp", "workspace_write"]
        assert mgr.get_tool_metadata(tool.name)["accepts_secret_refs"] is True


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_projects_history_and_boundaries(client):
    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-result",
                    "session_id": "session-1",
                    "event_type": "tool_result",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "workflow_web_brief_to_file succeeded (2 steps)",
                    "created_at": "2026-03-18T12:01:45Z",
                    "details": {
                        "workflow_name": "web-brief-to-file",
                        "run_fingerprint": "web-brief",
                        "step_tools": ["web_search", "write_file"],
                        "step_records": [
                            {
                                "id": "search",
                                "index": 1,
                                "tool": "web_search",
                                "status": "succeeded",
                                "argument_keys": ["query"],
                                "artifact_paths": [],
                                "result_summary": "text (14 chars)",
                                "error_kind": None,
                            }
                        ],
                        "artifact_paths": ["notes/brief.md"],
                        "continued_error_steps": [],
                    },
                },
                {
                    "id": "evt-call",
                    "session_id": "session-1",
                    "event_type": "tool_call",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "Calling workflow",
                    "created_at": "2026-03-18T12:01:00Z",
                    "details": {
                        "run_fingerprint": "web-brief",
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch(
            "src.api.workflows.approval_repository.list_pending",
            return_value=[
                {
                    "id": "approval-1",
                    "tool_name": "workflow_web_brief_to_file",
                    "session_id": "session-1",
                    "fingerprint": "web-brief",
                    "summary": "Approval pending for workflow_web_brief_to_file",
                    "risk_level": "medium",
                    "created_at": "2026-03-18T12:01:10Z",
                    "resume_message": "Continue the web brief once approved",
                }
            ],
        ),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "medium",
                "execution_boundaries": ["external_read", "workspace_write"],
                "accepts_secret_refs": False,
            },
        ),
        patch(
            "src.api.workflows.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "web-brief-to-file",
                    "inputs": {
                        "query": {"type": "string", "description": "Search query"},
                        "file_path": {"type": "string", "description": "Output path"},
                    },
                    "enabled": True,
                    "is_available": True,
                    "missing_tools": [],
                    "missing_skills": [],
                }
            ],
        ),
        patch(
            "src.api.workflows.session_manager.list_sessions",
            return_value=[{"id": "session-1", "title": "Research thread"}],
        ),
        patch("src.api.workflows.get_current_tool_policy_mode", return_value="balanced"),
    ):
        response = await client.get("/api/workflows/runs?session_id=session-1")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["runs"]) == 1
    run = payload["runs"][0]
    assert run["workflow_name"] == "web-brief-to-file"
    assert run["risk_level"] == "medium"
    assert run["execution_boundaries"] == ["external_read", "workspace_write"]
    assert run["artifact_paths"] == ["notes/brief.md"]
    assert run["run_fingerprint"] == "web-brief"
    assert run["run_identity"] == "session-1:workflow_web_brief_to_file:web-brief"
    assert run["step_records"][0]["tool"] == "web_search"
    assert run["pending_approval_count"] == 1
    assert run["pending_approval_ids"] == ["approval-1"]
    assert run["pending_approvals"][0]["resume_message"] == "Continue the web brief once approved"
    assert run["thread_id"] == "session-1"
    assert run["thread_label"] == "Research thread"
    assert run["replay_allowed"] is False
    assert run["replay_block_reason"] == "pending_approval"
    assert run["availability"] == "ready"
    assert run["parameter_schema"]["file_path"]["type"] == "string"
    assert run["resume_from_step"] == "approval_gate"
    assert run["resume_checkpoint_label"] == "Approval gate"
    assert run["thread_continue_message"] == "Continue the web brief once approved"
    assert run["approval_recovery_message"]
    assert run["timeline"][0]["kind"] == "workflow_started"
    assert run["timeline"][1]["kind"] == "workflow_step_succeeded"
    assert run["timeline"][2]["kind"] == "approval_pending"


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_uses_stored_fingerprint_for_redacted_arguments(client):
    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-result",
                    "session_id": "session-1",
                    "event_type": "tool_result",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "workflow_web_brief_to_file succeeded (2 steps)",
                    "created_at": "2026-03-18T12:01:45Z",
                    "details": {
                        "workflow_name": "web-brief-to-file",
                        "run_fingerprint": "web-brief-secret",
                        "step_tools": ["web_search", "write_file"],
                        "step_records": [],
                        "artifact_paths": ["notes/brief.md"],
                        "continued_error_steps": [],
                    },
                },
                {
                    "id": "evt-call",
                    "session_id": "session-1",
                    "event_type": "tool_call",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "Calling workflow",
                    "created_at": "2026-03-18T12:01:00Z",
                    "details": {
                        "run_fingerprint": "web-brief-secret",
                        "arguments": {
                            "query": "seraph",
                            "file_path": "notes/brief.md",
                            "secret_ref": "[redacted]",
                        },
                    },
                },
            ],
        ),
        patch(
            "src.api.workflows.approval_repository.list_pending",
            return_value=[
                {
                    "id": "approval-1",
                    "tool_name": "workflow_web_brief_to_file",
                    "session_id": "session-1",
                    "fingerprint": "web-brief-secret",
                    "summary": "Approval pending for workflow_web_brief_to_file",
                    "risk_level": "medium",
                    "created_at": "2026-03-18T12:01:10Z",
                    "resume_message": "Continue after approval",
                }
            ],
        ),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "medium",
                "execution_boundaries": ["external_read", "workspace_write"],
                "accepts_secret_refs": True,
            },
        ),
        patch(
            "src.api.workflows.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "web-brief-to-file",
                    "inputs": {
                        "query": {"type": "string", "description": "Search query"},
                        "file_path": {"type": "string", "description": "Output path"},
                        "secret_ref": {"type": "string", "description": "Secret ref"},
                    },
                    "enabled": True,
                    "is_available": True,
                    "missing_tools": [],
                    "missing_skills": [],
                }
            ],
        ),
        patch(
            "src.api.workflows.session_manager.list_sessions",
            return_value=[{"id": "session-1", "title": "Research thread"}],
        ),
        patch("src.api.workflows.get_current_tool_policy_mode", return_value="full"),
    ):
        response = await client.get("/api/workflows/runs?session_id=session-1")

    assert response.status_code == 200
    run = response.json()["runs"][0]
    assert run["run_fingerprint"] == "web-brief-secret"
    assert run["pending_approval_count"] == 1
    assert run["pending_approvals"][0]["id"] == "approval-1"
    assert run["thread_continue_message"] == "Continue after approval"


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_marks_waiting_runs_as_awaiting_approval(client):
    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-call",
                    "session_id": "session-1",
                    "event_type": "tool_call",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "Calling workflow",
                    "created_at": "2026-03-18T12:01:00Z",
                    "details": {"arguments": {"query": "seraph", "file_path": "notes/brief.md"}},
                },
            ],
        ),
        patch(
            "src.api.workflows.approval_repository.list_pending",
            return_value=[
                {
                    "id": "approval-1",
                    "tool_name": "workflow_web_brief_to_file",
                    "session_id": "session-1",
                    "fingerprint": "none",
                    "summary": "Approval pending for workflow_web_brief_to_file",
                    "risk_level": "medium",
                    "created_at": "2026-03-18T12:01:10Z",
                }
            ],
        ),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "medium",
                "execution_boundaries": ["external_read", "workspace_write"],
                "accepts_secret_refs": False,
            },
        ),
        patch(
            "src.api.workflows.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "web-brief-to-file",
                    "inputs": {"query": {"type": "string", "description": "Search query"}},
                    "enabled": True,
                    "is_available": False,
                    "missing_tools": ["write_file"],
                    "missing_skills": [],
                }
            ],
        ),
        patch(
            "src.api.workflows.session_manager.list_sessions",
            return_value=[{"id": "session-1", "title": "Research thread"}],
        ),
        patch("src.api.workflows.get_current_tool_policy_mode", return_value="balanced"),
    ):
        response = await client.get("/api/workflows/runs?session_id=session-1")

    assert response.status_code == 200
    run = response.json()["runs"][0]
    assert run["status"] == "awaiting_approval"
    assert run["pending_approval_count"] == 1
    assert run["availability"] == "blocked"
    assert any(action["type"] == "set_tool_policy" for action in run["replay_recommended_actions"])
    assert run["timeline"][1]["kind"] == "approval_pending"


@pytest.mark.asyncio
async def test_workflow_diagnostics_endpoint_exposes_load_errors(client):
    with patch(
        "src.api.workflows.workflow_manager.get_diagnostics",
        return_value={
            "loaded_count": 1,
            "error_count": 2,
            "workflows": [{"name": "web-brief-to-file"}],
            "load_errors": [
                {"file_path": "/tmp/broken.md", "message": "Workflow must define at least one step."},
                {"file_path": "/tmp/other.md", "message": "Step tool not declared."},
            ],
        },
    ):
        response = await client.get("/api/workflows/diagnostics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["loaded_count"] == 1
    assert payload["error_count"] == 2
    assert payload["workflows"][0]["name"] == "web-brief-to-file"
    assert payload["load_errors"][0]["file_path"] == "/tmp/broken.md"
    assert payload["load_errors"][0]["message"] == "Workflow must define at least one step."


def test_list_workflows_includes_policy_metadata(workflows_dir):
    mgr = WorkflowManager()
    mgr.init(workflows_dir)

    workflows = mgr.list_workflows()
    web_brief = next(workflow for workflow in workflows if workflow["name"] == "web-brief-to-file")

    assert set(web_brief["inputs"]) == {"query", "file_path"}
    assert web_brief["policy_modes"] == ["balanced", "full"]
    assert web_brief["execution_boundaries"] == ["external_read", "workspace_write"]
    assert web_brief["risk_level"] == "medium"
    assert web_brief["accepts_secret_refs"] is False


def test_list_workflows_includes_runtime_availability(workflows_dir):
    mgr = WorkflowManager()
    mgr.init(workflows_dir)

    workflows = mgr.list_workflows(
        available_tool_names=["web_search"],
        active_skill_names=[],
    )
    web_brief = next(workflow for workflow in workflows if workflow["name"] == "web-brief-to-file")

    assert web_brief["is_available"] is False
    assert web_brief["missing_tools"] == ["write_file"]
    assert web_brief["missing_skills"] == []


class TestWorkflowApi:
    @pytest.mark.asyncio
    async def test_list_workflows(self, client):
        mock_mgr = MagicMock()
        mock_mgr.list_workflows.return_value = [
            {
                "name": "web-brief-to-file",
                "tool_name": "workflow_web_brief_to_file",
                "description": "Search and save",
                "inputs": {
                    "query": {"type": "string", "description": "Query to search", "required": True, "default": None},
                    "file_path": {"type": "string", "description": "Output path", "required": True, "default": None},
                },
                "requires_tools": ["web_search", "write_file"],
                "requires_skills": [],
                "user_invocable": True,
                "enabled": True,
                "step_count": 2,
                "file_path": "/tmp/web-brief.md",
                "policy_modes": ["balanced", "full"],
                "execution_boundaries": ["external_read", "workspace_write"],
                "risk_level": "medium",
                "accepts_secret_refs": False,
                "is_available": False,
                "missing_tools": ["write_file"],
                "missing_skills": [],
            }
        ]
        with (
            patch("src.api.workflows.workflow_manager", mock_mgr),
            patch(
                "src.api.workflows.get_base_tools_and_active_skills",
                return_value=([SimpleNamespace(name="web_search")], [], "disabled"),
            ),
        ):
            resp = await client.get("/api/workflows")
        assert resp.status_code == 200
        assert resp.json()["workflows"][0]["tool_name"] == "workflow_web_brief_to_file"
        assert set(resp.json()["workflows"][0]["inputs"]) == {"query", "file_path"}
        assert resp.json()["workflows"][0]["approval_behavior"] == "never"
        assert resp.json()["workflows"][0]["requires_approval"] is False
        assert resp.json()["workflows"][0]["accepts_secret_refs"] is False
        assert resp.json()["workflows"][0]["is_available"] is False
        assert resp.json()["workflows"][0]["missing_tools"] == ["write_file"]

    @pytest.mark.asyncio
    async def test_update_workflow_logs(self, client):
        mock_mgr = MagicMock()
        mock_mgr.disable.return_value = True
        mock_log = AsyncMock()
        with (
            patch("src.api.workflows.workflow_manager", mock_mgr),
            patch("src.api.workflows.log_integration_event", mock_log),
        ):
            resp = await client.put("/api/workflows/web-brief-to-file", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "updated",
            "name": "web-brief-to-file",
            "enabled": False,
        }
        mock_log.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reload_workflows_logs(self, client):
        mock_mgr = MagicMock()
        mock_mgr.reload.return_value = [
            {"name": "web-brief-to-file", "enabled": True},
            {"name": "goal-snapshot-to-file", "enabled": False},
        ]
        mock_log = AsyncMock()
        with (
            patch("src.api.workflows.workflow_manager", mock_mgr),
            patch("src.api.workflows.log_integration_event", mock_log),
        ):
            resp = await client.post("/api/workflows/reload")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "reloaded"
        assert payload["count"] == 2
        mock_log.assert_awaited_once()


class TestWorkflowSurfaces:
    @pytest.mark.asyncio
    async def test_tools_api_includes_workflow_metadata(self, client):
        ctx = SimpleNamespace(tool_policy_mode="balanced", mcp_policy_mode="disabled")
        workflow_tool = MagicMock()
        workflow_tool.name = "workflow_web_brief_to_file"
        workflow_tool.description = "fallback description"

        with (
            patch("src.tools.policy.context_manager.get_context", return_value=ctx),
            patch("src.api.tools.get_tools", return_value=[workflow_tool]),
            patch("src.api.tools.workflow_manager.get_tool_metadata", return_value={
                "description": "Search the web and save a note",
                "policy_modes": ["balanced", "full"],
                "risk_level": "medium",
                "execution_boundaries": ["external_read", "workspace_write"],
            }),
        ):
            resp = await client.get("/api/tools")
        assert resp.status_code == 200
        assert resp.json() == [{
            "name": "workflow_web_brief_to_file",
            "description": "Search the web and save a note",
            "policy_modes": ["balanced", "full"],
            "requires_approval": False,
            "approval_behavior": "never",
            "risk_level": "medium",
            "execution_boundaries": ["external_read", "workspace_write"],
            "accepts_secret_refs": False,
        }]

    def test_factory_wraps_high_risk_workflows_for_approval(self, async_db):
        class DummyWorkflowTool:
            name = "workflow_shell_run"
            description = "Run shell via workflow"
            inputs = {"code": {"type": "string", "description": "Code"}}
            output_type = "string"

            def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
                return "ok"

        with (
            patch("src.agent.factory.discover_tools", return_value=[]),
            patch("src.agent.factory.mcp_manager.get_tools", return_value=[]),
            patch("src.agent.factory.skill_manager.get_active_skills", return_value=[]),
            patch("src.agent.factory.workflow_manager.build_workflow_tools", return_value=[DummyWorkflowTool()]),
            patch("src.agent.factory.workflow_manager.get_tool_metadata", return_value={
                "description": "Run shell via workflow",
                "policy_modes": ["full"],
                "risk_level": "high",
                "execution_boundaries": ["sandbox_execution"],
            }),
        ):
            workflow_tool = next(tool for tool in get_tools() if tool.name == "workflow_shell_run")

        tokens = set_runtime_context("s1", "high_risk")
        try:
            with pytest.raises(ApprovalRequired):
                workflow_tool(code="print('hi')")
        finally:
            reset_runtime_context(tokens)

    def test_factory_keeps_medium_risk_workflows_direct(self, async_db):
        class DummyWorkflowTool:
            name = "workflow_web_brief_to_file"
            description = "Run a medium-risk workflow"
            inputs = {
                "query": {"type": "string", "description": "Query"},
                "file_path": {"type": "string", "description": "Path"},
            }
            output_type = "string"

            def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
                return "ok"

        with (
            patch("src.agent.factory.discover_tools", return_value=[]),
            patch("src.agent.factory.mcp_manager.get_tools", return_value=[]),
            patch("src.agent.factory.skill_manager.get_active_skills", return_value=[]),
            patch("src.agent.factory.workflow_manager.build_workflow_tools", return_value=[DummyWorkflowTool()]),
            patch("src.agent.factory.workflow_manager.get_tool_metadata", return_value={
                "description": "Run a medium-risk workflow",
                "policy_modes": ["balanced", "full"],
                "risk_level": "medium",
                "execution_boundaries": ["external_read", "workspace_write"],
            }),
        ):
            workflow_tool = next(tool for tool in get_tools() if tool.name == "workflow_web_brief_to_file")

        assert not isinstance(workflow_tool, ApprovalTool)

        tokens = set_runtime_context("s1", "high_risk")
        try:
            assert workflow_tool(query="seraph", file_path="notes.md") == "ok"
        finally:
            reset_runtime_context(tokens)

    def test_factory_forces_approval_for_mcp_workflows_when_mcp_mode_requires_it(self, async_db):
        class DummyWorkflowTool:
            name = "workflow_mcp_export"
            description = "Export via MCP"
            inputs = {"file_path": {"type": "string", "description": "Path"}}
            output_type = "string"

            def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
                return "ok"

        ctx = SimpleNamespace(tool_policy_mode="full", mcp_policy_mode="approval")
        with (
            patch("src.tools.policy.context_manager.get_context", return_value=ctx),
            patch("src.agent.factory.discover_tools", return_value=[]),
            patch("src.agent.factory.mcp_manager.get_tools", return_value=[]),
            patch("src.agent.factory.skill_manager.get_active_skills", return_value=[]),
            patch("src.agent.factory.workflow_manager.build_workflow_tools", return_value=[DummyWorkflowTool()]),
            patch("src.agent.factory.workflow_manager.get_tool_metadata", return_value={
                "description": "Export via MCP",
                "policy_modes": ["full"],
                "risk_level": "high",
                "execution_boundaries": ["external_mcp", "workspace_write"],
            }),
        ):
            workflow_tool = next(tool for tool in get_tools() if tool.name == "workflow_mcp_export")

        tokens = set_runtime_context("s1", "off")
        try:
            with pytest.raises(ApprovalRequired):
                workflow_tool(file_path="tasks.md")
        finally:
            reset_runtime_context(tokens)

    def test_build_all_specialists_adds_workflow_runner(self):
        native_tool = MagicMock()
        native_tool.name = "read_file"
        workflow_tool = MagicMock()
        workflow_tool.name = "workflow_goal_snapshot_to_file"

        with patch("src.agent.specialists.create_specialist") as mock_create_specialist:
            with (
                patch("src.agent.specialists.discover_tools", return_value=[native_tool]),
                patch("src.agent.specialists.filter_tools", side_effect=lambda tools, *_args, **_kwargs: list(tools)),
                patch("src.agent.specialists.wrap_tools_for_secret_refs", side_effect=lambda tools: list(tools)),
                patch("src.agent.specialists.wrap_tools_for_audit", side_effect=lambda tools, **_kwargs: list(tools)),
                patch("src.agent.specialists.wrap_tools_for_approval", side_effect=lambda tools, **_kwargs: list(tools)),
                patch("src.agent.specialists.wrap_tools_with_forced_approval", side_effect=lambda tools, **_kwargs: list(tools)),
                patch("src.agent.specialists.mcp_manager.get_config", return_value=[]),
                patch("src.agent.specialists.skill_manager.get_active_skills", return_value=[]),
                patch("src.agent.specialists.workflow_manager.build_workflow_tools", return_value=[workflow_tool]),
            ):
                mock_create_specialist.side_effect = lambda name, description, tools, temperature, max_steps: SimpleNamespace(
                    name=name,
                    description=description,
                    tools=tools,
                )
                from src.agent.specialists import build_all_specialists

                specialists = build_all_specialists()

        workflow_runner = next(s for s in specialists if s.name == "workflow_runner")
        assert workflow_runner.tools == [workflow_tool]

    def test_build_all_specialists_keeps_medium_risk_workflows_direct(self):
        workflow_tool = MagicMock()
        workflow_tool.name = "workflow_web_brief_to_file"
        workflow_tool.description = "Search and save"
        workflow_tool.inputs = {"query": {"type": "string", "description": "Query"}}
        workflow_tool.output_type = "string"

        with patch("src.agent.specialists.create_specialist") as mock_create_specialist:
            with (
                patch("src.agent.specialists.discover_tools", return_value=[]),
                patch("src.agent.specialists.filter_tools", side_effect=lambda tools, *_args, **_kwargs: list(tools)),
                patch("src.agent.specialists.wrap_tools_for_secret_refs", side_effect=lambda tools: list(tools)),
                patch("src.agent.specialists.wrap_tools_for_audit", side_effect=lambda tools, **_kwargs: list(tools)),
                patch("src.agent.specialists.mcp_manager.get_config", return_value=[]),
                patch("src.agent.specialists.skill_manager.get_active_skills", return_value=[]),
                patch("src.agent.specialists.workflow_manager.build_workflow_tools", return_value=[workflow_tool]),
                patch("src.agent.specialists.workflow_manager.get_tool_metadata", return_value={
                    "description": "Search and save",
                    "policy_modes": ["balanced", "full"],
                    "risk_level": "medium",
                    "execution_boundaries": ["external_read", "workspace_write"],
                }),
            ):
                mock_create_specialist.side_effect = lambda name, description, tools, temperature, max_steps: SimpleNamespace(
                    name=name,
                    description=description,
                    tools=tools,
                )
                from src.agent.specialists import build_all_specialists

                specialists = build_all_specialists()

        workflow_runner = next(s for s in specialists if s.name == "workflow_runner")
        assert len(workflow_runner.tools) == 1
        assert not isinstance(workflow_runner.tools[0], ApprovalTool)
