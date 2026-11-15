"""Actual canonical publication process loss, with explicit test boundaries.

Provider/HTTP transport is intercepted. The approved repair, Root, job,
connection, producer/helper/Git processes, SQLite and marker bytes are real.
Only the admitted lease is shortened and a child-start notification paused.
No observer supplies terminal or quiescence proof.
"""
from contextlib import asynccontextmanager
import asyncio
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time
import uuid
from unittest.mock import patch

import httpx
import pytest

from src.execution import repo_publication_supervisor as supervisor
from src.execution.repo_supervisor import start_identity, exact_signal
from src.workflows.job_runtime import durable_job_repository as jobs
from tests.test_repo_publication_vertical import actual_repair, selected_connection, request_for, ORIGIN
from tests.repo_publication_support import GitDataTransport


def _canonical_request_process(database_url, job_id, principal, root, observed_path, continue_path, result_path):
    """Fresh child SQLite pool; production publisher and independently owned helper."""
    asyncio.events._set_running_loop(None)
    asyncio.set_event_loop(None)
    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from src.db.engine import _configure_sqlite_connection
    from tests.conftest import _PATCH_TARGETS
    from src.workflows.repo_publication import RepoPublicationService

    async def run():
        physical = create_async_engine(database_url, connect_args={"check_same_thread": False},
            pool_size=4, max_overflow=0, pool_timeout=3)
        event.listen(physical.sync_engine, "connect", _configure_sqlite_connection)
        factory = sessionmaker(physical, class_=AsyncSession, expire_on_commit=False)
        @asynccontextmanager
        async def session():
            async with factory() as db:
                try:
                    yield db
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise
        patches = [patch(target, session) for target in _PATCH_TARGETS]
        for item in patches: item.start()
        original_claim = jobs.claim_job
        async def shortened_claim(*args, **kwargs):
            kwargs["lease_seconds"] = min(10, kwargs.get("lease_seconds", 10))
            return await original_claim(*args, **kwargs)
        original_run = supervisor.run
        count = 0
        def observe(message):
            nonlocal count
            count += 1
            if count == 3:
                raw = json.dumps({"parent_pid": os.getpid(), "child_started": message,
                    "boundary": "pause notification only; actual child/helper own pipes and guard"}).encode()
                descriptor = os.open(observed_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    output.write(raw); output.flush(); os.fsync(output.fileno())
                until = time.monotonic()+6
                while time.monotonic() < until and not Path(continue_path).exists():
                    time.sleep(.01)
        def observed_run(admission, authority):
            return original_run(admission, authority, process_observer=observe)
        # A request parent must have no HTTP contact before local production.
        def forbidden_external(request):
            raise AssertionError("parent process contacted HTTP before local prefix completion")
        async def resolver(*_): return ["93.184.216.34"]
        adapter = __import__("src.extensions.github_followthrough", fromlist=["GitHubFollowthroughService"]).GitHubFollowthroughService(
            resolver=resolver, transport=httpx.MockTransport(forbidden_external))
        try:
            with patch.object(jobs, "claim_job", shortened_claim), patch.object(supervisor, "run", observed_run):
                result = await RepoPublicationService(adapter=adapter).execute(job_id, principal, root)
            temporary = Path(result_path + ".staged")
            with temporary.open("x") as output:
                json.dump(result, output, sort_keys=True)
                output.flush(); os.fsync(output.fileno())
            os.rename(temporary, result_path)
        finally:
            for item in reversed(patches): item.stop()
            await physical.dispose()
    asyncio.run(run())


async def _wait_file(path, seconds=10):
    until = time.monotonic()+seconds
    while time.monotonic() < until:
        if path.is_file(): return json.loads(path.read_bytes())
        await asyncio.sleep(.02)
    raise AssertionError("actual child process file did not appear: " + str(path))


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="Selected Linux helper proof; no macOS host proof claimed")
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("loss", ["request_parent", "supervisor"])
async def test_actual_canonical_process_loss_closes_only_with_positive_terminal(client, async_db, tmp_path, monkeypatch, loss):
    from src.extensions.github_followthrough import GitHubFollowthroughService
    from src.workflows import repo_publication
    flow = await actual_repair(client, async_db, tmp_path, monkeypatch)
    connection = await selected_connection(client, flow, monkeypatch)
    http = GitDataTransport(flow["repository"])
    async def resolver(*_): return ["93.184.216.34"]
    adapter = GitHubFollowthroughService(resolver=resolver, transport=httpx.MockTransport(http.handler))
    monkeypatch.setattr(repo_publication, "GitHubFollowthroughService", lambda: adapter)
    preview = await client.post("/api/capabilities/github/repo-publication/prepare", json=request_for(flow, connection), headers=ORIGIN)
    assert preview.status_code == 200, preview.text
    prepared = preview.json()
    approval = await client.post(f"/api/approvals/{prepared['approval_id']}/approve", headers=ORIGIN)
    assert approval.status_code == 200, approval.text
    async with async_db() as db:
        physical = db.bind
        database_url = str(physical.url)
    observed_path, continue_path, result_path = (tmp_path / name for name in ("actual-live-child.json", "continue-parent", "actual-parent-result.json"))
    process = multiprocessing.get_context("fork").Process(target=_canonical_request_process,
        args=(database_url, prepared["job_id"], flow["owner"].principal_id, flow["owner"].session_id,
            str(observed_path), str(continue_path), str(result_path)))
    process.start()
    try:
        observed = await _wait_file(observed_path)
        child = observed["child_started"]
        assert process.pid == observed["parent_pid"] and start_identity(child["pid"]) == child["start"]
        running = await jobs.get_job(prepared["job_id"])
        checkpoint = next(item["payload"] for item in running["checkpoints"] if item["checkpoint_id"] == "publication_supervisor_admission")
        admission = json.loads(Path(checkpoint["admission_path"]).read_bytes())
        progress_path = Path(checkpoint["admission_path"]).parent / (admission["token"] + ".progress.json")
        progress = await _wait_file(progress_path)
        assert progress["binding"]["job_id"] == prepared["job_id"]
        assert progress["binding"]["root"] == flow["owner"].session_id
        assert progress["binding"]["principal"] == flow["owner"].principal_id
        assert progress["binding"]["fence"] == running["lease"]["fencing_token"]
        assert progress["binding"]["authority_digest"] == running["authority_digest"]
        assert progress["binding"] == checkpoint["binding"]
        with pytest.raises(ValueError, match="publication_producer_still_live"):
            with supervisor.guard(Path(checkpoint["stage"])): pass
        if loss == "request_parent":
            process.kill()
            await asyncio.to_thread(process.join, 5)
            assert process.exitcode == -signal.SIGKILL
            assert start_identity(progress["supervisor"]["pid"]) == progress["supervisor"]["start"]
            proof_path = progress_path.with_name(admission["token"] + ".terminal.json")
            await _wait_file(proof_path)
            until = time.monotonic()+5
            while True:
                try:
                    with supervisor.guard(Path(checkpoint["stage"])):
                        terminal = supervisor.terminal(checkpoint)
                    break
                except ValueError as exc:
                    if str(exc) != "publication_producer_still_live" or time.monotonic() >= until: raise
                    await asyncio.sleep(.02)
            assert terminal["status"] == "prefix_complete" and terminal["result"] is None
            assert terminal["cleanup"]["signalled"] == 0
            assert all(item["direct_reaped"] and item["output_drained"] and item["group_empty"] for item in terminal["commands"])
            # Wait for the real shortened canonical lease, never edit expiry rows.
            await asyncio.sleep(max(0, admission["deadline_at"] - time.monotonic()) + .1)
        else:
            assert exact_signal(progress["supervisor"]["pid"], progress["supervisor"]["start"], signal.SIGKILL)
            with continue_path.open("x") as output: output.write("actual helper killed")
            await _wait_file(result_path)
            await asyncio.to_thread(process.join, 5)
            assert process.exitcode == 0
            terminal = None
            with supervisor.guard(Path(checkpoint["stage"])):
                with pytest.raises(ValueError, match="publication_supervisor_terminal_missing"):
                    supervisor.terminal(checkpoint)
        await physical.dispose()
        recovered = await jobs.get_job(prepared["job_id"])
        if recovered["status"] == "running": recovered = await jobs.recover_stale_job(prepared["job_id"])
        assert recovered["status"] == "unknown_external_effect" and not recovered["lease"]["owner"]
        from src.extensions.github_capacity_closure import original_effects
        effects = original_effects(recovered)
        assert len(effects) == 1 and effects[0]["effect_type"] == "repo_publication_local_producer"
        assert http.calls == []  # Positive finite inventory: zero remote intents/GETs.
        stopped = await client.post("/api/capabilities/github/connection/revoke", json={"expected_revision": connection["revision"]}, headers=ORIGIN)
        assert stopped.status_code == 200, stopped.text
        row = await adapter._get_connection_row(flow["owner"].principal_id)
        close = {"acknowledged_capacity_close": True, "expected_job_revision": recovered["revision"],
            "expected_connection_revision": row.revision, "expected_connection_fence": row.active_fence,
            "idempotency_key": str(uuid.uuid4())}
        response = await client.post(f"/api/capabilities/github/repo-publication/jobs/{prepared['job_id']}/close-capacity", json=close, headers=ORIGIN)
        assert response.status_code == (200 if loss == "request_parent" else 409), response.text
        after = await jobs.get_job(prepared["job_id"])
        final_connection = await adapter._get_connection_row(flow["owner"].principal_id)
        assert after["status"] == "unknown_external_effect" and after["effects"] == recovered["effects"] and http.calls == []
        if loss == "request_parent":
            assert after["github_capacity_closure"] and final_connection.active_job_id is None
            assert after["result"] == recovered["result"] and after["revision"] == recovered["revision"]+1
        else:
            assert after == recovered and final_connection.active_job_id == prepared["job_id"]
        receipt = {"loss": loss, "actual_parent_pid": process.pid, "observed_child": observed,
            "canonical_before_loss": running, "canonical_admission": checkpoint,
            "actual_progress": progress, "actual_terminal": terminal, "after_restart": recovered,
            "after_close": after, "close_request": close, "close_status": response.status_code,
            "http_calls": http.calls, "test_boundaries": ["provider transport intercepted", "admitted lease shortened120s→10s", "third child-start notification paused"]}
        path = tmp_path / "actual-canonical-process-capacity-close.json"
        with path.open("x") as output: json.dump(receipt, output, sort_keys=True)
        print("ACTUAL_CANONICAL_PROCESS_CAPACITY_CLOSE=" + str(path))
    finally:
        if process.is_alive(): process.kill()
        await asyncio.to_thread(process.join, 5)
