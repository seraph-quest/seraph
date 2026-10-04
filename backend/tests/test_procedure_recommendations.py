"""Actual file-SQLite inventory/feedback mechanics, not learning quality proof.

These cases create authentic Root sessions and canonical metadata fixtures but
never fabricate successful native readback or accepted memory. Full native
execution is tested separately before milestone acceptance.
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from config.settings import settings
from src.auth.service import create_session
from src.browser.task_runner import BrowserTaskInput, _browser_input_digests
from src.db.models import (
    Goal, GuardianRoutine, GuardianRoutineVersion, OperatorSession,
    WorkBoardEvent, WorkBoardInputArtifact, WorkBoardStatus, WorkBoardTask, WorkBoardAttempt,
)
from src.memory.procedure_recommendations import (
    FEEDBACK_KIND, ProcedureFeedbackRequest, assert_membership_unchanged,
    canonical, canonical_procedure_membership, record_procedure_feedback, resolve_scope,
    digest, bounded_json, read_private_proof, MAX_FILE_BYTES, MAX_METADATA_BYTES,
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
                "source_refs": [{"task_id": "missing-metadata-source"}],
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


async def _ended_task(db, scope, name):
    task = await _task(db, scope, name)
    # Terminal failure metadata only: never a verified native receipt.
    db.add(WorkBoardAttempt(attempt_id=f"attempt-{name}", task_id=name, fencing_token=1,
        task_revision_at_claim=1, ended_at=datetime.now(timezone.utc), outcome="capability"))
    await db.flush()
    return task


def _request(**changes):
    return ProcedureFeedbackRequest(version=1, expected_routine_revision=1,
        goal_id="goal", expected_goal_revision=1, expected_task_revision=1, expected_attempt_id="attempt-manual", expected_attempt_fence=1,
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
        await _ended_task(db, scope, "manual")
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
        await _ended_task(db, scope, "manual")
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
        await _ended_task(db, scope, "manual")
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
        task = await _ended_task(db, scope, "manual")
        task.input_artifact_id = None
        db.add(task)
    async with async_db() as db:
        with pytest.raises(BoardError, match="missing or changed"):
            await canonical_procedure_membership(db, scope)


async def test_pure_writer_inventory_and_feedback_do_not_open_files(async_db, monkeypatch):
    operator, scope = await _setup(async_db, monkeypatch)
    async with async_db() as db:
        await _ended_task(db, scope, "manual")
    async with async_db() as db:
        def forbidden(*args, **kwargs):
            raise AssertionError("filesystem access inside canonical writer")
        with monkeypatch.context() as scoped:
            scoped.setattr("builtins.open", forbidden)
            result = await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=_request())
            assert result["idempotent_replay"] is False
            assert (await canonical_procedure_membership(db, scope))["feedback_count"] == 1


async def test_full_feedback_history_preserves_exact_replay_and_rejects_overflow(async_db, monkeypatch):
    operator, scope = await _setup(async_db, monkeypatch)
    original = _request()
    async with async_db() as db:
        await _ended_task(db, scope, "manual")
    async with async_db() as db:
        result = await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=original)
    # Near-bound canonical metadata fixtures inherit the real first feedback
    # shape. They never assert a successful native outcome or learned memory.
    async with async_db() as db:
        first = await db.get(WorkBoardEvent, result["event_id"])
        metadata = bounded_json(first.metadata_json)
        tip = first.event_id
        for number in range(99):
            row = WorkBoardEvent(task_id=first.task_id, owner_principal_id=first.owner_principal_id,
                owner_session_id=first.owner_session_id, actor_principal_id=first.actor_principal_id,
                actor_session_id=first.actor_session_id, kind=first.kind,
                mutation_idempotency_key=str(uuid4()), mutation_request_digest=digest({"fixture_correction": number}),
                metadata_json=canonical({**metadata, "supersedes_event_id": tip}))
            db.add(row)
            await db.flush()
            tip = row.event_id
    async with async_db() as db:
        assert (await canonical_procedure_membership(db, scope))["feedback_count"] == 100
        assert (await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=original))["idempotent_replay"] is True
    async with async_db() as db:
        with pytest.raises(BoardError, match="history is full"):
            await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual",
                request=_request(supersedes_event_id=tip, reason="Explicit overflow correction"))
    async with async_db() as db:
        assert (await canonical_procedure_membership(db, scope))["feedback_count"] == 100
        first = await db.get(WorkBoardEvent, result["event_id"])
        db.add(WorkBoardEvent(task_id=first.task_id, owner_principal_id=first.owner_principal_id,
            owner_session_id=first.owner_session_id, actor_principal_id=first.actor_principal_id,
            actor_session_id=first.actor_session_id, kind=first.kind,
            mutation_idempotency_key=str(uuid4()), mutation_request_digest=digest("unsupported historical overflow"),
            metadata_json=canonical({**metadata, "supersedes_event_id": tip})))
    async with async_db() as db:
        with pytest.raises(BoardError, match="finite bound"):
            await canonical_procedure_membership(db, scope)


async def test_private_proof_and_metadata_bounds_fail_closed(async_db, monkeypatch, tmp_path: Path):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    file = tmp_path / "proof.json"
    raw = b'{"mechanical_file_fixture":true}'
    file.write_bytes(raw)
    file.chmod(0o600)
    sha = hashlib.sha256(raw).hexdigest()
    assert read_private_proof("proof.json", sha) == raw
    file.write_bytes(raw + b" ")
    with pytest.raises(BoardError, match="changed during staging"):
        read_private_proof("proof.json", sha)
    with file.open("wb") as stream:
        stream.truncate(MAX_FILE_BYTES + 1)
    with pytest.raises(BoardError, match="finite regular file"):
        read_private_proof("proof.json", sha)
    file.unlink()
    file.symlink_to(tmp_path / "missing-private-proof")
    with pytest.raises(OSError):
        read_private_proof("proof.json", sha)
    with pytest.raises(BoardError, match="metadata"):
        bounded_json(canonical({"oversized": "x" * MAX_METADATA_BYTES}))


@pytest.mark.parametrize("change", ["latest_attempt", "fence", "unended"])
async def test_stale_attempt_feedback_is_history_until_explicit_current_correction(async_db, monkeypatch, change):
    operator, scope = await _setup(async_db, monkeypatch)
    async with async_db() as db:
        await _ended_task(db, scope, "manual")
    original = _request()
    async with async_db() as db:
        first = await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=original)
    async with async_db() as db:
        if change == "latest_attempt":
            db.add(WorkBoardAttempt(attempt_id="attempt-next", task_id="manual", fencing_token=2,
                task_revision_at_claim=1, ended_at=datetime.now(timezone.utc), outcome="capability"))
        else:
            attempt = await db.get(WorkBoardAttempt, "attempt-manual")
            if change == "fence":
                attempt.fencing_token = 2
            else:
                attempt.ended_at = None
                attempt.outcome = None
    async with async_db() as db:
        member = (await canonical_procedure_membership(db, scope))["members"][0]
        assert member["feedback_tip"]["event_id"] == first["event_id"]
        assert member["feedback_tip"]["attempt_id"] == "attempt-manual"
        assert member["feedback_tip"]["attempt_fence"] == 1
        assert member["effective_feedback_tip"] is None
        assert (await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=original))["idempotent_replay"] is True
    correction = original.model_copy(update={"mutation_uuid": str(uuid4()), "supersedes_event_id": first["event_id"],
        "expected_attempt_id": member["attempt"]["attempt_id"], "expected_attempt_fence": member["attempt"]["fencing_token"],
        "reason": "Explicitly reviewed this current terminal failure metadata"})
    async with async_db() as db:
        if change == "unended":
            with pytest.raises(BoardError, match="current ended invocation"):
                await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=correction)
            return
        corrected = await record_procedure_feedback(db, operator, routine_id="routine", task_id="manual", request=correction)
    async with async_db() as db:
        token = await canonical_procedure_membership(db, scope)
        assert token["feedback_count"] == 2
        assert token["members"][0]["effective_feedback_tip"]["event_id"] == corrected["event_id"]
        assert (await db.get(WorkBoardEvent, first["event_id"])).event_id == first["event_id"]
