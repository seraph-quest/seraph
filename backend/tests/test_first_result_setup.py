"""First-result journeys through real typed tasks and workspace storage."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import patch
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import event
from sqlmodel import SQLModel
from tests.conftest import _PATCH_TARGETS
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select

from config.settings import settings
from src.db.models import AuditEvent, ScheduledJob, Goal, GuardianSourceWatch, WorkBoardTask
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.manager import workflow_manager
from src.skills.manager import skill_manager
from src.scheduler.scheduled_jobs import execute_scheduled_job
from src.api.auth import _reset_login_throttle_for_tests


@pytest_asyncio.fixture
async def async_db(tmp_path, request):
    # Separate connections and a real SQLite file expose cross-tab write races.
    if "cross_tab" in request.node.name:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'setup.db'}", connect_args={"timeout": 20})
    else:
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    from src.db.engine import _configure_sqlite_connection
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    @asynccontextmanager
    async def get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    patches = [patch(target, get_session) for target in _PATCH_TARGETS]
    for patched in patches: patched.start()
    get_session.engine = engine
    try:
        yield get_session
    finally:
        for patched in reversed(patches): patched.stop()
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def authenticated_setup_operator(client, monkeypatch):
    _reset_login_throttle_for_tests()
    monkeypatch.setattr(settings, "operator_auth_secret", "first-result-test-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    client.headers["origin"] = "http://localhost:3001"
    login = await client.post("/api/auth/login", json={"password": "first-result-test-secret"})
    assert login.status_code == 200, login.text


@pytest.fixture
def setup_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    defaults = Path(__file__).parents[1] / "src/defaults/workflows/goal-snapshot-to-file.md"
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    shutil.copy(defaults, workflows)
    skills = tmp_path / "skills"
    skills.mkdir()
    shutil.copy(Path(__file__).parents[1] / "src/defaults/skills/goal-reflection.md", skills)
    skill_manager.init(str(tmp_path / "skills"))
    workflow_manager.init(str(workflows))
    yield tmp_path
    workflow_manager.init(str(tmp_path / "empty"))


def progress(**values):
    return {"starter": "local_snapshot", "step": "preview", "journey_id": "test-first-result", "title": "Read my first goal", "source": "", **values}


@pytest.mark.asyncio
async def test_progress_survives_reload_and_cannot_claim_completion(client, async_db):
    saved = await client.put("/api/user/onboarding/progress", json=progress())
    assert saved.status_code == 200, saved.text
    assert (await client.get("/api/user/onboarding/progress")).json()["progress"]["journey_id"] == "test-first-result"
    forged = await client.put("/api/user/onboarding/progress", json=progress(step="result_opened"))
    assert forged.status_code == 422
    for source in ["https://user:secret@example.org/updates", "https://example.org/updates?token=secret", "https://127.0.0.1/secret", "https://example.org/updates#secret"]:
        denied = await client.put("/api/user/onboarding/progress", json=progress(source=source))
        assert denied.status_code == 422, denied.text
    assert (await client.get("/api/user/onboarding/progress")).json()["progress"]["source"] == ""
    unknown = await client.post("/api/user/onboarding/result/foreign-task/open")
    assert unknown.status_code == 404


async def create_snapshot(client, *, journey="test-first-result", goal=None):
    if goal is None:
        prepared = await client.post("/api/user/onboarding/starter", json=progress(journey_id=journey, title="First real local result"))
        assert prepared.status_code == 200, prepared.text
        goal = prepared.json()["goal"]
    reserved = await client.post("/api/work-board/input-artifacts", json={"schema_version": 1, "capability_id": "workflow.goal-snapshot-to-file", "goal_id": goal["id"], "goal_revision": goal["revision"], "input": {"file_path": f"artifacts/first-result/{journey}.md"}, "idempotency_key": f"{journey}:input"})
    assert reserved.status_code == 200, reserved.text
    created = await client.post("/api/work-board/tasks", json={"title": "Write first snapshot", "goal_id": goal["id"], "goal_revision": goal["revision"], "status": "todo", "capability_id": "workflow.goal-snapshot-to-file", "input_artifact_id": reserved.json()["artifact_id"], "priority": 80, "idempotency_key": f"{journey}:task"})
    assert created.status_code == 200, created.text
    return goal, created.json()["task"]


async def run_task(client, task, async_db):
    dispatcher = WorkBoardDispatcher(session_provider=async_db)
    for _ in range(4):
        receipt = await dispatcher.run_pass()
        task = (await client.get(f"/api/work-board/tasks/{task['task_id']}")).json()["task"]
        if task["status"] in {"done", "blocked", "review"}: break
    assert task["status"] == "done", (task.get("block_reason"), task.get("recovery_action"), receipt)
    assert task["readback_status"] == "verified"
    return task


@pytest.mark.asyncio
async def test_real_local_snapshot_first_result_readback_and_changed_artifact(client, async_db, setup_workspace):
    goal, task = await create_snapshot(client)
    saved = await client.put("/api/user/onboarding/progress", json=progress(step="task_saved", goal_id=goal["id"], goal_revision=goal["revision"], task_id=task["task_id"]))
    assert saved.status_code == 200
    incomplete = await client.post(f"/api/user/onboarding/result/{task['task_id']}/open")
    assert incomplete.status_code == 409
    await run_task(client, task, async_db)
    opened = await client.post(f"/api/user/onboarding/result/{task['task_id']}/open")
    assert opened.status_code == 200, opened.text
    assert "First real local result" in opened.json()["content"]
    assert opened.json()["memory_status"] == "no_learning"
    assert opened.json()["progress"]["step"] == "result_opened"
    assert (await client.get("/api/user/profile")).json()["onboarding_completed"] is True
    assert (setup_workspace / opened.json()["file_path"]).is_file()
    (setup_workspace / opened.json()["file_path"]).write_text("changed")
    denied = await client.post(f"/api/user/onboarding/result/{task['task_id']}/open")
    assert denied.status_code == 409
    assert denied.json()["detail"]["code"] == "setup_artifact_changed"
    outside = setup_workspace.parent / "outside-first-result.md"
    outside.write_text(opened.json()["content"])
    artifact = setup_workspace / opened.json()["file_path"]
    artifact.unlink()
    artifact.symlink_to(outside)
    symlink_denied = await client.post(f"/api/user/onboarding/result/{task['task_id']}/open")
    assert symlink_denied.status_code == 409
    assert "content" not in symlink_denied.json()
    async with async_db() as db:
        events = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "onboarding_first_result_step"))).scalars().all()
    assert sum("result_opened" in event.details_json for event in events) == 1


@pytest.mark.asyncio
async def test_public_baseline_then_snapshot_is_verified_and_schedule_stays_disabled(client, async_db, setup_workspace, monkeypatch):
    source_file = setup_workspace / "public-source.txt"
    source_file.write_text("A useful public-source baseline\nRelease notes version one\n")
    requests = []

    async def intercepted_public_transport(url):
        requests.append(url)
        assert url == "https://example.org/updates.txt"
        return SimpleNamespace(status_code=200, headers={"content-type": "text/plain", "etag": "version-one"}, content=source_file.read_bytes())

    monkeypatch.setattr("src.guardian.source_watch.fetch_pinned_https", intercepted_public_transport)
    prepared = await client.post("/api/user/onboarding/starter", json=progress(starter="public_watch", journey_id="public", title="Observe my public source", source="https://example.org/updates.txt"))
    assert prepared.status_code == 200, prepared.text
    goal = prepared.json()["goal"]
    watch = prepared.json()["watch"]
    assert watch["schedule"]["enabled"] is False
    assert watch["state"] == "active"
    await execute_scheduled_job(watch["scheduled_job_id"])
    assert requests == []
    async with async_db() as db:
        job = (await db.execute(select(ScheduledJob).where(ScheduledJob.id == watch["scheduled_job_id"]))).scalar_one()
        assert job.enabled is False
    reserved = await client.post("/api/work-board/input-artifacts", json={"schema_version": 1, "capability_id": "guardian.research-watch.v1", "goal_id": goal["id"], "goal_revision": goal["revision"], "input": {"watch_id": watch["id"], "expected_plan_revision": watch["plan_revision"]}, "idempotency_key": "public:watch:input"})
    assert reserved.status_code == 200, reserved.text
    created = await client.post("/api/work-board/tasks", json={"title": "Read public baseline", "goal_id": goal["id"], "goal_revision": goal["revision"], "status": "todo", "capability_id": "guardian.research-watch.v1", "input_artifact_id": reserved.json()["artifact_id"], "priority": 80, "idempotency_key": "public:watch:task"})
    assert created.status_code == 200, created.text
    watch_task = await run_task(client, created.json()["task"], async_db)
    assert requests == ["https://example.org/updates.txt"]
    observed = (await client.get(f"/api/capabilities/source-watches/{watch['id']}")).json()
    assert observed["last_status"] == "baseline_initialized"
    assert observed["baselines"][0]["sha256"]
    assert observed["latest_packet"] is None
    paused = await client.patch(f"/api/capabilities/source-watches/{watch['id']}", json={"expected_plan_revision": watch["plan_revision"], "state": "paused"})
    assert paused.status_code == 200, paused.text
    goal, task = await create_snapshot(client, journey="public", goal=goal)
    await run_task(client, task, async_db)
    await client.put("/api/user/onboarding/progress", json=progress(starter="public_watch", journey_id="public", source="https://example.org/updates.txt", step="admitted", goal_id=goal["id"], goal_revision=goal["revision"], watch_id=watch["id"], plan_revision=watch["plan_revision"], watch_task_id=watch_task["task_id"], task_id=task["task_id"]))
    opened = await client.post(f"/api/user/onboarding/result/{task['task_id']}/open")
    assert opened.status_code == 200, opened.text
    assert opened.json()["observation"]["status"] == "baseline_initialized"
    assert opened.json()["observation"]["memory_status"] == "no_learning"
    assert opened.json()["observation"]["schedule_state"] == "paused"
    assert opened.json()["memory_status"] == "no_learning"
    assert "Observe my public source" in opened.json()["content"]
    assert requests == ["https://example.org/updates.txt"]
    async with async_db() as db:
        job = (await db.execute(select(ScheduledJob).where(ScheduledJob.id == watch["scheduled_job_id"]))).scalar_one()
        assert job.enabled is False

    # Both same-owner watches have genuine completed dispatcher/readback proof.
    # Mixing either watch's row with the other's task must still fail closed.
    second = await client.post("/api/capabilities/source-watches", json={"goal_id": goal["id"], "expected_goal_revision": goal["revision"], "sources": [{"source_key": "primary", "kind": "public_https_text", "target": "https://example.org/updates.txt"}], "criteria": {}, "schedule": {"cron": "0 8 * * *", "timezone": "UTC", "enabled": False}, "write_mode": "approval_each_run"})
    assert second.status_code == 200, second.text
    other_watch = second.json()
    other_input = await client.post("/api/work-board/input-artifacts", json={"schema_version": 1, "capability_id": "guardian.research-watch.v1", "goal_id": goal["id"], "goal_revision": goal["revision"], "input": {"watch_id": other_watch["id"], "expected_plan_revision": other_watch["plan_revision"]}, "idempotency_key": "public:other:input"})
    other_created = await client.post("/api/work-board/tasks", json={"title": "Other real baseline", "goal_id": goal["id"], "goal_revision": goal["revision"], "status": "todo", "capability_id": "guardian.research-watch.v1", "input_artifact_id": other_input.json()["artifact_id"], "idempotency_key": "public:other:task"})
    other_task = await run_task(client, other_created.json()["task"], async_db)
    other_paused = await client.patch(f"/api/capabilities/source-watches/{other_watch['id']}", json={"expected_plan_revision": other_watch["plan_revision"], "state": "paused"})
    assert other_paused.status_code == 200
    exact = progress(starter="public_watch", journey_id="public", source="https://example.org/updates.txt", step="admitted", goal_id=goal["id"], goal_revision=goal["revision"], watch_id=watch["id"], plan_revision=watch["plan_revision"], watch_task_id=watch_task["task_id"], task_id=task["task_id"])
    for change in [
        {"watch_id": other_watch["id"]}, {"watch_task_id": other_task["task_id"]},
        {"plan_revision": watch["plan_revision"] + 1}, {"source": "https://example.org/other.txt"},
    ]:
        await client.put("/api/user/onboarding/progress", json={**exact, **change})
        denied = await client.post(f"/api/user/onboarding/result/{task['task_id']}/open")
        assert denied.status_code == 409, denied.text
        assert "content" not in denied.json()
    await client.put("/api/user/onboarding/progress", json={**exact, "watch_id": other_watch["id"], "watch_task_id": other_task["task_id"]})
    assert (await client.post(f"/api/user/onboarding/result/{task['task_id']}/open")).status_code == 200

    changed = await client.patch(f"/api/capabilities/source-watches/{watch['id']}", json={"expected_plan_revision": paused.json()["plan_revision"], "schedule": {"cron": "0 */6 * * *", "timezone": "UTC"}, "state": "active"})
    assert changed.status_code == 200, changed.text
    assert changed.json()["schedule"]["enabled"] is False
    paused = await client.patch(f"/api/capabilities/source-watches/{watch['id']}", json={"expected_plan_revision": changed.json()["plan_revision"], "state": "paused"})
    assert paused.json()["schedule"]["enabled"] is False
    await execute_scheduled_job(watch["scheduled_job_id"])
    assert requests == ["https://example.org/updates.txt"] * 2


@pytest.mark.asyncio
async def test_public_input_storage_rejects_forged_authority_and_keeps_private_capabilities_blocked(client, setup_workspace):
    goal, _task = await create_snapshot(client, journey="negative")
    for index, input_value in enumerate([
        {"file_path": "artifacts/result.md", "owner": "forged"},
        {"file_path": "artifacts/result.md", "session_id": "foreign"},
        {"file_path": "artifacts/result.md", "approval_id": "forged"},
        {"file_path": "artifacts/result.md", "api_key": "secret"},
        {"file_path": "../outside.md"},
        {"file_path": ".env"},
    ]):
        denied = await client.post("/api/work-board/input-artifacts", json={"schema_version": 1, "capability_id": "workflow.goal-snapshot-to-file", "goal_id": goal["id"], "goal_revision": goal["revision"], "input": input_value, "idempotency_key": f"invalid:{index}"})
        assert denied.status_code == 422, denied.text
    private = await client.post("/api/work-board/input-artifacts", json={"schema_version": 1, "capability_id": "engineering.repo-change.v1", "goal_id": goal["id"], "goal_revision": goal["revision"], "input": {"candidate_id": "candidate", "repository_path": "repo", "patch_artifact_id": "patch", "patch_sha256": "a" * 64, "allowed_paths": ["file.txt"], "test_args": ["pytest"]}, "idempotency_key": "private:input"})
    assert private.status_code == 422
    assert private.json()["detail"]["code"] == "secret_like_capability_blocked"


@pytest.mark.parametrize("status", ["baseline_initialized", "rebaseline_required", "no_change"])
def test_observation_status_exception_requires_watch_authority_terminal_root_and_readback(status):
    projection = {"status": "succeeded", "run_identity": "source-watch:run", "root_run_identity": "source-watch:run", "declared_authority": {"capability_id": "guardian.research-watch.v1"}, "effects": [{"receipt_kind": "readback", "effect_type": "source_observation", "status": "succeeded", "target_digest": "a" * 64, "readback_id": "readback:source", "verified_at": "2026-10-02T08:00:00Z", "details": {"verified": True}}]}
    assert WorkBoardDispatcher._direct_readback({"status": status}, projection, "source-watch:run")
    assert WorkBoardDispatcher._direct_readback({"status": status}, {**projection, "declared_authority": {"capability_id": "browser.public-task.v1"}}, "source-watch:run") is None
    assert WorkBoardDispatcher._direct_readback({"status": status}, {**projection, "status": "running"}, "source-watch:run") is None
    assert WorkBoardDispatcher._direct_readback({"status": status}, {**projection, "effects": []}, "source-watch:run") is None
    assert WorkBoardDispatcher._direct_readback({"status": status}, {**projection, "root_run_identity": "forged-run"}, "source-watch:run") is None


@pytest.mark.asyncio
async def test_progress_is_scoped_to_authenticated_owner_session(client):
    goal, task = await create_snapshot(client, journey="owner-scope")
    saved = await client.put("/api/user/onboarding/progress", json=progress())
    assert saved.status_code == 200
    client.cookies.clear()
    await client.post("/api/auth/login", json={"password": "first-result-test-secret"})
    assert (await client.get("/api/user/onboarding/progress")).json()["progress"] is None
    await client.put("/api/user/onboarding/progress", json=progress(task_id=task["task_id"], goal_id=goal["id"], goal_revision=goal["revision"]))
    foreign = await client.post(f"/api/user/onboarding/result/{task['task_id']}/open")
    missing = await client.post("/api/user/onboarding/result/unknown-task/open")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


@pytest.mark.parametrize("enabled", ["false", 0, None, []])
def test_schedule_enabled_is_a_strict_boolean(enabled):
    from src.guardian.source_watch import _schedule_enabled, SourceWatchError
    with pytest.raises(SourceWatchError, match="schedule_enabled_invalid"):
        _schedule_enabled({"enabled": enabled})
    assert _schedule_enabled({}) is True
    assert _schedule_enabled({}, default=False) is False


@pytest.mark.asyncio
async def test_skip_is_an_operator_activity_step_without_first_result_success(client, async_db):
    await client.put("/api/user/onboarding/progress", json=progress())
    skipped = await client.post("/api/user/onboarding/skip")
    assert skipped.status_code == 200
    async with async_db() as db:
        events = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "onboarding_first_result_skipped"))).scalars().all()
    assert len(events) == 1
    assert "accepted work remains in Work" in events[0].summary
    assert (await client.get("/api/user/onboarding/progress")).json()["progress"]["step"] == "preview"


@pytest.mark.asyncio
@pytest.mark.parametrize("starter", ["local_snapshot", "public_watch"])
async def test_starter_cross_tab_race_has_one_goal_and_watch_and_binds_immutable_inputs(client, async_db, starter):
    body = progress(starter=starter, source="https://example.org/updates.txt" if starter == "public_watch" else "")
    responses = await asyncio.gather(*(client.post("/api/user/onboarding/starter", json=body) for _ in range(4)))
    assert [response.status_code for response in responses] == [200] * 4, [response.text for response in responses]
    assert len({response.json()["goal"]["id"] for response in responses}) == 1
    if starter == "public_watch":
        assert len({response.json()["watch"]["id"] for response in responses}) == 1
        assert all(response.json()["watch"]["schedule"]["enabled"] is False for response in responses)
    changed = await client.post("/api/user/onboarding/starter", json={**body, "title": "Different tab title"})
    assert changed.status_code == 409
    if starter == "public_watch":
        changed_source = await client.post("/api/user/onboarding/starter", json={**body, "source": "https://example.org/other.txt"})
        assert changed_source.status_code == 409
    replay = await client.post("/api/user/onboarding/starter", json=body)
    assert replay.status_code == 200
    assert replay.json()["goal"]["admission_budget"] == responses[0].json()["goal"]["admission_budget"]
    async with async_db() as db:
        assert len((await db.execute(select(Goal))).scalars().all()) == 1
        assert len((await db.execute(select(GuardianSourceWatch))).scalars().all()) == (1 if starter == "public_watch" else 0)
        assert len((await db.execute(select(ScheduledJob))).scalars().all()) == (1 if starter == "public_watch" else 0)
        grants = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "goal_proactive_permission_authorized"))).scalars().all()
        assert len(grants) == (1 if starter == "public_watch" else 0)
        events = (await db.execute(select(AuditEvent))).scalars().all()
        watch_creations = [row for row in events if (details := json.loads(row.details_json or "{}"))
            and details.get("name") == "source_watch" and row.event_type == "integration_created"]
        assert len(watch_creations) == (1 if starter == "public_watch" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["disabled", "expired", "grant_revoked"])
async def test_public_starter_replay_preserves_revoked_and_expired_authority(client, async_db, revocation):
    body = progress(starter="public_watch", source="https://example.org/updates.txt")
    created = await client.post("/api/user/onboarding/starter", json=body)
    assert created.status_code == 200, created.text
    goal = created.json()["goal"]
    update = {"expected_revision": goal["revision"]}
    if revocation == "disabled":
        update["proactive_enabled"] = False
    else:
        budget = {**goal["admission_budget"]}
        if revocation == "expired":
            budget["period_started_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
            budget["period_expires_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        else:
            budget["reviewed_grant"] = False
            budget["grant_id"] = None
        update["admission_budget"] = budget
    revoked = await client.patch(f"/api/goals/{goal['id']}", json=update)
    assert revoked.status_code == 200, revoked.text
    before = next(row for row in (await client.get("/api/goals")).json() if row["id"] == goal["id"])
    async with async_db() as db:
        grants_before = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "goal_proactive_permission_authorized"))).scalars().all()
    assert len(grants_before) == 1
    replay = await client.post("/api/user/onboarding/starter", json=body)
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["code"] == {"disabled": "goal_proactive_disabled", "expired": "goal_budget_period_expired", "grant_revoked": "goal_budget_missing_reviewed_grant"}[revocation]
    after = next(row for row in (await client.get("/api/goals")).json() if row["id"] == goal["id"])
    assert after == before
    async with async_db() as db:
        grants_after = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "goal_proactive_permission_authorized"))).scalars().all()
        assert [row.id for row in grants_after] == [row.id for row in grants_before]
        assert len((await db.execute(select(Goal))).scalars().all()) == 1
        assert len((await db.execute(select(GuardianSourceWatch))).scalars().all()) == 1
        jobs = (await db.execute(select(ScheduledJob))).scalars().all()
        assert len(jobs) == 1 and jobs[0].enabled is False


@pytest.mark.asyncio
async def test_initial_permission_checkpoint_failure_rolls_back_and_replays_do_not_infer_consent(client, async_db):
    from sqlalchemy.exc import OperationalError
    from sqlalchemy import delete
    body = progress(starter="public_watch", source="https://example.org/updates.txt")
    def reject_grant_checkpoint(_conn, _cursor, statement, parameters, _context, _many):
        if "INSERT INTO audit_events" in statement and "goal_proactive_permission_authorized" in parameters:
            raise OperationalError(statement, parameters, RuntimeError("interrupted authorization checkpoint"))
    event.listen(async_db.engine.sync_engine, "before_cursor_execute", reject_grant_checkpoint)
    try:
        blocked = await client.post("/api/user/onboarding/starter", json=body)
    finally:
        event.remove(async_db.engine.sync_engine, "before_cursor_execute", reject_grant_checkpoint)
    assert blocked.status_code == 503, blocked.text
    assert blocked.json()["detail"]["code"] == "setup_authorization_storage_unavailable"
    async with async_db() as db:
        assert (await db.execute(select(Goal))).scalars().all() == []
        assert (await db.execute(select(GuardianSourceWatch))).scalars().all() == []
        assert (await db.execute(select(ScheduledJob))).scalars().all() == []
        assert (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "goal_proactive_permission_authorized"))).scalars().all() == []
    retry = await client.post("/api/user/onboarding/starter", json=body)
    assert retry.status_code == 200, retry.text
    goal = retry.json()["goal"]
    # Simulate a persisted disabled checkpoint from the earlier two-phase path.
    async with async_db() as db:
        row = (await db.execute(select(Goal).where(Goal.id == goal["id"]))).scalar_one()
        row.proactive_enabled = False
        db.add(row)
        await db.execute(delete(AuditEvent).where(AuditEvent.event_type == "goal_proactive_permission_authorized"))
    replay = await client.post("/api/user/onboarding/starter", json=body)
    assert replay.status_code == 409
    assert "Open Goals" in replay.json()["detail"]["recovery"]
    async with async_db() as db:
        row = (await db.execute(select(Goal).where(Goal.id == goal["id"]))).scalar_one()
        assert row.proactive_enabled is False
        grants = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "goal_proactive_permission_authorized"))).scalars().all()
        assert len(grants) == 0


@pytest.mark.asyncio
async def test_public_starter_replay_cannot_recreate_deleted_initial_grant(client, async_db):
    body = progress(starter="public_watch", source="https://example.org/updates.txt")
    created = await client.post("/api/user/onboarding/starter", json=body)
    assert created.status_code == 200
    goal = created.json()["goal"]
    deleted = await client.delete(f"/api/goals/{goal['id']}")
    assert deleted.status_code == 200, deleted.text
    replay = await client.post("/api/user/onboarding/starter", json=body)
    assert replay.status_code == 409, replay.text
    assert replay.json()["detail"]["code"] == "setup_initial_permission_already_recorded"
    async with async_db() as db:
        assert (await db.execute(select(Goal))).scalars().all() == []
        grants = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "goal_proactive_permission_authorized"))).scalars().all()
        assert len(grants) == 1
        # Existing watch/job storage is retained, never duplicated or enabled.
        jobs = (await db.execute(select(ScheduledJob))).scalars().all()
        assert all(job.enabled is False for job in jobs)
        assert len((await db.execute(select(GuardianSourceWatch))).scalars().all()) <= 1
        assert len(jobs) <= 1


@pytest.mark.asyncio
async def test_result_open_retry_reconciles_profile_after_committed_completion_event(client, async_db, setup_workspace):
    from sqlalchemy.exc import OperationalError
    goal, task = await create_snapshot(client, journey="completion-recovery")
    await client.put("/api/user/onboarding/progress", json=progress(journey_id="completion-recovery", step="task_saved", goal_id=goal["id"], goal_revision=goal["revision"], task_id=task["task_id"]))
    await run_task(client, task, async_db)
    assert (await client.get("/api/user/profile")).json()["onboarding_completed"] is False
    def fail_profile_write(_conn, _cursor, statement, parameters, _context, _many):
        if "UPDATE user_profiles" in statement:
            raise OperationalError(statement, parameters, RuntimeError("profile completion write unavailable"))
    event.listen(async_db.engine.sync_engine, "before_cursor_execute", fail_profile_write)
    try:
        failed = await client.post(f"/api/user/onboarding/result/{task['task_id']}/open")
    finally:
        event.remove(async_db.engine.sync_engine, "before_cursor_execute", fail_profile_write)
    assert failed.status_code == 503, failed.text
    assert failed.json()["detail"]["code"] == "setup_profile_completion_unavailable"
    assert (await client.get("/api/user/onboarding/progress")).json()["progress"]["step"] == "result_opened"
    assert (await client.get("/api/user/profile")).json()["onboarding_completed"] is False
    artifact = setup_workspace / "artifacts/first-result/completion-recovery.md"
    actual_bytes = artifact.read_bytes()
    artifact.write_text("changed after the committed completion checkpoint")
    unsafe_retry = await client.post(f"/api/user/onboarding/result/{task['task_id']}/open")
    assert unsafe_retry.status_code == 409
    assert unsafe_retry.json()["detail"]["code"] == "setup_artifact_changed"
    assert (await client.get("/api/user/profile")).json()["onboarding_completed"] is False
    artifact.write_bytes(actual_bytes)
    retried = await client.post(f"/api/user/onboarding/result/{task['task_id']}/open")
    assert retried.status_code == 200, retried.text
    assert retried.json()["content"].encode() == actual_bytes
    assert (await client.get("/api/user/profile")).json()["onboarding_completed"] is True
    again = await client.post(f"/api/user/onboarding/result/{task['task_id']}/open")
    assert again.status_code == 200
    async with async_db() as db:
        completions = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "onboarding_first_result_step"))).scalars().all()
        assert sum(json.loads(row.details_json).get("step") == "result_opened" for row in completions) == 1
        assert len((await db.execute(select(WorkBoardTask))).scalars().all()) == 1
        assert (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "goal_proactive_permission_authorized"))).scalars().all() == []
