"""Actual filesystem and stock MCP JSON-RPC vertical slice, with no sockets.

Only the MCP HTTP transport is owned by the fixture. Discovery, MCPClient,
policy, approval, audit, native children and physical artifacts remain real.
"""
import asyncio
import hashlib
import json
import socket
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from sqlalchemy import select

from tests.test_document_build_native_capacity import build_admission_lifecycle
from tests.test_general_task_planner import accounting_db, forbid_external_inference, prepare
from tests.test_work_board_m6_provider_free_journey import _goal


@pytest.mark.asyncio
async def test_authenticated_three_tools_stock_mcp_protocol_same_child_approval(accounting_db, monkeypatch, build_admission_lifecycle):
    from src.auth.service import authenticate_session
    from src.api import work_board as api
    from src.api.approvals import router as approvals_router
    from src.db.models import AuditEvent, MemoryProposal, WorkBoardAttempt, WorkflowRunState
    from src.native_tools.registry import ToolRegistry
    from src.tools.mcp_manager import MCPManager
    from src.work_board.contracts import GENERAL_TASK_NATIVE_CHILD_KIND
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.general_task import GeneralTaskService

    jobs, owner = await prepare(accounting_db, monkeypatch)
    await build_admission_lifecycle.start()
    workspace, _engine, factory = accounting_db
    sessions = factory.accounting_sessions
    source = "Private local content carried only by actual dependency artifacts.\n"
    (workspace / "original.txt").write_text(source)
    source_sha = hashlib.sha256(source.encode()).hexdigest()
    endpoint = "https://fixture.invalid/mcp"
    protocol_calls, effects, guarded_calls = [], [], []
    input_schema = {"type": "object", "properties": {
        "file_path": {"type": "string", "maxLength": 100},
        "expected_sha256": {"type": "string", "maxLength": 64}},
        "required": ["file_path", "expected_sha256"], "additionalProperties": False}
    output_schema = {"type": "object", "properties": {
        "value": {"type": "string", "maxLength": 4096},
        "sha256": {"type": "string", "maxLength": 64}},
        "required": ["value", "sha256"], "additionalProperties": False}
    mcp_app = FastAPI()
    @mcp_app.api_route("/mcp", methods=["POST", "GET", "DELETE"])
    async def mcp_protocol(request: Request):
        if request.method != "POST":
            return Response(status_code=405)
        message = await request.json()
        method = message["method"]
        protocol_calls.append(method)
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {},
                "serverInfo": {"name": "owned-local-readback", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "verify_copy", "description": "Read the physically copied owned file",
                "inputSchema": input_schema, "outputSchema": output_schema}]}
        elif method == "tools/call":
            assert message["params"]["name"] == "verify_copy"
            arguments = message["params"]["arguments"]
            assert arguments == {"file_path": "copied.txt", "expected_sha256": source_sha}
            physical = (workspace / arguments["file_path"]).read_bytes()
            assert hashlib.sha256(physical).hexdigest() == arguments["expected_sha256"]
            output = {"value": physical.decode(), "sha256": source_sha}
            effect_path = workspace / "mcp-physical-readback.json"
            effect_path.write_text(json.dumps(output, sort_keys=True))
            effect_path.chmod(0o600)
            effects.append({"request_id": message["id"], "arguments": arguments,
                "sha256": hashlib.sha256(effect_path.read_bytes()).hexdigest()})
            result = {"content": [{"type": "text", "text": json.dumps(output)}],
                "structuredContent": output, "isError": False}
        else:
            raise AssertionError(f"Unexpected owned MCP method: {method}")
        return Response(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}),
            media_type="application/json")

    class OwnedProtocolTransport(httpx.ASGITransport):
        async def handle_async_request(self, request):
            assert str(request.url) == endpoint
            if request.method == "POST":
                message = json.loads(request.content)
                if message["method"] == "tools/call":
                    assert request.extensions.get("seraph_task_output_bounded") is True
                    assert request.headers["accept-encoding"] == "identity"
                    guarded_calls.append(message["id"])
            return await super().handle_async_request(request)
    def owned_http_client(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(transport=OwnedProtocolTransport(app=mcp_app),
            headers=headers, timeout=timeout, auth=auth)
    monkeypatch.setattr("mcp.shared._httpx_utils.create_mcp_http_client", owned_http_client)
    # Exercise the stock endpoint policy without permitting fixture DNS egress.
    # This public-IP DNS answer is fixture data; all HTTP goes to owned ASGI.
    def owned_dns(host, port, *args, **kwargs):
        assert host == "fixture.invalid", "No ambient DNS is permitted"
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port or 443))]
    monkeypatch.setattr(socket, "getaddrinfo", owned_dns)
    manager = MCPManager()
    declaration = {"version": "1", "input_schema": input_schema, "output_schema": output_schema,
        "effects": ["external_read"], "permissions": ["capability_execute"],
        "verifier": "json_schema.v1", "deadline": 10}
    declaration_path = workspace / "local-mcp.json"
    declaration_path.write_text(json.dumps({"name": "local", "url": endpoint,
        "task_tools": {"verify_copy": declaration}}))
    contribution = SimpleNamespace(extension_id="owned.local", reference="local-mcp.json",
        metadata={"trust": "local", "name": "local", "url": endpoint,
            "resolved_path": str(declaration_path)})
    registry = ToolRegistry(mcp_runtime=manager,
        extension_registry=SimpleNamespace(list_contributions=lambda kind: [contribution]))
    service = None
    try:
        await asyncio.to_thread(manager.add_server, "local", endpoint,
            extension_id="owned.local", extension_reference="local-mcp.json")
        assert manager.is_connected("local"), manager.get_config()
        assert "initialize" in protocol_calls and "tools/list" in protocol_calls
        advertised = manager._clients["local"]._adapter.mcp_tools
        assert len(advertised) == 1 and advertised[0][0].name == "verify_copy"
        assert advertised[0][0].outputSchema == output_schema
        registry.start()
        service = GeneralTaskService(registry); service.start()
        descriptors, tool_digest = service.snapshot()
        selected = {item.tool_id: item for item in descriptors}
        mcp_id = "mcp:local:verify_copy"
        assert mcp_id in selected, (manager.get_config(), manager.task_tool_block_reason("local"),
            [(tool.name, tool.inputs) for tool in manager.get_server_tools("local")])
        dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
        monkeypatch.setattr(api, "dispatcher", dispatcher)
        goal = _goal("goal-full-native", "Read, copy and verify through actual local MCP")
        goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
        async with sessions() as db:
            db.add(goal)
        app = FastAPI()
        @app.middleware("http")
        async def operator(request, call_next):
            request.state.operator = await authenticate_session(owner.session_id, touch=False)
            return await call_next(request)
        app.include_router(api.router, prefix="/api")
        app.include_router(approvals_router, prefix="/api")
        plan = {"revision": 1, "steps": [
            {"step_id": "read", "tool_id": "read_file", "input": {"file_path": "original.txt"},
                "output_contract": selected["read_file"].output_schema},
            {"step_id": "write", "tool_id": "write_file", "depends_on": ["read"],
                "input": {"file_path": "copied.txt", "content": {"$dependency": {"step_id": "read", "pointer": "/content"}}},
                "output_contract": selected["write_file"].output_schema},
            {"step_id": "verify", "tool_id": mcp_id, "depends_on": ["write"],
                "input": {"file_path": {"$dependency": {"step_id": "write", "pointer": "/file_path"}},
                    "expected_sha256": {"$dependency": {"step_id": "write", "pointer": "/content_sha256"}}},
                "output_contract": output_schema}]}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            created = await client.post("/api/work-board/general-tasks", json={"goal_revision": 1,
                "idempotency_key": "actual-three-native-tools", "expected_plan_revision": 1, "input": {"goal_ref": goal.id,
                    "intent": "Read original.txt, copy its content and verify copied.txt with the local MCP tool",
                    "requested_output": output_schema, "tool_set_digest": tool_digest}, "plan": plan})
            assert created.status_code == 200, created.text
            card = created.json()["task"]
            assert card["status"] == "triage" and effects == []
            task_id = card["task_id"]
            accepted = await client.post(f"/api/work-board/tasks/{task_id}/actions",
                json={"action": "promote", "expected_revision": card["task_revision"]})
            assert accepted.status_code == 200, accepted.text
            assert (await dispatcher.run_pass())["blocked"] == 1
            assert (workspace / "copied.txt").read_text() == source and effects == []
            waiting = (await client.get(f"/api/work-board/tasks/{task_id}/plan")).json()
            pause = waiting["approval_pause"]
            assert waiting["native_execution"]["phase"] == "approval_wait"
            original_child = await jobs.get_job(pause["child_job_id"])
            original_parent = await jobs.get_job(pause["workflow_run_id"])
            assert original_child["attempt_count"] == 1 and original_child["status"] == "paused"
            approved = await client.post(f"/api/approvals/{pause['approval_id']}/approve")
            assert approved.status_code == 200, approved.text
            current = (await client.get(f"/api/work-board/tasks/{task_id}/plan")).json()
            from tests.test_general_task_native_dispatch_api import native_resume_body
            resume_body = native_resume_body(current)
            resumed = await client.post(f"/api/work-board/tasks/{task_id}/plan/resume", json=resume_body)
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["task"]["status"] == "review"
            assert len(effects) == 1 and protocol_calls.count("tools/call") == 1
            assert guarded_calls == [effects[0]["request_id"]]
            child = await jobs.get_job(pause["child_job_id"])
            parent = await jobs.get_job(pause["workflow_run_id"])
            assert child["status"] == parent["status"] == "succeeded"
            assert parent["owner"]["principal_id"] == owner.principal_id
            assert parent["operator_session_id"] == owner.session_id
            assert parent["goal_id"] == goal.id and parent["goal_revision"] == 1
            assert child["attempt_count"] == parent["attempt_count"] == 1
            assert child["deadline_at"] == original_child["deadline_at"]
            assert parent["deadline_at"] == original_parent["deadline_at"]
            async with sessions() as db:
                children = list((await db.execute(select(WorkflowRunState).where(
                    WorkflowRunState.parent_job_id == parent["job_id"]))).scalars())
                attempt = await db.get(WorkBoardAttempt, pause["attempt_id"])
                assert len(children) == 3 and all(row.job_kind == GENERAL_TASK_NATIVE_CHILD_KIND
                    and row.status == "succeeded" and row.attempt_count == 1
                    and row.owner_principal_id == owner.principal_id and row.operator_session_id == owner.session_id
                    and row.goal_id == goal.id and row.goal_revision == 1 for row in children)
                assert attempt.ended_at and attempt.outcome == "verified"
                audits = list((await db.execute(select(AuditEvent).where(AuditEvent.tool_name == "verify_copy"))).scalars())
                assert audits and any(row.event_type == "tool_call" for row in audits)
                assert not list((await db.execute(select(MemoryProposal))).scalars())
            final = (await client.get(f"/api/work-board/tasks/{task_id}/plan")).json()
            detail = await client.get(f"/api/work-board/tasks/{task_id}")
            assert detail.status_code == 200 and detail.json()["attempts"][0]["readback_status"] == "verified"
            assert final["native_execution"]["phase"] == "complete" and final["no_learning"] is True
            assert len(final["native_execution"]["partial_output_refs"]) == 3
            verified = [item for item in parent["checkpoints"] if item["checkpoint_id"].startswith("general:verified:")]
            assert {item["checkpoint_id"] for item in verified} == {
                "general:verified:read", "general:verified:write", "general:verified:verify"}
            for checkpoint in verified:
                artifact = checkpoint["payload"]
                path = workspace / artifact["file_path"]
                assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["content_sha256"]
                assert path.stat().st_mode & 0o077 == 0
            assert json.loads((workspace / "mcp-physical-readback.json").read_text()) == {"value": source, "sha256": source_sha}
            assert (await client.post(f"/api/work-board/tasks/{task_id}/plan/resume", json=resume_body)).status_code == 409
            await dispatcher.reconcile_linked_attempts()
            assert protocol_calls.count("tools/call") == 1
            print(json.dumps({"flow": "auth_manual_plan_accept_read_write_stock_mcp_approval_resume",
                "native_children": 3, "tools_call": 1, "physical_effect_sha256": effects[0]["sha256"],
                "no_learning": True, "external_socket_contacts": 0}, sort_keys=True))
    finally:
        if service is not None:
            service.stop()
        registry.stop()
        await asyncio.to_thread(manager.disconnect_all)
