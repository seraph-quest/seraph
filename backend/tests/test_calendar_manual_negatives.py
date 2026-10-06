"""Canonical Calendar dispatcher negative journeys through real durable state."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import (
    CalendarPrepReceipt,
    CalendarReadConsent,
    Goal,
    GoogleServiceConnection,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.integrations.google_calendar import (
    CalendarEventSnapshot,
    calendar_artifact_path_for_job,
    canonical_event_key,
    event_revision,
    persist_calendar_event_binding,
)
from src.model_fabric.configuration import (
    ModelFabricConfiguration,
    OpenRouterSetup,
    openrouter_profile_for_setup,
    write_model_fabric_configuration,
)
from src.model_fabric.contracts import EndpointClass
from src.model_fabric.proofs import build_model_route_proof
from src.model_fabric.receipts import RouteReceipt
from src.model_fabric.repository import model_fabric_repository
from src.security.trust_contract import EgressClass
from src.vault import encrypt
from src.work_board.contracts import (
    WorkBoardInputArtifactCreate,
    WorkBoardOwner,
    WorkBoardTaskCreate,
)
from src.work_board.dispatcher import WorkBoardDispatcher, registered_executor_id
from src.work_board.input_artifacts import prepare_input_artifact
from src.work_board.repository import WorkBoardRepository
from src.workflows.job_runtime import DurableJobRepository
from tests.test_inference_accounting import accounting_db


def _provider_event() -> dict[str, object]:
    return {
        "id": "event-negative-1",
        "summary": "Architecture review",
        "start": {"dateTime": "2026-10-01T09:00:00Z"},
        "end": {"dateTime": "2026-10-01T10:00:00Z"},
        "status": "confirmed",
    }


async def _seed_vertical(async_db, monkeypatch, tmp_path: Path, *, mode: str) -> SimpleNamespace:
    """Seed the same persisted owner/Goal/board graph used by the positive journey."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "calendar-negative-auth-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "openrouter_provider_only", True)

    from src.auth.service import create_session

    _token, operator = await create_session()
    owner = WorkBoardOwner(
        principal_id=operator.principal.principal_id,
        session_id=operator.session_id,
    )
    setup = OpenRouterSetup(
        model_ids=("openrouter/z-ai/glm-5.3-flash",),
        capabilities=("text", "structured_output"),
        allowed_upstreams=("z-ai",),
        egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True,
        spend_ceiling_microusd=25_000,
        credential_ref="env:OPENROUTER_API_KEY",
    )
    write_model_fabric_configuration(
        ModelFabricConfiguration(
            profiles=(openrouter_profile_for_setup(setup),),
            openrouter_setup=setup,
            status="ready",
        )
    )
    await DurableJobRepository().configure_inference_accounting(25_000)
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    monkeypatch.setattr("src.llm_runtime.gpu_admission_broker", broker)
    monkeypatch.setattr("src.model_fabric.execution.gpu_admission_broker", broker)
    from src.llm_runtime import _provider_profile

    proof_profile = _provider_profile("openrouter")
    assert proof_profile is not None
    checked_at = datetime.now(timezone.utc).timestamp() - 1.0
    probe_started = datetime.now(timezone.utc) - timedelta(seconds=1)
    probe_receipt = RouteReceipt(
        receipt_id=f"calendar-negative-{mode}-probe-route",
        request_id=f"calendar-negative-{mode}-probe-request",
        route_decision_id=f"calendar-negative-{mode}-probe-decision",
        runtime_path="strategist_agent",
        workload="background",
        outcome="succeeded",
        egress_class=setup.egress_class.value,
        started_at=probe_started,
        finished_at=probe_started + timedelta(milliseconds=100),
        latency_ms=100,
        actual_profile_id=proof_profile.id,
        actual_model=proof_profile.model,
        actual_adapter=proof_profile.transport_adapter,
        destination_class="remote",
        trust_decision_id=f"calendar-negative-{mode}-probe-trust",
    )
    probe_persisted = await model_fabric_repository.persist_route_receipt(probe_receipt)
    assert probe_persisted.persisted is True
    for capability, proven_value in (
        ("text", "present"),
        ("structured_output", "json"),
        ("health", "healthy"),
        ("latency_ms", 100),
    ):
        proof = build_model_route_proof(
            profile=proof_profile,
            endpoint_class=EndpointClass.REMOTE,
            adapter=proof_profile.transport_adapter,
            capability=capability,
            canary_version=f"calendar-negative-{mode}-v1",
            outcome="passed",
            checked_at=checked_at,
            expires_at=checked_at + 3600,
            probe_receipt_id=probe_receipt.receipt_id,
            probe_receipt_hash=probe_receipt.receipt_hash,
            proven_value=proven_value,
        )
        persisted = await model_fabric_repository.persist_capability_proof(proof)
        assert persisted.persisted is True

    event = _provider_event()
    selected = {
        "provider_event_id": event["id"],
        "recurrence_identity": "single",
        "summary": event["summary"],
        "start": "2026-10-01T09:00:00Z",
        "end": "2026-10-01T10:00:00Z",
        "location": None,
        "description": None,
        "attendees": None,
        "etag": None,
        "updated": None,
        "status": "confirmed",
    }
    revision = event_revision(selected)
    list_revision = "sha256:" + "c" * 64

    async with async_db() as db:
        goal = Goal(
            id=f"goal-calendar-negative-{mode}",
            title="Calendar negative journey",
            status="active",
            revision=1,
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
        )
        connection = GoogleServiceConnection(
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            vault_secret_key=f"vault:calendar-negative-{mode}",
            revision=1,
            state="active",
        )
        consent = CalendarReadConsent(
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            connection_id=connection.connection_id,
            calendar_id=encrypt("primary"),
            goal_id=goal.id,
            goal_revision=1,
            allowed_fields_json=json.dumps(["summary", "start", "end"]),
            allow_remote_model=True,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            state="active",
            revision=1,
            connection_revision=1,
        )
        db.add(goal)
        db.add(connection)
        db.add(consent)
        await db.flush()
        snapshot = CalendarEventSnapshot(
            event_key=canonical_event_key(owner.principal_id, connection.connection_id, "primary", event),
            event_revision=revision,
            calendar_list_revision=list_revision,
            provider_event_id=str(event["id"]),
            recurrence_identity="single",
            fields={
                "summary": event["summary"],
                "start": selected["start"],
                "end": selected["end"],
                "location": None,
                "description": None,
                "attendees": None,
            },
        )
        binding = await persist_calendar_event_binding(
            db,
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            connection=connection,
            consent=consent,
            snapshot=snapshot,
        )
        await db.commit()

    typed_input = {
        "schema_version": 1,
        "consent_id": consent.consent_id,
        "event_binding_id": binding.event_binding_id,
        "expected_event_binding_revision": binding.revision,
        "expected_consent_revision": consent.revision,
        "expected_connection_revision": connection.revision,
        "event_revision": revision,
        "calendar_list_revision": list_revision,
        "goal_id": goal.id,
        "goal_revision": goal.revision,
        "purpose": "Prepare the architecture review",
    }

    repository = WorkBoardRepository()
    async with async_db() as db:
        artifact = await prepare_input_artifact(
            db,
            owner,
            WorkBoardInputArtifactCreate(
                schema_version=1,
                capability_id="calendar.meeting-prep.v1",
                goal_id=goal.id,
                goal_revision=goal.revision,
                input=typed_input,
                idempotency_key=f"calendar-negative-{mode}-input",
            ),
        )

    async with async_db() as db:
        mutation = await repository.create_task(
            db,
            owner,
            WorkBoardTaskCreate(
                title="Prepare architecture review",
                goal_id=goal.id,
                goal_revision=goal.revision,
                status=WorkBoardStatus.todo,
                capability_id="calendar.meeting-prep.v1",
                input_artifact_id=artifact.artifact_id,
                executor_id=registered_executor_id("calendar.meeting-prep.v1"),
                idempotency_key=f"calendar-negative-{mode}-task",
            ),
        )
        await db.commit()

    async with async_db() as db:
        promoted = await repository.promote_task_ready(
            db,
            mutation.task.task_id,
            expected_revision=mutation.task.task_revision,
            actor_principal_id=owner.principal_id,
            actor_session_id=owner.session_id,
        )
        assert promoted is not None
        await db.commit()

    async with async_db() as db:
        claim = await repository.claim_ready_task(
            db,
            mutation.task.task_id,
            expected_revision=promoted.task.task_revision,
            lease_owner="service:work-board",
            lease_seconds=300,
            actor_principal_id="service:work-board",
            actor_session_id="service-session:work-board",
        )
        assert claim is not None
        await db.commit()

    ctx = SimpleNamespace(
        owner=owner,
        goal=goal,
        connection=connection,
        consent=consent,
        binding=binding,
        typed_input=typed_input,
        mutation=mutation,
        claim=claim,
        repository=repository,
        jobs=DurableJobRepository(),
        mode=mode,
        event=event,
        provider_requests=[],
        model_calls=[],
        model_started=threading.Event(),
        model_release=threading.Event(),
        revocation_task=None,
    )
    model_output = {
        "schema_version": 1,
        "event_key": snapshot.event_key,
        "event_revision": revision,
        "summary": "Review the architecture decisions and open risks.",
        "agenda": ["Decisions", "Risks"],
        "questions": ["What remains unresolved?"],
        "risks": ["Unbounded scope"],
        "preparation_steps": ["Read the current design notes"],
    }

    async def vault_get(key: str) -> str:
        assert key == f"vault:calendar-negative-{mode}"
        return json.dumps({"client_id": "calendar-client", "refresh_token": "calendar-refresh"})

    async def provider_request(url: str, **kwargs):
        ctx.provider_requests.append((str(kwargs.get("method", "GET")), url))
        if kwargs.get("method") == "POST":
            return SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                content=b'{"access_token":"calendar-access"}',
            )
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            content=json.dumps(event).encode("utf-8"),
        )

    def governed_transport(**kwargs):
        ctx.model_calls.append(dict(kwargs["body"]))
        if mode in {"revoke", "timeout"}:
            ctx.model_started.set()
            ctx.model_release.wait(timeout=10)
        message = SimpleNamespace(role="assistant", content=json.dumps(model_output))
        response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
        return response, {"choices": [{"message": {"role": "assistant", "content": json.dumps(model_output)}}]}

    monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider_request)
    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", governed_transport)

    if mode == "revoke":
        async def revoke_after_model_starts() -> None:
            await asyncio.to_thread(ctx.model_started.wait, 10)
            async with async_db() as db:
                current = await db.get(CalendarReadConsent, ctx.consent.consent_id)
                assert current is not None
                current.state = "revoked"
                current.revision += 1
                await db.commit()
            ctx.model_release.set()

        ctx.revocation_task = asyncio.create_task(revoke_after_model_starts())

    ctx.dispatcher = WorkBoardDispatcher(
        repository=repository,
        jobs=ctx.jobs,
        session_provider=async_db,
    )
    return ctx


