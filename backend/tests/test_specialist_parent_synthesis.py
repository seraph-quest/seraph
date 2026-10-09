"""Owned functional MCP execution; simulated network, real protocol and tools.

Only the fixture origin's DNS and final HTTP transport are simulated. The MCP
handshake, advertisement, guarded session, typed registry, approval wrapper,
CPU concatenation, filesystem effects and readbacks remain their actual owners.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import socket
from typing import Annotated

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, Field

from src.native_tools.registry import ToolRegistry
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime


ORIGIN = "https://specialist-mcp.functional-fixture.example"
MCP_URL = ORIGIN + "/mcp"


class CombinedOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: Annotated[str, Field(max_length=4096)]


@asynccontextmanager
async def genuine_mcp_registry(workspace, monkeypatch):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
    from src.extensions.registry import ExtensionRegistry
    from src.tools.mcp_manager import MCPManager

    received = []
    server = FastMCP("finite-concatenation", stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=["specialist-mcp.functional-fixture.example"]))

    @server.tool()
    def concatenate(left: Annotated[str, Field(max_length=2048)],
                    right: Annotated[str, Field(max_length=2048)]) -> CombinedOutput:
        received.append({"left": left, "right": right})
        return CombinedOutput(content=left + right)

    # This is the fixture server's actual advertisement, not a client override.
    server_tool = server._tool_manager.get_tool("concatenate")
    server_tool.parameters["additionalProperties"] = False
    app = server.streamable_http_app()
    protocol = []
    original_client = httpx.AsyncClient

    class OwnedMCPClient(original_client):
        async def __aenter__(self):
            self._fixture_lifespan = app.router.lifespan_context(app)
            await self._fixture_lifespan.__aenter__()
            return await super().__aenter__()

        async def __aexit__(self, *args):
            try:
                return await super().__aexit__(*args)
            finally:
                await self._fixture_lifespan.__aexit__(*args)

    async def request_receipt(request):
        assert str(request.url) == MCP_URL
        if request.method == "POST":
            protocol.append(json.loads(request.content))

    def owned_http_client(headers=None, timeout=None, auth=None):
        return OwnedMCPClient(headers=headers, timeout=timeout, auth=auth,
            transport=httpx.ASGITransport(app=app), event_hooks={"request": [request_receipt]})

    monkeypatch.setattr("mcp.shared._httpx_utils.create_mcp_http_client", owned_http_client)
    original_dns = socket.getaddrinfo
    def fixture_dns(host, *args, **kwargs):
        if host == "specialist-mcp.functional-fixture.example":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        return original_dns(host, *args, **kwargs)
    monkeypatch.setattr(socket, "getaddrinfo", fixture_dns)

    pack = workspace / "extensions" / "finite-concatenation"
    (pack / "mcp").mkdir(parents=True)
    (pack / "manifest.yaml").write_text(json.dumps({"id": "fixture.specialist-concatenation",
        "version": "1.0.0", "display_name": "Finite concatenation fixture", "kind": "capability-pack",
        "trust": "local", "compatibility": {"seraph": ">=2026.4.11"},
        "publisher": {"name": "Functional fixture"},
        "contributes": {"mcp_servers": ["mcp/finite.yaml"]}}))
    # Read actual server-declared contracts; no adapted SDK metadata mutation.
    declaration = {"version": "1", "input_schema": server_tool.parameters,
        "output_schema": server_tool.fn_metadata.output_schema,
        "effects": ["external_read"], "permissions": ["capability_execute"],
        "verifier": "json_schema.v1", "deadline": 10}
    (pack / "mcp" / "finite.yaml").write_text(json.dumps({"name": "finite", "url": MCP_URL,
        "enabled": True, "task_tools": {"concatenate": declaration}}))
    extensions = ExtensionRegistry(manifest_roots=[str(workspace / "extensions")],
        skill_dirs=[], workflow_dirs=[], mcp_runtime=None)
    snapshot = extensions.snapshot()
    assert snapshot.load_errors == [], snapshot.load_errors
    contribution = snapshot.list_contributions("mcp_servers")[0]
    manager = MCPManager()
    manager._config["finite"] = {"enabled": True, "url": MCP_URL,
        "extension_id": contribution.extension_id, "extension_reference": contribution.reference}
    assert manager.endpoint_policy_issues(MCP_URL) == []
    await asyncio.to_thread(manager.connect, "finite", MCP_URL)
    registry = ToolRegistry(mcp_runtime=manager, extension_registry=extensions)
    registry.start()
    try:
        assert manager._status["finite"]["status"] == "connected", manager._status
        guard = manager._task_output_guards["finite"]
        assert guard.bound and guard.inline_supported
        selected = next(item for item in registry.descriptors() if item.tool_id == "mcp:finite:concatenate")
        assert selected.input_schema == declaration["input_schema"]
        assert selected.output_schema == declaration["output_schema"]
        yield registry, selected, received, protocol
    finally:
        registry.stop()
        await asyncio.to_thread(manager.disconnect_all)


@pytest.mark.asyncio
async def test_genuine_mcp_protocol_consumes_actual_workspace_bodies(task_runtime, monkeypatch):
    from src.native_tools.task_adapters import TaskToolApprovalRequired
    from src.approval.repository import approval_repository
    sessions, workspace = task_runtime
    principal = TrustPrincipal(principal_id="fixture:concatenate", principal_type=PrincipalType.OPERATOR,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,), session_id="fixture-concatenate", job_id="fixture-job")
    async with genuine_mcp_registry(workspace, monkeypatch) as (registry, selected, received, protocol):
        by_id = {item.tool_id: item for item in registry.descriptors()}
        for path, content in (("child-A.txt", "physical first\n"), ("child-B.txt", "distinct second\n")):
            await registry.invoke(by_id["write_file"], {"file_path": path, "content": content},
                principal=principal, job_id="fixture-job", fencing_token=1)
        inputs = {}
        for key, path in (("left", "child-A.txt"), ("right", "child-B.txt")):
            result = await registry.invoke(by_id["read_file"], {"file_path": path},
                principal=principal, job_id="fixture-job", fencing_token=1)
            assert result["sha256"] == hashlib.sha256((workspace / path).read_bytes()).hexdigest()
            inputs[key] = result["content"]
        with pytest.raises(TaskToolApprovalRequired) as pending:
            await registry.invoke(selected, inputs, principal=principal, job_id="fixture-job", fencing_token=1)
        assert received == []
        assert await approval_repository.resolve(pending.value.approval_id, "approved")
        combined = await registry.invoke(selected, inputs, principal=principal, job_id="fixture-job", fencing_token=1)
        assert received == [inputs]
        assert combined == {"content": inputs["left"] + inputs["right"]}
        await registry.invoke(by_id["write_file"], {"file_path": "parent.txt", "content": combined["content"]},
            principal=principal, job_id="fixture-job", fencing_token=1)
        assert (workspace / "parent.txt").read_bytes() == (workspace / "child-A.txt").read_bytes() + (workspace / "child-B.txt").read_bytes()
        assert [item["method"] for item in protocol if "method" in item][:3] == ["initialize", "notifications/initialized", "tools/list"]
        calls = [item for item in protocol if item.get("method") == "tools/call"]
        assert len(calls) == 1 and calls[0]["params"]["arguments"] == inputs


async def charged_parent(task_runtime, monkeypatch, registry, mcp_descriptor):
    from config.settings import settings
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec, TaskLimits, WorkBoardOwner, WorkBoardActionRequest
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.dispatcher import WorkBoardDispatcher
    from tests.general_task_test_transport import prepare_literal_planner
    from tests.test_work_board_m6_provider_free_journey import OWNER, SESSION

    sessions, workspace = task_runtime
    monkeypatch.setattr(settings, "use_delegation", True)
    monkeypatch.setattr("src.workflows.job_runtime.get_session", sessions)
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    service = GeneralTaskService(registry)
    service.start()
    by_id = {item.tool_id: item for item in registry.descriptors()}
    def step(name, tool, inputs, deps=()):
        return {"step_id": name, "tool_id": tool, "input": inputs,
            "depends_on": list(deps), "output_contract": by_id[tool].output_schema}
    def delegate(which):
        return step("delegate-" + which, "delegate_task", {"role": "files",
            "instruction": "Write the " + which + " fragment to child-" + which + ".txt",
            "evidence_refs": [], "allowed_tool_ids": ["write_file"],
            "limits": {"max_steps": 1, "max_inference_calls": 1,
                "max_cost_microusd": 100, "wall_seconds": 300}})
    steps = [delegate("A"), delegate("B"),
        step("read-A", "read_file", {"file_path": "child-A.txt"}, ["delegate-A"]),
        step("read-B", "read_file", {"file_path": "child-B.txt"}, ["delegate-B"]),
        step("combine", mcp_descriptor.tool_id,
            {"left": {"$dependency": {"step_id": "read-A", "pointer": "/content"}},
             "right": {"$dependency": {"step_id": "read-B", "pointer": "/content"}}}, ["read-A", "read-B"]),
        step("write-parent", "write_file", {"file_path": "parent.txt",
            "content": {"$dependency": {"step_id": "combine", "pointer": "/content"}}}, ["combine"]),
        step("verify-parent", "read_file", {"file_path": "parent.txt"}, ["write-parent"])]
    initial_plan = PlanSpec(revision=1, steps=steps)
    child_plans = {which: PlanSpec(revision=1, steps=[step("write-" + which, "write_file",
        {"file_path": "child-" + which + ".txt", "content": content})])
        for which, content in (("A", "physical first\n"), ("B", "distinct second\n"))}
    planner, transport = await prepare_literal_planner(sessions, workspace, monkeypatch, owner, initial_plan)
    service.planner = planner
    class FinalModelCalls(list):
        def append(self, body):
            super().append(body)
            payloads = [json.loads(item["content"]) for item in body["messages"] if item["role"] == "user"]
            continuation = next((item for item in payloads if "current_plan_revision" in item), None)
            specialist = next((item for item in payloads if "specialist_role" in item), None)
            if continuation is not None:
                complete = {item["step_id"] for item in continuation["step_statuses"]}
                result = {"schema_version": 1, "revision": continuation["requested_revision"],
                    "steps": [item for item in steps if item["step_id"] not in complete]}
            elif specialist is not None:
                intent = payloads[0]["intent"]
                which = "A" if "child-A.txt" in intent else "B"
                result = child_plans[which].model_dump(mode="json")
            else:
                result = initial_plan.model_dump(mode="json")
            transport["content"] = json.dumps(result)
    transport["contacts"] = FinalModelCalls()
    request = GeneralTaskCreate(goal_revision=1, idempotency_key="charged-two-specialists",
        input=GeneralTaskInput(goal_ref="goal:fixture", intent="Produce two independent fragments and concatenate their actual outputs locally",
            requested_output=by_id["read_file"].output_schema,
            limits=TaskLimits(max_steps=7, max_inference_calls=12, max_cost_microusd=1000, wall_seconds=600),
            inference_egress_acknowledged=True))
    async with sessions() as db:
        created = await service.create(db, owner, request)
        task_id, revision = created.task.task_id, created.task.task_revision
    assert len(transport["contacts"]) == 1
    async with sessions() as db:
        await service.validate_acceptance(db, owner, task_id, revision)
        await service.repository.action_task(db, owner, task_id,
            WorkBoardActionRequest(action="promote", expected_revision=revision))
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    return sessions, workspace, owner, service, dispatcher, planner, transport, request, task_id


@pytest.mark.asyncio
async def test_charged_parent_consumes_two_distinct_specialists_after_restart(task_runtime, monkeypatch):
    from sqlalchemy import select
    from src.db.models import WorkBoardTask, WorkflowRunState, InferenceCostReservation, ApprovalRequest
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.contracts import GeneralTaskResume
    from src.workflows.general_task_guard import read_manifest
    from src.auth.service import authenticate_session
    from fastapi import FastAPI
    from src.api.approvals import router

    async with genuine_mcp_registry(task_runtime[1], monkeypatch) as (registry, selected, received, protocol):
        sessions, workspace, owner, service, dispatcher, planner, transport, request, task_id = await charged_parent(
            task_runtime, monkeypatch, registry, selected)
        await dispatcher.run_pass()
        async with sessions() as db:
            parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "agent.task.v1",
                WorkflowRunState.parent_job_id.is_(None)))
            assert parent.status == "paused" and parent.failure_reason == "general_task_native_wait"
            parent_id = parent.run_identity
            from src.work_board.dispatcher import _parse_typed_input
            original_group = _parse_typed_input(await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id)))["proposal_group"]
            assert original_group["max_inference_calls"] == 12
            assert original_group["max_cost_microusd"] == 1000
            assert read_manifest(parent).original_deadline_at.isoformat().replace("+00:00", "Z") == original_group["original_deadline_at"]
            child_ids = [(item.task_id, item.idempotency_key) for item in (await db.execute(select(WorkBoardTask).where(
                WorkBoardTask.idempotency_key.like("specialist:%")))).scalars()]
            assert len(child_ids) == 1
        contacts_before_restart = len(transport["contacts"])
        service.stop()
        registry.stop()
        registry.start()
        service = GeneralTaskService(registry, repository=service.repository, planner=planner)
        service.start()
        dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service, jobs=dispatcher.jobs)
        async with sessions() as db:
            replay = await service.create(db, owner, request)
            assert replay.idempotent_replay and replay.task.task_id == task_id
        assert len(transport["contacts"]) == contacts_before_restart
        for _pass in range(16):
            await dispatcher.reconcile_linked_attempts()
            async with sessions() as db:
                task = await service.repository.get_task(db, owner, task_id)
                if task.status.value == "review":
                    break
                parent = await dispatcher.jobs._fetch(db, parent_id)
                manifest = read_manifest(parent)
                if manifest.phase != "approval_wait":
                    continue
                approval = await db.scalar(select(ApprovalRequest).where(ApprovalRequest.status == "pending"))
                assert approval is not None
                child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == parent_id,
                    WorkflowRunState.status == "paused", WorkflowRunState.run_identity.in_(manifest.admitted_invocation_ids)))
                resume = GeneralTaskResume(expected_revision=task.task_revision, expected_plan_revision=manifest.plan_revision,
                    workflow_run_id=parent_id, attempt_id=manifest.attempt_id, fencing_token=manifest.board_fence,
                    workflow_revision=parent.revision, approval_id=approval.id, child_job_id=child.run_identity,
                    expected_manifest_revision=manifest.manifest_revision)
            app = FastAPI()
            @app.middleware("http")
            async def authenticated(request, call_next):
                request.state.operator = await authenticate_session(owner.session_id, touch=False)
                return await call_next(request)
            app.include_router(router, prefix="/api")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
                approved = await client.post("/api/approvals/" + resume.approval_id + "/approve")
                assert approved.status_code == 200, approved.text
            await dispatcher.resume_general_task(owner, task_id, resume)
        else:
            pytest.fail("Original parent did not finish within sixteen bounded dispatch passes")
        first, second = (workspace / "child-A.txt").read_bytes(), (workspace / "child-B.txt").read_bytes()
        assert first != second
        assert (workspace / "parent.txt").read_bytes() == first + second
        assert received == [{"left": first.decode(), "right": second.decode()}]
        async with sessions() as db:
            children = list((await db.execute(select(WorkBoardTask).where(WorkBoardTask.idempotency_key.like("specialist:%")))).scalars())
            assert len(children) == 2 and all(item.status.value == "done" for item in children)
            assert child_ids[0] in [(item.task_id, item.idempotency_key) for item in children]
            rows = list((await db.execute(select(InferenceCostReservation))).scalars())
            entries = [next(item for item in json.loads(row.evidence_json) if item["kind"] == "general_task_group_reservation.v1") for row in rows]
            assert len(rows) == len(transport["contacts"]) == 9
            assert [item["call_ordinal"] for item in entries] == list(range(1, 10))
            assert [item["role"] for item in entries].count("initial_proposal") == 1
            assert [item["role"] for item in entries].count("specialist") == 2
            assert all(item["group"] == original_group for item in entries)
            assert all(row.state == "settled" and row.actual_cost_microusd == 0 for row in rows)
            assert all(row.bound_microusd == 100 for row in rows)
            plan = await service.plan(db, owner, task_id)
            assert plan["native_execution"]["phase"] == "complete"
        before = len(transport["contacts"])
        await dispatcher.reconcile_linked_attempts()
        assert len(transport["contacts"]) == before and len(received) == 1
        assert "physical first" not in json.dumps(transport["contacts"])
        assert "distinct second" not in json.dumps(transport["contacts"])
