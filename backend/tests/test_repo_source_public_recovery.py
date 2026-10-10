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
async def test_authenticated_sameboot_original_recovery_and_current_get(
        accounting_db, monkeypatch, repository_admission_signer, language, outcome):
    async with _authenticated_original_api(accounting_db, monkeypatch, language,
            failed=outcome != "success", stop_requested=outcome == "stop") as (flow, tasks, client):
        jobs, owner = flow["jobs"], flow["kwargs"]["owner"]
        job_id = flow["kwargs"]["job_id"]
        url = f"/api/workflows/repo-repair/{job_id}"
        response = await client.get(url)
        assert response.status_code == 200, response.text
        initial = response.json()
        assert initial["source_recovery"]["public_actions"] == "reconcile_original_cleanup"
        revision = initial["revision"]
        before = await _rows(jobs)

        # Caller metadata is not authority and cannot introduce a host-boot action.
        with pytest.raises(ValidationError):
            await workflows_api._repo_repair_public_recovery_projection(
                tasks, flow["service"], jobs, job_id=job_id, owner=owner,
                projection={**initial, "source_recovery": {
                    **initial["source_recovery"], "public_actions": "settle_original_host_boot_cleanup"}})

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
        assert response.status_code == 200, response.text
        readback = await client.get(url)
        assert readback.status_code == 200, readback.text
        current = readback.json()
        assert current["job_id"] == job_id and current["no_learning"] is True
        assert current["revision"] > revision
        packet = recovery.RepositorySourceRecoveryProjection.model_validate(current["source_recovery"])
        assert packet.original_result == ("succeeded" if outcome == "success" else "failed")
        assert packet.public_actions == "unavailable"
        if outcome == "success":
            assert current["status"] == "succeeded"
            assert packet.physical_hold is False
        elif outcome == "stop":
            assert current["status"] == "cancelled"
            assert packet.state == "original_stop_committed"
            assert packet.physical_hold is False
        else:
            assert current["status"] == "running"
            assert packet.state == "continuation_ready"
        assert await _rows(jobs) != foreign_before
        # A stale repeat cannot replay or charge the original execution again.
        committed = await _rows(jobs)
        stale = await client.post(url + "/source-recovery", json={
            "expected_job_revision": revision, "action": "reconcile_original_cleanup"})
        assert stale.status_code == 409, stale.text
        assert await _rows(jobs) == committed
        ineligible = await client.post(url + "/source-recovery", json={
            "expected_job_revision": current["revision"], "action": "reconcile_original_cleanup"})
        assert ineligible.status_code == 409, ineligible.text
        assert await _rows(jobs) == committed



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
        assert result.status_code == 200, result.text
        readback = await client.get(url)
        assert readback.status_code == 200, readback.text
        assert readback.json()["revision"] > initial.json()["revision"]
        packet = readback.json()["source_recovery"]
        assert readback.json()["status"] == "unknown_external_effect"
        assert packet == {"state": "original_cleanup_committed", "reason": "original_stop_pending",
            "physical_hold": True, "original_result": "succeeded", "public_actions": "unavailable"}


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
async def test_public_sameboot_adapter_dispatches_once_outside_wrapper_fence(monkeypatch):
    """Call/fence wiring only; genuine authority comes from the API journeys."""
    service, jobs, owner = object(), object(), object()
    calls = []
    async def original(*args, **kwargs):
        calls.append((args, kwargs))
        return {"original": "readback"}
    @asynccontextmanager
    async def forbidden_fence(*args, **kwargs):
        raise AssertionError("Same-boot dispatch must not acquire the wrapper fence")
        yield
    monkeypatch.setattr(recovery, "_reconcile_original_repository_cleanup", original)
    monkeypatch.setattr(recovery, "_repository_recovery_fence", forbidden_fence)
    result = await recovery.recover_original_repository_cleanup(service, jobs,
        job_id="original", owner=owner, expected_job_revision=17, action="reconcile_original_cleanup")
    assert result == {"original": "readback"}
    assert calls == [((service, jobs), {"job_id": "original", "owner": owner, "expected_job_revision": 17, "_public_action": True})]


@pytest.mark.asyncio
@pytest.mark.parametrize("revision,action", [(True, "reconcile_original_cleanup"), (-1, "reconcile_original_cleanup"), (0, "available")])
async def test_public_invalid_request_never_dispatches(monkeypatch, revision, action):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Malformed request dispatched")
    monkeypatch.setattr(recovery, "_reconcile_original_repository_cleanup", forbidden)
    with pytest.raises(recovery.RepositorySourceRecoveryError, match="request_invalid"):
        await recovery.recover_original_repository_cleanup(object(), object(), job_id="original",
            owner=object(), expected_job_revision=revision, action=action)


