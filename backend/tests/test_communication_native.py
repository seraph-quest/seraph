"""Literal current native/private producer checks; no provider transport."""
from dataclasses import replace
import pytest
from src.work_board.contracts import (GeneralTaskCreate, GeneralTaskInput,
    CommunicationSelection, TaskLimits, PlanSpec)
from src.work_board.general_task import digest
from src.native_tools.registry import ToolRegistry
from tests.test_general_task_persistence import task_runtime, isolated_runtime
from tests.test_general_task_native_guard import running_task
from tests.test_inference_accounting import accounting_db
from tests.general_task_method_lifecycle import native_admission_lifecycle
from tests.test_document_build_native_capacity import build_admission_lifecycle


@pytest.mark.asyncio
async def test_actual_empty_inspection_native_encrypted_plan(task_runtime, monkeypatch, native_admission_lifecycle):
    from src.auth.service import authenticate_session
    from src.work_board.general_task_native import admit_native_step, run_native_step
    from src.workflows.mail_reply_draft import read_private_draft
    from src.work_board.communication_contracts import CommunicationPlan
    registry = ToolRegistry()
    registry.communication_dispatcher = object()  # descriptor lifetime only
    registry.start()
    try:
        descriptors = registry.descriptors()
        contract = next(item for item in descriptors if item.tool_id == "communication_prepare")
        selection = CommunicationSelection(acknowledge_private_review=True)
        inputs = {"selection_digest": digest(selection.model_dump(mode="json"))}
        request = GeneralTaskCreate(goal_revision=1, idempotency_key="communication-empty",
            expected_plan_revision=1,
            input=GeneralTaskInput(goal_ref="goal-1", intent="Prepare private communications",
                communication_selection=selection, requested_output=contract.output_schema,
                tool_set_digest=digest([item.model_dump(mode="json") for item in descriptors]),
                limits=TaskLimits(max_steps=1, max_outstanding_children=1,
                    max_inference_calls=0, max_cost_microusd=0, wall_seconds=120)),
            plan=PlanSpec(revision=1, steps=[{"step_id": "prepare", "tool_id": contract.tool_id,
                "input": inputs, "output_contract": contract.output_schema}]))
        _sessions, dispatcher, service, envelope, current = await running_task(task_runtime,
            creation_request=request, registry_override=registry)
        registry.communication_dispatcher = dispatcher
        binding, _admitted = await admit_native_step(dispatcher.jobs, current["job"]["job_id"],
            owner=current["job"]["lease"]["owner"], fence=current["job"]["lease"]["fencing_token"],
            step=envelope.plan.steps[0], descriptor=contract, inputs=inputs)
        operator = await authenticate_session(binding.original_root_id, touch=False)
        output, _artifact, _reference = await run_native_step(service, dispatcher.jobs, binding,
            child_owner="communication-owner", principal=replace(operator.principal,
                session_id=binding.original_root_id, operator_session_id=binding.original_root_id))
        assert output["source_readbacks"] == []
        assert CommunicationPlan.model_validate(read_private_draft(
            output["private_plan_ref"], output["private_plan_digest"])) == CommunicationPlan()
        assert (await dispatcher.jobs.get_job(binding.invocation_id))["status"] == "succeeded"
        from src.work_board.communication_preparation import read_plan
        from src.work_board.contracts import WorkBoardOwner
        from src.work_board.repository import BoardError
        owner = WorkBoardOwner(principal_id=binding.owner_principal_id, session_id=binding.original_root_id)
        async with _sessions() as db:
            assert await read_plan(db, owner, binding.task_id, service=service) == CommunicationPlan()
            with pytest.raises(BoardError):
                await read_plan(db, owner.model_copy(update={"principal_id": "foreign-owner"}),
                    binding.task_id, service=service)
        from src.work_board.communication_preparation import cleanup_plan
        async with _sessions() as db:
            task = await service.repository.get_task(db, owner, binding.task_id)
            revision = task.task_revision
            with pytest.raises(BoardError):
                await cleanup_plan(db, owner, binding.task_id, revision + 1, service=service)
            await db.rollback()
            with pytest.raises(BoardError):
                await cleanup_plan(db, owner.model_copy(update={"principal_id": "foreign-owner"}),
                    binding.task_id, revision, service=service)
            await db.rollback()
            import os
            with monkeypatch.context() as failure:
                def refused_unlink(*args, **kwargs):
                    raise OSError("scripted original file unlink refusal")
                failure.setattr(os, "unlink", refused_unlink)
                assert (await cleanup_plan(db, owner, binding.task_id, revision, service=service))["status"] == "cleanup_unresolved"
            assert CommunicationPlan.model_validate(read_private_draft(
                output["private_plan_ref"], output["private_plan_digest"])) == CommunicationPlan()
            assert (await cleanup_plan(db, owner, binding.task_id, revision, service=service))["status"] == "cleanup_verified"
            assert (await cleanup_plan(db, owner, binding.task_id, revision, service=service))["absent"] is True
            with pytest.raises(BoardError, match="unavailable"):
                await read_plan(db, owner, binding.task_id, service=service)
    finally:
        registry.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("include_calendar,failure_mode", [(False, None), (True, None), (True, "closed"), (True, "unknown"), (True, "insufficient"), (True, "revoked_closed"), (True, "effect_conflict"), (True, "effect_response_loss")])
