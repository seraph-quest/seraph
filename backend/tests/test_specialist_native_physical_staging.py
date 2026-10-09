"""Real original specialist claims/waits; physical parsing before selected SQL."""
import pytest
from sqlalchemy import select

from tests.general_task_method_lifecycle import native_admission_lifecycle
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime
from tests.test_specialist_evidence_runtime import copied_evidence_fixture, reserve_evidence_specialist


async def original_stage(task_runtime, monkeypatch, phase):
    from src.auth.service import authenticate_session
    from src.db.models import WorkflowRunState
    from src.workflows.specialist_delegation import current_delegation, capture_specialist_native_rows
    from src.work_board.general_task_runtime_artifacts import stage_specialist_native_physical
    fixture = await copied_evidence_fixture(task_runtime, monkeypatch)
    sessions, workspace, owner, dispatcher, service, envelope, original, *_ = fixture
    if phase == "running":
        current, _principal = await reserve_evidence_specialist(fixture)
        invocation_id = current.callback.run_identity
    else:
        operator = await authenticate_session(owner.session_id, touch=False)
        result = await service.execute(dispatcher.jobs, job_id=original["job"]["job_id"],
            owner=original["job"]["lease"]["owner"], fence=original["job"]["lease"]["fencing_token"],
            envelope=envelope, principal=operator.principal)
        assert not result.get("verified")
        async with sessions() as db:
            callback = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.failure_reason == "specialist_wait"))
            assert callback is not None
            invocation_id = callback.run_identity
    async with sessions() as db:
        current = await current_delegation(db, invocation_id)
        capture = await capture_specialist_native_rows(db, current.callback,
            evidence_refs=current.request.evidence_refs)
    physical = await stage_specialist_native_physical(capture)
    return sessions, invocation_id, capture, physical, current


def deny_selected_filesystem(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("selected specialist SQL attempted physical read")
    monkeypatch.setattr("src.work_board.pipelines.root_binding", denied)
    monkeypatch.setattr("src.work_board.input_artifacts._safe_file_bytes", denied)
    monkeypatch.setattr("src.work_board.general_task_runtime_artifacts.read_native_artifact_reference", denied)


@pytest.mark.parametrize("phase", ("running", "waiting"))
@pytest.mark.asyncio
async def test_original_claim_and_wait_semantics_consume_real_staged_bytes_without_sql_io(
        task_runtime, monkeypatch, native_admission_lifecycle, phase):
    from src.workflows.specialist_delegation import _current_delegation_data
    sessions, invocation_id, capture, physical, original = await original_stage(task_runtime, monkeypatch, phase)
    deny_selected_filesystem(monkeypatch)
    async with sessions() as db:
        current = await _current_delegation_data(db, invocation_id, _specialist_physical=physical)
        assert current.request == original.request
        assert current.reservation == original.reservation
        assert current.callback.status == ("running" if phase == "running" else "paused")
        assert current.callback.fencing_token == original.callback.fencing_token
        assert current.manifest == capture["manifest"]


@pytest.mark.parametrize("phase", ("running", "waiting"))
@pytest.mark.parametrize("scope,field", (("callback", "fencing_token"), ("parent", "revision"),
    ("task", "task_revision"), ("attempt", "fencing_token"), ("artifact", "revision")))
@pytest.mark.asyncio
async def test_staged_original_specialist_rejects_actual_row_drift_before_any_physical_fallback(
        task_runtime, monkeypatch, native_admission_lifecycle, phase, scope, field):
    from src.workflows.specialist_delegation import _current_delegation_data
    from src.work_board.repository import BoardError
    from src.workflows.job_runtime import DurableJobLeaseError
    from sqlalchemy import inspect
    sessions, invocation_id, capture, physical, _original = await original_stage(task_runtime, monkeypatch, phase)
    changed = capture[scope]
    identity = inspect(changed).identity
    async with sessions() as db:
        row = await db.get(type(changed), identity, populate_existing=True)
        setattr(row, field, getattr(row, field) + 1)
    deny_selected_filesystem(monkeypatch)
    async with sessions() as db:
        with pytest.raises((BoardError, DurableJobLeaseError)):
            await _current_delegation_data(db, invocation_id, _specialist_physical=physical)


@pytest.mark.asyncio
async def test_selected_physical_reference_missing_never_falls_back_to_original_files(
        task_runtime, monkeypatch, native_admission_lifecycle):
    from src.workflows.specialist_delegation import _current_delegation_data
    from src.work_board.repository import BoardError
    sessions, invocation_id, _capture, physical, _original = await original_stage(task_runtime, monkeypatch, "running")
    missing = {**physical, "references": {}}
    deny_selected_filesystem(monkeypatch)
    async with sessions() as db:
        with pytest.raises(BoardError):
            await _current_delegation_data(db, invocation_id, _specialist_physical=missing)


@pytest.mark.asyncio
async def test_unissued_stop_context_is_rejected_before_physical_or_semantic_fallback(
        task_runtime, monkeypatch, native_admission_lifecycle):
    from src.workflows.specialist_delegation import current_delegation
    from src.workflows.job_runtime import DurableJobLeaseError
    sessions, invocation_id, _capture, _physical, _original = await original_stage(task_runtime, monkeypatch, "running")
    deny_selected_filesystem(monkeypatch)
    async with sessions() as db:
        with pytest.raises(DurableJobLeaseError):
            await current_delegation(db, invocation_id, _repository_stop_context=object())
