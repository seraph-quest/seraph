"""Observe original one-time consumption in the genuine accepted Source journey."""
import json
import hashlib
import sqlite3
from pathlib import Path
import traceback

import pytest
from sqlalchemy import select

from src.db.models import WorkBoardInputArtifact
from src.work_board.repository import BoardError
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_native_memory_report_source_vertical import test_actual_accepted_report_source_with_mocked_browser_edge as _original_journey


@pytest.mark.asyncio
async def test_original_consume_same_bound_input_once(accounting_db, real_auth, monkeypatch, record_property):
    from src.work_board import input_artifacts
    original = input_artifacts.consume_input_artifact
    evidence = []
    failures = []
    async def observe(db, owner, **kwargs):
        row = await db.scalar(select(WorkBoardInputArtifact).where(
            WorkBoardInputArtifact.artifact_id == kwargs["artifact_id"]))
        before = row.model_dump(mode="json")
        assert row.state == "bound" and row.bound_task_revision == kwargs["task_revision"]
        try:
            await original(db, owner, **kwargs)
            await db.refresh(row)
            after = row.model_dump(mode="json")
            assert row.state == "consumed" and row.revision == before["revision"] + 1
            assert row.metadata_digest == input_artifacts._metadata_digest(row)
            assert row.bound_task_revision == kwargs["task_revision"]
            assert row.consumed_at is not None
            with pytest.raises(BoardError) as repeated:
                await original(db, owner, **kwargs)
            assert repeated.value.code == "input_artifact_consume_conflict"
            await db.refresh(row)
            assert row.model_dump(mode="json") == after
            evidence.append({"artifact_id": row.artifact_id, "before": before, "after": after,
                "bound_task_revision": kwargs["task_revision"],
                "live_task_revision": kwargs["expected_task_revision"], "repeat_conflict": repeated.value.code})
        except BaseException as exc:
            failures.append({"type": type(exc).__name__, "code": getattr(exc, "code", None),
                "message": str(exc), "traceback": traceback.format_exc()})
            raise
    monkeypatch.setattr(input_artifacts, "consume_input_artifact", observe)
    try:
        await _original_journey(accounting_db, real_auth, monkeypatch, record_property)
        assert evidence, "The real browser must consume its actual bound Input"
    finally:
        path = accounting_db[0] / "original-input-consume.json"
        path.write_text(json.dumps({"consumption": evidence, "failures": failures}, indent=2))
        path.chmod(0o600)


