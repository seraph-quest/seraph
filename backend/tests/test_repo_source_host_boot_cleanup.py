"""Genuine current-boot negatives; no fabricated cross-boot positive proof."""
import copy

import pytest

from src.workflows import repo_repair_source_recovery as recovery
from src.execution import repo_original_producer as producer
from src.work_board.contracts import WorkBoardOwner
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_source_recovered_finalizer import _signed_ownerless_fixture, _rows


def _private_bytes(service):
    root = service._workspace()
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()}


async def _scope(flow):
    async with flow["jobs"]._session() as db:
        run = await flow["jobs"]._fetch(db, flow["kwargs"]["job_id"])
        assert run.status == "unknown_external_effect"
        assert flow["jobs"]._repo_repair_reservation_state(run)["status"] == "held"
        return {"job_id": run.run_identity, "owner": flow["kwargs"]["owner"],
            "expected_job_revision": run.revision}


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
@pytest.mark.parametrize("public", [False, True])
async def test_genuine_current_boot_never_releases_unknown_physical_or_accounting_debt(
        accounting_db, monkeypatch, repository_admission_signer, language, public):
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, language, unknown=True)
    args = await _scope(flow)
    before_rows, before_files = await _rows(flow["jobs"]), _private_bytes(flow["service"])
    # These are forbidden only during recovery, after the actual original child
    # produced its genuine private bundle and original Unknown/Stop owners ran.
    def no_signal_or_dispatch(*args, **kwargs):
        pytest.fail("Physical-only recovery attempted a signal or process dispatch")
    from src.execution import repo_supervisor
    monkeypatch.setattr(producer.subprocess, "Popen", no_signal_or_dispatch)
    monkeypatch.setattr(repo_supervisor, "pidfd_send", no_signal_or_dispatch)
    if public:
        with pytest.raises(recovery.RepositorySourceRecoveryError) as denied:
            await recovery.recover_original_repository_cleanup(flow["service"], flow["jobs"],
                **args, action="settle_original_host_boot_cleanup")
        assert denied.value.code == "repository_source_recovery_unavailable"
        assert denied.value.status_code == 503
    else:
        with pytest.raises(ValueError, match="^original_host_boot_unchanged$"):
            await recovery._settle_original_host_boot_cleanup(flow["service"], flow["jobs"], **args)
    assert await _rows(flow["jobs"]) == before_rows
    assert _private_bytes(flow["service"]) == before_files
    async with flow["jobs"]._session() as db:
        run = await flow["jobs"]._fetch(db, args["job_id"])
        packet = recovery.repository_source_recovery_projection(flow["service"], flow["jobs"], run)
        assert packet["physical_hold"] is True
        assert packet["state"] != "physical_cleanup_only"


@pytest.mark.asyncio
@pytest.mark.parametrize("negative", ["stale", "foreign_session"])
async def test_host_boot_original_owner_denial_precedes_any_host_observation(
        accounting_db, monkeypatch, repository_admission_signer, negative):
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, "test_python", unknown=True)
    args = await _scope(flow)
    before_rows, before_files = await _rows(flow["jobs"]), _private_bytes(flow["service"])
    if negative == "stale":
        args["expected_job_revision"] -= 1
        code = "repository_source_recovery_stale"
    else:
        args["owner"] = WorkBoardOwner(principal_id=args["owner"].principal_id, session_id="foreign-session")
        code = "repository_source_recovery_owner_changed"
    def no_observation(*args, **kwargs):
        pytest.fail("Denied original owner reached a physical host observation")
    monkeypatch.setattr(producer, "_host_boot_id", no_observation)
    with pytest.raises(recovery.RepositorySourceRecoveryError) as denied:
        await recovery._settle_original_host_boot_cleanup(flow["service"], flow["jobs"], **args)
    assert denied.value.code == code
    assert await _rows(flow["jobs"]) == before_rows
    assert _private_bytes(flow["service"]) == before_files


