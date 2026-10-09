"""Actual original stop metadata, authenticated discovery, and no-private-read proof."""
import json

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from src.auth.service import authenticate_session
from src.db.models import InferenceCostReservation, WorkBoardTask, WorkBoardAttempt
from src.work_board.contracts import RepositoryReview
from src.workflows.job_runtime import DurableJobLeaseError, _digest
from src.workflows.repo_repair_source import (prepare_repository_native_source,
    repository_operator_projection, repository_review_projection, read_repository_original)
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_repo_work_task_publication import actual_native_source


NORMAL_KEYS = {"job_id", "status", "revision", "repository_review", "patch_proposal", "approval",
    "iterations", "iteration_states", "recovery_action", "provider_contacted", "no_learning", "operator_visible"}


def test_review_discriminant_is_closed_and_contains_no_preparation():
    value = {"native_child_id": "native:" + "a" * 64, "repository_job_id": "repository:" + "b" * 64,
        "iteration_index": None, "iteration_id": None, "preparation_digest": None,
        "source_preview_path": None, "contact_state": "not_prepared"}
    assert RepositoryReview.model_validate(value).model_dump(mode="json") == value
    for field, wrong in (("iteration_index", 1), ("iteration_id", "c" * 64),
            ("preparation_digest", "d" * 64), ("source_preview_path", "/fake"), ("authority", True)):
        with pytest.raises(ValidationError):
            RepositoryReview.model_validate({**value, field: wrong})
    with pytest.raises(ValidationError):
        RepositoryReview.model_validate({**value, "contact_state": "not_started"})


async def stopped_original(accounting_db, monkeypatch, *, pending=False, prepared=False):
    from src.workflows import repo_repair_source as source_module
    factory, owner, service, jobs, binding, _ = await actual_native_source(accounting_db, monkeypatch,
        goal_capacity=2, claim_child=False,
        work_limits=None if prepared else {"max_iterations": 3, "max_total_seconds": 900, "max_cost_usd": 0.0})
    operator = await authenticate_session(owner.session_id, touch=False)
    original_validator = source_module.validate_repository_stop_witness
    async def rollback_terminal(*args, **kwargs):
        await original_validator(*args, **kwargs)
        raise DurableJobLeaseError("disposable actual terminal rollback")
    if pending:
        monkeypatch.setattr(source_module, "validate_repository_stop_witness", rollback_terminal)
    outcome = await prepare_repository_native_source(service, jobs, binding,
        child_owner="actual-stop-metadata-worker", principal=operator.principal)
    if pending:
        monkeypatch.setattr(source_module, "validate_repository_stop_witness", original_validator)
    return factory, owner, service.repository_source_service, jobs, binding, outcome["repository_job_id"], service


def forbid_private_metadata_reads(monkeypatch, source):
    def forbidden(*args, **kwargs):
        raise AssertionError("Discovery metadata may read no private body, filesystem, Vault or policy")
    from src.workflows import repo_repair_source as source_module
    monkeypatch.setattr(source, "_read_private_artifact", forbidden)
    monkeypatch.setattr(source, "_write_private_artifact", forbidden)
    monkeypatch.setattr(source_module, "_repository_policy_limits", forbidden)
    monkeypatch.setattr(source.sandbox, "_read_job_marker", forbidden)


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_actual_no_preparation_stop_has_same_source_and_task_review(accounting_db, monkeypatch, repository_admission_signer, pending):
    factory, owner, source, jobs, binding, root_id, service = await stopped_original(
        accounting_db, monkeypatch, pending=pending)
    forbid_private_metadata_reads(monkeypatch, source)
    status = await repository_operator_projection(source, jobs, job_id=root_id, owner=owner)
    assert set(status) == NORMAL_KEYS | {"repository_stop"}
    stop = status["repository_stop"]
    assert set(stop) == {"reason", "pending", "limit_evidence", "limit_evidence_digest"}
    assert stop["pending"] is pending and stop["reason"] == "cost_exhausted"
    assert status["status"] == ("running" if pending else "failed")
    assert status["recovery_action"] == ("repository_stop_pending" if pending else "original_cost_exhausted")
    assert status["provider_contacted"] is False and status["patch_proposal"] is None
    review = status["repository_review"]
    assert len(review) == 7 and review["contact_state"] == "not_prepared"
    assert all(review[field] is None for field in ("iteration_index", "iteration_id", "preparation_digest", "source_preview_path"))
    async with factory() as db:
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
        assert await repository_review_projection(db, task=task, attempt=attempt, owner=owner) == review
        root = await jobs._fetch(db, root_id)
        assert jobs._repo_repair_reservation_state(root)["status"] == ("held" if pending else "released")
        assert list((await db.scalars(select(InferenceCostReservation))).all()) == []
    assert (root_id in source._iterative_lanes) is pending
    path = source._workspace() / "actual-stop-metadata-capture.json"
    # Persist literal metadata proof after removing the read trap; not a grant.
    path.write_text(json.dumps({"source": status, "task_review": review}, sort_keys=True))


@pytest.mark.asyncio
async def test_actual_prepared_without_stop_retains_normal_twelve_keys(accounting_db, monkeypatch, repository_admission_signer):
    factory, owner, source, jobs, binding, root_id, service = await stopped_original(accounting_db, monkeypatch, prepared=True)
    forbid_private_metadata_reads(monkeypatch, source)
    status = await repository_operator_projection(source, jobs, job_id=root_id, owner=owner)
    assert set(status) == NORMAL_KEYS and status["repository_review"]["contact_state"] == "not_started"
    assert status["repository_review"]["iteration_index"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "terminal", "prepared"])
