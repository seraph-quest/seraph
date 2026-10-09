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


@pytest.mark.asyncio
async def test_actual_empty_inspection_native_encrypted_plan(task_runtime):
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
    finally:
        registry.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("include_calendar", [False, True])
async def test_actual_original_task_prepares_mail_owner(accounting_db, monkeypatch, include_calendar):
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
    await mail._seed(sessions, monkeypatch)
    await mail._configure_model_route(sessions, monkeypatch, workspace)
    operator = mail._operator()
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    meeting_input, meeting_output = None, None
    if include_calendar:
        meeting_input, meeting_output = await _selected_calendar(sessions, monkeypatch, owner, mail.GOAL)
    class SourceBoundary:
        reads = 0
        def __init__(self, _connection, *, contact_observer=None, **_kwargs):
            self.observer = contact_observer
        async def get_message_full(self, _provider_message_id):
            type(self).reads += 1
            if self.observer:
                self.observer()
            return mail._body(type(self).reads)
    calls = []
    def inference_boundary(**kwargs):
        calls.append(kwargs["body"])
        assert len(calls) <= (2 if include_calendar else 1)
        value = ({"subject": "Private reply", "body": "Review this exact reply", "caveats": []}
            if len(calls) == 1 else meeting_output)
        if len(calls) == 2:
            assert "Review this exact reply" not in json.dumps(kwargs["body"])
        content = json.dumps(value)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content=content))]), {
            "choices": [{"message": {"role": "assistant", "content": content}}], "usage": {"cost": "0.000002"}}
    monkeypatch.setattr("src.integrations.gmail_read.GoogleGmailReadonlyAdapter", SourceBoundary)
    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", inference_boundary)
    from src.work_board import communication_preparation
    original_source = communication_preparation._run_source
    async def traced_source(*args, **kwargs):
        try:
            return await original_source(*args, **kwargs)
        except BaseException:
            import traceback
            traceback.print_exc()
            raise
    monkeypatch.setattr(communication_preparation, "_run_source", traced_source)
    registry = ToolRegistry()
    service = GeneralTaskService(registry)
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    registry.communication_dispatcher = dispatcher
    registry.start(); service.start()
    try:
        async with sessions() as db:
            created = await propose(db, owner, service, CommunicationCreate(goal_id=mail.GOAL,
                goal_revision=1, selection=CommunicationSelection(reply_inputs=[{key: value
                    for key, value in mail._reply_body("communication-source").items() if key != "idempotency_key"}],
                    meeting_inputs=[meeting_input] if include_calendar else [],
                    acknowledge_private_review=True), max_cost_microusd=25_000, wall_seconds=120,
                idempotency_key="communication-native-mail"))
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
        assert result["completed"] == 1, (result, diagnostics)
        assert SourceBoundary.reads == 2 and len(calls) == (2 if include_calendar else 1)
        async with sessions() as db:
            plan = await read_plan(db, owner, created.task.task_id, service=service)
        assert len(plan.reply_drafts) == 1
        assert plan.reply_drafts[0].body == "Review this exact reply"
        assert len(plan.source_refs) == (2 if include_calendar else 1) and plan.unresolved_questions == []
        assert len(plan.meeting_preparations) == (1 if include_calendar else 0)
    finally:
        service.stop(); registry.stop()


async def _selected_calendar(sessions, monkeypatch, owner, goal_id):
    """Actual selected source binding; no Calendar Task/Attempt is precreated."""
    import json
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from src.db.models import CalendarReadConsent, GoogleServiceConnection
    from src.integrations.google_calendar import (CalendarEventSnapshot, canonical_event_key,
        event_revision, persist_calendar_event_binding)
    from src.vault import encrypt
    from tests.test_calendar_manual_vertical import _provider_event
    event = _provider_event()
    selected = {"provider_event_id": event["id"], "recurrence_identity": "single",
        "summary": event["summary"], "start": "2026-10-01T09:00:00Z", "end": "2026-10-01T10:00:00Z",
        "location": None, "description": None, "attendees": None, "etag": None,
        "updated": None, "status": "confirmed"}
    revision, list_revision = event_revision(selected), "sha256:" + "b" * 64
    async with sessions() as db:
        connection = GoogleServiceConnection(owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, vault_secret_key="vault:communication-calendar", revision=1, state="active")
        consent = CalendarReadConsent(owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, connection_id=connection.connection_id,
            calendar_id=encrypt("primary"), goal_id=goal_id, goal_revision=1,
            allowed_fields_json=json.dumps(["summary", "start", "end"]), allow_remote_model=True,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1), state="active", revision=1, connection_revision=1)
        db.add(connection); db.add(consent); await db.flush()
        snapshot = CalendarEventSnapshot(event_key=canonical_event_key(owner.principal_id,
            connection.connection_id, "primary", event), event_revision=revision, calendar_list_revision=list_revision,
            provider_event_id=event["id"], recurrence_identity="single", fields={key: selected[key]
                for key in ("summary", "start", "end", "location", "description", "attendees")})
        binding = await persist_calendar_event_binding(db, owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, connection=connection, consent=consent, snapshot=snapshot)
        await db.commit()
    async def vault_get(key):
        assert key == "vault:communication-calendar"
        return json.dumps({"client_id": "calendar-client", "refresh_token": "calendar-refresh"})
    async def final_google_transport(url, **kwargs):
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
    return inputs, output
