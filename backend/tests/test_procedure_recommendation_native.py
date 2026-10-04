"""Real authenticated SQLite/Chromium procedure outcomes with HTTP intercepted.

Only public HTTP is a fixed boundary fixture. Root, source task, package review,
approvals, activation, two parent/leaf executions and feedback are production
paths. No successful native receipt or accepted memory is inserted by tests.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from config.settings import settings
from src.db.models import WorkBoardAttempt, WorkBoardTask, Memory, MemoryProposal, WorkBoardEvent, OperatorSession, Goal, WorkBoardInputArtifact, WorkflowRunState
from src.work_board.repository import BoardError
from src.memory.procedure_recommendations import ProcedureFeedbackRequest, record_procedure_feedback, stage_procedure_bundle
from src.memory.procedure_recommendation_job import ProcedureRecommendationRequest, prepare_recommendation, inspect_recommendation
from src.memory.procedure_preferences import ProcedurePreferenceActionRequest, apply_preference_action, inspect_preference
from src.memory.procedure_selection import current_procedure_preference
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.job_runtime import DurableJobRepository
from src.workflows.procedure_service import ProcedureV2InvokeRequest
from tests.test_procedure_v2_native_vertical import _activate_v2_routine, _seed_browser_source

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("async_db", ["file"], indirect=True)]


@pytest.mark.parametrize("barrier", ["normal", "writer_io", "feedback", "feedback_binding", "phantom", "root_revoked", "root_expired", "goal_changed", "input_changed", "readback_changed", "finalization_phantom", "package_paused", "cancel", "selection_phantom"])
async def test_two_real_manual_invocations_yield_verified_feedback_bundle(async_db, monkeypatch, tmp_path: Path, barrier):
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
            if barrier == "feedback_binding":
                async with async_db() as db:
                    pending = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == admitted["task_id"]))).scalar_one()
                    premature = ProcedureFeedbackRequest(version=1, expected_routine_revision=revision,
                        goal_id=source["goal_id"], expected_goal_revision=1, expected_task_revision=pending.task_revision,
                        expected_attempt_id=None, expected_attempt_fence=None, label="helpful", mutation_uuid=str(uuid4()))
                    with pytest.raises(BoardError, match="current ended invocation"):
                        await record_procedure_feedback(db, operator, routine_id=prepared["routine_id"],
                            task_id=pending.task_id, request=premature)
                async with async_db() as db:
                    assert not (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.mutation_idempotency_key == premature.mutation_uuid))).scalars().all()
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
            recommendation_request = ProcedureRecommendationRequest(version=1,
                expected_routine_revision=revision, goal_id=source["goal_id"], expected_goal_revision=1,
                request_uuid=str(uuid4()))
            recommended = await prepare_recommendation(operator, prepared["routine_id"], recommendation_request)
            assert recommended["job_status"] == "succeeded"
            assert recommended["status"] == projection["status"]
            assert bool(recommended["proposal_id"]) == (index == 1)
            assert (await prepare_recommendation(operator, prepared["routine_id"], recommendation_request))["job_id"] == recommended["job_id"]
            assert (await inspect_recommendation(operator, prepared["routine_id"], recommended["job_id"]))["bundle_digest"] == bundle.bundle_digest
        assert projection["included_count"] == 2
        assert sorted(item["task_id"] for item in projection["outcomes"]) == sorted(task_ids)
        assert all(item["verified"] and item["readback_id"] and item["artifact_digest"] for item in projection["outcomes"])
        assert projection["evidence_population"] == "matching_manual_invocations_only"
        assert projection["quality_evidence"] == "unmeasured"
        assert projection["memory_status"] == "no_learning"
        if barrier == "finalization_phantom":
            from src.memory import procedure_recommendation_job as jobs
            from uuid import uuid5, NAMESPACE_URL
            real_stage = jobs.stage_procedure_bundle
            fresh = recommendation_request.model_copy(update={"request_uuid": str(uuid4())})
            job_id = str(uuid5(NAMESPACE_URL, f"seraph:procedure-recommendation:{owner.principal_id}:{owner.session_id}:{fresh.request_uuid}"))
            async def stage_then_insert(*args, **kwargs):
                staged = await real_stage(*args, **kwargs)
                await routines.invoke_v2(prepared["routine_id"], invocation.model_copy(update={"invocation_uuid": str(uuid4())}),
                    owner_principal_id=owner.principal_id, owner_session_id=owner.session_id)
                return staged
            monkeypatch.setattr(jobs, "stage_procedure_bundle", stage_then_insert)
            with pytest.raises(BoardError, match="Matching invocation outcomes or feedback changed"):
                await prepare_recommendation(operator, prepared["routine_id"], fresh)
            blocked = await jobs.durable_job_repository.get_job(job_id)
            assert blocked["status"] == "blocked" and blocked["effects"] == []
            assert not any(item.get("receipt_kind") == "readback" for item in blocked["effects"])
            async with async_db() as db:
                assert not (await db.execute(select(Memory))).scalars().all()
                assert not (await db.execute(select(MemoryProposal).where(MemoryProposal.proposal_job_id == job_id))).scalars().all()
            path = tmp_path / "native-finalization-barrier-receipt.json"
            path.write_text(json.dumps({"barrier": barrier, "job_id": job_id, "job_status": blocked["status"],
                "positive_effects": blocked["effects"], "new_proposal_count": 0, "memory_status": "no_learning",
                "original_proposal_id": recommended["proposal_id"], "membership": json.loads(bundle.membership_json)}, indent=2))
            path.chmod(0o600)
            return
        review = await inspect_preference(operator, recommended["proposal_id"])
        action = ProcedurePreferenceActionRequest(action="accept", expected_revision=review["revision"],
            expected_preview_text_digest=review["preview_text_digest"], expected_bundle_digest=review["bundle_digest"],
            acknowledged_selection_only=True, mutation_uuid=str(uuid4()))
        if barrier == "feedback_binding":
            from src.work_board.contracts import WorkBoardCommentCreate
            async def comment_current():
                async with async_db() as db:
                    task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_ids[0]))).scalar_one()
                    await source["repository"].add_comment(db, owner, task.task_id,
                        WorkBoardCommentCreate(expected_revision=task.task_revision, body="Explicit operator context correction"))
            async def correct_current(staged):
                member = next(item for item in json.loads(staged.membership_json)["members"] if item["task"]["task_id"] == task_ids[0])
                request = ProcedureFeedbackRequest(version=1, expected_routine_revision=revision,
                    goal_id=source["goal_id"], expected_goal_revision=1,
                    expected_task_revision=member["task"]["task_revision"],
                    expected_attempt_id=member["attempt"]["attempt_id"], expected_attempt_fence=member["attempt"]["fencing_token"],
                    supersedes_event_id=member["feedback_tip"]["event_id"], label="helpful",
                    reason="Reviewed the exact current outcome after the operator context correction", mutation_uuid=str(uuid4()))
                async with async_db() as db:
                    result = await record_procedure_feedback(db, operator, routine_id=prepared["routine_id"], task_id=task_ids[0], request=request)
                return request, result
            async def stage_current():
                return await stage_procedure_bundle(operator, routine_id=prepared["routine_id"], version=1,
                    routine_revision=revision, goal_id=source["goal_id"], goal_revision=1)
            await comment_current()
            stale = await stage_current()
            stale_projection = stale.projection()
            assert stale_projection["status"] == "no_learning" and stale_projection["reason_code"] == "feedback_outcome_stale"
            assert stale_projection["helpful_count"] == 1
            item = next(item for item in stale_projection["outcomes"] if item["task_id"] == task_ids[0])
            assert item["verified"] and item["feedback"] is None and item["feedback_history_label"] == "helpful"
            with pytest.raises(BoardError):
                await apply_preference_action(operator, review["proposal_id"], action)
            async with async_db() as db:
                assert not (await db.execute(select(Memory))).scalars().all()
                assert (await db.get(MemoryProposal, review["proposal_id"])).status == "proposed"
            correction, corrected = await correct_current(stale)
            eligible = await stage_current()
            assert eligible.projection()["helpful_count"] == 2 and eligible.projection()["status"] == "proposed"
            renewed = await prepare_recommendation(operator, prepared["routine_id"], recommendation_request.model_copy(update={"request_uuid": str(uuid4())}))
            renewed_review = await inspect_preference(operator, renewed["proposal_id"])
            renewed_action = action.model_copy(update={"expected_revision": renewed_review["revision"],
                "expected_preview_text_digest": renewed_review["preview_text_digest"], "expected_bundle_digest": renewed_review["bundle_digest"],
                "mutation_uuid": str(uuid4())})
            accepted = await apply_preference_action(operator, renewed_review["proposal_id"], renewed_action)
            assert accepted["status"] == "accepted"
            assert (await current_procedure_preference(operator, routine_id=prepared["routine_id"], version=1,
                routine_revision=revision, goal_id=source["goal_id"], goal_revision=1))["status"] == "suggested"
            await comment_current()
            assert (await current_procedure_preference(operator, routine_id=prepared["routine_id"], version=1,
                routine_revision=revision, goal_id=source["goal_id"], goal_revision=1))["status"] == "blocked"
            async with async_db() as db:
                assert (await record_procedure_feedback(db, operator, routine_id=prepared["routine_id"], task_id=task_ids[0], request=correction))["idempotent_replay"] is True
            stale_again = await stage_current()
            assert stale_again.projection()["status"] == "no_learning"
            final_correction, final_result = await correct_current(stale_again)
            final_bundle = await stage_current()
            assert final_bundle.projection()["status"] == "proposed" and final_bundle.projection()["helpful_count"] == 2
            assert json.loads(final_bundle.membership_json)["feedback_count"] == 4
            rolled = await apply_preference_action(operator, accepted["proposal_id"], renewed_action.model_copy(update={
                "action": "rollback", "expected_revision": accepted["revision"], "reason": "Historical preference rollback after exact feedback test", "mutation_uuid": str(uuid4())}))
            assert rolled["status"] == "rolled_back"
            path = tmp_path / "native-feedback-binding-receipt.json"
            path.write_text(json.dumps({"preexecution_feedback_rejected_without_event": True,
                "stale_proposal_adoption_rejected": True, "accepted_preference_selection_rejected_after_revision_change": True,
                "historical_exact_replay_did_not_regrant": True, "new_current_correction_restores_eligibility": True,
                "stale_projection": stale_projection, "final_projection": final_bundle.projection(),
                "membership": json.loads(final_bundle.membership_json), "accepted": accepted, "rolled_back": rolled,
                "boundary": "Genuine native successes and actual Board comment CAS task-revision changes; no invented attempts/success/memory; fixed public HTTP fixture, no quality claim"}, indent=2))
            path.chmod(0o600)
            return
        from src.memory import procedure_preferences as preferences
        from src.db import engine as db_engine
        if barrier in {"feedback", "phantom", "root_revoked", "root_expired", "goal_changed", "input_changed", "readback_changed", "package_paused"}:
            real_stage = preferences.stage_procedure_bundle
            async def stage_then_change(*args, **kwargs):
                staged = await real_stage(*args, **kwargs)
                if barrier == "feedback":
                    async with async_db() as db:
                        tip = next(item["feedback_event_id"] for item in projection["outcomes"] if item["task_id"] == task_ids[-1])
                        correction = feedback.model_copy(update={"supersedes_event_id": tip,
                            "label": "harmful", "reason": "Corrected after staging", "mutation_uuid": str(uuid4())})
                        await record_procedure_feedback(db, operator, routine_id=prepared["routine_id"], task_id=task_ids[-1], request=correction)
                elif barrier == "phantom":
                    await routines.invoke_v2(prepared["routine_id"], invocation.model_copy(update={"invocation_uuid": str(uuid4())}),
                        owner_principal_id=owner.principal_id, owner_session_id=owner.session_id)
                elif barrier == "root_revoked":
                    await auth_service.revoke_session(operator.session_id)
                elif barrier == "root_expired":
                    async with async_db() as db:
                        root = await db.get(OperatorSession, operator.session_id)
                        root.idle_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                elif barrier == "goal_changed":
                    async with async_db() as db:
                        goal = await db.get(Goal, source["goal_id"])
                        goal.revision += 1
                elif barrier == "input_changed":
                    async with async_db() as db:
                        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_ids[-1]))).scalar_one()
                        artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
                        artifact.metadata_digest = "d" * 64
                elif barrier == "readback_changed":
                    member = next(item for item in json.loads(staged.membership_json)["members"] if item["task"]["task_id"] == task_ids[-1])
                    async with async_db() as db:
                        parent = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == member["parent"]["run_identity"]))).scalar_one()
                        parent.effect_receipts_json = "[]"
                else:
                    from src.extensions.capability_pack import CapabilityPackLifecycle
                    from src.workflows.routines import _routine_pack_id
                    lifecycle = CapabilityPackLifecycle()
                    pack_id = _routine_pack_id(prepared["routine_id"], 1)
                    pending = lifecycle.prepare_operator_approval(pack_id, action="pause", goal_id=source["goal_id"],
                        digest=staged.scope.package_digest, owner_principal_id=owner.principal_id, session_id=owner.session_id)
                    approval_id = pending["approval"]["approval_id"]
                    lifecycle.resolve_operator_approval(pack_id, approval_id, decision="approved",
                        owner_principal_id=owner.principal_id, session_id=owner.session_id)
                    lifecycle.pause(pack_id, approval_id=approval_id, owner_principal_id=owner.principal_id,
                        session_id=owner.session_id)
                return staged
            monkeypatch.setattr(preferences, "stage_procedure_bundle", stage_then_change)
            with pytest.raises(BoardError):
                await apply_preference_action(operator, review["proposal_id"], action)
            async with async_db() as db:
                assert not (await db.execute(select(Memory))).scalars().all()
                unchanged = await db.get(MemoryProposal, review["proposal_id"])
                assert unchanged.revision == review["revision"] and unchanged.accepted_memory_id is None
                assert not (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.mutation_idempotency_key == action.mutation_uuid))).scalars().all()
            path = tmp_path / "native-barrier-receipt.json"
            path.write_text(json.dumps({"barrier": barrier, "proposal_id": review["proposal_id"], "rejected": True,
                "canonical_memory_count": 0, "proposal_unchanged": True, "action_audit_absent": True,
                "membership": json.loads(bundle.membership_json), "files": json.loads(bundle.files_json)}, indent=2))
            path.chmod(0o600)
            return
        if barrier == "writer_io":
            # The actual successful adoption/rollback writer is guarded; the
            # fixture does not supply a canonical source or signed-memory row.
            import builtins
            import os
            from contextlib import asynccontextmanager
            from src.memory import m5, repository as memory_repository_module
            writer = {"active": False, "entries": 0}
            real_begin = preferences._begin_sqlite_immediate
            real_session = db_engine.get_session
            async def begin(db):
                await real_begin(db)
                writer.update(active=True, entries=writer["entries"] + 1)
            @asynccontextmanager
            async def session():
                assert not writer["active"], "nested session inside preference writer"
                try:
                    async with real_session() as db:
                        yield db
                finally:
                    writer["active"] = False
            def no_writer_io(real):
                def checked(*args, **kwargs):
                    assert not writer["active"], "physical/key read inside preference writer"
                    return real(*args, **kwargs)
                return checked
            real_sanitize = m5.sanitize_m5_memory_text_async
            async def sanitize(text):
                assert not writer["active"], "Vault-backed redaction inside writer"
                return await real_sanitize(text)
            monkeypatch.setattr(preferences, "_begin_sqlite_immediate", begin)
            monkeypatch.setattr(db_engine, "get_session", session)
            for module, name in ((builtins, "open"), (os, "open"), (Path, "read_bytes"), (Path, "read_text"),
                                 (preferences, "_effect_mac_key"), (memory_repository_module, "_effect_mac_key")):
                monkeypatch.setattr(module, name, no_writer_io(getattr(module, name)))
            monkeypatch.setattr(m5, "sanitize_m5_memory_text_async", sanitize)
        adopted = await apply_preference_action(operator, review["proposal_id"], action)
        assert adopted["status"] == "accepted" and adopted["accepted_memory_id"]
        selection = await current_procedure_preference(operator, routine_id=prepared["routine_id"], version=1,
            routine_revision=revision, goal_id=source["goal_id"], goal_revision=1)
        assert selection["status"] == "suggested" and selection["suggested_version_id"] == bundle.scope.version_id
        assert selection["review"]["included_count"] == 2
        assert (await apply_preference_action(operator, review["proposal_id"], action))["idempotent_replay"] is True
        if barrier == "selection_phantom":
            added, _ = await routines.invoke_v2(prepared["routine_id"], invocation.model_copy(update={"invocation_uuid": str(uuid4())}),
                owner_principal_id=owner.principal_id, owner_session_id=owner.session_id)
            stale = await current_procedure_preference(operator, routine_id=prepared["routine_id"], version=1,
                routine_revision=revision, goal_id=source["goal_id"], goal_revision=1)
            assert stale["status"] == "blocked" and stale["memory_status"] == "no_learning"
            async with async_db() as db:
                assert not (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == added["task_id"]))).scalars().all()
        if barrier == "cancel":
            from src.memory import procedure_recommendation_job as jobs
            from src.memory.procedure_recommendation_job import ProcedureRecommendationCancelRequest, cancel_recommendation
            from uuid import uuid5, NAMESPACE_URL
            real_stage = jobs.stage_procedure_bundle
            cancel_body = None
            new_request = recommendation_request.model_copy(update={"request_uuid": str(uuid4())})
            new_job_id = str(uuid5(NAMESPACE_URL, f"seraph:procedure-recommendation:{owner.principal_id}:{owner.session_id}:{new_request.request_uuid}"))
            async def stage_then_cancel(*args, **kwargs):
                nonlocal cancel_body
                staged = await real_stage(*args, **kwargs)
                running = await inspect_recommendation(operator, prepared["routine_id"], new_job_id)
                cancel_body = ProcedureRecommendationCancelRequest(**new_request.model_dump(),
                    expected_job_revision=running["job_revision"], expected_fencing_token=running["fencing_token"])
                cancelled = await cancel_recommendation(operator, prepared["routine_id"], new_job_id, cancel_body)
                assert cancelled["job_status"] == "cancelled"
                return staged
            monkeypatch.setattr(jobs, "stage_procedure_bundle", stage_then_cancel)
            cancelled = await prepare_recommendation(operator, prepared["routine_id"], new_request)
            assert cancelled["job_status"] == "cancelled" and cancelled["memory_status"] == "no_learning"
            canonical_job = await jobs.durable_job_repository.get_job(new_job_id)
            assert canonical_job["artifacts"] == [] and canonical_job["effects"] == []
            assert (await cancel_recommendation(operator, prepared["routine_id"], new_job_id, cancel_body))["job_revision"] == cancelled["job_revision"]
            with pytest.raises(BoardError, match="another exact request"):
                await cancel_recommendation(operator, prepared["routine_id"], new_job_id,
                    cancel_body.model_copy(update={"request_uuid": str(uuid4())}))
            async with async_db() as db:
                patterns = (await db.execute(select(Memory))).scalars().all()
                assert len(patterns) == 1 and patterns[0].id == adopted["accepted_memory_id"]
                assert (await db.get(MemoryProposal, review["proposal_id"])).status == "accepted"
        rollback = ProcedurePreferenceActionRequest(action="rollback", expected_revision=adopted["revision"],
            expected_preview_text_digest=adopted["preview_text_digest"], expected_bundle_digest=adopted["bundle_digest"],
            acknowledged_selection_only=True, reason="Mechanical test rollback", mutation_uuid=str(uuid4()))
        rolled_back = await apply_preference_action(operator, review["proposal_id"], rollback)
        assert rolled_back["status"] == "rolled_back"
        if barrier == "writer_io":
            assert writer["entries"] == 2
        assert (await current_procedure_preference(operator, routine_id=prepared["routine_id"], version=1,
            routine_revision=revision, goal_id=source["goal_id"], goal_revision=1))["status"] == "none"
        receipt = {"scope": projection["scope"], "outcomes": projection["outcomes"],
            "recommendation": recommended,
            "adopted": adopted, "rolled_back": rolled_back,
            "selected": selection, "barrier": barrier,
            "bundle_digest": bundle.bundle_digest, "membership": json.loads(bundle.membership_json),
            "files": json.loads(bundle.files_json), "boundary": "Real SQLite/auth/package/Chromium/native outcomes; fixed public HTTP fixture; no quality improvement claim"}
        proof_path = tmp_path / "native-stage-receipt.json"
        proof_path.write_text(json.dumps(receipt, indent=2))
        proof_path.chmod(0o600)
