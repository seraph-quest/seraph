"""Actual opportunity intents stay bound to the authenticated original Root.

Auth, source publication, native assessment, artifacts and SQLite are real;
only the existing source/provider HTTP boundaries are intercepted.
"""

from contextlib import asynccontextmanager
from datetime import timedelta
import json

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select

from config.settings import settings
from src.api import auth, observer
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import (
    Goal,
    GuardianIntervention,
    GuardianOpportunity,
    NativeNotificationDeliveryAttempt,
    NativeNotificationOutbox,
)
from src.guardian.opportunities import now
from tests.test_guardian_opportunity_notifications import _actual_notification_intent
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth


WORKER = "same-caller-chosen-daemon"
HEADERS = {"X-Seraph-Daemon-Id": WORKER}


@pytest_asyncio.fixture
async def notification_scene(accounting_db, real_auth, monkeypatch):
    cookies = {}
    sessions, owner, opportunity, notification_id, queue = await _actual_notification_intent(
        accounting_db, monkeypatch, auth_cookies=cookies,
    )
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(observer.router, prefix="/api")
    monkeypatch.setattr(observer, "native_notification_queue", queue)
    return sessions, owner, opportunity, notification_id, queue, app, cookies


@asynccontextmanager
async def authenticated_client(scene, *, foreign_scope=None):
    _, owner, _, _, _, app, cookies = scene
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"origin": "http://localhost:3001"},
    ) as client:
        if foreign_scope is None:
            client.cookies.update(cookies)
            authenticated = (await client.get("/api/auth/session")).json()
            assert authenticated["principal_id"] == owner["principal_id"]
            assert authenticated["session_id"] == owner["session_id"]
        else:
            if foreign_scope == "root":
                name = auth._continuity_cookie_name()
                client.cookies.set(name, cookies[name])
            login = await client.post(
                "/api/auth/login", json={"password": "research-vertical-private-secret"},
            )
            assert login.status_code == 200, login.text
            authenticated = login.json()
            assert authenticated["session_id"] != owner["session_id"]
            assert authenticated["principal_id"] != owner["principal_id"]
            if foreign_scope == "root":
                assert authenticated["operator_identity_id"] == owner["operator_identity_id"]
            else:
                assert authenticated["operator_identity_id"] != owner["operator_identity_id"]
        yield client, authenticated


async def delivery_snapshot(scene):
    sessions, _, opportunity, notification_id, _, _, _ = scene
    async with sessions() as db:
        intent = await db.get(NativeNotificationOutbox, notification_id)
        current = await db.get(GuardianOpportunity, opportunity.id)
        goal = await db.get(Goal, intent.goal_id)
        intervention = await db.get(GuardianIntervention, intent.intervention_id)
        attempts = (await db.execute(select(NativeNotificationDeliveryAttempt).where(
            NativeNotificationDeliveryAttempt.notification_id == notification_id,
        ).order_by(NativeNotificationDeliveryAttempt.attempt_index))).scalars().all()
        intent_count = (await db.execute(select(func.count()).select_from(
            NativeNotificationOutbox,
        ).where(NativeNotificationOutbox.intervention_type == "opportunity"))).scalar_one()
        return {
            "outbox": {column.name: getattr(intent, column.name) for column in intent.__table__.columns},
            "attempts": [{column.name: getattr(attempt, column.name)
                for column in attempt.__table__.columns} for attempt in attempts],
            "opportunity_intent_count": intent_count,
            "opportunity": {key: getattr(current, key) for key in (
                "id", "status", "revision", "goal_revision", "policy_revision", "dedupe_key",
                "job_id", "expires_at", "assessment_deadline_at",
            )},
            "goal_policy": (goal.revision, goal.guardian_policy_revision, goal.guardian_policy_json),
            "intervention": (intervention.delivery_status, intervention.feedback_type, intervention.latest_outcome),
        }


