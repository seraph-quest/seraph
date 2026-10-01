"""Opt-in native vertical proofs for the fixed v2 Calendar and Browser paths.

These tests intentionally exercise the production service and dispatcher seams:
the only network boundaries replaced are the pinned Google/fixture transports
and the final governed model transport.  The tests are opt-in because the
Calendar proof starts a real authenticated lifecycle and the Browser proof
requires local Chromium.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping
import uuid

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import (
    CalendarPrepReceipt,
    CalendarReadConsent,
    Goal,
    GovernedScheduleBinding,
    GoogleServiceConnection,
    GovernedScheduleOccurrence,
    GuardianRoutineVersion,
    ScheduledJobRun,
    WorkBoardAttempt,
    WorkBoardInputArtifact,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.extensions.capability_pack import CapabilityPackLifecycle
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.integrations.google_calendar import (
    CalendarEventSnapshot,
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
from src.scheduler.governed_schedules import claim_occurrence, latest_due_slot, reserve_occurrence, settle_occurrence
from src.scheduler.scheduled_jobs import execute_scheduled_job
from src.security.trust_contract import EgressClass
from src.vault import encrypt
from src.work_board.contracts import (
    WorkBoardInputArtifactCreate,
    WorkBoardOwner,
    WorkBoardTaskCreate,
)
from src.work_board.dispatcher import WorkBoardDispatcher, registered_executor_id
from src.work_board.input_artifacts import (
    _payload_path,
    delete_input_artifact,
    expire_input_artifacts,
    prepare_input_artifact,
)
from src.work_board.repository import WorkBoardRepository
from src.workflows.job_runtime import DurableJobLeaseError, DurableJobRepository
from src.workflows.procedure_service import (
    ProcedureV2CreateRequest,
    ProcedureV2InvokeRequest,
    ProcedureV2PreviewRequest,
    ProcedureV2ScheduleCadence,
    ProcedureV2ScheduleRequest,
)
from src.workflows.procedure_v2_runtime import (
    ProcedureV2Runtime,
    ProcedureV2RuntimeError,
    deterministic_child_job_id,
)
from src.workflows.routines import (
    RoutineActivateRequest,
    RoutineInstallRequest,
    RoutinePackageActivationRequest,
    RoutinePackageDecisionRequest,
    RoutineService,
)

from tests.test_browser_task_runtime import _input as browser_input
from tests.test_browser_task_lifecycle import _html_responses
from tests.test_calendar_manual_vertical import _provider_event


pytestmark = pytest.mark.asyncio


def _opt_in(name: str) -> None:
    if os.environ.get(name) != "1":
        pytest.skip(f"{name}=1 is required for this native vertical proof")


def _configure_openrouter() -> Any:
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
    from src.llm_runtime import _provider_profile

    profile = _provider_profile("openrouter")
    assert profile is not None
    checked_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    probe_started = checked_at - timedelta(seconds=1)
    receipt = RouteReceipt(
        receipt_id=f"native-vertical-probe-{uuid.uuid4().hex}",
        request_id=f"native-vertical-request-{uuid.uuid4().hex}",
        route_decision_id=f"native-vertical-decision-{uuid.uuid4().hex}",
        runtime_path="strategist_agent",
        workload="background",
        outcome="succeeded",
        egress_class=setup.egress_class.value,
        started_at=probe_started,
        finished_at=probe_started + timedelta(milliseconds=100),
        latency_ms=100,
        actual_profile_id=profile.id,
        actual_model=profile.model,
        actual_adapter=profile.transport_adapter,
        destination_class="remote",
        trust_decision_id=f"native-vertical-trust-{uuid.uuid4().hex}",
    )

    async def persist_proofs() -> None:
        persisted = await model_fabric_repository.persist_route_receipt(receipt)
        assert persisted.persisted is True
        for capability, proven_value in (
            ("text", "present"),
            ("structured_output", "json"),
            ("health", "healthy"),
            ("latency_ms", 100),
        ):
            proof = build_model_route_proof(
                profile=profile,
                endpoint_class=EndpointClass.REMOTE,
                adapter=profile.transport_adapter,
                capability=capability,
                canary_version="native-vertical-v1",
                outcome="passed",
                checked_at=checked_at.timestamp(),
                expires_at=checked_at.timestamp() + 3600,
                probe_receipt_id=receipt.receipt_id,
                probe_receipt_hash=receipt.receipt_hash,
                proven_value=proven_value,
            )
            persisted_proof = await model_fabric_repository.persist_capability_proof(proof)
            assert persisted_proof.persisted is True

    return persist_proofs


async def _seed_calendar_source(
    *,
    async_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_requests: list[tuple[str, str]],
    model_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create and execute one real M5 Calendar task for v2 source proof."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "native-vertical-auth-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "openrouter_provider_only", True)

    from src.auth.service import create_session

    _token, operator = await create_session()
    owner = WorkBoardOwner(
        principal_id=operator.principal.principal_id,
        session_id=operator.session_id,
    )
    persist_proofs = _configure_openrouter()
    await persist_proofs()

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
    list_revision = "sha256:" + "b" * 64
    goal_id = f"goal-native-calendar-{uuid.uuid4().hex}"
    async with async_db() as db:
        budget = GoalAdmissionBudget(
            reviewed_grant=True,
            grant_id=f"grant-{goal_id}",
            max_outstanding_jobs=2,
            max_attempts=2,
            max_runtime_seconds=300,
            period_started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            period_expires_at=datetime.now(timezone.utc) + timedelta(days=2),
            timezone="UTC",
        )
        goal = Goal(
            id=goal_id,
            title="Native Calendar procedure goal",
            status="active",
            revision=1,
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            admission_budget_json=serialize_admission_budget(budget),
        )
        connection = GoogleServiceConnection(
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            vault_secret_key=f"vault:{goal_id}",
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
        db.add_all([goal, connection, consent])
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
        await db.flush()
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
        source_artifact = await prepare_input_artifact(
            db,
            owner,
            WorkBoardInputArtifactCreate(
                schema_version=1,
                capability_id="calendar.meeting-prep.v1",
                goal_id=goal.id,
                goal_revision=goal.revision,
                input=typed_input,
                idempotency_key=f"calendar-native-source-{uuid.uuid4().hex}",
            ),
        )
        repository = WorkBoardRepository()
        mutation = await repository.create_task(
            db,
            owner,
            WorkBoardTaskCreate(
                title="Prepare architecture review source",
                body="Native v2 source proof",
                goal_id=goal.id,
                goal_revision=goal.revision,
                status=WorkBoardStatus.todo,
                capability_id="calendar.meeting-prep.v1",
                input_artifact_id=source_artifact.artifact_id,
                executor_id=registered_executor_id("calendar.meeting-prep.v1"),
                priority=90,
                idempotency_scope="calendar-native-source",
                idempotency_key=f"calendar-native-source-task-{uuid.uuid4().hex}",
                origin_thread_id=owner.session_id,
            ),
        )
        promoted = await repository.promote_task_ready(
            db,
            mutation.task.task_id,
            expected_revision=mutation.task.task_revision,
            actor_principal_id=owner.principal_id,
            actor_session_id=owner.session_id,
        )
        assert promoted is not None
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
        source_task_id = mutation.task.task_id
        source_revision = int(claim.task.task_revision)
        goal_revision = int(goal.revision)
        connection_id = connection.connection_id
        consent_id = consent.consent_id
        binding_id = binding.event_binding_id

    async def vault_get(key: str) -> str:
        assert key == f"vault:{goal_id}"
        return json.dumps({"client_id": "calendar-client", "refresh_token": "calendar-refresh"})

    async def provider_request(url: str, **kwargs: Any) -> Any:
        method = str(kwargs.get("method", "GET")).upper()
        provider_requests.append((method, str(url)))
        if method == "POST":
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

    monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider_request)

    source_output = {
        "schema_version": 1,
        "event_key": snapshot.event_key,
        "event_revision": revision,
        "summary": "Review the architecture decisions and open risks.",
        "agenda": ["Decisions", "Risks"],
        "questions": ["What remains unresolved?"],
        "risks": ["Unbounded scope"],
        "preparation_steps": ["Read the current design notes"],
    }

    def governed_transport(**kwargs: Any) -> Any:
        model_calls.append(dict(kwargs["body"]))
        message = SimpleNamespace(role="assistant", content=json.dumps(source_output))
        response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
        return response, {"choices": [{"message": {"role": "assistant", "content": json.dumps(source_output)}}]}

    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", governed_transport)
    dispatcher = WorkBoardDispatcher(
        repository=repository,
        jobs=DurableJobRepository(),
        session_provider=async_db,
    )
    source_result = await dispatcher._admit_execute_direct(claim, typed_input, runtime_seconds=180)
    assert source_result["completed"] is True, source_result

    async with async_db() as db:
        source_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == source_task_id))
        ).scalars().one()
        assert source_task is not None and source_task.status is WorkBoardStatus.done
        source_revision = int(source_task.task_revision)
        source_attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == source_task_id)
            )
        ).scalars().one()
        assert source_attempt.outcome == "verified"
        from src.api.work_board import _calendar_execution_payload

        source_job = await DurableJobRepository().get_job(source_attempt.workflow_run_id)
        source_projection = await _calendar_execution_payload(
            source_task,
            source_attempt,
            db=db,
            projection=source_job,
        )
        source_receipt = (
            await db.execute(
                select(CalendarPrepReceipt).where(CalendarPrepReceipt.task_id == source_task_id)
            )
        ).scalars().first()
        assert (
            isinstance(source_projection, Mapping)
            and source_projection.get("durable_status") == "succeeded"
            and all(source_projection.get(key) for key in ("artifact_id", "readback_id", "file_path", "content_sha256"))
        ), json.dumps({
            "workflow_run_id": source_attempt.workflow_run_id,
            "artifacts": source_job.get("artifacts") if isinstance(source_job, Mapping) else None,
            "effects": [
                item for item in (source_job.get("effects", []) if isinstance(source_job, Mapping) else [])
                if isinstance(item, Mapping) and item.get("receipt_kind") == "readback"
            ],
            "projection": source_projection,
            "receipt": {
                "status": getattr(source_receipt, "status", None),
                "artifact_id": getattr(source_receipt, "artifact_id", None),
                "file_path": getattr(source_receipt, "file_path", None),
                "readback_id": getattr(source_receipt, "readback_id", None),
                "content_sha256": getattr(source_receipt, "content_sha256", None),
            },
        }, default=str, sort_keys=True)

    return {
        "owner": owner,
        "goal_id": goal_id,
        "goal_revision": goal_revision,
        "connection_id": connection_id,
        "consent_id": consent_id,
        "binding_id": binding_id,
        "typed_input": typed_input,
        "source_task_id": source_task_id,
        "source_revision": source_revision,
        "provider_event": event,
        "snapshot_event_key": snapshot.event_key,
        "repository": repository,
        "dispatcher": dispatcher,
    }


async def _activate_v2_routine(
    source: Mapping[str, Any],
    *,
    async_db: Any,
    template_id: str = "selected-meeting-prep",
    name: str = "Native Calendar meeting preparation",
) -> tuple[RoutineService, dict[str, Any], int]:
    owner = source["owner"]
    routines = RoutineService()
    preview_request = ProcedureV2PreviewRequest(
        template_id=template_id,
        source_tasks=[
            {
                "task_id": source["source_task_id"],
                "expected_revision": source["source_revision"],
            }
        ],
        name=name,
        idempotency_key=f"native-calendar-procedure-{uuid.uuid4().hex}",
    )
    preview = await routines.preview_from_tasks(
        preview_request,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    prepared, status_code = await routines.create_from_tasks(
        ProcedureV2CreateRequest(
            **preview_request.model_dump(mode="json"),
            preview_digest=preview["preview_digest"],
        ),
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    assert status_code == 201
    assert prepared["status"] == "prepared"
    from src.approval.repository import approval_repository

    install_approval = await approval_repository.resolve(prepared["approval_id"], "approved")
    assert install_approval is not None and install_approval.status == "approved"
    installed = await routines.install(
        prepared["routine_id"],
        RoutineInstallRequest(version=1, expected_routine_revision=1, approval_id=prepared["approval_id"]),
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    installed_revision = int(installed["revision"])
    reviewed = await routines.review_package(
        prepared["routine_id"],
        1,
        expected_routine_revision=installed_revision,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    assert reviewed["digest"] == installed["versions"][0]["installed_package_digest"]
    package_pending = await routines.prepare_package_approval(
        prepared["routine_id"],
        1,
        expected_routine_revision=installed_revision,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    package_approval_id = package_pending["approval"]["approval_id"]
    decided = await routines.decide_package_approval(
        prepared["routine_id"],
        1,
        package_approval_id,
        RoutinePackageDecisionRequest(expected_routine_revision=installed_revision, decision="approved"),
        expected_routine_revision=installed_revision,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    assert decided["approval"]["status"] == "approved"
    package_active = await routines.activate_package(
        prepared["routine_id"],
        1,
        RoutinePackageActivationRequest(
            expected_routine_revision=installed_revision,
            approval_id=package_approval_id,
        ),
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    assert package_active["status"] == "active"
    active = await routines.activate(
        prepared["routine_id"],
        RoutineActivateRequest(expected_routine_revision=installed_revision, version=1),
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    assert active["state"] == "active"
    return routines, prepared, int(active["revision"])


async def _seed_browser_source(
    *,
    async_db: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    browser: Any,
    proactive_enabled: bool = False,
) -> dict[str, Any]:
    """Run one real Chromium task whose verified output can seed v2."""

    from src.auth.service import create_session
    from src.browser.pinned_transport import PinnedBrowserRequest, PinnedBrowserResponse, PinnedBrowserTransport
    from src.browser.task_runner import BrowserTaskRunner
    from src.security.site_policy import SiteAccessDecision

    _token, operator = await create_session()
    owner = WorkBoardOwner(
        principal_id=operator.principal.principal_id,
        session_id=operator.session_id,
    )
    goal_id = f"goal-native-browser-{uuid.uuid4().hex}"
    inputs = browser_input()
    budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id=f"grant-{goal_id}",
        max_outstanding_jobs=2,
        max_attempts=2,
        max_runtime_seconds=180,
        period_started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        period_expires_at=datetime.now(timezone.utc) + timedelta(days=2),
        timezone="UTC",
    )
    repository = WorkBoardRepository()
    dispatcher = WorkBoardDispatcher(
        repository=repository,
        jobs=DurableJobRepository(),
        session_provider=async_db,
    )
    async with async_db() as db:
        db.add(
            Goal(
                id=goal_id,
                title="Native Browser schedule source",
                status="active",
                revision=1,
                proactive_enabled=proactive_enabled,
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
                admission_budget_json=serialize_admission_budget(budget),
            )
        )
        await db.flush()
        artifact = await prepare_input_artifact(
            db,
            owner,
            WorkBoardInputArtifactCreate(
                schema_version=1,
                capability_id="browser.public-task.v1",
                goal_id=goal_id,
                goal_revision=1,
                input=inputs,
                idempotency_key=f"browser-native-source-{uuid.uuid4().hex}",
            ),
        )
        mutation = await repository.create_task(
            db,
            owner,
            WorkBoardTaskCreate(
                title="Run Browser source proof",
                body="Native Chromium source proof",
                goal_id=goal_id,
                goal_revision=1,
                status=WorkBoardStatus.todo,
                capability_id="browser.public-task.v1",
                input_artifact_id=artifact.artifact_id,
                executor_id=registered_executor_id("browser.public-task.v1"),
                priority=90,
                idempotency_scope="browser-native-source",
                idempotency_key=f"browser-native-source-task-{uuid.uuid4().hex}",
                origin_thread_id=owner.session_id,
            ),
        )
        promoted = await repository.promote_task_ready(
            db,
            mutation.task.task_id,
            expected_revision=mutation.task.task_revision,
            actor_principal_id=owner.principal_id,
            actor_session_id=owner.session_id,
        )
        assert promoted is not None
        claim = await repository.claim_ready_task(
            db,
            mutation.task.task_id,
            expected_revision=promoted.task.task_revision,
            lease_owner=dispatcher.runner_id,
            lease_seconds=180,
            actor_principal_id=dispatcher.runner_id,
            actor_session_id=dispatcher.runner_session,
        )
        assert claim is not None
        await db.commit()

    responses = _html_responses()

    async def fixture_fetch(request: PinnedBrowserRequest) -> PinnedBrowserResponse:
        return responses[request.url]

    async def fixture_policy(url: str, **_: Any) -> SiteAccessDecision:
        assert url.startswith("https://fixture.example/")
        return SiteAccessDecision(allowed=True, hostname="fixture.example")

    async def fixture_resolver(*_: Any) -> list[str]:
        return ["93.184.216.34"]

    real_runner = BrowserTaskRunner

    def runner_factory(**kwargs: Any) -> Any:
        kwargs["browser_launcher"] = browser if callable(browser) else (lambda: browser)
        kwargs["transport_factory"] = lambda: PinnedBrowserTransport(
            resolver=fixture_resolver,
            injected_fetch=fixture_fetch,
            site_policy=fixture_policy,
        )
        kwargs["workspace_root"] = tmp_path
        return real_runner(**kwargs)

    monkeypatch.setattr("src.browser.task_runner.BrowserTaskRunner", runner_factory)
    # Browser owns its native durable root; use the normal capability lane
    # rather than the direct-adapter path reserved for Calendar/procedure
    # roots.
    result = await dispatcher._admit_execute_project(claim)
    assert result["completed"] is True, result

    async with async_db() as db:
        source_task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == mutation.task.task_id))
        ).scalar_one_or_none()
        assert source_task is not None and source_task.status is WorkBoardStatus.done
        source_attempt = (
            await db.execute(
                select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == mutation.task.task_id)
            )
        ).scalars().one()
        assert source_attempt.outcome == "verified"
    return {
        "owner": owner,
        "goal_id": goal_id,
        "goal_revision": 1,
        "source_task_id": mutation.task.task_id,
        "source_revision": int(source_task.task_revision),
        "source_artifact_id": artifact.artifact_id,
        "repository": repository,
        "dispatcher": dispatcher,
        "inputs": inputs,
}


@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_PROCEDURE_V2_NATIVE") != "1",
    reason="native Calendar procedure proof is an explicit opt-in integration test",
)
async def test_native_selected_meeting_preparation_runs_real_v2_child_and_replays_readback(
    async_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider_requests: list[tuple[str, str]] = []
    source_model_calls: list[dict[str, Any]] = []
    source = await _seed_calendar_source(
        async_db=async_db,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider_requests=provider_requests,
        model_calls=source_model_calls,
    )
    provider_requests.clear()
    source_model_calls.clear()
    routines, prepared, active_revision = await _activate_v2_routine(source, async_db=async_db)
    owner = source["owner"]
    invocation_uuid = str(uuid.uuid4())
    invocation = ProcedureV2InvokeRequest(
        version=1,
        expected_routine_revision=active_revision,
        goal_id=source["goal_id"],
        expected_goal_revision=source["goal_revision"],
        parameters=source["typed_input"],
        invocation_uuid=invocation_uuid,
    )
    accepted, status_code = await routines.invoke_v2(
        prepared["routine_id"],
        invocation,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    assert status_code == 201
    assert accepted["status"] == "accepted"

    model_output = {
        "schema_version": 1,
        "event_key": source["snapshot_event_key"],
        "event_revision": source["typed_input"]["event_revision"],
        "summary": "Review decisions and unresolved risks before the meeting.",
        "agenda": ["Decisions", "Risks"],
        "questions": ["What remains unresolved?"],
        "risks": ["Unbounded scope"],
        "preparation_steps": ["Read the current design notes"],
    }
    active_model_calls = 0
    peak_model_calls = 0

    def governed_transport(**kwargs: Any) -> Any:
        nonlocal active_model_calls, peak_model_calls
        active_model_calls += 1
        peak_model_calls = max(peak_model_calls, active_model_calls)
        try:
            model_calls = kwargs["body"]
            response = SimpleNamespace(
                choices=[SimpleNamespace(role="assistant", message=SimpleNamespace(role="assistant", content=json.dumps(model_output)))]
            )
            return response, {"choices": [{"message": {"role": "assistant", "content": json.dumps(model_output)}}]}
        finally:
            active_model_calls -= 1

    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", governed_transport)
    dispatcher = WorkBoardDispatcher(
        repository=source["repository"],
        jobs=DurableJobRepository(),
        session_provider=async_db,
    )
    pass_receipt = await dispatcher.run_pass()
    if pass_receipt["completed"] < 1:
        async with async_db() as db:
            blocked_tasks = (
                await db.execute(
                    select(WorkBoardTask).where(
                        WorkBoardTask.owner_principal_id == owner.principal_id,
                        WorkBoardTask.owner_session_id == owner.session_id,
                    )
                )
            ).scalars().all()
        parent_candidate = next(
            task for task in blocked_tasks if task.capability_id == "guardian-routine.v2"
        )
        readiness = await dispatcher._readiness(parent_candidate)
        debug_jobs = []
        async with async_db() as db:
            debug_attempts = (
                await db.execute(
                    select(WorkBoardAttempt).where(
                        WorkBoardAttempt.task_id.in_([task.task_id for task in blocked_tasks])
                    )
                )
            ).scalars().all()
        for attempt in debug_attempts:
            if attempt.workflow_run_id:
                debug_jobs.append(await dispatcher.jobs.get_job(attempt.workflow_run_id))
        raise AssertionError(
            json.dumps(
                {
                    "receipt": pass_receipt,
                    "tasks": [
                        {
                            "id": task.task_id,
                            "capability": task.capability_id,
                            "status": str(task.status),
                            "revision": task.task_revision,
                            "block_kind": task.block_kind,
                            "block_reason": task.block_reason,
                        }
                        for task in blocked_tasks
                    ],
                    "readiness": readiness,
                    "jobs": debug_jobs,
                },
                default=str,
                sort_keys=True,
            )
        )
    assert peak_model_calls == 1
    assert len(provider_requests) == 3, provider_requests
    assert [method for method, _url in provider_requests] == ["POST", "GET", "GET"]

    async with async_db() as db:
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                )
            )
        ).scalars().all()
        attempts = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id.in_([task.task_id for task in tasks])
                )
            )
        ).scalars().all()
        parent_task = next(task for task in tasks if task.task_id == accepted["task_id"])
        child_tasks = [
            task
            for task in tasks
            if task.capability_id == "calendar.meeting-prep.v1"
            and task.task_id != source["source_task_id"]
        ]
        assert parent_task.status is WorkBoardStatus.done
        assert len(child_tasks) == 1
        assert child_tasks[0].status is WorkBoardStatus.done
        child_attempt = next(attempt for attempt in attempts if attempt.task_id == child_tasks[0].task_id)
        receipt = (
            await db.execute(
                select(CalendarPrepReceipt).where(CalendarPrepReceipt.task_id == child_tasks[0].task_id)
            )
        ).scalar_one()
        root_receipt = (
            await db.execute(
                select(WorkflowRunState).where(WorkflowRunState.run_identity == receipt.durable_job_id)
            )
        ).scalar_one()
    assert child_attempt.workflow_run_id == receipt.durable_job_id
    assert receipt.status == "succeeded"
    assert receipt.readback_id and receipt.content_sha256
    assert receipt.memory_status == "no_learning"
    assert root_receipt.status == "succeeded"
    assert root_receipt.parent_run_identity == f"procedure-v2:{prepared['routine_id']}:1:{invocation_uuid}"
    assert receipt.durable_job_id == deterministic_child_job_id(
        root_receipt.parent_run_identity,
        "selected-meeting-prep",
        1,
        "selected_meeting_prep",
    )
    child_projection = await dispatcher.jobs.get_job(receipt.durable_job_id)
    parent_projection = await dispatcher.jobs.get_job(root_receipt.parent_run_identity)
    assert child_projection is not None and child_projection["status"] == "succeeded"
    assert parent_projection is not None and parent_projection["status"] == "succeeded"
    assert child_projection["parent_job_id"] == root_receipt.parent_run_identity
    assert child_projection["declared_authority"]["routine_step_id"] == "selected_meeting_prep"
    artifact_path = tmp_path / str(receipt.file_path)
    assert artifact_path.is_file()
    assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == receipt.content_sha256

    # Public Board projection carries proof references and status, not the
    # private generated brief body.
    async with async_db() as db:
        payload = await __import__("src.api.work_board", fromlist=["_safe_task_payload"])._safe_task_payload(
            parent_task,
            db=db,
            latest_attempt=next(attempt for attempt in attempts if attempt.task_id == parent_task.task_id),
        )
    encoded_payload = json.dumps(payload, sort_keys=True)
    assert model_output["summary"] not in encoded_payload

    replay_calls = len(provider_requests)
    replay_models = len(source_model_calls)
    replay, replay_status = await routines.invoke_v2(
        prepared["routine_id"],
        invocation,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    assert replay_status == 200
    assert replay["task_id"] == accepted["task_id"]
    assert len(provider_requests) == replay_calls
    assert len(source_model_calls) == replay_models


@pytest.mark.parametrize("authority_change", ["cancel", "reclaim"])
@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_PROCEDURE_V2_NATIVE") != "1",
    reason="native Calendar procedure proof is an explicit opt-in integration test",
)
async def test_native_calendar_parent_change_after_selected_read_blocks_before_model(
    async_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    authority_change: str,
) -> None:
    """A parent cancel/reclaim between Calendar reads prevents later effects."""

    provider_requests: list[tuple[str, str]] = []
    source_model_calls: list[dict[str, Any]] = []
    source = await _seed_calendar_source(
        async_db=async_db,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider_requests=provider_requests,
        model_calls=source_model_calls,
    )
    provider_requests.clear()
    source_model_calls.clear()
    routines, prepared, active_revision = await _activate_v2_routine(source, async_db=async_db)
    owner = source["owner"]
    invocation = ProcedureV2InvokeRequest(
        version=1,
        expected_routine_revision=active_revision,
        goal_id=source["goal_id"],
        expected_goal_revision=source["goal_revision"],
        parameters=source["typed_input"],
        invocation_uuid=str(uuid.uuid4()),
    )
    accepted, status_code = await routines.invoke_v2(
        prepared["routine_id"],
        invocation,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    assert status_code == 201

    model_calls: list[dict[str, Any]] = []
    model_output = {
        "schema_version": 1,
        "event_key": source["snapshot_event_key"],
        "event_revision": source["typed_input"]["event_revision"],
        "summary": "This output must never be reached after the parent fence changes.",
        "agenda": [],
        "questions": [],
        "risks": [],
        "preparation_steps": [],
    }

    def governed_transport(**kwargs: Any) -> Any:
        model_calls.append(dict(kwargs["body"]))
        message = SimpleNamespace(role="assistant", content=json.dumps(model_output))
        response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
        return response, {"choices": [{"message": {"role": "assistant", "content": json.dumps(model_output)}}]}

    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", governed_transport)

    parent_task_id = accepted["task_id"]
    selected_event_reads = 0
    authority_changed = False
    event_id = str(source["provider_event"]["id"])

    async def provider_request(url: str, **kwargs: Any) -> Any:
        nonlocal selected_event_reads, authority_changed
        method = str(kwargs.get("method", "GET")).upper()
        provider_requests.append((method, str(url)))
        if method == "POST":
            return SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                content=b'{"access_token":"calendar-access"}',
            )
        response = SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            content=json.dumps(source["provider_event"]).encode("utf-8"),
        )
        if f"/events/{event_id}" in str(url) and not authority_changed:
            selected_event_reads += 1
            # The callback runs after the selected event bytes have been
            # received.  Mutate the canonical parent in a separate SQLite
            # writer before MeetingPrepService reaches its next boundary.
            async with async_db() as guard_db:
                parent_attempt = (
                    await guard_db.execute(
                        select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == parent_task_id)
                    )
                ).scalars().one()
                if authority_change == "cancel":
                    parent_attempt.cancel_requested_at = datetime.now(timezone.utc)
                else:
                    parent_attempt.lease_owner = "service:reclaimed-calendar-parent"
                    parent_attempt.fencing_token = int(parent_attempt.fencing_token) + 1
                await guard_db.commit()
            authority_changed = True
        return response

    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider_request)
    dispatcher = WorkBoardDispatcher(
        repository=source["repository"],
        jobs=DurableJobRepository(),
        session_provider=async_db,
    )
    pass_receipt = await dispatcher.run_pass()

    assert authority_changed is True
    assert selected_event_reads == 1
    assert [method for method, _url in provider_requests] == ["POST", "GET"]
    assert model_calls == []
    assert pass_receipt["blocked"] >= 1 or pass_receipt.get("unknown", 0) >= 1, pass_receipt

    async with async_db() as db:
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                )
            )
        ).scalars().all()
        attempts = (
            await db.execute(
                select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id.in_([task.task_id for task in tasks])
                )
            )
        ).scalars().all()
        receipts = (await db.execute(select(CalendarPrepReceipt))).scalars().all()

    parent_task = next(task for task in tasks if task.task_id == parent_task_id)
    parent_attempt = next(attempt for attempt in attempts if attempt.task_id == parent_task_id)
    if authority_change == "cancel":
        assert parent_task.status is WorkBoardStatus.blocked
        assert parent_task.status is not WorkBoardStatus.done
    else:
        # The old executor must not project through a reclaimed lease.  The
        # canonical new worker remains the owner of the running row until its
        # own bounded recovery path settles it.
        assert parent_task.status is WorkBoardStatus.running
        assert parent_attempt.ended_at is None
        assert parent_attempt.lease_owner == "service:reclaimed-calendar-parent"
    assert parent_attempt.outcome != "verified"
    child_tasks = [
        task
        for task in tasks
        if task.capability_id == "calendar.meeting-prep.v1"
        and task.task_id != source["source_task_id"]
    ]
    assert child_tasks
    assert all(task.status is not WorkBoardStatus.done for task in child_tasks)
    child_task_ids = {task.task_id for task in child_tasks}
    assert all(
        receipt.status != "succeeded"
        for receipt in receipts
        if receipt.task_id in child_task_ids
    )


@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_PROCEDURE_V2_NATIVE") != "1",
    reason="native Calendar procedure proof is an explicit opt-in integration test",
)
async def test_native_calendar_child_publication_rejection_revokes_unbound_artifact(
    async_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A writer-fenced parent rejection leaves no executable child bytes."""

    provider_requests: list[tuple[str, str]] = []
    source_model_calls: list[dict[str, Any]] = []
    source = await _seed_calendar_source(
        async_db=async_db,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider_requests=provider_requests,
        model_calls=source_model_calls,
    )
    provider_requests.clear()
    source_model_calls.clear()
    routines, prepared, active_revision = await _activate_v2_routine(source, async_db=async_db)
    owner = source["owner"]
    invocation = ProcedureV2InvokeRequest(
        version=1,
        expected_routine_revision=active_revision,
        goal_id=source["goal_id"],
        expected_goal_revision=source["goal_revision"],
        parameters=source["typed_input"],
        invocation_uuid=str(uuid.uuid4()),
    )
    accepted, status_code = await routines.invoke_v2(
        prepared["routine_id"],
        invocation,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    assert status_code == 201

    repository = source["repository"]
    original_create_task = repository.create_task
    mutated = False

    async def reject_child(db: Any, task_owner: Any, request: Any, **kwargs: Any) -> Any:
        nonlocal mutated
        if request.capability_id == "calendar.meeting-prep.v1" and not mutated:
            parent_attempt = (
                await db.execute(
                    select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == accepted["task_id"])
                )
            ).scalars().one()
            parent_attempt.cancel_requested_at = datetime.now(timezone.utc)
            await db.flush()
            await db.commit()
            mutated = True
        return await original_create_task(db, task_owner, request, **kwargs)

    monkeypatch.setattr(repository, "create_task", reject_child)
    dispatcher = WorkBoardDispatcher(repository=repository, jobs=DurableJobRepository(), session_provider=async_db)
    pass_receipt = await dispatcher.run_pass()

    assert mutated is True
    assert provider_requests == []
    assert source_model_calls == []
    assert pass_receipt["blocked"] >= 1, pass_receipt

    parent_job_id = f"procedure-v2:{prepared['routine_id']}:1:{invocation.invocation_uuid}"
    async with async_db() as db:
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                )
            )
        ).scalars().all()
        artifacts = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
                    WorkBoardInputArtifact.owner_session_id == owner.session_id,
                    WorkBoardInputArtifact.capability_id == "calendar.meeting-prep.v1",
                )
            )
        ).scalars().all()
    child_tasks = [
        task
        for task in tasks
        if task.capability_id == "calendar.meeting-prep.v1"
        and task.task_id != source["source_task_id"]
    ]
    assert child_tasks == []
    child_artifacts = [
        artifact
        for artifact in artifacts
        if artifact.idempotency_key.startswith(f"procedure-v2:{parent_job_id}:")
    ]
    assert len(child_artifacts) == 1
    child_artifact = child_artifacts[0]
    assert child_artifact.state == "revoked"
    assert child_artifact.bound_task_id is None
    assert not _payload_path(child_artifact).exists()