async def test_actual_original_task_prepares_mail_owner(accounting_db, monkeypatch, include_calendar, failure_mode, build_admission_lifecycle):
    import json
    from types import SimpleNamespace
    from tests import test_mail_reply_vertical as mail
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.communication_contracts import CommunicationCreate
    from src.work_board.communication_preparation import propose, read_plan
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.vault import crypto
    monkeypatch.setattr(crypto, "_fernet", None)
    workspace, _engine, factory = accounting_db
    sessions = factory.accounting_sessions
    import base64
    from src.integrations.gmail_read import _metadata_from_payload, GMAIL_READONLY_SCOPE
    message = {"id": "provider-message-1", "threadId": "provider-thread-1",
        "historyId": "stable-history", "labelIds": ["INBOX", "UNREAD"],
        "snippet": "Private preview", "payload": {"mimeType": "text/plain",
            "headers": [{"name": "Subject", "value": "Architecture review"}],
            "body": {"data": base64.urlsafe_b64encode(b"Private source body stable").decode()}}}
    monkeypatch.setattr(mail, "MESSAGE_REVISION", _metadata_from_payload(message).message_revision)
    from src.auth import service as auth_service
    issued = []
    create_session = auth_service.create_session
    async def capture_session(*args, **kwargs):
        result = await create_session(*args, **kwargs)
        issued.append(result[0])
        return result
    monkeypatch.setattr(auth_service, "create_session", capture_session)
    await mail._seed(sessions, monkeypatch)
    if include_calendar:
        setup_type = mail.OpenRouterSetup
        monkeypatch.setattr(mail, "OpenRouterSetup", lambda **values: setup_type(**{
            **values, "spend_ceiling_microusd": 50_000, "request_cost_bound_microusd": 25_000}))
    await mail._configure_model_route(sessions, monkeypatch, workspace)
    await build_admission_lifecycle.start()
    if include_calendar:
        from src.workflows.job_runtime import DurableJobRepository
        await DurableJobRepository().configure_inference_accounting(50_000)
    operator = mail._operator()
    effect_mode = failure_mode if failure_mode and failure_mode.startswith("effect_") else None
    if effect_mode:
        failure_mode = None
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    meeting_input, meeting_output, calendar_effect = None, None, None
    calendar_requests = []
    if include_calendar:
        meeting_input, meeting_output, calendar_effect = await _selected_calendar(sessions, monkeypatch, owner, mail.GOAL,
            transport_calls=calendar_requests)
        if failure_mode is None:
            calendar_effect = await _calendar_selection_permission(sessions, monkeypatch, operator,
                owner, meeting_input, calendar_effect)
            calendar_effect["mode"] = effect_mode
    source_reads = []
    async def source_transport(url, **kwargs):
        await kwargs["authority_check"]()
        if kwargs["method"] == "POST":
            assert url == "https://oauth2.googleapis.com/token"
            value = {"access_token": "mail-access", "scope": GMAIL_READONLY_SCOPE}
        else:
            assert kwargs["method"] == "GET"
            assert url == "https://gmail.googleapis.com/gmail/v1/users/me/messages/provider-message-1?format=full"
            source_reads.append(url)
            value = message
        return SimpleNamespace(status_code=200, headers={"content-type": "application/json"},
            content=json.dumps(value).encode())
    async def vault_get(key):
        assert key in {"mail-reply-vertical-secret", "vault:communication-calendar"}
        return json.dumps({"client_id": "fixture-client", "refresh_token": "fixture-refresh"})
    calls = []
    import asyncio
    loop = asyncio.get_running_loop()
    async def revoke_during_original_call():
        from src.db.models import MailReadConsent
        async with sessions() as db:
            consent = await db.get(MailReadConsent, mail.CONSENT)
            consent.model_egress_allowed = False
            consent.source_read_allowed = False
            consent.source_revision += 1
            consent.revision += 1
    def inference_boundary(**kwargs):
        calls.append(kwargs["body"])
        assert len(calls) <= (2 if include_calendar else 1)
        if len(calls) == 1 and failure_mode == "unknown":
            raise TimeoutError("scripted response lost after original provider callback")
        if len(calls) == 1 and failure_mode == "revoked_closed":
            asyncio.run_coroutine_threadsafe(revoke_during_original_call(), loop).result(timeout=10)
        value = ({"subject": "Private reply", "body": "Review this exact reply", "caveats": []}
            if len(calls) == 1 else meeting_output)
        if len(calls) == 1 and failure_mode in {"closed", "revoked_closed"}:
            value = {"subject": "", "body": "Invalid model reply", "caveats": []}
        if len(calls) == 2:
            assert "Review this exact reply" not in json.dumps(kwargs["body"])
        content = json.dumps(value)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content=content))]), {
            "choices": [{"message": {"role": "assistant", "content": content}}], "usage": {"cost": "0.000002"}}
    monkeypatch.setattr("src.integrations.gmail_read.request_pinned_https", source_transport)
    monkeypatch.setattr("src.integrations.gmail_read.vault_repository.get", vault_get)
    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", inference_boundary)
    registry = ToolRegistry()
    service = GeneralTaskService(registry)
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    registry.communication_dispatcher = dispatcher
    registry.start(); service.start()
    try:
        from fastapi import FastAPI
        import httpx
        from src.api import work_board as board_api
        from src.auth.middleware import OperatorAuthMiddleware
        monkeypatch.setattr(board_api, "dispatcher", dispatcher)
        app = FastAPI()
        app.add_middleware(OperatorAuthMiddleware)
        app.include_router(board_api.router, prefix="/api")
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:8004", cookies={mail.settings.operator_auth_cookie_name: issued[0]},
            headers={"Origin": "http://127.0.0.1:3001"})
        creation = CommunicationCreate(goal_id=mail.GOAL,
                goal_revision=1, selection=CommunicationSelection(reply_inputs=[{key: value
                    for key, value in mail._reply_body("communication-source").items() if key != "idempotency_key"}],
                    meeting_inputs=[meeting_input] if include_calendar else [],
                    reschedule_inputs=[calendar_effect["proposal"]] if include_calendar and failure_mode is None else [],
                    acknowledge_private_review=True), max_cost_microusd=50_000 if include_calendar and failure_mode != "insufficient" else 25_000, wall_seconds=120,
                idempotency_key="communication-native-mail")
        async with client:
            response = await client.post("/api/work-board/general-tasks/communications", json=creation.model_dump(mode="json"))
            assert response.status_code == 200, response.text
            task_id = response.json()["task"]["task_id"]
        result = await dispatcher.run_pass()
        from src.db.models import WorkflowRunState
        from sqlalchemy import select
        async with sessions() as db:
            diagnostics = [(row.job_kind, row.status, row.failure_reason, row.checkpoint_context_json)
                for row in (await db.execute(select(WorkflowRunState))).scalars()]
        if result["completed"] != 1:
            async with sessions() as db:
                for row in (await db.execute(select(WorkflowRunState))).scalars():
                    print(row.job_kind, row.run_identity, row.root_run_identity, row.parent_job_id,
                        row.parent_fencing_token, row.declared_authority_json)
        if failure_mode == "unknown":
            assert result["completed"] == 0
            assert len(calls) == 1 and len(source_reads) == 1
            assert calendar_requests == []
            from src.db.models import InferenceCostReservation
            from src.work_board.repository import BoardError
            async with sessions() as db:
                reservations = list((await db.execute(select(InferenceCostReservation))).scalars())
                paid = [row for row in reservations if row.runtime_path == "strategist_agent"]
                assert len(paid) == 1 and paid[0].state == "unknown"
                assert paid[0].bound_microusd == 25_000
                child = (await db.execute(select(WorkflowRunState).where(
                    WorkflowRunState.job_kind == "general_task_native_tool_v1"))).scalars().one()
                checkpoint = json.loads(child.checkpoint_context_json)
                assert checkpoint.get("communication_private_plan") is None
                record = checkpoint["communication_preparations"]["0"]
                assert record["producer_closed"] is True and record["phase"] == "unknown"
                assert record["accounting_liability"] == "held"
                assert "1" not in checkpoint["communication_preparations"]
                source_run = (await db.execute(select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == paid[0].job_id))).scalars().one()
                from src.work_board.communication_preparation import assert_preparation_run_current
                with pytest.raises(BoardError) as denied:
                    await assert_preparation_run_current(db, source_run)
                assert denied.value.code == "communication_original_producer_unavailable"
            await dispatcher.run_pass()
            assert len(calls) == 1 and len(source_reads) == 1
            return
        assert result["completed"] == 1, (result, diagnostics)
        expected_calls = 1 if failure_mode == "insufficient" else (2 if include_calendar else 1)
        expected_reads = 1 if failure_mode in {"closed", "revoked_closed"} else 2
        assert len(source_reads) == expected_reads and len(calls) == expected_calls
        async with sessions() as db:
            plan = await read_plan(db, owner, task_id, service=service)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8004", cookies={mail.settings.operator_auth_cookie_name: issued[0]}) as reader:
            private_response = await reader.get(f"/api/work-board/tasks/{task_id}/communications")
            assert private_response.status_code == 200, private_response.text
            assert private_response.json() == {"task_id": task_id,
                "plan": plan.model_dump(mode="json"), "no_learning": True}
            (workspace / "communication-api-private-receipt.json").write_text(private_response.text)
        if failure_mode == "insufficient":
            assert calendar_requests == [] and len(plan.reply_drafts) == 1
            assert plan.meeting_preparations == [] and len(plan.unresolved_questions) == 1
            assert plan.unresolved_questions[0].reason == "general_task_group_cost_limit"
            assert plan.unresolved_questions[0].source_id == meeting_input["event_binding_id"]
            assert not any(row[0] == "calendar_meeting_prep" for row in diagnostics)
            return
        if failure_mode in {"closed", "revoked_closed"}:
            assert plan.reply_drafts == [] and len(plan.meeting_preparations) == 1
            assert len(plan.source_refs) == 1 and len(plan.unresolved_questions) == 1
            assert plan.unresolved_questions[0].source_id == mail.BINDING
            async with sessions() as db:
                child = (await db.execute(select(WorkflowRunState).where(
                    WorkflowRunState.job_kind == "general_task_native_tool_v1"))).scalars().one()
                record = json.loads(child.checkpoint_context_json)["communication_preparations"]["0"]
                assert record["producer_closed"] is True and record["phase"] == "blocked"
                assert record["accounting_liability"] == "known"
                if failure_mode == "revoked_closed":
                    from src.db.models import MailReadConsent, InferenceCostReservation
                    consent = await db.get(MailReadConsent, mail.CONSENT)
                    assert consent.source_read_allowed is False and consent.source_revision == 2
                    paid = list((await db.execute(select(InferenceCostReservation).where(
                        InferenceCostReservation.job_id == record["binding"]["source_job_id"]))).scalars())
                    assert len(paid) == 1 and paid[0].state == "settled" and paid[0].actual_cost_microusd == 2
            return
        assert len(plan.reply_drafts) == 1
        assert plan.reply_drafts[0].body == "Review this exact reply"
        assert len(plan.source_refs) == (2 if include_calendar else 1) and plan.unresolved_questions == []
        assert len(plan.meeting_preparations) == (1 if include_calendar else 0)
        if include_calendar:
            before = (len(source_reads), len(calendar_requests), len(calls))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url="http://127.0.0.1:8004", cookies={mail.settings.operator_auth_cookie_name: issued[0]},
                    headers={"Origin": "http://127.0.0.1:3001"}) as reader:
                denied = await reader.post(f"/api/work-board/tasks/{task_id}/communications/selection", json={
                    "selected_actions": [{"kind": "reply", "source_input_digest": plan.reply_drafts[0].source_ref.source_input_digest,
                        "operation_id": "communication-missing-operation"}],
                    "exact_preview_digests": ["c" * 64], "approval_ids": ["missing-independent-approval"]})
                assert denied.status_code == 409
                assert denied.json() == {"detail": {"code": "communication_action_unavailable",
                    "recovery": "Inspect the affected original action and its exact current approval"}}
            assert before == (len(source_reads), len(calendar_requests), len(calls))
            assert calendar_effect["google"].patch is None
            await _mail_effect_subset(sessions, monkeypatch, operator, owner, service,
                task_id, plan, mail.GOAL)
            await _calendar_effect_subset(sessions, monkeypatch, operator, owner, service,
                task_id, plan, calendar_effect, app, issued[0], mail.settings.operator_auth_cookie_name)
        from src.work_board.repository import BoardError
        source_task_id = plan.source_refs[0].task_id
        async with sessions() as db:
            source_task = await service.repository.get_task(db, owner, source_task_id)
            revision = source_task.task_revision
        operations = [
            lambda db: service.repository.promote_task_ready(db, source_task_id,
                expected_revision=revision, actor_principal_id=owner.principal_id,
                actor_session_id=owner.session_id),
            lambda db: service.repository.claim_ready_task(db, source_task_id,
                expected_revision=revision, lease_owner="generic-restart",
                actor_principal_id=owner.principal_id, actor_session_id=owner.session_id),
            lambda db: service.repository.retry_task(db, owner, source_task_id,
                expected_revision=revision),
        ]
        for operation in operations:
            async with sessions() as db:
                with pytest.raises(BoardError) as denied:
                    await operation(db)
                assert denied.value.code == "communication_original_source_publication_required"
                await db.rollback()
        assert len(source_reads) == 2 and len(calls) == (2 if include_calendar else 1)
        from src.db.models import MailReadConsent
        async with sessions() as db:
            consent = await db.get(MailReadConsent, mail.CONSENT)
            consent.model_egress_allowed = False
            consent.revision += 1
            db.add(consent)
        async with sessions() as db:
            changed_plan = await read_plan(db, owner, task_id, service=service)
        assert changed_plan.reply_drafts == []
        assert len(changed_plan.meeting_preparations) == (1 if include_calendar else 0)
        assert len(changed_plan.unresolved_questions) == 1
        assert changed_plan.unresolved_questions[0].source_id == mail.BINDING
        from src.work_board.communication_preparation import cleanup_plan
        async with sessions() as db:
            task = await service.repository.get_task(db, owner, task_id)
            cleanup_revision = task.task_revision
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1:8004", cookies={mail.settings.operator_auth_cookie_name: issued[0]},
                headers={"Origin": "http://127.0.0.1:3001"}) as reader:
            cleaned = await reader.post(f"/api/work-board/tasks/{task_id}/communications/cleanup",
                json={"expected_task_revision": cleanup_revision})
            assert cleaned.status_code == 200, cleaned.text
            assert cleaned.json() == {"status": "cleanup_verified", "absent": True, "no_learning": True}
            (workspace / "communication-api-cleanup-receipt.json").write_text(cleaned.text)
            unavailable = await reader.get(f"/api/work-board/tasks/{task_id}/communications")
            assert unavailable.status_code == 409 and unavailable.json()["detail"]["code"] == "communication_plan_artifact_unavailable"
        await auth_service.revoke_session(owner.session_id)
        async with sessions() as db:
            with pytest.raises(BoardError) as denied:
                await read_plan(db, owner, task_id, service=service)
            assert denied.value.code == "session_unavailable"
        assert len(source_reads) == 2 and len(calls) == (2 if include_calendar else 1)
    finally:
        service.stop(); registry.stop()