def receipt(record_property, label, response, before, after):
    # JUnit receipts retain actual HTTP bodies and SQL state without bearer or
    # continuity cookies. The retained validation directory is private.
    record_property(label, json.dumps({
        "status_code": response.status_code, "raw_http_body": response.text,
        "before": before, "after": after,
    }, default=str, sort_keys=True))


def assert_private_metadata_absent(response, scene):
    _, owner, opportunity, notification_id, _, _, _ = scene
    for value in (notification_id, opportunity.id, opportunity.goal_id,
            owner["principal_id"], owner["session_id"], "Guardian opportunity"):
        assert value not in response.text, response.text


@pytest.mark.parametrize("foreign_scope", ["owner", "root"])
async def test_actual_daemon_poll_requires_authenticated_original_root(
    notification_scene, foreign_scope, record_property,
):
    scene = notification_scene
    before = await delivery_snapshot(scene)
    assert before["outbox"]["status"] == "queued"
    assert before["outbox"]["attempt_count"] == before["outbox"]["fencing_token"] == 0
    async with authenticated_client(scene, foreign_scope=foreign_scope) as (client, _):
        denied = await client.get("/api/observer/notifications/next",
            params={"worker_id": WORKER}, headers=HEADERS)
    after = await delivery_snapshot(scene)
    receipt(record_property, "denied_poll", denied, before, after)
    assert denied.status_code == 200 and denied.json() == {"notification": None}, denied.text
    assert_private_metadata_absent(denied, scene)
    assert after == before
    async with authenticated_client(scene) as (client, _):
        allowed = await client.get("/api/observer/notifications/next",
            params={"worker_id": WORKER}, headers=HEADERS)
    positive = await delivery_snapshot(scene)
    receipt(record_property, "original_root_poll", allowed, after, positive)
    assert allowed.status_code == 200 and allowed.json()["notification"]["id"] == scene[3]
    assert positive["outbox"]["status"] == "claimed"
    assert positive["outbox"]["attempt_count"] == positive["outbox"]["fencing_token"] == 1
    assert len(positive["attempts"]) == positive["opportunity_intent_count"] == 1
    assert positive["outbox"]["deadline_at"] == before["outbox"]["deadline_at"]


@pytest.mark.parametrize("foreign_scope", ["owner", "root"])
@pytest.mark.parametrize("action,response_key", [
    ("display-attempted", "display_attempted"), ("ack", "acked"), ("fail", "failed"),
])
async def test_actual_daemon_receipt_cannot_reuse_foreign_worker_and_fence(
    notification_scene, foreign_scope, action, response_key, record_property,
):
    scene = notification_scene
    async with authenticated_client(scene) as (client, _):
        claim = await client.get("/api/observer/notifications/next",
            params={"worker_id": WORKER}, headers=HEADERS)
        assert claim.status_code == 200, claim.text
        fence = claim.json()["notification"]["fencing_token"]
        body = {"worker_id": WORKER, "fencing_token": fence}
        if action == "ack":
            handoff = await client.post(f"/api/observer/notifications/{scene[3]}/display-attempted",
                json=body, headers=HEADERS)
            assert handoff.json() == {"display_attempted": True}, handoff.text
    before = await delivery_snapshot(scene)
    async with authenticated_client(scene, foreign_scope=foreign_scope) as (client, _):
        denied = await client.post(f"/api/observer/notifications/{scene[3]}/{action}",
            json=body, headers=HEADERS)
    after = await delivery_snapshot(scene)
    receipt(record_property, "denied_receipt", denied, before, after)
    assert denied.status_code == 200 and denied.json() == {response_key: False}, denied.text
    assert_private_metadata_absent(denied, scene)
    assert after == before
    async with authenticated_client(scene) as (client, _):
        allowed = await client.post(f"/api/observer/notifications/{scene[3]}/{action}",
            json=body, headers=HEADERS)
    positive = await delivery_snapshot(scene)
    receipt(record_property, "original_root_receipt", allowed, after, positive)
    assert allowed.status_code == 200 and allowed.json() == {response_key: True}, allowed.text
    assert positive["outbox"]["status"] == {
        "display-attempted": "display_attempted", "ack": "delivered", "fail": "unknown",
    }[action]
    assert positive["outbox"]["attempt_count"] == positive["outbox"]["fencing_token"] == 1
    assert len(positive["attempts"]) == positive["opportunity_intent_count"] == 1
    assert positive["outbox"]["deadline_at"] == before["outbox"]["deadline_at"]


