"""Real durable Calendar preparation journey through the WorkBoard adapter."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import time
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
    WorkflowRunState,
)
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
from src.model_fabric.repository import model_fabric_repository
from src.model_fabric.receipts import RouteReceipt
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


def _provider_event() -> dict[str, object]:
    return {
        "id": "event-vertical-1",
        "summary": "Architecture review",
        "start": {"dateTime": "2026-10-01T09:00:00Z"},
        "end": {"dateTime": "2026-10-01T10:00:00Z"},
        "status": "confirmed",
    }


@pytest.mark.asyncio
async def test_calendar_dispatcher_real_durable_two_reads_model_and_readback(
    async_db,
    monkeypatch,
    tmp_path,
):
    """An owned task reaches Done only through the canonical durable root."""

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "calendar-vertical-auth-secret")
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
    # Seed the same bounded, hashed capability proofs consumed by the real
    # governed preflight selector.  The test intercepts only the final model
    # transport; it does not replace route admission or its policy decision.
    from src.llm_runtime import _provider_profile

    proof_profile = _provider_profile("openrouter")
    assert proof_profile is not None
    checked_at = time.time() - 1.0
    probe_started = datetime.now(timezone.utc) - timedelta(seconds=1)
    probe_receipt = RouteReceipt(
        receipt_id="calendar-vertical-probe-route",
        request_id="calendar-vertical-probe-request",
        route_decision_id="calendar-vertical-probe-decision",
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
        trust_decision_id="calendar-vertical-probe-trust",
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
            canary_version="calendar-vertical-v1",
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
    list_revision = "sha256:" + "b" * 64

    async with async_db() as db:
        goal = Goal(
            id="goal-calendar-vertical",
            title="Calendar vertical",
            status="active",
            revision=1,
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
        )
        connection = GoogleServiceConnection(
            owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id,
            vault_secret_key="vault:calendar-vertical",
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
            event_key=canonical_event_key(
                owner.principal_id,
                connection.connection_id,
                "primary",
                event,
            ),
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
                idempotency_key="calendar-vertical-input",
            ),
        )

    repository = WorkBoardRepository()
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
                idempotency_key="calendar-vertical-task",
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
        assert promoted.task.status is WorkBoardStatus.ready
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

    provider_requests: list[tuple[str, str]] = []

    async def vault_get(key: str) -> str:
        assert key == "vault:calendar-vertical"
        return json.dumps(
            {
                "client_id": "calendar-client",
                "refresh_token": "calendar-refresh",
            }
        )

    async def provider_request(url: str, **kwargs):
        provider_requests.append((str(kwargs.get("method", "GET")), url))
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

    monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider_request)

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
    model_calls: list[dict[str, object]] = []

    def governed_transport(**kwargs):
        model_calls.append(dict(kwargs["body"]))
        message = SimpleNamespace(role="assistant", content=json.dumps(model_output))
        response = SimpleNamespace(choices=[SimpleNamespace(message=message)])
        return response, {"choices": [{"message": {"role": "assistant", "content": json.dumps(model_output)}}]}

    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", governed_transport)
    # The provider and model transport remain intercepted, while the real
    # FallbackLiteLLMModel, durable job repository, board link, queue claim,
    # artifact, readback, and terminal receipt paths execute.
    dispatcher = WorkBoardDispatcher(
        repository=repository,
        jobs=DurableJobRepository(),
        session_provider=async_db,
    )
    result = await dispatcher._admit_execute_direct(
        claim,
        typed_input,
        runtime_seconds=120,
    )

    assert result["completed"] is True
    assert [method for method, _url in provider_requests] == ["POST", "GET", "GET"]
    assert len(model_calls) == 1

    async with async_db() as db:
        receipt = (
            await db.execute(
                select(CalendarPrepReceipt).where(
                    CalendarPrepReceipt.task_id == mutation.task.task_id,
                )
            )
        ).scalar_one()
        root = (
            await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == receipt.durable_job_id,
                )
            )
        ).scalar_one()

    assert receipt.status == "succeeded"
    assert receipt.memory_status == "no_learning"
    assert receipt.readback_id
    assert receipt.content_sha256
    effective_route = json.loads(receipt.effective_route_json)
    assert set(effective_route) == {
        "runtime_path",
        "provider",
        "model",
        "upstream_provider",
        "profile_id",
        "admission_digest",
        "status",
        "cost_microusd",
    }
    assert effective_route["runtime_path"] == "strategist_agent"
    assert effective_route["provider"] == "openrouter"
    assert effective_route["status"] == "succeeded"
    assert effective_route["profile_id"]
    assert effective_route["model"]
    assert effective_route["admission_digest"]
    assert effective_route["cost_microusd"] is None or effective_route["cost_microusd"] >= 0
    assert root.status == "succeeded"
    assert any(
        isinstance(effect, dict)
        and effect.get("receipt_kind") == "readback"
        and effect.get("status") in {"succeeded", "read_back"}
        and effect.get("details", {}).get("memory_status") == "no_learning"
        for effect in json.loads(root.effect_receipts_json or "[]")
    )