async def _mail_effect_subset(sessions, monkeypatch, operator, owner, service, task_id, plan, goal_id):
    """Use the original source Task and existing exact native effect owners."""
    import base64
    import json
    from email.message import EmailMessage
    from email import policy
    import httpx
    from src.db.models import GoogleServiceConnection
    from src.vault import vault_repository
    from src.integrations import mail_reply_runtime as runtime
    from src.integrations.gmail_send import READ_SERVICE, SEND_SERVICE, SCOPES, digest
    from src.work_board.communication_contracts import ActionBundle
    from src.work_board.communication_preparation import review_bundle
    for kind in (READ_SERVICE, SEND_SERVICE):
        credentials = {"client_id": "fixture-client", "client_secret": None, "refresh_token": "fixture-" + kind}
        key = "communication-effect:" + kind
        await vault_repository.store(key, json.dumps(credentials), owner_principal_id=owner.principal_id)
        async with sessions() as db:
            db.add(GoogleServiceConnection(connection_id=kind, service=kind, state="active", revision=1,
                owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
                vault_secret_key=key, credential_fingerprint=digest(credentials),
                declared_scopes_json=json.dumps(sorted(SCOPES[kind])), setup_idempotency_key=kind))
    incoming = EmailMessage(policy=policy.SMTP)
    incoming["From"] = "author@example.test"
    incoming["To"] = "mailbox@example.test"
    incoming["Subject"] = "Architecture review"
    incoming["Message-ID"] = "<source@example.test>"
    incoming.set_content("Private source body stable")
    raw_source = base64.urlsafe_b64encode(incoming.as_bytes()).decode().rstrip("=")
    contacts, sent = [], []
    async def provider(request):
        contacts.append((request.method, request.url.host, request.url.path))
        if request.url.host == "oauth2.googleapis.com":
            kind = SEND_SERVICE if SEND_SERVICE.encode() in request.content else READ_SERVICE
            return httpx.Response(200, json={"access_token": "fixture-" + kind,
                "scope": " ".join(sorted(SCOPES[kind]))})
        if request.url.host == "openidconnect.googleapis.com":
            return httpx.Response(200, json={"sub": "communication-mailbox", "email": "mailbox@example.test",
                "email_verified": True})
        if request.url.path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "mailbox@example.test"})
        if request.url.path.endswith("/send"):
            assert request.method == "POST" and sent == []
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "sent-message-1", "threadId": "provider-thread-1"})
        assert request.method == "GET"
        if "/threads/" in request.url.path:
            return httpx.Response(200, json={"id": "provider-thread-1", "messages": [
                {"id": "provider-message-1", "threadId": "provider-thread-1"}]})
        if request.url.path.endswith("/sent-message-1"):
            return httpx.Response(200, json={"id": "sent-message-1", "threadId": "provider-thread-1",
                "labelIds": ["SENT"], "raw": sent[0]["raw"]})
        assert request.url.path.endswith("/provider-message-1")
        return httpx.Response(200, json={"id": "provider-message-1", "threadId": "provider-thread-1",
            "raw": raw_source})
    boundary = {"transport": httpx.MockTransport(provider), "resolver": lambda host, port: ["93.184.216.34"]}
    pair = await runtime.verify_pair(operator, request_uuid="communication-pair", goal_id=goal_id,
        goal_revision=1, read_connection_id=READ_SERVICE, send_connection_id=SEND_SERVICE, **boundary)
    assert pair["status"] == "succeeded" and len(sent) == 0
    reference = plan.reply_drafts[0].source_ref
    preview = await runtime.preview(operator, task_id=reference.task_id, read_connection_id=READ_SERVICE,
        send_connection_id=SEND_SERVICE, request_uuid="communication-reply", **boundary)
    assert preview["status"] == "paused" and preview["source_task_id"] == reference.task_id
    assert preview["preview"]["body"] == plan.reply_drafts[0].body
    approved = await runtime.decide(operator, preview["job_id"], decision="approved",
        expected_digest=preview["preview"]["decision_digest"])
    bundle = ActionBundle(selected_actions=[{"kind": "reply", "source_input_digest": reference.source_input_digest,
        "operation_id": preview["job_id"]}], exact_preview_digests=[approved["preview"]["decision_digest"]],
        approval_ids=[approved["preview"]["approval_id"]])
    async with sessions() as db:
        assert await review_bundle(db, owner, operator, task_id, bundle, service=service) == bundle
    assert sent == []
    effect = await runtime.execute(operator, preview["job_id"], **boundary)
    assert effect["status"] == "succeeded" and effect["outcome"] == "verified_in_sender_sent_mailbox"
    assert len(sent) == 1
    before = len(contacts)
    assert await runtime.execute(operator, preview["job_id"], **boundary) == effect
    assert len(contacts) == before and len(sent) == 1