async def _finish_background_model(ctx: SimpleNamespace) -> None:
    """Release a deliberately blocked model thread and await revocation work."""

    ctx.model_release.set()
    if ctx.revocation_task is not None:
        await asyncio.wait_for(ctx.revocation_task, timeout=5)
    if ctx.mode == "timeout":
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_calendar_real_vertical_revoked_consent_blocks_second_read_without_done(
    accounting_db,
    monkeypatch,
    tmp_path,
):
    tmp_path, _engine, factory = accounting_db
    async_db = factory.accounting_sessions
    ctx = await _seed_vertical(async_db, monkeypatch, tmp_path, mode="revoke")
    try:
        result = await ctx.dispatcher._admit_execute_direct(
            ctx.claim,
            ctx.typed_input,
            runtime_seconds=120,
        )
    finally:
        await _finish_background_model(ctx)

    assert result["admitted"] is True
    assert result["completed"] is False
    assert result["blocked"] is True
    assert [method for method, _url in ctx.provider_requests] == ["POST", "GET"]
    assert len(ctx.model_calls) == 1

    async with async_db() as db:
        consent = await db.get(CalendarReadConsent, ctx.consent.consent_id)
        task = (
            await db.execute(
                select(WorkBoardTask).where(WorkBoardTask.task_id == ctx.claim.task.task_id)
            )
        ).scalar_one_or_none()
        root = (
            await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.goal_id == ctx.goal.id,
                    WorkflowRunState.job_kind == "calendar_meeting_prep",
                )
            )
        ).scalar_one()
        receipts = (
            await db.execute(
                select(CalendarPrepReceipt).where(CalendarPrepReceipt.task_id == ctx.mutation.task.task_id)
            )
        ).scalars().all()

    assert consent is not None and consent.state == "revoked"
    assert task is not None and task.status is WorkBoardStatus.blocked
    assert root.status == "running"
    assert not receipts


