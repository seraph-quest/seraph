"""Focused negative proofs; seeded selection rows are never execution evidence."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select

from config.settings import settings
from src.db.models import Goal, GuardianOpportunity, WorkBoardProposal, WorkBoardTask, WorkBoardLink, WorkBoardStatus
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.work_board import pipelines
from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate, WorkBoardInputArtifactCreate
from src.work_board.pipeline_contracts import PipelinePreviewRequest, PipelineAcceptRequest, PipelineRevisionRequest, PipelineReuseRequest, PIPELINE_KIND, canonical_bytes, digest
from src.work_board.repository import WorkBoardRepository, BoardError, _begin_sqlite_immediate
from src.work_board.input_artifacts import prepare_input_artifact, RetiredInputCleanupWitness, cleanup_retired_input, _write_payload, INPUT_ARTIFACT_ROOT
from src.workspace import canonical_workspace_root_identity


def cleanup_intent():
    entry = {"slot": "evidence_dossier", "task_ref": "consumer", "task_revision": 4,
        "artifact_ref": "old-input", "typed_input_ref": f"workspace-json:{INPUT_ARTIFACT_ROOT}/old.json",
        "content_sha256": "a"*64, "size_bytes": 1, "source_revision": 2,
        "source_metadata_digest": "b"*64, "tombstone_revision": 3,
        "tombstone_metadata_digest": "c"*64, "state": "pending", "reason_code": None}
    return {"schema_version": 1, "accepted_digest": "d"*64, "accepted_request_digest": "e"*64,
        "owner_principal_id": "operator", "original_root_id": "original-root",
        "workspace_identity_digest": "f"*64, "plan_version": 2,
        "revision_request_key": "replacement", "entries": [entry]}


@pytest.mark.parametrize("correction", ["unknown", "duplicate_slot", "oversize", "bool_revision", "raw_reason", "bad_type"])
def test_retirement_json_is_closed_and_bounded(correction):
    intent = cleanup_intent()
    if correction == "unknown": intent["invented_authority"] = True
    elif correction == "duplicate_slot": intent["entries"].append(dict(intent["entries"][0]))
    elif correction == "oversize": intent["revision_request_key"] = "x"*4096
    elif correction == "bool_revision": intent["entries"][0]["source_revision"] = True
    elif correction == "raw_reason": intent["entries"][0].update(state="cleanup_required", reason_code="/private/secret.txt")
    else: intent["entries"][0]["slot"] = []
    with pytest.raises(BoardError) as failure:
        pipelines.validate_retired_cleanup(intent)
    assert failure.value.code == "pipeline_corrupt"


def test_raw_duplicate_keys_fail_before_digest_projection():
    value = {"kind": PIPELINE_KIND, "retired_input_cleanup": cleanup_intent()}
    raw = canonical_bytes(value).decode().replace('"schema_version":1', '"schema_version":1,"schema_version":1')
    row = WorkBoardProposal(proposal_json=raw, proposal_digest=digest(value), kind=PIPELINE_KIND)
    with pytest.raises(BoardError) as failure:
        pipelines.unpack(row)
    assert failure.value.code == "pipeline_corrupt"


@pytest.mark.parametrize("correction", ["none", "leaf_missing", "parent_missing", "root_changed", "symlink", "digest", "mode"])
def test_exact_private_cleanup_never_deletes_foreign_path(monkeypatch, tmp_path, correction):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    path = tmp_path / INPUT_ARTIFACT_ROOT / "old.json"
    raw = b'{"quoted":"public data"}'
    _write_payload(path, raw)
    intent = cleanup_intent()
    entry = intent["entries"][0]
    entry.update(content_sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    workspace = canonical_bytes(canonical_workspace_root_identity(tmp_path))
    witness = RetiredInputCleanupWitness("operation", "operator", "original-root", "a"*64,
        intent["accepted_request_digest"], canonical_bytes(intent), canonical_bytes(entry), workspace, str(tmp_path))
    foreign = tmp_path / "foreign.txt"
    foreign.write_bytes(b"retain foreign bytes")
    if correction == "leaf_missing": path.unlink()
    elif correction == "parent_missing": path.unlink(); path.parent.rmdir()
    elif correction == "root_changed":
        old = tmp_path.with_name(tmp_path.name+"-old")
        tmp_path.rename(old); tmp_path.mkdir()
    elif correction == "symlink": path.unlink(); path.symlink_to(foreign)
    elif correction == "digest": path.write_bytes(b"different bytes")
    elif correction == "mode": path.chmod(0o644)
    result = cleanup_retired_input(witness)
    if correction in {"none", "leaf_missing"}:
        assert result.outcome == "absent"
        assert not path.exists()
    else:
        assert result.outcome == "cleanup_required"
        assert result.reason_code in {"cleanup_target_missing", "cleanup_root_changed", "cleanup_target_unavailable", "cleanup_target_metadata_mismatch", "cleanup_digest_mismatch"}
        if correction != "root_changed": assert foreign.read_bytes() == b"retain foreign bytes"
    if correction == "symlink": assert path.is_symlink()


async def unaccepted_operation(async_db, monkeypatch, tmp_path):
    from tests.test_browser_task_runtime import _input
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "browser_site_allowlist", "fixture.example")
    owner = WorkBoardOwner(principal_id="operator:test-bypass", session_id="test-auth-bypass")
    repository = WorkBoardRepository()
    async with async_db() as db:
        db.add(Goal(id="goal-957-neg", title="Finite negative proof", status="active", revision=1,
            owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
            admission_budget_json=serialize_admission_budget(GoalAdmissionBudget(reviewed_grant=True,
                grant_id="negative-proof", max_outstanding_jobs=1, max_attempts=2, max_runtime_seconds=300))))
        await db.commit()
        artifact = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(schema_version=1,
            capability_id="browser.public-task.v1", goal_id="goal-957-neg", goal_revision=1,
            input=_input(), idempotency_key="negative-input"))
        created = await repository.create_task(db, owner, WorkBoardTaskCreate(title="Public source",
            goal_id="goal-957-neg", goal_revision=1, capability_id="browser.public-task.v1",
            input_artifact_id=artifact.artifact_id, status=WorkBoardStatus.todo, idempotency_key="negative-source"))
        preview = await pipelines.preview(db, owner, created.task.task_id, PipelinePreviewRequest(
            expected_revision=created.task.task_revision, source_input_artifact_id=artifact.artifact_id,
            idempotency_key="negative-operation"))
    return owner, preview


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("failure_after_child", [False, True])
@pytest.mark.asyncio
async def test_acceptance_writer_has_no_physical_callbacks_and_rolls_back(async_db, monkeypatch, tmp_path, failure_after_child):
    from src.work_board import repository, input_artifacts, dispatcher
    from src.workflows.job_runtime import durable_job_repository
    owner, preview = await unaccepted_operation(async_db, monkeypatch, tmp_path)
    request = PipelineAcceptRequest(expected_revision=preview["revision"],
        expected_parent_revision=preview["parent_revision"], expected_digest=preview["digest"])
    async with async_db() as db:
        context = await pipelines.stage_accept(db, owner, preview["operation_id"], request, source_witness=None)
        await _begin_sqlite_immediate(db)
        def forbidden(*args, **kwargs): raise AssertionError("physical callback under writer")
        async def async_forbidden(*args, **kwargs): forbidden()
        with monkeypatch.context() as locked:
            locked.setattr(pipelines, "root_binding", forbidden)
            locked.setattr(pipelines, "canonical_workspace_root", forbidden)
            locked.setattr(input_artifacts, "_payload_path", forbidden)
            locked.setattr(input_artifacts, "_safe_file_bytes", forbidden)
            locked.setattr(repository.WorkBoardRepository, "_safe_text", async_forbidden)
            locked.setattr(dispatcher, "_parse_typed_input", forbidden)
            locked.setattr(durable_job_repository, "get_job", async_forbidden)
            locked.setattr(pipelines, "_begin_sqlite_immediate", async_forbidden)
            if failure_after_child:
                async def fail_link(*args, **kwargs):
                    raise BoardError("pipeline_materialization_conflict", "injected after child")
                locked.setattr(repository.WorkBoardRepository, "_add_link_locked", fail_link)
                with pytest.raises(BoardError):
                    await pipelines._accept_locked(db, owner, preview["operation_id"], request, staged_context=context)
            else:
                projection = await pipelines._accept_locked(db, owner, preview["operation_id"], request, staged_context=context)
                assert projection["status"] == "accepted"
                assert len(projection["steps"]) == 3
        if failure_after_child: await db.rollback()
    async with async_db() as db:
        row = await db.get(WorkBoardProposal, preview["operation_id"])
        tasks = list((await db.scalars(select(WorkBoardTask))).all())
        assert row.status == ("proposed" if failure_after_child else "accepted")
        assert len(tasks) == (1 if failure_after_child else 3)
        assert len(list((await db.scalars(select(WorkBoardLink))).all())) == (0 if failure_after_child else 2)


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_linked_revision_and_reuse_reject_before_input_staging(async_db, monkeypatch, tmp_path):
    owner, preview = await unaccepted_operation(async_db, monkeypatch, tmp_path)
    async with async_db() as db:
        row = await db.get(WorkBoardProposal, preview["operation_id"])
        row.opportunity_id = "linked-opportunity"
    async with async_db() as db:
        with pytest.raises(BoardError) as failure:
            await pipelines.stage_revision(db, owner, preview["operation_id"], PipelineRevisionRequest(
                expected_revision=preview["revision"], source_input_artifact_id="missing-input",
                idempotency_key="source-substitution"))
        assert failure.value.code == "pipeline_review_required"
        with pytest.raises(BoardError) as failure:
            await pipelines.reuse_preview(db, owner, preview["operation_id"], PipelineReuseRequest(
                expected_revision=preview["revision"], expected_parent_revision=preview["parent_revision"], idempotency_key="reuse-substitution"))
        assert failure.value.code == "pipeline_review_required"


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_concurrent_staged_acceptance_loser_creates_no_duplicate_children(async_db, monkeypatch, tmp_path):
    owner, preview = await unaccepted_operation(async_db, monkeypatch, tmp_path)
    request = PipelineAcceptRequest(expected_revision=preview["revision"],
        expected_parent_revision=preview["parent_revision"], expected_digest=preview["digest"])
    async with async_db() as first, async_db() as second:
        first_context = await pipelines.stage_accept(first, owner, preview["operation_id"], request, source_witness=None)
        second_context = await pipelines.stage_accept(second, owner, preview["operation_id"], request, source_witness=None)
        await _begin_sqlite_immediate(first)
        await pipelines._accept_locked(first, owner, preview["operation_id"], request, staged_context=first_context)
        await first.commit()
        await _begin_sqlite_immediate(second)
        with pytest.raises(BoardError) as failure:
            await pipelines._accept_locked(second, owner, preview["operation_id"], request, staged_context=second_context)
        assert failure.value.code == "pipeline_revision_conflict"
        await second.rollback()
    async with async_db() as db:
        assert len(list((await db.scalars(select(WorkBoardTask))).all())) == 3
        assert len(list((await db.scalars(select(WorkBoardLink))).all())) == 2


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_recovery_cursor_reaches_after_twenty_blocked_candidates_and_wraps(async_db, monkeypatch):
    from src.work_board.dispatcher import WorkBoardDispatcher
    stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with async_db() as db:
        selection_engine = db.bind.sync_engine
        links = []
        for index in range(25):
            op, producer, child, opportunity = (f"{prefix}-{index:02}" for prefix in ("operation", "producer", "consumer", "opportunity"))
            db.add(WorkBoardProposal(proposal_id=op, kind=PIPELINE_KIND, status="accepted", opportunity_id=opportunity,
                owner_principal_id="operator", owner_session_id="root", parent_task_id=producer,
                idempotency_key=op, created_at=stamp, expires_at=stamp+timedelta(minutes=5)))
            db.add(GuardianOpportunity(id=opportunity, proposal_id=op, status="planned", owner_principal_id="operator",
                original_root_id="root", goal_id="goal", goal_revision=1, policy_revision=1, watch_id="watch", watch_revision=1,
                source_packet_id="packet", source_digest="a"*64, source_token_json="{}", dedupe_key=op,
                expires_at=stamp+timedelta(days=1), assessment_deadline_at=stamp+timedelta(minutes=5)))
            db.add(WorkBoardTask(task_id=producer, owner_principal_id="operator", owner_session_id="root", goal_id="goal",
                goal_revision=1, title="Selection fixture only", capability_id="browser.public-task.v1", status=WorkBoardStatus.done,
                pipeline_operation_id=op, pipeline_slot="public_source", idempotency_key=producer))
            db.add(WorkBoardTask(task_id=child, owner_principal_id="operator", owner_session_id="root", goal_id="goal", goal_revision=1,
                title="Never executed", capability_id="work.evidence-dossier.v1", status=WorkBoardStatus.triage,
                pipeline_operation_id=op, pipeline_slot="evidence_dossier", idempotency_key=child))
            links.append(WorkBoardLink(parent_task_id=producer, child_task_id=child, owner_principal_id="operator", owner_session_id="root"))
        # Duplicate eligible links must still consume only one operation slot.
        db.add(WorkBoardTask(task_id="producer-00-duplicate", owner_principal_id="operator", owner_session_id="root",
            goal_id="goal", goal_revision=1, title="Selection duplicate only", capability_id="browser.public-task.v1",
            status=WorkBoardStatus.done, pipeline_operation_id="operation-00", pipeline_slot="public_source",
            idempotency_key="selection-duplicate"))
        links.append(WorkBoardLink(parent_task_id="producer-00-duplicate", child_task_id="consumer-00",
            owner_principal_id="operator", owner_session_id="root"))
        await db.flush()  # FK parents precede their links; no ORM relationship.
        db.add_all(links)
    dispatcher = WorkBoardDispatcher(session_provider=async_db)
    seen = []
    async def blocked_candidate(task):
        seen.append(task.pipeline_operation_id)
    monkeypatch.setattr(dispatcher, "_advance_linked_pipeline", blocked_candidate)
    selections = []
    def count_selection(connection, cursor, statement, parameters, context, executemany):
        if "GROUP BY work_board_proposals.created_at" in statement:
            selections.append(statement)
    event.listen(selection_engine, "before_cursor_execute", count_selection)
    try:
        await dispatcher._recover_linked_pipelines()
        assert seen == [f"operation-{index:02}" for index in range(20)]
        assert len(selections) == 1
        selections.clear()
        await dispatcher._recover_linked_pipelines()
        assert seen[20:] == [f"operation-{index:02}" for index in range(20, 25)]
        assert len(selections) == 1
        selections.clear()
        await dispatcher._recover_linked_pipelines()
        assert seen[25:] == [f"operation-{index:02}" for index in range(20)]
        assert len(selections) == 2
    finally:
        event.remove(selection_engine, "before_cursor_execute", count_selection)


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_staged_input_rejects_forged_input_bytes_without_physical_reader(async_db, monkeypatch, tmp_path):
    from dataclasses import replace
    from src.work_board import input_artifacts
    from src.db.models import WorkBoardInputArtifact
    owner, preview = await unaccepted_operation(async_db, monkeypatch, tmp_path)
    async with async_db() as db:
        original = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == preview["steps"][0]["task_id"]))
        artifact = await db.get(WorkBoardInputArtifact, original.input_artifact_id)
        # Prepare a fresh unbound artifact; an already bound source is not adoptable.
        from tests.test_browser_task_runtime import _input
        prepared = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(schema_version=1,
            capability_id=artifact.capability_id, goal_id=artifact.goal_id, goal_revision=artifact.goal_revision,
            input=_input(), idempotency_key="forged-witness-input"))
        request = WorkBoardTaskCreate(title="Consumer", capability_id=artifact.capability_id,
            goal_id=artifact.goal_id, goal_revision=artifact.goal_revision, input_artifact_id=prepared.artifact_id,
            status=WorkBoardStatus.todo, idempotency_key="forged-witness-task")
        witness = await input_artifacts.stage_input_artifact(db, owner, artifact_id=prepared.artifact_id,
            capability_id=request.capability_id, goal_id=request.goal_id, goal_revision=request.goal_revision)
        await _begin_sqlite_immediate(db)
        def forbidden(*args, **kwargs): raise AssertionError("physical input read under writer")
        monkeypatch.setattr(input_artifacts, "_safe_file_bytes", forbidden)
        with pytest.raises(BoardError) as failure:
            await input_artifacts.recheck_staged_input(db, owner, request, witness=replace(witness, input_bytes=b"{}"))
        assert failure.value.code == "pipeline_input_changed"
        resolved = await input_artifacts.recheck_staged_input(db, owner, request, witness=witness)
        assert canonical_bytes(resolved.input) == witness.input_bytes


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_staged_source_card_is_only_unbound_nonexecutable_plan_triage(async_db, monkeypatch, tmp_path):
    from src.work_board.repository import stage_safe_task_text
    owner, _preview = await unaccepted_operation(async_db, monkeypatch, tmp_path)
    repository = WorkBoardRepository()
    request = WorkBoardTaskCreate(goal_id="goal-957-neg", goal_revision=1,
        title="Generating advisory plan", status=WorkBoardStatus.triage, capability_id="browser.public-task.v1",
        idempotency_scope="opportunity-plan", idempotency_key="advisory-card")
    async with async_db() as db:
        safe_text = await stage_safe_task_text(db, owner, request)
        await _begin_sqlite_immediate(db)
        card = (await repository._create_task_locked(db, owner, request, staged_text=safe_text)).task
        assert card.status is WorkBoardStatus.triage
        assert card.executor_id is None and card.input_artifact_id is None and card.capability_id == "browser.public-task.v1"
    for correction in ({"status": WorkBoardStatus.todo}, {"capability_id": None}, {"capability_id": "work.arbitrary-code.v1"}):
        changed = request.model_copy(update={**correction, "idempotency_key": "forged-card"})
        async with async_db() as db:
            safe_text = await stage_safe_task_text(db, owner, changed)
            await _begin_sqlite_immediate(db)
            with pytest.raises(BoardError) as failure:
                await repository._create_task_locked(db, owner, changed, staged_text=safe_text)
            assert failure.value.code == ("browser_input_artifact_required" if correction.get("status") is WorkBoardStatus.todo else "pipeline_plan_changed")
            await db.rollback()


@pytest.mark.asyncio
async def test_failed_recovery_does_not_starve_ordinary_dispatch(monkeypatch):
    from src.work_board import dispatcher as module
    from contextlib import asynccontextmanager
    called = []
    @asynccontextmanager
    async def session(): yield object()
    dispatcher = module.WorkBoardDispatcher(session_provider=session)
    async def zero(*args, **kwargs): return 0
    async def empty(*args, **kwargs): return []
    async def candidates(*args, **kwargs): called.append("ordinary"); return []
    async def blocked(): raise BoardError("pipeline_output_unverified", "blocked recovery")
    monkeypatch.setattr(module, "expire_inbox_items", zero)
    monkeypatch.setattr(module, "repair_inbox_dispositions", zero)
    monkeypatch.setattr(dispatcher, "_expire_review_windows", zero)
    monkeypatch.setattr(dispatcher, "reconcile_pending_attempts", empty)
    monkeypatch.setattr(dispatcher, "reconcile_linked_attempts", empty)
    monkeypatch.setattr(dispatcher, "_recover_linked_pipelines", blocked)
    monkeypatch.setattr(dispatcher.repository, "list_dispatch_candidates", candidates)
    receipt = await dispatcher.run_pass()
    assert called == ["ordinary"] and receipt["status"] == "completed"
