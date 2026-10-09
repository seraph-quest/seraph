"""Actual filesystem and stock MCP JSON-RPC vertical slice, with no sockets.

Only final search/MCP HTTPS transports are owned by the fixture. Discovery, MCPClient,
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
from src.memory import task_methods as methods

from tests.test_general_task_planner import accounting_db, forbid_external_inference, prepare
from tests.test_work_board_m6_provider_free_journey import _goal


@pytest.mark.asyncio
async def test_actual_native_dag_saved_as_private_immutable_parameterized_method(accounting_db, monkeypatch):
    from src.auth.service import authenticate_session
    from src.api import work_board as api
    from src.api.approvals import router as approvals_router
    from src.db.models import AuditEvent, MemoryProposal, WorkBoardAttempt, WorkflowRunState
    from src.native_tools.registry import ToolRegistry
    from src.tools.mcp_manager import MCPManager
    from src.work_board.contracts import GENERAL_TASK_NATIVE_CHILD_KIND
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.general_task import GeneralTaskService

    from src.auth import service as auth_service
    original_create = auth_service.create_session
    issued_tokens = []
    async def capture_original_login(*args, **kwargs):
        token, operator = await original_create(*args, **kwargs)
        issued_tokens.append(token)
        return token, operator
    monkeypatch.setattr(auth_service, "create_session", capture_original_login)
    jobs, owner = await prepare(accounting_db, monkeypatch)
    from src.auth.ownership import enroll
    operator = await auth_service.authenticate_token(issued_tokens[0], touch=False)
    await enroll(operator)
    workspace, _engine, factory = accounting_db
    workspace.chmod(0o700)
    sessions = factory.accounting_sessions
    source = "Private local content carried only by actual dependency artifacts.\n"
    (workspace / "original.txt").write_text(source)
    source_sha = hashlib.sha256(source.encode()).hexdigest()
    expected_copy = {"file_path": "copied.txt", "expected_sha256": source_sha}
    endpoint = "https://fixture.invalid/mcp"
    protocol_calls, effects, guarded_calls = [], [], []
    search_requests = []
    search_html = """<html><body><li class="b_algo serp-item"><h2><a href="https://fixture.invalid/source">Fixture source</a></h2>
      <h3><a href="https://fixture.invalid/source">Fixture source</a></h3><p>Bounded research receipt</p><div class="text">Bounded research receipt</div></li>
      <div data-type="web"><a href="https://fixture.invalid/source"><div class="title">Fixture source</div></a>
      <div class="snippet"><div class="content">Bounded research receipt</div></div></div>
      <div class="body"><h2>Fixture source</h2><a href="https://fixture.invalid/source">Bounded research receipt</a></div></body></html>"""
    def search_https(self, method, url, **kwargs):
        assert str(url).startswith("https://")
        search_requests.append({"url": str(url), "params": kwargs.get("params"), "data": kwargs.get("data")})
        if "wikipedia.org" in str(url):
            payload = json.dumps(["ordinary research", ["Fixture source"], ["Bounded research receipt"], ["https://fixture.invalid/source"]])
        else:
            payload = search_html
        return SimpleNamespace(status_code=200, content=payload.encode(), text=payload)
    monkeypatch.setattr("primp.Client.request", search_https)
    original_httpx_request = httpx.Client.request
    def search_httpx_https(self, method, url, **kwargs):
        if "duckduckgo.com" not in str(url):
            return original_httpx_request(self, method, url, **kwargs)
        response = search_https(self, method, url, **kwargs)
        return httpx.Response(200, content=response.content, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.Client, "request", search_httpx_https)
    input_schema = {"type": "object", "properties": {
        "file_path": {"type": "string", "minLength": 1, "maxLength": 1024},
        "expected_sha256": {"type": "string", "maxLength": 64, "pattern": "^[a-f0-9]{64}$"}},
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
            assert arguments == expected_copy
            physical = (workspace / arguments["file_path"]).read_bytes()
            assert hashlib.sha256(physical).hexdigest() == arguments["expected_sha256"]
            output = {"value": physical.decode(), "sha256": arguments["expected_sha256"]}
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
        "verifier": "json_schema.v1", "deadline": 10,
        "procedure_inputs": [{"input_pointer": "/" + name, "kind": "typed_dependency", "schema": schema}
            for name, schema in input_schema["properties"].items()]}
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
        from src.memory import task_methods as methods
        method_owner = methods.current_method
        await method_owner.start()
        service = GeneralTaskService(registry,
            strategy_resolver=methods.TaskMethodStrategyResolver(method_owner)); service.start()
        descriptors, tool_digest = service.snapshot()
        selected = {item.tool_id: item for item in descriptors}
        mcp_id = "mcp:local:verify_copy"
        assert mcp_id in selected, (manager.get_config(), manager.task_tool_block_reason("local"),
            [(tool.name, tool.inputs) for tool in manager.get_server_tools("local")])
        dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
        monkeypatch.setattr(api, "dispatcher", dispatcher)
        monkeypatch.setattr("src.work_board.dispatcher._dispatcher", dispatcher)
        goal = _goal("goal-full-native", "Read, copy and verify through actual local MCP")
        goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
        async with sessions() as db:
            db.add(goal)
        app = FastAPI()
        @app.middleware("http")
        async def operator(request, call_next):
            request.state.operator = await auth_service.authenticate_token(issued_tokens[0], touch=False)
            return await call_next(request)
        app.include_router(api.router, prefix="/api")
        app.include_router(approvals_router, prefix="/api")
        plan = {"revision": 1, "steps": [
            {"step_id": "research", "tool_id": "web_search", "input": {"query": "ordinary research", "max_results": 1},
                "output_contract": selected["web_search"].output_schema},
            {"step_id": "read", "tool_id": "read_file", "depends_on": ["research"], "input": {"file_path": "original.txt"},
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
                assert len(children) == 4 and all(row.job_kind == GENERAL_TASK_NATIVE_CHILD_KIND
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
            assert len(final["native_execution"]["partial_output_refs"]) == 4
            assert search_requests
            verified = [item for item in parent["checkpoints"] if item["checkpoint_id"].startswith("general:verified:")]
            assert {item["checkpoint_id"] for item in verified} == {
                    "general:verified:research", "general:verified:read", "general:verified:write", "general:verified:verify"}
            for checkpoint in verified:
                artifact = checkpoint["payload"]
                path = workspace / artifact["file_path"]
                assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["content_sha256"]
                assert path.stat().st_mode & 0o077 == 0
                if checkpoint["checkpoint_id"] == "general:verified:research":
                    assert "Fixture source" in json.loads(path.read_text())["output"]["content"]
            assert json.loads((workspace / "mcp-physical-readback.json").read_text()) == {"value": source, "sha256": source_sha}
            assert (await client.post(f"/api/work-board/tasks/{task_id}/plan/resume", json=resume_body)).status_code == 409
            await dispatcher.reconcile_linked_attempts()
            assert protocol_calls.count("tools/call") == 1
            # Explicit review first completes the original task, then a
            # separate save action stages original symbolic DAG data only.
            completed = await client.post(f"/api/work-board/tasks/{task_id}/actions", json={
                "action": "complete_review", "expected_revision": detail.json()["task"]["task_revision"],
                "attempt_id": pause["attempt_id"]})
            assert completed.status_code == 200, completed.text
            from src.memory.task_lessons import eligible_procedure_source, save_procedure_method, inspect_task_lesson
            from src.workflows.procedure_contracts import ProcedureSaveRequest
            operator = await auth_service.authenticate_token(issued_tokens[0], touch=False)
            source_info = await eligible_procedure_source(operator, task_id)
            assert source_info["eligible"], source_info["reason_code"]
            wire_fixtures = {"source": source_info, "source_task": completed.json()["task"],
                "goal": goal.model_dump(mode="json")}
            fixture_path = workspace / "procedure-wire-fixtures.json"
            def persist_wire(name, value):
                wire_fixtures[name] = value
                fixture_path.write_text(json.dumps(wire_fixtures, sort_keys=True))
                fixture_path.chmod(0o600)
            persist_wire("source", source_info)
            assert {offer["step_id"] for offer in source_info["parameter_offers"]} == {"research", "read", "write"}
            request = ProcedureSaveRequest(expected_revision=source_info["expected_revision"],
                source_attempt=source_info["source_attempt"], idempotency_key="save-original-dag",
                parameter_selections=[{"offer_id": offer["offer_id"], "name": offer["step_id"] +
                    ("_path" if offer["step_id"] != "research" else "_" + offer["input_pointer"][1:])}
                    for offer in source_info["parameter_offers"]])
            proposed = await save_procedure_method(operator, task_id, request)
            persist_wire("save", proposed)
            assert proposed["status"] == "proposed" and proposed["result"] == "candidate_inert"
            inspected = await inspect_task_lesson(operator, proposed["proposal_id"])
            persist_wire("proposal", inspected)
            assert inspected["source_current"] is True
            candidate = inspected["new_method"]
            assert candidate["schema_version"] == "ProcedurePlan.v3"
            assert len(candidate["plan"]) == 7 and len(candidate["plan"]["steps"]) == 4
            assert candidate["plan"]["steps"][2]["input"]["content"] == plan["steps"][2]["input"]["content"]
            assert candidate["plan"]["steps"][3]["input"] == plan["steps"][3]["input"]
            assert source not in json.dumps(candidate)
            assert (await save_procedure_method(operator, task_id, request))["proposal_id"] == proposed["proposal_id"]
            from src.work_board.repository import BoardError
            with pytest.raises(BoardError) as conflict:
                await save_procedure_method(operator, task_id, request.model_copy(update={"parameter_selections": []}))
            assert conflict.value.code == "procedure_save_idempotency_conflict"
            from src.db.models import Memory
            async with sessions() as db:
                assert not list((await db.execute(select(Memory))).scalars())
                proposal = await db.get(MemoryProposal, proposed["proposal_id"])
                private = workspace / proposal.artifact_ref
                assert private.stat().st_mode & 0o077 == 0
                assert hashlib.sha256(private.read_bytes()).hexdigest() == proposal.artifact_digest
            # Separate explicit canonical review/adoption, then the ordinary
            # C1 publisher/dispatcher executes a fresh exact reviewed pin.
            from src.memory.task_method_invocation import TaskMethodInvoke, invoke_method
            def action(preview, name, key):
                return methods.TaskMethodReview(proposal_id=preview["proposal_id"],
                    expected_revision=preview["expected_revision"], artifact_digest=preview["artifact_digest"],
                    scope_digest=preview["scope_digest"], action=name,
                    reason="Explicit reviewed procedure " + name, idempotency_key=key)
            preview = await methods.inspect_method(operator, proposed["proposal_id"])
            persist_wire("pre_adopt", preview)
            persist_wire("adopt", await methods.review_method(operator, action(preview, "accept", "adopt-source-dag")))
            persist_wire("post_adopt", await methods.inspect_method(operator, proposed["proposal_id"]))
            binding = await method_owner.resolve(owner, goal.id, "work.general-task.v1")
            assert binding.status == "active", binding.reason
            from src.db.task_method_models import TaskMethodActive
            async with sessions() as db:
                pointer = (await db.execute(select(TaskMethodActive))).scalar_one()
                pointer_revision = pointer.revision
            new_source = "Changed ordinary input in a fresh physical native journey.\n"
            (workspace / "new-original.txt").write_text(new_source)
            expected_copy.update(file_path="new-copied.txt",
                expected_sha256=hashlib.sha256(new_source.encode()).hexdigest())
            invoke_body = TaskMethodInvoke(version=binding.version, digest=binding.digest,
                expected_pointer_revision=pointer_revision, goal_id=goal.id, goal_revision=1,
                parameters={"research_query": "changed ordinary research", "research_max_results": 1,
                    "read_path": "new-original.txt", "write_path": "new-copied.txt"},
                limits={"max_inference_calls": 0, "max_cost_microusd": 0},
                inference_egress_acknowledged=False, idempotency_key="fresh-invocation")
            persist_wire("invoke_request", invoke_body.model_dump(mode="json"))
            invoked = await invoke_method(operator, proposed["proposal_id"], invoke_body, service)
            persist_wire("invoke", invoked)
            new_task = invoked["task_id"]
            assert new_task != task_id
            replayed = await invoke_method(operator, proposed["proposal_id"], invoke_body, service)
            assert replayed == {"task_id": new_task, "idempotent_replay": True}
            with pytest.raises(BoardError):
                await invoke_method(operator, proposed["proposal_id"], invoke_body.model_copy(update={
                    "parameters": {**invoke_body.parameters, "read_path": "original.txt", "write_path": "other-copy.txt"}}), service)
            # Corrupt only real first-publication receipts, then restore them:
            # neither missing nor conflicting provenance may unlock a replay.
            from src.db.models import WorkBoardEvent, WorkBoardTask
            from src.memory.task_method_invocation import INVOCATION_EVENT
            async with sessions() as db:
                event = (await db.execute(select(WorkBoardEvent).where(
                    WorkBoardEvent.task_id == new_task, WorkBoardEvent.kind == INVOCATION_EVENT))).scalar_one()
                event_id, original_event = event.event_id, event.metadata_json
                task_count_before = len(list((await db.execute(select(WorkBoardTask))).scalars()))
                event.kind = "test.missing-invocation-binding"
                await db.commit()
            contacts_before = (len(protocol_calls), len(search_requests), len(effects))
            with pytest.raises(BoardError) as missing:
                await invoke_method(operator, proposed["proposal_id"], invoke_body, service)
            assert missing.value.code == "method_invocation_binding_missing"
            async with sessions() as db:
                event = await db.get(WorkBoardEvent, event_id)
                event.kind = INVOCATION_EVENT
                changed_event = json.loads(original_event)
                changed_event["identity"]["request_digest"] = "0" * 64
                event.metadata_json = json.dumps(changed_event)
                await db.commit()
            with pytest.raises(BoardError) as corrupted:
                await invoke_method(operator, proposed["proposal_id"], invoke_body, service)
            assert corrupted.value.code == "method_invocation_idempotency_conflict"
            async with sessions() as db:
                event = await db.get(WorkBoardEvent, event_id)
                event.metadata_json = original_event
                await db.commit()
                stored_task = (await db.execute(select(WorkBoardTask).where(
                    WorkBoardTask.task_id == new_task))).scalar_one()
                original_payload_path = workspace / stored_task.typed_input_ref.removeprefix("workspace-json:")
                original_payload = original_payload_path.read_bytes()
            # A literal envelope alteration is rejected by the original private
            # artifact digest before it can be treated as a new valid request.
            altered_payload = json.loads(original_payload)
            altered_payload["input"]["strategy"]["digest"] = "0" * 64
            original_payload_path.write_text(json.dumps(altered_payload))
            try:
                from src.work_board.dispatcher import TypedInputError
                with pytest.raises(TypedInputError):
                    await invoke_method(operator, proposed["proposal_id"], invoke_body, service)
            finally:
                original_payload_path.write_bytes(original_payload)
            # Even mutually consistent corrupted file/Task/artifact/event
            # hashes cannot replace the independently signed current pin or
            # the complete original caller body. All records are restored.
            from src.db.models import WorkBoardInputArtifact
            from src.memory.procedure_recommendations import canonical, digest
            for corrupted_field, denial in (("pin", "method_invocation_pin_changed"),
                                             ("body", "method_invocation_input_changed")):
                altered_payload = json.loads(original_payload)
                if corrupted_field == "pin":
                    altered_payload["input"]["strategy"]["digest"] = "0" * 64
                else:
                    altered_payload["input"]["task_input"]["intent"] = "Changed original caller intent"
                altered_bytes = canonical(altered_payload).encode()
                altered_sha = hashlib.sha256(altered_bytes).hexdigest()
                original_payload_path.write_bytes(altered_bytes)
                async with sessions() as db:
                    corrupted_task = (await db.execute(select(WorkBoardTask).where(
                        WorkBoardTask.task_id == new_task))).scalar_one()
                    corrupted_artifact = await db.get(WorkBoardInputArtifact, corrupted_task.input_artifact_id)
                    original_sha = corrupted_task.typed_input_digest
                    corrupted_task.typed_input_digest = corrupted_artifact.payload_sha256 = altered_sha
                    corrupted_event = await db.get(WorkBoardEvent, event_id)
                    altered_event = json.loads(original_event)
                    altered_event["typed_input_digest"] = altered_sha
                    altered_event["envelope_digest"] = digest(altered_payload["input"])
                    corrupted_event.metadata_json = canonical(altered_event)
                    await db.commit()
                try:
                    with pytest.raises(BoardError) as changed_original:
                        await invoke_method(operator, proposed["proposal_id"], invoke_body, service)
                    assert changed_original.value.code == denial
                finally:
                    original_payload_path.write_bytes(original_payload)
                    async with sessions() as db:
                        corrupted_task = (await db.execute(select(WorkBoardTask).where(
                            WorkBoardTask.task_id == new_task))).scalar_one()
                        corrupted_artifact = await db.get(WorkBoardInputArtifact, corrupted_task.input_artifact_id)
                        corrupted_task.typed_input_digest = corrupted_artifact.payload_sha256 = original_sha
                        corrupted_event = await db.get(WorkBoardEvent, event_id)
                        corrupted_event.metadata_json = original_event
                        await db.commit()
            assert await invoke_method(operator, proposed["proposal_id"], invoke_body, service) == replayed
            async with sessions() as db:
                assert len(list((await db.execute(select(WorkBoardTask))).scalars())) == task_count_before
            assert (len(protocol_calls), len(search_requests), len(effects)) == contacts_before
            new_detail = await client.get(f"/api/work-board/tasks/{new_task}")
            new_card = new_detail.json()["task"]
            promoted = await client.post(f"/api/work-board/tasks/{new_task}/actions", json={
                "action": "promote", "expected_revision": new_card["task_revision"]})
            assert promoted.status_code == 200, promoted.text
            assert (await dispatcher.run_pass())["blocked"] == 1
            assert (workspace / "new-copied.txt").read_text() == new_source
            waiting_new = (await client.get(f"/api/work-board/tasks/{new_task}/plan")).json()
            new_pause = waiting_new["approval_pause"]
            assert new_pause["approval_id"] != pause["approval_id"]
            assert new_pause["attempt_id"] != pause["attempt_id"]
            assert new_pause["workflow_run_id"] != pause["workflow_run_id"]
            approved_new = await client.post(f"/api/approvals/{new_pause['approval_id']}/approve")
            assert approved_new.status_code == 200, approved_new.text
            new_current = (await client.get(f"/api/work-board/tasks/{new_task}/plan")).json()
            resumed_new = await client.post(f"/api/work-board/tasks/{new_task}/plan/resume", json=native_resume_body(new_current))
            assert resumed_new.status_code == 200, resumed_new.text
            assert resumed_new.json()["task"]["status"] == "review"
            assert protocol_calls.count("tools/call") == 2
            assert json.loads((workspace / "mcp-physical-readback.json").read_text())["value"] == new_source
            completed_new = await client.post(f"/api/work-board/tasks/{new_task}/actions", json={
                "action": "complete_review", "expected_revision": resumed_new.json()["task"]["task_revision"],
                "attempt_id": new_pause["attempt_id"]})
            assert completed_new.status_code == 200, completed_new.text
            persist_wire("rollback_source_task", completed_new.json()["task"])
            source_b = await eligible_procedure_source(operator, new_task)
            assert source_b["eligible"], source_b["reason_code"]
            persist_wire("source_b", source_b)
            request_b = ProcedureSaveRequest(expected_revision=source_b["expected_revision"],
                source_attempt=source_b["source_attempt"], idempotency_key="save-fresh-dag-B",
                parameter_selections=[{"offer_id": offer["offer_id"], "name": offer["step_id"] +
                    ("_path" if offer["step_id"] != "research" else "_" + offer["input_pointer"][1:])}
                    for offer in source_b["parameter_offers"]])
            proposal_b = await save_procedure_method(operator, new_task, request_b)
            persist_wire("save_b", proposal_b)
            preview_b = await methods.inspect_method(operator, proposal_b["proposal_id"])
            await methods.review_method(operator, action(preview_b, "accept", "adopt-fresh-dag-B"))
            binding_b = await method_owner.resolve(owner, goal.id, "work.general-task.v1")
            assert binding_b.version != binding.version and binding_b.digest != binding.digest
            async with sessions() as db:
                pointer_b = (await db.execute(select(TaskMethodActive))).scalar_one()
                revision_b = pointer_b.revision
                from src.db.models import WorkBoardTask
                task_count = len(list((await db.execute(select(WorkBoardTask))).scalars()))
            with pytest.raises(BoardError):
                await invoke_method(operator, proposal_b["proposal_id"], invoke_body.model_copy(update={
                    "version": binding_b.version, "digest": binding_b.digest,
                    "expected_pointer_revision": revision_b}), service)
            async with sessions() as db:
                assert len(list((await db.execute(select(WorkBoardTask))).scalars())) == task_count
            preview_b = await methods.inspect_method(operator, proposal_b["proposal_id"])
            persist_wire("pre_rollback", preview_b)
            persist_wire("rollback", await methods.review_method(operator, action(preview_b, "rollback", "rollback-exact-A")))
            persist_wire("post_rollback_b", await methods.inspect_method(operator, proposal_b["proposal_id"]))
            assert (await method_owner.resolve(owner, goal.id, "work.general-task.v1")) == binding
            persist_wire("post_rollback", await methods.inspect_method(operator, proposed["proposal_id"]))
            async with sessions() as db:
                restored_pointer = (await db.execute(select(TaskMethodActive))).scalar_one()
                restored_revision = restored_pointer.revision
            restored = await invoke_method(operator, proposed["proposal_id"], invoke_body.model_copy(update={
                "expected_pointer_revision": restored_revision, "idempotency_key": "restored-A-new-task"}), service)
            assert restored["task_id"] not in {task_id, new_task}
            # Exercise both existing early-return paths directly against the
            # genuine published Task/envelope and its original caller key.
            from src.memory.task_method_invocation import _InvocationGate
            from src.memory.procedure_recommendations import digest
            from src.work_board.contracts import GeneralTaskCreate, GeneralTaskEnvelope, WorkBoardTaskCreate
            from src.work_board.dispatcher import _parse_typed_input
            restored_request = invoke_body.model_copy(update={
                "expected_pointer_revision": restored_revision, "idempotency_key": "restored-A-new-task"})
            async with sessions() as db:
                stored = (await db.execute(select(WorkBoardTask).where(
                    WorkBoardTask.task_id == restored["task_id"]))).scalar_one()
                original_envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(stored))
                artifact_id = stored.input_artifact_id
                task_count_before = len(list((await db.execute(select(WorkBoardTask))).scalars()))
            def original_gate():
                gate = _InvocationGate(owner, proposed["proposal_id"], restored_request,
                    digest({"owner": owner.model_dump(mode="json"), "proposal_id": proposed["proposal_id"],
                        "request": restored_request.model_dump(mode="json")}), binding=binding)
                gate.stage_envelope(original_envelope)
                return gate
            preview = await methods.inspect_method(operator, proposed["proposal_id"])
            await methods.review_method(operator, action(preview, "disable", "matrix-disable-replays"))
            async with sessions() as db:
                with pytest.raises(BoardError) as service_replay:
                    await service.create(db, owner, GeneralTaskCreate(input=original_envelope.task_input,
                        plan=original_envelope.plan, goal_revision=1, expected_plan_revision=1,
                        idempotency_key=restored_request.idempotency_key), _procedure_invocation=original_gate())
                assert service_replay.value.code == "method_invocation_pointer_changed"
            async with sessions() as db:
                with pytest.raises(BoardError) as repository_replay:
                    await service.repository.create_task(db, owner, WorkBoardTaskCreate(
                        title=original_envelope.task_input.intent[:200], body="General registered-tool task",
                        goal_id=goal.id, goal_revision=1, capability_id="agent.task.v1",
                        input_artifact_id=artifact_id, status="triage", requires_review=True,
                        idempotency_scope="general-task", idempotency_key=restored_request.idempotency_key),
                        _procedure_invocation=original_gate())
                assert repository_replay.value.code == "method_invocation_pointer_changed"
            preview = await methods.inspect_method(operator, proposed["proposal_id"])
            await methods.review_method(operator, action(preview, "activate", "matrix-activate-replays"))
            async with sessions() as db:
                stage_pointer = (await db.execute(select(TaskMethodActive))).scalar_one()
                stage_revision = stage_pointer.revision
            # Real private artifact staging completes first; a separate genuine
            # M5 writer withdraws the pointer before Task publication acquires
            # its writer. This must leave only an inert staged input artifact.
            from src.work_board import input_artifacts
            real_prepare_input = input_artifacts.prepare_input_artifact
            staged_artifacts = []
            staging_action_key = "matrix-disable-staging"
            async def withdraw_after_real_staging(*args, **kwargs):
                staged = await real_prepare_input(*args, **kwargs)
                staged_artifacts.append(staged.artifact_id)
                preview = await methods.inspect_method(operator, proposed["proposal_id"])
                await methods.review_method(operator, action(preview, "disable", staging_action_key))
                return staged
            contacts_before = (len(protocol_calls), len(search_requests), len(effects))
            with monkeypatch.context() as scoped_patch:
                scoped_patch.setattr(input_artifacts, "prepare_input_artifact", withdraw_after_real_staging)
                with pytest.raises(BoardError) as staging_race:
                    await invoke_method(operator, proposed["proposal_id"], invoke_body.model_copy(update={
                        "expected_pointer_revision": stage_revision, "idempotency_key": "pointer-stage-race"}), service)
                assert staging_race.value.code == "method_invocation_pointer_changed"
            from src.db.models import WorkBoardInputArtifact
            async with sessions() as db:
                assert len(list((await db.execute(select(WorkBoardTask))).scalars())) == task_count_before
                assert len(staged_artifacts) == 1
                staged = await db.get(WorkBoardInputArtifact, staged_artifacts[0])
                assert staged.bound_task_id is None and staged.state == "pending"
            assert (len(protocol_calls), len(search_requests), len(effects)) == contacts_before
            preview = await methods.inspect_method(operator, proposed["proposal_id"])
            await methods.review_method(operator, action(preview, "activate", "matrix-activate-staging"))
            staging_action_key = "matrix-disable-ordinary-staging"
            with monkeypatch.context() as scoped_patch:
                scoped_patch.setattr(input_artifacts, "prepare_input_artifact", withdraw_after_real_staging)
                async with sessions() as db:
                    with pytest.raises(BoardError) as ordinary_staging_race:
                        await service.create(db, owner, GeneralTaskCreate(input=original_envelope.task_input,
                            plan=original_envelope.plan, goal_revision=1, expected_plan_revision=1,
                            idempotency_key="ordinary-pointer-stage-race"))
                    assert ordinary_staging_race.value.code == "method_invocation_pointer_changed"
            async with sessions() as db:
                assert len(list((await db.execute(select(WorkBoardTask))).scalars())) == task_count_before
                assert len(staged_artifacts) == 2
                ordinary_staged = await db.get(WorkBoardInputArtifact, staged_artifacts[-1])
                assert ordinary_staged.bound_task_id is None and ordinary_staged.state == "pending"
            assert (len(protocol_calls), len(search_requests), len(effects)) == contacts_before
            preview = await methods.inspect_method(operator, proposed["proposal_id"])
            await methods.review_method(operator, action(preview, "activate", "matrix-activate-ordinary-staging"))
            # Restart the dedicated owner and read exact immutable selection.
            await method_owner.stop()
            await method_owner.start()
            assert (await method_owner.resolve(owner, goal.id, "work.general-task.v1")) == binding
            preview = await methods.inspect_method(operator, proposed["proposal_id"])
            persist_wire("disable", await methods.review_method(operator, action(preview, "disable", "disable-family")))
            persist_wire("post_disable", await methods.inspect_method(operator, proposed["proposal_id"]))
            assert (await method_owner.resolve(owner, goal.id, "work.general-task.v1")).status == "none"
            with pytest.raises(BoardError):
                await invoke_method(operator, proposed["proposal_id"], invoke_body.model_copy(update={"idempotency_key": "disabled-new"}), service)
            preview = await methods.inspect_method(operator, proposed["proposal_id"])
            persist_wire("activate", await methods.review_method(operator, action(preview, "activate", "reactivate-family")))
            persist_wire("post_activate", await methods.inspect_method(operator, proposed["proposal_id"]))
            preview = await methods.inspect_method(operator, proposed["proposal_id"])
            persist_wire("delete", await methods.review_method(operator, action(preview, "delete", "delete-canonical-method")))
            persist_wire("post_delete", await methods.inspect_method(operator, proposed["proposal_id"]))
            with pytest.raises(BoardError):
                await method_owner.validate_pinned(owner, goal.id, binding)
            print(json.dumps({"flow": "auth_manual_plan_accept_read_write_stock_mcp_approval_resume",
                "native_children": 8, "tools_call": 2, "search_https_receipts": len(search_requests),
                "physical_effect_sha256": effects[0]["sha256"],
                "native_no_learning": True, "explicit_review_adoptions": 2,
                "canonical_tombstones": 1, "external_socket_contacts": 0}, sort_keys=True))
    finally:
        if service is not None:
            service.stop()
            await method_owner.stop()
        registry.stop()
        await asyncio.to_thread(manager.disconnect_all)
