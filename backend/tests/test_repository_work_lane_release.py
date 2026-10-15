"""Ordinary expiry, partial checks and Stop always release the repository lane.

One held original Root blocks every repository Task in the workspace, so
each ordinary path must end visible, bounded and recoverable: closed with
cleanup proven and capacity released, after which a successor Task runs.
Inference is scripted only at the iteration's own ``httpx.Client``
transport; ``forbid_external_inference`` denies every other socket.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from sqlalchemy import select

from src.db.models import OperatorSession
from src.work_board.contracts import TaskLimits
from src.work_board.repository import BoardError
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_work_task_publication import actual_native_source, admit_existing_source_request


async def _original(accounting_db, monkeypatch, **limits):
    from src.auth.service import authenticate_session
    from src.workflows.repo_repair_source import prepare_repository_native_source
    factory, owner, service, jobs, binding, request = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=3, claim_child=False, **limits)
    operator = await authenticate_session(owner.session_id, touch=False)
    prepared = await prepare_repository_native_source(service, jobs, binding,
        child_owner="lane-original-worker", principal=operator.principal)
    return {"factory": factory, "owner": owner, "service": service, "jobs": jobs, "binding": binding,
        "request": request, "operator": operator, "root_id": prepared["repository_job_id"]}


async def _cutoff(flow):
    from src.workflows.repo_repair_source import read_repository_original
    async with flow["factory"]() as db:
        root = await flow["jobs"]._fetch(db, flow["root_id"])
        return datetime.fromisoformat(read_repository_original(root)[0]["original_deadline_at"])


async def _assert_released(flow, *, status, reason):
    from src.workflows.repo_repair_source import _repository_record
    async with flow["factory"]() as db:
        root = await flow["jobs"]._fetch(db, flow["root_id"])
        assert root.status == status
        assert flow["jobs"]._repo_repair_reservation_state(root)["status"] == "released"
        assert _repository_record(root, "repository:terminal:v1")["closure"]["stop_reason"] == reason
    assert flow["root_id"] not in flow["service"].repository_source_service._iterative_lanes


async def _successor_runs(flow, key):
    """A distinct new Task acquires the released lane and reaches consent review."""
    from src.workflows.repo_repair_source import prepare_repository_native_source
    _, _, service, jobs, binding, _ = await admit_existing_source_request(flow["factory"], flow["owner"],
        flow["service"], flow["request"].model_copy(update={"idempotency_key": key}), claim_child=False)
    successor = await prepare_repository_native_source(service, jobs, binding,
        child_owner="lane-successor-worker", principal=flow["operator"].principal)
    assert successor["repository_job_id"] != flow["root_id"]
    assert successor["awaiting_repository_consent"] is True
    return successor


async def _execute(flow, patch):
    """One consented, approved iteration through the actual original producer."""
    from src.api.workflows import RepoRepairEgressConsentRequest, RepoRepairResumeRequest
    from src.approval.repository import approval_repository, approval_decision_digest
    from src.db.models import RepoRepairProposal
    from src.workflows.repo_repair_source import (execute_repository_iteration,
        grant_repository_iteration_consent, repository_source_preview)
    service, jobs, owner, root_id = flow["service"], flow["jobs"], flow["owner"], flow["root_id"]
    preview = await repository_source_preview(service.repository_source_service, jobs, job_id=root_id, owner=owner)
    review, egress, packet = preview["repository_review"], preview["egress"], preview["source_packet"]
    source = json.loads(egress["request_body"]["messages"][1]["content"])
    output = {"summary": "Scripted change", "base_snapshot_sha256": source["source_packet"]["base_snapshot_sha256"],
        "patch_unified_diff": patch, "allowed_paths": ["calculator.py", "tests/test_calculator.py"],
        "test_args": ["pytest", "-q", "tests/test_calculator.py"], "expected_outcome": "Checks run"}
    def iteration_http(request):
        assert json.loads(request.content) == egress["request_body"]
        return httpx.Response(200, json={"id": "scripted-lane-iteration",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": "0"},
            "choices": [{"message": {"role": "assistant", "content": json.dumps(output)}}]})
    real_client = httpx.Client
    with pytest.MonkeyPatch.context() as transport:
        transport.setattr(httpx, "Client", lambda *args, **kwargs:
            real_client(*args, **kwargs, transport=httpx.MockTransport(iteration_http)))
        await grant_repository_iteration_consent(service.repository_source_service, jobs, job_id=root_id,
            owner=owner, request=RepoRepairEgressConsentRequest(expected_job_revision=preview["revision"],
                source_packet_digest=packet["artifact_sha256"],
                expected_source_manifest_digest=packet["source_manifest_sha256"],
                expected_profile_id=egress["effective_profile_id"], acknowledged_selected_source=True,
                idempotency_key="lane-contact", expected_iteration_index=review["iteration_index"],
                expected_iteration_id=review["iteration_id"], expected_preparation_digest=review["preparation_digest"],
                expected_request_body_digest=egress["request_body_digest"],
                expected_request_route_digest=egress["request_route_digest"],
                expected_egress_envelope_digest=egress["egress_envelope_digest"],
                expected_diagnostics_digest=egress["diagnostics_digest"],
                expected_redaction_version=egress["redaction_version"], acknowledged_diagnostics=True),
            general_task_service=service, principal=flow["operator"].principal)
    async with flow["factory"]() as db:
        proposal = await db.get(RepoRepairProposal, "repository-proposal:" + review["iteration_id"])
    approval = await approval_repository.get(proposal.approval_id)
    await approval_repository.resolve_exact(approval.id, "approved", expected_digest=approval_decision_digest(approval),
        owner_principal_id=owner.principal_id, operator_session_id=owner.session_id)
    return await execute_repository_iteration(service.repository_source_service, jobs, job_id=root_id, owner=owner,
        request=RepoRepairResumeRequest(approval_id=proposal.approval_id, proposal_id=proposal.proposal_id,
            expected_proposal_revision=proposal.revision,
            expected_job_revision=(await jobs.get_job(root_id))["revision"], idempotency_key="lane-process"),
        principal=flow["operator"].principal)


@pytest.mark.asyncio
async def test_operator_stop_after_original_cutoff_closes_by_deadline(
        accounting_db, monkeypatch, repository_admission_signer):
    from src.workflows.repo_repair_stop import stop_repository_root
    flow = await _original(accounting_db, monkeypatch,
        task_limits=TaskLimits(max_inference_calls=5, max_cost_microusd=500, wall_seconds=8))
    await asyncio.sleep(max(0, (await _cutoff(flow) - datetime.now(timezone.utc)).total_seconds()) + 0.05)
    # The default operator reason, after the original group has expired: the
    # expired group authority cannot back an ordinary Stop, so the original
    # closes through its own deadline cleanup instead of staying held.
    stopped = await stop_repository_root(flow["service"].repository_source_service, flow["jobs"],
        job_id=flow["root_id"], owner=flow["owner"], general_task_service=flow["service"])
    assert stopped["pending"] is False
    await _assert_released(flow, status="failed", reason="deadline_exhausted")
    await _successor_runs(flow, "after-expired-operator-stop")


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["closer", "next_admission"])
async def test_backend_closes_expired_original_without_live_operator_session(
        accounting_db, monkeypatch, repository_admission_signer, entry):
    from src.workflows.repo_repair_stop import close_expired_repository_roots
    flow = await _original(accounting_db, monkeypatch,
        work_limits={"max_iterations": 3, "max_total_seconds": 5, "max_cost_usd": 0.0010019})
    source, jobs = flow["service"].repository_source_service, flow["jobs"]
    # Nothing is due before the original cutoff.
    assert await close_expired_repository_roots(source, jobs) == []
    await asyncio.sleep(max(0, (await _cutoff(flow) - datetime.now(timezone.utc)).total_seconds()) + 0.05)
    if entry == "closer":
        # The original operator session is gone; the backend still closes.
        async with flow["factory"]() as db:
            session = await db.get(OperatorSession, flow["owner"].session_id)
            session.idle_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
        assert await close_expired_repository_roots(source, jobs) == [flow["root_id"]]
        await _assert_released(flow, status="failed", reason="deadline_exhausted")
        assert await close_expired_repository_roots(source, jobs) == []
    else:
        # The next repository admission closes the expired holder first.
        await _successor_runs(flow, "after-expired-holder")
        await _assert_released(flow, status="failed", reason="deadline_exhausted")


@pytest.mark.asyncio
async def test_live_holder_denies_successor_before_claim_until_stopped(
        accounting_db, monkeypatch, repository_admission_signer):
    from src.workflows.repo_repair_source import prepare_repository_native_source
    from src.workflows.repo_repair_stop import stop_repository_root
    flow = await _original(accounting_db, monkeypatch)
    _, _, service, jobs, binding, _ = await admit_existing_source_request(flow["factory"], flow["owner"],
        flow["service"], flow["request"].model_copy(update={"idempotency_key": "busy-successor"}), claim_child=False)
    with pytest.raises(BoardError) as busy:
        await prepare_repository_native_source(service, jobs, binding,
            child_owner="busy-successor-worker", principal=flow["operator"].principal)
    assert busy.value.code == "repository_execution_busy" and busy.value.status_code == 409
    # Denied before claim: the successor child is untouched and retryable.
    pending = await jobs.get_job(binding.invocation_id)
    assert pending["status"] in {"accepted", "queued"} and not pending["attempt_count"]
    stopped = await stop_repository_root(service.repository_source_service, jobs, job_id=flow["root_id"],
        owner=flow["owner"], general_task_service=service)
    assert stopped["pending"] is False
    await _assert_released(flow, status="cancelled", reason="operator_cancelled")
    retried = await prepare_repository_native_source(service, jobs, binding,
        child_owner="busy-successor-worker", principal=flow["operator"].principal)
    assert retried["awaiting_repository_consent"] is True


_TRUNCATED_OUTPUT = ("--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n"
    "+    print('x' * 2000000)\n     return a - b\n")
_HUNG_TEST = ("--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n"
    "+    while True: pass\n     return a - b\n")


@pytest.mark.asyncio
async def test_truncated_check_output_is_partial_and_stop_releases_the_lane(
        accounting_db, monkeypatch, repository_admission_signer):
    from src.workflows.repo_repair_source import _repository_record
    from src.workflows.repo_repair_stop import stop_repository_root
    flow = await _original(accounting_db, monkeypatch)
    executed = await _execute(flow, _TRUNCATED_OUTPUT)
    # Output beyond the stream bound is never a requested-check result.
    assert executed["status"] == "held_partial" and executed["cleanup_proven"] is True
    async with flow["factory"]() as db:
        root = await flow["jobs"]._fetch(db, flow["root_id"])
        assert root.status == "running"
        readback = _repository_record(root, "repository:readback:" + executed["iteration_id"])
        assert readback["status"] == "held_partial" and readback["command_results"] == []
    stopped = await stop_repository_root(flow["service"].repository_source_service, flow["jobs"],
        job_id=flow["root_id"], owner=flow["owner"], general_task_service=flow["service"])
    assert stopped["pending"] is False
    await _assert_released(flow, status="cancelled", reason="operator_cancelled")
    async with flow["factory"]() as db:
        root = await flow["jobs"]._fetch(db, flow["root_id"])
        # The partial logs stay retained with the closed original.
        assert _repository_record(root, "repository:readback:" + executed["iteration_id"])["status"] == "held_partial"
    await _successor_runs(flow, "after-truncated-output")


@pytest.mark.asyncio
async def test_hung_check_times_out_at_the_original_cutoff_and_releases_the_lane(
        accounting_db, monkeypatch, repository_admission_signer):
    flow = await _original(accounting_db, monkeypatch,
        work_limits={"max_iterations": 3, "max_total_seconds": 20, "max_cost_usd": 0.0010019})
    from src.workflows.repo_repair_stop import close_expired_repository_roots
    executed = await _execute(flow, _HUNG_TEST)
    # The producer kills the hung command inside its original cutoff and
    # proves cleanup; a timed-out command is a partial, not a check result.
    assert executed["status"] == "held_partial" and executed["cleanup_proven"] is True
    await asyncio.sleep(max(0, (await _cutoff(flow) - datetime.now(timezone.utc)).total_seconds()) + 0.05)
    # The backend closes the expired original by its own deadline cause.
    source = flow["service"].repository_source_service
    assert await close_expired_repository_roots(source, flow["jobs"]) == [flow["root_id"]]
    await _assert_released(flow, status="failed", reason="deadline_exhausted")
    await _successor_runs(flow, "after-hung-check")


@pytest.mark.asyncio
async def test_concurrent_admissions_cannot_both_claim_the_lane(accounting_db, monkeypatch, repository_admission_signer):
    from src.auth.service import authenticate_session
    from src.workflows.repo_repair_source import prepare_repository_native_source
    from src.workflows.repo_repair_stop import stop_repository_root
    from tests.test_repo_work_task_publication import actual_native_source
    factory, owner, service, jobs, first, request = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=4, claim_child=False)
    _, _, _, _, second, _ = await admit_existing_source_request(factory, owner, service,
        request.model_copy(update={"idempotency_key": "concurrent-second"}), claim_child=False)
    operator = await authenticate_session(owner.session_id, touch=False)
    bindings = (first, second)
    results = await asyncio.gather(*(prepare_repository_native_source(service, jobs, binding,
        child_owner="concurrent-worker-" + str(index), principal=operator.principal)
        for index, binding in enumerate(bindings)), return_exceptions=True)
    winners = [(binding, result) for binding, result in zip(bindings, results) if isinstance(result, dict)]
    losers = [(binding, result) for binding, result in zip(bindings, results) if isinstance(result, BaseException)]
    assert len(winners) == 1 and len(losers) == 1, results
    (loser, error), (_, won) = losers[0], winners[0]
    assert isinstance(error, BoardError) and error.code == "repository_execution_busy"
    # The loser never claimed its child, so it stays retryable.
    pending = await jobs.get_job(loser.invocation_id)
    assert pending["status"] in {"accepted", "queued"} and not pending["attempt_count"]
    stopped = await stop_repository_root(service.repository_source_service, jobs,
        job_id=won["repository_job_id"], owner=owner, general_task_service=service)
    assert stopped["pending"] is False
    retried = await prepare_repository_native_source(service, jobs, loser,
        child_owner="concurrent-retry", principal=operator.principal)
    assert retried["awaiting_repository_consent"] is True


@pytest.mark.asyncio
async def test_closer_backs_off_a_root_whose_closure_stays_pending(accounting_db, monkeypatch, repository_admission_signer):
    from src.workflows import repo_repair_stop as stop
    flow = await _original(accounting_db, monkeypatch,
        work_limits={"max_iterations": 3, "max_total_seconds": 5, "max_cost_usd": 0.0010019})
    source, jobs = flow["service"].repository_source_service, flow["jobs"]
    await asyncio.sleep(max(0, (await _cutoff(flow) - datetime.now(timezone.utc)).total_seconds()) + 0.05)
    attempts = []
    async def pending_stop(*args, **kwargs):
        attempts.append(kwargs["job_id"])
        return {"pending": True}
    with monkeypatch.context() as pending:
        pending.setattr(stop, "stop_repository_root", pending_stop)
        assert await stop.close_expired_repository_roots(source, jobs) == []
        assert await stop.close_expired_repository_roots(source, jobs) == []
    assert attempts == [flow["root_id"]]  # Not re-staged on the next pass.
    source._expiry_closer_retry_at[flow["root_id"]] = 0.0  # The back-off has elapsed.
    assert await stop.close_expired_repository_roots(source, jobs) == [flow["root_id"]]
    await _assert_released(flow, status="failed", reason="deadline_exhausted")
