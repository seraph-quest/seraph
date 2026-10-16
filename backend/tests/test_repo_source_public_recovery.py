"""Authenticated original-producer API journeys; no managed restart claim."""
from contextlib import asynccontextmanager
import json

import httpx
import pytest
from pydantic import ValidationError

from config.settings import settings
from src.api import work_board as work_board_api
from src.api import workflows as workflows_api
# Managed create_app registers these routes/models before lifespan init_db.
# Match that order before accounting_db materializes SQLModel's full inventory.
from src.api.router import api_router as _registered_api_router
from src.app import create_app
from src.auth import service as auth_service
from src.work_board.general_task import GeneralTaskService
from src.workflows import repo_repair_source_recovery as recovery
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_source_recovered_finalizer import _signed_ownerless_fixture, _rows
from tests.test_repo_source_stop_knownpost_candidate import _retain_original_node_runtime


@asynccontextmanager
async def _authenticated_original_api(accounting_db, monkeypatch, language, *, failed=False,
                                      unknown=False, stop_requested=False):
    captured = {}
    create_session, start = auth_service.create_session, GeneralTaskService.start

    async def capture_session(*args, **kwargs):
        token, operator = await create_session(*args, **kwargs)
        captured.update(token=token, operator=operator)
        return token, operator

    def capture_start(tasks):
        start(tasks)
        captured["tasks"] = tasks

    # Retain the original fixture's actual auth and server-selected runtime
    # settings beyond its inner monkeypatch scope; no authentication bypass.
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "planner-disposable-password")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    _retain_original_node_runtime(monkeypatch, language)
    monkeypatch.setattr(auth_service, "create_session", capture_session)
    monkeypatch.setattr(GeneralTaskService, "start", capture_start)
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, language,
        failed=failed, unknown=unknown, stop_requested=stop_requested)
    original_tasks = captured["tasks"]
    original_tasks.stop()
    tasks = GeneralTaskService(original_tasks.registry,
        repository_source_service=flow["service"])
    tasks.start()
    owner = flow["kwargs"]["owner"]
    assert captured["operator"].principal.principal_id == owner.principal_id
    assert captured["operator"].session_id == owner.session_id
    factory, jobs = accounting_db[2], flow["jobs"]
    monkeypatch.setattr(workflows_api, "get_session", factory.accounting_sessions)
    monkeypatch.setattr(workflows_api, "durable_job_repository", jobs)
    monkeypatch.setattr(work_board_api, "get_session", factory.accounting_sessions)
    monkeypatch.setattr(work_board_api.dispatcher, "jobs", jobs)
    monkeypatch.setattr(work_board_api.dispatcher, "general_tasks", tasks)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "localhost,test,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()),
                base_url="http://localhost", headers={"Origin": "http://localhost:3001"}) as client:
            client.cookies.set(settings.operator_auth_cookie_name, captured["token"])
            yield flow, tasks, client
    finally:
        tasks.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
