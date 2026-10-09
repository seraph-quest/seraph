"""Genuine original owners; internal candidate only, no public recovery proof."""
import copy
import json
import os
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import OperatorSession, WorkBoardEvent
from src.workflows import repo_repair_source as source
from src.workflows import repo_repair_source_recovery as recovery
from src.workflows import repo_repair_stop as stop
from src.workflows.job_runtime import DurableJobLeaseError, _utc_now
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_source_recovered_finalizer import _signed_ownerless_fixture, _scope, _rows, DENIED


def _retain_original_node_runtime(monkeypatch, language):
    if language == "test_node":
        node = Path("/home/pawel/repos/seraph/.agent-worktrees/986-a1-host/.agent-evidence/986/a1-upstream/node-v24.13.1-linux-x64/bin/node")
        assert node.is_file(), "Required cached fixed Node24 runtime; no ambient fallback or download"
        monkeypatch.setattr(settings, "repo_sandbox", settings.repo_sandbox.model_copy(
            update={"node_runtime_path": str(node)}))


@contextmanager
def _physical_outside_sql(monkeypatch, flow):
    """Observe real helpers, including short readers; never supply authority."""
    from src.execution import repo_original_producer as producer
    from src.work_board import pipelines, general_task_runtime_artifacts as native
    from src.execution import repo_sandbox
    jobs, service = flow["jobs"], flow["service"]
    session = jobs._session
    active = {"sessions": 0, "guard_entries": 0}
    with monkeypatch.context() as patch:
        @asynccontextmanager
        async def observed_session():
            async with session() as db:
                active["sessions"] += 1
                try:
                    yield db
                finally:
                    active["sessions"] -= 1
        patch.setattr(jobs, "_session", observed_session)
        for target, name in (
            (producer, "original_producer_completion_result"),
            (producer, "_current_native_host_binding"),
            (producer, "_assert_original_pid_absent"),
            (producer, "_read_registered_bundle"),
            (pipelines, "root_binding"), (native, "read_native_artifact_reference"),
            (repo_sandbox, "load_persisted_repo_sandbox_settings"),
            (service, "_read_private_artifact"), (service, "_write_private_artifact"),
            (service, "recheck_task_source_snapshot"),
            *((os, name) for name in ("open", "stat", "fstat", "lstat", "readlink")),
            *((Path, name) for name in ("read_bytes", "read_text", "open", "resolve", "stat", "lstat")),
        ):
            real = getattr(target, name)
            def outside(*args, _real=real, _name=name, **kwargs):
                assert active["sessions"] == 0, "physical helper inside SQL reader: " + _name
                return _real(*args, **kwargs)
            patch.setattr(target, name, outside)
        real_stage = producer.stage_original_producer_completion
        @contextmanager
        def observed_stage(*args, **kwargs):
            assert active["sessions"] == 0
            active["guard_entries"] += 1
            with real_stage(*args, **kwargs) as actual:
                yield actual
        patch.setattr(producer, "stage_original_producer_completion", observed_stage)
        yield active


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
@pytest.mark.parametrize("committed_first", [False, True])
async def test_genuine_original_stop_one_guard_terminal_readback(
        accounting_db, monkeypatch, repository_admission_signer, language, committed_first):
    _retain_original_node_runtime(monkeypatch, language)
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, language,
        failed=True, stop_requested=True)
    jobs, service, owner = flow["jobs"], flow["service"], flow["kwargs"]["owner"]
    job_id = flow["kwargs"]["job_id"]
    if committed_first:
        async with await _scope(flow):
            pass  # Actual original publisher commits cleanup; no finalizer or fake receipt.
    async with jobs._session() as db:
        revision = (await jobs._fetch(db, job_id)).revision
    with _physical_outside_sql(monkeypatch, flow) as observed:
        result = await recovery._reconcile_original_repository_cleanup(service, jobs,
            job_id=job_id, owner=owner, expected_job_revision=revision)
    assert observed["guard_entries"] == 1
    assert result["status"] == "cancelled" and result["no_learning"] is True
    assert result["source_recovery"]["state"] == "original_stop_committed"
    assert result["source_recovery"]["physical_hold"] is False
    assert result["source_recovery"]["public_actions"] == "unavailable"
    assert job_id not in service._iterative_lanes
    async with jobs._session() as db:
        root = await jobs._fetch(db, job_id)
        original, *_ = source.read_repository_original(root)
        assert jobs._repo_repair_reservation_state(root)["status"] == "released"
        assert source._repository_record(root, "repository:terminal:v1")["schema"] == "repository.stop_terminal.v1"
        for identity in (original["native_binding"]["parent_job_id"], original["native_binding"]["invocation_id"]):
            assert (await jobs._fetch(db, identity)).status == "cancelled"
        events = list((await db.scalars(select(WorkBoardEvent).where(
            WorkBoardEvent.task_id == original["repository_task_id"],
            WorkBoardEvent.kind == "attempt.repository_stopped"))).all())
        assert len(events) == 1
        assert json.loads(events[0].metadata_json)["no_learning"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
async def test_genuine_committed_unknown_get_fresh_auth_and_exact_scope(
        accounting_db, monkeypatch, repository_admission_signer, language):
    from src.auth.service import authenticate_session
    _retain_original_node_runtime(monkeypatch, language)
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, language, unknown=True)
    jobs, service, owner = flow["jobs"], flow["service"], flow["kwargs"]["owner"]
    job_id = flow["kwargs"]["job_id"]
    async with await _scope(flow):
        pass
    # Make the original auth touch genuinely due; the real auth owner refreshes
    # its actual session, then GET independently stages the current full row.
    async with jobs._session() as db:
        session = await db.get(OperatorSession, owner.session_id)
        session.last_seen_at = _utc_now() - timedelta(seconds=60)
        await db.commit()
    await authenticate_session(owner.session_id, touch=True)
    before = await _rows(jobs)
    with _physical_outside_sql(monkeypatch, flow) as observed:
        result = await source.repository_operator_projection(service, jobs, job_id=job_id, owner=owner)
    assert observed["guard_entries"] == 1
    assert await _rows(jobs) == before
    assert result["status"] == "unknown_external_effect"
    assert result["repository_stop"]["pending"] is True
    assert result["source_recovery"]["physical_hold"] is True
    assert result["source_recovery"]["public_actions"] == "unavailable"
    async with jobs._session() as db:
        revision = (await jobs._fetch(db, job_id)).revision
    async with recovery.stage_repository_knownpost_completion(service, jobs, job_id=job_id,
            owner=owner, iteration_index=1, expected_job_revision=revision) as actual:
        for bad in (copy.copy(actual), object()):
            with pytest.raises(DENIED):
                recovery.repository_completion_knownpost_context(bad, service=service, jobs=jobs,
                    owner=owner, job_id=job_id)
        for changes in ({"jobs": object()}, {"job_id": "foreign"},
                {"owner": owner.model_copy(update={"session_id": "foreign"})}):
            args = {"service": service, "jobs": jobs, "owner": owner, "job_id": job_id, **changes}
            with pytest.raises(DENIED):
                recovery.repository_completion_knownpost_context(actual, **args)
        async with jobs._session() as db:
            with pytest.raises(DENIED):
                await source._repository_operator_projection_sql(db, service, jobs, job_id=job_id, owner=owner)
            transaction = await db.begin_nested()
            session = await db.get(OperatorSession, owner.session_id, populate_existing=True)
            session.idle_expires_at = _utc_now() - timedelta(seconds=1)
            await db.flush()
            with pytest.raises(DENIED):
                await recovery.validate_repository_knownpost_projection_sql(db, service, jobs,
                    witness=actual, owner=owner, job_id=job_id)
            await transaction.rollback()
    with pytest.raises(DENIED):
        recovery.repository_completion_knownpost_context(actual, service=service, jobs=jobs,
            owner=owner, job_id=job_id)
    assert await _rows(jobs) == before


@pytest.mark.asyncio
async def test_original_stop_borrow_rejects_copied_completion_without_mutation(
        accounting_db, monkeypatch, repository_admission_signer):
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, "test_python",
        failed=True, stop_requested=True)
    jobs, service, owner = flow["jobs"], flow["service"], flow["kwargs"]["owner"]
    async with await _scope(flow) as actual:
        fence = recovery.repository_scoped_completion_fence(actual)
        context = await source.stage_repository_completion_post_context(service, jobs,
            witness=actual, owner=owner, fence=fence)
        before = await _rows(jobs)
        for bad in (copy.copy(actual), object()):
            with pytest.raises(DENIED):
                async with recovery.stage_repository_original_stop_completion(service, jobs,
                        context=context, owner=owner, fence=fence, completion_witness=bad):
                    pytest.fail("invalid completion may not reopen or borrow a primary")
            assert await _rows(jobs) == before
