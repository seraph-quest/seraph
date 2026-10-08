"""Provider-free mechanics checks using real current filesystem/MCP wrappers."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
from smolagents import Tool

from config.settings import settings
from src.native_tools.registry import ToolRegistry
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.tools.mcp_manager import MCPManager


@pytest.fixture
def registry(tmp_path, monkeypatch, async_db):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    # Never let these checks accidentally contact a provider or external host.
    import socket
    def denied(*args, **kwargs):
        raise AssertionError("network access forbidden in adapter checks")
    monkeypatch.setattr(socket.socket, "connect", denied)
    instance = ToolRegistry()
    instance.start()
    yield instance
    instance.stop()


@pytest.fixture
def principal():
    return TrustPrincipal(principal_id="test:adapter", principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,), session_id="test-session", job_id="test-job")


def descriptor(registry, name):
    return next(item for item in registry.descriptors() if item.tool_id == name)


async def test_real_filesystem_wrappers_and_readback(registry, principal, tmp_path):
    # Actual bundled tools and actual effect journal, never a success fixture.
    written = await registry.invoke(descriptor(registry, "write_file"),
        {"file_path": "result.txt", "content": "local receipt"}, principal=principal,
        job_id="test-job", fencing_token=1)
    assert written["bytes_written"] == 13
    assert (tmp_path / "result.txt").read_text() == "local receipt"
    result = await registry.invoke(descriptor(registry, "read_file"),
        {"file_path": "result.txt"}, principal=principal, job_id="test-job", fencing_token=1)
    assert result["content"] == "local receipt"
    assert result["sha256"] == written["content_sha256"]


@pytest.mark.parametrize("inputs", [
    {"file_path": "../escape", "content": "x"},
    {"file_path": ".env", "content": "x"},
    {"file_path": "x", "content": "x", "approved": True},
])
async def test_closed_schema_and_workspace_boundaries(registry, principal, tmp_path, inputs):
    with pytest.raises(Exception):
        await registry.invoke(descriptor(registry, "write_file"), inputs,
            principal=principal, job_id="test-job", fencing_token=1)
    assert not (tmp_path / "x").exists()


async def test_authority_stale_descriptor_and_failed_readback(registry, principal, tmp_path):
    selected = descriptor(registry, "write_file")
    for actor in (replace(principal, revoked=True), replace(principal, grants=()),
                  replace(principal, job_id="other")):
        with pytest.raises(Exception):
            await registry.invoke(selected, {"file_path": "denied", "content": "x"},
                principal=actor, job_id="test-job", fencing_token=1)
    assert not (tmp_path / "denied").exists()
    with pytest.raises(PermissionError):
        await registry.invoke(selected.model_copy(update={"version": "stale"}),
            {"file_path": "denied", "content": "x"}, principal=principal,
            job_id="test-job", fencing_token=1)
    with pytest.raises(Exception):
        await registry.invoke(descriptor(registry, "read_file"), {"file_path": "missing"},
            principal=principal, job_id="test-job", fencing_token=1)
    registry.stop()
    with pytest.raises(RuntimeError):
        registry.descriptors()


class LocalMCPTool(Tool):
    name = "repo_read"
    description = "Local typed MCP repository adapter fixture"
    inputs = {"query": {"type": "string", "description": "query"}}
    output_type = "string"
    output_schema = {"type": "object", "properties": {"value": {"type": "string", "maxLength": 1000}},
                     "required": ["value"], "additionalProperties": False}
    def __init__(self):
        super().__init__()
        self.is_initialized = True
        self.calls = 0
        self.result = '{"value":"local repository readback"}'
        self.failure = None
    def forward(self, query: str) -> str:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.result


@pytest.fixture
def mcp_registry(tmp_path, registry):
    manager = MCPManager()
    tool = LocalMCPTool()
    source = manager._build_source_context(name="local", url="https://example.invalid/mcp",
        extension_id="local.extension", extension_reference="mcp.yaml")
    source["connection_revision"] = 1
    manager._instrument_mcp_tool(tool, source)
    manager._config["local"] = {"enabled": True, "url": source["url"],
        "extension_id": "local.extension", "extension_reference": "mcp.yaml"}
    manager._clients["local"] = SimpleNamespace(disconnect=lambda: None)
    manager._tools["local"] = [tool]
    manager._connection_revisions["local"] = 1
    manager._status["local"] = {"status": "connected", "error": None}
    from src.tools.mcp_manager import _TaskOutputGuard
    manager._task_output_guards["local"] = _TaskOutputGuard()
    declaration = {"version": "1", "input_schema": {"type": "object",
        "properties": {"query": {"type": "string", "maxLength": 100}},
        "required": ["query"], "additionalProperties": False}, "output_schema": tool.output_schema,
        "effects": ["external_read"], "permissions": ["capability_execute"],
        "verifier": "json_schema.v1", "deadline": 10}
    path = tmp_path / "mcp.yaml"
    path.write_text(json.dumps({"name": "local", "url": source["url"],
                               "task_tools": {tool.name: declaration}}))
    contribution = SimpleNamespace(extension_id="local.extension", reference="mcp.yaml",
        metadata={"trust": "local", "name": "local", "url": source["url"], "resolved_path": str(path)})
    registry.mcp_runtime = manager
    registry.extension_registry = SimpleNamespace(list_contributions=lambda kind: [contribution])
    return registry, manager, tool, path, declaration


async def test_mcp_current_guarded_wrapper_schema_and_connection_revision(mcp_registry, principal):
    registry, manager, tool, _, _ = mcp_registry
    selected = descriptor(registry, "mcp:local:repo_read")
    from src.approval.exceptions import ApprovalRequired
    from src.native_tools.task_adapters import TaskToolApprovalRequired
    from src.approval.repository import approval_repository
    with pytest.raises(TaskToolApprovalRequired) as approval:
        await registry.invoke(selected, {"query": "read"}, principal=principal,
            job_id="test-job", fencing_token=1)
    assert tool.calls == 0
    snapshot = registry.approval_context(selected, {"query": "read"}, job_id="test-job")
    from src.approval.repository import fingerprint_tool_call
    expected_context = dict(tool.get_approval_context({"query": "read"}))
    expected_context["workflow_run_identity"] = "test-job"
    assert snapshot == {"tool_name": tool.name, "approval_context": expected_context,
        "fingerprint": fingerprint_tool_call(tool.name, {"query": "read"},
            approval_context=expected_context)}
    pending_row = await approval_repository.get(approval.value.approval_id)
    assert pending_row.status == "pending"
    assert pending_row.fingerprint == snapshot["fingerprint"]
    assert json.loads(pending_row.details_json)["approval_context"] == snapshot["approval_context"]
    assert tool.calls == 0
    assert approval.value.precontact is True
    assert approval.value.binding.job_id == "test-job"
    assert approval.value.binding.fencing_token == 1
    from src.work_board.general_task import digest
    assert approval.value.binding.input_digest == digest({"query": "read"})
    assert approval.value.binding.descriptor_digest == digest(selected.model_dump(mode="json"))
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        approval.value.binding.job_id = "other-job"
    assert await approval_repository.resolve(approval.value.approval_id, "approved")
    result = await registry.invoke(selected, {"query": "read"}, principal=principal,
        job_id="test-job", fencing_token=1)
    assert result == {"value": "local repository readback"}
    assert tool.calls == 1
    tool.result = '{"unexpected":"untyped"}'
    with pytest.raises(ApprovalRequired) as approval:
        await registry.invoke(selected, {"query": "read"}, principal=principal,
            job_id="test-job", fencing_token=1)
    assert await approval_repository.resolve(approval.value.approval_id, "approved")
    with pytest.raises(Exception):
        await registry.invoke(selected, {"query": "read"}, principal=principal,
            job_id="test-job", fencing_token=1)
    manager._connection_revisions["local"] += 1
    with pytest.raises(PermissionError):
        registry.approval_context(selected, {"query": "read"}, job_id="test-job")
    with pytest.raises(PermissionError):
        await registry.invoke(selected, {"query": "read"}, principal=principal,
            job_id="test-job", fencing_token=1)
    assert tool.calls == 2


async def test_tool_approval_exception_after_contact_is_not_precontact_proof(mcp_registry, principal):
    from src.approval.exceptions import ApprovalRequired
    from src.approval.repository import approval_repository
    from src.native_tools.task_adapters import TaskToolApprovalRequired
    registry, _, tool, _, _ = mcp_registry
    selected = descriptor(registry, "mcp:local:repo_read")
    with pytest.raises(TaskToolApprovalRequired) as pending:
        await registry.invoke(selected, {"query": "effect"}, principal=principal,
            job_id="test-job", fencing_token=1)
    assert tool.calls == 0
    assert await approval_repository.resolve(pending.value.approval_id, "approved")
    tool.failure = ApprovalRequired(approval_id="untrusted-after-contact",
        session_id=principal.session_id, tool_name=tool.name,
        risk_level="high", summary="Tool reports approval after contacting its target")
    with pytest.raises(ApprovalRequired) as contacted:
        await registry.invoke(selected, {"query": "effect"}, principal=principal,
            job_id="test-job", fencing_token=1)
    assert type(contacted.value) is ApprovalRequired
    assert tool.calls == 1


async def test_metadata_hook_cannot_forge_wrapper_precontact_proof(mcp_registry, principal):
    from src.approval.exceptions import ApprovalRequired
    registry, _, tool, _, _ = mcp_registry
    selected = descriptor(registry, "mcp:local:repo_read")
    def untrusted_hook(arguments):
        raise ApprovalRequired(approval_id="untrusted-hook", session_id=principal.session_id,
            tool_name=tool.name, risk_level="high", summary="Untrusted hook approval")
    tool.get_approval_context = untrusted_hook
    with pytest.raises(ApprovalRequired) as failure:
        await registry.invoke(selected, {"query": "effect"}, principal=principal,
            job_id="test-job", fencing_token=1)
    assert type(failure.value) is ApprovalRequired
    assert tool.calls == 0


async def test_oversized_result_is_rejected_before_task_or_audit_result_parsing(mcp_registry, principal, monkeypatch):
    from src.approval.repository import approval_repository
    from src.native_tools.task_adapters import TaskToolApprovalRequired
    from src.tools.mcp_manager import MCPTaskOutputLimit, TASK_OUTPUT_BYTES
    registry, _, tool, _, _ = mcp_registry
    selected = descriptor(registry, "mcp:local:repo_read")
    with pytest.raises(TaskToolApprovalRequired) as pending:
        await registry.invoke(selected, {"query": "effect"}, principal=principal,
            job_id="test-job", fencing_token=1)
    assert await approval_repository.resolve(pending.value.approval_id, "approved")
    tool.result = '{"value":"PRIVATE_FIXTURE_SENTINEL' + "x" * TASK_OUTPUT_BYTES + '"}'
    original_parse = json.loads
    parsed = []
    def parse(value, *args, **kwargs):
        if value is tool.result:
            parsed.append(True)
        return original_parse(value, *args, **kwargs)
    monkeypatch.setattr(json, "loads", parse)
    with pytest.raises(MCPTaskOutputLimit) as failure:
        await registry.invoke(selected, {"query": "effect"}, principal=principal,
            job_id="test-job", fencing_token=1)
    assert tool.calls == 1 and parsed == []
    assert "PRIVATE_FIXTURE_SENTINEL" not in str(failure.value)


def test_mcp_unknown_missing_changed_contracts_are_excluded(mcp_registry):
    registry, manager, tool, path, declaration = mcp_registry
    assert descriptor(registry, "mcp:local:repo_read")
    declaration["effects"] = ["unknown"]
    path.write_text(json.dumps({"task_tools": {tool.name: declaration}}))
    assert not [item for item in registry.descriptors() if item.server_id]
    declaration["effects"] = ["external_read"]
    path.write_text(json.dumps({"task_tools": {tool.name: declaration}}))
    tool.output_schema = None
    assert not [item for item in registry.descriptors() if item.server_id]
    assert registry.blocked_tools() == [{"tool_id": "mcp:local:repo_read",
        "reason": "trusted_typed_contract_or_policy_unavailable"}]
    tool.output_schema = declaration["output_schema"]
    manager._status["local"] = {"status": "error", "error": "reconnect failed"}
    assert not [item for item in registry.descriptors() if item.server_id]
    manager._status["local"] = {"status": "connected", "error": None}
    manager._connection_revisions["local"] = 2
    assert not [item for item in registry.descriptors() if item.server_id]


def test_sessionful_transport_exclusion_has_operator_visible_reason(mcp_registry):
    registry, manager, _, _, _ = mcp_registry
    manager._task_output_guards["local"].inline_supported = False
    assert not [item for item in registry.descriptors() if item.server_id]
    assert registry.blocked_tools() == [{"tool_id": "mcp:local:repo_read",
        "reason": "mcp_task_stateless_inline_transport_required"}]
