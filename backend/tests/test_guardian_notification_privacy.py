"""Metadata projection privacy tests, not native execution or delivery proof."""
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from src.db.models import Goal, NativeNotificationOutbox
from src.observer.native_notification_queue import NativeNotificationQueue, native_notification_queue


async def _metadata_rows(async_db, monkeypatch):
    from config.settings import settings
    from src.auth.service import create_session

    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "notification-privacy-test")
    first_token, first = await create_session()
    second_token, second = await create_session()
    stamp = datetime.now(timezone.utc)

    def row(label, owner=None, root=None, kind="opportunity", goal_bound=False):
        identifier = str(uuid4())
        goal_id = str(uuid4()) if goal_bound else None
        value = NativeNotificationOutbox(
            id=identifier, idempotency_key=f"privacy-fixture:{identifier}",
            payload_digest=hashlib.sha256(identifier.encode()).hexdigest(),
            intervention_id=f"opportunity:{uuid4()}" if kind == "opportunity" else None,
            owner_principal_id=owner, operator_session_id=root,
            goal_id=goal_id, goal_revision=1 if goal_id else None,
            title="Guardian opportunity" if kind == "opportunity" else "Legacy notification",
            body="A cited public-source judgment is ready in Guardian Inbox.",
            intervention_type=kind, urgency=2, status="queued",
            deadline_at=stamp + timedelta(minutes=10),
        )
        return value, Goal(id=goal_id, title=f"Privacy fixture {label}",
            owner_principal_id=owner, owner_session_id=root, revision=1) if goal_id else None

    entries = {
        "own": row("own", first.principal.principal_id, first.session_id, goal_bound=True),
        "foreign": row("foreign", second.principal.principal_id, second.session_id, goal_bound=True),
        "other_root": row("other-root", first.principal.principal_id, second.session_id, goal_bound=True),
        "unbound": row("unbound"),
        "owner_only": row("owner-only", first.principal.principal_id),
        "legacy": row("legacy", kind="advisory"),
        "legacy_null": row("legacy-null", kind=None),
    }
    # Queued metadata-only SQL fixtures exercise this read boundary. They do
    # not assert successful enqueue, assessed opportunity, proof or execution.
    async with async_db() as db:
        db.add_all([goal for _, goal in entries.values() if goal is not None])
        await db.flush()
        db.add_all([notification for notification, _ in entries.values()])
    return first_token, first, second_token, second, {key: value[0] for key, value in entries.items()}


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_opportunity_notification_list_requires_exact_owner_root(async_db, monkeypatch):
    _, first, _, second, rows = await _metadata_rows(async_db, monkeypatch)
    queue = NativeNotificationQueue()
    legacy_ids = {rows["legacy"].id, rows["legacy_null"].id}
    own_scope = {"owner_principal_id": first.principal.principal_id, "operator_session_id": first.session_id}
    assert {item.id for item in await queue.list(**own_scope)} == legacy_ids | {rows["own"].id}
    foreign_scope = {"owner_principal_id": second.principal.principal_id, "operator_session_id": second.session_id}
    assert {item.id for item in await queue.list(**foreign_scope)} == legacy_ids | {rows["foreign"].id}
    for scope in ({}, {"owner_principal_id": first.principal.principal_id},
                  {"operator_session_id": first.session_id},
                  {"owner_principal_id": first.principal.principal_id, "operator_session_id": "different-root"}):
        assert {item.id for item in await queue.list(**scope)} == legacy_ids
    async with async_db() as db:
        for value in rows.values():
            stored = await db.get(NativeNotificationOutbox, value.id)
            assert stored.status == "queued"
            assert stored.attempt_count == 0
            assert stored.fencing_token == 0


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_authenticated_opportunity_notifications_do_not_enter_generic_surfaces(async_db, client, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(native_notification_queue, "_lock", asyncio.Lock())
    first_token, first, second_token, second, rows = await _metadata_rows(async_db, monkeypatch)
    legacy_ids = {rows["legacy"].id, rows["legacy_null"].id}
    for token, owned in ((first_token, rows["own"]), (second_token, rows["foreign"])):
        client.cookies.set(settings.operator_auth_cookie_name, token)
        for surface in ("/api/observer/notifications", "/api/observer/continuity"):
            response = await client.get(surface)
            assert response.status_code == 200
            assert {item["id"] for item in response.json()["notifications"]} == legacy_ids | {owned.id}
            for label, value in rows.items():
                if label not in {"legacy", "legacy_null"} and value.id != owned.id:
                    assert value.id not in response.text
        for surface in ("/api/activity/ledger", "/api/operator/timeline", "/api/operator/continuity-graph"):
            response = await client.get(surface)
            assert response.status_code == 200
            for label, value in rows.items():
                if label not in {"legacy", "legacy_null"}:
                    assert value.id not in response.text


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("surface", ["/api/activity/ledger", "/api/operator/timeline", "/api/operator/continuity-graph"])
async def test_authenticated_generic_surface_excludes_opportunity_notifications(
    async_db, client, monkeypatch, surface,
):
    from config.settings import settings

    monkeypatch.setattr(native_notification_queue, "_lock", asyncio.Lock())
    first_token, _, _, _, rows = await _metadata_rows(async_db, monkeypatch)
    client.cookies.set(settings.operator_auth_cookie_name, first_token)
    response = await client.get(surface)
    assert response.status_code == 200
    assert rows["foreign"].id not in response.text
    assert rows["own"].id not in response.text
