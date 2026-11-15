"""Startup preserves actual registered original repository lineage."""
from datetime import datetime, timedelta, timezone
import json
import asyncio

import pytest
import httpx
from sqlalchemy import select

from src.db.models import WorkflowRunState, InferenceCostReservation, InferenceAccountingOwner
from src.workflows import repo_repair_source as source
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_repo_work_task_publication import _actual_source_callback_journey


class _KeepOriginalSourceRunning(Exception):
    pass


@pytest.mark.asyncio
async def test_inherited_context_does_not_grant_startup_fence_to_child_task():
    from src.workflows.job_runtime import DurableJobLeaseError
    async with source._repository_startup_mutation_fence():
        async def inherited_child():
            with pytest.raises(DurableJobLeaseError, match="private repository startup mutation fence"):
                await source._repository_startup_protected_lineage(object())
        await asyncio.create_task(inherited_child())


async def registered_running_source(accounting_db, monkeypatch, *, goal_capacity=2):
    captured = {}

    async def hold_final(service, jobs, **kwargs):
        captured.update(service=service, jobs=jobs, **kwargs)
        raise _KeepOriginalSourceRunning()

    with monkeypatch.context() as patch:
        patch.setattr(source, "finalize_repository_iteration", hold_final)
        with pytest.raises(_KeepOriginalSourceRunning):
            await _actual_source_callback_journey(accounting_db, monkeypatch, False, "test_python",
                goal_capacity=goal_capacity)
    return captured


