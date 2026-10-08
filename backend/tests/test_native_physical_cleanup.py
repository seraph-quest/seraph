"""Cleanup journal checks with disposable canonical rows; no resource contacts."""
from dataclasses import replace
import asyncio
from datetime import datetime, timedelta, timezone
import json
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import update

from src.db.models import Goal, OperatorIdentity, OperatorSession, Session, WorkflowRunState
from src.workflows.job_runtime import (
    BrowserPhysicalCleanupOwner, ConnectedSourcePhysicalCleanupOwner, DurableJobError, DurableJobLeaseError, NativePhysicalCleanupBinding,
    NativePhysicalCleanupProof, _canonical, _digest, durable_job_repository as jobs,
    native_physical_cleanup_binding_payload,
    _bounded_checkpoint_receipts,
    native_external_effect_state,
)


@pytest_asyncio.fixture
async def original_resource(async_db):
    now = datetime.now(timezone.utc)
    authority = {"principal": "original-principal", "session_id": "original-session",
                 "goal_id": "cleanup-goal", "goal_revision": 1,
                 "capability_id": "mail.messages.read", "connection_id": "connection"}
    witness = {"platform":"linux", "boot_id": "f2310181-26ae-49c2-8c11-75d161b52ab1", "runtime_nonce": "a"*32,
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
@pytest.mark.parametrize("change", ["wrong_identity", "missing_original", "revoked_current", "wrong_token", "missing_reservation", "transferred_lease", "missing_identity", "revoked_identity", "unsafe_reservation", "bad_state_digest", "paused_status"])
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
        elif change == "paused_status":
            (await db.get(WorkflowRunState, "physical-row")).status = "paused"
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


async def make_precontact(original_resource):
    db_factory, binding, _ = original_resource
    now = datetime.now(timezone.utc)
    async with db_factory() as db:
        run = await db.get(WorkflowRunState, "physical-row")
        witness = json.loads(run.checkpoint_receipts_json)[0]["payload"]["witness"]
        run.checkpoint_receipts_json = "[]"
        run.status = "running"
        run.lease_owner = binding.lease_owner
        run.lease_expires_at = now+timedelta(minutes=2)
        run.deadline_at = now+timedelta(minutes=2)
        original = await db.get(OperatorSession, "original-session")
        original.revoked_at = None
        original.idle_expires_at = original.absolute_expires_at = now+timedelta(hours=1)
        (await db.get(Goal, "cleanup-goal")).revision = 1
    return binding, witness, SimpleNamespace(session_id="original-session", principal_id="original-principal")


@pytest.mark.asyncio
async def test_typed_precontact_reservation_preserves_exact_identity_and_cannot_replace(original_resource):
    db_factory, _, _ = original_resource
    binding, witness, owner = await make_precontact(original_resource)
    row = await jobs.reserve_native_physical_resource(binding, witness=witness,
        current_owner=owner, authenticated_token_hash="original-token")
    assert row["revision"] == 5
    async with db_factory() as db:
        receipt = json.loads((await db.get(WorkflowRunState, "physical-row")).checkpoint_receipts_json)[0]
        assert receipt["safe"] is True and receipt["payload"]["witness"] == witness
        assert receipt["payload"]["binding"]["fencing_token"] == 2
        assert receipt["state_digest"] == _digest(receipt["payload"])
    with pytest.raises(DurableJobError):
        await jobs.reserve_native_physical_resource(replace(binding, expected_revision=5), witness=witness,
            current_owner=owner, authenticated_token_hash="original-token")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["root", "goal", "deadline", "witness", "generic_writer"])
