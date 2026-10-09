"""Actual capture producer proof for the source-only retirement MAC validator."""
import json
import sys
import asyncio

import pytest
from sqlalchemy import select

from src.db.models import TelegramInboundUpdate, WorkBoardTask
from src.work_board.channel_capture import (
    ChannelCaptureReservationV1, check_document_retirement_origin,
    stage_captured_identity,
    staged_captured_source_identity, check_current_captured_task_source,
)
from src.work_board.repository import BoardError, _begin_sqlite_immediate
from tests.test_telegram_document_ingest import prepare, setup_workspace, authenticated_setup_operator  # noqa: F401

pytestmark = pytest.mark.parametrize("async_db", ["file"], indirect=True)


async def owned_stock_mcp_for_approval(registry, workspace, monkeypatch):
    """Stock MCP client and protocol discovery over an owned ASGI transport."""
    import httpx
    import socket
    from fastapi import FastAPI, Request, Response
    from types import SimpleNamespace
    from src.tools.mcp_manager import MCPManager
    endpoint = 'https://fixture.invalid/mcp'
    input_schema = {'type':'object','properties':{'query':{'type':'string','maxLength':100}},
        'required':['query'],'additionalProperties':False}
    output_schema = {'type':'object','properties':{'value':{'type':'string','maxLength':4096}},
        'required':['value'],'additionalProperties':False}
    app = FastAPI()
    calls = []
    @app.api_route('/mcp', methods=['POST','GET','DELETE'])
    async def protocol(request: Request):
        if request.method != 'POST':
            return Response(status_code=405)
        message = await request.json()
        method = message['method']
        calls.append(method)
        if method == 'notifications/initialized':
            return Response(status_code=202)
        if method == 'initialize':
            result = {'protocolVersion':'2025-06-18','capabilities':{},
                'serverInfo':{'name':'owned-approval-source','version':'1'}}
        elif method == 'tools/list':
            result = {'tools':[{'name':'read_owned','description':'Read an owned local proof',
                'inputSchema':input_schema,'outputSchema':output_schema}]}
        else:
            raise AssertionError('Retired Source must never reach tools/call')
        return Response(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}),
            media_type='application/json')
    def client(headers=None, timeout=None, auth=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=headers, timeout=timeout, auth=auth)
    monkeypatch.setattr('mcp.shared._httpx_utils.create_mcp_http_client', client)
    def dns(host, port, *args, **kwargs):
        assert host == 'fixture.invalid'
        return [(socket.AF_INET,socket.SOCK_STREAM,socket.IPPROTO_TCP,'',('93.184.216.34',port or 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    declaration = {'version':'1','input_schema':input_schema,'output_schema':output_schema,
        'effects':['external_read'],'permissions':['capability_execute'],'verifier':'json_schema.v1','deadline':10}
    path = workspace / 'owned-approval-mcp.json'
    path.write_text(json.dumps({'name':'local','url':endpoint,'task_tools':{'read_owned':declaration}}))
    contribution = SimpleNamespace(extension_id='owned.local',reference=path.name,
        metadata={'trust':'local','name':'local','url':endpoint,'resolved_path':str(path)})
    manager = MCPManager()
    registry.mcp_runtime = manager
    registry.extension_registry = SimpleNamespace(list_contributions=lambda kind:[contribution])
    await asyncio.to_thread(manager.add_server, 'local', endpoint,
        extension_id='owned.local', extension_reference=path.name)
    assert manager.is_connected('local')
    assert 'initialize' in calls and 'tools/list' in calls
    assert any(item.tool_id == 'mcp:local:read_owned' for item in registry.descriptors()), manager.task_tool_block_reason('local')
    return manager, calls


@pytest.mark.asyncio
async def test_actual_original_document_retirement_origin_is_sql_only_and_rejects_mac_drift(
    client, async_db, setup_workspace, monkeypatch,
):
    adapter, _boundary, service, event, owner, raw, contacts = await prepare(
        client, monkeypatch, async_db, "csv")
    try:
        captured = await client.post("/api/telegram/updates", json=event)
        assert captured.status_code == 200, captured.text
        task_id = captured.json()["channel_task_capture"]["task_id"]
        event_key = captured.json()["idempotency_key"]
        assert contacts == ["POST", "GET"]
        identity = stage_captured_identity()
        observing = [False]
        opened = []

        def audit(name, arguments):
            if observing[0] and name == "open":
                opened.append(arguments)

        sys.addaudithook(audit)
        async with async_db() as db:
            await _begin_sqlite_immediate(db)
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
            original_mac = task.channel_capture_origin_json
            original_input = (task.typed_input_ref, task.typed_input_digest, task.input_artifact_id)
            source_event = await db.scalar(select(TelegramInboundUpdate).where(
                TelegramInboundUpdate.idempotency_key == event_key))
            reservation = ChannelCaptureReservationV1.model_validate(
                json.loads(source_event.receipt_json)["channel_task_capture"]["reservation"])
            observing[0] = True
            try:
                await check_document_retirement_origin(db, owner, task, reservation, identity)
            finally:
                observing[0] = False
            assert opened == [], "Source retirement MAC validation performed physical I/O in the writer"
            assert task.channel_capture_origin_json == original_mac
            assert original_input == (task.typed_input_ref, task.typed_input_digest, task.input_artifact_id)
            tampered = json.loads(original_mac)
            tampered["mac"] = "0" * 64
            task.channel_capture_origin_json = json.dumps(tampered)
            await db.flush()
            with pytest.raises(BoardError, match="Original captured source"):
                await check_document_retirement_origin(db, owner, task, reservation, identity)
            await db.rollback()
        async with async_db() as db:
            preserved = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
            assert preserved.channel_capture_origin_json == original_mac
            assert original_input == (preserved.typed_input_ref, preserved.typed_input_digest, preserved.input_artifact_id)
        assert contacts == ["POST", "GET"]
        assert raw
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_actual_capture_current_gate_and_private_plan_retire_without_mutating_task(
    client, async_db, setup_workspace, monkeypatch,
):
    from src.db.models import WorkBoardInputArtifact, InferenceCostReservation
    from src.work_board import input_artifacts as inputs
    from src.work_board import dispatcher as dispatch_module
    from src.api import documents as document_api
    monkeypatch.setattr(document_api, 'get_session', async_db)
    adapter, boundary, service, event, owner, raw, contacts = await prepare(client, monkeypatch, async_db, 'csv')
    try:
        captured = await client.post('/api/telegram/updates', json=event)
        assert captured.status_code == 200, captured.text
        task_id = captured.json()['channel_task_capture']['task_id']
        event_key = captured.json()['idempotency_key']
        from src.api import work_board as board_api
        with staged_captured_source_identity():
            async with async_db() as db:
                current_plan = await board_api.dispatcher.general_tasks.plan(db, owner, task_id)
                assert current_plan['task_id'] == task_id
        with staged_captured_source_identity():
            async with async_db() as db:
                await _begin_sqlite_immediate(db)
                task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                await check_current_captured_task_source(db, owner, task)
                async def inherited():
                    with pytest.raises(BoardError):
                        await check_current_captured_task_source(db, owner, task)
                await asyncio.create_task(inherited())
                original_task = task.model_dump(mode='json')
                assert dispatch_module._parse_typed_input(task)['task_input'].get('document_source') is None
                artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
                original_bytes = inputs._payload_path(artifact).read_bytes()
                source_event = await db.scalar(select(TelegramInboundUpdate).where(TelegramInboundUpdate.idempotency_key == event_key))
                source_id = json.loads(source_event.receipt_json)['channel_task_capture']['document_source']['artifact_id']
                source = await db.get(WorkBoardInputArtifact, source_id)
                revision = source.revision
                original_costs = [row.model_dump(mode='json') for row in (await db.scalars(select(InferenceCostReservation))).all()]
                await db.rollback()
        retired = await client.delete(f'/api/documents/sources/{source_id}', params={'expected_revision': revision})
        assert retired.status_code == 200, retired.text
        parsed = []
        def forbidden_parse(task):
            parsed.append(task.task_id)
            raise AssertionError('Retired capture reached private input parsing')
        monkeypatch.setattr(dispatch_module, '_parse_typed_input', forbidden_parse)
        plan = await client.get(f'/api/work-board/tasks/{task_id}/plan')
        assert plan.status_code == 409 and plan.json()['detail']['code'] == 'channel_document_source_unsealed', plan.text
        accepted = await client.post(f'/api/work-board/tasks/{task_id}/actions', json={'action':'promote','expected_revision':original_task['task_revision']})
        assert accepted.status_code == 409 and accepted.json()['detail']['code'] == 'channel_document_source_unsealed', accepted.text
        assert parsed == []
        metadata = await client.get(f'/api/work-board/tasks/{task_id}')
        assert metadata.status_code == 200, metadata.text
        with staged_captured_source_identity():
            async with async_db() as db:
                await _begin_sqlite_immediate(db)
                task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                with pytest.raises(BoardError) as denied:
                    await check_current_captured_task_source(db, owner, task)
                assert denied.value.code == 'channel_document_source_unsealed'
                assert task.model_dump(mode='json') == original_task
                assert inputs._payload_path(await db.get(WorkBoardInputArtifact, task.input_artifact_id)).read_bytes() == original_bytes
                assert [row.model_dump(mode='json') for row in (await db.scalars(select(InferenceCostReservation))).all()] == original_costs
        assert contacts == ['POST','GET'] and raw
    finally:
        await service.stop()
        await boundary.http.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('retire_before_claim', [True, False, 'after_callback', 'approval_callback', 'pause_before_claim'])
async def test_actual_original_capture_native_claim_checks_source_before_attempt_mutation(
    client, async_db, setup_workspace, monkeypatch, retire_before_claim,
):
    from src.api import documents as document_api, work_board as board_api
    from src.db.models import WorkBoardInputArtifact, InferenceCostReservation
    from src.work_board.contracts import GeneralTaskPlanUpdate, PlanSpec, GENERAL_TASK_NATIVE_CHILD_KIND
    from src.work_board.dispatcher import WorkBoardDispatcher
    monkeypatch.setattr(document_api, 'get_session', async_db)
    adapter, boundary, service, event, owner, raw, contacts = await prepare(client, monkeypatch, async_db, 'csv')
    dispatcher = WorkBoardDispatcher(session_provider=async_db, general_tasks=board_api.dispatcher.general_tasks)
    monkeypatch.setattr(board_api, 'dispatcher', dispatcher)
    manager = None
    try:
        if retire_before_claim == 'approval_callback':
            from config.settings import settings
            from pathlib import Path
            manager, protocol_calls = await owned_stock_mcp_for_approval(
                dispatcher.general_tasks.registry, Path(settings.workspace_dir), monkeypatch)
        captured = await client.post('/api/telegram/updates', json=event)
        assert captured.status_code == 200, captured.text
        task_id = captured.json()['channel_task_capture']['task_id']
        descriptors, _ = dispatcher.general_tasks.snapshot()
        tool_id = 'mcp:local:read_owned' if retire_before_claim == 'approval_callback' else 'read_file'
        read = next(item for item in descriptors if item.tool_id == tool_id)
        from config.settings import settings
        from pathlib import Path
        (Path(settings.workspace_dir) / 'claim-proof.txt').write_text('Actual local readback')
        with staged_captured_source_identity():
            async with async_db() as db:
                task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                task = await dispatcher.general_tasks.update_plan(db, owner, task_id,
                    GeneralTaskPlanUpdate(expected_revision=task.task_revision, expected_plan_revision=0,
                        idempotency_key='original-capture-claim-plan', plan=PlanSpec(revision=1, steps=[{
                            'step_id':'read', 'tool_id':tool_id,
                            'input':({'query':'Explicit approved owned local proof'}
                                if retire_before_claim == 'approval_callback' else {'file_path':'claim-proof.txt'}),
                            'output_contract':read.output_schema}])))
                revision = task.task_revision
        accepted = await client.post(f'/api/work-board/tasks/{task_id}/actions',
            json={'action':'promote', 'expected_revision':revision})
        assert accepted.status_code == 200, accepted.text
        from sqlalchemy import event as sql_event
        from src import workspace as workspace_module
        from src.work_board import input_artifacts as input_module
        async with async_db() as db:
            actual_engine = db.get_bind().engine
        writer = [False]
        def writer_started(connection, cursor, statement, parameters, context, executemany):
            if statement.strip().upper() == 'BEGIN IMMEDIATE':
                writer[0] = True
        def writer_ended(connection):
            writer[0] = False
        sql_event.listen(actual_engine, 'before_cursor_execute', writer_started)
        sql_event.listen(actual_engine, 'commit', writer_ended)
        sql_event.listen(actual_engine, 'rollback', writer_ended)
        actual_identity = workspace_module.canonical_workspace_root_identity
        def physical_identity(*args, **kwargs):
            assert not writer[0], 'Actual Source Root filesystem identity accessed inside canonical writer'
            return actual_identity(*args, **kwargs)
        monkeypatch.setattr(workspace_module, 'canonical_workspace_root_identity', physical_identity)
        actual_envelope_read = input_module.resolve_input_artifact_for_task
        async def envelope_read(*args, **kwargs):
            assert not writer[0], 'Private Source envelope physically resolved inside canonical writer'
            return await actual_envelope_read(*args, **kwargs)
        monkeypatch.setattr(input_module, 'resolve_input_artifact_for_task', envelope_read)
        callback_returns = []
        if retire_before_claim in {'after_callback', 'approval_callback'}:
            from src.native_tools.task_adapters import TaskToolInvocation, TaskToolApprovalRequired
            actual_wait = TaskToolInvocation.wait
            async def invoke_then_retire(invocation, **kwargs):
                error = None
                try:
                    returned = await actual_wait(invocation, **kwargs)
                except TaskToolApprovalRequired as raised:
                    error = raised
                    returned = raised
                async with async_db() as db:
                    event_row = await db.scalar(select(TelegramInboundUpdate).where(
                        TelegramInboundUpdate.idempotency_key == captured.json()['idempotency_key']))
                    source_id = json.loads(event_row.receipt_json)['channel_task_capture']['document_source']['artifact_id']
                    source_revision = (await db.get(WorkBoardInputArtifact, source_id)).revision
                retired = await client.delete(f'/api/documents/sources/{source_id}',
                    params={'expected_revision':source_revision})
                assert retired.status_code == 200, retired.text
                callback_returns.append(returned)
                if error is not None:
                    raise error
                return returned
            monkeypatch.setattr(TaskToolInvocation, 'wait', invoke_then_retire)
        actual_claim = dispatcher.jobs.claim_job
        claims = []
        async def claim_after_retirement(job_id, **kwargs):
            before = await dispatcher.jobs.get_job(job_id)
            if before['job_kind'] != GENERAL_TASK_NATIVE_CHILD_KIND:
                return await actual_claim(job_id, **kwargs)
            assert before['attempt_count'] == 0 and before['lease']['fencing_token'] == 0
            async with async_db() as db:
                task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                original_task = task.model_dump(mode='json')
                event_row = await db.scalar(select(TelegramInboundUpdate).where(
                    TelegramInboundUpdate.idempotency_key == captured.json()['idempotency_key']))
                source_id = json.loads(event_row.receipt_json)['channel_task_capture']['document_source']['artifact_id']
                source_revision = (await db.get(WorkBoardInputArtifact, source_id)).revision
                costs = [row.model_dump(mode='json') for row in (await db.scalars(select(InferenceCostReservation))).all()]
            if retire_before_claim is True or retire_before_claim == 'pause_before_claim':
                retired = await client.delete(f'/api/documents/sources/{source_id}',
                    params={'expected_revision':source_revision})
                assert retired.status_code == 200, retired.text
                with pytest.raises(BoardError) as rejected:
                    await actual_claim(job_id, **kwargs)
                assert rejected.value.code == 'channel_document_source_unsealed'
                assert await dispatcher.jobs.get_job(job_id) == before
                async with async_db() as db:
                    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                    assert task.model_dump(mode='json') == original_task
                    assert [row.model_dump(mode='json') for row in (await db.scalars(select(InferenceCostReservation))).all()] == costs
                claims.append(job_id)
                if retire_before_claim == 'pause_before_claim':
                    cancelled = await dispatcher.jobs.cancel_job(job_id)
                    assert cancelled['status'] == 'cancelled' and cancelled['attempt_count'] == 0
                raise rejected.value
            claimed = await actual_claim(job_id, **kwargs)
            assert claimed['attempt_count'] == 1 and claimed['lease']['fencing_token'] == 1
            claims.append(job_id)
            return claimed
        monkeypatch.setattr(dispatcher.jobs, 'claim_job', claim_after_retirement)
        outcome = await dispatcher.run_pass()
        assert len(claims) == 1, outcome
        if retire_before_claim is not False:
            child_metadata = await client.get(f'/api/jobs/{claims[0]}')
            assert child_metadata.status_code == 200, child_metadata.text
            assert child_metadata.json()['job']['job_id'] == claims[0]
            safe_child = await dispatcher.jobs.get_job(claims[0])
            parent_metadata = await client.get(f"/api/jobs/{safe_child['parent_job_id']}")
            assert parent_metadata.status_code == 200, parent_metadata.text
            assert parent_metadata.json()['job']['job_id'] == safe_child['parent_job_id']
        if retire_before_claim is False:
            assert outcome['completed'] == 1, outcome
        elif retire_before_claim == 'after_callback':
            assert len(callback_returns) == 1, outcome
            child = await dispatcher.jobs.get_job(claims[0])
            assert child['status'] == 'unknown_external_effect', child
            parent = await dispatcher.jobs.get_job(child['parent_job_id'])
            assert any(item.get('payload', {}).get('outcome') == 'returned' for item in parent['checkpoints']), parent
            assert outcome['completed'] == 0, outcome
            original_effect = child['effects'][0]
            observed = await dispatcher.jobs.record_effect(child['job_id'],
                effect_id=original_effect['effect_id'], effect_type=original_effect['effect_type'],
                target_path=original_effect['target_path'], receipt_kind='readback', status='failed',
                details={'reconciliation_owner_id':owner.principal_id, 'verified':False,
                    'reason':'Original callback returned but retired Source forbids adoption'})
            assert observed['status'] == 'unknown_external_effect'
            assert observed['effects'][0]['effect_id'] == original_effect['effect_id']
            forensic = [item for item in observed['effects'] if item.get('original_effect_id') == original_effect['effect_id']]
            assert len(forensic) == 1 and forensic[0]['receipt_kind'] == 'readback'
            assert forensic[0]['details']['readback_observation_only'] is True
        elif retire_before_claim == 'approval_callback':
            assert len(callback_returns) == 1, outcome
            child = await dispatcher.jobs.get_job(claims[0])
            assert child['status'] == 'paused', child
            assert all(item['status'] == 'succeeded' and item.get('details', {}).get('never_contacted') is True
                for item in child['effects']), child
            assert not (Path(settings.workspace_dir) / 'claim-output.txt').exists()
            assert 'tools/call' not in protocol_calls
            async with async_db() as db:
                task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                assert task.block_reason == 'general_task_approval_required'
            metadata = await client.get(f'/api/work-board/tasks/{task_id}')
            assert metadata.status_code == 200, metadata.text
            denied_plan = await client.get(f'/api/work-board/tasks/{task_id}/plan')
            assert denied_plan.status_code == 409, denied_plan.text
            from src.approval.repository import ApprovalRepository
            approved = await ApprovalRepository().resolve(callback_returns[0].approval_id, 'approved')
            assert approved is not None and approved.status == 'approved'
            resume = await client.post(f'/api/work-board/tasks/{task_id}/actions',
                json={'action':'resume','expected_revision':task.task_revision})
            assert resume.status_code == 409, resume.text
            assert resume.json()['detail']['code'] == 'channel_document_source_unsealed', resume.text
        elif retire_before_claim == 'pause_before_claim':
            async with async_db() as db:
                task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                pause_revision = task.task_revision
            paused = await client.post(f'/api/work-board/tasks/{task_id}/actions',
                json={'action':'pause','expected_revision':pause_revision})
            assert paused.status_code == 200, paused.text
            assert paused.json()['task']['block_reason'] == 'general_task_operator_paused'
            resume = await client.post(f'/api/work-board/tasks/{task_id}/actions',
                json={'action':'resume','expected_revision':paused.json()['task']['task_revision']})
            assert resume.status_code == 409, resume.text
            assert resume.json()['detail']['code'] == 'channel_document_source_unsealed', resume.text
        assert contacts == ['POST','GET'] and raw
    finally:
        if manager is not None:
            await asyncio.to_thread(manager.disconnect, 'local')
        await service.stop()
        await boundary.http.aclose()