async def test_actual_daemon_foreign_poll_and_audit_count_cannot_reconcile_original_intent(
    notification_scene, record_property,
):
    scene = notification_scene
    sessions, _, _, notification_id, queue, _, _ = scene
    # Negative expiry of an actual previously admitted intent. This does not
    # insert a success, grant, source proof, or model result.
    async with sessions() as db:
        intent = await db.get(NativeNotificationOutbox, notification_id)
        intent.deadline_at = now() - timedelta(seconds=1)
        db.add(intent)
    ordinary = await queue.enqueue(intervention_id=None, title="Ordinary recovery",
        body="Existing ambient notice", intervention_type="recovery", urgency=1,
        idempotency_key="daemon-privacy-ordinary")
    before = await delivery_snapshot(scene)
    async with authenticated_client(scene, foreign_scope="owner") as (client, _):
        response = await client.get("/api/observer/notifications/next",
            params={"worker_id": WORKER}, headers=HEADERS)
    after = await delivery_snapshot(scene)
    receipt(record_property, "foreign_ordinary_poll", response, before, after)
    assert response.status_code == 200 and response.json()["notification"]["id"] == ordinary.id
    assert_private_metadata_absent(response, scene)
    assert after == before
    async with authenticated_client(scene) as (client, _):
        own_worker = "original-root-expiry-check"
        response = await client.get("/api/observer/notifications/next",
            params={"worker_id": own_worker}, headers={"X-Seraph-Daemon-Id": own_worker})
    positive = await delivery_snapshot(scene)
    receipt(record_property, "original_root_expiry", response, after, positive)
    assert positive["outbox"]["status"] == "failed"
    assert positive["outbox"]["last_error"] == "deadline_expired"
    assert positive["outbox"]["attempt_count"] == positive["outbox"]["fencing_token"] == 0
    assert positive["opportunity_intent_count"] == 1


async def test_actual_opportunity_internal_missing_or_mismatched_caller_scope_is_denied(notification_scene):
    scene = notification_scene
    _, owner, _, notification_id, queue, _, _ = scene
    async with authenticated_client(scene, foreign_scope="root") as (_, alternate):
        invalid_scopes = [
            {}, {"owner_principal_id": owner["principal_id"]},
            {"operator_session_id": owner["session_id"]},
            {"owner_principal_id": owner["principal_id"], "operator_session_id": alternate["session_id"]},
            {"owner_principal_id": alternate["principal_id"], "operator_session_id": owner["session_id"]},
        ]
    original_scope = {"owner_principal_id": owner["principal_id"], "operator_session_id": owner["session_id"]}
    before = await delivery_snapshot(scene)
    for scope in invalid_scopes:
        assert await queue.claim_next(worker_id=WORKER, **scope) is None
        assert await queue.get(notification_id, **scope) is None
        assert await delivery_snapshot(scene) == before
    claim = await queue.claim_next(worker_id=WORKER, **original_scope)
    assert claim.id == notification_id
    assert await queue.mark_display_attempted(notification_id, worker_id=WORKER,
        fencing_token=claim.fencing_token, **original_scope)
    before = await delivery_snapshot(scene)
    for scope in invalid_scopes:
        for callback in (queue.mark_display_attempted, queue.ack, queue.fail):
            assert await callback(notification_id, worker_id=WORKER,
                fencing_token=claim.fencing_token, **scope) is False
            assert await delivery_snapshot(scene) == before
    assert await queue.ack(notification_id, worker_id=WORKER,
        fencing_token=claim.fencing_token, **original_scope)
    assert (await delivery_snapshot(scene))["outbox"]["status"] == "delivered"
