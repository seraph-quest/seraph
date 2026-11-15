"""Current ordinary formatter execution → private inert corrected method."""
import json
import socket
from pathlib import Path

import pytest
import httpx
from fastapi import FastAPI
from sqlalchemy import select

from src.db.models import WorkBoardTask, WorkBoardAttempt, WorkflowRunState, Memory, MemoryProposal
from src.auth.service import authenticate_token
from src.memory.task_lessons import LessonRequest, LessonScope, create_task_lesson, eligible_lesson_source, inspect_task_lesson
from tests.test_inference_accounting import accounting_db
from tests.test_tool_package_native import test_actual_authenticated_review_approval_native_formatter_reopen as formatter_journey


@pytest.mark.asyncio
async def _actual_formatter(accounting_db, monkeypatch):
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
    failure = None
    try:
        await formatter_journey(accounting_db, monkeypatch, "positive")
    except AssertionError as exc:
        # Only the exact real host sandbox failure is usable failure evidence.
        # All unrelated assertion failures retain the existing positive gate.
        root, _, diagnostic_factory = accounting_db
        receipts = list(root.glob("artifacts/tool-package-runs/*/out/supervisor-result.json"))
        if len(receipts) != 1:
            raise
        failure = json.loads(receipts[0].read_text())
        if failure.get("exit_code") != 1 or failure.get("cleanup_proven") is not True or "Failed RTM_NEWADDR: Operation not permitted" not in failure.get("stderr", ""):
            raise exc
    assert contacts == []
    assert len(tokens) == 1
    return tokens[0], failure, contacts


@pytest.mark.asyncio
async def test_existing_native_formatter_correction_is_private_and_never_activates(accounting_db, monkeypatch):
    token, failure, contacts = await _actual_formatter(accounting_db, monkeypatch)
    if failure is not None:
        pytest.skip("actual bwrap loopback RTM_NEWADDR denied by host; distinct terminal-failure lesson test covers this receipt")
    root, engine, factory = accounting_db
    from src.db import engine as db_engine
    monkeypatch.setattr(db_engine, "get_session", factory.accounting_sessions)
    async with factory.accounting_sessions() as db:
        task = (await db.execute(select(WorkBoardTask))).scalar_one()
        attempt = (await db.execute(select(WorkBoardAttempt))).scalar_one()
    # Reuse the actual issued HTTP login token; no canonical hash is promoted
    # into bearer authority and no service principal is synthesized.
    operator = await authenticate_token(token, touch=False)
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


@pytest.mark.asyncio
async def test_actual_failed_formatter_authenticated_lesson_has_proven_cleanup_and_no_activation(accounting_db, monkeypatch):
    # Fault only the existing optional lesson callback. Actual task admission,
    # native execution, terminal projection and process cleanup stay real.
    from src.memory import task_lessons
    async def failed_optional_callback(*args, **kwargs):
        raise OSError("declared optional callback failure")
    monkeypatch.setattr(task_lessons, "propose_automatic_task_lesson", failed_optional_callback)
    token, failure, contacts = await _actual_formatter(accounting_db, monkeypatch)
    assert failure is not None, "This negative journey requires the actually observed host sandbox failure"
    root, _, factory = accounting_db
    from src.db import engine as db_engine
    monkeypatch.setattr(db_engine, "get_session", factory.accounting_sessions)
    async with factory.accounting_sessions() as db:
        task = (await db.execute(select(WorkBoardTask))).scalar_one()
        attempt = (await db.execute(select(WorkBoardAttempt))).scalar_one()
        run = (await db.execute(select(WorkflowRunState))).scalar_one()
        assert task.status.value == "blocked" and attempt.ended_at is not None
        assert run.status == "failed" and run.finished_at is not None
        assert run.failure_reason == "tool_package_execution_failed"
        assert not json.loads(run.artifact_receipts_json)
        # The runtime pre-creates an empty confined output inode; it contains
        # no formatter output and no adopted artifact receipt.
        assert all(path.read_bytes() == b"" for path in root.glob("artifacts/tool-package-runs/*/out/result.json"))
        assert failure["cleanup_proven"] is True
    from src.api import memory
    from src.auth.middleware import OperatorAuthMiddleware
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(memory.router, prefix="/api")
    from config.settings import settings
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", cookies={settings.operator_auth_cookie_name: token}, headers={"Origin": "http://localhost:3001"}) as client:
        discovered = await client.get("/api/memory/task-lessons/sources/" + task.task_id)
        assert discovered.status_code == 200, discovered.text
        source = discovered.json()
        assert source["eligible"] and source["observed"]["status"] == "failed"
        assert source["automatic_policy"]["enabled"] is False
        assert source["automatic_outcome"]["reason_code"] == "automatic_lesson_unavailable"
        assert source["automatic_outcome"]["error_type"] == "OSError"
        request = {"task_id": task.task_id, "attempt_id": attempt.attempt_id,
            "correction": "Verify readback after the task.", "source_refs": source["source_refs"],
            "scope": source["scope"], "expected_revision": source["expected_revision"]}
        proposed = await client.post("/api/memory/task-lessons", json=request)
        assert proposed.status_code == 201, proposed.text
        result = proposed.json()
        inspected = await client.get("/api/memory/task-lessons/" + result["proposal_id"])
        assert inspected.status_code == 200, inspected.text
        draft = inspected.json()
        assert draft["source_current"] is True and draft["observed"]["status"] == "failed"
        assert draft["old_method"]["steps"][0]["capability_id"] == "work.json-format.v1"
        assert draft["new_method"]["steps"] == [*draft["old_method"]["steps"], {"kind": "guard", "check": "verified_readback"}]
        assert draft["reflection"]["provider_contacts"] == 0
        assert draft["behavior_changed"] is draft["positive_preference_vote"] is False
        api_receipt = root / "task-lesson-api-receipt.json"
        api_receipt.write_text(json.dumps({"source_get": source, "proposal_post": result,
            "inspect_get": draft, "native_failure": failure, "adopted_output": False}, indent=2))
        api_receipt.chmod(0o600)
        async with factory.accounting_sessions() as db:
            proposal = await db.get(MemoryProposal, result["proposal_id"])
            raw = (root / proposal.artifact_ref).read_bytes()
            import hashlib
            assert hashlib.sha256(raw).hexdigest() == result["candidate_digest"]
            assert (root / proposal.artifact_ref).stat().st_mode & 0o077 == 0
            assert b"<script>literal</script>" not in raw
            # The exact contract snapshot declares the json_text input field;
            # the admitted source value is never copied into either method.
            assert "json_text" not in json.loads(raw)["old_method"]["input_parameters"]
            assert "json_text" not in json.loads(raw)["new_method"]["input_parameters"]
            assert proposal.accepted_memory_id is None
            assert not list((await db.execute(select(Memory))).scalars())
            current = await db.get(WorkflowRunState, run.id)
            current.effect_receipts_json = json.dumps([{"status": "unknown"}])
        unknown = await client.get("/api/memory/task-lessons/sources/" + task.task_id)
        assert unknown.status_code == 200 and unknown.json()["eligible"] is False
        rejected = await client.post("/api/memory/task-lessons", json=request)
        assert rejected.status_code == 409
        assert rejected.json()["detail"]["code"] == "lesson_outcome_unresolved"
    assert contacts == []
