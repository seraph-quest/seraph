"""Cleanup journal checks with disposable canonical rows; no resource contacts."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import update

from src.db.models import Goal, OperatorIdentity, OperatorSession, Session, WorkflowRunState
from src.workflows.job_runtime import (
    ConnectedSourcePhysicalCleanupOwner, DurableJobError, DurableJobLeaseError, NativePhysicalCleanupBinding,
    NativePhysicalCleanupProof, _canonical, _digest, durable_job_repository as jobs,
    native_physical_cleanup_binding_payload,
)


@pytest_asyncio.fixture
async def original_resource(async_db):
    now = datetime.now(timezone.utc)
    authority = {"principal": "original-principal", "session_id": "original-session",
                 "goal_id": "cleanup-goal", "goal_revision": 1,
                 "capability_id": "mail.messages.read", "connection_id": "connection"}
    witness = {"boot_id": "f2310181-26ae-49c2-8c11-75d161b52ab1", "runtime_nonce": "a"*32,
               "pid":123, "pid_start_ticks":"456", "pid_namespace":789,
               "connection_id": "connection", "scope_digest": "b"*64, "original_cursor_revision": 1}
    binding = NativePhysicalCleanupBinding(
        "physical-job", 4, "original-principal", "original-session", "original-session",
        "input-digest", _digest(authority), "request-fingerprint", 1,
        "original-worker", 2, "connection-sync:connection", _digest(witness))
    payload = {"binding": native_physical_cleanup_binding_payload(binding), "witness": witness}
    async with async_db() as db:
        db.add(OperatorIdentity(id="stable-operator"))
        db.add(Session(id="original-session"))
        db.add(OperatorSession(id="original-session", principal_id="original-principal",
            token_hash="original-token", operator_identity_id="stable-operator",
            revoked_at=now, idle_expires_at=now-timedelta(hours=1), absolute_expires_at=now-timedelta(hours=1)))
        db.add(OperatorSession(id="current-session", principal_id="new-root-principal",
            token_hash="current-token", operator_identity_id="stable-operator",
            idle_expires_at=now+timedelta(hours=1), absolute_expires_at=now+timedelta(hours=1)))
        db.add(Goal(id="cleanup-goal", title="Revoked original Goal", owner_principal_id="original-principal",
                    owner_session_id="original-session", revision=2))
        db.add(WorkflowRunState(id="physical-row", run_identity=binding.job_id,
            root_run_identity=binding.job_id, workflow_name="physical-cleanup-fixture",
            session_id=binding.original_session_id, operator_session_id=binding.original_operator_session_id,
            owner_kind="user", owner_principal_id=binding.original_owner_principal_id,
            job_kind="connection_source_sync", capability_version="connection-sync-v1",
            input_digest=binding.input_digest, authority_digest=binding.authority_digest,
            declared_authority_json=_canonical(authority), run_fingerprint=binding.run_fingerprint,
            goal_id="cleanup-goal", goal_revision=1, status="unknown_external_effect", revision=4,
            attempt_count=1, fencing_token=2, lease_owner=None,
            deadline_at=now-timedelta(minutes=1), resource_claims_json='["connection-sync:connection"]',
            effect_receipts_json='[{"effect_id":"remote-contact","status":"unknown"}]',
            checkpoint_receipts_json=_canonical([{"checkpoint_id":"native-physical-resource-reservation",
                "fencing_token":2, "safe":True, "payload":payload, "state_digest":_digest(payload)}])))
    owner = SimpleNamespace(session_id="current-session", principal_id="new-root-principal")
    return async_db, binding, owner


async def cleanup(binding, owner, proof=None, **kwargs):
    callback = AsyncMock(return_value=proof or NativePhysicalCleanupProof(binding.witness_digest, "owned_positive_close"))
    result = await jobs.record_native_physical_cleanup(binding, current_owner=owner,
        authenticated_token_hash="current-token", proof_kind="owned_positive_close",
        cleanup_owner=ConnectedSourcePhysicalCleanupOwner(callback, kwargs.pop("release_pointer", AsyncMock())), **kwargs)
    return result, callback


@pytest.mark.asyncio
async def test_same_stable_operator_cleanup_after_original_root_goal_and_deadline_invalid(original_resource):
    db_factory, binding, owner = original_resource
    release = AsyncMock()
    result, callback = await cleanup(binding, owner, release_pointer=release)
    callback.assert_awaited_once()
    release.assert_awaited_once()
    assert set(result) == {"job_id", "revision", "status", "receipt"}
    assert result["status"] == "unknown_external_effect" and result["revision"] == 5
    async with db_factory() as db:
        run = await db.get(WorkflowRunState, "physical-row")
        assert run.status == "unknown_external_effect" and run.lease_owner is None
        assert run.effect_receipts_json == '[{"effect_id":"remote-contact","status":"unknown"}]'
        assert run.goal_revision == 1 and run.fencing_token == 2 and run.attempt_count == 1
        assert json.loads(run.checkpoint_receipts_json)[-1]["payload"]["scope"] == "physical_cleanup_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"expected_revision":3}, {"input_digest":"other"}, {"original_session_id":"other"},
    {"original_operator_session_id":"other"}, {"attempt_count":2}, {"fencing_token":3},
    {"lease_owner":"other"}, {"resource_claim":"browser-task-lane"}, {"witness_digest":"other"},
])
async def test_immutable_binding_mismatch_rejects_before_physical_callback(original_resource, changes):
    _, binding, owner = original_resource
    callback = AsyncMock()
    with pytest.raises(DurableJobError):
        await jobs.record_native_physical_cleanup(replace(binding, **changes), current_owner=owner,
            authenticated_token_hash="current-token", proof_kind="owned_positive_close",
            cleanup_owner=ConnectedSourcePhysicalCleanupOwner(callback, AsyncMock()))
    callback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["wrong_identity", "missing_original", "revoked_current", "wrong_token", "missing_reservation", "transferred_lease", "missing_identity", "revoked_identity", "unsafe_reservation", "bad_state_digest", "succeeded_status"])
async def test_unavailable_auth_or_reservation_fails_closed(original_resource, change):
    db_factory, binding, owner = original_resource
    async with db_factory() as db:
        if change == "missing_original":
            await db.delete(await db.get(OperatorSession, "original-session"))
        elif change in {"wrong_identity", "revoked_current"}:
            session = await db.get(OperatorSession, "current-session")
            if change == "wrong_identity": session.operator_identity_id = "different-operator"
            else: session.revoked_at = datetime.now(timezone.utc)
        elif change == "missing_reservation":
            (await db.get(WorkflowRunState, "physical-row")).checkpoint_receipts_json = "[]"
        elif change == "transferred_lease":
            (await db.get(WorkflowRunState, "physical-row")).lease_owner = "other-worker"
        elif change == "missing_identity":
            await db.delete(await db.get(OperatorIdentity, "stable-operator"))
        elif change == "revoked_identity":
            (await db.get(OperatorIdentity, "stable-operator")).revoked_at = datetime.now(timezone.utc)
        elif change == "succeeded_status":
            (await db.get(WorkflowRunState, "physical-row")).status = "succeeded"
        elif change in {"unsafe_reservation", "bad_state_digest"}:
            run = await db.get(WorkflowRunState, "physical-row")
            history = json.loads(run.checkpoint_receipts_json)
            history[0]["safe" if change == "unsafe_reservation" else "state_digest"] = False
            run.checkpoint_receipts_json = _canonical(history)
    callback = AsyncMock()
    with pytest.raises(DurableJobError):
        await jobs.record_native_physical_cleanup(binding, current_owner=owner,
            authenticated_token_hash="wrong-token" if change == "wrong_token" else "current-token",
            proof_kind="owned_positive_close", cleanup_owner=ConnectedSourcePhysicalCleanupOwner(callback, AsyncMock()))
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_unproven_cleanup_and_pointer_failure_preserve_unknown_journal(original_resource):
    db_factory, binding, owner = original_resource
    with pytest.raises(DurableJobError):
        await cleanup(binding, owner, proof=NativePhysicalCleanupProof(binding.witness_digest, "lease_expired"))
    with pytest.raises(RuntimeError):
        await cleanup(binding, owner, release_pointer=AsyncMock(side_effect=RuntimeError("pointer changed")))
    async with db_factory() as db:
        run = await db.get(WorkflowRunState, "physical-row")
        assert run.revision == 4 and len(json.loads(run.checkpoint_receipts_json)) == 1


@pytest.mark.asyncio
async def test_cas_changed_during_owner_verification_never_releases_pointer(original_resource):
    db_factory, binding, owner = original_resource
    async def competing_write(db, run, reservation):
        await db.execute(update(WorkflowRunState).where(WorkflowRunState.id == run.id).values(revision=5))
        return NativePhysicalCleanupProof(binding.witness_digest, "owned_positive_close")
    release = AsyncMock()
    with pytest.raises(DurableJobLeaseError):
        await jobs.record_native_physical_cleanup(binding, current_owner=owner,
            authenticated_token_hash="current-token", proof_kind="owned_positive_close",
            cleanup_owner=ConnectedSourcePhysicalCleanupOwner(competing_write, release))
    release.assert_not_awaited()
    async with db_factory() as db:
        assert (await db.get(WorkflowRunState, "physical-row")).revision == 4


@pytest.mark.asyncio
async def test_exact_cleanup_replay_never_calls_owner_or_clears_new_pointer(original_resource):
    _, binding, owner = original_resource
    await cleanup(binding, owner)
    callback, release = AsyncMock(), AsyncMock()
    result = await jobs.record_native_physical_cleanup(replace(binding, expected_revision=5), current_owner=owner,
        authenticated_token_hash="current-token", proof_kind="owned_positive_close",
        cleanup_owner=ConnectedSourcePhysicalCleanupOwner(callback, release))
    assert result["receipt"]["deduped"] is True and result["revision"] == 5
    callback.assert_not_awaited(); release.assert_not_awaited()
    with pytest.raises(DurableJobError):
        await jobs.record_native_physical_cleanup(replace(binding, expected_revision=5), current_owner=owner,
            authenticated_token_hash="current-token", proof_kind="linux_boot_changed",
            cleanup_owner=ConnectedSourcePhysicalCleanupOwner(callback, release))
