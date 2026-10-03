"""Actual file-SQLite inventory/feedback mechanics, not learning quality proof.

These cases create authentic Root sessions and canonical metadata fixtures but
never fabricate successful native readback or accepted memory. Full native
execution is tested separately before milestone acceptance.
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from config.settings import settings
from src.auth.service import create_session
from src.browser.task_runner import BrowserTaskInput, _browser_input_digests
from src.db.models import (
    Goal, GuardianRoutine, GuardianRoutineVersion, OperatorSession,
    WorkBoardEvent, WorkBoardInputArtifact, WorkBoardStatus, WorkBoardTask,
)
from src.memory.procedure_recommendations import (
    FEEDBACK_KIND, ProcedureFeedbackRequest, assert_membership_unchanged,
    canonical, canonical_procedure_membership, record_procedure_feedback, resolve_scope,
)
from src.work_board.repository import BoardError, _begin_sqlite_immediate
from src.workflows.procedure_contracts import build_procedure_plan, plan_digest
from tests.test_procedures_v2 import _copied_browser_input

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("async_db", ["file"], indirect=True)]


async def _setup(async_db, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-procedure-test")
    _, operator = await create_session()
    model = BrowserTaskInput.model_validate(_copied_browser_input())
    envelope, body, consent = _browser_input_digests(model)
    plan = build_procedure_plan("public-browser-check", step_inputs={"public_browser_check": {
        "typed_input_ref": "source/browser", "typed_input_digest": envelope}})
    owner = operator.principal.principal_id
    async with async_db() as db:
        db.add(Goal(id="goal", title="Explicit manual procedure", status="active", revision=1,
            owner_principal_id=owner, owner_session_id=operator.session_id))
        db.add(GuardianRoutine(id="routine", name="Existing reviewed fixture version", state="active",
            owner_principal_id=owner, owner_session_id=operator.session_id, revision=1, current_version=1))
        db.add(GuardianRoutineVersion(id="version", routine_id="routine", version=1,
            installed_package_digest="a" * 64, source_provenance_json=canonical({"schema_version": 2,
                "plan": plan.model_dump(mode="json"), "plan_digest": plan_digest(plan),
                "immutable_step_inputs": {"public_browser_check": {
                    "browser_input": model.model_dump(mode="json", exclude_none=True),
                    "browser_input_digest": body, "input_envelope_digest": envelope,
                    "action_consent_digest": consent}}})))
    async with async_db() as db:
        scope = await resolve_scope(db, operator, routine_id="routine", version=1,
            routine_revision=1, goal_id="goal", goal_revision=1)
    return operator, scope


async def _task(db, scope, name, *, status=WorkBoardStatus.blocked, scope_override=None):
    task = WorkBoardTask(task_id=name, owner_principal_id=scope.owner_principal_id,
        owner_session_id=scope.owner_session_id, goal_id=scope.goal_id, goal_revision=scope.goal_revision,
        capability_id="guardian-routine.v2", input_artifact_id=f"input-{name}",
        typed_input_digest="b" * 64, task_revision=1, status=status,
        idempotency_scope=scope_override or scope.invocation_scope, idempotency_key=name)
    db.add(task)
    db.add(WorkBoardInputArtifact(artifact_id=f"input-{name}", owner_principal_id=scope.owner_principal_id,
        owner_session_id=scope.owner_session_id, goal_id=scope.goal_id, goal_revision=scope.goal_revision,
        capability_id="guardian-routine.v2", capability_version="guardian-routine.v2", idempotency_key=name,
        payload_sha256="b" * 64, metadata_digest="c" * 64, typed_input_ref=f"input/{name}",
        state="bound", bound_task_id=name, bound_task_revision=1,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)))
    await db.flush()
    return task


def _request(**changes):
    return ProcedureFeedbackRequest(version=1, expected_routine_revision=1,
        goal_id="goal", expected_goal_revision=1, expected_task_revision=1,
        label="helpful", mutation_uuid=str(uuid4()), **changes)


async def test_complete_membership_detects_new_unreviewed_failed_invocation(async_db, monkeypatch):
    _, scope = await _setup(async_db, monkeypatch)
    async with async_db() as db:
        await _task(db, scope, "original")
    async with async_db() as db:
        staged = await canonical_procedure_membership(db, scope)
        assert staged["task_ids"] == ["original"]
        assert staged["members"][0]["attempt"] is None
    async with async_db() as db:
        await _task(db, scope, "new-no-feedback", status=WorkBoardStatus.todo)
    async with async_db() as db:
        await _begin_sqlite_immediate(db)
        with pytest.raises(BoardError, match="invocation outcomes or feedback changed"):
            await assert_membership_unchanged(db, scope, staged)
        actual = await canonical_procedure_membership(db, scope)
        assert actual["task_ids"] == ["new-no-feedback", "original"]
        assert actual["feedback_count"] == 0


async def test_failed_members_count_before_cap_and_scheduled_scope_is_excluded(async_db, monkeypatch):
    _, scope = await _setup(async_db, monkeypatch)
    async with async_db() as db:
        for number in range(20):
            await _task(db, scope, f"failed-{number:02}")
        await _task(db, scope, "scheduled", scope_override="guardian-routine-v2-schedule")
    async with async_db() as db:
        token = await canonical_procedure_membership(db, scope)
        assert token["task_count"] == 20
        assert "scheduled" not in token["task_ids"]
        assert all(member["task"]["status"] == "blocked" for member in token["members"])
    async with async_db() as db:
        await _task(db, scope, "twenty-first")
    async with async_db() as db:
        with pytest.raises(BoardError, match="twenty"):
            await canonical_procedure_membership(db, scope)


async def test_feedback_correction_is_append_only_exact_and_idempotent(async_db, monkeypatch):
    operator, scope = await _setup(async_db, monkeypatch)
    async with async_db() as db:
        await _task(db, scope, "manual")
    initial = _request()
    async with async_db() as db:
        first = await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=initial)
    corrected = initial.model_copy(update={"label": "harmful", "mutation_uuid": str(uuid4()),
        "supersedes_event_id": first["event_id"], "reason": "Outcome was not useful"})
    async with async_db() as db:
        second = await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=corrected)
    async with async_db() as db:
        replay = await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=corrected)
        assert replay == {**second, "idempotent_replay": True}
        token = await canonical_procedure_membership(db, scope)
        assert token["feedback_count"] == 2
        assert token["members"][0]["feedback_tip"]["label"] == "harmful"
        events = list((await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind == FEEDBACK_KIND))).scalars())
        assert len(events) == 2
        assert all(event.task_id == "manual" for event in events)
    async with async_db() as db:
        with pytest.raises(BoardError, match="different exact request"):
            await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual",
                request=corrected.model_copy(update={"label": "helpful"}))
    async with async_db() as db:
        with pytest.raises(BoardError, match="current feedback"):
            await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual",
                request=corrected.model_copy(update={"mutation_uuid": str(uuid4())}))


async def test_feedback_insert_invalidates_staged_full_membership(async_db, monkeypatch):
    operator, scope = await _setup(async_db, monkeypatch)
    async with async_db() as db:
        await _task(db, scope, "manual")
    async with async_db() as db:
        staged = await canonical_procedure_membership(db, scope)
    async with async_db() as db:
        await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=_request())
    async with async_db() as db:
        with pytest.raises(BoardError, match="invocation outcomes or feedback changed"):
            await assert_membership_unchanged(db, scope, staged)


async def test_revoked_root_cannot_append_even_exact_prior_feedback(async_db, monkeypatch):
    operator, scope = await _setup(async_db, monkeypatch)
    async with async_db() as db:
        await _task(db, scope, "manual")
    request = _request()
    async with async_db() as db:
        await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=request)
    async with async_db() as db:
        row = await db.get(OperatorSession, operator.session_id)
        row.revoked_at = datetime.now(timezone.utc)
        db.add(row)
    async with async_db() as db:
        with pytest.raises(BoardError, match="Root"):
            await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=request)
        assert len(list((await db.execute(select(WorkBoardEvent))).scalars())) == 1


async def test_missing_canonical_input_is_blocking_member_not_filtered(async_db, monkeypatch):
    _, scope = await _setup(async_db, monkeypatch)
    async with async_db() as db:
        task = await _task(db, scope, "manual")
        task.input_artifact_id = None
        db.add(task)
    async with async_db() as db:
        with pytest.raises(BoardError, match="missing or changed"):
            await canonical_procedure_membership(db, scope)


async def test_pure_writer_inventory_and_feedback_do_not_open_files(async_db, monkeypatch):
    operator, scope = await _setup(async_db, monkeypatch)
    async with async_db() as db:
        await _task(db, scope, "manual")
    async with async_db() as db:
        def forbidden(*args, **kwargs):
            raise AssertionError("filesystem access inside canonical writer")
        with monkeypatch.context() as scoped:
            scoped.setattr("builtins.open", forbidden)
            result = await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=_request())
            assert result["idempotent_replay"] is False
            assert (await canonical_procedure_membership(db, scope))["feedback_count"] == 1
