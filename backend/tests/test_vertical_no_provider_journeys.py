"""ADR-035 retired-world vertical journeys (#1156).

OpenRouter is retired by policy and no NEAR TEE slot is configured, so no
canonical route has an active provider. Each journey drives one vertical
family through its real owner up to its model step and proves:

- a terminal blocked state;
- a user-visible ``openrouter_retired`` (retained historical OpenRouter
  configuration) or ``no_active_provider`` (fresh install) reason;
- no orphaned durable job, Work Board lease or card;
- an empty inference accounting snapshot;
- no provider call: every provider transport is a failing spy.

No marker re-enables OpenRouter here. Retained configuration is written
inside an explicit, scoped "historical" block that models an install
configured before retirement; the journey itself always runs retired.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from config.settings import settings
from src.model_fabric import retirement
from src.model_fabric.retirement import NO_ACTIVE_PROVIDER_REASON, OPENROUTER_RETIRED_REASON
from tests.test_inference_accounting import accounting_db  # noqa: F401
from tests.test_work_board_m6_provider_free_journey import isolated_runtime  # noqa: F401
from tests.test_general_task_persistence import task_runtime  # noqa: F401
from tests.general_task_method_lifecycle import native_admission_lifecycle  # noqa: F401

RETIRED_REASONS = {True: OPENROUTER_RETIRED_REASON, False: NO_ACTIVE_PROVIDER_REASON}
_ACTIVE_RUN_STATES = {"accepted", "queued", "running"}


@contextmanager
def historical_openrouter():
    """Scope for writing configuration as a pre-retirement install had it."""
    with patch.object(retirement, "_OPENROUTER_RETIRED", False):
        yield
    assert retirement.openrouter_retired() is True


@contextmanager
def no_provider_contact():
    """Every provider transport fails the test and is recorded.

    The in-process ASGI client is not a provider transport and stays live.
    """
    calls: list[str] = []

    def spy(name):
        def forbidden(*_args, **_kwargs):
            calls.append(name)
            raise AssertionError(f"provider transport contacted: {name}")
        return forbidden

    async def async_spy_request(self, request):
        calls.append(f"httpx:{request.url.host}")
        raise AssertionError(f"provider transport contacted: {request.url}")

    with (
        patch.object(httpx.AsyncHTTPTransport, "handle_async_request", async_spy_request),
        patch.object(httpx.HTTPTransport, "handle_request", spy("httpx.HTTPTransport")),
        patch("litellm.completion", spy("litellm.completion")),
        patch("litellm.acompletion", spy("litellm.acompletion")),
        patch("src.llm_runtime._governed_openai_chat_completion", spy("governed_openai_chat")),
        patch("src.llm_runtime._governed_bounded_chat_transfer", spy("governed_bounded_transfer")),
        patch("src.model_fabric.near_tee.near_tee_transport", spy("near_tee_transport")),
    ):
        yield calls
    assert calls == []


async def assert_no_orphans_or_accounting(sessions, jobs=None):
    """No live durable job, open Work Board attempt or accounting row remains."""
    from src.db.models import InferenceCostReservation, WorkBoardAttempt, WorkflowRunState
    from src.workflows.job_runtime import DurableJobRepository
    async with sessions() as db:
        runs = list((await db.execute(select(WorkflowRunState))).scalars())
        open_attempts = list((await db.execute(select(WorkBoardAttempt).where(
            WorkBoardAttempt.ended_at.is_(None)))).scalars())
        reservations = list((await db.execute(select(InferenceCostReservation))).scalars())
    assert [run.status for run in runs if run.status in _ACTIVE_RUN_STATES] == []
    assert open_attempts == [] and reservations == []
    snapshot = await (jobs or DurableJobRepository()).inference_accounting_snapshot()
    assert snapshot["operations"] == []
    assert snapshot.get("unknown_microusd", 0) in (0, None)
    return runs


def _assert_retired_world(retained):
    from src.model_fabric.configuration import effective_workload_policy, read_model_fabric_configuration
    assert retirement.openrouter_retired() is True
    assert (read_model_fabric_configuration().openrouter_setup is not None) is retained
    assert read_model_fabric_configuration().near_tee is None
    for route in ("strategist_agent", "general_task_planner", "readonly_research_child"):
        assert effective_workload_policy(route).allowed_provider_kinds == ()
        assert retirement.inactive_route_reason(route) == RETIRED_REASONS[retained]


async def _operator_session(monkeypatch, secret="vertical-no-provider-auth"):
    from src.auth.service import create_session
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", secret)
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    _token, operator = await create_session()
    return operator


# ── Work Board card proposal (triage) ────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("retained", [True, False], ids=["retained_openrouter", "fresh_install"])
async def test_work_board_card_proposal_blocks_with_retired_reason(accounting_db, monkeypatch, retained):
    from src.db.models import WorkBoardProposal, WorkBoardStatus, WorkBoardTask
    from src.work_board.contracts import WorkBoardOwner, WorkBoardProposalRequest, WorkBoardTaskCreate
    from src.work_board.repository import WorkBoardRepository
    from src.work_board import triage as triage_service
    from tests.test_general_task_planner import prepare
    from tests.test_work_board_m6_provider_free_journey import _goal

    _root, _engine, factory = accounting_db
    sessions = factory.accounting_sessions
    monkeypatch.setattr("src.work_board.triage.get_session", sessions)
    if retained:
        with historical_openrouter():
            _jobs, owner = await prepare(accounting_db, monkeypatch)
        operator = await _authenticated(owner)
    else:
        operator = await _operator_session(monkeypatch)
        owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    _assert_retired_world(retained)
    goal = _goal("goal-card-no-provider", "Specify a card without a provider")
    goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
    async with sessions() as db:
        db.add(goal)
    async with sessions() as db:
        created = await WorkBoardRepository().create_task(db, owner, WorkBoardTaskCreate(
            title="Card awaiting a specification", goal_id=goal.id, goal_revision=1,
            status=WorkBoardStatus.triage, idempotency_key="card-no-provider"))
        task = created.task
    with no_provider_contact():
        result = await triage_service.create_proposal(owner, task.task_id, kind="specify",
            request=WorkBoardProposalRequest(expected_revision=task.task_revision, idempotency_key="no-provider"),
            operator=operator)
        replay = await triage_service.create_proposal(owner, task.task_id, kind="specify",
            request=WorkBoardProposalRequest(expected_revision=task.task_revision, idempotency_key="no-provider"),
            operator=operator)
    reason = RETIRED_REASONS[retained]
    assert result["status"] == "blocked" and result["blocked_reason"] == reason, result
    assert result["provider_contact_state"] == "not_started"
    assert replay["status"] == "blocked" and replay["blocked_reason"] == reason
    async with sessions() as db:
        proposals = list((await db.execute(select(WorkBoardProposal))).scalars())
        cards = list((await db.execute(select(WorkBoardTask))).scalars())
    assert len(proposals) == 1 and proposals[0].provider_contact_started is False
    # The proposal's admission job identity is reserved but never admitted (runs == [] below).
    assert proposals[0].status == "blocked"
    # The parent card is unchanged and no child card was created.
    assert [(card.task_id, card.status) for card in cards] == [(task.task_id, WorkBoardStatus.triage)]
    runs = await assert_no_orphans_or_accounting(sessions)
    assert runs == []


async def _authenticated(owner):
    from src.auth.service import authenticate_session
    return await authenticate_session(owner.session_id, touch=False)


# ── General task and repository work (shared governed planner) ───


@asynccontextmanager
async def _general_task_client(accounting_db, monkeypatch, owner):
    from src.auth.service import authenticate_session
    from src.api import work_board as module
    from src.native_tools.registry import ToolRegistry
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.general_task_planner import GeneralTaskPlanner
    sessions = accounting_db[2].accounting_sessions
    registry = ToolRegistry()
    registry.start()
    service = GeneralTaskService(registry, planner=GeneralTaskPlanner())
    service.start()
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    monkeypatch.setattr(module, "dispatcher", dispatcher)
    app = FastAPI()

    @app.middleware("http")
    async def current_operator(request, call_next):
        request.state.operator = await authenticate_session(owner.session_id, touch=False)
        return await call_next(request)
    app.include_router(module.router, prefix="/api")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            yield client, registry, dispatcher
    finally:
        service.stop()
        registry.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("retained", [True, False], ids=["retained_openrouter", "fresh_install"])
async def test_general_task_planning_blocks_with_retired_reason(accounting_db, monkeypatch, retained):
    from src.db.models import WorkBoardTask
    from src.work_board.contracts import WorkBoardOwner
    from tests.test_general_task_planner import prepare
    from tests.test_work_board_m6_provider_free_journey import _goal

    sessions = accounting_db[2].accounting_sessions
    if retained:
        with historical_openrouter():
            jobs, owner = await prepare(accounting_db, monkeypatch)
    else:
        operator = await _operator_session(monkeypatch)
        jobs, owner = None, WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    _assert_retired_world(retained)
    goal = _goal("goal-general-no-provider", "Read local notes through one governed plan")
    goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
    async with sessions() as db:
        db.add(goal)
    reason = RETIRED_REASONS[retained]
    async with _general_task_client(accounting_db, monkeypatch, owner) as (client, registry, dispatcher):
        descriptor = next(item for item in registry.descriptors() if item.tool_id == "read_file")
        body = {"goal_revision": 1, "idempotency_key": "general-no-provider",
            "input": {"goal_ref": goal.id, "intent": "Read notes.txt and return its content",
                "requested_output": descriptor.output_schema, "inference_egress_acknowledged": True,
                "limits": {"max_cost_microusd": 1000}}}
        with no_provider_contact():
            for _ in range(2):  # a retry gets the same block, never a different path
                created = await client.post("/api/work-board/general-tasks", json=body)
                assert created.status_code == 409, created.text
                detail = created.json()["detail"]
                assert detail["code"] == reason, detail
                assert detail["message"] == retirement.inactive_route_label(reason)
            assert (await dispatcher.run_pass())["claimed"] == 0
    async with sessions() as db:
        assert list((await db.execute(select(WorkBoardTask))).scalars()) == []
    assert await assert_no_orphans_or_accounting(sessions, jobs) == []


@pytest.mark.asyncio
async def test_repository_work_intent_blocks_with_retired_reason(accounting_db, monkeypatch):
    """Natural-language repository work plans through the same governed
    planner; with no active provider it blocks before any card, job or lease."""
    from src.db.models import WorkBoardTask
    from src.work_board.contracts import GeneralTaskCreate
    from src.work_board.repository import BoardError
    from tests.test_general_task_planner import prepare
    from tests.test_work_board_m6_provider_free_journey import _goal

    sessions = accounting_db[2].accounting_sessions
    with historical_openrouter():
        jobs, owner = await prepare(accounting_db, monkeypatch)
    _assert_retired_world(True)
    goal = _goal("goal-repository-no-provider", "Fix the failing parser test")
    goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
    async with sessions() as db:
        db.add(goal)
    from src.native_tools.registry import ToolRegistry
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.general_task_planner import GeneralTaskPlanner
    registry = ToolRegistry()
    registry.start()
    service = GeneralTaskService(registry, planner=GeneralTaskPlanner())
    service.start()
    try:
        descriptors, tool_digest = service.snapshot()
        repo_tools = [item for item in descriptors if item.tool_id.startswith("repo") or "repository" in item.tool_id]
        descriptor = repo_tools[0] if repo_tools else descriptors[0]
        request = GeneralTaskCreate.model_validate({"goal_revision": 1, "idempotency_key": "repository-no-provider",
            "input": {"goal_ref": goal.id, "intent": "Fix the bug in parser.py so tests pass",
                "requested_output": descriptor.output_schema, "tool_set_digest": tool_digest,
                "inference_egress_acknowledged": True, "limits": {"max_cost_microusd": 1000}}})
        with no_provider_contact():
            with pytest.raises(BoardError) as blocked:
                async with sessions() as db:
                    await service.create(db, owner, request)
    finally:
        service.stop()
        registry.stop()
    assert blocked.value.code == OPENROUTER_RETIRED_REASON
    assert blocked.value.extra.get("reason_code") == OPENROUTER_RETIRED_REASON
    async with sessions() as db:
        assert list((await db.execute(select(WorkBoardTask))).scalars()) == []
    assert await assert_no_orphans_or_accounting(sessions, jobs) == []


# ── Mail reply (durable Work Board capability) ───────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("retained", [True, False], ids=["retained_openrouter", "fresh_install"])
async def test_mail_reply_blocks_with_retired_reason(accounting_db, monkeypatch, retained):
    from src.db.models import WorkBoardStatus, WorkBoardTask
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.repository import WorkBoardRepository
    from src.workflows.job_runtime import DurableJobRepository
    from tests import test_mail_reply_vertical as mail

    monkeypatch.setattr(mail.vault_crypto, "_fernet", None)
    tmp_path, _engine, factory = accounting_db
    sessions = factory.accounting_sessions
    await mail._seed(sessions, monkeypatch)
    if retained:
        with historical_openrouter():
            await mail._configure_model_route(sessions, monkeypatch, tmp_path)
    _assert_retired_world(retained)
    created = await mail.mail_api.create_reply_task(mail._request(mail._reply_body("reply-no-provider")))
    assert created.status_code == 201, created.body
    task_id = json.loads(created.body)["task_id"]

    class SourceAdapter:
        reads = 0

        def __init__(self, *_args, **_kwargs):
            pass

        async def get_message_full(self, _provider_message_id):
            type(self).reads += 1
            raise AssertionError("no source read may happen without an active model route")

    monkeypatch.setattr("src.integrations.gmail_read.GoogleGmailReadonlyAdapter", SourceAdapter)
    dispatcher = WorkBoardDispatcher(repository=WorkBoardRepository(), jobs=DurableJobRepository(),
        session_provider=sessions, runner_id="service:work-board")
    reason = RETIRED_REASONS[retained]
    with no_provider_contact():
        receipts = [await dispatcher.run_pass() for _ in range(2)]
    assert all(receipt.get("admitted", 0) == 0 and receipt.get("completed", 0) == 0 for receipt in receipts), receipts
    assert SourceAdapter.reads == 0
    async with sessions() as db:
        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))).scalar_one()
    assert task.status is WorkBoardStatus.blocked, task.status
    assert task.block_kind == "capability"
    assert task.block_reason == retirement.inactive_route_label(reason)
    assert f"({reason})" in task.block_reason
    assert await assert_no_orphans_or_accounting(sessions) == []


@pytest.mark.asyncio
async def test_mail_reply_claimed_execution_names_retired_reason(accounting_db, monkeypatch):
    """Defense in depth: an already-claimed Mail attempt (claimed before the
    route went away) is closed at its model gate with the stable reason."""
    from src.db.models import WorkBoardStatus, WorkBoardTask
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.workflows.job_runtime import DurableJobRepository
    from tests import test_mail_reply_vertical as mail

    monkeypatch.setattr(mail.vault_crypto, "_fernet", None)
    _tmp, _engine, factory = accounting_db
    sessions = factory.accounting_sessions
    await mail._seed(sessions, monkeypatch)
    _assert_retired_world(False)
    repository, owner, claim, inputs, created = await mail._create_claim(sessions, monkeypatch, "reply-claimed-no-provider")
    dispatcher = WorkBoardDispatcher(repository=repository, jobs=DurableJobRepository(), session_provider=sessions)
    with no_provider_contact():
        result = await dispatcher._admit_execute_direct(claim, inputs, runtime_seconds=120)
    assert result["blocked"] is True and result["admitted"] is False and result["completed"] is False, result
    async with sessions() as db:
        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == created["task_id"]))).scalar_one()
    assert task.status is WorkBoardStatus.blocked and task.block_kind == "capability"
    assert task.block_reason == retirement.inactive_route_label(NO_ACTIVE_PROVIDER_REASON)
    assert await assert_no_orphans_or_accounting(sessions) == []


# ── Guardian opportunity assessment ──────────────────────────────


@pytest.mark.asyncio
async def test_guardian_opportunity_assessment_blocks_with_retired_reason(isolated_runtime):
    """A published opportunity reaches its assessment model step and stops
    with the stable cause: row and native job blocked, nothing contacted."""
    from src.db.models import GuardianOpportunity, WorkflowRunState
    from src.guardian import opportunity_runtime
    from src.workflows.job_runtime import durable_job_repository
    from tests.test_guardian_opportunity_policy import publish_source

    sessions, _goal, _watch, _request, row, _packet = await publish_source(isolated_runtime)
    _assert_retired_world(False)
    assert row.status == "queued" and row.job_id is None
    with no_provider_contact():
        await opportunity_runtime.execute_assessment(row.id)
        # A blocked assessment is terminal: re-execution is refused, never replayed.
        from src.guardian.opportunity_contracts import OpportunityError
        with pytest.raises(OpportunityError, match="opportunity_not_pending"):
            await opportunity_runtime.execute_assessment(row.id)
        assert (await opportunity_runtime.run_opportunity_tick())["started"] == 0
    async with sessions() as db:
        final = await db.get(GuardianOpportunity, row.id)
        runs = list((await db.execute(select(WorkflowRunState))).scalars())
    assert final.status == "blocked" and final.reason_code == NO_ACTIVE_PROVIDER_REASON, (final.status, final.reason_code)
    native = await durable_job_repository.get_job(final.job_id)
    assert native["status"] == "blocked" and native["failure_reason"] == NO_ACTIVE_PROVIDER_REASON
    assert native["lease"]["owner"] is None
    # Exactly one assessment job (the source-watch runs are the publication setup).
    assert [run.run_identity for run in runs if run.run_identity.startswith("opportunity:")] == [final.job_id]
    await assert_no_orphans_or_accounting(sessions)


# ── Calendar meeting prep and research (durable Work Board capabilities) ──


async def _todo_capability_task(sessions, owner, *, capability, goal_id, typed_input, key):
    from src.db.models import WorkBoardStatus
    from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardTaskCreate
    from src.work_board.dispatcher import registered_executor_id
    from src.work_board.input_artifacts import prepare_input_artifact
    from src.work_board.repository import WorkBoardRepository
    async with sessions() as db:
        artifact = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(schema_version=1,
            capability_id=capability, goal_id=goal_id, goal_revision=1, input=typed_input,
            idempotency_key=key + "-input"))
    async with sessions() as db:
        mutation = await WorkBoardRepository().create_task(db, owner, WorkBoardTaskCreate(
            title="Capability awaiting its model step", goal_id=goal_id, goal_revision=1,
            status=WorkBoardStatus.todo, capability_id=capability, input_artifact_id=artifact.artifact_id,
            executor_id=registered_executor_id(capability), idempotency_key=key))
        await db.commit()
    return mutation.task.task_id


async def _seed_calendar(sessions, owner):
    from src.db.models import CalendarReadConsent, Goal, GoogleServiceConnection
    from src.integrations.google_calendar import (CalendarEventSnapshot, canonical_event_key, event_revision,
        persist_calendar_event_binding)
    from src.vault import encrypt
    from tests.test_calendar_manual_vertical import _provider_event
    event = _provider_event()
    selected = {"provider_event_id": event["id"], "recurrence_identity": "single", "summary": event["summary"],
        "start": "2026-10-01T09:00:00Z", "end": "2026-10-01T10:00:00Z", "location": None, "description": None,
        "attendees": None, "etag": None, "updated": None, "status": "confirmed"}
    revision, list_revision = event_revision(selected), "sha256:" + "b" * 64
    async with sessions() as db:
        goal = Goal(id="goal-calendar-no-provider", title="Calendar without a provider", status="active",
            revision=1, owner_principal_id=owner.principal_id, owner_session_id=owner.session_id)
        connection = GoogleServiceConnection(owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
            vault_secret_key="vault:calendar-no-provider", revision=1, state="active")
        consent = CalendarReadConsent(owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
            connection_id=connection.connection_id, calendar_id=encrypt("primary"), goal_id=goal.id, goal_revision=1,
            allowed_fields_json=json.dumps(["summary", "start", "end"]), allow_remote_model=True,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1), state="active", revision=1,
            connection_revision=1)
        db.add_all([goal, connection, consent])
        await db.flush()
        snapshot = CalendarEventSnapshot(event_key=canonical_event_key(owner.principal_id, connection.connection_id,
            "primary", event), event_revision=revision, calendar_list_revision=list_revision,
            provider_event_id=str(event["id"]), recurrence_identity="single", fields={"summary": event["summary"],
                "start": selected["start"], "end": selected["end"], "location": None, "description": None,
                "attendees": None})
        binding = await persist_calendar_event_binding(db, owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, connection=connection, consent=consent, snapshot=snapshot)
        await db.commit()
    return goal.id, {"schema_version": 1, "consent_id": consent.consent_id,
        "event_binding_id": binding.event_binding_id, "expected_event_binding_revision": binding.revision,
        "expected_consent_revision": consent.revision, "expected_connection_revision": connection.revision,
        "event_revision": revision, "calendar_list_revision": list_revision, "goal_id": goal.id,
        "goal_revision": 1, "purpose": "Prepare the architecture review"}


async def _seed_research(sessions, owner):
    from src.db.models import Goal
    from src.goals.contracts import GoalAdmissionBudget
    from src.goals.repository import serialize_admission_budget
    async with sessions() as db:
        db.add(Goal(id="goal-research-no-provider", title="Finite public evidence research", status="active",
            revision=1, owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
            admission_budget_json=serialize_admission_budget(GoalAdmissionBudget(reviewed_grant=True,
                grant_id="research-no-provider", max_outstanding_jobs=1, max_attempts=1, max_runtime_seconds=300))))
    return "goal-research-no-provider", {"schema_version": 1,
        "question": "What does the selected software license state?",
        "perspectives": [{"instruction": "Summarize supplied evidence", "source_slots": [0]},
            {"instruction": "Describe uncertainty", "source_slots": [0]}],
        "sources": [{"kind": "public_https_text", "url": "https://raw.githubusercontent.com/python/cpython/v3.12.8/LICENSE",
            "first_line": 3, "last_line": 8}], "source_egress_acknowledged": True, "no_learning": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["calendar.meeting-prep.v1", "work.research-dossier.v1"])
@pytest.mark.parametrize("retained", [True, False], ids=["retained_openrouter", "fresh_install"])
async def test_calendar_and_research_block_with_retired_reason(accounting_db, monkeypatch, capability, retained):
    from src.db.models import WorkBoardStatus, WorkBoardTask
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.repository import WorkBoardRepository
    from src.workflows.job_runtime import DurableJobRepository
    from tests.test_inference_accounting import setup_configuration

    tmp_path, _engine, factory = accounting_db
    sessions = factory.accounting_sessions
    operator = await _operator_session(monkeypatch)
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    if retained:
        with historical_openrouter():
            setup_configuration(ceiling=25_000, bound=1000)
    _assert_retired_world(retained)
    seed = _seed_calendar if capability.startswith("calendar") else _seed_research
    goal_id, typed_input = await seed(sessions, owner)
    task_id = await _todo_capability_task(sessions, owner, capability=capability, goal_id=goal_id,
        typed_input=typed_input, key=f"{capability}-no-provider")

    async def forbidden_source(*_args, **_kwargs):
        raise AssertionError("no source read may happen without an active model route")
    monkeypatch.setattr("src.integrations.google_calendar.GoogleCalendarReadonlyAdapter", forbidden_source)
    monkeypatch.setattr("src.security.http_transport.fetch_pinned_https", forbidden_source)
    dispatcher = WorkBoardDispatcher(repository=WorkBoardRepository(), jobs=DurableJobRepository(),
        session_provider=sessions, runner_id="service:work-board")
    reason = RETIRED_REASONS[retained]
    with no_provider_contact():
        receipts = [await dispatcher.run_pass() for _ in range(2)]
    assert all(receipt.get("admitted", 0) == 0 and receipt.get("completed", 0) == 0 for receipt in receipts), receipts
    async with sessions() as db:
        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))).scalar_one()
    assert task.status is WorkBoardStatus.blocked, task.status
    assert task.block_kind == "capability"
    assert task.block_reason == retirement.inactive_route_label(reason)
    assert await assert_no_orphans_or_accounting(sessions) == []


# ── Specialist delegation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_specialist_delegation_planning_blocks_with_retired_reason(task_runtime, monkeypatch,
        native_admission_lifecycle):
    """A reserved specialist delegation reaches its child-planning model step
    and is refused with the stable cause: no child card, planning job,
    reservation or contact; the parent's existing jobs are unchanged."""
    from dataclasses import replace
    from src.auth.service import authenticate_session
    from src.db.models import WorkBoardTask, WorkflowRunState
    from src.work_board.repository import BoardError
    from src.workflows.specialist_delegation import execute_specialist
    from tests.test_general_task_specialist_planning import specialist_fixture

    monkeypatch.setattr("src.workflows.job_runtime.get_session", task_runtime[0])
    with historical_openrouter():
        (sessions, dispatcher, planner, transport, owner, _group, accounting, _inputs, _descriptors,
            _provenance) = await specialist_fixture(task_runtime, monkeypatch)
    _assert_retired_world(True)
    service = dispatcher.general_tasks
    service.planner, service.delegation_jobs = planner, dispatcher.jobs
    operator = await authenticate_session(owner.session_id, touch=False)
    invocation_id = accounting["delegation_invocation_id"]
    principal = replace(operator.principal, session_id=owner.session_id, operator_session_id=owner.session_id,
        job_id=invocation_id)

    async def state():
        async with sessions() as db:
            tasks = [(task.task_id, task.status) for task in (await db.execute(select(WorkBoardTask))).scalars()]
            runs = [(run.run_identity, run.status) for run in (await db.execute(select(WorkflowRunState))).scalars()]
        return tasks, runs
    before = await state()
    with no_provider_contact():
        for _ in range(2):  # a retried callback gets the same precontact refusal
            with pytest.raises(BoardError) as blocked:
                await execute_specialist(dispatcher.jobs, service=service, invocation_id=invocation_id,
                    fencing_token=accounting["parent_fence"], principal=principal)
            assert blocked.value.code == OPENROUTER_RETIRED_REASON
            assert blocked.value.message == retirement.inactive_route_label(OPENROUTER_RETIRED_REASON)
    assert transport["contacts"] == []
    # No child card and no new durable job; only the parent's pre-existing rows.
    assert await state() == before
    assert len(before[0]) == 1
    assert (await dispatcher.jobs.inference_accounting_snapshot())["operations"] == []
