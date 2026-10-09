"""Original browser/dossier/report journey and four retained consumer writers."""
from contextvars import ContextVar
from importlib.machinery import SourceFileLoader
import asyncio
import json
import sys

import pytest
from sqlalchemy import event, select

from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_native_memory_report_source_vertical import (
    test_actual_accepted_report_source_with_mocked_browser_edge as _original_journey,
)


@pytest.mark.asyncio
async def test_original_pipeline_four_writer_journey(accounting_db, real_auth, monkeypatch, record_property):
    from src.db.models import WorkBoardInputArtifact, WorkBoardProposal, WorkBoardTask
    from src.runtime_plugins import ownership
    from src.work_board import input_artifacts, pipeline_cpu
    from src.guardian import opportunity_runtime
    from src.workspace.accounting_witness import CompositionSessionGuard

    phases = {
        "_reserve_advance_consumer": "operation_reservation",
        "_reserve_pipeline_consumer_input": "input_reservation",
        "_finalize_pipeline_consumer_input": "input_metadata",
        "_bind_advance_consumer": "consumer_binding",
    }
    selected_writer = ContextVar("original_pipeline_selected_writer", default=None)
    receipts = []
    physical_denials = []
    failures = []
    commit_receipts = {}
    original_begin = ownership.begin_native_writer

    async def observe_begin(db, **kwargs):
        frame = sys._getframe(1)
        phase = consumer_id = None
        while frame is not None:
            phase = phases.get(frame.f_code.co_name, phase)
            if frame.f_code.co_name == "_begin_pipeline_advance_writer":
                consumer_id = frame.f_locals.get("consumer_id")
            frame = frame.f_back
        result = await original_begin(db, **kwargs)
        if phase is not None:
            assert consumer_id is not None
            assert db.info["composition_writer_owner"] == "native_ingress"
            assert db.info["native_writer_started"] is True
            assert db.info.get("composition_read_guard") is None
            selected_writer.set((db, phase, consumer_id, asyncio.current_task()))
            def committed(session):
                receipt = commit_receipts[id(db)]
                receipt["commit_observed"] = True
                receipt["committed_owner"] = session.info["composition_writer_owner"]
            event.listen(db.sync_session, "after_commit", committed)
        return result

    monkeypatch.setattr(ownership, "begin_native_writer", observe_begin)
    original_publish = CompositionSessionGuard.publish

    async def observe_publish(self):
        held = selected_writer.get()
        if held is not None and held[0] is self.db:
            _db, phase, consumer_id, _actor = held
            consumer = await self.db.scalar(select(WorkBoardTask).where(
                WorkBoardTask.task_id == consumer_id).execution_options(populate_existing=True))
            proposal = await self.db.get(WorkBoardProposal, consumer.pipeline_operation_id,
                populate_existing=True)
            value = json.loads(proposal.proposal_json)
            reservation = value["reservations"][consumer.pipeline_slot]
            artifacts = list((await self.db.execute(select(WorkBoardInputArtifact).where(
                WorkBoardInputArtifact.owner_principal_id == consumer.owner_principal_id,
                WorkBoardInputArtifact.owner_session_id == consumer.owner_session_id,
                WorkBoardInputArtifact.idempotency_key == reservation["key"]))).scalars())
            receipt = {"phase": phase, "consumer_task_id": consumer_id,
                "consumer": consumer.model_dump(mode="json"),
                "operation": proposal.model_dump(mode="json"),
                "inputs": [item.model_dump(mode="json") for item in artifacts],
                "commit_observed": False}
            receipts.append(receipt)
            commit_receipts[id(self.db)] = receipt
            # The existing canonical publisher intentionally verifies files.
            # The sentinel covers selected business writers, not that owner.
            selected_writer.set(None)
        return await original_publish(self)

    monkeypatch.setattr(CompositionSessionGuard, "publish", observe_publish)

    def forbid_selected_reader(original, entry):
        def checked(*args, **kwargs):
            held = selected_writer.get()
            if held is not None and (held[3] is not asyncio.current_task()
                    or not held[0].in_transaction()
                    or held[0].info["composition_guard"].closed):
                selected_writer.set(None)
                held = None
            if held is not None:
                frames = []
                frame = sys._getframe(1)
                while frame is not None:
                    frames.append({"file": frame.f_code.co_filename,
                        "function": frame.f_code.co_name, "line": frame.f_lineno})
                    frame = frame.f_back
                physical_denials.append({"entry": entry, "phase": held[1],
                    "path": str(args[1] if entry == "get_data" else args[0]),
                    "session_identity": id(held[0]),
                    "guard_identity": id(held[0].info["composition_guard"]),
                    "guard_owner": held[0].info["composition_writer_owner"],
                    "transaction_active": held[0].in_transaction(),
                    "task_name": held[3].get_name(), "frames": frames})
                raise AssertionError("physical reader inside selected pipeline writer")
            return original(*args, **kwargs)
        return checked

    for module, name in ((input_artifacts, "_safe_file_bytes"),
                         (input_artifacts, "_write_payload"),
                         (pipeline_cpu, "read_output"),
                         (opportunity_runtime, "read_snapshot"),
                         (SourceFileLoader, "get_data")):
        monkeypatch.setattr(module, name, forbid_selected_reader(getattr(module, name), name))
    def observe_failure(original, entry):
        async def observed(*args, **kwargs):
            try:
                return await original(*args, **kwargs)
            except BaseException as exc:
                trace = []
                current = exc.__traceback__
                while current is not None:
                    trace.append({"file": current.tb_frame.f_code.co_filename,
                        "function": current.tb_frame.f_code.co_name, "line": current.tb_lineno})
                    current = current.tb_next
                failures.append({"entry": entry, "type": type(exc).__name__,
                    "code": getattr(exc, "code", None), "message": str(exc), "frames": trace})
                raise
        return observed
    for name in ("_reserve_pipeline_consumer_input", "_finalize_pipeline_consumer_input",
                 "_prepare_pipeline_consumer_input"):
        monkeypatch.setattr(input_artifacts, name, observe_failure(getattr(input_artifacts, name), name))
    from src.runtime_plugins import task_capability
    from src.work_board.dispatcher import WorkBoardDispatcher

    def observe_original_report(function, entry):
        async def observed(*args, **kwargs):
            try:
                return await function(*args, **kwargs)
            except Exception as exc:
                frames = []
                current = exc.__traceback__
                while current is not None:
                    frames.append({"file": current.tb_frame.f_code.co_filename,
                        "function": current.tb_frame.f_code.co_name, "line": current.tb_lineno})
                    current = current.tb_next
                task = next((item for item in args if isinstance(item, WorkBoardTask)), None)
                failures.append({"entry": entry, "type": type(exc).__name__,
                    "code": getattr(exc, "code", None), "frames": frames,
                    "task_id": task.task_id if task else None,
                    "task_revision": task.task_revision if task else None})
                raise
        return observed
    for name in ("execute_report", "stage_report_current"):
        monkeypatch.setattr(task_capability, name,
            observe_original_report(getattr(task_capability, name), name))
    monkeypatch.setattr(WorkBoardDispatcher, "_canonical_direct_admission",
        observe_original_report(WorkBoardDispatcher._canonical_direct_admission, "_canonical_direct_admission"))
    try:
        await _original_journey(accounting_db, real_auth, monkeypatch, record_property)
        consumers = {item["consumer_task_id"] for item in receipts}
        assert len(consumers) == 2
        for consumer_id in consumers:
            actual = [item["phase"] for item in receipts if item["consumer_task_id"] == consumer_id]
            assert actual == list(phases.values())
        assert physical_denials == []
        assert all(item["commit_observed"] and item["committed_owner"] == "native_ingress"
            for item in receipts)
        # The actual completed operation cannot publish unrelated caller state
        # through its idempotent inspection return or autoflush before owned().
        from src.db import engine as db_engine
        from src.db.models import ProgrammeDigestReceipt
        from src.work_board import pipelines
        from src.work_board.contracts import WorkBoardOwner
        from src.work_board.repository import BoardError
        operation = receipts[-1]["operation"]
        retained_revision = json.loads(operation["proposal_json"])["advance_request"]["expected_revision"]
        owner = WorkBoardOwner(principal_id=operation["owner_principal_id"],
            session_id=operation["owner_session_id"])
        unrelated = ProgrammeDigestReceipt(owner_identity_id="unrelated-pending-owner",
            local_date="2026-10-09", timezone="UTC", digest_json="{}")
        async with db_engine.get_session() as db:
            assert db.info.get("composition_read_guard") is not None
            db.add(unrelated)
            with pytest.raises(BoardError, match="Original clean pipeline caller required"):
                await pipelines.advance(db, owner, operation["proposal_id"], retained_revision)
            assert unrelated in db.new
            await db.rollback()
        async with db_engine.get_session() as db:
            assert await db.get(ProgrammeDigestReceipt, unrelated.id) is None
    finally:
        path = accounting_db[0] / "original-pipeline-four-writer-receipts.json"
        path.write_text(json.dumps({"writers": receipts, "physical_denials": physical_denials,
            "failures": failures}, indent=2))
        path.chmod(0o600)


