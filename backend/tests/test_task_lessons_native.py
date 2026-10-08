"""Current ordinary formatter execution → private inert corrected method."""
import json
import socket
from pathlib import Path

import pytest
from sqlalchemy import select

from src.db.models import WorkBoardTask, WorkBoardAttempt, Memory, MemoryProposal
from src.auth.service import authenticate_token
from src.memory.task_lessons import LessonRequest, LessonScope, create_task_lesson, eligible_lesson_source, inspect_task_lesson
from tests.test_inference_accounting import accounting_db
from tests.test_tool_package_native import test_actual_authenticated_review_approval_native_formatter_reopen as formatter_journey


@pytest.mark.asyncio
async def test_existing_native_formatter_correction_is_private_and_never_activates(accounting_db, monkeypatch):
    contacts = []
    def denied(*args, **kwargs):
        contacts.append((args, kwargs))
        raise AssertionError("Implementation-time inference/network contact forbidden")
    monkeypatch.setattr("litellm.completion", denied)
    monkeypatch.setattr("litellm.acompletion", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    from src.api import auth
    actual_create = auth.create_session
    tokens = []
    async def capture_actual_login(*args, **kwargs):
        result = await actual_create(*args, **kwargs)
        tokens.append(result[0])
        return result
    monkeypatch.setattr(auth, "create_session", capture_actual_login)
    from src.execution import tool_package_profile
    actual_inspect = tool_package_profile.inspect_runtime
    def inspect_with_reason(*args, **kwargs):
        try:
            return actual_inspect(*args, **kwargs)
        except tool_package_profile.ToolPackageBlocked as exc:
            print(f"native formatter profile blocked: {exc}")
            raise
    monkeypatch.setattr(tool_package_profile, "inspect_runtime", inspect_with_reason)
    # Existing complete native path: HTTP auth/Goal/pack approval/input/task →
    # dispatcher → real constrained formatter process → actual reopen/readback.
    try:
        await formatter_journey(accounting_db, monkeypatch, "positive")
    except AssertionError:
        from src.db.models import WorkflowRunState
        _, _, diagnostic_factory = accounting_db
        async with diagnostic_factory.accounting_sessions() as db:
            runs = list((await db.execute(select(WorkflowRunState))).scalars())
            print([{ "status": run.status, "failure_reason": run.failure_reason,
                "result_summary": run.result_summary, "effects": json.loads(run.effect_receipts_json)} for run in runs])
        raise
    root, engine, factory = accounting_db
    from src.db import engine as db_engine
    monkeypatch.setattr(db_engine, "get_session", factory.accounting_sessions)
    async with factory.accounting_sessions() as db:
        task = (await db.execute(select(WorkBoardTask))).scalar_one()
        attempt = (await db.execute(select(WorkBoardAttempt))).scalar_one()
    # Reuse the actual issued HTTP login token; no canonical hash is promoted
    # into bearer authority and no service principal is synthesized.
    assert len(tokens) == 1
    operator = await authenticate_token(tokens[0], touch=False)
    source = await eligible_lesson_source(operator, task.task_id)
    assert source["eligible"] is True
    assert source["observed"]["status"] == "completed"
    result = await create_task_lesson(operator, LessonRequest(task_id=task.task_id,
        attempt_id=attempt.attempt_id, correction="Verify readback after the task.",
        source_refs=source["source_refs"], scope=LessonScope.model_validate(source["scope"]),
        expected_revision=source["expected_revision"]))
    inspected = await inspect_task_lesson(operator, result["proposal_id"])
    old = inspected["old_method"]
    new = inspected["new_method"]
    assert old["steps"][0]["kind"] == "registered_capability"
    assert old["steps"][0]["capability_id"] == "work.json-format.v1"
    assert new["steps"] == [*old["steps"], {"kind": "guard", "check": "verified_readback"}]
    assert old["registered_tool_ids"] == new["registered_tool_ids"] == []
    assert inspected["behavior_changed"] is False
    assert inspected["positive_preference_vote"] is False
    async with factory.accounting_sessions() as db:
        assert not list((await db.execute(select(Memory))).scalars())
        proposal = await db.get(MemoryProposal, result["proposal_id"])
        raw = (root / proposal.artifact_ref).read_text()
        assert "<script>literal</script>" not in raw
        assert "json_text" not in json.loads(raw)["old_method"]
        assert proposal.accepted_memory_id is None
    assert contacts == []
