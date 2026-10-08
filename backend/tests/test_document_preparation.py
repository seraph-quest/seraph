"""Actual adopted source → inert task → native job → cited private readback."""
import hashlib
import json
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from tests.test_inference_accounting import accounting_db
from tests.test_general_documents import fixture_bytes, read_request
from tests.test_general_task_planner import prepare, forbid_external_inference
from tests.test_work_board_m6_provider_free_journey import _goal


@pytest.mark.parametrize("fmt,scenario", [(fmt, "success") for fmt in ("pdf", "docx", "xlsx", "csv")]
    + [("csv", case) for case in ("late_source", "late_cipher", "late_cancel", "late_fence", "late_deadline", "late_goal", "late_root", "early_goal")])
async def test_actual_document_preparation_task_private_readback(accounting_db, monkeypatch, fmt, scenario):
    from src.api import documents, work_board
    from src.auth.service import create_session, authenticate_token
    from src.work_board.contracts import WorkBoardOwner
    from src.native_tools.registry import ToolRegistry
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.db.models import WorkflowRunState
    from src.vault import crypto
    from config.settings import settings
    jobs, owner = await prepare(accounting_db, monkeypatch)
    token, operator = await create_session()
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    workspace, _engine, factory = accounting_db
    sessions = factory.accounting_sessions
    monkeypatch.setattr(crypto, "_fernet", None)
    monkeypatch.setattr(settings, "vault_encryption_key", "")
    monkeypatch.setattr(documents, "get_session", sessions)
    goal = _goal("prepare-" + fmt, "Local cited preparation")
    goal.owner_principal_id, goal.owner_session_id = owner.principal_id, owner.session_id
    async with sessions() as db:
        db.add(goal)
    registry = ToolRegistry(); registry.start()
    actual_invoke = registry._invoke_document
    async def diagnostic_invoke(*args, **kwargs):
        job_id = args[3]
        try:
            result = await actual_invoke(*args, **kwargs)
            if scenario.startswith("late_"):
                from datetime import datetime, timedelta, timezone
                from src.db.models import WorkBoardInputArtifact, WorkBoardAttempt
                from src.work_board.input_artifacts import _metadata_digest
                async with sessions() as db:
                    if scenario in {"late_source", "late_cipher"}:
                        row = await db.get(WorkBoardInputArtifact, result["source_binding"]["artifact_ref"].split(":")[1])
                        if scenario == "late_source":
                            row.revision += 1; row.metadata_digest = _metadata_digest(row)
                        else:
                            from src.work_board import document_pairs as sources
                            path = sources.source_path(row, sources.metadata(row), "evidence")
                            ciphertext = path.read_bytes(); path.write_bytes(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
                    elif scenario == "late_goal":
                        from src.db.models import Goal
                        current = await db.get(Goal, goal.id); current.revision += 1
                    elif scenario == "late_root":
                        from src.db.models import OperatorSession
                        current = await db.get(OperatorSession, owner.session_id); current.revoked_at = datetime.now(timezone.utc)
                    elif scenario == "late_cancel":
                        child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
                        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.workflow_run_id == child.parent_job_id))
                        attempt.cancel_requested_at = datetime.now(timezone.utc)
                    else:
                        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
                        if scenario == "late_fence": run.fencing_token += 1
                        else: run.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            return result
        except Exception as exc:
            print("NATIVE ADAPTER ERROR", type(exc).__name__, getattr(exc, "code", str(exc)))
            raise
    monkeypatch.setattr(registry, "_invoke_document", diagnostic_invoke)
    service = GeneralTaskService(registry); service.start()
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    monkeypatch.setattr(work_board, "dispatcher", dispatcher)
    credentials = {"token": token}
    app = FastAPI()
    @app.middleware("http")
    async def auth(request, call_next):
        request.state.operator = await authenticate_token(credentials["token"], touch=False)
        return await call_next(request)
    app.include_router(documents.router, prefix="/api")
    app.include_router(work_board.router, prefix="/api")
    async with documents.lifespan(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            raw = fixture_bytes(fmt)
            response = await client.post("/api/documents/sources", json={"format": fmt,
                "source": {"size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
                "goal_id": goal.id, "goal_revision": 1, "idempotency_key": "source", "no_learning": True})
            assert response.status_code == 200, response.text
            state = response.json(); identifier = state["artifact_id"]
            response = await client.put(f"/api/documents/sources/{identifier}/content", params={"expected_revision": state["revision"]},
                content=raw, headers={"content-type": "application/octet-stream"})
            assert response.status_code == 200, response.text
            response = await client.post(f"/api/documents/sources/{identifier}/seal", params={"expected_revision": response.json()["revision"]})
            assert response.status_code == 200, response.text
            parsed = await client.post("/api/documents/read", json=read_request(fmt, identifier).model_dump())
            assert parsed.status_code == 200 and parsed.json()["status"] == "succeeded", parsed.text
            assert parsed.json()["cleanup"] == "wait_reaped"
            section = parsed.json()["evidence"]["sections"][0]
            leaf = next((cell for section in parsed.json()["evidence"]["sections"] for cell in section["table_cells"] if cell["formula"]),
                (section["table_cells"] or [section])[0])
            state = (await client.get(f"/api/documents/sources/{identifier}")).json()
            body = {"artifact_ref": state["artifact_ref"], "expected_source_revision": state["revision"],
                "citation_refs": [leaf["source_ref"]], "acknowledge_local_use": True, "idempotency_key": "local-preparation"}
            for altered, expected in [({"acknowledge_local_use": False}, 422),
                ({"acknowledge_local_use": 1}, 422),
                ({"citation_refs": [leaf["source_ref"], leaf["source_ref"]]}, 422),
                ({"citation_refs": [state["artifact_ref"] + "#not-a-leaf"]}, 409),
                ({"expected_source_revision": state["revision"] + 1}, 409)]:
                denied = await client.post("/api/documents/preparations", json={**body, **altered})
                assert denied.status_code == expected, denied.text
            response = await client.post("/api/documents/preparations", json=body)
            assert response.status_code == 200, response.text
            task = response.json()["task"]; task_id = task["task_id"]
            assert task["status"] == "triage"
            replay = await client.post("/api/documents/preparations", json=body)
            assert replay.status_code == 200 and replay.json()["idempotent_replay"], replay.text
            assert (await client.get(f"/api/documents/preparations/{task_id}")).status_code == 409
            plan = (await client.get(f"/api/work-board/tasks/{task_id}/plan")).json()
            assert not plan["accepted"] and plan["task_input"]["limits"]["max_inference_calls"] == 0
            assert not plan["task_input"]["inference_egress_acknowledged"]
            injected = {"goal_revision": 1, "idempotency_key": "egress-injection", "input": {**plan["task_input"], "inference_egress_acknowledged": True},
                "plan": plan["plan"], "expected_plan_revision": 1}
            rejected = await client.post("/api/work-board/general-tasks", json=injected)
            assert rejected.status_code == 422, rejected.text
            response = await client.post(f"/api/work-board/tasks/{task_id}/actions", json={"action": "promote", "expected_revision": task["task_revision"]})
            assert response.status_code == 200, response.text
            if scenario == "early_goal":
                from src.db.models import Goal
                async with sessions() as db:
                    current = await db.get(Goal, goal.id); current.revision += 1
            outcome = await dispatcher.run_pass()
            if scenario != "success":
                assert outcome["completed"] == 0, outcome
                if scenario == "late_root":
                    from src.auth.service import AuthFailure
                    with pytest.raises(AuthFailure): await authenticate_token(token, touch=False)
                else:
                    readback = await client.get(f"/api/documents/preparations/{task_id}")
                    assert readback.status_code == 409 and leaf["text"] not in readback.text
                async with sessions() as db:
                    runs = list((await db.scalars(select(WorkflowRunState))).all())
                for run in runs:
                    assert run.status != "succeeded"
                    from tests.test_general_task_persistence import actual_output_checkpoints
                    assert not actual_output_checkpoints(await jobs.get_job(run.run_identity))
                print(json.dumps({"negative": scenario, "succeeded": False, "provider_contacts": 0}))
                return
            if outcome["completed"] != 1:
                async with sessions() as db:
                    diagnostic = list((await db.scalars(select(WorkflowRunState))).all())
                print([await jobs.get_job(row.run_identity) for row in diagnostic])
            assert outcome["completed"] == 1, outcome
            response = await client.get(f"/api/documents/preparations/{task_id}")
            assert response.status_code == 200, response.text
            view = response.json()
            assert view["sections"][0]["source_ref"] == leaf["source_ref"]
            assert view["sections"][0]["text"] == leaf["text"]
            if leaf.get("formula"):
                assert view["sections"][0]["formula"] == leaf["formula"]
                assert view["sections"][0]["cached_value"] == leaf["cached_value"]
            assert view["no_learning"] and view["provider_contacts"] == 0
            assert len(json.dumps(view).encode()) < 16384
            async with sessions() as db:
                runs = list((await db.scalars(select(WorkflowRunState))).all())
            roots = [run for run in runs if run.job_kind == "agent.task.v1"]
            children = [run for run in runs if run.job_kind == "general_task_native_tool_v1"]
            assert len(runs) == 2 and len(roots) == len(children) == 1
            assert roots[0].status == children[0].status == "succeeded"
            assert children[0].parent_job_id == roots[0].run_identity and children[0].attempt_count == 1
            root = roots[0]
            projection = await jobs.get_job(root.run_identity)
            encoded = json.dumps(projection)
            assert leaf["text"] not in encoded
            artifact = next(item["payload"] for item in projection["checkpoints"] if item["checkpoint_id"] == "general:verified:prepare")
            packet = (workspace / artifact["file_path"]).read_text()
            assert leaf["text"] not in packet and "source_binding" in packet
            from src.db.models import WorkBoardTask, WorkBoardEvent, AuditEvent
            async with sessions() as db:
                persisted_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
                events = list((await db.scalars(select(WorkBoardEvent).where(WorkBoardEvent.task_id == task_id))).all())
                audit_events = list((await db.scalars(select(AuditEvent).where(AuditEvent.tool_name == "document_prepare"))).all())
            input_file = workspace / persisted_task.typed_input_ref.removeprefix("workspace-json:")
            generic_values = [input_file.read_text(), persisted_task.title, persisted_task.body,
                *(event.metadata_json for event in events), *(run.arguments_json for run in runs),
                *(run.checkpoint_receipts_json for run in runs), *(run.artifact_receipts_json for run in runs),
                *(run.effect_receipts_json for run in runs),
                *(event.details_json for event in audit_events)]
            assert all(leaf["text"] not in value for value in generic_values)
            assert {event.event_type for event in audit_events} == {"tool_call", "tool_result"}
            print(json.dumps({"format": fmt, "task_id": task_id, "job_id": root.run_identity,
                "artifact_sha256": artifact["content_sha256"], "provider_contacts": 0, "no_learning": True}))
            accounting = await jobs.inference_accounting_snapshot()
            assert accounting["operation_count"] == 0
            other_token, _other_operator = await create_session()
            credentials["token"] = other_token
            cross_owner = await client.get(f"/api/documents/preparations/{task_id}")
            assert cross_owner.status_code == 403 and leaf["text"] not in cross_owner.text
            assert cross_owner.json()["detail"]["code"] == "task_owner_mismatch"
            credentials["token"] = token
            async with sessions() as db:
                from src.db.models import Goal
                current = await db.get(Goal, goal.id)
                current.revision += 1
            stale = await client.get(f"/api/documents/preparations/{task_id}")
            assert stale.status_code == 409 and leaf["text"] not in stale.text
    service.stop(); registry.stop()


def test_selection_is_exact_leaf_only_and_never_truncates_private_output():
    from src.work_board.document_preparation import selected_view
    from src.work_board.documents import DocumentEvidence
    from src.work_board.repository import BoardError
    source = "a" * 64
    evidence = DocumentEvidence(sections=[{"source_ref": "table", "text": "unselected-container-canary",
        "table_cells": [{"source_ref": "cell", "text": "selected", "formula": "=SUM(A1)", "cached_value": "2"}]}], warnings=[], source_digest=source, no_learning=True)
    with pytest.raises(BoardError, match="Select current leaf citations"):
        selected_view(evidence, ["table"])
    result = selected_view(evidence, ["cell"])
    assert result[0]["formula"] == "=SUM(A1)" and "unselected-container-canary" not in json.dumps(result)
    for refs in (["cell", "cell"], ["missing"], ["cell"] * 17):
        with pytest.raises(BoardError): selected_view(evidence, refs)
    oversize = evidence.model_copy(update={"sections": [evidence.sections[0].model_copy(update={"table_cells": [], "text": "界" * 6000})]})
    with pytest.raises(BoardError, match="complete private view exceeds"):
        selected_view(oversize, ["table"])
