"""Real authenticated SQLite/Chromium procedure outcomes with HTTP intercepted.

Only public HTTP is a fixed boundary fixture. Root, source task, package review,
approvals, activation, two parent/leaf executions and feedback are production
paths. No successful native receipt or accepted memory is inserted by tests.
"""
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import WorkBoardAttempt, WorkBoardTask
from src.memory.procedure_recommendations import ProcedureFeedbackRequest, record_procedure_feedback, stage_procedure_bundle
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.job_runtime import DurableJobRepository
from src.workflows.procedure_service import ProcedureV2InvokeRequest
from tests.test_procedure_v2_native_vertical import _activate_v2_routine, _seed_browser_source

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("async_db", ["file"], indirect=True)]


async def test_two_real_manual_invocations_yield_verified_feedback_bundle(async_db, monkeypatch, tmp_path: Path):
    from playwright.async_api import async_playwright
    tmp_path.chmod(0o700)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-procedure-native-test")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    # Preserve the genuine authenticated creation context, not a service
    # session-ID lookup which intentionally has no bearer-hash request proof.
    from src.auth import service as auth_service
    real_create_session = auth_service.create_session
    authenticated = []
    async def create_session_with_context(**kwargs):
        token, operator = await real_create_session(**kwargs)
        authenticated.append(operator)
        return token, operator
    monkeypatch.setattr(auth_service, "create_session", create_session_with_context)
    async with async_playwright() as playwright:
        async def launch():
            return await playwright.chromium.launch(headless=True)
        source = await _seed_browser_source(async_db=async_db, monkeypatch=monkeypatch,
            tmp_path=tmp_path, browser=launch)
        routines, prepared, revision = await _activate_v2_routine(source, async_db=async_db,
            template_id="public-browser-check", name="Reviewed manual preference")
        owner = source["owner"]
        dispatcher = WorkBoardDispatcher(repository=source["repository"], jobs=DurableJobRepository(), session_provider=async_db)
        operator = next(item for item in authenticated if item.session_id == owner.session_id)
        task_ids = []
        for index in range(2):
            invocation = ProcedureV2InvokeRequest(version=1, expected_routine_revision=revision,
                goal_id=source["goal_id"], expected_goal_revision=1,
                parameters={"goal_id": source["goal_id"], "expected_goal_revision": 1}, invocation_uuid=str(uuid4()))
            admitted, status = await routines.invoke_v2(prepared["routine_id"], invocation,
                owner_principal_id=owner.principal_id, owner_session_id=owner.session_id)
            assert status == 201
            result = await dispatcher.run_pass()
            assert result["completed"] >= 1, result
            async with async_db() as db:
                task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == admitted["task_id"]))).scalar_one()
                attempt = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id))).scalar_one()
                feedback = ProcedureFeedbackRequest(version=1, expected_routine_revision=revision,
                    goal_id=source["goal_id"], expected_goal_revision=1,
                    expected_task_revision=task.task_revision, expected_attempt_id=attempt.attempt_id,
                    expected_attempt_fence=attempt.fencing_token, label="helpful", mutation_uuid=str(uuid4()))
                await record_procedure_feedback(db, operator, routine_id=prepared["routine_id"],
                    task_id=task.task_id, request=feedback)
                task_ids.append(task.task_id)
            bundle = await stage_procedure_bundle(operator, routine_id=prepared["routine_id"], version=1,
                routine_revision=revision, goal_id=source["goal_id"], goal_revision=1)
            projection = bundle.projection()
            assert projection["helpful_count"] == index + 1
            assert projection["status"] == ("no_learning" if index == 0 else "proposed")
        assert projection["included_count"] == 2
        assert sorted(item["task_id"] for item in projection["outcomes"]) == sorted(task_ids)
        assert all(item["verified"] and item["readback_id"] and item["artifact_digest"] for item in projection["outcomes"])
        assert projection["evidence_population"] == "matching_manual_invocations_only"
        assert projection["quality_evidence"] == "unmeasured"
        assert projection["memory_status"] == "no_learning"
        receipt = {"scope": projection["scope"], "outcomes": projection["outcomes"],
            "bundle_digest": bundle.bundle_digest, "membership": json.loads(bundle.membership_json),
            "files": json.loads(bundle.files_json), "boundary": "Real SQLite/auth/package/Chromium/native outcomes; fixed public HTTP fixture; no quality improvement claim"}
        proof_path = tmp_path / "native-stage-receipt.json"
        proof_path.write_text(json.dumps(receipt, indent=2))
        proof_path.chmod(0o600)