@pytest.mark.parametrize("action", [True, False, "available", "enabled", "settle_original_host_boot_cleanup"])
def test_public_response_action_rejects_noncanonical_metadata(action):
    with pytest.raises(ValidationError):
        recovery.RepositorySourceRecoveryProjection.model_validate({
            "state": "held_unknown", "reason": "original_completion_unproven",
            "physical_hold": None, "original_result": None, "public_actions": action})


@pytest.mark.asyncio
async def test_public_v3_original_post_is_unavailable_without_mutation(
        accounting_db, monkeypatch, repository_admission_signer):
    from src.workflows import repo_repair_source as source
    append = source._append_repository_record
    def select_legacy(run, identity, payload, **kwargs):
        if identity == 'repository:inventory:v1':
            assert source._repository_record(run, identity) is None
            payload = {**payload, 'schema': 'repository.checkpoint_inventory.v3'}
        return append(run, identity, payload, **kwargs)
    monkeypatch.setattr(source, '_append_repository_record', select_legacy)
    async with _authenticated_original_api(accounting_db, monkeypatch, 'test_python') as (flow, _, client):
        url = '/api/workflows/repo-repair/' + flow['kwargs']['job_id']
        get = await client.get(url)
        assert get.status_code == 200, get.text
        assert get.json()['source_recovery']['public_actions'] == 'unavailable'
        before = await _rows(flow['jobs'])
        post = await client.post(url + '/source-recovery', json={
            'expected_job_revision': get.json()['revision'], 'action': 'reconcile_original_cleanup'})
        assert post.status_code == 503, post.text
        assert await _rows(flow['jobs']) == before


@pytest.mark.asyncio
async def test_public_committed_original_stop_retry_uses_original_gate(
        accounting_db, monkeypatch, repository_admission_signer):
    async with _authenticated_original_api(accounting_db, monkeypatch, 'test_python',
            failed=True, stop_requested=True) as (flow, _, client):
        jobs, job_id, owner = flow['jobs'], flow['kwargs']['job_id'], flow['kwargs']['owner']
        async with jobs._session() as db:
            revision = (await jobs._fetch(db, job_id)).revision
        # Genuine publication commits cleanup but deliberately does not consume
        # the original Stop witness: this is the authentic restart/retry gap.
        async with recovery.stage_original_repository_completion_publication(flow['service'], jobs,
                job_id=job_id, owner=owner, iteration_index=1, expected_job_revision=revision) as witness:
            assert recovery.repository_completion_outcome(witness)['status'] == 'failed'
        url = '/api/workflows/repo-repair/' + job_id
        get = await client.get(url)
        assert get.status_code == 200, get.text
        assert get.json()['status'] == 'running'
        assert get.json()['source_recovery']['public_actions'] == 'reconcile_original_cleanup'
        post = await client.post(url + '/source-recovery', json={
            'expected_job_revision': get.json()['revision'], 'action': 'reconcile_original_cleanup'})
        assert post.status_code == 200, post.text
        current = await client.get(url)
        assert current.status_code == 200, current.text
        assert current.json()['status'] == 'cancelled'
        assert current.json()['source_recovery']['physical_hold'] is False
        assert current.json()['source_recovery']['public_actions'] == 'unavailable'