async def _calendar_selection_permission(sessions, monkeypatch, operator, owner, source, context):
    import json
    import uuid
    import httpx
    from datetime import datetime, timedelta, timezone
    from src.api.calendar_reschedule import ConsentCreate
    from src.integrations import calendar_reschedule_runtime as runtime
    from src.integrations.calendar_reschedule_contract import READ_SERVICE, SEND_SERVICE, SCOPES, digest
    from src.db.models import GoogleServiceConnection
    from src.vault import vault_repository
    from tests.test_calendar_reschedule_contract import timed
    for kind in (READ_SERVICE, SEND_SERVICE):
        credentials = {"client_id": "fixture-client", "client_secret": None, "refresh_token": kind}
        key = "communication-effect:" + kind
        await vault_repository.store(key, json.dumps(credentials), owner_principal_id=owner.principal_id)
        async with sessions() as db:
            db.add(GoogleServiceConnection(connection_id=kind, service=kind, state="active", revision=1,
                owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
                vault_secret_key=key, credential_fingerprint=digest(credentials),
                declared_scopes_json=json.dumps(sorted(SCOPES[kind])), setup_idempotency_key=kind))
    boundary = {"transport": httpx.MockTransport(context["google"].handle),
        "resolver": lambda host, port: ["93.184.216.34"]}
    pair = dict(goal_id=source["goal_id"], goal_revision=source["goal_revision"],
        read_connection_id=READ_SERVICE, send_connection_id=SEND_SERVICE,
        event_binding_id=source["event_binding_id"], expected_event_binding_revision=source["expected_event_binding_revision"])
    verified = await runtime.verify_pair(operator, request_uuid=str(uuid.uuid4()), **pair, **boundary)
    assert verified["status"] == "succeeded" and context["google"].patch is None
    body = ConsentCreate(read_connection_id=READ_SERVICE, expected_read_revision=1,
        write_connection_id=SEND_SERVICE, expected_write_revision=1,
        event_binding_id=source["event_binding_id"], expected_event_binding_revision=source["expected_event_binding_revision"],
        goal_id=source["goal_id"], goal_revision=source["goal_revision"],
        acknowledge_identity_and_selected_calendar_read=True, request_uuid=str(uuid.uuid4()),
        expires_at=datetime.now(timezone.utc)+timedelta(minutes=5), acknowledge_owned_event_read=True,
        acknowledge_calendar_list_metadata_read=True, acknowledge_one_conditional_reschedule=True)
    consent = await runtime.create_consent(operator, body=body)
    context.update(boundary=boundary, proposal={"schema_version": 1,
        "consent_id": consent["consent_id"], "expected_consent_revision": consent["revision"],
        "event_binding_id": source["event_binding_id"], "expected_event_binding_revision": source["expected_event_binding_revision"],
        "goal_id": source["goal_id"], "goal_revision": source["goal_revision"],
        "new_start": timed(timedelta(days=2)), "new_end": timed(timedelta(days=2,hours=1))})
    return context


