"""Actual Stop issuance negative integration; no synthetic optional grant."""
import asyncio
import copy

import pytest

from src.workflows import repo_repair_source as source
from src.workflows import repo_repair_source_recovery as recovery
from src.workflows import repo_repair_stop as stop
from src.workflows.specialist_delegation import current_delegation
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_source_recovered_finalizer import _signed_ownerless_fixture, _scope, _rows, DENIED


@pytest.mark.asyncio
async def test_genuine_stop_context_never_grants_unstaged_specialist_scope(
        accounting_db, monkeypatch, repository_admission_signer):
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, "test_python",
        failed=True, stop_requested=True)
    service, jobs, owner = flow["service"], flow["jobs"], flow["kwargs"]["owner"]
    async with await _scope(flow) as completion:
        fence = recovery.repository_scoped_completion_fence(completion)
        actual = await source.stage_repository_completion_post_context(service, jobs,
            witness=completion, owner=owner, fence=fence)
        stop.assert_repository_stop_context(actual, service=service, jobs=jobs)
        assert actual["scope_fence"] is fence
        assert actual["envelope"].specialist_handoff is None  # Actual original parent, never coerced.
        before = await _rows(jobs)
        from src.work_board import general_task_runtime_artifacts as native
        def forbidden(*args, **kwargs):
            raise AssertionError("Missing selected specialist coverage must not read a file")
        with monkeypatch.context() as patch:
            patch.setattr(native, "read_native_artifact_reference", forbidden)
            for candidate in (actual, copy.copy(actual), object()):
                with pytest.raises(DENIED):
                    stop.specialist_native_physical(candidate,
                        invocation_id=actual["binding"].invocation_id)
                async with jobs._session() as db:
                    with pytest.raises(DENIED):
                        await current_delegation(db, actual["binding"].invocation_id,
                            _repository_stop_context=candidate)
            async def foreign_task():
                with pytest.raises(DENIED):
                    stop.specialist_native_physical(actual,
                        invocation_id=actual["binding"].invocation_id)
            await asyncio.create_task(foreign_task())
        assert await _rows(jobs) == before
    # A genuine registered context cannot preserve an expired fence across calls.
    with pytest.raises(DENIED):
        stop.specialist_native_physical(actual, invocation_id=actual["binding"].invocation_id)
    assert await _rows(jobs) == before