@pytest.mark.parametrize("outcome", ["success", "failure", "stop"])
async def test_authenticated_sameboot_original_recovery_unavailable_and_current_get(
        accounting_db, monkeypatch, repository_admission_signer, language, outcome):
    async with _authenticated_original_api(accounting_db, monkeypatch, language,
            failed=outcome != "success", stop_requested=outcome == "stop") as (flow, tasks, client):
        jobs, owner = flow["jobs"], flow["kwargs"]["owner"]
        job_id = flow["kwargs"]["job_id"]
        url = f"/api/workflows/repo-repair/{job_id}"
        response = await client.get(url)
        assert response.status_code == 200, response.text
        initial = response.json()
        assert initial["source_recovery"]["public_actions"] == "unavailable"
        revision = initial["revision"]
        before = await _rows(jobs)

        async def forbidden_recovery(*args, **kwargs):
            raise AssertionError("Public recovery must not enter the original mutation helper")

        monkeypatch.setattr(recovery, "_reconcile_original_repository_cleanup", forbidden_recovery)

        # Even a non-null internal DTO carrying an actionable string cannot
        # enable the public surface before ADR-030 acceptance.
        with pytest.raises(ValidationError):
            await workflows_api._repo_repair_public_recovery_projection(
                tasks, flow["service"], jobs, job_id=job_id, owner=owner,
                projection={**initial, "source_recovery": {
                    **initial["source_recovery"], "public_actions": "reconcile_original_cleanup"}})

        # Diagnostic metadata must be current and is never an execution grant.
        with pytest.raises(recovery.RepositorySourceRecoveryError, match="stale"):
            await workflows_api._repo_repair_public_recovery_projection(tasks, flow["service"], jobs,
                job_id=job_id, owner=owner, projection={**initial, "revision": revision + 1})
        assert await _rows(jobs) == before
        tasks.stop()
        inactive = await client.get(url)
        assert inactive.status_code == 503
        assert inactive.json()["detail"]["code"] == "general_task_inactive"
        tasks.start()
        assert await _rows(jobs) == before

        for body, expected in (
            ({"expected_job_revision": True, "action": "reconcile_original_cleanup"}, 422),
            ({"expected_job_revision": revision, "action": "available"}, 422),
            ({"expected_job_revision": revision + 1, "action": "reconcile_original_cleanup"}, 409),
            ({"expected_job_revision": revision, "action": "settle_original_host_boot_cleanup"}, 503),
        ):
            denied = await client.post(url + "/source-recovery", json=body)
            assert denied.status_code == expected, denied.text
            assert await _rows(jobs) == before

        # Another genuine authenticated session cannot select this original
        # owner. Creating that session is separate from the recovery request.
        original_token = client.cookies.get(settings.operator_auth_cookie_name)
        foreign_token, foreign_operator = await auth_service.create_session()
        assert foreign_operator.principal.principal_id != owner.principal_id
        client.cookies.set(settings.operator_auth_cookie_name, foreign_token)
        foreign_before = await _rows(jobs)
        foreign = await client.post(url + "/source-recovery", json={
            "expected_job_revision": revision, "action": "reconcile_original_cleanup"})
        assert foreign.status_code == 403, foreign.text
        assert foreign.json()["detail"]["code"] == "repo_repair_owner_mismatch"
        assert await _rows(jobs) == foreign_before
        client.cookies.set(settings.operator_auth_cookie_name, original_token)

        response = await client.post(url + "/source-recovery", json={
            "expected_job_revision": revision, "action": "reconcile_original_cleanup"})
        assert response.status_code == 503, response.text
        assert response.json()["detail"] == {
            "code": "repository_source_recovery_unavailable", "operator_visible": True, "no_learning": True}
        assert await _rows(jobs) == foreign_before
        readback = await client.get(url)
        assert readback.status_code == 200, readback.text
        current = readback.json()
        assert current["job_id"] == job_id and current["no_learning"] is True
        assert current == initial
        packet = recovery.RepositorySourceRecoveryProjection.model_validate(current["source_recovery"])
        assert packet.public_actions == "unavailable"
        assert await _rows(jobs) == foreign_before


@pytest.mark.asyncio
async def test_authenticated_unknown_original_recovery_stays_held(
        accounting_db, monkeypatch, repository_admission_signer):
    async with _authenticated_original_api(accounting_db, monkeypatch, "test_python",
            unknown=True) as (flow, _tasks, client):
        job_id = flow["kwargs"]["job_id"]
        url = f"/api/workflows/repo-repair/{job_id}"
        initial = await client.get(url)
        assert initial.status_code == 200, initial.text
        before = await _rows(flow["jobs"])
        result = await client.post(url + "/source-recovery", json={
            "expected_job_revision": initial.json()["revision"], "action": "reconcile_original_cleanup"})
        assert result.status_code == 503, result.text
        assert result.json()["detail"]["code"] == "repository_source_recovery_unavailable"
        assert await _rows(flow["jobs"]) == before
        readback = await client.get(url)
        assert readback.status_code == 200, readback.text
        assert readback.json() == initial.json()
        packet = readback.json()["source_recovery"]
        assert readback.json()["status"] == "unknown_external_effect"
        assert packet == {"state": "held_unknown", "reason": "original_stop_pending",
            "physical_hold": True, "original_result": None, "public_actions": "unavailable"}