async def test_actual_authenticated_stop_and_task_discovery_capture(accounting_db, monkeypatch, repository_admission_signer, state):
    import httpx
    from config.settings import settings
    from src.api import workflows as workflows_api, work_board as board_api
    from src.app import create_app
    from src.auth import service as auth_service
    credentials = {}
    create_session = auth_service.create_session
    async def capture_session(*args, **kwargs):
        token, operator = await create_session(*args, **kwargs)
        credentials.update(token=token, operator=operator)
        return token, operator
    monkeypatch.setattr(auth_service, "create_session", capture_session)
    factory, owner, source, jobs, binding, root_id, service = await stopped_original(accounting_db, monkeypatch,
        pending=state == "pending", prepared=state == "prepared")
    # Existing singleton owns the exact fixture Source and durable repository.
    monkeypatch.setattr(workflows_api, "get_session", factory.accounting_sessions)
    monkeypatch.setattr(workflows_api, "durable_job_repository", jobs)
    monkeypatch.setattr(board_api, "get_session", factory.accounting_sessions)
    monkeypatch.setattr(board_api.dispatcher, "general_tasks", service)
    monkeypatch.setattr(board_api.dispatcher, "jobs", jobs)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "localhost,test,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    forbid_private_metadata_reads(monkeypatch, source)
    captures = []
    prefix = "/api/workflows/repo-repair/" + root_id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),
            base_url="http://localhost", headers={"Origin": "http://localhost:3001"}) as client:
        async def get(path, expected):
            response = await client.get(path)
            captures.append({"method": "GET", "path": path, "status": response.status_code,
                "response": response.json()})
            assert response.status_code == expected, response.text
            return response.json()
        await get(prefix, 401)
        client.cookies.set(settings.operator_auth_cookie_name, credentials["token"])
        status = await get(prefix, 200)
        task = await get("/api/work-board/tasks/" + binding.task_id, 200)
        assert task["task"]["repository_review"] == status["repository_review"]
        assert set(status) == NORMAL_KEYS | ({"repository_stop"} if state != "prepared" else set())
        if state != "prepared":
            assert status["repository_stop"]["pending"] is (state == "pending")
        foreign_token, _ = await create_session()
        client.cookies.set(settings.operator_auth_cookie_name, foreign_token)
        await get(prefix, 403)
    (source._workspace() / ("actual-auth-stop-" + state + ".json")).write_text(json.dumps({
        "owner": {"principal_id": owner.principal_id, "session_id": owner.session_id}, "captures": captures}, sort_keys=True))


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["wrong_owner", "stop_missing", "stop_binding", "stop_unknown_field",
    "evidence", "inventory", "reservation", "terminal", "preparation_without_tuple"])
async def test_actual_stop_metadata_corruption_fails_closed(accounting_db, monkeypatch, repository_admission_signer, drift):
    factory, owner, source, jobs, binding, root_id, service = await stopped_original(accounting_db, monkeypatch)
    if drift != "wrong_owner":
        async with factory() as db:
            root = await jobs._fetch(db, root_id)
            journal = json.loads(root.checkpoint_receipts_json)
            stop = next(item for item in journal if item["checkpoint_id"] == "repository:stop-intent:v1")
            if drift == "stop_missing":
                journal.remove(stop)
            elif drift == "stop_binding":
                stop["payload"]["native_binding_digest"] = "0" * 64
            elif drift == "stop_unknown_field":
                stop["payload"]["caller_authority"] = True
            elif drift == "evidence":
                stop["payload"]["limit_evidence"]["original_server_bound_microusd"] = 1
            elif drift == "inventory":
                item = next(item for item in journal if item["checkpoint_id"] == "repository:inventory:v1")
                del item["payload"]["original_limits"]
                item["state_digest"] = _digest(item["payload"])
            elif drift == "reservation":
                item = next(item for item in journal if item["checkpoint_id"] == "repo-repair-execution-release")
                item["payload"]["attempt_id"] = "foreign"
                item["state_digest"] = _digest(item["payload"])
            elif drift == "terminal":
                item = next(item for item in journal if item["checkpoint_id"] == "repository:terminal:v1")
                item["payload"]["closure"]["source_checkpoint_digest"] = "0" * 64
                item["state_digest"] = _digest(item["payload"])
            else:
                original = read_repository_original(root)[0]
                from src.workflows.repo_repair_source import iteration_identity, _source_digest
                identity = iteration_identity(root_id, original["repository_attempt_id"], _source_digest(original["original_input"]), 1)
                payload = {"schema": "caller.fake"}
                journal.append({"checkpoint_id": "repository:prepared:" + identity, "safe": True,
                    "payload": payload, "state_digest": _digest(payload)})
            stop["state_digest"] = _digest(stop["payload"])
            root.checkpoint_receipts_json = json.dumps(journal)
            await db.commit()
    else:
        from src.work_board.contracts import WorkBoardOwner
        owner = WorkBoardOwner(principal_id="operator:foreign", session_id=owner.session_id)
    forbid_private_metadata_reads(monkeypatch, source)
    with pytest.raises(DurableJobLeaseError):
        await repository_operator_projection(source, jobs, job_id=root_id, owner=owner)
