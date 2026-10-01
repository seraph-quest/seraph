"""Cross-surface proof for stable operator ownership across bearer refreshes.

The rows seeded below are owner-bound metadata only.  They do not represent a
provider-ready Calendar connection or a successful routine/browser execution.
The test exercises the real authenticated API projections and the real SQLite
fixture, while keeping provider contact and model calls out of the proof.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from config.settings import settings
from src.api.auth import _reset_login_throttle_for_tests
from src.db.models import (
    CalendarReadConsent,
    Goal,
    GoogleServiceConnection,
    GuardianRoutine,
    GuardianRoutineVersion,
    GovernedScheduleBinding,
    OperatorSession,
    Session,
    ScheduledJob,
    WorkBoardInputArtifact,
    WorkBoardTask,
)


ORIGIN = "http://localhost:3001"
ORIGIN_HEADERS = {"origin": ORIGIN}
PASSWORD = "continuity-auth-secret"
FORBIDDEN_RESPONSE_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "client_secret",
    "vault_secret_key",
    "_token_hash",
    "calendar_id",
}


@pytest.fixture(autouse=True)
def configured_auth(monkeypatch):
    _reset_login_throttle_for_tests()
    monkeypatch.setattr(settings, "operator_auth_secret", PASSWORD)
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", ORIGIN)
    monkeypatch.setattr(settings, "operator_auth_idle_seconds", 300)
    monkeypatch.setattr(settings, "operator_auth_absolute_seconds", 3600)
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    yield
    _reset_login_throttle_for_tests()


async def _login(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/api/auth/login",
        json={"password": PASSWORD},
        headers=ORIGIN_HEADERS,
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get(settings.operator_auth_cookie_name)
    assert token
    client.cookies.set(settings.operator_auth_cookie_name, token)
    return response.json()


def _assert_no_private_response_fields(value: Any) -> None:
    if isinstance(value, dict):
        assert not FORBIDDEN_RESPONSE_KEYS.intersection(value), value
        for item in value.values():
            _assert_no_private_response_fields(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_private_response_fields(item)


async def _read_owner_surfaces(
    client: httpx.AsyncClient,
    *,
    task_id: str,
    artifact_id: str,
) -> dict[str, Any]:
    responses = {
        "goals": await client.get("/api/goals"),
        "task": await client.get(f"/api/work-board/tasks/{task_id}"),
        "input_artifact": await client.get(f"/api/work-board/input-artifacts/{artifact_id}"),
        "routines": await client.get("/api/capabilities/routines"),
        "connections": await client.get("/api/calendar/connections"),
        "schedules": await client.get("/api/governed-schedules"),
    }
    for name, response in responses.items():
        assert response.status_code == 200, f"{name}: {response.status_code} {response.text}"
    payload = {name: response.json() for name, response in responses.items()}
    _assert_no_private_response_fields(payload)
    return payload


async def _persisted_owner_snapshot(async_db, ids: dict[str, str]) -> dict[str, tuple[str, str]]:
    models_and_keys = (
        (Goal, "goal_id", ids["goal_id"], "id"),
        (OperatorSession, "session_id", ids["owner_session_id"], "id"),
        (GoogleServiceConnection, "connection_id", ids["connection_id"], "connection_id"),
        (CalendarReadConsent, "consent_id", ids["consent_id"], "consent_id"),
        (GuardianRoutine, "routine_id", ids["routine_id"], "id"),
        (ScheduledJob, "scheduled_job_id", ids["scheduled_job_id"], "id"),
        (GovernedScheduleBinding, "binding_id", ids["binding_id"], "binding_id"),
        (WorkBoardTask, "task_id", ids["task_id"], "task_id"),
    )
    snapshot: dict[str, tuple[str, str]] = {}
    async with async_db() as db:
        for model, label, identifier, field_name in models_and_keys:
            field = getattr(model, field_name)
            row = (await db.execute(select(model).where(field == identifier))).scalar_one()
            snapshot[label] = (
                str(getattr(row, "owner_principal_id", "")),
                str(getattr(row, "owner_session_id", None) or identifier),
            )
        artifact = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.artifact_id == ids["artifact_id"]
                )
            )
        ).scalar_one()
        snapshot["artifact_id"] = (artifact.owner_principal_id, artifact.owner_session_id)
        task = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.task_id == ids["task_id"]
                )
            )
        ).scalar_one()
        assert task.goal_id == ids["goal_id"]
    return snapshot


@pytest.mark.asyncio
async def test_owner_scope_survives_two_refreshes_across_operator_surfaces(
    client,
    app,
    async_db,
):
    owner_login = await _login(client)
    owner_session_id = owner_login["session_id"]
    principal_id = owner_login["principal_id"]

    goal_response = await client.post(
        "/api/goals",
        headers=ORIGIN_HEADERS,
        json={
            "title": "Continuity proof goal",
            "level": "daily",
            "domain": "productivity",
            "description": "Owner continuity metadata proof",
        },
    )
    assert goal_response.status_code == 200, goal_response.text
    goal = goal_response.json()
    goal_id = goal["id"]
    goal_revision = int(goal["revision"])

    browser_input = {
        "schema_version": 1,
        "start_url": "https://example.com/",
        "allowed_hosts": ["example.com"],
        "approved_url_prefixes": ["https://example.com/"],
        "actions": [
            {
                "kind": "navigate",
                "url": "https://example.com/",
                "expected_checks": [{"kind": "url_host", "value": "example.com"}],
            }
        ],
        "final_expected_checks": [{"kind": "url_host", "value": "example.com"}],
    }
    artifact_response = await client.post(
        "/api/work-board/input-artifacts",
        headers=ORIGIN_HEADERS,
        json={
            "schema_version": 1,
            "capability_id": "browser.public-task.v1",
            "goal_id": goal_id,
            "goal_revision": goal_revision,
            "input": browser_input,
            "idempotency_key": "continuity-browser-input",
        },
    )
    assert artifact_response.status_code == 200, artifact_response.text
    artifact = artifact_response.json()
    assert artifact["capability_id"] == "browser.public-task.v1"
    assert "input" not in artifact

    task_response = await client.post(
        "/api/work-board/tasks",
        headers=ORIGIN_HEADERS,
        json={
            "title": "Hold browser task for operator review",
            "body": "Triage only; no browser execution is requested by this proof.",
            "goal_id": goal_id,
            "goal_revision": goal_revision,
            "status": "triage",
            "capability_id": "browser.public-task.v1",
            "typed_input_ref": artifact["typed_input_ref"],
            "typed_input_digest": artifact["typed_input_digest"],
            "idempotency_key": "continuity-triage-task",
        },
    )
    assert task_response.status_code == 200, task_response.text
    task = task_response.json()["task"]
    task_id = task["task_id"]
    assert task["status"] == "triage"
    assert task["typed_input_ref"] == artifact["typed_input_ref"]
    assert task["typed_input_digest"] == artifact["typed_input_digest"]

    now = datetime.now(timezone.utc)
    routine_id = "continuity-guardian-routine"
    version_id = "continuity-guardian-routine-v1"
    connection_id = "continuity-google-connection"
    consent_id = "continuity-calendar-consent"
    schedule_artifact_id = "continuity-calendar-input"
    scheduled_job_id = "continuity-governed-job"
    binding_id = "continuity-governed-binding"
    async with async_db() as db:
        # Operator auth owns the bearer-session row.  Existing scheduled-job
        # metadata still references the conversation Session table, so bind
        # that historical identity to the same stable owner root explicitly.
        db.add(Session(id=owner_session_id, owner_principal_id=principal_id, title="Continuity proof"))
        await db.flush()
        db.add(
            GuardianRoutine(
                id=routine_id,
                owner_principal_id=principal_id,
                owner_session_id=owner_session_id,
                name="Continuity metadata routine",
                state="prepared",
                revision=1,
                current_version=1,
            )
        )
        db.add(
            GuardianRoutineVersion(
                id=version_id,
                routine_id=routine_id,
                version=1,
                source_provenance_json='{"source":"continuity-proof"}',
                workflow_bytes="",
                workflow_sha256="",
                runbook_bytes="",
                runbook_sha256="",
            )
        )
        db.add(
            GoogleServiceConnection(
                connection_id=connection_id,
                owner_principal_id=principal_id,
                owner_session_id=owner_session_id,
                service="calendar_readonly",
                label="Continuity Calendar metadata",
                vault_secret_key="vault://continuity/opaque-key",
                credential_fingerprint="sha256:" + "1" * 64,
                setup_idempotency_key="continuity-connection-key",
                setup_request_digest="sha256:" + "2" * 64,
                state="active",
                revision=1,
            )
        )
        db.add(
            CalendarReadConsent(
                consent_id=consent_id,
                owner_principal_id=principal_id,
                owner_session_id=owner_session_id,
                connection_id=connection_id,
                creation_idempotency_key="continuity-consent-key",
                creation_request_digest="sha256:" + "3" * 64,
                connection_revision=1,
                calendar_id="opaque-ciphertext-only",
                goal_id=goal_id,
                goal_revision=goal_revision,
                allowed_fields_json='["summary","start","end"]',
                window_minutes=60,
                max_events=10,
                allow_remote_model=False,
                expires_at=now + timedelta(days=1),
                state="active",
                revision=1,
                consent_digest="sha256:" + "4" * 64,
            )
        )
        db.add(
            WorkBoardInputArtifact(
                artifact_id=schedule_artifact_id,
                owner_principal_id=principal_id,
                owner_session_id=owner_session_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                capability_id="calendar.observe_due_events.v1",
                capability_version="1",
                idempotency_key="continuity-schedule-input",
                payload_sha256="5" * 64,
                typed_input_ref="workspace-json:artifacts/work-board/input/continuity-schedule.json",
                size_bytes=0,
                state="pending",
                expires_at=now + timedelta(days=1),
            )
        )
        db.add(
            ScheduledJob(
                id=scheduled_job_id,
                name="Continuity paused schedule",
                enabled=False,
                trigger_type="cron",
                trigger_spec_json='{"cadence":"hourly"}',
                action_type="calendar.observe_due_events.v1",
                action_spec_json="{}",
                session_id=owner_session_id,
                created_by_session_id=owner_session_id,
            )
        )
        db.add(
            GovernedScheduleBinding(
                binding_id=binding_id,
                scheduled_job_id=scheduled_job_id,
                owner_principal_id=principal_id,
                owner_session_id=owner_session_id,
                goal_id=goal_id,
                goal_revision=goal_revision,
                capability_id="calendar.observe_due_events.v1",
                action_type="calendar.observe_due_events.v1",
                input_artifact_id=schedule_artifact_id,
                input_digest="sha256:" + "5" * 64,
                action_digest="sha256:" + "6" * 64,
                consent_kind="calendar_read",
                read_consent_id=consent_id,
                consent_revision=1,
                consent_digest="sha256:" + "4" * 64,
                schedule_idempotency_key="continuity-schedule-key",
                schedule_request_digest="sha256:" + "7" * 64,
                cadence_kind="hourly",
                timezone="UTC",
                binding_revision=1,
                expires_at=now + timedelta(days=1),
                state="paused",
            )
        )

    ids = {
        "owner_session_id": owner_session_id,
        "goal_id": goal_id,
        "artifact_id": artifact["artifact_id"],
        "task_id": task_id,
        "routine_id": routine_id,
        "connection_id": connection_id,
        "consent_id": consent_id,
        "scheduled_job_id": scheduled_job_id,
        "binding_id": binding_id,
    }
    before_rows = await _persisted_owner_snapshot(async_db, ids)
    before = await _read_owner_surfaces(client, task_id=task_id, artifact_id=artifact["artifact_id"])
    assert [item["id"] for item in before["goals"]] == [goal_id]
    assert before["task"]["task"]["task_id"] == task_id
    assert before["input_artifact"]["artifact_id"] == artifact["artifact_id"]
    assert [item["id"] for item in before["routines"]["routines"]] == [routine_id]
    assert [item["connection_id"] for item in before["connections"]["connections"]] == [connection_id]
    assert [item["binding_id"] for item in before["schedules"]["bindings"]] == [binding_id]
    assert before["schedules"]["bindings"][0]["consent_id"] == consent_id

    for _ in range(2):
        refreshed = await client.post("/api/auth/refresh", headers=ORIGIN_HEADERS)
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["session_id"] == owner_session_id
        assert refreshed.json()["ownership_continuity"] == "stable"
        token = refreshed.cookies.get(settings.operator_auth_cookie_name)
        assert token
        client.cookies.set(settings.operator_auth_cookie_name, token)

    session_readback = await client.get("/api/auth/session")
    assert session_readback.status_code == 200
    assert session_readback.json()["session_id"] == owner_session_id
    assert session_readback.json()["ownership_continuity"] == "stable"
    after = await _read_owner_surfaces(client, task_id=task_id, artifact_id=artifact["artifact_id"])
    assert after == before
    after_rows = await _persisted_owner_snapshot(async_db, ids)
    assert after_rows == before_rows

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as foreign:
        foreign_login = await _login(foreign)
        assert foreign_login["session_id"] != owner_session_id
        foreign_goals = await foreign.get("/api/goals")
        foreign_tasks = await foreign.get(f"/api/work-board/tasks/{task_id}")
        foreign_artifact = await foreign.get(f"/api/work-board/input-artifacts/{artifact['artifact_id']}")
        foreign_routines = await foreign.get("/api/capabilities/routines")
        foreign_connections = await foreign.get("/api/calendar/connections")
        foreign_schedules = await foreign.get("/api/governed-schedules")

    assert foreign_goals.status_code == 200 and foreign_goals.json() == []
    assert foreign_tasks.status_code in {403, 404}
    assert foreign_artifact.status_code in {403, 404}
    assert foreign_routines.status_code == 200 and foreign_routines.json() == {"routines": []}
    assert foreign_connections.status_code == 200 and foreign_connections.json() == {"connections": []}
    assert foreign_schedules.status_code == 200 and foreign_schedules.json() == {"bindings": []}
    _assert_no_private_response_fields(
        {
            "goals": foreign_goals.json(),
            "tasks": foreign_tasks.json(),
            "artifacts": foreign_artifact.json(),
            "routines": foreign_routines.json(),
            "connections": foreign_connections.json(),
            "schedules": foreign_schedules.json(),
        }
    )