@pytest.mark.asyncio
async def test_authenticated_missing_original_registration_never_advertises_or_recovers(
        accounting_db, monkeypatch, repository_admission_signer):
    async with _authenticated_original_api(accounting_db, monkeypatch, "test_python") as (flow, _tasks, client):
        jobs, job_id = flow["jobs"], flow["kwargs"]["job_id"]
        # Negative corruption of the actual durable record, never a positive
        # fixture or replacement producer/authority. Preserve all other bytes.
        async with jobs._session() as db:
            root = await jobs._fetch(db, job_id)
            history = json.loads(root.checkpoint_receipts_json)
            checkpoint_id = "repository:producer:" + flow["registration"]["iteration_id"]
            assert sum(item["checkpoint_id"] == checkpoint_id for item in history) == 1
            root.checkpoint_receipts_json = json.dumps([
                item for item in history if item["checkpoint_id"] != checkpoint_id])
            db.add(root)
            revision = root.revision
        before = await _rows(jobs)
        url = f"/api/workflows/repo-repair/{job_id}"
        get = await client.get(url)
        assert get.status_code == 409, get.text
        assert "source_recovery" not in get.json()
        post = await client.post(url + "/source-recovery", json={
            "expected_job_revision": revision, "action": "reconcile_original_cleanup"})
        assert post.status_code == 409, post.text
        assert await _rows(jobs) == before


@pytest.mark.asyncio
async def test_public_sameboot_adapter_validates_original_but_never_dispatches(monkeypatch):
    """Boundary observation only; genuine authority comes from the API tests."""
    service, jobs, owner = object(), object(), object()
    observed = []

    async def adapter(*args, **kwargs):
        raise AssertionError("Public adapter must not dispatch the private candidate")

    fence = object()
    original_run = object()
    fence_active = False

    @asynccontextmanager
    async def wrapper_fence(actual_service, actual_jobs, **kwargs):
        nonlocal fence_active
        observed.append((actual_service, actual_jobs, kwargs))
        fence_active = True
        try:
            yield fence
        finally:
            fence_active = False

    async def load(actual_service, actual_jobs, **kwargs):
        assert fence_active and kwargs["fence"] is fence
        observed.append((actual_service, actual_jobs, kwargs))
        return {"run": original_run}

    def projection(actual_service, actual_jobs, run):
        assert fence_active and run is original_run
        assert actual_service is service and actual_jobs is jobs
        observed.append((actual_service, actual_jobs, run))
        return recovery.RepositorySourceRecoveryProjection(
            state="held_unknown", reason="original_completion_unproven",
            physical_hold=None, original_result=None, public_actions="unavailable")

    monkeypatch.setattr(recovery, "_reconcile_original_repository_cleanup", adapter)
    monkeypatch.setattr(recovery, "_repository_recovery_fence", wrapper_fence)
    monkeypatch.setattr(recovery, "_load_recovery_original", load)
    monkeypatch.setattr(recovery, "repository_source_recovery_projection", projection)
    with pytest.raises(recovery.RepositorySourceRecoveryError) as denied:
        await recovery.recover_original_repository_cleanup(service, jobs,
            job_id="original", owner=owner, expected_job_revision=17, action="reconcile_original_cleanup")
    assert denied.value.status_code == 503
    assert denied.value.code == "repository_source_recovery_unavailable"
    assert observed == [(service, jobs, {"job_id": "original", "owner": owner,
        }), (service, jobs, {"job_id": "original", "owner": owner,
        "expected_job_revision": 17, "fence": fence}),
        (service, jobs, original_run)]
    assert fence_active is False


@pytest.mark.parametrize("action", [True, False, "available", "enabled", "reconcile_original_cleanup", "settle_original_host_boot_cleanup"])
def test_public_response_action_rejects_noncanonical_metadata(action):
    with pytest.raises(ValidationError):
        recovery.RepositorySourceRecoveryProjection.model_validate({
            "state": "held_unknown", "reason": "original_completion_unproven",
            "physical_hold": None, "original_result": None, "public_actions": action})