@pytest.mark.asyncio
async def test_original_pipeline_unfinished_producer_denies_before_writer(
        accounting_db, real_auth, monkeypatch, record_property):
    from src.db import engine as db_engine
    from src.db.models import WorkBoardAttempt, WorkBoardInputArtifact, WorkBoardProposal, WorkBoardTask, WorkflowRunState
    from src.runtime_plugins import ownership
    from src.work_board import pipelines, review
    from src.work_board.repository import BoardError

    class ObservedUnfinishedRun(BaseException):
        pass

    original_stage = review.stage_pipeline_producer_readback
    original_reserve = pipelines._reserve_advance_consumer
    original_begin = ownership.begin_native_writer
    observed = {}
    selected_begins = []

    async def snapshot(db, operation_id, consumer_id):
        proposal = await db.get(WorkBoardProposal, operation_id, populate_existing=True)
        consumer = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == consumer_id)
            .execution_options(populate_existing=True))
        inputs = list((await db.scalars(select(WorkBoardInputArtifact)
            .order_by(WorkBoardInputArtifact.artifact_id))).all())
        return {"proposal": proposal.model_dump(mode="json"),
            "consumer": consumer.model_dump(mode="json"),
            "inputs": [row.model_dump(mode="json") for row in inputs]}

    async def observe_begin(db, **kwargs):
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_code.co_name == "_begin_pipeline_advance_writer":
                selected_begins.append(kwargs["owner"])
            frame = frame.f_back
        return await original_begin(db, **kwargs)

    async def unfinished_actual_run(db, owner, producer):
        frame = sys._getframe(1)
        if frame.f_code.co_name == "_begin_pipeline_advance_writer" and not observed:
            operation_id, consumer_id = frame.f_locals["operation_id"], frame.f_locals["consumer_id"]
            producer_id = producer.task_id
            attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == producer_id)
                .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1))
            run_id = attempt.workflow_run_id
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_id))
            assert run.status == "succeeded" and run.finished_at is not None and attempt.ended_at is not None
            observed.update(operation_id=operation_id, consumer_id=consumer_id, run_id=run_id,
                before=await snapshot(db, operation_id, consumer_id), native_run_before=run.model_dump(mode="json"))
            await db.rollback()
            # Negative tamper targets only the actual completed native Run in
            # this private DB; the original Source and producer readback remain genuine.
            async with db_engine.get_session() as mutation_db:
                await original_begin(mutation_db, owner="native_ingress")
                actual = await mutation_db.scalar(select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == run_id).execution_options(populate_existing=True))
                actual.finished_at = None
                mutation_db.add(actual)
            producer = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == producer_id)
                .execution_options(populate_existing=True))
        return await original_stage(db, owner, producer)

    async def observe_denial(*args, **kwargs):
        try:
            return await original_reserve(*args, **kwargs)
        except BoardError as exc:
            if not observed or exc.code != "pipeline_output_unverified" or str(exc) != "The actual finished producer Run is required":
                raise
            assert selected_begins == []
            async with db_engine.get_session() as db:
                observed["after"] = await snapshot(db, observed["operation_id"], observed["consumer_id"])
                run = await db.scalar(select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == observed["run_id"]).execution_options(populate_existing=True))
                assert run.status == "succeeded" and run.finished_at is None
                observed["native_run_after"] = run.model_dump(mode="json")
            assert observed["after"] == observed["before"]
            observed["denial"] = {"code": exc.code, "message": str(exc), "selected_writer_begins": selected_begins}
            raise ObservedUnfinishedRun() from exc

    monkeypatch.setattr(ownership, "begin_native_writer", observe_begin)
    monkeypatch.setattr(review, "stage_pipeline_producer_readback", unfinished_actual_run)
    monkeypatch.setattr(pipelines, "_reserve_advance_consumer", observe_denial)
    try:
        with pytest.raises(ObservedUnfinishedRun):
            await _original_journey(accounting_db, real_auth, monkeypatch, record_property)
        assert observed["denial"]["selected_writer_begins"] == []
    finally:
        path = accounting_db[0] / "original-pipeline-unfinished-run-denial.json"
        path.write_text(json.dumps(observed, indent=2))
        path.chmod(0o600)