async def _calendar_effect_subset(sessions, monkeypatch, operator, owner, service, task_id,
        plan, context, app, token, cookie_name):
    import uuid
    import httpx
    from src.api import calendar_reschedule as api
    from src.integrations import calendar_reschedule_runtime as runtime
    from src.integrations.calendar_reschedule_contract import READ_SERVICE, SEND_SERVICE
    from src.work_board.communication_contracts import ActionBundle
    from src.work_board.communication_preparation import review_bundle
    app.include_router(api.router, prefix="/api")
    assert len(plan.reschedule_proposals) == 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8004",
            cookies={cookie_name: token}, headers={"Origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/capabilities/calendar/reschedule/tasks",
            json={"request_uuid": str(uuid.uuid4()), "input": plan.reschedule_proposals[0].input})
        assert response.status_code == 200, response.text
        source_task = response.json()["task"]
    preview = await runtime.preview(operator, task_id=source_task["task_id"], read_connection_id=READ_SERVICE,
        send_connection_id=SEND_SERVICE, request_uuid=str(uuid.uuid4()), **context["boundary"])
    assert preview["status"] == "paused" and context["google"].patch is None
    approved = await runtime.decide(operator, preview["job_id"], decision="approved",
        expected_digest=preview["preview"]["decision_digest"])
    bundle = ActionBundle(selected_actions=[{"kind": "reschedule",
        "source_input_digest": plan.reschedule_proposals[0].source_ref.source_input_digest,
        "operation_id": preview["job_id"]}], exact_preview_digests=[approved["preview"]["decision_digest"]],
        approval_ids=[approved["preview"]["approval_id"]])
    async with sessions() as db:
        assert await review_bundle(db, owner, operator, task_id, bundle, service=service) == bundle
    assert context["google"].patch is None
    mode = context.get("mode")
    context["google"].conflict = mode == "effect_conflict"
    context["google"].lose_response = mode == "effect_response_loss"
    if mode == "effect_response_loss":
        with pytest.raises(httpx.ReadError):
            await runtime.execute(operator, preview["job_id"], **context["boundary"])
        original = await runtime.snapshot(operator, preview["job_id"])
        assert original["status"] == "unknown_external_effect" and original["transport_quiescent"] is True
        # Existing owner recovery requires an independent explicit read-only
        # Goal grant; the original Unknown mutation keeps its outstanding slot.
        from datetime import datetime, timedelta, timezone
        from src.db.models import Goal
        from src.goals.contracts import GoalAdmissionBudget
        from src.goals.repository import serialize_admission_budget
        now = datetime.now(timezone.utc)
        grant = GoalAdmissionBudget(reviewed_grant=True, grant_id="communication-readonly-recovery",
            max_outstanding_jobs=1, max_attempts=1, max_runtime_seconds=120, notifications_per_day=0,
            period_started_at=now-timedelta(seconds=1), period_expires_at=now+timedelta(minutes=5), timezone="UTC")
        async with sessions() as db:
            db.add(Goal(id="communication-readonly-recovery", title="Read the original reschedule only",
                status="active", owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
                revision=1, admission_budget_json=serialize_admission_budget(grant)))
        before = len(context["google"].calls)
        observed = await runtime.observe(operator, original_job_id=preview["job_id"],
            expected_original_revision=original["revision"], read_connection_id=READ_SERVICE,
            goal_id="communication-readonly-recovery", goal_revision=1, request_uuid=str(uuid.uuid4()), **context["boundary"])
        assert observed["status"] == "succeeded" and observed["outcome"] == "verified_reschedule_observation"
        assert all(value["method"] != "PATCH" and value["profile"] != "write" for value in context["google"].calls[before:])
        assert (await runtime.snapshot(operator, preview["job_id"]))["status"] == "unknown_external_effect"
        return
    result = await runtime.execute(operator, preview["job_id"], **context["boundary"])
    if mode == "effect_conflict":
        assert result["status"] == "blocked" and result["outcome"] == "precondition_conflict"
        assert context["google"].event["description"] == "Protected concurrent edit retained"
        assert context["google"].event["start"] == preview["preview"]["old_start"]
        return
    assert result["status"] == "succeeded" and result["outcome"] == "verified_reschedule"
    assert context["google"].patch is not None
    before = len(context["google"].calls)
    replay = await runtime.execute(operator, preview["job_id"], **context["boundary"])
    assert replay["status"] == "succeeded" and replay["outcome"] == result["outcome"]
    assert replay["revision"] == result["revision"] and replay["contacts_spent"] == result["contacts_spent"]
    assert len(context["google"].calls) == before