def _raw_rows(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        tables = db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name").fetchall()
        return {name: tuple(sorted(db.execute('SELECT * FROM "' + name.replace('"', '""') + '"').fetchall(), key=repr))
                for (name,) in tables}


@pytest.mark.asyncio
async def test_bounded_consume_actual_authority_negatives(accounting_db, real_auth, monkeypatch, record_property):
    """Consume scope only: retain and name the later whole-pipeline boundary."""
    from src.db.models import Goal, GuardianSourceWatch, OperatorSession, WorkBoardTask, WorkflowRunState
    from src.guardian.opportunity_contracts import OpportunityError
    from src.runtime_plugins.ownership import begin_native_writer
    from src.work_board import input_artifacts, pipelines, pipeline_cpu
    from src.work_board.repository import WorkBoardRepository
    root, _, factory = accounting_db
    original = input_artifacts.consume_input_artifact
    negatives, positive, later = [], [], []
    active_db, physical_reads, freezes = [None], [], []
    def physical_observer(reader, name):
        def read(*args, **kwargs):
            current = active_db[0]
            assert current is None or not (current.in_transaction() and current.info.get("native_writer_started")), name
            physical_reads.append(name)
            return reader(*args, **kwargs)
        return read
    monkeypatch.setattr(Path, "read_bytes", physical_observer(Path.read_bytes, "Path.read_bytes"))
    monkeypatch.setattr(input_artifacts, "_safe_file_bytes", physical_observer(input_artifacts._safe_file_bytes, "_safe_file_bytes"))
    monkeypatch.setattr(pipeline_cpu, "read_output", physical_observer(pipeline_cpu.read_output, "read_output"))

    async def observe(db, owner, **kwargs):
        if negatives:
            return await original(db, owner, **kwargs)
        active_db[0] = db
        artifact = await db.get(WorkBoardInputArtifact, kwargs["artifact_id"])
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == kwargs["task_id"]))
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == kwargs["expected_workflow_run_id"]))
        before_input = artifact.model_dump(mode="json")
        input_path = root / artifact.typed_input_ref.removeprefix("workspace-json:")
        input_bytes = input_path.read_bytes()
        outputs = json.loads(run.artifact_receipts_json)
        output_path = root / next(item["file_path"] for item in outputs if item.get("exists") is True)
        watch = await db.scalar(select(GuardianSourceWatch).where(GuardianSourceWatch.goal_id == task.goal_id))
        identifiers = {"goal": task.goal_id, "watch": watch.id, "run": run.id}
        await db.rollback()

        async def deny(name, changes=None):
            before = _raw_rows(root / "seraph.db")
            try:
                await original(db, owner, **{**kwargs, **(changes or {})})
            except (BoardError, OpportunityError, ValueError, OSError) as exc:
                code = getattr(exc, "code", type(exc).__name__)
            else:
                raise AssertionError(f"{name} unexpectedly consumed the actual Input")
            await db.rollback()
            after = _raw_rows(root / "seraph.db")
            assert before == after, name
            negatives.append({"case": name, "code": code, "raw_rows_unchanged": True,
                "raw_rows_sha256": hashlib.sha256(repr(before).encode()).hexdigest()})

        async def mutate(cls, identifier, field, value):
            async with factory.accounting_sessions() as mutation:
                await begin_native_writer(mutation, owner="native_ingress")
                row = await mutation.get(cls, identifier)
                prior = getattr(row, field)
                setattr(row, field, value)
                await mutation.flush()
                return prior

        await deny("missing_expected_task_revision", {"expected_task_revision": None})
        await deny("wrong_attempt_fence", {"expected_fencing_token": kwargs["expected_fencing_token"] + 1})
        from datetime import datetime, timezone
        prior = await mutate(OperatorSession, owner.session_id, "revoked_at", datetime.now(timezone.utc))
        try:
            await deny("actual_revoked_root")
        finally:
            await mutate(OperatorSession, owner.session_id, "revoked_at", prior)
        async with factory.accounting_sessions() as read:
            goal = await read.get(Goal, identifiers["goal"])
            revision = goal.revision
        prior = await mutate(Goal, identifiers["goal"], "revision", revision + 1)
        try:
            await deny("actual_stale_goal")
        finally:
            await mutate(Goal, identifiers["goal"], "revision", prior)
        prior = await mutate(GuardianSourceWatch, identifiers["watch"], "state", "paused")
        try:
            await deny("actual_inactive_source")
        finally:
            await mutate(GuardianSourceWatch, identifiers["watch"], "state", prior)
        input_path.write_bytes(b"tampered actual bound private Input")
        try:
            await deny("actual_private_input_tamper")
        finally:
            input_path.write_bytes(input_bytes)
        prior = await mutate(WorkflowRunState, identifiers["run"], "status", "failed")
        try:
            await deny("actual_unsuccessful_native_run")
        finally:
            await mutate(WorkflowRunState, identifiers["run"], "status", prior)
        hidden_output = output_path.with_name(output_path.name + ".consume-test-missing")
        output_path.rename(hidden_output)
        try:
            await deny("actual_missing_browser_output")
        finally:
            hidden_output.rename(output_path)
        actual_guard, actual_goal_check = pipelines.task_guard, WorkBoardRepository.validate_task_goal
        actual_freeze, inside_guard = pipelines.freeze_unfinished, [False]
        async def original_guard_after_goal_race(db, actual_task, **binding):
            inside_guard[0] = True
            try:
                return await actual_guard(db, actual_task, **binding)
            finally:
                inside_guard[0] = False
        async def original_goal_check_after_race(self, db, actual_owner, actual_task):
            if inside_guard[0]:
                goal = await db.get(Goal, actual_task.goal_id, populate_existing=True)
                goal.revision += 1  # Actual writer row race, not an authority DTO.
                await db.flush()
            return await actual_goal_check(self, db, actual_owner, actual_task)
        async def observe_original_freeze(*args, **kwargs):
            result = await actual_freeze(*args, **kwargs)
            freezes.append("original_freeze_unfinished_executed")
            return result
        with monkeypatch.context() as race:
            race.setattr(pipelines, "task_guard", original_guard_after_goal_race)
            race.setattr(WorkBoardRepository, "validate_task_goal", original_goal_check_after_race)
            race.setattr(pipelines, "freeze_unfinished", observe_original_freeze)
            await deny("original_pipeline_freeze_rolled_back")
        assert freezes == ["original_freeze_unfinished_executed"]

        await original(db, owner, **kwargs)
        artifact = await db.get(WorkBoardInputArtifact, kwargs["artifact_id"], populate_existing=True)
        after_input = artifact.model_dump(mode="json")
        assert artifact.state == "consumed" and artifact.revision == before_input["revision"] + 1
        assert artifact.metadata_digest == input_artifacts._metadata_digest(artifact)
        assert artifact.bound_task_revision == kwargs["task_revision"] and artifact.consumed_at is not None
        with pytest.raises(BoardError) as replay:
            await original(db, owner, **kwargs)
        assert replay.value.code == "input_artifact_consume_conflict"
        await db.refresh(artifact)
        assert artifact.model_dump(mode="json") == after_input
        positive.append({"artifact_id": artifact.artifact_id, "task_id": kwargs["task_id"],
            "before": before_input, "after": after_input, "repeat_conflict": replay.value.code})
        active_db[0] = None

    monkeypatch.setattr(input_artifacts, "consume_input_artifact", observe)
    try:
        try:
            await _original_journey(accounting_db, real_auth, monkeypatch, record_property)
        except AssertionError as exc:
            terminal = exc.__traceback__
            while terminal.tb_next is not None:
                terminal = terminal.tb_next
            # Only the already observed final pipeline triage assertion is
            # outside this bounded consume test; every other error is fatal.
            if (terminal.tb_frame.f_code.co_name != "_actual_plan_journey"
                or terminal.tb_lineno != 256
                or Path(terminal.tb_frame.f_code.co_filename).resolve() != Path(__file__).with_name("test_guardian_opportunity_plan_vertical.py").resolve()
                or "'tasks':" not in str(exc) or "'passes':" not in str(exc)):
                raise
            # Pytest rewrites the assertion's dict message into a string.
            # Read the actual committed Tasks instead of parsing that string.
            async with factory.accounting_sessions() as read:
                actual_tasks = list((await read.scalars(select(WorkBoardTask))).all())
                assert sorted(task.status.value for task in actual_tasks) == ["done", "triage", "triage"]
                assert positive and next(task for task in actual_tasks if task.task_id == positive[0]["task_id"]).status.value == "done"
            later.append({"boundary": "original_final_pipeline_triage_assertion", "whole_journey_pass": False})
        assert len(negatives) == 9 and len(positive) == 1
        async with factory.accounting_sessions() as committed:
            artifact = await committed.get(WorkBoardInputArtifact, positive[0]["artifact_id"])
            task = await committed.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == positive[0]["task_id"]))
            assert artifact.model_dump(mode="json") == positive[0]["after"]
            assert artifact.metadata_digest == input_artifacts._metadata_digest(artifact)
            assert task.status.value == "done"
            positive[0]["canonical_commit_readback"] = True
    finally:
        path = root / "bounded-original-consume-negatives.json"
        path.write_text(json.dumps({"negative": negatives, "positive": positive, "later_boundary": later,
            "physical_reads_outside_writer": physical_reads, "original_freezes": freezes}, indent=2))
        path.chmod(0o600)