@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_PROCEDURE_V2_NATIVE") != "1",
    reason="native Calendar procedure proof is an explicit opt-in integration test",
)
async def test_native_calendar_child_publication_unknown_retains_exact_committed_binding(
    async_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A committed child with a lost writer return stays for reconciliation."""

    provider_requests: list[tuple[str, str]] = []
    source_model_calls: list[dict[str, Any]] = []
    source = await _seed_calendar_source(
        async_db=async_db,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider_requests=provider_requests,
        model_calls=source_model_calls,
    )
    provider_requests.clear()
    source_model_calls.clear()
    routines, prepared, active_revision = await _activate_v2_routine(source, async_db=async_db)
    owner = source["owner"]
    invocation = ProcedureV2InvokeRequest(
        version=1,
        expected_routine_revision=active_revision,
        goal_id=source["goal_id"],
        expected_goal_revision=source["goal_revision"],
        parameters=source["typed_input"],
        invocation_uuid=str(uuid.uuid4()),
    )
    accepted, status_code = await routines.invoke_v2(
        prepared["routine_id"],
        invocation,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    assert status_code == 201

    repository = source["repository"]
    original_create_task = repository.create_task
    committed = False

    async def lose_writer_return(db: Any, task_owner: Any, request: Any, **kwargs: Any) -> Any:
        nonlocal committed
        mutation = await original_create_task(db, task_owner, request, **kwargs)
        if request.capability_id == "calendar.meeting-prep.v1" and not committed:
            # The child row and artifact binding are committed before the
            # caller learns whether the writer returned.  Raising now models a
            # lost response after a real SQLite commit, so recovery must retain
            # the exact key and bytes rather than revoke or replay it.
            await db.commit()
            committed = True
            raise RuntimeError("lost child publication response after commit")
        return mutation

    monkeypatch.setattr(repository, "create_task", lose_writer_return)
    dispatcher = WorkBoardDispatcher(repository=repository, jobs=DurableJobRepository(), session_provider=async_db)
    pass_receipt = await dispatcher.run_pass()

    assert committed is True
    assert provider_requests == []
    assert source_model_calls == []
    assert pass_receipt["blocked"] >= 1, pass_receipt

    parent_job_id = f"procedure-v2:{prepared['routine_id']}:1:{invocation.invocation_uuid}"
    async with async_db() as db:
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                )
            )
        ).scalars().all()
        artifacts = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
                    WorkBoardInputArtifact.owner_session_id == owner.session_id,
                    WorkBoardInputArtifact.capability_id == "calendar.meeting-prep.v1",
                )
            )
        ).scalars().all()
    child_tasks = [
        task
        for task in tasks
        if task.capability_id == "calendar.meeting-prep.v1"
        and task.task_id != source["source_task_id"]
    ]
    assert len(child_tasks) == 1
    child_task = child_tasks[0]
    child_artifacts = [
        artifact
        for artifact in artifacts
        if artifact.idempotency_key.startswith(f"procedure-v2:{parent_job_id}:")
    ]
    assert len(child_artifacts) == 1
    child_artifact = child_artifacts[0]
    assert child_artifact.state == "bound"
    assert child_artifact.bound_task_id == child_task.task_id
    expected_child_job_id = deterministic_child_job_id(
        parent_job_id,
        "selected-meeting-prep",
        1,
        "selected_meeting_prep",
    )
    assert child_task.idempotency_key == f"{parent_job_id}:selected_meeting_prep:{expected_child_job_id}"
    assert child_artifact.idempotency_key == f"procedure-v2:{parent_job_id}:selected_meeting_prep:{expected_child_job_id}"
    assert _payload_path(child_artifact).is_file()
    assert child_task.status is not WorkBoardStatus.done


@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_PROCEDURE_V2_NATIVE") != "1",
    reason="native Calendar procedure proof is an explicit opt-in integration test",
)
async def test_native_calendar_admission_denial_settles_materialized_child_and_parent(
    async_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider_requests: list[tuple[str, str]] = []
    source_model_calls: list[dict[str, Any]] = []
    source = await _seed_calendar_source(
        async_db=async_db,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider_requests=provider_requests,
        model_calls=source_model_calls,
    )
    provider_requests.clear()
    source_model_calls.clear()
    routines, prepared, active_revision = await _activate_v2_routine(source, async_db=async_db)
    owner = source["owner"]
    invocation_uuid = str(uuid.uuid4())
    invocation = ProcedureV2InvokeRequest(
        version=1,
        expected_routine_revision=active_revision,
        goal_id=source["goal_id"],
        expected_goal_revision=source["goal_revision"],
        parameters=source["typed_input"],
        invocation_uuid=invocation_uuid,
    )
    accepted, status_code = await routines.invoke_v2(
        prepared["routine_id"],
        invocation,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    assert status_code == 201

    dispatcher = WorkBoardDispatcher(
        repository=source["repository"],
        jobs=DurableJobRepository(),
        session_provider=async_db,
    )
    parent_job_id = f"procedure-v2:{prepared['routine_id']}:1:{invocation_uuid}"
    child_job_id = deterministic_child_job_id(
        parent_job_id,
        "selected-meeting-prep",
        1,
        "selected_meeting_prep",
    )
    original_admit = dispatcher.jobs.admit_job

    async def deny_child(spec: Any) -> Mapping[str, Any]:
        if str(getattr(getattr(spec, "identity", None), "job_id", "")) == child_job_id:
            raise DurableJobLeaseError("procedure_goal_budget_exhausted")
        return await original_admit(spec)

    monkeypatch.setattr(dispatcher.jobs, "admit_job", deny_child)
    pass_receipt = await dispatcher.run_pass()
    assert pass_receipt["blocked"] >= 1
    assert provider_requests == []
    assert source_model_calls == []

    async with async_db() as db:
        tasks = list(
            (
                await db.execute(
                    select(WorkBoardTask).where(
                        WorkBoardTask.owner_principal_id == owner.principal_id,
                        WorkBoardTask.owner_session_id == owner.session_id,
                    )
                )
            ).scalars().all()
        )
        attempts = list(
            (
                await db.execute(
                    select(WorkBoardAttempt).where(
                        WorkBoardAttempt.task_id.in_([task.task_id for task in tasks])
                    )
                )
            ).scalars().all()
        )
    parent_task = next(task for task in tasks if task.task_id == accepted["task_id"])
    child_task = next(
        task
        for task in tasks
        if task.capability_id == "calendar.meeting-prep.v1"
        and task.task_id != source["source_task_id"]
    )
    parent_attempt = next(attempt for attempt in attempts if attempt.task_id == parent_task.task_id)
    child_attempt = next(attempt for attempt in attempts if attempt.task_id == child_task.task_id)
    assert parent_task.status is WorkBoardStatus.blocked
    assert child_task.status is WorkBoardStatus.blocked
    assert parent_attempt.ended_at is not None
    assert child_attempt.ended_at is not None
    assert child_attempt.outcome == "procedure_leaf_admission_denied"
    assert child_attempt.workflow_run_id is None
    assert child_task.block_kind == "capability"
    assert child_task.block_reason == "procedure_leaf_admission_denied"
    parent_projection = await dispatcher.jobs.get_job(parent_job_id)
    assert parent_projection is not None
    assert parent_projection["status"] == "blocked"
    assert parent_projection["failure_reason"] == "procedure_leaf_admission_denied"


@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_PROCEDURE_V2_NATIVE") != "1",
    reason="native Calendar procedure proof is an explicit opt-in integration test",
)
async def test_native_calendar_ambiguous_admission_quarantines_materialized_child_and_parent(
    async_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An untyped admission failure cannot leave a claimed child running.

    The Calendar adapter is replaced only at the admission boundary.  The
    runtime still creates the canonical child Board row first, so this proves
    the unknown/ reconciliation path against real task, attempt, and durable
    parent rows without contacting the provider or model.
    """

    provider_requests: list[tuple[str, str]] = []
    source_model_calls: list[dict[str, Any]] = []
    source = await _seed_calendar_source(
        async_db=async_db,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        provider_requests=provider_requests,
        model_calls=source_model_calls,
    )
    provider_requests.clear()
    source_model_calls.clear()
    routines, prepared, active_revision = await _activate_v2_routine(source, async_db=async_db)
    owner = source["owner"]
    invocation = ProcedureV2InvokeRequest(
        version=1,
        expected_routine_revision=active_revision,
        goal_id=source["goal_id"],
        expected_goal_revision=source["goal_revision"],
        parameters=source["typed_input"],
        invocation_uuid=str(uuid.uuid4()),
    )
    accepted, status_code = await routines.invoke_v2(
        prepared["routine_id"],
        invocation,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
    )
    assert status_code == 201

    dispatcher = WorkBoardDispatcher(
        repository=source["repository"],
        jobs=DurableJobRepository(),
        session_provider=async_db,
    )

    original_admit = dispatcher._admit_v2_calendar_leaf

    async def ambiguous_admission(**kwargs: Any) -> Mapping[str, Any]:
        # Let the real adapter commit its deterministic durable root, then
        # lose the response at the admission boundary.  Recovery must retain
        # that exact root for canonical reconciliation instead of deleting or
        # admitting a second child.
        await original_admit(**kwargs)
        raise RuntimeError("admission commit boundary is ambiguous")

    # The runtime installs this method as the Calendar leaf adapter while it
    # is executing the guardian parent.  No provider/model seam is touched.
    monkeypatch.setattr(dispatcher, "_admit_v2_calendar_leaf", ambiguous_admission)
    pass_receipt = await dispatcher.run_pass()
    assert pass_receipt["blocked"] >= 1, pass_receipt
    assert provider_requests == []
    assert source_model_calls == []

    parent_job_id = f"procedure-v2:{prepared['routine_id']}:1:{invocation.invocation_uuid}"
    child_job_id = deterministic_child_job_id(
        parent_job_id,
        "selected-meeting-prep",
        1,
        "selected_meeting_prep",
    )
    async with async_db() as db:
        tasks = list(
            (
                await db.execute(
                    select(WorkBoardTask).where(
                        WorkBoardTask.owner_principal_id == owner.principal_id,
                        WorkBoardTask.owner_session_id == owner.session_id,
                    )
                )
            ).scalars().all()
        )
        attempts = list(
            (
                await db.execute(
                    select(WorkBoardAttempt).where(
                        WorkBoardAttempt.task_id.in_([task.task_id for task in tasks])
                    )
                )
            ).scalars().all()
        )
    parent_task = next(task for task in tasks if task.task_id == accepted["task_id"])
    child_task = next(
        task
        for task in tasks
        if task.capability_id == "calendar.meeting-prep.v1"
        and task.task_id != source["source_task_id"]
    )
    parent_attempt = next(attempt for attempt in attempts if attempt.task_id == parent_task.task_id)
    child_attempt = next(attempt for attempt in attempts if attempt.task_id == child_task.task_id)
    assert parent_task.status is WorkBoardStatus.blocked
    assert child_task.status is WorkBoardStatus.blocked
    assert parent_attempt.ended_at is not None
    assert child_attempt.ended_at is not None
    assert child_attempt.outcome == "unknown_external_effect"
    assert child_attempt.workflow_run_id == child_job_id
    assert child_task.block_kind == "unknown_effect"
    assert child_task.block_reason == "procedure_leaf_admission_unknown"
    parent_projection = await dispatcher.jobs.get_job(parent_job_id)
    child_projection = await dispatcher.jobs.get_job(child_job_id)
    assert parent_projection is not None
    assert child_projection is not None
    assert child_projection["status"] in {"accepted", "queued", "blocked", "unknown_external_effect"}
    assert child_projection["status"] != "running"
    assert parent_projection["status"] == "unknown_external_effect"
    assert parent_projection["failure_reason"] == "procedure_leaf_admission_unknown"


@pytest.mark.asyncio
async def test_unadmitted_child_reclaim_does_not_settle_new_worker_lease(
    async_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A lost admission response cannot project through a reclaimed attempt."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    repository = WorkBoardRepository()
    owner = WorkBoardOwner(
        principal_id="owner-native-reclaim",
        session_id="session-native-reclaim",
    )
    goal_id = "goal-native-reclaim"
    input_payload = browser_input()
    async with async_db() as db:
        db.add(
            Goal(
                id=goal_id,
                title="Native reclaim fencing",
                status="active",
                revision=1,
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
                admission_budget_json=serialize_admission_budget(
                    GoalAdmissionBudget(
                        reviewed_grant=True,
                        grant_id="grant-native-reclaim",
                        max_outstanding_jobs=2,
                        max_attempts=2,
                        max_runtime_seconds=180,
                        period_started_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                        period_expires_at=datetime.now(timezone.utc) + timedelta(days=1),
                        timezone="UTC",
                    )
                ),
            )
        )
        await db.flush()
        artifact = await prepare_input_artifact(
            db,
            owner,
            WorkBoardInputArtifactCreate(
                schema_version=1,
                capability_id="browser.public-task.v1",
                goal_id=goal_id,
                goal_revision=1,
                input=input_payload,
                idempotency_key="native-reclaim-input",
            ),
        )
        mutation = await repository.create_task(
            db,
            owner,
            WorkBoardTaskCreate(
                title="Native reclaim child",
                body="",
                goal_id=goal_id,
                goal_revision=1,
                status=WorkBoardStatus.todo,
                capability_id="browser.public-task.v1",
                input_artifact_id=artifact.artifact_id,
                executor_id=registered_executor_id("browser.public-task.v1"),
                priority=50,
                idempotency_scope="native-reclaim",
                idempotency_key="native-reclaim-task",
                origin_thread_id=owner.session_id,
            ),
        )
        promoted = await repository.promote_task_ready(
            db,
            mutation.task.task_id,
            expected_revision=mutation.task.task_revision,
            actor_principal_id="service:work-board",
            actor_session_id="service:work-board:session",
        )
        assert promoted is not None
        claim = await repository.claim_ready_task(
            db,
            mutation.task.task_id,
            expected_revision=promoted.task.task_revision,
            lease_owner="service:old-admitter",
            lease_seconds=180,
            actor_principal_id="service:old-admitter",
            actor_session_id="service:old-admitter:session",
        )
        assert claim is not None
        original_attempt = SimpleNamespace(
            attempt_id=claim.attempt.attempt_id,
            task_id=claim.attempt.task_id,
            lease_owner=claim.attempt.lease_owner,
            fencing_token=claim.attempt.fencing_token,
        )
        original_task = SimpleNamespace(
            task_id=claim.task.task_id,
            owner_principal_id=claim.task.owner_principal_id,
            owner_session_id=claim.task.owner_session_id,
        )
        await db.commit()

    async with async_db() as db:
        reclaimed = await db.get(WorkBoardAttempt, original_attempt.attempt_id)
        assert reclaimed is not None
        reclaimed.lease_owner = "service:new-worker"
        reclaimed.fencing_token = int(original_attempt.fencing_token) + 1
        await db.commit()

    runtime = ProcedureV2Runtime(
        jobs=DurableJobRepository(),
        session_provider=async_db,
        board_repository=repository,
    )
    with pytest.raises(ProcedureV2RuntimeError) as exc_info:
        await runtime._settle_unadmitted_child(
            {"task": original_task, "attempt": original_attempt},
            reason="procedure_leaf_admission_unknown",
            unknown=True,
        )
    assert exc_info.value.code == "procedure_child_settlement_unknown"

    async with async_db() as db:
        task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == original_task.task_id))
        ).scalar_one_or_none()
        attempt = await db.get(WorkBoardAttempt, original_attempt.attempt_id)
        assert task is not None and attempt is not None
        assert task.status is WorkBoardStatus.running
        assert attempt.ended_at is None
        assert attempt.lease_owner == "service:new-worker"
        assert attempt.fencing_token == int(original_attempt.fencing_token) + 1
        assert task.result_refs_json == "[]"
        assert task.artifact_refs_json == "[]"