async def _selected_calendar(sessions, monkeypatch, owner, goal_id, *, transport_calls):
    """Actual selected source binding; no Calendar Task/Attempt is precreated."""
    import json
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from src.db.models import CalendarReadConsent, GoogleServiceConnection
    from src.integrations.google_calendar import (CalendarEventSnapshot, canonical_event_key,
        event_revision, persist_calendar_event_binding)
    from src.vault import encrypt
    from tests.test_calendar_reschedule_native import Google
    from tests.test_calendar_reschedule_contract import CALENDAR
    from src.integrations.google_calendar import _selected_event
    google = Google()
    event = google.event
    selected = _selected_event(event, allowed_fields=["summary", "start", "end"])
    revision, list_revision = event_revision(selected), "sha256:" + "b" * 64
    async with sessions() as db:
        connection = GoogleServiceConnection(owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, vault_secret_key="vault:communication-calendar", revision=1,
            state="active", setup_idempotency_key="communication-calendar-fixture")
        consent = CalendarReadConsent(owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, connection_id=connection.connection_id,
            calendar_id=encrypt(CALENDAR["id"]), goal_id=goal_id, goal_revision=1,
            allowed_fields_json=json.dumps(["summary", "start", "end"]), allow_remote_model=True,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1), state="active", revision=1, connection_revision=1)
        db.add(connection); db.add(consent); await db.flush()
        snapshot = CalendarEventSnapshot(event_key=canonical_event_key(owner.principal_id,
            connection.connection_id, CALENDAR["id"], event), event_revision=revision, calendar_list_revision=list_revision,
            provider_event_id=event["id"], recurrence_identity="single", fields={key: selected[key]
                for key in ("summary", "start", "end", "location", "description", "attendees")})
        binding = await persist_calendar_event_binding(db, owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, connection=connection, consent=consent, snapshot=snapshot)
        await db.commit()
    async def vault_get(key):
        assert key == "vault:communication-calendar"
        return json.dumps({"client_id": "calendar-client", "refresh_token": "calendar-refresh"})
    async def final_google_transport(url, **kwargs):
        if kwargs.get("authority_check") is not None:
            await kwargs["authority_check"]()
        transport_calls.append((kwargs.get("method"), str(url)))
        assert str(url).startswith("https://")
        if kwargs.get("method") == "POST":
            return SimpleNamespace(status_code=200, headers={"content-type": "application/json"},
                content=b'{"access_token":"calendar-access"}')
        return SimpleNamespace(status_code=200, headers={"content-type": "application/json"},
            content=json.dumps(event).encode())
    monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", final_google_transport)
    inputs = {"schema_version": 1, "consent_id": consent.consent_id, "event_binding_id": binding.event_binding_id,
        "expected_event_binding_revision": binding.revision, "expected_consent_revision": consent.revision,
        "expected_connection_revision": connection.revision, "event_revision": revision,
        "calendar_list_revision": list_revision, "goal_id": goal_id, "goal_revision": 1,
        "purpose": "Prepare this selected meeting"}
    output = {"schema_version": 1, "event_key": snapshot.event_key, "event_revision": revision,
        "summary": "Review the architecture", "agenda": ["Decisions"], "questions": [],
        "risks": [], "preparation_steps": ["Read the design"]}
    return inputs, output, {"google": google}