@pytest.mark.asyncio
async def test_same_request_automatic_stop_followup_is_not_a_new_public_entry(monkeypatch):
    """Call-boundary regression only; no mock is a physical/SQL/Stop witness."""
    from types import SimpleNamespace
    from src.workflows import repo_repair_stop as stop
    run = SimpleNamespace(owner_principal_id='owner', operator_session_id='session',
        revision=7, status='running')
    owner = SimpleNamespace(principal_id='owner', session_id='session')
    work = SimpleNamespace(limits=SimpleNamespace(max_iterations=3))
    source = SimpleNamespace(_repository_record=lambda *a: None,
        read_repository_original=lambda r: (None, work))
    calls = []
    @asynccontextmanager
    async def session():
        yield object()
    async def fetch(db, job_id):
        return run
    jobs = SimpleNamespace(_session=session, _fetch=fetch)
    @asynccontextmanager
    async def publication(*args, **kwargs):
        calls.append(('publication', kwargs))
        yield object()
    async def automatic(*args, **kwargs):
        return 'deadline_exhausted'
    async def original_stop(*args, **kwargs):
        calls.append(('stop', kwargs))
    async def projection(*args, **kwargs):
        return {'boundary_test_only': True}
    source.repository_operator_projection = projection
    monkeypatch.setattr(recovery, '_source', lambda: source)
    monkeypatch.setattr(recovery, '_v4_root', lambda r: True)
    monkeypatch.setattr(recovery, '_latest_original_repository_registration',
        lambda r: {'iteration_index': 1, 'iteration_id': 'identity'})
    monkeypatch.setattr(recovery, 'repository_source_recovery_projection',
        lambda *a: {'public_actions': 'reconcile_original_cleanup'})
    monkeypatch.setattr(recovery, 'stage_original_repository_completion_publication', publication)
    monkeypatch.setattr(recovery, 'repository_completion_outcome', lambda w: {'status': 'succeeded'})
    monkeypatch.setattr(recovery, 'repository_completion_context', lambda w: {'work': work})
    monkeypatch.setattr(stop, 'repository_automatic_limit_reason', automatic)
    monkeypatch.setattr(recovery, '_reconcile_original_repository_stop', original_stop)
    await recovery._reconcile_original_repository_cleanup(object(), jobs, job_id='root',
        owner=owner, expected_job_revision=7, _public_action=True)
    assert calls[0][1]['_public_action'] is True
    assert calls[1] == ('stop', {'job_id': 'root', 'owner': owner,
        'expected_job_revision': 7, 'reason': 'deadline_exhausted'})


@pytest.mark.asyncio
async def test_original_writer_unknown_without_stop_is_readback_only(
        accounting_db, monkeypatch, repository_admission_signer):
    from hashlib import sha256
    from pathlib import Path
    from src.workflows import repo_repair_source as source
    async with _authenticated_original_api(accounting_db, monkeypatch, 'test_python') as (flow, _, client):
        jobs, job_id, owner = flow['jobs'], flow['kwargs']['job_id'], flow['kwargs']['owner']
        async with jobs._session() as db:
            root = await jobs._fetch(db, job_id)
            assert source._repository_record(root, 'repository:stop-intent:v1') is None
            lease_owner, fence = root.lease_owner, root.fencing_token
        # Actual original quarantine and CAS writer, not assigned SQL status or
        # a fabricated uncertainty witness. Absent Stop selects witness=None.
        await source._quarantine_original_uncertainty(flow['service'], jobs, job_id=job_id,
            owner=owner, lease_owner=lease_owner, fencing_token=fence,
            reason='repository_process_closure_unproven', result={'no_learning': True,
                'operator_action': 'reconcile_original_process',
                'iteration_id': flow['registration']['iteration_id']})
        async with jobs._session() as db:
            root = await jobs._fetch(db, job_id)
            assert root.status == 'unknown_external_effect'
            assert source._repository_record(root, 'repository:stop-intent:v1') is None
            assert source._repository_record(root, 'repository:stop-uncertainty-successor:v1') is None
            revision = root.revision
        url = '/api/workflows/repo-repair/' + job_id
        get = await client.get(url)
        assert get.status_code == 200, get.text
        assert get.json()['source_recovery']['public_actions'] == 'unavailable'
        before = await _rows(jobs)
        directory = Path(flow['registration']['directory_path'])
        def physical_snapshot():
            members = sorted(directory.iterdir())
            assert len(members) <= 24
            snapshot = {}
            for member in members:
                assert not member.is_symlink() and member.is_file()
                stat = member.stat()
                snapshot[member.name] = (stat.st_dev, stat.st_ino, stat.st_mode,
                    stat.st_uid, stat.st_nlink, sha256(member.read_bytes()).hexdigest())
            return snapshot
        physical_before = physical_snapshot()
        private = await recovery._reconcile_original_repository_cleanup(flow['service'], jobs,
            job_id=job_id, owner=owner, expected_job_revision=revision)
        assert private['status'] == 'unknown_external_effect'
        assert private['source_recovery']['physical_hold'] is True
        assert await _rows(jobs) == before
        assert physical_snapshot() == physical_before
        post = await client.post(url + '/source-recovery', json={
            'expected_job_revision': revision, 'action': 'reconcile_original_cleanup'})
        assert post.status_code == 409, post.text
        assert post.json()['detail']['code'] == 'repository_source_recovery_not_eligible'
        assert await _rows(jobs) == before
        assert physical_snapshot() == physical_before