@pytest.mark.asyncio
async def test_parent_reclaim_does_not_adopt_new_worker_lease() -> None:
    """Old procedure recovery must leave a reclaimed parent untouched."""

    parent = {
        "job_id": "procedure-parent-reclaimed",
        "status": "running",
        "revision": 4,
        "lease": {"owner": "service:new-parent-worker", "fencing_token": 2},
    }

    class Jobs:
        transition_calls = 0

        async def get_job(self, _job_id: str) -> Mapping[str, Any]:
            return parent

        async def transition_job(self, *_args: Any, **_kwargs: Any) -> Mapping[str, Any]:
            self.transition_calls += 1
            raise AssertionError("old procedure executor must not transition a reclaimed parent")

    jobs = Jobs()
    runtime = ProcedureV2Runtime(jobs=jobs)
    observed = await runtime._terminate_parent_before_leaf(
        parent["job_id"],
        reason="procedure_leaf_admission_unknown",
        unknown=True,
        expected_lease_owner="service:old-parent-worker",
        expected_fencing_token=1,
    )
    assert observed is parent
    assert jobs.transition_calls == 0
    assert parent["status"] == "running"
    assert parent["lease"] == {"owner": "service:new-parent-worker", "fencing_token": 2}


@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_PROCEDURE_V2_BROWSER_COPY") != "1",
    reason="native Browser copied-input lifetime proof is an explicit opt-in Chromium test",
)
async def test_native_browser_copied_input_survives_source_artifact_cleanup(
    async_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Execute a reviewed Browser plan after its transient source bytes are gone."""

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "native-browser-copy-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    async with async_playwright() as playwright:
        async def launch_browser() -> Any:
            return await playwright.chromium.launch(headless=True)

        try:
            source = await _seed_browser_source(
                async_db=async_db,
                monkeypatch=monkeypatch,
                tmp_path=tmp_path,
                browser=launch_browser,
            )
            routines, prepared, active_revision = await _activate_v2_routine(
                source,
                async_db=async_db,
                template_id="public-browser-check",
                name="Native Browser copied-input check",
            )

            # Preparation copied the reviewed Browser envelope into the
            # immutable version.  Remove the original task-bound payload using
            # the real lifecycle CAS before creating the invocation.
            async with async_db() as db:
                source_artifact = await db.get(WorkBoardInputArtifact, source["source_artifact_id"])
                assert source_artifact is not None
                source_ref = str(source_artifact.typed_input_ref)
                assert source_ref.startswith("workspace-json:")
                source_path = Path(settings.workspace_dir) / source_ref[len("workspace-json:") :]
                assert source_path.is_file()
                deleted = await delete_input_artifact(
                    db,
                    source["owner"],
                    artifact_id=source["source_artifact_id"],
                    expected_revision=int(source_artifact.revision),
                )
                assert deleted.state == "deleted"
            assert not source_path.exists()

            owner = source["owner"]
            invocation = ProcedureV2InvokeRequest(
                version=1,
                expected_routine_revision=active_revision,
                goal_id=source["goal_id"],
                expected_goal_revision=source["goal_revision"],
                parameters={
                    "goal_id": source["goal_id"],
                    "expected_goal_revision": source["goal_revision"],
                },
                invocation_uuid=str(uuid.uuid4()),
            )
            accepted, status_code = await routines.invoke_v2(
                prepared["routine_id"],
                invocation,
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
            )
            assert status_code == 201
            assert accepted["status"] == "accepted"

            dispatcher = WorkBoardDispatcher(
                repository=source["repository"],
                jobs=DurableJobRepository(),
                session_provider=async_db,
            )
            pass_receipt = await dispatcher.run_pass()
            if pass_receipt["completed"] < 1:
                async with async_db() as db:
                    debug_tasks = (
                        await db.execute(
                            select(WorkBoardTask).where(
                                WorkBoardTask.owner_principal_id == owner.principal_id,
                                WorkBoardTask.owner_session_id == owner.session_id,
                            )
                        )
                    ).scalars().all()
                    debug_attempts = (
                        await db.execute(
                            select(WorkBoardAttempt).where(
                                WorkBoardAttempt.task_id.in_([task.task_id for task in debug_tasks])
                            )
                        )
                    ).scalars().all()
                debug_jobs = []
                for debug_attempt in debug_attempts:
                    if debug_attempt.workflow_run_id:
                        debug_jobs.append(await dispatcher.jobs.get_job(debug_attempt.workflow_run_id))
                raise AssertionError(json.dumps({
                    "receipt": pass_receipt,
                    "tasks": [
                        {
                            "id": task.task_id,
                            "capability": task.capability_id,
                            "status": str(task.status),
                            "revision": task.task_revision,
                            "block_kind": task.block_kind,
                            "block_reason": task.block_reason,
                        }
                        for task in debug_tasks
                    ],
                    "attempts": [
                        {
                            "task_id": attempt.task_id,
                            "attempt_id": attempt.attempt_id,
                            "outcome": attempt.outcome,
                            "workflow_run_id": attempt.workflow_run_id,
                            "ended_at": attempt.ended_at,
                        }
                        for attempt in debug_attempts
                    ],
                    "jobs": debug_jobs,
                }, default=str, sort_keys=True))

            async with async_db() as db:
                tasks = (
                    await db.execute(
                        select(WorkBoardTask).where(
                            WorkBoardTask.owner_principal_id == owner.principal_id,
                            WorkBoardTask.owner_session_id == owner.session_id,
                        )
                    )
                ).scalars().all()
                attempts = (
                    await db.execute(
                        select(WorkBoardAttempt).where(
                            WorkBoardAttempt.task_id.in_([task.task_id for task in tasks])
                        )
                    )
                ).scalars().all()
                source_task = next(task for task in tasks if task.task_id == source["source_task_id"])
                parent_task = next(task for task in tasks if task.task_id == accepted["task_id"])
                child_tasks = [
                    task
                    for task in tasks
                    if task.capability_id == "browser.public-task.v1"
                    and task.task_id != source["source_task_id"]
                ]
                assert source_task.status is WorkBoardStatus.done
                assert parent_task.status is WorkBoardStatus.done
                assert len(child_tasks) == 1
                child_task = child_tasks[0]
                assert child_task.status is WorkBoardStatus.done
                child_attempt = next(attempt for attempt in attempts if attempt.task_id == child_task.task_id)
                assert child_attempt.outcome == "verified"
                assert child_attempt.workflow_run_id
                child_artifact = await db.get(WorkBoardInputArtifact, child_task.input_artifact_id)
                assert child_artifact is not None and child_artifact.state == "consumed"

            child_projection = await dispatcher.jobs.get_job(child_attempt.workflow_run_id)
            assert child_projection is not None
            assert child_projection["status"] == "succeeded"
            assert child_projection["job_kind"] == "browser_public_task"
            effects = child_projection.get("effects") if isinstance(child_projection.get("effects"), list) else []
            readbacks = [
                effect
                for effect in effects
                if isinstance(effect, Mapping)
                and effect.get("receipt_kind") == "readback"
                and effect.get("status") in {"succeeded", "read_back", "reconciled"}
            ]
            assert readbacks
            readback = readbacks[-1]
            details = readback.get("details") if isinstance(readback.get("details"), Mapping) else {}
            artifact_path = Path(settings.workspace_dir) / str(readback.get("target_path") or "")
            digest = str(readback.get("content_sha256") or details.get("content_sha256") or "")
            assert readback.get("verified") is True or details.get("verified") is True
            assert artifact_path.is_file()
            assert len(digest) == 64
            assert hashlib.sha256(artifact_path.read_bytes()).hexdigest() == digest

            replay, replay_status = await routines.invoke_v2(
                prepared["routine_id"],
                invocation,
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
            )
            assert replay_status == 200
            assert replay["task_id"] == accepted["task_id"]
            async with async_db() as db:
                assert len(
                    (
                        await db.execute(
                            select(WorkBoardTask).where(
                                WorkBoardTask.owner_principal_id == owner.principal_id,
                                WorkBoardTask.owner_session_id == owner.session_id,
                                WorkBoardTask.capability_id == "browser.public-task.v1",
                            )
                        )
                    ).scalars().all()
                ) == 2
        finally:
            pass


@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_PROCEDURE_V2_BROWSER_SCHEDULE") != "1",
    reason="native Browser schedule proof is an explicit opt-in Chromium test",
)
async def test_native_public_browser_schedule_occurrence_runs_and_replays_without_second_child(
    async_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise the governed scheduler and Chromium leaf through real rows."""

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "native-browser-auth-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")

    async with async_playwright() as playwright:
        async def launch_browser() -> Any:
            return await playwright.chromium.launch(headless=True)

        source = await _seed_browser_source(
            async_db=async_db,
            monkeypatch=monkeypatch,
            tmp_path=tmp_path,
            browser=launch_browser,
            proactive_enabled=True,
        )
        routines, prepared, active_revision = await _activate_v2_routine(
            source,
            async_db=async_db,
            template_id="public-browser-check",
            name="Native finite Browser schedule",
        )
        owner = source["owner"]
        now = datetime.now(timezone.utc)
        slot = latest_due_slot(
            {"kind": "hourly", "timezone": "UTC", "daily_hour": None, "daily_minute": None},
            now_utc=now,
        )
        assert slot is not None and slot < now
        schedule_request = ProcedureV2ScheduleRequest(
            version=1,
            expected_routine_revision=active_revision,
            goal_id=source["goal_id"],
            expected_goal_revision=source["goal_revision"],
            parameters={
                "goal_id": source["goal_id"],
                "expected_goal_revision": source["goal_revision"],
            },
            cadence=ProcedureV2ScheduleCadence(kind="hourly", timezone="UTC"),
            expires_at=now + timedelta(hours=36),
            idempotency_key=f"native-browser-schedule-{uuid.uuid4().hex}",
        )
        scheduled, schedule_status = await routines.schedule_v2(
            prepared["routine_id"],
            schedule_request,
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
        )
        assert schedule_status == 201
        assert scheduled["status"] == "scheduled"
        assert scheduled["template_id"] == "public-browser-check"
        assert scheduled["expires_at"] == schedule_request.expires_at.isoformat().replace("+00:00", "Z")

        # The reviewed schedule seed must outlive the ordinary 24-hour input
        # cleanup window.  Exercise the real SQLite cleanup clock at +25h and
        # prove the seed bytes remain available for the next native occurrence.
        async with async_db() as db:
            binding = await db.get(GovernedScheduleBinding, scheduled["binding_id"])
            assert binding is not None
            seed = await db.get(WorkBoardInputArtifact, binding.input_artifact_id)
            assert seed is not None
            seed_path = _payload_path(seed)
            assert seed_path.is_file()
            assert await expire_input_artifacts(db, now=now + timedelta(hours=25)) == 0
            await db.refresh(seed)
            assert seed.state == "pending"
            seed_expires = seed.expires_at
            if seed_expires.tzinfo is None or seed_expires.utcoffset() is None:
                seed_expires = seed_expires.replace(tzinfo=timezone.utc)
            assert seed_expires > now + timedelta(hours=25)
            assert seed_path.is_file()

        # The governed scheduler owns the finite occurrence and publishes a
        # real guardian-routine task; a same-slot retry must return that exact
        # task without minting another artifact or child.
        await execute_scheduled_job(scheduled["scheduled_job_id"], scheduled_slot_utc=slot)
        await execute_scheduled_job(scheduled["scheduled_job_id"], scheduled_slot_utc=slot)
        dispatcher = WorkBoardDispatcher(
            repository=source["repository"],
            jobs=DurableJobRepository(),
            session_provider=async_db,
        )
        pass_receipt = await dispatcher.run_pass()
        assert pass_receipt["completed"] >= 1, pass_receipt

        async with async_db() as db:
            tasks = (
                await db.execute(
                    select(WorkBoardTask).where(
                        WorkBoardTask.owner_principal_id == owner.principal_id,
                        WorkBoardTask.owner_session_id == owner.session_id,
                    )
                )
            ).scalars().all()
            attempts = (
                await db.execute(
                    select(WorkBoardAttempt).where(
                        WorkBoardAttempt.task_id.in_([task.task_id for task in tasks])
                    )
                )
            ).scalars().all()
            occurrences = (await db.execute(select(GovernedScheduleOccurrence))).scalars().all()
            source_task = next(task for task in tasks if task.task_id == source["source_task_id"])
            parent_tasks = [task for task in tasks if task.capability_id == "guardian-routine.v2"]
            child_tasks = [
                task
                for task in tasks
                if task.capability_id == "browser.public-task.v1"
                and task.task_id != source["source_task_id"]
            ]
            assert source_task.status is WorkBoardStatus.done
            assert len(parent_tasks) == 1
            assert parent_tasks[0].status is WorkBoardStatus.done
            assert len(child_tasks) == 1
            assert child_tasks[0].status is WorkBoardStatus.done
            child_attempt = next(attempt for attempt in attempts if attempt.task_id == child_tasks[0].task_id)
            assert child_attempt.outcome == "verified"
            assert len(occurrences) == 1
            assert occurrences[0].state == "running"
            assert occurrences[0].work_board_task_id == parent_tasks[0].task_id
            running_occurrence = occurrences[0]
            binding = await db.get(GovernedScheduleBinding, scheduled["binding_id"])
            assert binding is not None
            # A later canonical slot drives the existing scheduler settlement
            # path: it verifies the completed parent/readback before settling
            # the prior occurrence, then this test coalesces the unused slot so
            # no second Browser execution is admitted.
            next_occurrence, replay = await reserve_occurrence(
                db,
                binding,
                slot_utc=slot + timedelta(hours=1),
            )
            assert replay is False
            settled_previous = await db.get(GovernedScheduleOccurrence, running_occurrence.occurrence_id)
            assert settled_previous is not None and settled_previous.state == "succeeded"
            await claim_occurrence(db, next_occurrence)
            await settle_occurrence(
                db,
                next_occurrence,
                state="coalesced",
                job_id=None,
                failure_code="native_vertical_single_occurrence",
                recovery_action="wait_for_next_slot",
                claim_token=next_occurrence.claim_token,
                fencing_token=next_occurrence.fencing_token,
            )
            await db.commit()

        child_attempt_projection = await dispatcher.jobs.get_job(child_attempt.workflow_run_id)
        assert child_attempt_projection is not None
        assert child_attempt_projection["status"] == "succeeded"
        effects = child_attempt_projection.get("effects") if isinstance(child_attempt_projection.get("effects"), list) else []
        readbacks = [
            effect
            for effect in effects
            if isinstance(effect, Mapping)
            and effect.get("receipt_kind") == "readback"
            and effect.get("status") == "succeeded"
        ]
        assert readbacks
        readback = readbacks[-1]
        target_path = Path(settings.workspace_dir) / str(readback.get("target_path") or "")
        digest = str(readback.get("content_sha256") or "")
        assert target_path.is_file()
        assert len(digest) == 64
        assert hashlib.sha256(target_path.read_bytes()).hexdigest() == digest

        # The exact same slot is a durable replay receipt and cannot create a
        # second parent, child, or provider/browser execution.
        await execute_scheduled_job(scheduled["scheduled_job_id"], scheduled_slot_utc=slot)
        async with async_db() as db:
            assert len(
                (
                    await db.execute(
                        select(WorkBoardTask).where(
                            WorkBoardTask.owner_principal_id == owner.principal_id,
                            WorkBoardTask.owner_session_id == owner.session_id,
                            WorkBoardTask.capability_id == "guardian-routine.v2",
                        )
                    )
                ).scalars().all()
            ) == 1
            final_occurrences = (await db.execute(select(GovernedScheduleOccurrence))).scalars().all()
            assert sorted(row.state for row in final_occurrences) == ["coalesced", "succeeded"]