@pytest.mark.asyncio
async def test_genuine_registration_missing_kernel_observation_is_fail_closed(
        accounting_db, monkeypatch, repository_admission_signer):
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, "test_python", unknown=True)
    args = await _scope(flow)
    before_rows, before_files = await _rows(flow["jobs"]), _private_bytes(flow["service"])
    def unavailable():
        raise OSError("original kernel boot observation unavailable")
    monkeypatch.setattr(producer, "_host_boot_id", unavailable)
    with pytest.raises(OSError, match="^original kernel boot observation unavailable$"):
        await recovery._settle_original_host_boot_cleanup(flow["service"], flow["jobs"], **args)
    assert await _rows(flow["jobs"]) == before_rows
    assert _private_bytes(flow["service"]) == before_files


@pytest.mark.asyncio
async def test_direct_host_boot_entry_requires_original_active_source_fence(
        accounting_db, monkeypatch, repository_admission_signer):
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, "test_python", unknown=True)
    before_rows, before_files = await _rows(flow["jobs"]), _private_bytes(flow["service"])
    copied = copy.deepcopy(flow["registration"])
    # This proves direct entry without a Source fence is denied before any
    # caller metadata is inspected; it is not a copied-registration gate test.
    copied["native_host_binding"]["machine_digest"] = "0" * 64
    with pytest.raises(recovery.RepositorySourceRecoveryError, match="^repository_source_recovery_fence_unavailable$"):
        with producer._stage_original_host_boot_cleanup(copied, service=flow["service"], jobs=flow["jobs"],
                owner=flow["kwargs"]["owner"], fence=None):
            pytest.fail("Copied caller metadata supplied host-boot authority")
    assert await _rows(flow["jobs"]) == before_rows
    assert _private_bytes(flow["service"]) == before_files


@pytest.mark.asyncio
@pytest.mark.parametrize("physical", ["missing", "empty"])
async def test_authenticated_source_get_rejects_unproven_host_boot_release_without_effects(
        accounting_db, monkeypatch, repository_admission_signer, physical):
    import json
    from sqlalchemy import update
    from src.workflows.job_runtime import _canonical, _digest
    from src.workflows import repo_repair_source as source
    from tests.test_repo_source_public_recovery import _authenticated_original_api
    async with _authenticated_original_api(accounting_db, monkeypatch, "test_python", unknown=True) as (flow, _, client):
        jobs, service = flow["jobs"], flow["service"]
        job_id = flow["kwargs"]["job_id"]
        url = "/api/workflows/repo-repair/" + job_id
        baseline = await client.get(url)
        assert baseline.status_code == 200, baseline.text
        assert baseline.json()["source_recovery"]["physical_hold"] is True
        async with jobs._session() as db:
            run = await jobs._fetch(db, job_id)
            held = jobs._repo_repair_reservation_state(run)
            assert held["status"] == "held"
            assert source._repository_record(run, "repository:stop-intent:v1") is not None
            assert source._repository_record(run, "repository:stop-uncertainty-successor:v1") is not None
            journal = json.loads(run.checkpoint_receipts_json)
            if physical == "empty":
                journal.append({"checkpoint_id": "repository:physical-cleanup:v1", "safe": True,
                    "payload": {}, "state_digest": _digest({}), "created_at": held["recorded_at"]})
            # Deliberately invalid isolated DB damage: no real or synthetic
            # changed-boot proof and no mutation owner receives this packet.
            release = {**held, "status": "released", "cleanup_proven": True, "readback_verified": False,
                "outcome_status": "unknown_external_effect", "readback_scope": "original_host_boot_cleanup",
                "physical_cleanup_digest": _digest({})}
            journal.append({"checkpoint_id": "repo-repair-execution-release", "safe": True,
                "payload": release, "state_digest": _digest(release), "created_at": held["recorded_at"]})
            await db.execute(update(type(run)).where(type(run).id == run.id).values(
                checkpoint_receipts_json=_canonical(journal), updated_at=run.updated_at))
            await db.commit()
        before_rows, before_files = await _rows(jobs), _private_bytes(service)
        blocked = await client.get(url)
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"] == {"code": "repair_recovery_blocked",
            "reason": "original repository host boot cleanup release proof is malformed", "operator_visible": True}
        assert await _rows(jobs) == before_rows
        assert _private_bytes(service) == before_files
