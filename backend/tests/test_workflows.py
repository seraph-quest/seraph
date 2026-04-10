"""Tests for reusable workflow composition."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.approval.repository import fingerprint_tool_call
from src.extensions.registry import default_manifest_roots_for_workspace
from src.workflows.loader import Workflow, WorkflowStep, _parse_workflow_file, load_workflows
from src.workflows.manager import (
    WorkflowManager,
    WorkflowTool,
    _approval_context_for_workflow,
    _checkpoint_context_allowed,
    workflow_manager,
)
from src.approval.exceptions import ApprovalRequired
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.agent.factory import get_tools
from src.agent.session import SessionManager
from src.audit.repository import audit_repository
from src.tools.approval import ApprovalTool
from src.workflows.run_identity import build_workflow_run_identity


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

    def forward(self, *args, **kwargs):
        return self.__call__(*args, **kwargs)


def _write_manifest_workflow_package(
    root,
    *,
    package_name: str = "research-pack",
    extension_id: str = "seraph.research-pack",
    workflow_file_name: str = "packaged.md",
    workflow_name: str = "packaged-workflow",
    description: str = "Packaged workflow",
    step_tool: str = "web_search",
    manifest_reference: str | None = None,
    workflow_content: str | None = None,
):
    package_dir = root / "extensions" / package_name
    package_dir.mkdir(parents=True, exist_ok=True)
    reference = manifest_reference or f"workflows/{workflow_file_name}"
    (package_dir / "manifest.yaml").write_text(
        "id: " + extension_id + "\n"
        "version: 2026.3.21\n"
        "display_name: Research Pack\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  workflows:\n"
        f"    - {reference}\n"
        "permissions:\n"
        "  tools: []\n"
        "  network: false\n",
        encoding="utf-8",
    )
    if workflow_content is None:
        workflow_content = (
            "---\n"
            f"name: {workflow_name}\n"
            f"description: {description}\n"
            "requires:\n"
            f"  tools: [{step_tool}]\n"
            "steps:\n"
            "  - id: run\n"
            f"    tool: {step_tool}\n"
            "    arguments: {}\n"
            "---\n\n"
            "Packaged workflow.\n"
        )
    workflows_path = package_dir / "workflows"
    workflows_path.mkdir(exist_ok=True)
    (workflows_path / workflow_file_name).write_text(workflow_content, encoding="utf-8")
    return package_dir


def _write_manifest_canvas_package(
    root,
    *,
    package_name: str = "canvas-pack",
    extension_id: str = "seraph.canvas-pack",
    display_name: str = "Canvas Pack",
    title: str = "Guardian Board",
):
    package_dir = root / "extensions" / package_name
    (package_dir / "canvas").mkdir(parents=True, exist_ok=True)
    (package_dir / "manifest.yaml").write_text(
        "id: " + extension_id + "\n"
        "version: 2026.3.24\n"
        f"display_name: {display_name}\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  canvas_outputs:\n"
        "    - canvas/guardian-board.yaml\n",
        encoding="utf-8",
    )
    (package_dir / "canvas" / "guardian-board.yaml").write_text(
        "name: guardian-board\n"
        f"title: {title}\n"
        "description: Structured workflow board.\n"
        "surface_kind: board\n"
        "sections:\n"
        "  - Summary\n"
        "  - Steps\n",
        encoding="utf-8",
    )
    return package_dir


def _write_manifest_workflow_runtime_package(
    root,
    *,
    package_name: str = "runtime-pack",
    extension_id: str = "seraph.runtime-pack",
    display_name: str = "Runtime Pack",
    default_output_surface: str = "guardian-board",
):
    package_dir = root / "extensions" / package_name
    (package_dir / "runtimes").mkdir(parents=True, exist_ok=True)
    (package_dir / "manifest.yaml").write_text(
        "id: " + extension_id + "\n"
        "version: 2026.3.24\n"
        f"display_name: {display_name}\n"
        "kind: capability-pack\n"
        "compatibility:\n"
        "  seraph: \">=2026.4.10\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  workflow_runtimes:\n"
        "    - runtimes/openprose.yaml\n",
        encoding="utf-8",
    )
    (package_dir / "runtimes" / "openprose.yaml").write_text(
        "name: openprose\n"
        "engine_kind: openprose\n"
        "description: Narrative drafting runtime.\n"
        "delegation_mode: inline\n"
        "checkpoint_policy: step\n"
        "structured_output: true\n"
        f"default_output_surface: {default_output_surface}\n",
        encoding="utf-8",
    )
    return package_dir


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

    def test_parse_workflow_tracks_runtime_profile_and_output_surface(self, tmp_path):
        workflow_path = tmp_path / "runtime-aware.md"
        workflow_path.write_text(
            "---\n"
            "name: runtime-aware\n"
            "description: Runtime-aware workflow\n"
            "runtime_profile: openprose\n"
            "output_surface: guardian-board\n"
            "requires:\n"
            "  tools: [web_search]\n"
            "steps:\n"
            "  - id: search\n"
            "    tool: web_search\n"
            "    arguments: {}\n"
            "---\n\n"
            "Runtime-aware workflow.\n",
            encoding="utf-8",
        )

        workflow = _parse_workflow_file(str(workflow_path))

        assert workflow is not None
        assert workflow.runtime_profile == "openprose"
        assert workflow.output_surface == "guardian-board"

    def test_parse_workflow_tracks_artifact_input_metadata(self, tmp_path):
        workflow_path = tmp_path / "artifact-aware.md"
        workflow_path.write_text(
            "---\n"
            "name: artifact-aware\n"
            "description: Workflow with typed artifact input\n"
            "requires:\n"
            "  tools: [write_file]\n"
            "inputs:\n"
            "  source_path:\n"
            "    type: string\n"
            "    description: Existing workspace artifact\n"
            "    artifact_input: true\n"
            "    artifact_types: [markdown_document, workspace_file]\n"
            "  destination_path:\n"
            "    type: string\n"
            "    description: Output path\n"
            "steps:\n"
            "  - id: save\n"
            "    tool: write_file\n"
            "    arguments:\n"
            "      file_path: \"{{ destination_path }}\"\n"
            "      content: \"Copied from {{ source_path }}\"\n"
            "---\n\n"
            "Artifact-aware workflow.\n",
            encoding="utf-8",
        )

        workflow = _parse_workflow_file(str(workflow_path))

        assert workflow is not None
        assert workflow.inputs["source_path"]["artifact_input"] is True
        assert workflow.inputs["source_path"]["artifact_types"] == [
            "markdown_document",
            "workspace_file",
        ]
        assert workflow.inputs["destination_path"]["artifact_input"] is False
        assert workflow.inputs["destination_path"]["artifact_types"] == []


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

    def test_active_workflow_gating_uses_step_tools_even_without_requires_tools(self, workflows_dir):
        mgr = WorkflowManager()
        mgr.init(workflows_dir)
        workflow = mgr.get_workflow("web-brief-to-file")
        assert workflow is not None
        workflow.requires_tools = []

        listed = next(
            workflow
            for workflow in mgr.list_workflows(
                available_tool_names=["web_search"],
                active_skill_names=[],
            )
            if workflow["name"] == "web-brief-to-file"
        )
        assert listed["is_available"] is False
        assert listed["missing_tools"] == ["write_file"]
        assert mgr.get_active_workflows(["web_search"], []) == []

    def test_enable_disable_persists(self, workflows_dir):
        mgr = WorkflowManager()
        mgr.init(workflows_dir)
        assert mgr.disable("web-brief-to-file") is True
        assert mgr.get_workflow("web-brief-to-file").enabled is False

        config_path = os.path.join(os.path.dirname(workflows_dir), "workflows-config.json")
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["disabled"] == ["web-brief-to-file"]

    def test_runtime_availability_tracks_runtime_profiles_and_output_surfaces(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        (workflows_dir / "runtime-aware.md").write_text(
            "---\n"
            "name: runtime-aware\n"
            "description: Runtime-aware workflow\n"
            "runtime_profile: openprose\n"
            "output_surface: guardian-board\n"
            "requires:\n"
            "  tools: [web_search]\n"
            "steps:\n"
            "  - id: search\n"
            "    tool: web_search\n"
            "    arguments: {}\n"
            "---\n\n"
            "Runtime-aware workflow.\n",
            encoding="utf-8",
        )
        _write_manifest_canvas_package(tmp_path)
        manifest_roots = default_manifest_roots_for_workspace(str(tmp_path))

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir), manifest_roots=manifest_roots)

        without_runtime = next(
            item
            for item in mgr.list_workflows(available_tool_names=["web_search"], active_skill_names=[])
            if item["name"] == "runtime-aware"
        )
        assert without_runtime["is_available"] is False
        assert without_runtime["missing_runtime_profiles"] == ["openprose"]
        assert without_runtime["missing_output_surfaces"] == []

        _write_manifest_workflow_runtime_package(tmp_path)
        mgr.init(str(workflows_dir), manifest_roots=manifest_roots)
        with_runtime = next(
            item
            for item in mgr.list_workflows(available_tool_names=["web_search"], active_skill_names=[])
            if item["name"] == "runtime-aware"
        )
        assert with_runtime["is_available"] is True
        assert with_runtime["runtime_profile"] == "openprose"
        assert with_runtime["output_surface"] == "guardian-board"
        assert with_runtime["missing_runtime_profiles"] == []
        assert with_runtime["missing_output_surfaces"] == []

    def test_runtime_profile_defaults_output_surface_and_gates_active_workflows(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        (workflows_dir / "runtime-default.md").write_text(
            "---\n"
            "name: runtime-default\n"
            "description: Runtime-default workflow\n"
            "runtime_profile: openprose\n"
            "requires:\n"
            "  tools: [web_search]\n"
            "steps:\n"
            "  - id: search\n"
            "    tool: web_search\n"
            "    arguments: {}\n"
            "---\n\n"
            "Runtime-default workflow.\n",
            encoding="utf-8",
        )
        manifest_roots = default_manifest_roots_for_workspace(str(tmp_path))

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir), manifest_roots=manifest_roots)
        assert mgr.get_active_workflows(["web_search"], []) == []

        _write_manifest_workflow_runtime_package(tmp_path)
        _write_manifest_canvas_package(tmp_path)
        mgr.init(str(workflows_dir), manifest_roots=manifest_roots)

        workflow_item = next(
            item
            for item in mgr.list_workflows(available_tool_names=["web_search"], active_skill_names=[])
            if item["name"] == "runtime-default"
        )
        assert workflow_item["is_available"] is True
        assert workflow_item["output_surface"] == "guardian-board"
        active_workflows = mgr.get_active_workflows(["web_search"], [])
        assert [item.name for item in active_workflows if item.name == "runtime-default"] == ["runtime-default"]

        workflow_tool = next(
            tool
            for tool in mgr.build_workflow_tools(
                [DummyTool("web_search", lambda: "runtime result")],
                [],
            )
            if tool.name == "workflow_runtime_default"
        )
        result = workflow_tool()
        audit_payload = workflow_tool.get_audit_result_payload({}, result)
        assert audit_payload is not None
        _summary, details = audit_payload
        canvas_output = details["canvas_output"]
        assert canvas_output["surface"] == "guardian-board"
        assert canvas_output["title"] == "Guardian Board"
        assert [section["label"] for section in canvas_output["sections"]] == ["Summary", "Steps"]
        assert canvas_output["summary"] == "workflow content redacted"
        assert canvas_output["sections"][0]["item_count"] == 1

    def test_workflow_tool_audit_payload_includes_canvas_output(self):
        workflow = Workflow(
            name="runtime-aware",
            description="Runtime-aware workflow",
            inputs={"query": {"type": "string", "required": True}},
            steps=[WorkflowStep(tool="web_search", arguments={"query": "{{ query }}"}, id="search")],
            requires_tools=["web_search"],
            result_template="Done for {{ query }}.",
            output_surface="guardian-board",
        )
        workflow_tool = WorkflowTool(
            workflow,
            tools_by_name={"web_search": DummyTool("web_search", lambda query: f"result for {query}")},
        )

        result = workflow_tool(query="seraph")
        audit_payload = workflow_tool.get_audit_result_payload({"query": "seraph"}, result)

        assert audit_payload is not None
        _summary, details = audit_payload
        canvas_output = details["canvas_output"]
        assert canvas_output["surface"] == "guardian-board"
        assert canvas_output["title"] == "runtime-aware"
        assert canvas_output["section_count"] == 2
        assert canvas_output["summary"] == "workflow content redacted"
        assert all("items" not in section for section in canvas_output["sections"])

    def test_workflow_tool_resolves_legacy_shell_execute_alias(self):
        workflow = Workflow(
            name="legacy-shell-workflow",
            description="Legacy shell alias workflow",
            inputs={},
            steps=[WorkflowStep(id="run", tool="shell_execute", arguments={"code": "print('hello')"})],
            requires_tools=["shell_execute"],
        )
        execute_code = DummyTool("execute_code", lambda code: f"ran {code}")
        workflow_tool = WorkflowTool(workflow, {"execute_code": execute_code})

        result = workflow_tool()
        assert "ran print('hello')" in result
        assert "shell_execute" not in result
        assert "execute_code" in result
        assert execute_code.calls == [{"code": "print('hello')"}]
        audit_payload = workflow_tool.get_audit_result_payload({}, "ignored")
        assert audit_payload is not None
        assert audit_payload[1]["step_tools"] == ["execute_code"]
        assert audit_payload[1]["step_records"][0]["tool"] == "execute_code"

        mgr = WorkflowManager()
        mgr._workflows = [workflow]
        assert mgr.list_workflows()[0]["requires_tools"] == ["execute_code"]
        assert mgr.get_tool_metadata(workflow.tool_name)["requires_tools"] == ["execute_code"]

    def test_workflow_tool_resumes_from_checkpoint_state(self):
        workflow = Workflow(
            name="web-brief-to-file",
            description="Search the web and save a note",
            inputs={
                "query": {"type": "string", "required": True},
                "file_path": {"type": "string", "required": True},
            },
            steps=[
                WorkflowStep(id="search", tool="web_search", arguments={"query": "{{ query }}"}),
                WorkflowStep(
                    id="save",
                    tool="write_file",
                    arguments={
                        "file_path": "{{ file_path }}",
                        "content": "Search results\n\n{{ steps.search.result }}",
                    },
                ),
            ],
            requires_tools=["web_search", "write_file"],
            result_template="Saved search results for {{ query }} to {{ file_path }}.",
        )
        search = DummyTool("web_search", lambda query: f"fresh result for {query}")
        write = DummyTool("write_file", lambda file_path, content: f"wrote {file_path}: {content}")
        workflow_tool = WorkflowTool(workflow, {"web_search": search, "write_file": write})
        parent_run_identity = "session-1:workflow_web_brief_to_file:parent"
        checkpoint_payload = {
            "workflow_name": "web-brief-to-file",
            "checkpoint_context": {
                "search": {
                    "tool": "web_search",
                    "arguments": {"query": "seraph"},
                    "result": "cached search result",
                }
            },
            "step_records": [
                {
                    "id": "search",
                    "tool": "web_search",
                    "status": "succeeded",
                    "artifact_paths": [],
                    "started_at": "2026-03-28T10:00:00Z",
                    "completed_at": "2026-03-28T10:00:01Z",
                    "duration_ms": 321,
                }
            ],
        }

        with patch(
            "src.workflows.manager._load_workflow_checkpoint_payload",
            AsyncMock(return_value=checkpoint_payload),
        ):
            result = workflow_tool(
                query="seraph",
                file_path="notes/brief.md",
                _seraph_resume_from_step="save",
                _seraph_parent_run_identity=parent_run_identity,
                _seraph_root_run_identity=parent_run_identity,
                _seraph_branch_kind="retry_failed_step",
                _seraph_branch_depth=1,
            )

        assert result == "Saved search results for seraph to notes/brief.md."
        assert search.calls == []
        assert write.calls == [
            {
                "file_path": "notes/brief.md",
                "content": "Search results\n\ncached search result",
            }
        ]
        audit_payload = workflow_tool.get_audit_result_payload({}, result)
        assert audit_payload is not None
        _summary, details = audit_payload
        assert details["parent_run_identity"] == parent_run_identity
        assert details["root_run_identity"] == parent_run_identity
        assert details["branch_kind"] == "retry_failed_step"
        assert details["resume_from_step"] == "save"
        assert details["branch_depth"] == 1
        assert details["checkpoint_context_available"] is True
        assert details["step_records"][0]["status"] == "checkpoint_reused"
        assert details["step_records"][0]["reused_from_run_identity"] == parent_run_identity
        assert details["step_records"][1]["status"] == "succeeded"

    def test_workflow_tool_failure_payload_keeps_checkpoint_and_failed_step_context(self):
        workflow = Workflow(
            name="web-brief-to-file",
            description="Search the web and save a note",
            inputs={
                "query": {"type": "string", "required": True},
                "file_path": {"type": "string", "required": True},
            },
            steps=[
                WorkflowStep(id="search", tool="web_search", arguments={"query": "{{ query }}"}),
                WorkflowStep(
                    id="save",
                    tool="write_file",
                    arguments={
                        "file_path": "{{ file_path }}",
                        "content": "Search results\n\n{{ steps.search.result }}",
                    },
                ),
            ],
            requires_tools=["web_search", "write_file"],
        )
        workflow_tool = WorkflowTool(
            workflow,
            {
                "web_search": DummyTool("web_search", lambda query: f"fresh result for {query}"),
                "write_file": DummyTool("write_file", lambda **_kwargs: (_ for _ in ()).throw(PermissionError("denied"))),
            },
        )
        parent_run_identity = "session-1:workflow_web_brief_to_file:parent"
        checkpoint_payload = {
            "workflow_name": "web-brief-to-file",
            "checkpoint_context": {
                "search": {
                    "tool": "web_search",
                    "arguments": {"query": "seraph"},
                    "result": "cached search result",
                }
            },
            "step_records": [
                {
                    "id": "search",
                    "tool": "web_search",
                    "status": "succeeded",
                    "artifact_paths": [],
                }
            ],
        }

        with (
            patch(
                "src.workflows.manager._load_workflow_checkpoint_payload",
                AsyncMock(return_value=checkpoint_payload),
            ),
            pytest.raises(PermissionError, match="denied"),
        ):
            workflow_tool(
                query="seraph",
                file_path="notes/brief.md",
                _seraph_resume_from_step="save",
                _seraph_parent_run_identity=parent_run_identity,
                _seraph_root_run_identity=parent_run_identity,
                _seraph_branch_kind="retry_failed_step",
                _seraph_branch_depth=2,
            )

        failure_payload = workflow_tool.get_audit_failure_payload({}, PermissionError("denied"))
        assert failure_payload is not None
        summary, details = failure_payload
        assert summary == "workflow_web_brief_to_file failed"
        assert details["parent_run_identity"] == parent_run_identity
        assert details["branch_depth"] == 2
        assert details["checkpoint_context_available"] is True
        assert details["failed_step_ids"] == ["save"]
        assert details["step_records"][0]["status"] == "checkpoint_reused"
        assert details["step_records"][1]["status"] == "failed"
        assert details["step_records"][1]["error_kind"] == "PermissionError"
        assert details["error"] == "denied"

def test_workflow_tool_resume_rejects_when_delegation_boundary_changes():
    workflow = Workflow(
        name="delegation-replay",
        description="Resume delegated work",
        inputs={"topic": {"type": "string", "required": True}},
        steps=[
            WorkflowStep(id="search", tool="web_search", arguments={"query": "{{ topic }}"}),
            WorkflowStep(
                id="delegate",
                tool="delegate_task",
                arguments={"task": "Investigate {{ topic }}"},
            ),
        ],
        requires_tools=["web_search", "delegate_task"],
    )
    workflow_tool = WorkflowTool(
        workflow,
        {
            "web_search": DummyTool("web_search", lambda query: f"fresh result for {query}"),
            "delegate_task": DummyTool("delegate_task", lambda task: f"delegated {task}"),
        },
    )
    workflow_tool.get_approval_context = lambda _arguments: {
        "workflow_name": "delegation-replay",
        "risk_level": "high",
        "execution_boundaries": ["delegation", "external_mcp"],
        "accepts_secret_refs": True,
        "step_tools": ["delegate_task", "web_search"],
        "delegated_specialists": ["mcp_github"],
    }
    parent_run_identity = "session-1:workflow_delegation_replay:parent"
    checkpoint_payload = {
        "workflow_name": "delegation-replay",
        "approval_context": {
            "workflow_name": "delegation-replay",
            "risk_level": "high",
            "execution_boundaries": ["delegation", "external_mcp"],
            "accepts_secret_refs": True,
            "step_tools": ["web_search", "delegate_task"],
            "delegated_specialists": ["mcp_jira"],
        },
        "checkpoint_context": {
            "search": {
                "tool": "web_search",
                "arguments": {"query": "seraph"},
                "result": "cached search result",
            }
        },
        "step_records": [{"id": "search", "tool": "web_search", "status": "succeeded"}],
    }

    with (
        patch(
            "src.workflows.manager._load_workflow_checkpoint_payload",
            AsyncMock(return_value=checkpoint_payload),
        ),
        pytest.raises(RuntimeError, match="trust boundary"),
    ):
        workflow_tool(
            topic="seraph",
            _seraph_resume_from_step="delegate",
            _seraph_parent_run_identity=parent_run_identity,
            _seraph_root_run_identity=parent_run_identity,
        )


def test_workflow_tool_resume_rejects_legacy_checkpoint_for_authenticated_surface():
    workflow = Workflow(
        name="authenticated-replay",
        description="Resume authenticated source work",
        inputs={"query": {"type": "string", "required": True}},
        steps=[
            WorkflowStep(id="search", tool="web_search", arguments={"query": "{{ query }}"}),
            WorkflowStep(id="save", tool="write_file", arguments={"file_path": "notes/out.md", "content": "{{ steps.search.result }}"}),
        ],
        requires_tools=["web_search", "write_file"],
    )
    workflow_tool = WorkflowTool(
        workflow,
        {
            "web_search": DummyTool("web_search", lambda query: f"fresh result for {query}"),
            "write_file": DummyTool("write_file", lambda file_path, content: f"saved {file_path}: {content}"),
        },
    )
    workflow_tool.get_approval_context = lambda _arguments: {
        "workflow_name": "authenticated-replay",
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "accepts_secret_refs": False,
        "step_tools": ["web_search", "write_file"],
        "authenticated_source": True,
        "source_systems": [{"server_name": "github", "hostname": "api.github.com", "source": "extension", "authenticated_source": True}],
    }
    parent_run_identity = "session-1:workflow_authenticated_replay:parent"
    checkpoint_payload = {
        "workflow_name": "authenticated-replay",
        "checkpoint_context": {
            "search": {
                "tool": "web_search",
                "arguments": {"query": "seraph"},
                "result": "cached search result",
            }
        },
        "step_records": [{"id": "search", "tool": "web_search", "status": "succeeded"}],
    }

    with (
        patch(
            "src.workflows.manager._load_workflow_checkpoint_payload",
            AsyncMock(return_value=checkpoint_payload),
        ),
        pytest.raises(RuntimeError, match="predates trust-boundary tracking"),
    ):
        workflow_tool(
            query="seraph",
            _seraph_resume_from_step="save",
            _seraph_parent_run_identity=parent_run_identity,
            _seraph_root_run_identity=parent_run_identity,
        )

    def test_workflow_tool_audit_call_payload_uses_normalized_control_inputs_for_fingerprint(self):
        workflow = Workflow(
            name="web-brief",
            description="Search the web",
            inputs={"query": {"type": "string", "required": True}},
            steps=[
                WorkflowStep(id="search", tool="web_search", arguments={"query": "{{ query }}"}),
            ],
            requires_tools=["web_search"],
            result_template="{{ steps.search.result }}",
        )
        search = DummyTool("web_search", lambda query: f"fresh result for {query}")
        workflow_tool = WorkflowTool(workflow, {"web_search": search})

        call_payload = workflow_tool.get_audit_call_payload(
            {"query": "seraph", "_seraph_branch_depth": "1"}
        )
        result = workflow_tool(query="seraph", _seraph_branch_depth="1")
        result_payload = workflow_tool.get_audit_result_payload({}, result)

        assert result_payload is not None
        assert call_payload[1]["branch_depth"] == 1
        assert call_payload[1]["run_fingerprint"] == result_payload[1]["run_fingerprint"]

    def test_workflow_tool_audit_call_payload_keeps_precall_validation_boundary(self):
        workflow = Workflow(
            name="web-brief",
            description="Search the web",
            inputs={"query": {"type": "string", "required": True}},
            steps=[
                WorkflowStep(id="search", tool="web_search", arguments={"query": "{{ query }}"}),
            ],
            requires_tools=["web_search"],
            result_template="{{ steps.search.result }}",
        )
        workflow_tool = WorkflowTool(
            workflow,
            {"web_search": DummyTool("web_search", lambda query: f"fresh result for {query}")},
        )

        call_payload = workflow_tool.get_audit_call_payload({"_seraph_branch_depth": "1"})

        assert call_payload[1]["branch_depth"] == 1
        assert isinstance(call_payload[1]["run_fingerprint"], str)
        assert call_payload[1]["run_fingerprint"]

    @pytest.mark.asyncio
    async def test_load_workflow_checkpoint_payload_uses_call_event_id_to_disambiguate_duplicate_fingerprints(
        self,
        async_db,
    ):
        with patch("src.workflows.manager.get_session", async_db):
            older_call = await audit_repository.log_event(
                session_id="session-1",
                actor="agent",
                event_type="tool_call",
                tool_name="workflow_web_brief_to_file",
                summary="Calling workflow",
                details={"run_fingerprint": "shared-fingerprint", "arguments": {"query": "seraph"}},
            )
            await audit_repository.log_event(
                session_id="session-1",
                actor="agent",
                event_type="tool_result",
                tool_name="workflow_web_brief_to_file",
                summary="workflow_web_brief_to_file succeeded",
                details={
                    "workflow_name": "web-brief-to-file",
                    "run_fingerprint": "shared-fingerprint",
                    "call_event_id": older_call.id,
                    "checkpoint_context": {
                        "search": {
                            "tool": "web_search",
                            "arguments": {"query": "seraph"},
                            "result": "older cached result",
                        }
                    },
                    "step_records": [{"id": "search", "tool": "web_search", "status": "succeeded"}],
                },
            )
            newer_call = await audit_repository.log_event(
                session_id="session-1",
                actor="agent",
                event_type="tool_call",
                tool_name="workflow_web_brief_to_file",
                summary="Calling workflow",
                details={"run_fingerprint": "shared-fingerprint", "arguments": {"query": "seraph"}},
            )
            await audit_repository.log_event(
                session_id="session-1",
                actor="agent",
                event_type="tool_result",
                tool_name="workflow_web_brief_to_file",
                summary="workflow_web_brief_to_file succeeded",
                details={
                    "workflow_name": "web-brief-to-file",
                    "run_fingerprint": "shared-fingerprint",
                    "call_event_id": newer_call.id,
                    "checkpoint_context": {
                        "search": {
                            "tool": "web_search",
                            "arguments": {"query": "seraph"},
                            "result": "newer cached result",
                        }
                    },
                    "step_records": [{"id": "search", "tool": "web_search", "status": "succeeded"}],
                },
            )

            from src.workflows.manager import _load_workflow_checkpoint_payload

            details = await _load_workflow_checkpoint_payload(
                build_workflow_run_identity(
                    "session-1",
                    "workflow_web_brief_to_file",
                    "shared-fingerprint",
                    run_discriminator=older_call.id,
                )
            )

        assert details is not None
        assert details["checkpoint_context"]["search"]["result"] == "older cached result"

    def test_manifest_backed_workflow_with_missing_manifest_permissions_is_not_active(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        _write_manifest_workflow_package(
            tmp_path,
            workflow_name="packaged-workflow",
            description="Packaged workflow",
            step_tool="write_file",
        )

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir), manifest_roots=[str(tmp_path / "extensions")])

        listed = {
            workflow["name"]: workflow
            for workflow in mgr.list_workflows(
                available_tool_names=["write_file"],
                active_skill_names=[],
            )
        }
        assert listed["packaged-workflow"]["permission_status"] == "insufficient"
        assert listed["packaged-workflow"]["missing_manifest_tools"] == ["write_file"]
        assert listed["packaged-workflow"]["is_available"] is False
        assert mgr.get_active_workflows(["write_file"], []) == []

    def test_init_loads_manifest_backed_workflows_alongside_legacy_workflows(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        (workflows_dir / "legacy.md").write_text(
            "---\n"
            "name: legacy-workflow\n"
            "description: Legacy workflow\n"
            "requires:\n"
            "  tools: [web_search]\n"
            "steps:\n"
            "  - id: search\n"
            "    tool: web_search\n"
            "    arguments: {}\n"
            "---\n\n"
            "Legacy workflow.\n",
            encoding="utf-8",
        )
        _write_manifest_workflow_package(
            tmp_path,
            workflow_name="packaged-workflow",
            description="Packaged workflow",
            step_tool="write_file",
        )

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir), manifest_roots=[str(tmp_path / "extensions")])

        listed = {workflow["name"]: workflow for workflow in mgr.list_workflows()}
        assert set(listed) == {"legacy-workflow", "packaged-workflow"}
        assert listed["legacy-workflow"]["source"] == "legacy"
        assert listed["legacy-workflow"]["extension_id"].startswith("legacy.workflows.")
        assert listed["packaged-workflow"]["source"] == "manifest"
        assert listed["packaged-workflow"]["extension_id"] == "seraph.research-pack"

    def test_manifest_backed_workflow_parse_errors_surface_in_diagnostics(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        _write_manifest_workflow_package(
            tmp_path,
            package_name="broken-pack",
            extension_id="seraph.broken-pack",
            workflow_file_name="broken.md",
            manifest_reference="workflows/broken.md",
            workflow_content="not frontmatter",
        )

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir), manifest_roots=[str(tmp_path / "extensions")])

        diagnostics = mgr.get_diagnostics()
        assert diagnostics["loaded_count"] == 0
        assert diagnostics["error_count"] == 1
        assert diagnostics["load_errors"][0]["phase"] == "manifest-workflows"
        assert diagnostics["load_errors"][0]["file_path"].endswith("broken.md")

    def test_manifest_workflow_layout_errors_surface_without_workflows_directory(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        package_dir = tmp_path / "extensions" / "broken-layout-pack"
        package_dir.mkdir(parents=True)
        (package_dir / "manifest.yaml").write_text(
            "id: seraph.broken-layout-pack\n"
            "version: 2026.3.21\n"
            "display_name: Broken Layout Pack\n"
            "kind: capability-pack\n"
            "compatibility:\n"
            "  seraph: \">=2026.4.10\"\n"
            "publisher:\n"
            "  name: Seraph\n"
            "trust: local\n"
            "contributes:\n"
            "  workflows:\n"
            "    - wrong-dir/packaged.md\n"
            "permissions:\n"
            "  tools: []\n"
            "  network: false\n",
            encoding="utf-8",
        )

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir), manifest_roots=[str(tmp_path / "extensions")])

        diagnostics = mgr.get_diagnostics()
        assert diagnostics["loaded_count"] == 0
        assert diagnostics["error_count"] == 1
        assert diagnostics["load_errors"][0]["phase"] == "manifest"
        assert diagnostics["load_errors"][0]["file_path"].endswith("manifest.yaml")

    def test_malformed_manifest_errors_surface_as_shared_manifest_errors(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        package_dir = tmp_path / "extensions" / "broken-manifest-pack"
        package_dir.mkdir(parents=True)
        (package_dir / "manifest.yaml").write_text("id: [broken\n", encoding="utf-8")

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir), manifest_roots=[str(tmp_path / "extensions")])

        diagnostics = mgr.get_diagnostics()
        assert diagnostics["loaded_count"] == 0
        assert diagnostics["error_count"] == 0
        assert diagnostics["shared_error_count"] == 1
        assert diagnostics["shared_manifest_errors"][0]["phase"] == "manifest"
        assert diagnostics["shared_manifest_errors"][0]["file_path"].endswith("manifest.yaml")

    def test_unrelated_manifest_validation_errors_stay_out_of_workflow_load_errors(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        package_dir = tmp_path / "extensions" / "mixed-pack"
        workflows_path = package_dir / "workflows"
        workflows_path.mkdir(parents=True)
        (workflows_path / "packaged.md").write_text(
            "---\n"
            "name: packaged-workflow\n"
            "description: Packaged workflow\n"
            "requires:\n"
            "  tools: [web_search]\n"
            "steps:\n"
            "  - id: run\n"
            "    tool: web_search\n"
            "    arguments: {}\n"
            "---\n\n"
            "Packaged workflow.\n",
            encoding="utf-8",
        )
        (package_dir / "manifest.yaml").write_text(
            "id: seraph.mixed-pack\n"
            "version: 2026.3.21\n"
            "display_name: Mixed Pack\n"
            "kind: capability-pack\n"
            "compatibility:\n"
            "  seraph: \">=2026.4.10\"\n"
            "publisher:\n"
            "  name: Seraph\n"
            "trust: LOCAL\n"
            "contributes:\n"
            "  workflows:\n"
            "    - workflows/packaged.md\n"
            "permissions:\n"
            "  tools: []\n"
            "  network: false\n",
            encoding="utf-8",
        )

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir), manifest_roots=[str(tmp_path / "extensions")])

        diagnostics = mgr.get_diagnostics()
        assert diagnostics["error_count"] == 0
        assert diagnostics["shared_error_count"] == 1
        assert diagnostics["shared_manifest_errors"][0]["phase"] == "manifest"
        assert diagnostics["shared_manifest_errors"][0]["file_path"].endswith("manifest.yaml")

    def test_incompatible_manifest_backed_workflow_stays_in_workflow_load_errors(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        package_dir = tmp_path / "extensions" / "future-pack"
        workflows_path = package_dir / "workflows"
        workflows_path.mkdir(parents=True)
        (workflows_path / "packaged.md").write_text(
            "---\n"
            "name: packaged-workflow\n"
            "description: Packaged workflow\n"
            "requires:\n"
            "  tools: [web_search]\n"
            "steps:\n"
            "  - id: run\n"
            "    tool: web_search\n"
            "    arguments: {}\n"
            "---\n\n"
            "Packaged workflow.\n",
            encoding="utf-8",
        )
        (package_dir / "manifest.yaml").write_text(
            "id: seraph.future-pack\n"
            "version: 2026.3.21\n"
            "display_name: Future Pack\n"
            "kind: capability-pack\n"
            "compatibility:\n"
            "  seraph: \">=9999.1.1\"\n"
            "publisher:\n"
            "  name: Seraph\n"
            "trust: local\n"
            "contributes:\n"
            "  workflows:\n"
            "    - workflows/packaged.md\n"
            "permissions:\n"
            "  tools: []\n"
            "  network: false\n",
            encoding="utf-8",
        )

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir), manifest_roots=[str(tmp_path / "extensions")])

        diagnostics = mgr.get_diagnostics()
        assert diagnostics["error_count"] == 1
        assert diagnostics["load_errors"][0]["phase"] == "compatibility"
        assert diagnostics["shared_error_count"] == 0

    def test_manifest_backed_workflow_names_win_duplicate_name_collisions(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        (workflows_dir / "duplicate.md").write_text(
            "---\n"
            "name: shared-workflow\n"
            "description: Legacy workflow\n"
            "requires:\n"
            "  tools: [web_search]\n"
            "steps:\n"
            "  - id: search\n"
            "    tool: web_search\n"
            "    arguments: {}\n"
            "---\n\n"
            "Legacy workflow.\n",
            encoding="utf-8",
        )
        _write_manifest_workflow_package(
            tmp_path,
            workflow_name="shared-workflow",
            description="Manifest workflow",
            step_tool="write_file",
        )

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir), manifest_roots=[str(tmp_path / "extensions")])

        assert [workflow["name"] for workflow in mgr.list_workflows()] == ["shared-workflow"]
        selected = mgr.get_workflow("shared-workflow")
        assert selected is not None
        assert selected.source == "manifest"
        assert selected.description == "Manifest workflow"
        assert any(error["phase"] == "duplicate-workflow-name" for error in mgr.get_diagnostics()["load_errors"])

    def test_manifest_backed_workflow_tool_names_win_duplicate_tool_collisions(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        (workflows_dir / "duplicate.md").write_text(
            "---\n"
            "name: shared-workflow\n"
            "description: Legacy workflow\n"
            "requires:\n"
            "  tools: [web_search]\n"
            "steps:\n"
            "  - id: search\n"
            "    tool: web_search\n"
            "    arguments: {}\n"
            "---\n\n"
            "Legacy workflow.\n",
            encoding="utf-8",
        )
        _write_manifest_workflow_package(
            tmp_path,
            workflow_name="shared_workflow",
            description="Manifest workflow",
            step_tool="write_file",
        )

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir), manifest_roots=[str(tmp_path / "extensions")])

        assert [workflow["name"] for workflow in mgr.list_workflows()] == ["shared_workflow"]
        selected = mgr.get_workflow("shared_workflow")
        assert selected is not None
        assert selected.source == "manifest"
        assert selected.description == "Manifest workflow"
        assert any(error["phase"] == "duplicate-workflow-tool-name" for error in mgr.get_diagnostics()["load_errors"])

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
        assert audit_payload[1]["checkpoint_step_ids"] == ["search", "save"]
        assert audit_payload[1]["last_completed_step_id"] == "save"
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
        mcp_tool.inputs = {
            "headers": {"type": "object", "description": "Authentication headers"},
        }
        write = DummyTool("write_file", lambda file_path, content: f"saved {file_path}: {content}")
        workflow_tools = mgr.build_workflow_tools([mcp_tool, write], active_skill_names=[])
        tool = workflow_tools[0]

        result = tool(file_path="tasks.md")

        assert tool.name == "workflow_mcp_export"
        assert "saved tasks.md: task-a" in result
        assert mgr.get_tool_metadata(tool.name)["policy_modes"] == ["full"]
        assert mgr.get_tool_metadata(tool.name)["execution_boundaries"] == ["external_mcp", "workspace_write"]
        assert mgr.get_tool_metadata(tool.name)["accepts_secret_refs"] is True

    def test_workflow_metadata_uses_runtime_mcp_secret_ref_fields(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        (workflows_dir / "mcp_read_only.md").write_text(
            "---\n"
            "name: mcp-read-only\n"
            "description: Export MCP body-only result\n"
            "requires:\n"
            "  tools: [mcp_read_only]\n"
            "steps:\n"
            "  - id: fetch\n"
            "    tool: mcp_read_only\n"
            "    arguments: {}\n"
            "---\n"
        )

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir))
        mcp_tool = DummyTool("mcp_read_only", lambda: "ok")
        mcp_tool.inputs = {
            "body": {"type": "string", "description": "Request body"},
        }

        workflow_tools = mgr.build_workflow_tools([mcp_tool], active_skill_names=[])

        assert workflow_tools[0].get_approval_context({})["accepts_secret_refs"] is False
        assert mgr.get_tool_metadata(workflow_tools[0].name)["accepts_secret_refs"] is False

    def test_build_tools_tracks_last_completed_step_before_continued_error(self, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        (workflows_dir / "retryable.md").write_text(
            "---\n"
            "name: retryable-save\n"
            "description: Continue after a write failure\n"
            "requires:\n"
            "  tools: [web_search, write_file]\n"
            "inputs:\n"
            "  query:\n"
            "    type: string\n"
            "  file_path:\n"
            "    type: string\n"
            "steps:\n"
            "  - id: search\n"
            "    tool: web_search\n"
            "    arguments:\n"
            "      query: \"{{ query }}\"\n"
            "  - id: save\n"
            "    tool: write_file\n"
            "    continue_on_error: true\n"
            "    arguments:\n"
            "      file_path: \"{{ file_path }}\"\n"
            "      content: \"{{ steps.search.result }}\"\n"
            "---\n"
        )

        mgr = WorkflowManager()
        mgr.init(str(workflows_dir))
        search = DummyTool("web_search", lambda query: f"SEARCH<{query}>")

        def _write_file(**_kwargs):
            raise PermissionError("workspace write denied")

        write = DummyTool("write_file", _write_file)
        workflow_tool = mgr.build_workflow_tools([search, write], active_skill_names=[])[0]

        workflow_tool(query="seraph", file_path="notes/brief.md")
        audit_payload = workflow_tool.get_audit_result_payload({}, "ignored")

        assert audit_payload[1]["continued_error_steps"] == ["save"]
        assert audit_payload[1]["failed_step_ids"] == ["save"]
        assert audit_payload[1]["last_completed_step_id"] == "search"


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
    assert run["branch_kind"] == "approval_resume"
    assert run["parent_run_identity"] is None
    assert run["root_run_identity"] == "session-1:workflow_web_brief_to_file:web-brief"
    assert run["checkpoint_candidates"][0]["step_id"] == "approval_gate"
    assert run["checkpoint_candidates"][1]["step_id"] == "search"
    assert run["resume_plan"]["source_run_identity"] == run["run_identity"]
    assert run["resume_plan"]["parent_run_identity"] == run["run_identity"]
    assert run["resume_plan"]["branch_kind"] == "approval_resume"
    assert run["resume_plan"]["checkpoint_candidates"][0]["kind"] == "approval_gate"
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
    assert run["replay_block_reason"] == "approval_context_missing"
    assert run["thread_continue_message"] is None
    assert "predates trust-boundary tracking" in run["approval_recovery_message"]


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_hides_resume_metadata_when_pending_run_lacks_tracked_authenticated_context(client):
    current_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "accepts_secret_refs": False,
        "step_tools": ["web_search", "write_file"],
        "authenticated_source": True,
        "source_systems": [
            {
                "server_name": "github",
                "hostname": "api.github.com",
                "source": "extension",
                "authenticated_source": True,
            }
        ],
    }
    runtime_workflow_tool = SimpleNamespace(
        name="workflow_web_brief_to_file",
        get_approval_context=lambda _arguments: current_context,
    )

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
                    "details": {
                        "run_fingerprint": "web-brief-auth-pending",
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
                    "fingerprint": "web-brief-auth-pending",
                    "summary": "Approval pending for workflow_web_brief_to_file",
                    "risk_level": "medium",
                    "created_at": "2026-03-18T12:01:10Z",
                    "resume_message": "Continue the authenticated brief once approved",
                }
            ],
        ),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "medium",
                "execution_boundaries": ["external_read", "workspace_write"],
                "accepts_secret_refs": False,
                "approval_context": {
                    "workflow_name": "web-brief-to-file",
                    "risk_level": "medium",
                    "execution_boundaries": ["external_read", "workspace_write"],
                    "accepts_secret_refs": False,
                    "step_tools": ["web_search", "write_file"],
                },
            },
        ),
        patch("src.api.workflows.get_base_tools_and_active_skills", return_value=([], [], "full")),
        patch("src.api.workflows.workflow_manager.build_workflow_tools", return_value=[runtime_workflow_tool]),
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
    run = response.json()["runs"][0]
    assert run["replay_allowed"] is False
    assert run["replay_block_reason"] == "approval_context_missing"
    assert run["resume_from_step"] is None
    assert run["resume_checkpoint_label"] is None
    assert run["checkpoint_candidates"] == []
    assert run["resume_plan"] is None
    assert run["thread_continue_message"] is None
    assert "predates trust-boundary tracking" in run["approval_recovery_message"]
    assert run["trust_boundary"]["status"] == "missing"
    assert run["trust_boundary"]["blocked"] is True
    assert run["trust_boundary"]["reason"] == "approval_context_missing"
    assert run["trust_boundary"]["current"]["authenticated_source"] is True


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
async def test_workflow_runs_endpoint_does_not_suggest_tool_policy_for_unrelated_step_failures(client):
    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-result",
                    "session_id": "session-1",
                    "event_type": "tool_result",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "workflow_web_brief_to_file degraded",
                    "created_at": "2026-03-18T12:01:45Z",
                    "details": {
                        "workflow_name": "web-brief-to-file",
                        "run_fingerprint": "web-brief-error",
                        "step_tools": ["web_search"],
                        "step_records": [
                            {
                                "id": "search",
                                "index": 1,
                                "tool": "web_search",
                                "status": "continued_error",
                                "argument_keys": ["query"],
                                "artifact_paths": [],
                                "result_summary": "Error: upstream timeout",
                                "error_kind": "TimeoutError",
                                "error_summary": "upstream timeout while fetching results",
                            }
                        ],
                        "artifact_paths": [],
                        "continued_error_steps": ["search"],
                    },
                },
                {
                    "id": "evt-call",
                    "session_id": "session-1",
                    "event_type": "tool_call",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "Calling workflow",
                    "created_at": "2026-03-18T12:01:00Z",
                    "details": {"run_fingerprint": "web-brief-error", "arguments": {"query": "seraph"}},
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "medium",
                "execution_boundaries": ["external_read"],
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
                    "is_available": True,
                    "availability": "ready",
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
    run = response.json()["runs"][0]
    step = run["step_records"][0]
    assert step["status"] == "continued_error"
    assert not any(action["type"] == "set_tool_policy" for action in step["recovery_actions"])


@pytest.mark.asyncio
async def test_workflow_resume_plan_endpoint_returns_structured_branch_metadata(client):
    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-result",
                    "session_id": "session-1",
                    "event_type": "tool_result",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "workflow_web_brief_to_file degraded",
                    "created_at": "2026-03-18T12:01:45Z",
                    "details": {
                        "workflow_name": "web-brief-to-file",
                        "run_fingerprint": "web-brief-error",
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
                            },
                            {
                                "id": "save",
                                "index": 2,
                                "tool": "write_file",
                                "status": "continued_error",
                                "argument_keys": ["file_path", "content"],
                                "artifact_paths": ["notes/brief.md"],
                                "result_summary": "Error: denied",
                                "error_kind": "PermissionError",
                                "error_summary": "denied",
                            },
                        ],
                        "checkpoint_step_ids": ["search", "save"],
                        "last_completed_step_id": "save",
                        "artifact_paths": ["notes/brief.md"],
                        "continued_error_steps": ["save"],
                        "checkpoint_context_available": True,
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
                        "run_fingerprint": "web-brief-error",
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
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
        response = await client.post(
            "/api/workflows/runs/session-1:workflow_web_brief_to_file:web-brief-error/resume-plan",
            json={"step_id": "save"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_identity"] == "session-1:workflow_web_brief_to_file:web-brief-error"
    assert payload["workflow_name"] == "web-brief-to-file"
    assert payload["resume_plan"]["branch_kind"] == "retry_failed_step"
    assert payload["resume_plan"]["resume_from_step"] == "save"
    assert payload["resume_plan"]["resume_checkpoint_label"] == "save (write_file)"
    assert payload["resume_plan"]["parent_run_identity"] == payload["run_identity"]
    assert payload["resume_plan"]["root_run_identity"] == payload["run_identity"]
    assert payload["resume_plan"]["requires_manual_execution"] is True
    assert payload["resume_plan"]["checkpoint_candidates"][1]["step_id"] == "save"
    assert '_seraph_resume_from_step="save"' in payload["resume_plan"]["draft"]
    assert "_seraph_parent_run_identity=" in payload["resume_plan"]["draft"]


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_hides_later_retry_draft_without_checkpoint_state(client):
    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-result",
                    "session_id": "session-1",
                    "event_type": "tool_result",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "workflow_web_brief_to_file degraded",
                    "created_at": "2026-03-18T12:01:45Z",
                    "details": {
                        "workflow_name": "web-brief-to-file",
                        "run_fingerprint": "web-brief-no-checkpoint",
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
                            },
                            {
                                "id": "save",
                                "index": 2,
                                "tool": "write_file",
                                "status": "continued_error",
                                "argument_keys": ["file_path", "content"],
                                "artifact_paths": ["notes/brief.md"],
                                "result_summary": "Error: denied",
                                "error_kind": "PermissionError",
                                "error_summary": "denied",
                            },
                        ],
                        "artifact_paths": ["notes/brief.md"],
                        "continued_error_steps": ["save"],
                        "checkpoint_context_available": False,
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
                        "run_fingerprint": "web-brief-no-checkpoint",
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
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
    run = response.json()["runs"][0]
    assert run["checkpoint_context_available"] is False
    assert run["retry_from_step_draft"] is None
    assert run["checkpoint_candidates"][0]["step_id"] == "search"
    assert run["checkpoint_candidates"][0]["resume_supported"] is True
    assert run["checkpoint_candidates"][1]["step_id"] == "save"
    assert run["checkpoint_candidates"][1]["resume_supported"] is False
    assert run["checkpoint_candidates"][1]["resume_draft"] is None


@pytest.mark.asyncio
async def test_workflow_resume_plan_rejects_approval_gate_for_non_approval_run(client):
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
                        "run_fingerprint": "web-brief-ok",
                        "step_tools": ["web_search"],
                        "step_records": [{"id": "search", "tool": "web_search", "status": "succeeded"}],
                        "checkpoint_step_ids": ["search"],
                        "last_completed_step_id": "search",
                        "continued_error_steps": [],
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={"risk_level": "medium", "execution_boundaries": ["external_read"], "accepts_secret_refs": False},
        ),
        patch(
            "src.api.workflows.workflow_manager.list_workflows",
            return_value=[{"name": "web-brief-to-file", "inputs": {}, "enabled": True, "is_available": True, "missing_tools": [], "missing_skills": []}],
        ),
        patch("src.api.workflows.session_manager.list_sessions", return_value=[{"id": "session-1", "title": "Research thread"}]),
        patch("src.api.workflows.get_current_tool_policy_mode", return_value="balanced"),
    ):
        response = await client.post(
            "/api/workflows/runs/session-1:workflow_web_brief_to_file:web-brief-ok/resume-plan",
            json={"step_id": "approval_gate"},
        )

    assert response.status_code == 404
    assert "approval_gate" in response.json()["detail"]


@pytest.mark.asyncio
async def test_workflow_resume_plan_rejects_noninitial_checkpoint_without_reusable_state(client):
    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-result",
                    "session_id": "session-1",
                    "event_type": "tool_result",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "workflow_web_brief_to_file degraded",
                    "created_at": "2026-03-18T12:01:45Z",
                    "details": {
                        "workflow_name": "web-brief-to-file",
                        "run_fingerprint": "web-brief-no-checkpoint",
                        "step_tools": ["web_search", "write_file"],
                        "step_records": [
                            {"id": "search", "tool": "web_search", "status": "succeeded"},
                            {"id": "save", "tool": "write_file", "status": "continued_error"},
                        ],
                        "artifact_paths": ["notes/brief.md"],
                        "continued_error_steps": ["save"],
                        "checkpoint_context_available": False,
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
                        "run_fingerprint": "web-brief-no-checkpoint",
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
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
        response = await client.post(
            "/api/workflows/runs/session-1:workflow_web_brief_to_file:web-brief-no-checkpoint/resume-plan",
            json={"step_id": "save"},
        )

    assert response.status_code == 409
    assert "did not persist reusable checkpoint state" in response.json()["detail"]


@pytest.mark.asyncio
async def test_workflow_resume_plan_blocks_branching_past_pending_approval_gate(client):
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
                    "details": {
                        "run_fingerprint": "web-brief-pending",
                        "arguments": {"query": "seraph"},
                    },
                },
                {
                    "id": "evt-result",
                    "session_id": "session-1",
                    "event_type": "tool_result",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "workflow_web_brief_to_file succeeded (1 steps)",
                    "created_at": "2026-03-18T12:01:45Z",
                    "details": {
                        "workflow_name": "web-brief-to-file",
                        "run_fingerprint": "web-brief-pending",
                        "step_tools": ["web_search"],
                        "step_records": [{"id": "search", "tool": "web_search", "status": "succeeded"}],
                        "checkpoint_step_ids": ["search"],
                        "last_completed_step_id": "search",
                        "continued_error_steps": [],
                    },
                },
            ],
        ),
        patch(
            "src.api.workflows.approval_repository.list_pending",
            return_value=[{
                "id": "approval-1",
                "tool_name": "workflow_web_brief_to_file",
                "session_id": "session-1",
                "fingerprint": "web-brief-pending",
                "summary": "Approval pending",
                "risk_level": "medium",
                "created_at": "2026-03-18T12:02:00Z",
                "resume_message": "Continue after approval",
            }],
        ),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={"risk_level": "medium", "execution_boundaries": ["external_read"], "accepts_secret_refs": False},
        ),
        patch(
            "src.api.workflows.workflow_manager.list_workflows",
            return_value=[{"name": "web-brief-to-file", "inputs": {}, "enabled": True, "is_available": True, "missing_tools": [], "missing_skills": []}],
        ),
        patch("src.api.workflows.session_manager.list_sessions", return_value=[{"id": "session-1", "title": "Research thread"}]),
        patch("src.api.workflows.get_current_tool_policy_mode", return_value="balanced"),
    ):
        response = await client.post(
            "/api/workflows/runs/session-1:workflow_web_brief_to_file:web-brief-pending/resume-plan",
            json={"step_id": "search"},
        )

    assert response.status_code == 409
    assert "approval gate" in response.json()["detail"]


@pytest.mark.asyncio
async def test_workflow_resume_plan_falls_back_to_scoped_run_lookup(client):
    run_identity = "session-1:workflow_web_brief_to_file:older-run"
    fallback_run = {
        "run_identity": run_identity,
        "workflow_name": "web-brief-to-file",
        "pending_approvals": [],
        "continued_error_steps": ["save"],
        "step_records": [
            {"id": "search", "tool": "web_search", "status": "succeeded"},
            {"id": "save", "tool": "write_file", "status": "continued_error"},
        ],
        "thread_continue_message": None,
        "approval_recovery_message": None,
        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
        "session_id": "session-1",
        "workflow_name": "web-brief-to-file",
        "checkpoint_context_available": True,
    }

    with (
        patch("src.api.workflows._list_workflow_runs", AsyncMock(side_effect=[[], [fallback_run]])) as list_runs,
        patch(
            "src.api.workflows._load_workflow_events_for_identity",
            AsyncMock(return_value=([{"id": "evt"}], "session-1")),
        ) as load_events,
    ):
        response = await client.post(
            f"/api/workflows/runs/{run_identity}/resume-plan",
            json={"step_id": "save"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_identity"] == run_identity
    assert payload["resume_plan"]["resume_from_step"] == "save"
    assert list_runs.await_count == 2
    load_events.assert_awaited_once_with(run_identity)


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_disambiguates_duplicate_fingerprinted_runs_by_call_event_id(client):
    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-call-older",
                    "session_id": "session-1",
                    "event_type": "tool_call",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "Calling workflow",
                    "created_at": "2026-03-18T12:01:00Z",
                    "details": {
                        "run_fingerprint": "shared-fingerprint",
                        "arguments": {"query": "older", "file_path": "notes/older.md"},
                    },
                },
                {
                    "id": "evt-result-older",
                    "session_id": "session-1",
                    "event_type": "tool_result",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "workflow_web_brief_to_file succeeded",
                    "created_at": "2026-03-18T12:01:10Z",
                    "details": {
                        "workflow_name": "web-brief-to-file",
                        "run_fingerprint": "shared-fingerprint",
                        "call_event_id": "evt-call-older",
                        "step_tools": ["web_search", "write_file"],
                        "step_records": [],
                        "artifact_paths": ["notes/older.md"],
                        "continued_error_steps": [],
                    },
                },
                {
                    "id": "evt-call-newer",
                    "session_id": "session-1",
                    "event_type": "tool_call",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "Calling workflow",
                    "created_at": "2026-03-18T12:02:00Z",
                    "details": {
                        "run_fingerprint": "shared-fingerprint",
                        "arguments": {"query": "newer", "file_path": "notes/newer.md"},
                    },
                },
                {
                    "id": "evt-result-newer",
                    "session_id": "session-1",
                    "event_type": "tool_result",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "workflow_web_brief_to_file succeeded",
                    "created_at": "2026-03-18T12:02:10Z",
                    "details": {
                        "workflow_name": "web-brief-to-file",
                        "run_fingerprint": "shared-fingerprint",
                        "call_event_id": "evt-call-newer",
                        "step_tools": ["web_search", "write_file"],
                        "step_records": [],
                        "artifact_paths": ["notes/newer.md"],
                        "continued_error_steps": [],
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
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
    runs = response.json()["runs"]
    assert len(runs) == 2
    assert runs[0]["run_identity"] == "session-1:workflow_web_brief_to_file:shared-fingerprint:evt-call-newer"
    assert runs[0]["replay_inputs"] == {"query": "newer", "file_path": "notes/newer.md"}
    assert runs[1]["run_identity"] == "session-1:workflow_web_brief_to_file:shared-fingerprint:evt-call-older"
    assert runs[1]["replay_inputs"] == {"query": "older", "file_path": "notes/older.md"}


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_uses_approval_context_in_pending_fingerprint_projection(client):
    arguments = {"query": "seraph", "file_path": "notes/brief.md"}
    approval_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "accepts_secret_refs": False,
        "step_tools": ["web_search", "write_file"],
    }
    expected_fingerprint = fingerprint_tool_call(
        "workflow_web_brief_to_file",
        arguments,
        approval_context=approval_context,
    )

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
                    "details": {
                        "arguments": arguments,
                        "approval_context": approval_context,
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
                    "fingerprint": expected_fingerprint,
                    "summary": "Approval pending",
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
                "approval_context": approval_context,
            },
        ),
        patch(
            "src.api.workflows.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "web-brief-to-file",
                    "inputs": {"query": {"type": "string", "description": "Search query"}},
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
    run = response.json()["runs"][0]
    assert run["run_fingerprint"] == expected_fingerprint
    assert run["pending_approval_count"] == 1
    assert run["pending_approval_ids"] == ["approval-1"]


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_blocks_replay_when_approval_context_changes(client):
    recorded_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "accepts_secret_refs": False,
        "step_tools": ["web_search", "write_file"],
    }
    current_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "high",
        "execution_boundaries": ["secret_injection", "workspace_write"],
        "accepts_secret_refs": True,
        "step_tools": ["get_secret_ref", "write_file"],
    }

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
                        "run_fingerprint": "web-brief-legacy",
                        "approval_context": recorded_context,
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
                        "run_fingerprint": "web-brief-legacy",
                        "approval_context": recorded_context,
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "high",
                "execution_boundaries": ["secret_injection", "workspace_write"],
                "accepts_secret_refs": True,
                "approval_context": current_context,
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
    run = response.json()["runs"][0]
    assert run["approval_context_mismatch"] is True
    assert run["replay_allowed"] is False
    assert run["replay_block_reason"] == "approval_context_changed"
    assert run["risk_level"] == "medium"
    assert run["execution_boundaries"] == ["external_read", "workspace_write"]
    assert run["resume_from_step"] is None
    assert run["resume_checkpoint_label"] is None
    assert run["checkpoint_candidates"] == []
    assert run["resume_plan"] is None
    assert "trust boundary" in run["approval_recovery_message"]
    assert run["trust_boundary"]["status"] == "changed"
    assert run["trust_boundary"]["blocked"] is True
    assert run["trust_boundary"]["reason"] == "approval_context_changed"
    assert "risk_level" in run["trust_boundary"]["changed_fields"]
    assert "execution_boundaries" in run["trust_boundary"]["changed_fields"]
    assert run["trust_boundary"]["recorded"]["risk_level"] == "medium"
    assert run["trust_boundary"]["current"]["risk_level"] == "high"


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_hides_repair_actions_when_boundary_drift_blocks_replay(client):
    recorded_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "accepts_secret_refs": False,
        "step_tools": ["web_search", "write_file"],
    }
    current_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "high",
        "execution_boundaries": ["secret_injection", "workspace_write"],
        "accepts_secret_refs": True,
        "step_tools": ["get_secret_ref", "write_file"],
    }

    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-failed",
                    "session_id": "session-1",
                    "event_type": "tool_failed",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "workflow_web_brief_to_file failed on save",
                    "created_at": "2026-03-18T12:01:45Z",
                    "details": {
                        "workflow_name": "web-brief-to-file",
                        "run_fingerprint": "web-brief-repair-blocked",
                        "approval_context": recorded_context,
                        "step_tools": ["web_search", "write_file"],
                        "step_records": [
                            {"id": "search", "tool": "web_search", "status": "succeeded"},
                            {
                                "id": "save",
                                "tool": "write_file",
                                "status": "failed",
                                "error_summary": "write_file blocked by policy",
                            },
                        ],
                        "continued_error_steps": ["save"],
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
                        "run_fingerprint": "web-brief-repair-blocked",
                        "approval_context": recorded_context,
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "high",
                "execution_boundaries": ["secret_injection", "workspace_write"],
                "accepts_secret_refs": True,
                "approval_context": current_context,
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
    assert run["replay_block_reason"] == "approval_context_changed"
    assert run["replay_recommended_actions"] == []
    assert run["step_records"][1]["recovery_actions"] == []
    assert run["step_records"][1]["recovery_hint"] is None
    assert run["step_records"][1]["is_recoverable"] is False


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_ignores_approval_context_list_reordering(client):
    recorded_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "medium",
        "execution_boundaries": ["workspace_write", "external_read"],
        "accepts_secret_refs": False,
        "step_tools": ["write_file", "web_search"],
    }
    current_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "accepts_secret_refs": False,
        "step_tools": ["web_search", "write_file"],
    }

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
                        "run_fingerprint": "web-brief-stable",
                        "approval_context": recorded_context,
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
                        "run_fingerprint": "web-brief-stable",
                        "approval_context": recorded_context,
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "medium",
                "execution_boundaries": ["external_read", "workspace_write"],
                "accepts_secret_refs": False,
                "approval_context": current_context,
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
    run = response.json()["runs"][0]
    assert run["approval_context_mismatch"] is False
    assert run["replay_allowed"] is True
    assert run["replay_block_reason"] is None


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_detects_authenticated_source_context_drift(client):
    recorded_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "accepts_secret_refs": False,
        "step_tools": ["web_search", "write_file"],
        "authenticated_source": False,
        "source_systems": [],
    }
    current_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "accepts_secret_refs": False,
        "step_tools": ["web_search", "write_file"],
        "authenticated_source": True,
        "source_systems": [
            {
                "server_name": "github",
                "hostname": "api.github.com",
                "source": "extension",
                "authenticated_source": True,
                "credential_sources": ["vault:github_token"],
            }
        ],
    }
    runtime_workflow_tool = SimpleNamespace(
        name="workflow_web_brief_to_file",
        get_approval_context=lambda _arguments: current_context,
    )

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
                        "run_fingerprint": "web-brief-auth",
                        "approval_context": recorded_context,
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
                        "run_fingerprint": "web-brief-auth",
                        "approval_context": recorded_context,
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "medium",
                "execution_boundaries": ["external_read", "workspace_write"],
                "accepts_secret_refs": False,
                "approval_context": {
                    "workflow_name": "web-brief-to-file",
                    "risk_level": "medium",
                    "execution_boundaries": ["external_read", "workspace_write"],
                    "accepts_secret_refs": False,
                    "step_tools": ["web_search", "write_file"],
                    "authenticated_source": False,
                },
            },
        ),
        patch("src.api.workflows.get_base_tools_and_active_skills", return_value=([], [], "full")),
        patch("src.api.workflows.workflow_manager.build_workflow_tools", return_value=[runtime_workflow_tool]),
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
    run = response.json()["runs"][0]
    assert run["approval_context_mismatch"] is True
    assert run["replay_allowed"] is False
    assert run["replay_block_reason"] == "approval_context_changed"
    assert run["checkpoint_candidates"] == []
    assert run["resume_plan"] is None
    assert run["current_approval_context"]["authenticated_source"] is True
    assert run["current_approval_context"]["source_systems"] == [
        {
            "server_name": "github",
            "hostname": "api.github.com",
            "source": "extension",
            "authenticated_source": True,
            "credential_sources": ["vault:github_token"],
        }
    ]


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_ignores_authenticated_source_system_reordering(client):
    recorded_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "accepts_secret_refs": False,
        "step_tools": ["web_search", "write_file"],
        "authenticated_source": True,
        "source_systems": [
            {
                "server_name": "github",
                "hostname": "api.github.com",
                "source": "extension",
                "authenticated_source": True,
                "credential_sources": ["vault:github_token", "env:GITHUB_TOKEN"],
            },
            {
                "server_name": "jira",
                "hostname": "acme.atlassian.net",
                "source": "manual",
                "authenticated_source": True,
                "credential_sources": ["vault:jira_token"],
            },
        ],
    }
    current_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "medium",
        "execution_boundaries": ["workspace_write", "external_read"],
        "accepts_secret_refs": False,
        "step_tools": ["write_file", "web_search"],
        "authenticated_source": True,
        "source_systems": [
            {
                "server_name": "jira",
                "hostname": "acme.atlassian.net",
                "source": "manual",
                "authenticated_source": True,
                "credential_sources": ["vault:jira_token"],
            },
            {
                "server_name": "github",
                "hostname": "api.github.com",
                "source": "extension",
                "authenticated_source": True,
                "credential_sources": ["env:GITHUB_TOKEN", "vault:github_token"],
            },
        ],
    }
    runtime_workflow_tool = SimpleNamespace(
        name="workflow_web_brief_to_file",
        get_approval_context=lambda _arguments: current_context,
    )

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
                        "run_fingerprint": "web-brief-auth-stable",
                        "approval_context": recorded_context,
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
                        "run_fingerprint": "web-brief-auth-stable",
                        "approval_context": recorded_context,
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "medium",
                "execution_boundaries": ["external_read", "workspace_write"],
                "accepts_secret_refs": False,
                "approval_context": recorded_context,
            },
        ),
        patch("src.api.workflows.get_base_tools_and_active_skills", return_value=([], [], "full")),
        patch("src.api.workflows.workflow_manager.build_workflow_tools", return_value=[runtime_workflow_tool]),
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
    run = response.json()["runs"][0]
    assert run["approval_context_mismatch"] is False
    assert run["replay_allowed"] is True
    assert run["replay_block_reason"] is None
    assert run["current_approval_context"]["source_systems"] == [
        {
            "server_name": "github",
            "hostname": "api.github.com",
            "source": "extension",
            "authenticated_source": True,
            "credential_sources": ["env:GITHUB_TOKEN", "vault:github_token"],
        },
        {
            "server_name": "jira",
            "hostname": "acme.atlassian.net",
            "source": "manual",
            "authenticated_source": True,
            "credential_sources": ["vault:jira_token"],
        },
    ]


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_detects_delegated_specialist_context_drift(client):
    recorded_context = {
        "workflow_name": "delegated-brief",
        "risk_level": "high",
        "execution_boundaries": ["delegation", "external_mcp"],
        "accepts_secret_refs": True,
        "step_tools": ["delegate_task", "write_file"],
        "delegated_specialists": ["mcp_jira"],
    }
    current_context = {
        "workflow_name": "delegated-brief",
        "risk_level": "high",
        "execution_boundaries": ["delegation", "external_mcp"],
        "accepts_secret_refs": True,
        "step_tools": ["write_file", "delegate_task"],
        "delegated_specialists": ["mcp_github"],
    }
    runtime_workflow_tool = SimpleNamespace(
        name="workflow_delegated_brief",
        get_approval_context=lambda _arguments: current_context,
    )

    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-result",
                    "session_id": "session-1",
                    "event_type": "tool_result",
                    "tool_name": "workflow_delegated_brief",
                    "summary": "workflow_delegated_brief succeeded (2 steps)",
                    "created_at": "2026-03-18T12:01:45Z",
                    "details": {
                        "workflow_name": "delegated-brief",
                        "run_fingerprint": "delegated-brief-drift",
                        "approval_context": recorded_context,
                        "step_tools": ["delegate_task", "write_file"],
                        "step_records": [],
                        "artifact_paths": ["notes/brief.md"],
                        "continued_error_steps": [],
                    },
                },
                {
                    "id": "evt-call",
                    "session_id": "session-1",
                    "event_type": "tool_call",
                    "tool_name": "workflow_delegated_brief",
                    "summary": "Calling workflow",
                    "created_at": "2026-03-18T12:01:00Z",
                    "details": {
                        "run_fingerprint": "delegated-brief-drift",
                        "approval_context": recorded_context,
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "high",
                "execution_boundaries": ["delegation", "external_mcp"],
                "accepts_secret_refs": True,
                "approval_context": recorded_context,
            },
        ),
        patch("src.api.workflows.get_base_tools_and_active_skills", return_value=([], [], "full")),
        patch("src.api.workflows.workflow_manager.build_workflow_tools", return_value=[runtime_workflow_tool]),
        patch(
            "src.api.workflows.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "delegated-brief",
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
    run = response.json()["runs"][0]
    assert run["approval_context_mismatch"] is True
    assert run["replay_allowed"] is False
    assert run["replay_block_reason"] == "approval_context_changed"
    assert run["checkpoint_candidates"] == []
    assert run["resume_plan"] is None
    assert run["current_approval_context"]["delegated_specialists"] == ["mcp_github"]


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_detects_delegated_authenticated_credential_source_drift(client):
    recorded_context = {
        "workflow_name": "delegated-brief",
        "risk_level": "high",
        "execution_boundaries": ["delegation", "external_mcp", "authenticated_external_source"],
        "accepts_secret_refs": True,
        "step_tools": ["delegate_task", "write_file"],
        "delegated_specialists": ["mcp_github"],
        "authenticated_source": True,
        "source_systems": [
            {
                "server_name": "github",
                "hostname": "api.github.com",
                "source": "extension",
                "authenticated_source": True,
                "credential_sources": ["vault:github_token"],
            }
        ],
    }
    current_context = {
        "workflow_name": "delegated-brief",
        "risk_level": "high",
        "execution_boundaries": ["authenticated_external_source", "external_mcp", "delegation"],
        "accepts_secret_refs": True,
        "step_tools": ["write_file", "delegate_task"],
        "delegated_specialists": ["mcp_github"],
        "authenticated_source": True,
        "source_systems": [
            {
                "server_name": "github",
                "hostname": "api.github.com",
                "source": "extension",
                "authenticated_source": True,
                "credential_sources": ["env:GITHUB_TOKEN"],
            }
        ],
    }
    runtime_workflow_tool = SimpleNamespace(
        name="workflow_delegated_brief",
        get_approval_context=lambda _arguments: current_context,
    )

    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-result",
                    "session_id": "session-1",
                    "event_type": "tool_result",
                    "tool_name": "workflow_delegated_brief",
                    "summary": "workflow_delegated_brief succeeded (2 steps)",
                    "created_at": "2026-03-18T12:01:45Z",
                    "details": {
                        "workflow_name": "delegated-brief",
                        "run_fingerprint": "delegated-brief-auth-cred",
                        "approval_context": recorded_context,
                        "step_tools": ["delegate_task", "write_file"],
                        "step_records": [],
                        "artifact_paths": ["notes/brief.md"],
                        "continued_error_steps": [],
                    },
                },
                {
                    "id": "evt-call",
                    "session_id": "session-1",
                    "event_type": "tool_call",
                    "tool_name": "workflow_delegated_brief",
                    "summary": "Calling workflow",
                    "created_at": "2026-03-18T12:01:00Z",
                    "details": {
                        "run_fingerprint": "delegated-brief-auth-cred",
                        "approval_context": recorded_context,
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "high",
                "execution_boundaries": ["delegation", "external_mcp", "authenticated_external_source"],
                "accepts_secret_refs": True,
                "approval_context": recorded_context,
            },
        ),
        patch("src.api.workflows.get_base_tools_and_active_skills", return_value=([], [], "full")),
        patch("src.api.workflows.workflow_manager.build_workflow_tools", return_value=[runtime_workflow_tool]),
        patch(
            "src.api.workflows.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "delegated-brief",
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
    run = response.json()["runs"][0]
    assert run["approval_context_mismatch"] is True
    assert run["replay_allowed"] is False
    assert run["replay_block_reason"] == "approval_context_changed"
    assert run["checkpoint_candidates"] == []
    assert run["resume_plan"] is None
    assert run["current_approval_context"]["source_systems"] == [
        {
            "server_name": "github",
            "hostname": "api.github.com",
            "source": "extension",
            "authenticated_source": True,
            "credential_sources": ["env:GITHUB_TOKEN"],
        }
    ]


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_detects_delegated_tool_inventory_drift(client):
    recorded_context = {
        "workflow_name": "delegated-brief",
        "risk_level": "high",
        "execution_boundaries": ["delegation", "external_mcp"],
        "accepts_secret_refs": True,
        "step_tools": ["delegate_task", "write_file"],
        "delegated_specialists": ["mcp_github"],
        "delegated_tool_names": ["mcp_github_issues"],
    }
    current_context = {
        "workflow_name": "delegated-brief",
        "risk_level": "high",
        "execution_boundaries": ["external_mcp", "delegation"],
        "accepts_secret_refs": True,
        "step_tools": ["write_file", "delegate_task"],
        "delegated_specialists": ["mcp_github"],
        "delegated_tool_names": ["mcp_github_issues", "mcp_github_repo"],
    }
    runtime_workflow_tool = SimpleNamespace(
        name="workflow_delegated_brief",
        get_approval_context=lambda _arguments: current_context,
    )

    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-result",
                    "session_id": "session-1",
                    "event_type": "tool_result",
                    "tool_name": "workflow_delegated_brief",
                    "summary": "workflow_delegated_brief succeeded (2 steps)",
                    "created_at": "2026-03-18T12:01:45Z",
                    "details": {
                        "workflow_name": "delegated-brief",
                        "run_fingerprint": "delegated-brief-tool-inventory",
                        "approval_context": recorded_context,
                        "step_tools": ["delegate_task", "write_file"],
                        "step_records": [],
                        "artifact_paths": ["notes/brief.md"],
                        "continued_error_steps": [],
                    },
                },
                {
                    "id": "evt-call",
                    "session_id": "session-1",
                    "event_type": "tool_call",
                    "tool_name": "workflow_delegated_brief",
                    "summary": "Calling workflow",
                    "created_at": "2026-03-18T12:01:00Z",
                    "details": {
                        "run_fingerprint": "delegated-brief-tool-inventory",
                        "approval_context": recorded_context,
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "high",
                "execution_boundaries": ["delegation", "external_mcp"],
                "accepts_secret_refs": True,
                "approval_context": recorded_context,
            },
        ),
        patch("src.api.workflows.get_base_tools_and_active_skills", return_value=([], [], "full")),
        patch("src.api.workflows.workflow_manager.build_workflow_tools", return_value=[runtime_workflow_tool]),
        patch(
            "src.api.workflows.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "delegated-brief",
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
    run = response.json()["runs"][0]
    assert run["approval_context_mismatch"] is True
    assert run["replay_allowed"] is False
    assert run["replay_block_reason"] == "approval_context_changed"
    assert run["checkpoint_candidates"] == []
    assert run["resume_plan"] is None
    assert run["current_approval_context"]["delegated_tool_names"] == [
        "mcp_github_issues",
        "mcp_github_repo",
    ]


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_ignores_delegated_specialist_reordering(client):
    recorded_context = {
        "workflow_name": "delegated-brief",
        "risk_level": "high",
        "execution_boundaries": ["delegation", "external_mcp"],
        "accepts_secret_refs": True,
        "step_tools": ["delegate_task", "write_file"],
        "delegated_specialists": ["mcp_jira", "mcp_github"],
    }
    current_context = {
        "workflow_name": "delegated-brief",
        "risk_level": "high",
        "execution_boundaries": ["external_mcp", "delegation"],
        "accepts_secret_refs": True,
        "step_tools": ["write_file", "delegate_task"],
        "delegated_specialists": ["mcp_github", "mcp_jira"],
    }
    runtime_workflow_tool = SimpleNamespace(
        name="workflow_delegated_brief",
        get_approval_context=lambda _arguments: current_context,
    )

    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-result",
                    "session_id": "session-1",
                    "event_type": "tool_result",
                    "tool_name": "workflow_delegated_brief",
                    "summary": "workflow_delegated_brief succeeded (2 steps)",
                    "created_at": "2026-03-18T12:01:45Z",
                    "details": {
                        "workflow_name": "delegated-brief",
                        "run_fingerprint": "delegated-brief-stable",
                        "approval_context": recorded_context,
                        "step_tools": ["delegate_task", "write_file"],
                        "step_records": [],
                        "artifact_paths": ["notes/brief.md"],
                        "continued_error_steps": [],
                    },
                },
                {
                    "id": "evt-call",
                    "session_id": "session-1",
                    "event_type": "tool_call",
                    "tool_name": "workflow_delegated_brief",
                    "summary": "Calling workflow",
                    "created_at": "2026-03-18T12:01:00Z",
                    "details": {
                        "run_fingerprint": "delegated-brief-stable",
                        "approval_context": recorded_context,
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "high",
                "execution_boundaries": ["delegation", "external_mcp"],
                "accepts_secret_refs": True,
                "approval_context": recorded_context,
            },
        ),
        patch("src.api.workflows.get_base_tools_and_active_skills", return_value=([], [], "full")),
        patch("src.api.workflows.workflow_manager.build_workflow_tools", return_value=[runtime_workflow_tool]),
        patch(
            "src.api.workflows.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "delegated-brief",
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
    run = response.json()["runs"][0]
    assert run["approval_context_mismatch"] is False
    assert run["replay_allowed"] is False
    assert run["replay_block_reason"] == "secret_ref_surface"
    assert run["current_approval_context"]["delegated_specialists"] == [
        "mcp_github",
        "mcp_jira",
    ]


@pytest.mark.asyncio
async def test_workflow_resume_plan_rejects_when_approval_context_changes(client):
    recorded_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "accepts_secret_refs": False,
        "step_tools": ["web_search", "write_file"],
    }
    current_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "high",
        "execution_boundaries": ["secret_injection", "workspace_write"],
        "accepts_secret_refs": True,
        "step_tools": ["get_secret_ref", "write_file"],
    }

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
                        "run_fingerprint": "web-brief-legacy",
                        "approval_context": recorded_context,
                        "step_tools": ["web_search", "write_file"],
                        "step_records": [
                            {"id": "search", "tool": "web_search", "status": "succeeded"},
                            {"id": "save", "tool": "write_file", "status": "succeeded"},
                        ],
                        "checkpoint_step_ids": ["search", "save"],
                        "last_completed_step_id": "save",
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
                        "run_fingerprint": "web-brief-legacy",
                        "approval_context": recorded_context,
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "high",
                "execution_boundaries": ["secret_injection", "workspace_write"],
                "accepts_secret_refs": True,
                "approval_context": current_context,
            },
        ),
        patch(
            "src.api.workflows.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "web-brief-to-file",
                    "inputs": {},
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
        response = await client.post(
            "/api/workflows/runs/session-1:workflow_web_brief_to_file:web-brief-legacy/resume-plan",
            json={"step_id": "save"},
        )

    assert response.status_code == 409
    assert "trust boundary" in response.json()["detail"]


@pytest.mark.asyncio
async def test_workflow_runs_endpoint_blocks_replay_when_approval_context_is_missing_for_authenticated_surface(client):
    current_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "accepts_secret_refs": False,
        "step_tools": ["web_search", "write_file"],
        "authenticated_source": True,
        "source_systems": [
            {
                "server_name": "github",
                "hostname": "api.github.com",
                "source": "extension",
                "authenticated_source": True,
                "credential_sources": ["vault:github_token"],
            }
        ],
    }
    runtime_workflow_tool = SimpleNamespace(
        name="workflow_web_brief_to_file",
        get_approval_context=lambda _arguments: current_context,
    )

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
                        "run_fingerprint": "web-brief-legacy-auth",
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
                        "run_fingerprint": "web-brief-legacy-auth",
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "medium",
                "execution_boundaries": ["external_read", "workspace_write"],
                "accepts_secret_refs": False,
                "approval_context": {
                    "workflow_name": "web-brief-to-file",
                    "risk_level": "medium",
                    "execution_boundaries": ["external_read", "workspace_write"],
                    "accepts_secret_refs": False,
                    "step_tools": ["web_search", "write_file"],
                },
            },
        ),
        patch("src.api.workflows.get_base_tools_and_active_skills", return_value=([], [], "full")),
        patch("src.api.workflows.workflow_manager.build_workflow_tools", return_value=[runtime_workflow_tool]),
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
    run = response.json()["runs"][0]
    assert run["approval_context_mismatch"] is False
    assert run["replay_allowed"] is False
    assert run["replay_block_reason"] == "approval_context_missing"
    assert run["resume_from_step"] is None
    assert run["resume_checkpoint_label"] is None
    assert run["checkpoint_candidates"] == []
    assert run["resume_plan"] is None
    assert "predates trust-boundary tracking" in run["approval_recovery_message"]


@pytest.mark.asyncio
async def test_workflow_resume_plan_rejects_when_approval_context_is_missing_for_authenticated_surface(client):
    current_context = {
        "workflow_name": "web-brief-to-file",
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "accepts_secret_refs": False,
        "step_tools": ["web_search", "write_file"],
        "authenticated_source": True,
        "source_systems": [
            {
                "server_name": "github",
                "hostname": "api.github.com",
                "source": "extension",
                "authenticated_source": True,
            }
        ],
    }

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
                        "run_fingerprint": "web-brief-legacy-auth",
                        "step_tools": ["web_search", "write_file"],
                        "step_records": [
                            {"id": "search", "tool": "web_search", "status": "succeeded"},
                            {"id": "save", "tool": "write_file", "status": "succeeded"},
                        ],
                        "checkpoint_step_ids": ["search", "save"],
                        "last_completed_step_id": "save",
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
                        "run_fingerprint": "web-brief-legacy-auth",
                        "arguments": {"query": "seraph", "file_path": "notes/brief.md"},
                    },
                },
            ],
        ),
        patch("src.api.workflows.approval_repository.list_pending", return_value=[]),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "medium",
                "execution_boundaries": ["external_read", "workspace_write"],
                "accepts_secret_refs": False,
                "approval_context": current_context,
            },
        ),
        patch("src.api.workflows.get_base_tools_and_active_skills", return_value=([], [], "full")),
        patch("src.api.workflows.workflow_manager.build_workflow_tools", return_value=[SimpleNamespace(name="workflow_web_brief_to_file", get_approval_context=lambda _arguments: current_context)]),
        patch(
            "src.api.workflows.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "web-brief-to-file",
                    "inputs": {},
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
        response = await client.post(
            "/api/workflows/runs/session-1:workflow_web_brief_to_file:web-brief-legacy-auth/resume-plan",
            json={"step_id": "save"},
        )

    assert response.status_code == 409
    assert "predates trust-boundary tracking" in response.json()["detail"]


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
    assert web_brief["source"] == "legacy"
    assert web_brief["extension_id"].startswith("legacy.workflows.")


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


@pytest.fixture
def _setup_manifest_workflow_manager(tmp_path):
    from src.workflows.manager import workflow_manager

    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    (workflows_dir / "legacy.md").write_text(
        "---\n"
        "name: legacy-workflow\n"
        "description: Legacy workflow\n"
        "requires:\n"
        "  tools: [web_search]\n"
        "steps:\n"
        "  - id: search\n"
        "    tool: web_search\n"
        "    arguments: {}\n"
        "---\n\n"
        "Legacy workflow.\n",
        encoding="utf-8",
    )
    _write_manifest_workflow_package(
        tmp_path,
        workflow_name="packaged-workflow",
        description="Packaged workflow",
        step_tool="write_file",
    )

    workflow_manager.init(str(workflows_dir), manifest_roots=[str(tmp_path / "extensions")])
    yield
    workflow_manager._workflows = []
    workflow_manager._load_errors = []
    workflow_manager._shared_manifest_errors = []
    workflow_manager._disabled = set()
    workflow_manager._workflows_dir = ""
    workflow_manager._manifest_roots = []
    workflow_manager._config_path = ""
    workflow_manager._registry = None


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
    async def test_list_workflows_includes_manifest_backed_entries(self, client, _setup_manifest_workflow_manager):
        with patch(
            "src.api.workflows.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="web_search"), SimpleNamespace(name="write_file")], [], "disabled"),
        ):
            resp = await client.get("/api/workflows")

        assert resp.status_code == 200
        workflows = {item["name"]: item for item in resp.json()["workflows"]}
        assert set(workflows) == {"legacy-workflow", "packaged-workflow"}
        assert workflows["legacy-workflow"]["source"] == "legacy"
        assert workflows["legacy-workflow"]["extension_id"].startswith("legacy.workflows.")
        assert workflows["packaged-workflow"]["source"] == "manifest"
        assert workflows["packaged-workflow"]["extension_id"] == "seraph.research-pack"

    @pytest.mark.asyncio
    async def test_enable_disable_manifest_backed_workflow(self, client, _setup_manifest_workflow_manager):
        resp = await client.put(
            "/api/workflows/packaged-workflow",
            json={"enabled": False},
        )
        assert resp.status_code == 200

        with patch(
            "src.api.workflows.get_base_tools_and_active_skills",
            return_value=([SimpleNamespace(name="web_search"), SimpleNamespace(name="write_file")], [], "disabled"),
        ):
            resp = await client.get("/api/workflows")
        workflows = {item["name"]: item for item in resp.json()["workflows"]}
        assert workflows["packaged-workflow"]["enabled"] is False

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

    @pytest.mark.asyncio
    async def test_save_workflow_draft_rejects_path_traversal(self, client):
        content = (
            "---\n"
            "name: Retryable Save\n"
            "description: Save a note\n"
            "requires:\n"
            "  tools: [write_file]\n"
            "steps:\n"
            "  - id: save\n"
            "    tool: write_file\n"
            "    arguments:\n"
            "      file_path: notes/brief.md\n"
            "      content: hello\n"
            "---\n"
        )
        with (
            patch("src.api.workflows.workflow_manager._workflows_dir", "/tmp/workflows"),
            patch("src.extensions.workspace_package.settings.workspace_dir", "/tmp"),
            patch(
                "src.api.workflows.get_base_tools_and_active_skills",
                return_value=([SimpleNamespace(name="write_file")], [], "disabled"),
            ),
        ):
            resp = await client.post(
                "/api/workflows/save",
                json={"content": content, "file_name": "../outside.md"},
            )

        assert resp.status_code == 400
        assert "managed workspace package" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_save_workflow_draft_persists_to_managed_workspace_package(self, client, tmp_path):
        content = (
            "---\n"
            "name: Retryable Save\n"
            "description: Save a note\n"
            "requires:\n"
            "  tools: [write_file]\n"
            "steps:\n"
            "  - id: save\n"
            "    tool: write_file\n"
            "    arguments:\n"
            "      file_path: notes/brief.md\n"
            "      content: hello\n"
            "---\n"
        )
        with (
            patch("src.api.workflows.workflow_manager._workflows_dir", str(tmp_path / "workflows")),
            patch("src.api.workflows.workflow_manager._manifest_roots", []),
            patch("src.extensions.workspace_package.settings.workspace_dir", str(tmp_path)),
            patch(
                "src.api.workflows.get_base_tools_and_active_skills",
                return_value=([SimpleNamespace(name="write_file")], [], "disabled"),
            ),
            patch("src.api.workflows.workflow_manager.init") as init_manager,
            patch("src.api.workflows.workflow_manager.reload", return_value=[]),
            patch("src.api.workflows.log_integration_event", AsyncMock()),
        ):
            resp = await client.post("/api/workflows/save", json={"content": content})

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "saved"
        init_manager.assert_called_once_with(
            str(tmp_path / "workflows"),
            manifest_roots=default_manifest_roots_for_workspace(str(tmp_path)),
        )
        assert payload["file_path"].endswith(
            "extensions/workspace-capabilities/workflows/retryable-save.md"
        )

    @pytest.mark.asyncio
    async def test_validate_workflow_draft_applies_runtime_default_output_surface(self, client, tmp_path):
        workspace = tmp_path / "workspace"
        (workspace / "extensions").mkdir(parents=True)
        _write_manifest_canvas_package(workspace)
        _write_manifest_workflow_runtime_package(workspace)
        content = (
            "---\n"
            "name: Runtime Default\n"
            "description: Use runtime default output surface.\n"
            "runtime_profile: openprose\n"
            "requires:\n"
            "  tools: [web_search]\n"
            "steps:\n"
            "  - id: search\n"
            "    tool: web_search\n"
            "    arguments: {}\n"
            "---\n"
        )
        with (
            patch("src.api.workflows.settings.workspace_dir", str(workspace)),
            patch(
                "src.api.workflows.get_base_tools_and_active_skills",
                return_value=([SimpleNamespace(name="web_search")], [], "disabled"),
            ),
        ):
            resp = await client.post("/api/workflows/validate", json={"content": content})

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["workflow"]["runtime_profile"] == "openprose"
        assert payload["workflow"]["output_surface"] == "guardian-board"
        assert payload["workflow"]["output_surface_title"] == "Guardian Board"
        assert payload["workflow"]["declared_output_surface"] == ""
        assert payload["missing_output_surfaces"] == []

    @pytest.mark.asyncio
    async def test_workflow_source_surfaces_runtime_default_output_surface(self, client, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        _write_manifest_canvas_package(tmp_path)
        _write_manifest_workflow_runtime_package(tmp_path)
        content = (
            "---\n"
            "name: runtime-source\n"
            "description: Use runtime default output surface.\n"
            "runtime_profile: openprose\n"
            "requires:\n"
            "  tools: [web_search]\n"
            "steps:\n"
            "  - id: search\n"
            "    tool: web_search\n"
            "    arguments: {}\n"
            "---\n"
        )
        workflow_path = workflows_dir / "runtime-source.md"
        workflow_path.write_text(content, encoding="utf-8")
        with (
            patch("src.api.workflows.settings.workspace_dir", str(tmp_path)),
            patch(
                "src.api.workflows.get_base_tools_and_active_skills",
                return_value=([SimpleNamespace(name="web_search")], [], "disabled"),
            ),
        ):
            workflow_manager.init(str(workflows_dir), manifest_roots=default_manifest_roots_for_workspace(str(tmp_path)))
            resp = await client.get("/api/workflows/runtime-source/source")

        assert resp.status_code == 200
        payload = resp.json()
        assert payload["workflow"]["runtime_profile"] == "openprose"
        assert payload["workflow"]["output_surface"] == "guardian-board"
        assert payload["workflow"]["output_surface_title"] == "Guardian Board"

    @pytest.mark.asyncio
    async def test_runtime_default_and_canvas_metadata_ignore_lower_priority_duplicate_names(self, client, tmp_path):
        workflows_dir = tmp_path / "workflows"
        workflows_dir.mkdir()
        _write_manifest_canvas_package(
            tmp_path,
            package_name="canvas-a",
            extension_id="seraph.canvas-a",
            display_name="A Canvas Winner",
            title="Winner Board",
        )
        _write_manifest_canvas_package(
            tmp_path,
            package_name="canvas-z",
            extension_id="seraph.canvas-z",
            display_name="Z Canvas Loser",
            title="Loser Board",
        )
        _write_manifest_workflow_runtime_package(
            tmp_path,
            package_name="runtime-a",
            extension_id="seraph.runtime-a",
            display_name="A Runtime Winner",
            default_output_surface="guardian-board",
        )
        _write_manifest_workflow_runtime_package(
            tmp_path,
            package_name="runtime-z",
            extension_id="seraph.runtime-z",
            display_name="Z Runtime Loser",
            default_output_surface="loser-board",
        )
        content = (
            "---\n"
            "name: duplicate-runtime-surface\n"
            "description: Use runtime default output surface.\n"
            "runtime_profile: openprose\n"
            "requires:\n"
            "  tools: [web_search]\n"
            "steps:\n"
            "  - id: search\n"
            "    tool: web_search\n"
            "    arguments: {}\n"
            "---\n"
        )
        workflow_path = workflows_dir / "duplicate-runtime-surface.md"
        workflow_path.write_text(content, encoding="utf-8")

        with patch("src.api.workflows.settings.workspace_dir", str(tmp_path)):
            workflow_manager.init(str(workflows_dir), manifest_roots=default_manifest_roots_for_workspace(str(tmp_path)))
            workflow = workflow_manager.get_workflow("duplicate-runtime-surface")
            assert workflow is not None
            assert workflow.output_surface == "guardian-board"
            assert workflow.output_surface_title == "Winner Board"


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
            "secret_ref_fields": [],
        }]

    def test_factory_wraps_high_risk_workflows_for_approval(self, async_db):
        asyncio.run(SessionManager().get_or_create("s1"))

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
        asyncio.run(SessionManager().get_or_create("s1"))

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

    def test_workflow_approval_context_marks_authenticated_mcp_sources(self):
        workflow = SimpleNamespace(name="mcp_sync", step_tools=["mcp_fetch_repo"])
        mcp_tool = MagicMock()
        mcp_tool.seraph_source_context = {
            "server_name": "github",
            "hostname": "api.github.com",
            "source": "extension",
            "authenticated_source": True,
            "credential_sources": ["vault:github_token"],
        }

        approval_context = _approval_context_for_workflow(
            workflow,
            {"mcp_fetch_repo": mcp_tool},
        )

        assert approval_context["authenticated_source"] is True
        assert "authenticated_external_source" in approval_context["execution_boundaries"]
        assert approval_context["source_systems"] == [
            {
                "server_name": "github",
                "hostname": "api.github.com",
                "source": "extension",
                "authenticated_source": True,
                "credential_sources": ["vault:github_token"],
            }
        ]
        assert _checkpoint_context_allowed(approval_context) is False

    def test_workflow_approval_context_marks_delegated_vault_routes_high_risk(self):
        workflow = Workflow(
            name="vault-delegation",
            description="Delegate secret handling",
            inputs={},
            steps=[
                WorkflowStep(
                    id="delegate",
                    tool="delegate_task",
                    arguments={
                        "task": "Store this API key in the vault.",
                        "specialist": "vault",
                    },
                )
            ],
            requires_tools=["delegate_task"],
        )

        approval_context = _approval_context_for_workflow(workflow)

        assert approval_context["risk_level"] == "high"
        assert approval_context["delegated_specialists"] == ["vault_keeper"]
        assert "delegation" in approval_context["execution_boundaries"]
        assert "secret_management" in approval_context["execution_boundaries"]
        assert "secret_injection" in approval_context["execution_boundaries"]
        assert _checkpoint_context_allowed(approval_context) is False

    def test_workflow_tool_approval_context_includes_authenticated_delegated_source(self):
        workflow = Workflow(
            name="delegated-mcp-sync",
            description="Delegate authenticated source work",
            inputs={
                "task": {"type": "string", "required": True},
                "specialist": {"type": "string", "required": True},
            },
            steps=[
                WorkflowStep(
                    id="delegate",
                    tool="delegate_task",
                    arguments={
                        "task": "{{ task }}",
                        "specialist": "{{ specialist }}",
                    },
                )
            ],
            requires_tools=["delegate_task"],
        )
        workflow_tool = WorkflowTool(
            workflow,
            {"delegate_task": DummyTool("delegate_task", lambda **_kwargs: "delegated")},
        )
        mcp_tool = MagicMock()
        mcp_tool.name = "mcp_github_issues"
        mcp_tool.seraph_source_context = {
            "server_name": "github",
            "hostname": "api.github.com",
            "source": "extension",
            "authenticated_source": True,
            "credential_sources": ["env:GITHUB_TOKEN", "vault:github_token"],
        }
        github_specialist = SimpleNamespace(name="mcp_github", tools=[mcp_tool])

        with patch("src.agent.specialists.build_all_specialists", return_value=[github_specialist]):
            approval_context = workflow_tool.get_approval_context(
                {
                    "task": "Review the assigned issues.",
                    "specialist": "mcp_github",
                }
            )

        assert approval_context["risk_level"] == "high"
        assert approval_context["authenticated_source"] is True
        assert approval_context["delegated_specialists"] == ["mcp_github"]
        assert approval_context["delegated_tool_names"] == ["mcp_github_issues"]
        assert "external_mcp" in approval_context["execution_boundaries"]
        assert "authenticated_external_source" in approval_context["execution_boundaries"]
        assert approval_context["source_systems"] == [
            {
                "server_name": "github",
                "hostname": "api.github.com",
                "source": "extension",
                "authenticated_source": True,
                "credential_sources": ["env:GITHUB_TOKEN", "vault:github_token"],
            }
        ]
        assert _checkpoint_context_allowed(approval_context) is False

    def test_workflow_tool_approval_context_records_delegated_tool_inventory(self):
        workflow = Workflow(
            name="delegated-mcp-sync",
            description="Delegate authenticated source work",
            inputs={
                "task": {"type": "string", "required": True},
                "specialist": {"type": "string", "required": True},
            },
            steps=[
                WorkflowStep(
                    id="delegate",
                    tool="delegate_task",
                    arguments={
                        "task": "{{ task }}",
                        "specialist": "{{ specialist }}",
                    },
                )
            ],
            requires_tools=["delegate_task"],
        )
        workflow_tool = WorkflowTool(
            workflow,
            {"delegate_task": DummyTool("delegate_task", lambda **_kwargs: "delegated")},
        )
        issues_tool = MagicMock()
        issues_tool.name = "mcp_github_issues"
        repo_tool = MagicMock()
        repo_tool.name = "mcp_github_repo"
        github_specialist = SimpleNamespace(name="mcp_github", tools=[issues_tool, repo_tool])

        with patch("src.agent.specialists.build_all_specialists", return_value=[github_specialist]):
            approval_context = workflow_tool.get_approval_context(
                {
                    "task": "Review the assigned issues.",
                    "specialist": "mcp_github",
                }
            )

        assert approval_context["delegated_specialists"] == ["mcp_github"]
        assert approval_context["delegated_tool_names"] == ["mcp_github_issues", "mcp_github_repo"]

    def test_workflow_metadata_fails_closed_when_delegate_target_is_dynamic(self):
        workflow = Workflow(
            name="dynamic-delegation",
            description="Dynamic delegation workflow",
            inputs={"task": {"type": "string", "required": True}},
            steps=[
                WorkflowStep(
                    id="delegate",
                    tool="delegate_task",
                    arguments={"task": "{{ task }}"},
                )
            ],
            requires_tools=["delegate_task"],
        )
        mgr = WorkflowManager()
        mgr._workflows = [workflow]

        metadata = mgr.get_tool_metadata(workflow.tool_name)

        assert metadata is not None
        assert metadata["policy_modes"] == ["full"]
        assert metadata["risk_level"] == "high"
        assert metadata["accepts_secret_refs"] is True
        assert metadata["approval_context"]["delegation_target_unresolved"] is True

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