@pytest.mark.asyncio
async def test_calendar_real_vertical_tampered_artifact_cannot_terminally_succeed(
    accounting_db,
    monkeypatch,
    tmp_path,
):
    tmp_path, _engine, factory = accounting_db
    async_db = factory.accounting_sessions
    ctx = await _seed_vertical(async_db, monkeypatch, tmp_path, mode="tamper")
    original_transition = ctx.jobs.transition_job

    async def tamper_before_terminal(job_id: str, to_status: str, **kwargs):
        if to_status == "succeeded":
            artifact = Path(tmp_path) / calendar_artifact_path_for_job(job_id)
            artifact.write_bytes(artifact.read_bytes() + b"tampered")
        return await original_transition(job_id, to_status, **kwargs)

    ctx.jobs.transition_job = tamper_before_terminal
    result = await ctx.dispatcher._admit_execute_direct(
        ctx.claim,
        ctx.typed_input,
        runtime_seconds=120,
    )

    assert result["admitted"] is True
    assert result["completed"] is False
    assert result["blocked"] is True
    async with async_db() as db:
        root = (
            await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.goal_id == ctx.goal.id,
                    WorkflowRunState.job_kind == "calendar_meeting_prep",
                )
            )
        ).scalar_one()
        receipt = (
            await db.execute(
                select(CalendarPrepReceipt).where(CalendarPrepReceipt.task_id == ctx.mutation.task.task_id)
            )
        ).scalar_one()

    assert root is not None and root.status == "running"
    assert receipt.status == "verified"
    artifact = Path(tmp_path) / calendar_artifact_path_for_job(root.run_identity)
    assert artifact.read_bytes().endswith(b"tampered")


@pytest.mark.asyncio
async def test_calendar_real_vertical_model_timeout_keeps_root_liability_without_retry(
    accounting_db,
    monkeypatch,
    tmp_path,
):
    tmp_path, _engine, factory = accounting_db
    async_db = factory.accounting_sessions
    ctx = await _seed_vertical(async_db, monkeypatch, tmp_path, mode="timeout")
    try:
        result = await ctx.dispatcher._admit_execute_direct(
            ctx.claim,
            ctx.typed_input,
            runtime_seconds=1,
        )
        async with async_db() as db:
            root = (
                await db.execute(
                    select(WorkflowRunState).where(
                        WorkflowRunState.goal_id == ctx.goal.id,
                        WorkflowRunState.job_kind == "calendar_meeting_prep",
                    )
                )
            ).scalar_one()
    finally:
        await _finish_background_model(ctx)

    assert result["admitted"] is True
    assert result["completed"] is False
    assert result["blocked"] is True
    assert len(ctx.model_calls) == 1
    assert root is not None
    assert root.status == "running"
    assert root.finished_at is None
    assert json.loads(root.artifact_receipts_json or "[]") == []
