"""Actual finite-auth, file-SQLite approval timestamp boundary proof."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from tests.test_attention_recovery_journey import (
    async_db, authenticated_setup_operator, setup_workspace,
    explicit_test_external_permission, forbid_unintercepted_http,
    _prepare_actual_github_task,
)
from src.approval.repository import _approval_expiry, _approval_is_expired, _approval_timestamp


@pytest.mark.asyncio
async def test_real_sqlite_pending_approval_serializes_utc(client, async_db, setup_workspace, monkeypatch, tmp_path):
    _, task, job, auth = await _prepare_actual_github_task(client, async_db, setup_workspace, monkeypatch)
    approval_id = job["declared_authority"]["approval_id"]
    response = await client.get(f"/api/approvals/pending?approval_id={approval_id}&limit=1")
    assert response.status_code == 200 and len(response.json()) == 1
    row = response.json()[0]
    receipt = {key: row[key] for key in ("id", "status", "session_id", "conversation_id", "owner_principal_id", "operator_session_id", "expires_at", "created_at")}
    receipt.update(task_id=task["task_id"], job_id=job["job_id"], attempt_id=task["latest_attempt"]["attempt_id"], database=str(async_db.engine.url))
    target = tmp_path
    filename = "actual-pending-after-fix.json" if row["expires_at"].endswith("+00:00") else "actual-pending-before-fix.json"
    (target / filename).write_text(json.dumps(receipt, indent=2))
    for field in ("expires_at", "created_at"):
        stamp = datetime.fromisoformat(row[field])
        assert stamp.tzinfo == timezone.utc
    assert datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc)


@pytest.mark.parametrize("value", [
    datetime(2030, 1, 1), "2030-01-01T00:00:00",
    datetime(2030, 1, 1, 2, tzinfo=timezone(timedelta(hours=2))),
    "2030-01-01T02:00:00+02:00", "2030-01-01T00:00:00Z",
])
def test_approval_expiry_normalizes_datetime_and_iso_to_same_utc_instant(value):
    expected = datetime(2030, 1, 1, tzinfo=timezone.utc)
    actual = _approval_expiry(value)
    assert actual == expected and actual.tzinfo == timezone.utc
    assert _approval_timestamp(value) == expected.isoformat()
    assert _approval_is_expired(value, now=expected)
    assert not _approval_is_expired(value, now=expected - timedelta(seconds=1))


@pytest.mark.parametrize("value", ["malformed", "", float("inf"), float("nan"), {}])
def test_malformed_expiry_remains_fail_closed(value):
    assert _approval_expiry(value) is None
    assert _approval_is_expired(value)
    assert _approval_timestamp(value) is None


def test_absent_timestamp_stays_absent():
    assert _approval_expiry(None) is None
    assert _approval_timestamp(None) is None
