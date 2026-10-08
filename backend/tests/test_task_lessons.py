"""Ordinary task lesson mechanics; no model calls or improvement claims."""
from datetime import datetime, timezone
import json
import socket
import hashlib
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from config.settings import settings
from src.auth.service import create_session
from src.db.models import Goal, Memory, MemoryProposal, Session, WorkBoardAttempt, WorkBoardTask, WorkflowRunState, OperatorSession, WorkflowStepState
from src.memory.task_lessons import LessonRequest, LessonScope, TaskMethod, ResearchStrategy, create_task_lesson, inspect_task_lesson, _task
from src.work_board.repository import BoardError
from src.workflows.durable_state import workflow_state_repository
from src.memory.task_lessons import eligible_lesson_source, LessonAutoPolicyRequest, set_automatic_lesson_policy, propose_automatic_task_lesson, maybe_propose_automatic_lesson

pytestmark = pytest.mark.parametrize("async_db", ["file"], indirect=True)


@pytest.fixture(autouse=True)
def no_inference(monkeypatch):
    calls = []
    def denied(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("external inference is forbidden in task lesson tests")
    # Owned provider transports plus sockets are denied; SQLite uses no sockets.
    monkeypatch.setattr("litellm.completion", denied)
    monkeypatch.setattr("litellm.acompletion", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    yield calls
    assert calls == []


async def failed_local_task(async_db, monkeypatch, tmp_path):
    """Actually attempt a missing local file read and record its durable failure."""
    root = tmp_path / "workspace"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-lesson-test")
    monkeypatch.setattr("src.memory.m5.get_session", async_db)
    _, operator = await create_session()
    now = datetime.now(timezone.utc)
    async with async_db() as db:
        db.add(Session(id=operator.session_id))
        db.add(Goal(id="goal", title="Read a local document", revision=1, status="active",
            owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id))
        db.add(WorkBoardTask(task_id="task", owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id, goal_id="goal", goal_revision=1, task_revision=1,
            status="blocked", title="Read local document", capability_id="", idempotency_key="local-document-task"))
        await db.flush()
        db.add(WorkBoardAttempt(attempt_id="attempt", task_id="task", workflow_run_id="run",
            fencing_token=1, started_at=now, ended_at=now, outcome="capability"))
        db.add(WorkflowRunState(run_identity="run", root_run_identity="run", workflow_name="local-read",
            operator_session_id=operator.session_id, session_id=operator.session_id,
            owner_kind="user", owner_principal_id=operator.principal.principal_id,
            goal_id="goal", goal_revision=1, status="running", record_schema_version=1))
        await db.commit()
    async with async_db() as db:
        assert await _task(db, "task") is not None
    await workflow_state_repository.record_step_started(run_identity="run", workflow_name="local-read",
        step_id="read", step_index=1, tool_name="read_file", arguments={"file_path": "missing.txt"})
    from src.tools.filesystem_tool import read_file
    result = read_file.forward(file_path="missing.txt")
    assert "Error" in result or "not found" in result.lower()
    await workflow_state_repository.record_step_failed(run_identity="run", step_id="read",
        error_kind="FileNotFoundError", error_summary="Selected local source is absent")
    await workflow_state_repository.finish_run(run_identity="run", status="failed", error="Selected local source is absent")
    async with async_db() as db:
        saved = await _task(db, "task")
        assert saved is not None
        assert (saved.owner_principal_id, saved.owner_session_id) == (operator.principal.principal_id, operator.session_id)
    from src.db import engine as db_engine
    async with db_engine.get_session() as db:
        saved = await _task(db, "task")
        assert saved is not None
    request = LessonRequest(task_id="task", attempt_id="attempt", correction="Check source existence before drafting.",
        source_refs=["run", "attempt"], scope=LessonScope(goal_id="goal", goal_revision=1, family="knowledge"), expected_revision=1)
    return operator, request


@pytest.mark.asyncio
async def test_failed_task_correction_is_private_inspectable_idempotent_and_inert(async_db, monkeypatch, tmp_path, no_inference):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    result = await create_task_lesson(operator, request)
    assert result["status"] == "proposed"
    assert result["behavior_changed"] is False
    inspected = await inspect_task_lesson(operator, result["proposal_id"])
    assert inspected["observed"]["status"] == "failed"
    assert inspected["new_method"]["steps"][0] == {"kind": "guard", "check": "source_exists"}
    assert inspected["old_method"]["registered_tool_ids"] == ["read_file"]
    assert inspected["positive_preference_vote"] is False
    assert inspected["reflection"] == {"mode": "local_projection", "provider_contacts": 0, "spend_microusd": 0}
    assert inspected["source_current"] is True
    replay = await create_task_lesson(operator, request)
    assert replay["idempotent_replay"] is True
    assert replay["proposal_id"] == result["proposal_id"]
    async with async_db() as db:
        assert not list((await db.execute(select(Memory))).scalars())
        rows = list((await db.execute(select(MemoryProposal))).scalars())
        assert len(rows) == 1
        assert rows[0].preview_text is None
        path = tmp_path / "workspace" / rows[0].artifact_ref
        assert path.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_source_changes_block_inspection_and_generic_memory_acceptance(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    result = await create_task_lesson(operator, request)
    from src.memory.m5 import apply_memory_proposal_action
    with pytest.raises(ValueError, match="task_method_requires_specialized_review"):
        await apply_memory_proposal_action(owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id, proposal_id=result["proposal_id"], action="accept", expected_revision=1)
    async with async_db() as db:
        row = await _task(db, "task")
        row.task_revision = 2
    inspected = await inspect_task_lesson(operator, result["proposal_id"])
    assert inspected["source_current"] is False
    assert inspected["status"] == "blocked"
    assert inspected["reason_code"] == "lesson_source_changed"


@pytest.mark.asyncio
async def test_no_correction_is_no_change_and_unbound_source_is_rejected(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    result = await create_task_lesson(operator, request.model_copy(update={"correction": ""}))
    inspected = await inspect_task_lesson(operator, result["proposal_id"])
    assert inspected["new_method"] is None
    assert inspected["reason_code"] == "no_explicit_correction"
    with pytest.raises(BoardError, match="Only references verified"):
        await create_task_lesson(operator, request.model_copy(update={"source_refs": ["private-unrelated-source"]}))


@pytest.mark.asyncio
async def test_actual_completed_read_has_no_success_inferred_preference(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    path = tmp_path / "workspace" / "ordinary.txt"
    path.write_text("Ordinary permitted local source\n")
    path.chmod(0o600)
    from src.tools.filesystem_tool import read_file
    content = read_file.forward(file_path="ordinary.txt")
    assert content == path.read_text()
    sha = hashlib.sha256(content.encode()).hexdigest()
    await workflow_state_repository.record_step_completed(run_identity="run", step_id="read", status="completed", result=content)
    await workflow_state_repository.finish_run(run_identity="run", status="succeeded")
    async with async_db() as db:
        task = await _task(db, "task")
        task.status = "done"
        attempt = await db.get(WorkBoardAttempt, "attempt")
        attempt.outcome = "verified"
        attempt.receipt_refs_json = json.dumps([{"receipt_kind": "readback", "status": "succeeded", "verified": True,
            "workflow_run_id": "run", "content_sha256": sha, "readback_id": "readback:ordinary",
            "artifact_id": "artifact:ordinary", "verified_at": datetime.now(timezone.utc).isoformat()}])
    source = await eligible_lesson_source(operator, "task")
    assert source["eligible"] is True
    assert source["observed"]["status"] == "completed"
    request = request.model_copy(update={"source_refs": source["source_refs"], "correction": ""})
    result = await create_task_lesson(operator, request)
    inspected = await inspect_task_lesson(operator, result["proposal_id"])
    assert inspected["new_method"] is None
    assert inspected["positive_preference_vote"] is False
    assert inspected["result"] == "no_change"


@pytest.mark.asyncio
async def test_automatic_requires_explicit_policy_and_is_idempotent_without_positive_vote(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    denied = await propose_automatic_task_lesson(operator, "task")
    assert denied["reason_code"] == "automatic_lessons_not_opted_in"
    enabled = await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(
        enabled=True, expected_revision=1, mutation_uuid=str(uuid4())))
    assert enabled["enabled"] is True
    assert enabled["daily_cap"] == 2
    source = await eligible_lesson_source(operator, "task")
    assert source["automatic_policy"]["enabled"] is True
    async with async_db() as db:
        task = await _task(db, "task")
    result = await maybe_propose_automatic_lesson(task)
    assert result["result"] == "candidate_inert"
    inspected = await inspect_task_lesson(operator, result["proposal_id"])
    assert inspected["correction_provenance"] == "observed_failure_rule"
    assert inspected["positive_preference_vote"] is False
    replay = await maybe_propose_automatic_lesson(task)
    assert replay["idempotent_replay"] is True
    # Renewing proposal consent never renews the owner-wide daily cap.
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(
        enabled=True, expected_revision=1, mutation_uuid=str(uuid4())))
    assert (await maybe_propose_automatic_lesson(task))["result"] == "candidate_inert"
    await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(
        enabled=True, expected_revision=1, mutation_uuid=str(uuid4())))
    assert (await maybe_propose_automatic_lesson(task))["reason_code"] == "automatic_lesson_daily_cap"
    async with async_db() as db:
        rows = list((await db.execute(select(MemoryProposal))).scalars())
        assert sum(json.loads(row.provenance_json).get("automatic") is True for row in rows) == 2
    disabled = await set_automatic_lesson_policy(operator, "task", LessonAutoPolicyRequest(
        enabled=False, expected_revision=1, mutation_uuid=str(uuid4())))
    assert disabled["enabled"] is False
    assert (await maybe_propose_automatic_lesson(task))["reason_code"] == "automatic_lessons_not_opted_in"


@pytest.mark.asyncio
async def test_missing_redaction_revoked_root_secret_and_method_tamper_fail_closed(async_db, monkeypatch, tmp_path):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    with pytest.raises(BoardError, match="secret or authority"):
        await create_task_lesson(operator, request.model_copy(update={"correction": "api_key=never-copy-this"}))
    result = await create_task_lesson(operator, request)
    async with async_db() as db:
        step = (await db.execute(select(WorkflowStepState).where(WorkflowStepState.run_identity == "run"))).scalar_one()
        step.tool_name = "write_file"
    assert (await inspect_task_lesson(operator, result["proposal_id"]))["source_current"] is False
    async with async_db() as db:
        root = await db.get(OperatorSession, operator.session_id)
        root.revoked_at = datetime.now(timezone.utc)
    with pytest.raises(BoardError, match="no longer current"):
        await inspect_task_lesson(operator, result["proposal_id"])


def test_closed_method_schema_cannot_install_tools_or_modify_authority(async_db):
    method = {"schema_version": "TaskMethod.v1", "family": "knowledge", "steps": [{"kind": "registered_tool", "tool_id": "read_file"}],
        "registered_tool_ids": ["read_file"], "input_parameters": {},
        "output_contract": {"artifact_type": "task_result", "required_fields": ["status"]}}
    assert TaskMethod.model_validate(method)
    for changes in ({"permissions": ["all"]}, {"registered_tool_ids": ["install_new_tool"]},
                    {"input_parameters": {"provider": "other"}}, {"steps": []}):
        with pytest.raises(ValidationError):
            TaskMethod.model_validate({**method, **changes})
    with pytest.raises(ValidationError):
        ResearchStrategy.model_validate({"query_templates": [], "source_preferences": [],
            "required_evidence_fields": [], "draft_sections": [], "stop_conditions": ["Stop when bounded"], "code": "execute"})
