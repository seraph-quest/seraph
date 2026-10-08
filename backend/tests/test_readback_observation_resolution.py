"""Exact diagnostic settlement in the existing successful-readback CAS."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from tests.test_durable_job_runtime import _spec
from src.workflows.job_runtime import (
    DurableJobLeaseError, _digest, _job_has_unsafe_effects,
    _resolve_readback_observations, durable_job_repository,
)


def receipts():
    parent = {"effect_id": "github:operation", "effect_type": "github_publication", "receipt_kind": "readback", "status": "succeeded", "target_path": "/repos/example/repo/issues", "target_digest": "a" * 64, "approval_id": "approval", "adapter_idempotency_key": "operation", "readback_id": "verified-operation", "verified_at": "2026-10-02T12:00:02Z", "content_sha256": "b" * 64, "details": {"verified": True}, "reconciled": True, "reconciliation_status": "resolved"}
    observation = {**parent, "effect_id": f"github:operation:readback:{_digest({'status': 'unknown', 'target_path': parent['target_path']})[:16]}", "original_effect_id": "github:operation", "status": "unknown", "recorded_at": "2026-10-02T12:00:01Z", "details": {"verified": False, "readback_observation_only": True}}
    for field in ("reconciled", "reconciliation_status", "verified_at", "readback_id", "content_sha256"):
        observation.pop(field)
    return parent, observation


def test_resolution_preserves_original_observation_and_durable_parent_link():
    parent, observation = receipts()
    assert _job_has_unsafe_effects([observation, parent])
    result = _resolve_readback_observations([observation, parent], parent)
    assert not _job_has_unsafe_effects(result)
    assert result[0]["status"] == "unknown" and result[0]["details"] == observation["details"]
    assert result[0]["resolution_parent_effect_id"] == parent["effect_id"]
    assert result[0]["resolution_readback_id"] == parent["readback_id"]
    assert "reconciled" not in observation


@pytest.mark.parametrize("change", [
    {"original_effect_id": "foreign-operation"}, {"effect_id": "forged-observation"},
    {"effect_type": "another-capability"}, {"target_path": "/foreign"}, {"target_digest": "c" * 64},
    {"approval_id": "foreign"}, {"adapter_idempotency_key": "foreign"},
    {"recorded_at": "2026-10-02T12:00:03Z"}, {"recorded_at": "invalid"},
    {"receipt_kind": "effect"}, {"details": {"verified": False}},
    {"details": {"verified": False, "readback_observation_only": True, "unknown_cost_outstanding": True}},
    {"details": {"verified": False, "readback_observation_only": True, "receipt": {"reconciliation_required": True}}},
])
def test_foreign_malformed_stale_and_independent_liabilities_remain_unsafe(change):
    parent, observation = receipts(); observation.update(change)
    result = _resolve_readback_observations([observation, parent], parent)
    assert result[0] == observation and _job_has_unsafe_effects(result)


@pytest.mark.parametrize("condition", ["missing", "duplicate", "unverified", "empty-digest", "no-time"])
def test_parent_must_be_unique_exact_verified_receipt(condition):
    parent, observation = receipts()
    if condition == "unverified": parent["details"] = {"verified": False}
    if condition == "empty-digest": parent["target_digest"] = ""
    if condition == "no-time": parent.pop("verified_at")
    ledger = [observation] + ([] if condition == "missing" else [parent, parent] if condition == "duplicate" else [parent])
    result = _resolve_readback_observations(ledger, parent)
    assert result[0] == observation and _job_has_unsafe_effects(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stale_revision", "zero_row_cas"])
async def test_failed_readback_cas_cannot_publish_diagnostic_resolution(async_db, monkeypatch, failure):
    repo = durable_job_repository
    admitted = await repo.admit_job(_spec(job_id=f"attention-cas-{failure}", dedupe_key=f"attention-cas-{failure}"))
    await repo.queue_job(admitted["job_id"])
    claimed = await repo.claim_job(admitted["job_id"], owner="attention-runner")
    binding = {"owner": "attention-runner", "fencing_token": claimed["lease"]["fencing_token"]}
    fields = {"effect_id": "effect", "effect_type": "destination_write", "target_path": "target", "target_digest": "a" * 64}
    intent = await repo.record_effect(admitted["job_id"], **fields, status="intent", **binding)
    observed = await repo.record_readback(admitted["job_id"], **fields, status="unknown", details={"verified": False}, expected_revision=intent["revision"], **binding)
    original_execute = AsyncSession.execute
    async def fail_cas(self, statement, *args, **kwargs):
        if isinstance(statement, Update) and any(getattr(key, "name", str(key)) == "effect_receipts_json" for key in statement._values):
            return SimpleNamespace(rowcount=0)
        return await original_execute(self, statement, *args, **kwargs)
    if failure == "zero_row_cas": monkeypatch.setattr(AsyncSession, "execute", fail_cas)
    with pytest.raises(DurableJobLeaseError):
        await repo.record_readback(admitted["job_id"], **fields, status="succeeded", details={"verified": True}, readback_id="exact-readback", verified_at=datetime.now(timezone.utc).isoformat(), content_sha256="b" * 64, expected_revision=intent["revision"] if failure == "stale_revision" else observed["revision"], **binding)
    reopened = await repo.get_job(admitted["job_id"])
    assert reopened["revision"] == observed["revision"] and reopened["effects"] == observed["effects"]
    assert _job_has_unsafe_effects(reopened["effects"])