async def test_precontact_reservation_cannot_skip_normal_execution_fences(original_resource, invalid):
    db_factory, _, _ = original_resource
    binding, witness, owner = await make_precontact(original_resource)
    async with db_factory() as db:
        if invalid == "root":
            (await db.get(OperatorSession, "original-session")).revoked_at = datetime.now(timezone.utc)
        elif invalid == "goal":
            (await db.get(Goal, "cleanup-goal")).revision = 2
        elif invalid == "deadline":
            (await db.get(WorkflowRunState, "physical-row")).deadline_at = datetime.now(timezone.utc)-timedelta(seconds=1)
    with pytest.raises(DurableJobError):
        if invalid == "witness":
            malformed = {**witness, "operator_assertion":True}
            await jobs.reserve_native_physical_resource(replace(binding, witness_digest=_digest(malformed)),
                witness=malformed, current_owner=owner, authenticated_token_hash="original-token")
        elif invalid == "generic_writer":
            payload = {"binding":native_physical_cleanup_binding_payload(binding), "witness":witness}
            await jobs.record_checkpoint(binding.job_id, checkpoint_id="native-physical-resource-reservation",
                state=payload, checkpoint_payload=payload, owner=binding.lease_owner,
                fencing_token=binding.fencing_token, expected_revision=binding.expected_revision)
        else:
            await jobs.reserve_native_physical_resource(binding, witness=witness,
                current_owner=owner, authenticated_token_hash="original-token")
    async with db_factory() as db:
        assert (await db.get(WorkflowRunState, "physical-row")).checkpoint_receipts_json == "[]"


def test_physical_reservation_and_cleanup_survive_history_churn():
    special = [{"checkpoint_id":"native-physical-resource-reservation"}, {"checkpoint_id":"native-physical-resource-cleanup"}]
    retained = _bounded_checkpoint_receipts(special + [{"checkpoint_id":f"event:{i}"} for i in range(100)])
    assert len(retained) == 50 and all(item in retained for item in special)


def test_native_external_state_is_strict_redacted_and_separate_from_cleanup():
    run = SimpleNamespace(job_kind="connection_source_sync", effect_receipts_json='[{"status":"intent"}]')
    assert native_external_effect_state(run) == "unknown"
    run.effect_receipts_json = "[]"
    assert native_external_effect_state(run) == "none"
    run.effect_receipts_json = '[{"status":"succeeded"}]'
    assert native_external_effect_state(run) == "settled"
    run.effect_receipts_json = "malformed"
    with pytest.raises(DurableJobError): native_external_effect_state(run)


@pytest.mark.asyncio
async def test_source_requires_fixed_pointer_owner_adapter(original_resource):
    _, binding, owner = original_resource
    callback = AsyncMock()
    with pytest.raises(DurableJobError):
        await jobs.record_native_physical_cleanup(binding, current_owner=owner,
            authenticated_token_hash="current-token", proof_kind="owned_positive_close",
            cleanup_owner=BrowserPhysicalCleanupOwner(callback))
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_fixed_owner_requires_exact_lane_witness_and_proof_kind(original_resource):
    db_factory, binding, owner = original_resource
    witness = {"schema_version":4, "pid":123, "process_nonce":"c"*32, "context_nonce":"d"*32,
               "job_digest":hashlib.sha256(binding.job_id.encode()).hexdigest(), "root_path_digest":"e"*64,
               "root_device":1, "root_inode":2, "lock_device":1, "lock_inode":3,
               "boot_platform":"linux", "boot_session_id":"f2310181-26ae-49c2-8c11-75d161b52ab1", "positive_cleanup_required":True}
    async with db_factory() as db:
        run = await db.get(WorkflowRunState, "physical-row")
        authority = json.loads(run.declared_authority_json)
        authority["capability_id"] = "browser.interact.v2"
        run.job_kind = "browser_interact_v2"; run.capability_version = "2"
        run.resource_claims_json = '["browser-task-lane"]'
        run.declared_authority_json = _canonical(authority); run.authority_digest = _digest(authority)
        binding = replace(binding, authority_digest=run.authority_digest,
                          resource_claim="browser-task-lane", witness_digest=_digest(witness))
        payload = {"binding":native_physical_cleanup_binding_payload(binding), "witness":witness}
        run.checkpoint_receipts_json = _canonical([{"checkpoint_id":"native-physical-resource-reservation",
            "safe":True, "state_digest":_digest(payload), "payload":payload, "fencing_token":2}])
    callback = AsyncMock(return_value=NativePhysicalCleanupProof(binding.witness_digest, "owned_positive_close"))
    for adapter, kind in [(ConnectedSourcePhysicalCleanupOwner(callback, AsyncMock()), "owned_positive_close"),
                          (BrowserPhysicalCleanupOwner(callback), "positive_process_death")]:
        with pytest.raises(DurableJobError):
            await jobs.record_native_physical_cleanup(binding, current_owner=owner,
                authenticated_token_hash="current-token", proof_kind=kind, cleanup_owner=adapter)
    callback.assert_not_awaited()
    result = await jobs.record_native_physical_cleanup(binding, current_owner=owner,
        authenticated_token_hash="current-token", proof_kind="owned_positive_close",
        cleanup_owner=BrowserPhysicalCleanupOwner(callback))
    assert result["status"] == "unknown_external_effect" and result["revision"] == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_two_writer_cas_race_releases_original_pointer_once(original_resource):
    _, binding, owner = original_resource
    callback = AsyncMock(return_value=NativePhysicalCleanupProof(binding.witness_digest, "owned_positive_close"))
    release = AsyncMock()
    async def contender():
        return await jobs.record_native_physical_cleanup(binding, current_owner=owner,
            authenticated_token_hash="current-token", proof_kind="owned_positive_close",
            cleanup_owner=ConnectedSourcePhysicalCleanupOwner(callback, release))
    results = await asyncio.gather(contender(), contender(), return_exceptions=True)
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, DurableJobLeaseError) for result in results) == 1
    callback.assert_awaited_once(); release.assert_awaited_once()


