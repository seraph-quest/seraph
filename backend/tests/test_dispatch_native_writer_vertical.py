"""Observe selected original repository boundaries in the genuine Source journey."""
import json
import traceback
from types import SimpleNamespace

import pytest

from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_native_memory_report_source_vertical import test_actual_accepted_report_source_with_mocked_browser_edge as _original_journey


@pytest.mark.asyncio
async def test_original_dispatch_writer_journey(accounting_db, real_auth, monkeypatch, record_property):
    from src.work_board.repository import WorkBoardRepository

    failures = []
    def observer(original, entry):
        async def observe(self, *args, **kwargs):
            try:
                return await original(self, *args, **kwargs)
            except Exception as exc:
                failures.append({"entry": entry, "action": kwargs.get("action"),
                    "code": getattr(exc, "code", None), "type": type(exc).__name__,
                    "message": str(exc), "traceback": traceback.format_exc()})
                raise
        return observe

    for entry in ("_begin_dispatch_writer", "project_attempt", "_project_attempt", "link_attempt_workflow_run"):
        monkeypatch.setattr(WorkBoardRepository, entry, observer(getattr(WorkBoardRepository, entry), entry))
    from src.work_board import input_artifacts
    original_consume = input_artifacts.consume_input_artifact
    async def observe_consume(*args, **kwargs):
        try:
            return await original_consume(*args, **kwargs)
        except Exception as exc:
            failures.append({"entry": "consume_input_artifact", "code": getattr(exc, "code", None),
                "type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()})
            raise
    monkeypatch.setattr(input_artifacts, "consume_input_artifact", observe_consume)
    from src.work_board import pipelines
    def observe_function(original, entry):
        async def call(*args, **kwargs):
            try:
                return await original(*args, **kwargs)
            except Exception as exc:
                failures.append({"entry": entry, "code": getattr(exc, "code", None),
                    "type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()})
                raise
        return call
    for module, entry in ((pipelines, "advance"), (input_artifacts, "prepare_input_artifact")):
        monkeypatch.setattr(module, entry, observe_function(getattr(module, entry), entry))
    try:
        await _original_journey(
            accounting_db, real_auth, monkeypatch, record_property)
    finally:
        path = accounting_db[0] / "original-dispatch-writer-diagnostic.json"
        path.write_text(json.dumps(failures, indent=2))
        path.chmod(0o600)


@pytest.mark.asyncio
async def test_constructed_physical_stage_has_no_authority(accounting_db):
    from src.work_board.repository import BoardError
    from src.work_board.review import _DispatchReadbacks, _recheck_dispatch_projection
    from src.work_board.research_readback import _DossierProjection, _recheck_dossier_projection
    from sqlalchemy import text

    _root, _engine, factory = accounting_db
    async with factory.accounting_sessions() as db:
        # Real FILE SQLite; constructed identity handles carry no registration,
        # physical bytes, current rows or authenticated original invocation.
        assert await db.scalar(text("select 1")) == 1
        task = SimpleNamespace(task_id="constructed-unbound-task")
        with pytest.raises(BoardError, match="Original projection stage required"):
            await _recheck_dispatch_projection(db, task, None, None, {}, _DispatchReadbacks())
        with pytest.raises(ValueError, match="research original staged readback changed"):
            await _recheck_dossier_projection(db, task, None, None, _DossierProjection())
