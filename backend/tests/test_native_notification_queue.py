"""Focused durable-outbox coverage for the built-in native notification path."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from src.db.models import NativeNotificationOutbox, QueuedInsight
from src.observer.native_notification_queue import (
    MAX_BODY_CHARS,
    NativeNotificationConflictError,
    NativeNotificationQueue,
    native_notification_queue,
)


def _enqueue_kwargs(**overrides):
    values = {
        "intervention_id": "intervention-1",
        "title": "Seraph update",
        "body": "Review the next step on your active goal.",
        "intervention_type": "advisory",
        "urgency": 3,
    }
    values.update(overrides)
    return values


@pytest.mark.asyncio
async def test_outbox_survives_queue_reconstruction_and_deduplicates(async_db):
    first_queue = NativeNotificationQueue()
    first = await first_queue.enqueue(**_enqueue_kwargs(), idempotency_key="native:stable-1")

    # A new queue object models a daemon/backend restart. The canonical DB row
    # remains the source of truth rather than process-local memory.
    restarted_queue = NativeNotificationQueue()
    reread = await restarted_queue.list()
    duplicate = await restarted_queue.enqueue(
        **_enqueue_kwargs(),
        idempotency_key="native:stable-1",
    )

    assert [item.id for item in reread] == [first.id]
    assert duplicate.id == first.id
    assert duplicate.attempt_count == 0
    assert await restarted_queue.count() == 1

    with pytest.raises(NativeNotificationConflictError):
        await restarted_queue.enqueue(
            **_enqueue_kwargs(body="A different authorized payload."),
            idempotency_key="native:stable-1",
        )


@pytest.mark.asyncio
async def test_idempotency_key_is_atomic_across_queue_instances(async_db):
    first_queue = NativeNotificationQueue()
    second_queue = NativeNotificationQueue()
    results = await asyncio.gather(
        first_queue.enqueue(**_enqueue_kwargs(), idempotency_key="native:race-1"),
        second_queue.enqueue(**_enqueue_kwargs(), idempotency_key="native:race-1"),
    )

    assert results[0].id == results[1].id
    assert await first_queue.count() == 1


@pytest.mark.asyncio
async def test_bundle_handoff_deletes_source_in_same_transaction(async_db):
    source = QueuedInsight(
        id="queued-source-1",
        content="Queued guardian update",
        intervention_type="advisory",
        urgency=3,
    )
    async with async_db() as db:
        db.add(source)

    queue = NativeNotificationQueue()
    first = await queue.enqueue(
        **_enqueue_kwargs(intervention_id=None, body="Queued guardian update"),
        idempotency_key="native:bundle-handoff-1",
        source_insight_ids=[source.id],
    )
    async with async_db() as db:
        assert (
            await db.execute(
                select(QueuedInsight).where(QueuedInsight.id == source.id)
            )
        ).scalar_one_or_none() is None

    duplicate = await queue.enqueue(
        **_enqueue_kwargs(intervention_id=None, body="Queued guardian update"),
        idempotency_key="native:bundle-handoff-1",
        source_insight_ids=[source.id],
    )
    assert duplicate.id == first.id


@pytest.mark.asyncio
async def test_claim_requires_fence_and_records_attempt(async_db):
    queue = NativeNotificationQueue(max_attempts=2, lease_seconds=60, ttl_seconds=300)
    notification = await queue.enqueue(**_enqueue_kwargs(), idempotency_key="native:claim-1")

    claimed = await queue.claim_next(worker_id="daemon-a")
    assert claimed is not None
    assert claimed.id == notification.id
    assert claimed.delivery_status == "claimed"
    assert claimed.attempt_count == 1
    assert claimed.fencing_token == 1

    # The same worker sees its active claim again; a competing worker cannot
    # steal it before lease expiry.
    assert (await queue.claim_next(worker_id="daemon-a")).fencing_token == 1
    assert await queue.claim_next(worker_id="daemon-b") is None

    assert await queue.ack(
        notification.id,
        fencing_token=claimed.fencing_token,
        worker_id="daemon-a",
    ) is False
    assert await queue.mark_display_attempted(
        notification.id,
        fencing_token=claimed.fencing_token,
        worker_id="daemon-a",
    ) is True
    assert await queue.ack(notification.id) is False
    assert await queue.ack(notification.id, fencing_token=999) is False
    assert await queue.ack(notification.id, fencing_token=claimed.fencing_token, worker_id="daemon-b") is False
    assert await queue.ack(notification.id, fencing_token=claimed.fencing_token, worker_id="daemon-a") is True
    assert await queue.ack(notification.id, fencing_token=claimed.fencing_token, worker_id="daemon-a") is False

    stored = await queue.get(notification.id)
    assert stored is not None
    assert stored.delivery_status == "delivered"
    assert await queue.count() == 0
    attempts = await queue.get_attempts(notification.id)
    assert len(attempts) == 1
    assert attempts[0]["status"] == "delivered"
    assert attempts[0]["fencing_token"] == claimed.fencing_token


@pytest.mark.asyncio
async def test_expired_lease_becomes_unknown_and_rejects_stale_ack(async_db):
    queue = NativeNotificationQueue(max_attempts=2, lease_seconds=60, ttl_seconds=300)
    notification = await queue.enqueue(**_enqueue_kwargs(), idempotency_key="native:lease-1")
    first = await queue.claim_next(worker_id="daemon-a")
    assert first is not None

    async with async_db() as db:
        row = (
            await db.execute(
                select(NativeNotificationOutbox).where(
                    NativeNotificationOutbox.id == notification.id
                )
            )
        ).scalar_one()
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    # Reconciliation must prevent replay or a legacy empty-body ACK after an
    # external display may already have happened.
    assert await queue.ack(notification.id) is False
    unknown = await queue.get(notification.id)
    assert unknown is not None
    assert unknown.delivery_status == "unknown"
    assert await NativeNotificationQueue(max_attempts=2, lease_seconds=60, ttl_seconds=300).claim_next(
        worker_id="daemon-b"
    ) is None
    assert await queue.fail(
        notification.id,
        reason="stale worker",
        fencing_token=first.fencing_token,
        worker_id="daemon-a",
    ) is False
    assert await queue.ack(notification.id, fencing_token=first.fencing_token, worker_id="daemon-a") is False
    attempts = await queue.get_attempts(notification.id)
    assert [item["status"] for item in attempts] == ["unknown"]


@pytest.mark.asyncio
async def test_failure_becomes_unknown_without_automatic_replay(async_db):
    queue = NativeNotificationQueue(max_attempts=2, lease_seconds=60, ttl_seconds=300)
    notification = await queue.enqueue(**_enqueue_kwargs(), idempotency_key="native:retry-1")

    first = await queue.claim_next(worker_id="daemon")
    assert first is not None
    assert await queue.fail(
        notification.id,
        reason="osascript unavailable",
        fencing_token=first.fencing_token,
        worker_id="daemon",
    ) is True
    failed = await queue.get(notification.id)
    assert failed is not None
    assert failed.delivery_status == "unknown"
    assert await queue.claim_next(worker_id="daemon") is None
    assert await queue.retry(notification.id) is False
    assert await queue.count() == 0

    attempts = await queue.get_attempts(notification.id)
    assert [item["status"] for item in attempts] == ["unknown"]


@pytest.mark.asyncio
async def test_dismiss_cancels_and_same_key_cannot_replay(async_db):
    queue = NativeNotificationQueue()
    notification = await queue.enqueue(**_enqueue_kwargs(), idempotency_key="native:cancel-1")

    dismissed = await queue.dismiss(notification.id)
    assert dismissed is not None
    assert dismissed.delivery_status == "cancelled"
    assert await queue.count() == 0
    assert await queue.peek() is None
    assert await queue.ack(notification.id) is False

    replay = await queue.enqueue(**_enqueue_kwargs(), idempotency_key="native:cancel-1")
    assert replay.id == notification.id
    assert replay.delivery_status == "cancelled"
    assert await queue.count() == 0


@pytest.mark.asyncio
async def test_expired_queued_notification_is_failed_without_replay(async_db):
    queue = NativeNotificationQueue()
    notification = await queue.enqueue(**_enqueue_kwargs(), idempotency_key="native:expiry-1")

    async with async_db() as db:
        row = (
            await db.execute(
                select(NativeNotificationOutbox).where(
                    NativeNotificationOutbox.id == notification.id
                )
            )
        ).scalar_one()
        row.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    assert await queue.peek() is None
    expired = await queue.get(notification.id)
    assert expired is not None
    assert expired.delivery_status == "failed"
    assert await queue.count() == 0


@pytest.mark.asyncio
async def test_payload_limits_and_failure_receipts_redact_secrets(async_db):
    queue = NativeNotificationQueue()
    with pytest.raises(ValueError, match="body exceeds"):
        await queue.enqueue(
            **_enqueue_kwargs(body="x" * (MAX_BODY_CHARS + 1)),
            idempotency_key="native:oversized-1",
        )
    with pytest.raises(ValueError, match="control characters"):
        await queue.enqueue(
            **_enqueue_kwargs(title="Seraph\x00update"),
            idempotency_key="native:malformed-1",
        )

    notification = await queue.enqueue(**_enqueue_kwargs(), idempotency_key="native:secret-1")
    claimed = await queue.claim_next(worker_id="daemon")
    assert claimed is not None
    assert await queue.fail(
        notification.id,
        reason="authorization=super-secret-value",
        fencing_token=claimed.fencing_token,
        worker_id="daemon",
    ) is True
    attempts = await queue.get_attempts(notification.id)
    assert "super-secret-value" not in (attempts[0]["error_code"] or "")
    assert "[redacted]" in (attempts[0]["error_code"] or "")


@pytest.mark.asyncio
async def test_observer_api_requires_delivery_fence_and_records_failure(async_db, client):
    await native_notification_queue.clear()
    notification = await native_notification_queue.enqueue(
        **_enqueue_kwargs(),
        idempotency_key="native:http-fence-1",
    )

    worker_id = "test-daemon"
    poll_response = await client.get(
        "/api/observer/notifications/next",
        params={"worker_id": worker_id},
        headers={"X-Seraph-Daemon-Id": worker_id},
    )
    assert poll_response.status_code == 200
    polled = poll_response.json()["notification"]
    assert polled["id"] == notification.id
    assert polled["delivery_status"] == "claimed"
    fencing_token = polled["fencing_token"]

    ack_url = f"/api/observer/notifications/{notification.id}/ack"
    assert (await client.post(ack_url, json={"worker_id": worker_id, "fencing_token": fencing_token + 1}, headers={"X-Seraph-Daemon-Id": worker_id})).json() == {
        "acked": False
    }
    assert (await client.post(ack_url, json={"worker_id": "other-daemon", "fencing_token": fencing_token}, headers={"X-Seraph-Daemon-Id": "other-daemon"})).json() == {
        "acked": False
    }
    assert (await client.post(
        f"/api/observer/notifications/{notification.id}/display-attempted",
        json={"worker_id": worker_id, "fencing_token": fencing_token},
        headers={"X-Seraph-Daemon-Id": worker_id},
    )).json() == {"display_attempted": True}
    assert (await client.post(ack_url, json={"worker_id": worker_id, "fencing_token": fencing_token}, headers={"X-Seraph-Daemon-Id": worker_id})).json() == {
        "acked": True
    }

    failed_notification = await native_notification_queue.enqueue(
        **_enqueue_kwargs(body="The daemon should retry this."),
        idempotency_key="native:http-failure-1",
    )
    failed_poll = await client.get(
        "/api/observer/notifications/next",
        params={"worker_id": worker_id},
        headers={"X-Seraph-Daemon-Id": worker_id},
    )
    failed_payload = failed_poll.json()["notification"]
    assert failed_payload["id"] == failed_notification.id
    fail_url = f"/api/observer/notifications/{failed_notification.id}/fail"
    assert (await client.post(fail_url, json={"worker_id": "other-daemon", "fencing_token": failed_payload["fencing_token"]}, headers={"X-Seraph-Daemon-Id": "other-daemon"})).json() == {
        "failed": False
    }
    assert (await client.post(
        f"/api/observer/notifications/{failed_notification.id}/display-attempted",
        json={"worker_id": worker_id, "fencing_token": failed_payload["fencing_token"]},
        headers={"X-Seraph-Daemon-Id": worker_id},
    )).json() == {"display_attempted": True}
    assert (
        await client.post(
            fail_url,
            json={
                "reason": "authorization=hidden-value",
                "worker_id": worker_id,
                "fencing_token": failed_payload["fencing_token"],
            },
            headers={"X-Seraph-Daemon-Id": worker_id},
        )
    ).json() == {"failed": True}
    assert (await native_notification_queue.get(failed_notification.id)).delivery_status == "unknown"
    attempts = await native_notification_queue.get_attempts(failed_notification.id)
    assert attempts[0]["error_code"] == "authorization=[redacted]"

    await native_notification_queue.clear()