async def _replace_witness(original_resource, witness, *, browser=False):
    db_factory, binding, owner = original_resource
    async with db_factory() as db:
        run = await db.get(WorkflowRunState, "physical-row")
        authority = json.loads(run.declared_authority_json)
        if browser:
            authority["capability_id"] = "browser.interact.v2"
            run.job_kind = "browser_interact_v2"
            run.capability_version = "2"
            run.resource_claims_json = '["browser-task-lane"]'
            run.declared_authority_json = _canonical(authority)
            run.authority_digest = _digest(authority)
        binding = replace(binding, authority_digest=run.authority_digest,
            resource_claim="browser-task-lane" if browser else binding.resource_claim,
            witness_digest=_digest(witness))
        payload = {"binding": native_physical_cleanup_binding_payload(binding), "witness": witness}
        run.checkpoint_receipts_json = _canonical([{"checkpoint_id": "native-physical-resource-reservation",
            "safe": True, "state_digest": _digest(payload), "payload": payload, "fencing_token": 2}])
    return binding, owner


def _browser_witness(binding, platform):
    return {"schema_version": 4, "pid": 123, "process_nonce": "c"*32, "context_nonce": "d"*32,
        "job_digest": hashlib.sha256(binding.job_id.encode()).hexdigest(), "root_path_digest": "e"*64,
        "root_device": 1, "root_inode": 2, "lock_device": 1, "lock_inode": 3,
        "boot_platform": platform, "boot_session_id": "f2310181-26ae-49c2-8c11-75d161b52ab1",
        "positive_cleanup_required": True}


@pytest.mark.parametrize("status", ["running", "unknown_external_effect", "cost_liability", "failed", "succeeded"])
@pytest.mark.parametrize("effects", ["[]", '[{"effect_id":"remote","status":"unknown"}]', '[{"effect_id":"remote","status":"succeeded"}]'])
async def test_source_finite_reachable_cleanup_preserves_all_canonical_execution(status, effects, original_resource):
    db_factory, binding, owner = original_resource
    async with db_factory() as db:
        run = await db.get(WorkflowRunState, "physical-row")
        run.status = status
        run.effect_receipts_json = effects
        run.artifact_receipts_json = '[{"artifact_id":"unchanged-output"}]'
    callback = AsyncMock(return_value=NativePhysicalCleanupProof(binding.witness_digest, "owned_positive_close",
        succeeded_adoption_verified=status == "succeeded"))
    release = AsyncMock()
    result = await jobs.record_native_physical_cleanup(binding, current_owner=owner,
        authenticated_token_hash="current-token", proof_kind="owned_positive_close",
        cleanup_owner=ConnectedSourcePhysicalCleanupOwner(callback, release))
    async with db_factory() as db:
        run = await db.get(WorkflowRunState, "physical-row")
        assert result["status"] == run.status == status
        assert run.effect_receipts_json == effects and run.lease_owner is None
        assert run.artifact_receipts_json == '[{"artifact_id":"unchanged-output"}]'
        assert native_external_effect_state(run) == ("none" if effects == "[]" else "unknown" if "unknown" in effects else "settled")
    callback.assert_awaited_once(); release.assert_awaited_once()


