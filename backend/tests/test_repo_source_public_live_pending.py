"""One genuine live Source/API Pending journey; no restart/readiness claim."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

import httpx
import pytest

from config.settings import settings
from src.api import work_board as work_board_api, workflows as workflows_api
from src.api.router import api_router as _registered_api_router
from src.app import create_app
from src.auth import service as auth_service
from src.execution import repo_original_producer as producer
from src.work_board.general_task import GeneralTaskService
from src.workflows import repo_repair_source as source
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_source_recovered_finalizer import _rows
from tests.test_repo_work_task_publication import _actual_source_callback_journey


def _physical(root):
    return {str(path.relative_to(root)): {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size} for path in sorted(root.rglob("*")) if path.is_file()}


async def _root_state(jobs, job_id):
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        return {"status": run.status, "revision": run.revision,
            "owner_principal_id": run.owner_principal_id, "operator_session_id": run.operator_session_id,
            "hold": deepcopy(jobs._repo_repair_reservation_state(run))}


@pytest.mark.asyncio
async def test_authenticated_live_original_producer_pending_preserves_root_and_physical_bytes(
        accounting_db, monkeypatch, repository_admission_signer, record_property):
    captured, physical_pending = {}, []
    reached, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    original_issue = producer.issue_original_producer_owner
    original_create, original_start = auth_service.create_session, GeneralTaskService.start
    original_stage = producer.stage_original_producer_completion

    async def capture_session(*args, **kwargs):
        token, operator = await original_create(*args, **kwargs)
        captured.update(token=token, operator=operator)
        return token, operator

    def capture_start(tasks):
        original_start(tasks)
        captured["tasks"] = tasks

    async def observe_issuer(service, jobs, job, *, register_ready, authorize_command):
        assert "producer_owner" not in captured
        def observe_ready(ready):
            ack = register_ready(ready)  # Original commit/readback/fence first.
            observation = producer.original_producer_observation(captured["producer_owner"], ready)
            admission = json.loads(observation.admission_json)
            started = time.monotonic()
            touch_interval = min(30.0, float(settings.operator_auth_idle_seconds) / 2)
            window = min(5.0, admission["deadline_at"] - started, touch_interval)
            assert window > 0, "No original deadline remains for live observation"
            captured.update(ready=ready, observation=observation, ack=ack,
                observation_started=started, observation_deadline=started + window)
            loop.call_soon_threadsafe(reached.set)
            assert release.wait(window), "Original bounded ACK observation expired"
            return ack  # Exact original object, never reconstructed authority.
        actual = await original_issue(service, jobs, job,
            register_ready=observe_ready, authorize_command=authorize_command)
        captured.update(producer_owner=actual, source=service, jobs=jobs, job=job)
        return actual

    @contextmanager
    def observe_physical_stage(registration, **kwargs):
        try:
            with original_stage(registration, **kwargs) as physical:
                yield physical
        except ValueError as exc:
            if str(exc) == "pending_original_producer":
                physical_pending.append(source._source_digest(registration))
            raise

    monkeypatch.setattr(auth_service, "create_session", capture_session)
    monkeypatch.setattr(GeneralTaskService, "start", capture_start)
    monkeypatch.setattr(producer, "issue_original_producer_owner", observe_issuer)
    monkeypatch.setattr(producer, "stage_original_producer_completion", observe_physical_stage)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "localhost,test,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    app = create_app()  # Original routed models were registered at collection.
    journey = asyncio.create_task(_actual_source_callback_journey(
        accounting_db, monkeypatch, False, "test_python"))
    waiting = asyncio.create_task(reached.wait())
    receipt_path = accounting_db[0].parent / "public-live-pending-receipt.json"
    receipt = {"case": "actual_live_original_producer", "state": "started"}

    def retain():
        receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
        receipt_path.chmod(0o600)

    try:
        done, _pending = await asyncio.wait({journey, waiting}, return_when=asyncio.FIRST_COMPLETED)
        if journey in done:
            await journey
            raise AssertionError("Original journey ended before registered live ACK")
        remaining = captured["observation_deadline"] - time.monotonic()
        assert remaining > 0
        async with asyncio.timeout(remaining):
            jobs, service, tasks = captured["jobs"], captured["source"], captured["tasks"]
            job, ready = captured["job"], captured["ready"]
            owner = captured["operator"]
            assert tasks.started and tasks.repository_source_service is service
            assert owner.principal.principal_id and owner.session_id
            assert settings.operator_auth_allow_unauthenticated_tests is False
            callback = service._iterative_process_callbacks[job.iteration_binding.iteration_id]
            captured["callback"] = callback
            assert not callback.done()
            assert service._iterative_process_jobs[job.iteration_binding.iteration_id] is job
            producer.assert_original_producer_ready(captured["producer_owner"], ready)
            state = await _root_state(jobs, job.job_id)
            assert state["status"] == "running" and state["hold"]["status"] == "held"
            assert (state["owner_principal_id"], state["operator_session_id"]) == (
                owner.principal.principal_id, owner.session_id)
            receipt.update(root_at_live_registration=state, original_pid=ready.pid,
                original_start_identity=ready.start_identity, original_guard_identity=list(ready.guard_identity),
                original_registration_digest=captured["ack"].registration_digest)
            retain()
            monkeypatch.setattr(workflows_api, "get_session", accounting_db[2].accounting_sessions)
            monkeypatch.setattr(workflows_api, "durable_job_repository", jobs)
            monkeypatch.setattr(work_board_api, "get_session", accounting_db[2].accounting_sessions)
            monkeypatch.setattr(work_board_api.dispatcher, "jobs", jobs)
            monkeypatch.setattr(work_board_api.dispatcher, "general_tasks", tasks)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                    base_url="http://localhost", headers={"Origin": "http://localhost:3001"}) as client:
                client.cookies.set(settings.operator_auth_cookie_name, captured["token"])
                url = f"/api/workflows/repo-repair/{job.job_id}"
                initial = await client.get(url)
                assert initial.status_code == 200, initial.text
                first = initial.json()
                receipt["initial_get"] = first
                assert await _root_state(jobs, job.job_id) == state
                assert first["status"] == "running" and first["revision"] == state["revision"]
                expected = {"state": "pending_original_producer", "reason": "pending_original_producer",
                    "physical_hold": True, "original_result": None, "public_actions": "reconcile_original_cleanup"}
                assert first["source_recovery"] == expected  # Live-map metadata.
                # Middleware's legitimate FIRST GET session touch is included
                # in this literal baseline; no clock patch or timestamp masks.
                before = await _rows(jobs)
                physical_before = _physical(accounting_db[0])
                receipt.update(raw_before=before, physical_before=physical_before)
                retain()
                pre_post = await _root_state(jobs, job.job_id)
                assert pre_post == state and pre_post["status"] == "running"
                assert not callback.done() and not physical_pending
                response = await client.post(url + "/source-recovery", json={
                    "expected_job_revision": pre_post["revision"], "action": "reconcile_original_cleanup"})
                after_post = await _root_state(jobs, job.job_id)
                receipt.update(root_after_post=after_post, action_status_code=response.status_code,
                    physical_pending_registration_digests=list(physical_pending))
                assert after_post == state and after_post["status"] == "running"
                assert response.status_code == 200, response.text
                post = response.json()
                receipt["action_post"] = post
                assert post["status"] == "running" and post["revision"] == state["revision"]
                assert post["source_recovery"] == expected
                assert len(physical_pending) == 1  # Actual guard/PID stage error.
                fresh = await client.get(url)
                assert fresh.status_code == 200, fresh.text
                current = fresh.json()
                receipt["fresh_get"] = current
                assert current["source_recovery"] == expected
                assert current["status"] == "running" and current["revision"] == state["revision"]
                assert current["no_learning"] is True and post["no_learning"] is True
                after = await _rows(jobs)
                physical_after = _physical(accounting_db[0])
                receipt.update(raw_after=after, physical_after=physical_after)
                retain()
                assert after == before
                assert physical_after == physical_before
                assert await _root_state(jobs, job.job_id) == state
                assert not callback.done()
                producer.assert_original_producer_ready(captured["producer_owner"], ready)
                assert time.monotonic() < captured["observation_deadline"]
                receipt.update(state="pending_observed", root=state,
                    original_pid=ready.pid, original_start_identity=ready.start_identity,
                    original_guard_identity=list(ready.guard_identity),
                    original_registration_digest=captured["ack"].registration_digest,
                    physical_pending_registration_digests=physical_pending,
                    initial_get=first, action_post=post, fresh_get=current,
                    raw_before=before, raw_after=after,
                    physical_before=physical_before, physical_after=physical_after,
                    observation_elapsed=time.monotonic() - captured["observation_started"])
                retain()
                record_property("live_pending_physical_stage_count", len(physical_pending))
                record_property("live_pending_original_pid", ready.pid)
                record_property("live_pending_receipt_sha256", hashlib.sha256(receipt_path.read_bytes()).hexdigest())
    finally:
        failing = sys.exc_info()[0] is not None
        release.set()  # The actual original ACK may now be forwarded.
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        try:
            await journey  # Original bounded completion/cleanup, no cancellation.
            callback = captured["callback"]
            assert callback.done() and not callback.cancelled()
            actual_result = callback.result()
            assert actual_result["original_producer_ready"] is captured["ready"]
            producer._assert_original_pid_absent(captured["ready"])
            receipt["original_journey_reaped"] = True
            receipt["original_result_after_release"] = actual_result["status"]
            retain()
            record_property("live_pending_original_journey_reaped", True)
        except BaseException as exc:
            receipt.update(original_journey_reaped=False, terminal_error_type=type(exc).__name__)
            retain()
            if not failing:
                raise