@pytest.mark.skipif(
    os.environ.get("SERAPH_RUN_REAL_PROCEDURE_V2_BROWSER_SCHEDULE") != "1",
    reason="native Browser package-revocation proof is an explicit opt-in Chromium test",
)
async def test_native_browser_schedule_package_revocation_stops_before_occurrence_contact(
    async_db,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A direct package revoke blocks a persisted schedule before a task exists."""

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("Playwright is not installed")

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "native-browser-revoke-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            source = await _seed_browser_source(
                async_db=async_db,
                monkeypatch=monkeypatch,
                tmp_path=tmp_path,
                browser=browser,
                proactive_enabled=True,
            )
            routines, prepared, active_revision = await _activate_v2_routine(
                source,
                async_db=async_db,
                template_id="public-browser-check",
                name="Native revoked Browser schedule",
            )
            owner = source["owner"]
            now = datetime.now(timezone.utc)
            slot = latest_due_slot(
                {"kind": "hourly", "timezone": "UTC", "daily_hour": None, "daily_minute": None},
                now_utc=now,
            )
            assert slot is not None and slot < now
            request = ProcedureV2ScheduleRequest(
                version=1,
                expected_routine_revision=active_revision,
                goal_id=source["goal_id"],
                expected_goal_revision=source["goal_revision"],
                parameters={
                    "goal_id": source["goal_id"],
                    "expected_goal_revision": source["goal_revision"],
                },
                cadence=ProcedureV2ScheduleCadence(kind="hourly", timezone="UTC"),
                expires_at=now + timedelta(hours=36),
                idempotency_key=f"native-browser-revoked-schedule-{uuid.uuid4().hex}",
            )
            scheduled, schedule_status = await routines.schedule_v2(
                prepared["routine_id"],
                request,
                owner_principal_id=owner.principal_id,
                owner_session_id=owner.session_id,
            )
            assert schedule_status == 201

            async with async_db() as db:
                version_row = (
                    await db.execute(
                        select(GuardianRoutineVersion).where(
                            GuardianRoutineVersion.routine_id == prepared["routine_id"],
                            GuardianRoutineVersion.version == 1,
                        )
                    )
                ).scalar_one()
            package = routines._package_readback(
                owner.principal_id,
                owner.session_id,
                prepared["routine_id"],
                1,
                version_row.installed_package_digest,
            )
            assert package["status"] == "active"
            lifecycle = CapabilityPackLifecycle()
            active_pointer = lifecycle.status(
                package["pack_id"],
                owner_principal_id=owner.principal_id,
                session_id=owner.session_id,
            )["active"]
            assert isinstance(active_pointer, Mapping)
            approval = lifecycle.create_operator_approval(
                package["pack_id"],
                action="revoke",
                goal_id=str(active_pointer["goal_id"]),
                digest=package["digest"],
                version=str(active_pointer["version"]),
                owner_principal_id=owner.principal_id,
                session_id=owner.session_id,
                content_digest=package["digest"],
                authority_digest=str(active_pointer["authority_digest"]),
            )
            revoked = lifecycle.revoke(
                package["pack_id"],
                digest=package["digest"],
                approval_id=approval["approval"]["approval_id"],
                owner_principal_id=owner.principal_id,
                session_id=owner.session_id,
                content_digest=package["digest"],
                authority_digest=str(active_pointer["authority_digest"]),
            )
            assert revoked["status"] == "revoked"

            await execute_scheduled_job(scheduled["scheduled_job_id"], scheduled_slot_utc=slot)
            async with async_db() as db:
                occurrences = (await db.execute(select(GovernedScheduleOccurrence))).scalars().all()
                guardian_tasks = (
                    await db.execute(
                        select(WorkBoardTask).where(
                            WorkBoardTask.owner_principal_id == owner.principal_id,
                            WorkBoardTask.owner_session_id == owner.session_id,
                            WorkBoardTask.capability_id == "guardian-routine.v2",
                        )
                    )
                ).scalars().all()
                run = (
                    await db.execute(
                        select(ScheduledJobRun).where(
                            ScheduledJobRun.scheduled_job_id == scheduled["scheduled_job_id"]
                        )
                    )
                ).scalar_one()
            assert occurrences == []
            assert guardian_tasks == []
            assert run.outcome == "deferred"
            assert json.loads(run.metadata_json or "{}")["recovery_action"] == "review_procedure"
        finally:
            await browser.close()