@pytest.mark.parametrize("status,flag", [("succeeded", False), ("succeeded", 1), ("failed", True), ("running", True)])
async def test_source_adoption_assertion_is_strict_and_only_for_succeeded(original_resource, status, flag):
    db_factory, binding, owner = original_resource
    async with db_factory() as db:
        (await db.get(WorkflowRunState, "physical-row")).status = status
    callback = AsyncMock(return_value=NativePhysicalCleanupProof(binding.witness_digest, "owned_positive_close", flag))
    release = AsyncMock()
    with pytest.raises(DurableJobError):
        await jobs.record_native_physical_cleanup(binding, current_owner=owner, authenticated_token_hash="current-token",
            proof_kind="owned_positive_close", cleanup_owner=ConnectedSourcePhysicalCleanupOwner(callback, release))
    release.assert_not_awaited()
    async with db_factory() as db:
        assert (await db.get(WorkflowRunState, "physical-row")).revision == 4


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("proof_kind", ["owned_positive_close", "owned_no_child", "linux_boot_changed", "darwin_boot_changed"])
async def test_browser_native_platform_proof_union_and_prechild_receipt(original_resource, platform, proof_kind):
    db_factory, binding, _ = original_resource
    binding, owner = await _replace_witness(original_resource, _browser_witness(binding, platform), browser=True)
    async with db_factory() as db:
        (await db.get(WorkflowRunState, "physical-row")).effect_receipts_json = "[]"
    callback = AsyncMock(return_value=NativePhysicalCleanupProof(binding.witness_digest, proof_kind))
    call = lambda: jobs.record_native_physical_cleanup(binding, current_owner=owner, authenticated_token_hash="current-token",
        proof_kind=proof_kind, cleanup_owner=BrowserPhysicalCleanupOwner(callback))
    if proof_kind.endswith("_boot_changed") and not proof_kind.startswith(platform):
        with pytest.raises(DurableJobError): await call()
        callback.assert_not_awaited()
    else:
        result = await call()
        assert result["status"] == "unknown_external_effect"
        binding = replace(binding, expected_revision=result["revision"])
        assert (await call())["receipt"]["deduped"] is True
        callback.assert_awaited_once()
        async with db_factory() as db:
            assert native_external_effect_state(await db.get(WorkflowRunState, "physical-row")) == "none"


@pytest.mark.parametrize("denial", ["contact_unknown", "contact_settled", "missing_reservation", "wrong_callback_kind", "adoption_flag"])
async def test_browser_no_child_cannot_be_claimed_by_caller_flag_or_contact(original_resource, denial):
    db_factory, binding, _ = original_resource
    binding, owner = await _replace_witness(original_resource, _browser_witness(binding, "darwin"), browser=True)
    async with db_factory() as db:
        run = await db.get(WorkflowRunState, "physical-row")
        run.effect_receipts_json = ('[{"effect_id":"contact","status":"unknown"}]' if denial == "contact_unknown" else
            '[{"effect_id":"contact","status":"succeeded"}]' if denial == "contact_settled" else "[]")
        if denial == "missing_reservation": run.checkpoint_receipts_json = "[]"
    callback = AsyncMock(return_value=NativePhysicalCleanupProof(binding.witness_digest,
        "owned_positive_close" if denial == "wrong_callback_kind" else "owned_no_child", denial == "adoption_flag"))
    with pytest.raises(DurableJobError):
        await jobs.record_native_physical_cleanup(binding, current_owner=owner, authenticated_token_hash="current-token",
            proof_kind="owned_no_child", cleanup_owner=BrowserPhysicalCleanupOwner(callback))
    async with db_factory() as db:
        assert (await db.get(WorkflowRunState, "physical-row")).revision == 4


