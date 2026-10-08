"""Actual SQLite private read journal; host identity fixture is not stock execution."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import update, select

from tests.test_runtime_composition_ownership import composition_db
from config.settings import settings
from src.auth.service import create_session
from src.auth.ownership import _current_root
from src.db.engine import get_session
from src.db.models import WorkflowRunState
from src.runtime_plugins.bridge import CordisHost
from src.runtime_plugins.composition import ReviewedComposition
from src.runtime_plugins.ownership import begin_native_writer
from src.runtime_plugins.read_admission import NativeServiceReadAdmission
from src.runtime_plugins.read_journal import native_read_spec, read_context, read_candidate, seal_read_result
from src.runtime_plugins.dispatch import NativeServiceBlocked
from src.runtime_plugins.contracts import succeeded
from src.workspace.production import ProductionWorkspaceReconciliationError
from src.workflows.job_runtime import DurableJobRepository, DurableJobAdmissionDenied, _native_plain


async def fetched(db, job_id):
    return await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))


async def admit(monkeypatch, *, candidate=None, key="original", artifact=False):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "offline-read-journal")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    _, operator = await create_session()
    reviewed = ReviewedComposition(Path("/owned-fixture"), Path("/owned-fixture/node"),
        "v24.13.1", {}, "b" * 64, "c" * 64)
    host = CordisHost()
    host.reviewed, host.boot_nonce, host.state = reviewed, "d" * 64, "ready"
    host.process, host._cleanup_state = SimpleNamespace(returncode=None), "pending"
    admission = NativeServiceReadAdmission.from_candidate(candidate or {
        "schema_version": 1, "method": "memory.retrieve", "query": "private café", "limit": 2, "status": "active"})
    repo = DurableJobRepository()
    async def owner_check(db, run):
        await _current_root(db, operator)
    async with get_session() as db:
        await begin_native_writer(db, owner="durable_jobs")
        spec = await native_read_spec(db, admission=admission, operator=operator,
            reviewed_composition=reviewed, idempotency_key=key)
        if not artifact:
            # Keep the separately scoped base-journal fixture explicit. Real
            # authenticated producers always select the artifact branch.
            from src.runtime_plugins.ownership import bind_invocation
            spec = replace(spec, composition_binding=await bind_invocation(db,
                method=admission.candidate()["method"], native_branch="base", reviewed_composition=reviewed))
        job = await repo._admit_in_session(db, spec, native_read_admission=admission,
            native_read_host=host, native_read_host_boot_nonce=host.boot_nonce,
            admission_authority_check=owner_check)
    return repo, spec, admission, operator, host, owner_check, job


@pytest.mark.asyncio
async def test_artifact_branch_cannot_complete_snapshot_only(composition_db, monkeypatch):
    repo, spec, _, _, host, _, _ = await admit(monkeypatch, artifact=True)
    await repo.transition_job(spec.identity.job_id, "queued")
    claim = await repo.claim_service_job(spec.identity.job_id, host=host, owner="original-reader", lease_seconds=20)
    payload = succeeded("memory.retrieve", {"records": []})
    async with get_session() as db:
        await begin_native_writer(db, owner="finite_service")
        run = await fetched(db, spec.identity.job_id)
        seal_read_result(db, run, payload, invocation_ref=run.run_identity, claim_ref=claim.checkpoint["payload"]["claim_ref"])
    with pytest.raises(NativeServiceBlocked, match="completion_unproven"):
        await repo.complete_native_read(claim, result=payload)
    assert (await repo.get_job(spec.identity.job_id))["status"] == "running"


@pytest.mark.asyncio
async def test_prepared_operation_requires_exact_wire_and_single_original_request(composition_db, monkeypatch):
    from src.runtime_plugins.read_journal import prepare_operation, operation_for_wire, start_operation, validate_operation
    repo, spec, _, _, host, _, _ = await admit(monkeypatch, artifact=True)
    await repo.transition_job(spec.identity.job_id, "queued")
    claim = await repo.claim_service_job(spec.identity.job_id, host=host, owner="original-reader", lease_seconds=20)
    async with get_session() as db:
        await begin_native_writer(db, owner="finite_service")
        run = await fetched(db, spec.identity.job_id)
        seal_read_result(db, run, succeeded("memory.retrieve", {"records": []}),
            invocation_ref=run.run_identity, claim_ref=claim.checkpoint["payload"]["claim_ref"])
    async with get_session() as db:
        await begin_native_writer(db, owner="finite_service")
        operation = prepare_operation(db, await fetched(db, spec.identity.job_id), "artifacts.stage")
    async with get_session() as db:
        await begin_native_writer(db, owner="finite_service")
        run = await fetched(db, spec.identity.job_id)
        with pytest.raises(NativeServiceBlocked, match="wire_changed"):
            operation_for_wire(run, "artifacts.stage", {"request_ref": "foreign"})
        assert operation_for_wire(run, "artifacts.stage", operation.wire_inputs()).candidate_digest == operation.candidate_digest
        start_operation(db, run, operation, call_ref="original-request")
    async with get_session() as db:
        await begin_native_writer(db, owner="finite_service")
        run = await fetched(db, spec.identity.job_id)
        validate_operation(run, operation, call_ref="original-request")
        with pytest.raises(NativeServiceBlocked, match="operation_changed"):
            validate_operation(run, operation, call_ref="other-request")
        with pytest.raises(NativeServiceBlocked, match="replay_denied"):
            start_operation(db, run, operation, call_ref="repeated-request")
        with pytest.raises(NativeServiceBlocked, match="replay_denied"):
            prepare_operation(db, run, "artifacts.stage")


@pytest.mark.asyncio
async def test_accounting_owner_component_survives_ordinary_read_ingress(composition_db, monkeypatch):
    from src.workspace.production import read_accounting_checkpoint, read_lifecycle_receipt
    repo = DurableJobRepository()
    await repo.configure_inference_accounting(1000)
    workspace = composition_db[3]
    before = read_accounting_checkpoint(workspace)
    await admit(monkeypatch)
    after = read_accounting_checkpoint(workspace)
    assert after["witness"] == before["witness"] == read_lifecycle_receipt(workspace)["inference_accounting"]
    assert after["account"] == before["account"]
    assert after["operations"] == []


@pytest.mark.asyncio
async def test_composition_only_writer_cannot_carry_changed_accounting_owner(composition_db):
    from src.db.models import InferenceAccountingOwner
    repo = DurableJobRepository()
    await repo.configure_inference_accounting(1000)
    with pytest.raises(ProductionWorkspaceReconciliationError, match="accounting_continuity_unavailable"):
        async with get_session() as db:
            await begin_native_writer(db, owner="finite_service")
            account = await db.get(InferenceAccountingOwner, "deployment")
            account.ceiling_microusd += 1
    async with get_session() as db:
        assert (await db.get(InferenceAccountingOwner, "deployment")).ceiling_microusd == 1000


@pytest.mark.asyncio
async def test_private_candidate_actual_claim_and_result_seal_are_hidden(composition_db, monkeypatch):
    repo, spec, admission, _, host, _, job = await admit(monkeypatch)
    assert "private café" not in json.dumps(job)
    await repo.transition_job(spec.identity.job_id, "queued")
    claim = await repo.claim_service_job(spec.identity.job_id, host=host, owner="original-reader", lease_seconds=20)
    assert "private café" not in json.dumps(_native_plain(claim.job), ensure_ascii=False)
    async with get_session() as db:
        await begin_native_writer(db, owner="finite_service")
        run = await fetched(db, job["job_id"])
        assert read_candidate(run) == admission.candidate()
        payload = succeeded("memory.retrieve", {"records": []})
        digest = seal_read_result(db, run, payload, invocation_ref=job["job_id"], claim_ref=claim.checkpoint["payload"]["claim_ref"])
        await db.flush()
    async with get_session() as db:
        assert read_context(await fetched(db, job["job_id"]))["result"]["result_digest"] == digest
    assert "checkpoint_context" not in json.dumps(await repo.get_job(job["job_id"]))
    completed = await repo.complete_native_read(claim, result=payload)
    assert completed["status"] == "succeeded"
    async with get_session() as db:
        run = await fetched(db, job["job_id"])
        assert run.result_digest == digest
        from src.workflows.durable_state import workflow_state_repository
        workflow_projection = workflow_state_repository._serialize_run(run)
        assert workflow_projection["checkpoint_context"] == {}
        assert workflow_projection["checkpoint_context_available"] is False
        assert workflow_projection["checkpoint_receipts"] == []
        assert "private café" not in json.dumps(workflow_projection, ensure_ascii=False)
    assert "private café" not in json.dumps(completed, ensure_ascii=False)


@pytest.mark.asyncio
async def test_exact_retry_keeps_original_deadline_and_private_context(composition_db, monkeypatch):
    repo, spec, admission, operator, host, check, job = await admit(monkeypatch)
    async with get_session() as db:
        await begin_native_writer(db, owner="durable_jobs")
        retried = await native_read_spec(db, admission=admission, operator=operator,
            reviewed_composition=host.reviewed, idempotency_key="original")
        retried = replace(retried, composition_binding=spec.composition_binding)
        assert retried.deadline_at == spec.deadline_at
        assert await repo._admit_in_session(db, retried, native_read_admission=admission,
            native_read_host=host, native_read_host_boot_nonce=host.boot_nonce,
            admission_authority_check=check)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["query", "clear", "owner", "jobkind", "bulk"])
async def test_public_writer_cannot_replace_read_candidate_or_owner(composition_db, monkeypatch, mutation):
    _, spec, _, _, _, _, _ = await admit(monkeypatch)
    with pytest.raises((ProductionWorkspaceReconciliationError, NativeServiceBlocked)):
        async with get_session() as db:
            await begin_native_writer(db, owner="finite_service")
            run = await fetched(db, spec.identity.job_id)
            if mutation == "query":
                context = json.loads(run.checkpoint_context_json)
                context["candidate"]["query"] = "other secret"
                run.checkpoint_context_json = json.dumps(context)
            elif mutation == "clear":
                run.checkpoint_context_json = None
            elif mutation == "owner":
                run.owner_principal_id = "different"
            elif mutation == "jobkind":
                run.job_kind = "workflow"
            else:
                await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == spec.identity.job_id).values(checkpoint_context_json="{}"))
            await db.flush()


@pytest.mark.asyncio
async def test_generic_admission_cannot_mint_typed_read_job(composition_db, monkeypatch):
    repo, spec, _, _, _, _, _ = await admit(monkeypatch)
    with pytest.raises(DurableJobAdmissionDenied, match="native_read_admission_required"):
        await repo.admit_job(replace(spec, identity=replace(spec.identity, job_id="forged-read")))


@pytest.mark.asyncio
async def test_candidate_retry_replacement_is_rejected(composition_db, monkeypatch):
    _, _, admission, operator, host, _, _ = await admit(monkeypatch)
    replacement = NativeServiceReadAdmission.from_candidate({**admission.candidate(), "limit": 1})
    with pytest.raises(NativeServiceBlocked, match="native_read_idempotency_candidate_changed"):
        async with get_session() as db:
            await begin_native_writer(db, owner="finite_service")
            await native_read_spec(db, admission=replacement, operator=operator,
                reviewed_composition=host.reviewed, idempotency_key="original")


@pytest.mark.asyncio
async def test_result_requires_original_claim_and_is_one_shot(composition_db, monkeypatch):
    repo, spec, _, _, host, _, _ = await admit(monkeypatch)
    await repo.transition_job(spec.identity.job_id, "queued")
    claim = await repo.claim_service_job(spec.identity.job_id, host=host, owner="reader", lease_seconds=20)
    with pytest.raises(NativeServiceBlocked, match="native_read_result_claim_changed"):
        async with get_session() as db:
            await begin_native_writer(db, owner="finite_service")
            run = await fetched(db, spec.identity.job_id)
            seal_read_result(db, run, succeeded("memory.retrieve", {"records": []}),
                invocation_ref=spec.identity.job_id, claim_ref="wrong-claim")
    async with get_session() as db:
        await begin_native_writer(db, owner="finite_service")
        run = await fetched(db, spec.identity.job_id)
        seal_read_result(db, run, succeeded("memory.retrieve", {"records": []}),
            invocation_ref=spec.identity.job_id, claim_ref=claim.checkpoint["payload"]["claim_ref"])
    with pytest.raises(NativeServiceBlocked, match="native_read_result_seal_unavailable"):
        async with get_session() as db:
            await begin_native_writer(db, owner="finite_service")
            run = await fetched(db, spec.identity.job_id)
            seal_read_result(db, run, succeeded("memory.retrieve", {"records": []}),
                invocation_ref=spec.identity.job_id, claim_ref=claim.checkpoint["payload"]["claim_ref"])


@pytest.mark.asyncio
async def test_restart_before_claim_cannot_replace_original_host_boot(composition_db, monkeypatch):
    from src.workflows.job_runtime import DurableJobLeaseError
    repo, spec, _, _, host, _, _ = await admit(monkeypatch)
    await repo.transition_job(spec.identity.job_id, "queued")
    host.boot_nonce = "e" * 64
    with pytest.raises(DurableJobLeaseError, match="native_read_original_host_boot_changed"):
        await repo.claim_service_job(spec.identity.job_id, host=host, owner="reader", lease_seconds=20)
    assert (await repo.get_job(spec.identity.job_id))["status"] == "queued"


@pytest.mark.asyncio
async def test_child_echo_and_generic_transition_cannot_invent_read_success(composition_db, monkeypatch):
    from src.workflows.job_runtime import DurableJobLeaseError, DurableJobTransitionError
    repo, spec, _, _, host, _, _ = await admit(monkeypatch)
    await repo.transition_job(spec.identity.job_id, "queued")
    claim = await repo.claim_service_job(spec.identity.job_id, host=host, owner="reader", lease_seconds=20)
    with pytest.raises(DurableJobLeaseError, match="original native read completion changed"):
        await repo.complete_native_read(claim, result=succeeded("memory.retrieve", {"records": []}))
    with pytest.raises(DurableJobTransitionError, match="original native read sealed completion required"):
        await repo.transition_job(spec.identity.job_id, "succeeded", owner="reader", fencing_token=claim.checkpoint["payload"]["fencing_token"])
    assert (await repo.get_job(spec.identity.job_id))["status"] == "running"


@pytest.mark.asyncio
async def test_restart_inside_original_admission_callback_rolls_back_new_job(composition_db, monkeypatch):
    repo, _, admission, operator, host, _, _ = await admit(monkeypatch)
    job_id = None
    async def restart(db, run):
        await _current_root(db, operator)
        host.boot_nonce = "e" * 64
    with pytest.raises(DurableJobAdmissionDenied, match="native_read_original_host_changed"):
        async with get_session() as db:
            await begin_native_writer(db, owner="durable_jobs")
            spec = await native_read_spec(db, admission=admission, operator=operator,
                reviewed_composition=host.reviewed, idempotency_key="callback-race")
            job_id = spec.identity.job_id
            await repo._admit_in_session(db, spec, native_read_admission=admission,
                native_read_host=host, native_read_host_boot_nonce=host.boot_nonce,
                admission_authority_check=restart)
    assert await repo.get_job(job_id) is None


@pytest.mark.asyncio
async def test_retry_after_restart_does_not_rebind_original_read_job(composition_db, monkeypatch):
    repo, _, admission, operator, host, check, job = await admit(monkeypatch)
    host.boot_nonce = "e" * 64
    with pytest.raises(DurableJobAdmissionDenied, match="native_read_original_host_changed"):
        async with get_session() as db:
            await begin_native_writer(db, owner="durable_jobs")
            spec = await native_read_spec(db, admission=admission, operator=operator,
                reviewed_composition=host.reviewed, idempotency_key="original")
            await repo._admit_in_session(db, spec, native_read_admission=admission,
                native_read_host=host, native_read_host_boot_nonce=host.boot_nonce,
                admission_authority_check=check)
    assert (await repo.get_job(job["job_id"]))["status"] == "accepted"