async def exact_canonical_bytes(factory):
    async with factory() as db:
        values = {}
        for model in (WorkflowRunState, InferenceCostReservation, InferenceAccountingOwner):
            values[model.__tablename__] = sorted(row.model_dump_json() for row in
                (await db.scalars(select(model))).all())
        return values


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("corruption", ["missing_member", "rehashed_shape"])
@pytest.mark.parametrize("reverse_candidates", [False, True])
async def test_startup_preserves_proven_lineage_before_malformed_original(
        accounting_db, monkeypatch, unknown, corruption, reverse_candidates, repository_admission_signer):
    from sqlalchemy import event, or_
    from src.workflows.job_runtime import _canonical, _digest
    captured = await registered_running_source(accounting_db, monkeypatch)
    jobs, service, job_id, owner = (captured[key] for key in ("jobs", "service", "job_id", "owner"))
    factory = accounting_db[2]
    async with factory() as db:
        root = await jobs._fetch(db, job_id)
        binding = source.read_repository_original(root)[4]
        lease_owner, fence = root.lease_owner, root.fencing_token
    if unknown:
        await source._quarantine_original_uncertainty(service, jobs, job_id=job_id, owner=owner,
            lease_owner=lease_owner, fencing_token=fence, reason="repository_process_closure_unproven",
            result={"no_learning": True, "operator_action": "reconcile_original_process",
                "iteration_id": captured["actual_job"].iteration_binding.iteration_id})
    async with factory() as db:
        root = await jobs._fetch(db, job_id)
        history = json.loads(root.checkpoint_receipts_json)
        original = next(item for item in history if item["checkpoint_id"] == "repository:original:v1")
        if corruption == "missing_member":
            original["payload"].pop("original_input")
        else:
            original["payload"]["native_binding"] = {"invocation_id": "foreign"}
        original["state_digest"] = _digest(original["payload"])
        root.checkpoint_receipts_json = _canonical(history)
        await db.commit()
    before = await exact_canonical_bytes(factory)
    observed = datetime.now(timezone.utc) + timedelta(days=1)
    candidates = select(WorkflowRunState).where(WorkflowRunState.status == "running",
        or_(WorkflowRunState.lease_expires_at.is_(None), WorkflowRunState.lease_expires_at <= observed))
    async with factory() as db:
        original_order = [row.run_identity for row in (await db.scalars(candidates)).all()]

    def reverse_scan(connection, _record, _proxy):
        cursor = connection.cursor()
        cursor.execute("PRAGMA reverse_unordered_selects=ON")
        cursor.close()

    if reverse_candidates:
        event.listen(accounting_db[1].sync_engine, "checkout", reverse_scan)
    try:
        async with factory() as db:
            observed_order = [row.run_identity for row in (await db.scalars(candidates)).all()]
        assert len(original_order) >= (1 if unknown else 2)
        assert observed_order == (list(reversed(original_order)) if reverse_candidates else original_order)
        assert await jobs.recover_inference_accounting(now=observed) == []
        assert await jobs.recover_stale_jobs(now=observed) == []
        for protected_id in (job_id, binding.invocation_id, binding.parent_job_id):
            receipt = await jobs.recover_stale_job(protected_id, now=observed)
            assert receipt["receipt"]["reason"] == "original_repository_source_recovery_required"
    finally:
        if reverse_candidates:
            event.remove(accounting_db[1].sync_engine, "checkout", reverse_scan)
    assert await exact_canonical_bytes(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("edge", ["indexed_root", "native_owner", "parent_owner",
    "indexed_root_malformed_marker", "native_owner_malformed_marker", "parent_owner_malformed_marker",
    "marker_prefix", "marker_duplicate", "marker_unsafe", "marker_hash"])
async def test_startup_protects_only_exact_proven_prefix(
        accounting_db, monkeypatch, edge, repository_admission_signer):
    from src.workflows.job_runtime import _canonical
    from src.work_board.repository import _begin_sqlite_immediate
    captured = await registered_running_source(accounting_db, monkeypatch)
    jobs, job_id = (captured[key] for key in ("jobs", "job_id"))
    factory = accounting_db[2]
    async with factory() as db:
        root = await jobs._fetch(db, job_id)
        binding = source.read_repository_original(root)[4]
        expected = {job_id}
        if edge.endswith("_malformed_marker"):
            history = json.loads(root.checkpoint_receipts_json)
            marker = next(item for item in history if item["checkpoint_id"].startswith("repository:producer:"))
            marker["checkpoint_id"] = "repository:producer:"
            root.checkpoint_receipts_json = _canonical(history)
        if edge in {"indexed_root", "indexed_root_malformed_marker"}:
            root.idempotency_binding = "foreign-indexed-binding"
        elif edge in {"native_owner", "native_owner_malformed_marker"}:
            child = await jobs._fetch(db, binding.invocation_id)
            child.owner_principal_id = "foreign-owner"
        elif edge in {"parent_owner", "parent_owner_malformed_marker"}:
            parent = await jobs._fetch(db, binding.parent_job_id)
            parent.owner_principal_id = "foreign-owner"
            expected.add(binding.invocation_id)
        else:
            history = json.loads(root.checkpoint_receipts_json)
            marker = next(item for item in history if item["checkpoint_id"].startswith("repository:producer:"))
            if edge == "marker_prefix":
                marker["checkpoint_id"] += ":foreign"
                expected.update({binding.invocation_id, binding.parent_job_id})
            elif edge == "marker_duplicate":
                history.append(dict(marker))
                expected.update({binding.invocation_id, binding.parent_job_id})
            elif edge == "marker_unsafe":
                marker["safe"] = False
                expected.update({binding.invocation_id, binding.parent_job_id})
            else:
                marker["state_digest"] = "0" * 64
                expected.update({binding.invocation_id, binding.parent_job_id})
            root.checkpoint_receipts_json = _canonical(history)
        await db.commit()
    before = await exact_canonical_bytes(factory)
    async with source._repository_startup_mutation_fence():
        async with factory() as db:
            await _begin_sqlite_immediate(db)
            assert await source._repository_startup_protected_lineage(db) == expected
    if edge in {"marker_prefix", "marker_duplicate", "marker_unsafe", "marker_hash"}:
        observed = datetime.now(timezone.utc) + timedelta(days=1)
        assert await jobs.recover_inference_accounting(now=observed) == []
        assert await jobs.recover_stale_jobs(now=observed) == []
        for protected_id in expected:
            receipt = await jobs.recover_stale_job(protected_id, now=observed)
            assert receipt["receipt"]["reason"] == "original_repository_source_recovery_required"
    assert await exact_canonical_bytes(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("marker_shape", ["suffix", "truncated", "empty", "nonhex"])
@pytest.mark.parametrize("reverse_candidates", [False, True])
async def test_startup_retains_malformed_reserved_marker_in_both_candidate_orders(
        accounting_db, monkeypatch, marker_shape, reverse_candidates, repository_admission_signer):
    from sqlalchemy import event, or_
    from src.workflows.job_runtime import _canonical
    captured = await registered_running_source(accounting_db, monkeypatch)
    jobs, job_id = (captured[key] for key in ("jobs", "job_id"))
    factory = accounting_db[2]
    async with factory() as db:
        root = await jobs._fetch(db, job_id)
        binding = source.read_repository_original(root)[4]
        history = json.loads(root.checkpoint_receipts_json)
        marker = next(item for item in history if item["checkpoint_id"].startswith("repository:producer:"))
        marker["checkpoint_id"] = {
            "suffix": marker["checkpoint_id"] + ":foreign",
            "truncated": marker["checkpoint_id"][:-1],
            "empty": "repository:producer:",
            "nonhex": "repository:producer:" + "g" * 64,
        }[marker_shape]
        root.checkpoint_receipts_json = _canonical(history)
        await db.commit()
    before = await exact_canonical_bytes(factory)
    observed = datetime.now(timezone.utc) + timedelta(days=1)
    candidates = select(WorkflowRunState).where(WorkflowRunState.status == "running",
        or_(WorkflowRunState.lease_expires_at.is_(None), WorkflowRunState.lease_expires_at <= observed))
    async with factory() as db:
        original_order = [row.run_identity for row in (await db.scalars(candidates)).all()]

    def reverse_scan(connection, _record, _proxy):
        cursor = connection.cursor()
        cursor.execute("PRAGMA reverse_unordered_selects=ON")
        cursor.close()

    if reverse_candidates:
        event.listen(accounting_db[1].sync_engine, "checkout", reverse_scan)
    try:
        async with factory() as db:
            actual_order = [row.run_identity for row in (await db.scalars(candidates)).all()]
        assert len(original_order) >= 2
        assert actual_order == (list(reversed(original_order)) if reverse_candidates else original_order)
        assert await jobs.recover_inference_accounting(now=observed) == []
        assert await jobs.recover_stale_jobs(now=observed) == []
        for protected_id in (job_id, binding.invocation_id, binding.parent_job_id):
            result = await jobs.recover_stale_job(protected_id, now=observed)
            assert result["receipt"]["reason"] == "original_repository_source_recovery_required"
    finally:
        if reverse_candidates:
            event.remove(accounting_db[1].sync_engine, "checkout", reverse_scan)
    assert await exact_canonical_bytes(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
@pytest.mark.parametrize("corrupt", [False, True])
async def test_startup_preserves_registered_original_lineage_and_accounting(
        accounting_db, monkeypatch, unknown, corrupt, repository_admission_signer):
    captured = await registered_running_source(accounting_db, monkeypatch)
    jobs, service, job_id, owner = (captured[key] for key in ("jobs", "service", "job_id", "owner"))
    factory = accounting_db[2]
    async with factory() as db:
        root = await jobs._fetch(db, job_id)
        original, _, _, _, binding, _ = source.read_repository_original(root)
        assert root.status == "running"
        if unknown:
            original_owner, original_fence = root.lease_owner, root.fencing_token
    if unknown:
        await source._quarantine_original_uncertainty(service, jobs, job_id=job_id, owner=owner,
            lease_owner=original_owner, fencing_token=original_fence,
            reason="repository_process_closure_unproven",
            result={"no_learning": True, "operator_action": "reconcile_original_process",
                "iteration_id": captured["actual_job"].iteration_binding.iteration_id})
    if corrupt:
        # Corruption of a genuinely registered producer is not absence. This
        # negative fixture changes no owner identity or caller authority.
        from src.workflows.job_runtime import _digest, _canonical
        async with factory() as db:
            root = await jobs._fetch(db, job_id)
            history = json.loads(root.checkpoint_receipts_json)
            producer = next(item for item in history if item["checkpoint_id"].startswith("repository:producer:"))
            producer["payload"]["ready_digest"] = "0" * 64
            producer["state_digest"] = _digest(producer["payload"])
            root.checkpoint_receipts_json = _canonical(history)
            await db.commit()
    before = await exact_canonical_bytes(factory)
    observed = datetime.now(timezone.utc) + timedelta(days=1)
    assert await jobs.recover_inference_accounting(now=observed) == []
    assert await jobs.recover_stale_jobs(now=observed) == []
    for protected_id in (job_id, binding.invocation_id, binding.parent_job_id):
        receipt = await jobs.recover_stale_job(protected_id, now=observed)
        assert receipt["receipt"]["reason"] == "original_repository_source_recovery_required"
    assert await exact_canonical_bytes(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_marker", [False, True])
async def test_registered_lineage_does_not_protect_unrelated_same_goal_job(
        accounting_db, monkeypatch, malformed_marker, repository_admission_signer):
    from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec
    captured = await registered_running_source(accounting_db, monkeypatch, goal_capacity=3)
    jobs, owner, job_id = (captured[key] for key in ("jobs", "owner", "job_id"))
    factory = accounting_db[2]
    async with factory() as db:
        original = await jobs._fetch(db, job_id)
        _, _, _, _, binding, _ = source.read_repository_original(original)
        if malformed_marker:
            from src.workflows.job_runtime import _canonical
            history = json.loads(original.checkpoint_receipts_json)
            marker = next(item for item in history if item["checkpoint_id"].startswith("repository:producer:"))
            marker["checkpoint_id"] += ":foreign"
            original.checkpoint_receipts_json = _canonical(history)
            await db.commit()
        protected_ids = {job_id, binding.invocation_id, binding.parent_job_id}
        before = {row.run_identity: row.model_dump_json() for row in
            (await db.scalars(select(WorkflowRunState))).all() if row.run_identity in protected_ids}
    unrelated = await jobs.admit_job(DurableJobSpec(identity=DurableJobIdentity(
        job_id="startup-unrelated-same-goal", owner_kind="user", owner_principal_id=owner.principal_id,
        job_kind="startup_unrelated", capability_version="1",
        idempotency_scope="startup-unrelated", idempotency_key="same-goal"), inputs={"work": "unrelated"},
        session_id=owner.session_id, operator_session_id=owner.session_id,
        goal_id=binding.goal_id, goal_revision=binding.goal_revision,
        declared_authority={"principal": owner.principal_id, "owner_kind": "user", "session_id": owner.session_id,
            "goal_id": binding.goal_id, "goal_revision": binding.goal_revision}))
    await jobs.queue_job(unrelated["job_id"])
    await jobs.claim_job(unrelated["job_id"], owner="unrelated-worker", lease_seconds=1)
    recovered = await jobs.recover_stale_jobs(now=datetime.now(timezone.utc) + timedelta(seconds=5))
    assert [row["job_id"] for row in recovered] == [unrelated["job_id"]]
    assert recovered[0]["status"] == "blocked"
    async with factory() as db:
        after = {row.run_identity: row.model_dump_json() for row in
            (await db.scalars(select(WorkflowRunState))).all() if row.run_identity in protected_ids}
        assert after == before
    async with source._repository_startup_mutation_fence():
        async with factory() as db:
            from src.work_board.repository import _begin_sqlite_immediate
            await _begin_sqlite_immediate(db)
            protected = await source._repository_startup_protected_lineage(db)
            assert protected == {job_id, binding.invocation_id, binding.parent_job_id}


@pytest.mark.asyncio
async def test_startup_retains_real_unknown_provider_debt_after_registered_iteration(
        accounting_db, monkeypatch, repository_admission_signer):
    # Fail only the second actual intercepted provider billing response, after
    # the first real failed Python iteration registered and closed its producer.
    # No reservation or accounting evidence is inserted by this fixture.
    captured, responses = {}, []
    original_grant = source.grant_repository_iteration_consent
    original_response = httpx.Response

    async def capture_grant(service, jobs, **kwargs):
        captured.update(service=service, jobs=jobs, job_id=kwargs["job_id"], owner=kwargs["owner"])
        return await original_grant(service, jobs, **kwargs)

    def missing_second_bill(*args, **kwargs):
        payload = kwargs.get("json")
        if isinstance(payload, dict) and payload.get("id") == "scripted-source-final-transport":
            responses.append(payload)
            if len(responses) == 2:
                payload["usage"].pop("cost")
        return original_response(*args, **kwargs)

    monkeypatch.setattr(source, "grant_repository_iteration_consent", capture_grant)
    monkeypatch.setattr(httpx, "Response", missing_second_bill)
    with pytest.raises(Exception):
        await _actual_source_callback_journey(accounting_db, monkeypatch, True, "test_python")
    assert len(responses) == 2
    factory, jobs, job_id = accounting_db[2], captured["jobs"], captured["job_id"]
    async with factory() as db:
        root = await jobs._fetch(db, job_id)
        assert root.status == "unknown_external_effect"
        original, work, _, _, _, _ = source.read_repository_original(root)
        identity = source.iteration_identity(job_id, original["repository_attempt_id"],
            source._source_digest(original["original_input"]), 1)
        assert source._repository_record(root, "repository:producer:" + identity) is not None
        reservations = list((await db.scalars(select(InferenceCostReservation))).all())
        debt = next(row for row in reservations if row.state == "unknown")
        from src.workflows.general_task_accounting import reservation_liability
        assert debt.contact_started_at is not None and debt.actual_cost_microusd is None
        assert reservation_liability(debt) == debt.bound_microusd > 0
    before = await exact_canonical_bytes(factory)
    observed = datetime.now(timezone.utc) + timedelta(days=1)
    assert await jobs.recover_inference_accounting(now=observed) == []
    assert await jobs.recover_stale_jobs(now=observed) == []
    assert await exact_canonical_bytes(factory) == before


async def undispatched_original_source(accounting_db, monkeypatch, *, work_limits=None):
    """Actual NEW admission/preparation; no producer registration or command."""
    from src.auth.service import authenticate_session
    from tests.test_repo_work_task_publication import actual_native_source
    factory, owner, service, jobs, binding, _ = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False, work_limits=work_limits)
    operator = await authenticate_session(owner.session_id, touch=False)
    prepared = await source.prepare_repository_native_source(service, jobs, binding,
        child_owner="actual-undispatched-startup-worker", principal=operator.principal)
    return {"factory": factory, "owner": owner, "service": service.repository_source_service,
        "jobs": jobs, "job_id": prepared["repository_job_id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatched", [False, True])
@pytest.mark.parametrize("unknown", [False, True])
async def test_startup_protects_actual_v4_without_registration_before_any_mutation(
        accounting_db, monkeypatch, repository_admission_signer, dispatched, unknown):
    from src.workflows.job_runtime import _canonical
    from src.work_board.repository import _begin_sqlite_immediate
    captured = (await registered_running_source(accounting_db, monkeypatch) if dispatched else
        await undispatched_original_source(accounting_db, monkeypatch))
    jobs, service, job_id, owner = (captured[key] for key in ("jobs", "service", "job_id", "owner"))
    factory = accounting_db[2]
    async with factory() as db:
        root = await jobs._fetch(db, job_id)
        original, _, _, _, binding, _ = source.read_repository_original(root)
        assert source.read_repository_inventory(root)["schema"] == "repository.checkpoint_inventory.v4"
        history = json.loads(root.checkpoint_receipts_json)
        registrations = [item for item in history if item["checkpoint_id"].startswith("repository:producer:")]
        assert bool(registrations) is dispatched
        # Erasure is a negative corruption of actual dispatched evidence, never
        # a newly supplied registration or an assertion that dispatch did not occur.
        if dispatched:
            root.checkpoint_receipts_json = _canonical([item for item in history if item not in registrations])
            db.add(root)
            await db.commit()
        lease_owner, fence = root.lease_owner, root.fencing_token
        identity = source.iteration_identity(job_id, original["repository_attempt_id"],
            source._source_digest(original["original_input"]), 1)
    if unknown:
        await source._quarantine_original_uncertainty(service, jobs, job_id=job_id, owner=owner,
            lease_owner=lease_owner, fencing_token=fence, reason="repository_process_closure_unproven",
            result={"no_learning": True, "operator_action": "reconcile_original_process", "iteration_id": identity})
    before = await exact_canonical_bytes(factory)
    async with source._repository_startup_mutation_fence():
        async with factory() as db:
            await _begin_sqlite_immediate(db)
            assert await source._repository_startup_protected_lineage(db) == {
                job_id, binding.invocation_id, binding.parent_job_id}
    observed = datetime.now(timezone.utc) + timedelta(days=1)
    assert await jobs.recover_inference_accounting(now=observed) == []
    assert await jobs.recover_stale_jobs(now=observed) == []
    for protected_id in (job_id, binding.invocation_id, binding.parent_job_id):
        result = await jobs.recover_stale_job(protected_id, now=observed)
        assert result["receipt"]["reason"] == "original_repository_source_recovery_required"
    assert await exact_canonical_bytes(factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["missing_inventory", "missing_mode", "unknown_mode",
    "ordinary_mode_with_producer_vector", "foreign_identity", "malformed_history"])
async def test_startup_missing_or_corrupt_mode_holds_actual_undispatched_mapped_prefix(
        accounting_db, monkeypatch, repository_admission_signer, corruption):
    from src.workflows.job_runtime import _canonical, _digest
    from src.work_board.repository import _begin_sqlite_immediate
    captured = await undispatched_original_source(accounting_db, monkeypatch)
    factory, jobs, job_id = (captured[key] for key in ("factory", "jobs", "job_id"))
    async with factory() as db:
        root = await jobs._fetch(db, job_id)
        binding = source.read_repository_original(root)[4]
        history = json.loads(root.checkpoint_receipts_json)
        inventory = next(item for item in history if item["checkpoint_id"] == "repository:inventory:v1")
        if corruption == "missing_inventory":
            history.remove(inventory)
        elif corruption == "missing_mode":
            inventory["payload"].pop("schema")
        elif corruption == "unknown_mode":
            inventory["payload"]["schema"] = "repository.checkpoint_inventory.v5"
        elif corruption == "ordinary_mode_with_producer_vector":
            inventory["payload"]["schema"] = "repository.checkpoint_inventory.v2"
        elif corruption == "foreign_identity":
            inventory["payload"]["identities"][-1] = "repository:physical-cleanup:foreign"
        if corruption != "missing_inventory":
            inventory["state_digest"] = _digest(inventory["payload"])
        root.checkpoint_receipts_json = "not-json" if corruption == "malformed_history" else _canonical(history)
        db.add(root)
        await db.commit()
    before = await exact_canonical_bytes(factory)
    async with source._repository_startup_mutation_fence():
        async with factory() as db:
            await _begin_sqlite_immediate(db)
            assert await source._repository_startup_protected_lineage(db) == {
                job_id, binding.invocation_id, binding.parent_job_id}
    observed = datetime.now(timezone.utc) + timedelta(days=1)
    assert await jobs.recover_inference_accounting(now=observed) == []
    assert await jobs.recover_stale_jobs(now=observed) == []
    assert await exact_canonical_bytes(factory) == before