@pytest.mark.parametrize("invalid", [None, "namespace", "boolean_sec", "float_usec", "overflow_usec", "zero_sec", "zero_boot", "wrong_platform_boot"])
async def test_source_darwin_closed_witness_and_matching_boot_proof(original_resource, invalid):
    witness = {"platform": "darwin", "boot_id": "f2310181-26ae-49c2-8c11-75d161b52ab1", "pid": 123,
        "pid_start_sec": 1700000000, "pid_start_usec": 25, "runtime_nonce": "a"*32,
        "connection_id": "connection", "scope_digest": "b"*64, "original_cursor_revision": 1}
    if invalid == "namespace": witness["pid_namespace"] = 789
    elif invalid == "boolean_sec": witness["pid_start_sec"] = True
    elif invalid == "float_usec": witness["pid_start_usec"] = 1.5
    elif invalid == "overflow_usec": witness["pid_start_usec"] = 1000000
    elif invalid == "zero_sec": witness["pid_start_sec"] = 0
    elif invalid == "zero_boot": witness["boot_id"] = "00000000-0000-0000-0000-000000000000"
    binding, owner = await _replace_witness(original_resource, witness)
    kind = "linux_boot_changed" if invalid == "wrong_platform_boot" else "darwin_boot_changed"
    callback = AsyncMock(return_value=NativePhysicalCleanupProof(binding.witness_digest, kind))
    release = AsyncMock()
    call = lambda: jobs.record_native_physical_cleanup(binding, current_owner=owner, authenticated_token_hash="current-token",
        proof_kind=kind, cleanup_owner=ConnectedSourcePhysicalCleanupOwner(callback, release))
    if invalid is not None:
        with pytest.raises(DurableJobError): await call()
        callback.assert_not_awaited(); release.assert_not_awaited()
    else:
        assert (await call())["receipt"]["proof_kind"] == "darwin_boot_changed"


async def test_cleanup_malformed_effect_ledger_is_denied_before_callback(original_resource):
    db_factory, binding, owner = original_resource
    async with db_factory() as db:
        (await db.get(WorkflowRunState, "physical-row")).effect_receipts_json = "malformed"
    callback, release = AsyncMock(), AsyncMock()
    with pytest.raises(DurableJobError):
        await jobs.record_native_physical_cleanup(binding, current_owner=owner, authenticated_token_hash="current-token",
            proof_kind="owned_positive_close", cleanup_owner=ConnectedSourcePhysicalCleanupOwner(callback, release))
    callback.assert_not_awaited(); release.assert_not_awaited()


@pytest.mark.parametrize("change", ["prototype", "unknown_boot", "zero_boot", "caller_no_child_flag", "boolean_pid", "wrong_platform", "wrong_platform_type"])
async def test_browser_closed_schema_rejects_unknown_boot_and_caller_prechild_flag(original_resource, change):
    _, binding, _ = original_resource
    witness = _browser_witness(binding, "darwin")
    if change == "prototype": witness["schema_version"] = 3
    elif change == "unknown_boot": witness["boot_session_id"] = None
    elif change == "zero_boot": witness["boot_session_id"] = "00000000-0000-0000-0000-000000000000"
    elif change == "caller_no_child_flag": witness["owned_no_child"] = True
    elif change == "boolean_pid": witness["pid"] = True
    elif change == "wrong_platform_type": witness["boot_platform"] = []
    else: witness["boot_platform"] = "unknown"
    binding, owner = await _replace_witness(original_resource, witness, browser=True)
    callback = AsyncMock()
    with pytest.raises(DurableJobError):
        await jobs.record_native_physical_cleanup(binding, current_owner=owner, authenticated_token_hash="current-token",
            proof_kind="owned_no_child", cleanup_owner=BrowserPhysicalCleanupOwner(callback))
    callback.assert_not_awaited()
