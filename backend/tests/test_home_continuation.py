"""Actual authenticated local Home projection; no provider/model contacts."""
from datetime import datetime, timedelta, timezone
import json

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import event
from sqlalchemy import select

from tests.test_inference_accounting import accounting_db
from tests.test_general_task_planner import forbid_external_inference
from config.settings import settings
from src.auth.service import create_session, authenticate_home_token_readonly
from src.auth.ownership import enroll
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import OperatorSession
from src.goals.repository import GoalRepository
from src.operator import home_projection as home_module
from src.operator.home_projection import home_projection
from src.api.operator import router
from src.api.work_board import router as work_router


async def test_actual_programme_passive_states_and_policy_publication(accounting_db,monkeypatch,forbid_external_inference):
    from pathlib import Path
    from tests.test_inference_accounting import setup_configuration
    from src.guardian.goal_programmes import GoalProgrammeService
    from src.goals.contracts import GoalProgrammeRequest,GoalProgrammeAccept
    from src.model_fabric.configuration import read_model_fabric_configuration
    from src.model_fabric.effective_policy import revoke_effective_policy
    from types import SimpleNamespace
    setup_configuration()
    client,operator = await home_setup(accounting_db,monkeypatch)
    goal = await GoalRepository().create('Private programme title',description='PRIVATE_PROGRAMME_BODY',
        owner_principal_id=operator.principal.principal_id,owner_session_id=operator.session_id)
    service = GoalProgrammeService()
    await service.start()
    request = GoalProgrammeRequest(expected_goal_revision=1,public_brief='Track public release notes',
        budget={'max_inference_microusd':1000})
    preview = await service.preview(operator=operator,goal_id=goal.id,request=request)
    accepted = await service.accept(operator=operator,goal_id=goal.id,request=GoalProgrammeAccept(
        **request.model_dump(),review_digest=preview['review_digest'],public_web_acknowledged=True,
        local_artifacts_acknowledged=True,inference_ceiling_acknowledged=True))
    statements = []
    def observe(connection,cursor,statement,parameters,context,many):
        statements.append(statement)
        assert 'from secrets' not in statement.lower()
    def deny_file(*args,**kwargs):
        raise AssertionError('Home programme GET attempted physical read')
    event.listen(accounting_db[1].sync_engine,'before_cursor_execute',observe)
    try:
        async with client:
            async def read(expected):
                statements.clear()
                with monkeypatch.context() as scoped:
                    scoped.setattr(Path,'read_text',deny_file)
                    scoped.setattr(Path,'read_bytes',deny_file)
                    response = await client.get('/api/operator/continuation')
                assert response.status_code==200,response.text
                body = response.json()
                assert body['programme_status']['items'],response.text
                row = body['programme_status']['items'][0]
                assert row['programme_id']==accepted['id'] and row['state']==expected
                assert 'PRIVATE_PROGRAMME_BODY' not in response.text and 'Track public release notes' not in response.text
                assert len([s for s in statements if s.lstrip().upper().startswith(('SELECT','WITH'))])<=18
                assert not any(s.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for s in statements)
                (accounting_db[0]/f'home-programme-{expected}-wire.json').write_text(response.text)
                (accounting_db[0]/f'home-programme-{expected}-receipt.json').write_text(json.dumps({
                    'route':'/api/operator/continuation','status':200,'cursor':response.headers.get('x-continuation-cursor')},sort_keys=True))
                return row
            active = await read('active')
            assert active['next_digest_at'] is not None
            # Actual common policy writer refreshes the passive handle; no GET file read.
            configured = read_model_fabric_configuration()
            await revoke_effective_policy(SimpleNamespace(state=SimpleNamespace(operator=operator)),
                SimpleNamespace(grant_id='provider_policy:openrouter',expected_revision=configured.egress_revision,
                    idempotency_key='home-policy-revoke'))
            blocked = await read('blocked')
            assert blocked['next_digest_at'] is None
            from dataclasses import replace
            from src.model_fabric.configuration import write_model_fabric_configuration
            revoked = read_model_fabric_configuration()
            write_model_fabric_configuration(replace(revoked,egress_revoked=False,
                egress_revision=revoked.egress_revision+1,egress_revocation_key=None))
            paused = await read('paused')
            assert paused['next_digest_at'] is None
            await GoalRepository().update(goal.id,description='Changed private source',expected_revision=1)
            paused = await read('paused')
            assert paused['goal_revision']==2
    finally:
        event.remove(accounting_db[1].sync_engine,'before_cursor_execute',observe)
        await service.stop()
        home_projection.stop()


async def home_setup(accounting_db, monkeypatch, *, enrolled=True):
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-home-password")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://test")
    monkeypatch.setattr(home_module, "get_session", accounting_db[2].accounting_sessions)
    token, operator = await create_session()
    if enrolled:
        await enroll(operator)
    operator = await authenticate_home_token_readonly(token)
    home_projection.start()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.include_router(work_router, prefix="/api")
    app.add_middleware(OperatorAuthMiddleware)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test",headers={"Origin":"http://test"})
    client.cookies.set(settings.operator_auth_cookie_name, token)
    return client, operator


async def test_actual_home_goal_pagination_is_bounded_and_readonly(accounting_db, monkeypatch, forbid_external_inference):
    client, operator = await home_setup(accounting_db, monkeypatch)
    goals = GoalRepository()
    for index in range(23):
        await goals.create(f"Private goal title {index}", owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id)
    statements = []
    commits = []
    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(accounting_db[1].sync_engine, "before_cursor_execute", observe)
    def observe_commit(connection):
        commits.append("COMMIT")
    event.listen(accounting_db[1].sync_engine,"commit",observe_commit)
    try:
        async with client:
            result = await client.get("/api/operator/continuation")
            assert result.status_code == 200, result.text
            body = result.json()
            assert set(body) == {*home_module.SECTIONS, "as_of"}
            assert sum(len(body[name]["items"]) for name in home_module.SECTIONS) == 20
            assert len(body["active_goals"]["items"]) == 20
            assert "Private goal title" not in result.text
            assert result.headers.get("x-continuation-cursor")
            selects = [s for s in statements if s.lstrip().upper().startswith(("SELECT", "WITH"))]
            assert len(selects) <= 18
            assert sum(s.lstrip().upper().startswith("BEGIN") for s in statements)+len(commits)<=4
            assert not any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements)
            first_ids = {row["goal_id"] for row in body["active_goals"]["items"]}
            second = await client.get("/api/operator/continuation", params={"cursor": result.headers["x-continuation-cursor"]})
            assert second.status_code == 200, second.text
            assert len(second.json()["active_goals"]["items"]) == 3
            assert not first_ids.intersection(row["goal_id"] for row in second.json()["active_goals"]["items"])
            assert second.json()["as_of"] == body["as_of"]
            assert "x-continuation-cursor" not in second.headers
            stale_anchor = body["active_goals"]["items"][-1]
            await goals.update(stale_anchor["goal_id"],title="Changed private anchor",expected_revision=stale_anchor["goal_revision"])
            stale = await client.get("/api/operator/continuation",params={"cursor":result.headers["x-continuation-cursor"]})
            assert stale.status_code==409 and stale.json()["detail"]["code"]=="continuation_stale"
            for params in ({"cursor":result.headers["x-continuation-cursor"],"limit":10},{"cursor":""},{"unknown":"x"}):
                invalid = await client.get("/api/operator/continuation",params=params)
                assert invalid.status_code==400 and invalid.json()["detail"]["code"]=="continuation_invalid"
    finally:
        event.remove(accounting_db[1].sync_engine, "before_cursor_execute", observe)
        event.remove(accounting_db[1].sync_engine,"commit",observe_commit)
        home_projection.stop()


async def test_ordinary_current_root_null_identity_and_enrollment_cursor_conflict(accounting_db, monkeypatch, forbid_external_inference):
    client, operator = await home_setup(accounting_db, monkeypatch, enrolled=False)
    assert operator.operator_identity_id is None
    goals = GoalRepository()
    for index in range(22):
        await goals.create(f"Unenrolled private title {index}", owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id)
    sessions = accounting_db[2].accounting_sessions
    async with sessions() as db:
        before = (await db.get(OperatorSession, operator.session_id)).model_dump()
    try:
        async with client:
            first = await client.get("/api/operator/continuation")
            assert first.status_code == 200, first.text
            assert len(first.json()["active_goals"]["items"]) == 20
            assert first.json()["programme_status"]["state"] == "blocked"
            assert first.json()["blocked_items"]["state"] == "empty"
            async with sessions() as db:
                after = (await db.get(OperatorSession, operator.session_id)).model_dump()
                assert after == before
            cursor = first.headers["x-continuation-cursor"]
            await enroll(operator)
            conflict = await client.get("/api/operator/continuation", params={"cursor":cursor})
            assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "continuation_stale"
    finally:
        home_projection.stop()


async def test_actual_expired_home_auth_never_revokes_or_renews_root(accounting_db, monkeypatch, forbid_external_inference):
    client, operator = await home_setup(accounting_db, monkeypatch)
    sessions = accounting_db[2].accounting_sessions
    async with sessions() as db:
        root = await db.get(OperatorSession, operator.session_id)
        root.idle_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        before = root.model_dump()
    try:
        async with client:
            result = await client.get("/api/operator/continuation")
            assert result.status_code == 401 and "session_expired" in result.text
        async with sessions() as db:
            root = await db.get(OperatorSession, operator.session_id)
            after = root.model_dump()
            assert root.revoked_at is None
            assert after["idle_expires_at"].replace(tzinfo=timezone.utc) == before["idle_expires_at"]
            assert after["last_seen_at"].replace(tzinfo=timezone.utc) == before["last_seen_at"].replace(tzinfo=timezone.utc)
    finally:
        home_projection.stop()


async def test_public_priority_provenance_and_actual_mixed_inspector_wire(accounting_db, monkeypatch, forbid_external_inference):
    from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate
    from src.work_board.repository import WorkBoardRepository
    from src.db.models import WorkBoardTask
    client, operator = await home_setup(accounting_db, monkeypatch)
    goal = await GoalRepository().create("Private priority goal", owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id)
    sessions = accounting_db[2].accounting_sessions
    common = {"goal_id":goal.id,"goal_revision":goal.revision,"title":"Private task title"}
    for index in range(21):
        await GoalRepository().create(f"Private mixed pagination title {index}",
            owner_principal_id=operator.principal.principal_id,owner_session_id=operator.session_id)
    try:
        async with client:
            default = await client.post("/api/work-board/tasks", json={**common,"idempotency_key":"default-priority"})
            explicit = await client.post("/api/work-board/tasks", json={**common,"idempotency_key":"explicit-priority","priority":50})
            assert default.status_code == explicit.status_code == 200, (default.text,explicit.text)
            default_id = default.json()["task"]["task_id"]
            explicit_id = explicit.json()["task"]["task_id"]
            async with sessions() as db:
                assert not (await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==default_id))).priority_explicit
                assert (await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==explicit_id))).priority_explicit
                service = await WorkBoardRepository().create_task(db,
                    WorkBoardOwner(principal_id=operator.principal.principal_id,session_id=operator.session_id),
                    WorkBoardTaskCreate(**common,idempotency_key="service-priority",priority=100))
                assert not service.task.priority_explicit
            result = await client.get("/api/operator/continuation")
            assert result.status_code == 200, result.text
            assert sum(len(result.json()[name]["items"]) for name in home_module.SECTIONS) == 20
            assert result.headers.get("x-continuation-cursor")
            tasks = result.json()["task_next_actions"]["items"]
            assert tasks[0]["task_id"] == explicit_id
            assert "Private task title" not in result.text and "Private priority goal" not in result.text
            inspector = await client.get(f"/api/work-board/tasks/{explicit_id}")
            assert inspector.status_code == 200, inspector.text
            assert inspector.json()["task"]["task_id"] == explicit_id
            native_list = await client.get("/api/work-board/tasks",params={"limit":100})
            assert native_list.status_code == 200,native_list.text
            native_events = await client.get("/api/work-board/events",params={"after":native_list.json()["last_event_id"],"limit":100})
            assert native_events.status_code == 200,native_events.text
            changed = await client.patch(f"/api/work-board/tasks/{default_id}", json={
                "expected_revision":default.json()["task"]["task_revision"],"priority":50})
            assert changed.status_code == 200, changed.text
            async with sessions() as db:
                assert (await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id==default_id))).priority_explicit
            # Genuine literal owner receipts for the UI decoder, no artifact bytes.
            (accounting_db[0]/"home-mixed-wire.json").write_text(result.text)
            (accounting_db[0]/"home-selected-task-wire.json").write_text(inspector.text)
            (accounting_db[0]/"home-native-task-list-wire.json").write_text(native_list.text)
            (accounting_db[0]/"home-native-events-wire.json").write_text(native_events.text)
            (accounting_db[0]/"home-mixed-receipt.json").write_text(json.dumps({
                "route":"/api/operator/continuation","status":result.status_code,
                "owner_root":operator.session_id,"selected_task":explicit_id,
                "native_list_request":"/api/work-board/tasks?limit=100",
                "native_events_request":f"/api/work-board/events?after={native_list.json()['last_event_id']}&limit=100",
                "cursor":result.headers.get("x-continuation-cursor")},sort_keys=True))
    finally:
        home_projection.stop()


@pytest.mark.parametrize("corrupt",["{",'{"revision":0,"preview":null,"generations":["bad"]}',
    '{"revision":0,"preview":null,"generations":[],"unexpected":true}'])
async def test_corrupt_programme_metadata_blocks_section_without_private_reads(accounting_db,monkeypatch,forbid_external_inference,corrupt):
    from pathlib import Path
    from src.db.models import Goal
    client, operator = await home_setup(accounting_db,monkeypatch)
    goal = await GoalRepository().create("Private corrupt programme goal",owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id)
    async with accounting_db[2].accounting_sessions() as db:
        row = await db.get(Goal,goal.id)
        row.goal_programmes_json = corrupt
    def deny_read(*args,**kwargs):
        raise AssertionError("Home attempted a physical file read")
    try:
        monkeypatch.setattr(Path,"read_text",deny_read)
        monkeypatch.setattr(Path,"read_bytes",deny_read)
        async with client:
            result = await client.get("/api/operator/continuation")
            assert result.status_code == 200,result.text
            assert result.json()["programme_status"]["state"] == "blocked"
            assert result.json()["programme_status"]["items"] == []
            assert "Private corrupt" not in result.text
    finally:
        home_projection.stop()


async def test_actual_additive_legacy_migration_never_backfills_method_or_priority(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    from src.db.engine import _ensure_work_board_columns
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'legacy-home.db'}")
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("CREATE TABLE work_board_tasks (task_id VARCHAR PRIMARY KEY,status VARCHAR)")
            await connection.exec_driver_sql("CREATE TABLE work_board_attempts (attempt_id VARCHAR PRIMARY KEY)")
            await connection.exec_driver_sql("INSERT INTO work_board_tasks VALUES ('legacy-task','todo')")
            await connection.exec_driver_sql("INSERT INTO work_board_attempts VALUES ('legacy-attempt')")
            await _ensure_work_board_columns(connection)
            await _ensure_work_board_columns(connection)
            task = (await connection.exec_driver_sql("SELECT priority_explicit,admitted_method_json FROM work_board_tasks")).one()
            attempt = (await connection.exec_driver_sql("SELECT admitted_method_json FROM work_board_attempts")).one()
            assert tuple(task) == (0,None)
            assert tuple(attempt) == (None,)
    finally:
        await engine.dispose()


async def test_genuine_selected_task_only_recovery_never_selects_goal_and_rollback_removes(accounting_db, monkeypatch, forbid_external_inference):
    from src.auth.ownership import RecoveryRequest, RecoveryConfirmRequest, preview, confirm, rollback
    client, original = await home_setup(accounting_db,monkeypatch,enrolled=False)
    _, continuity, _ = await enroll(original)
    goal = await GoalRepository().create("Historical private goal",owner_principal_id=original.principal.principal_id,
        owner_session_id=original.session_id)
    try:
        async with client:
            created = await client.post("/api/work-board/tasks",json={"title":"Historical private task",
                "goal_id":goal.id,"goal_revision":goal.revision,"idempotency_key":"historical-selected"})
            assert created.status_code == 200,created.text
            task_id = created.json()["task"]["task_id"]
            token, receiving = await create_session(continuity_token=continuity)
            client.cookies.set(settings.operator_auth_cookie_name,token)
            unselected = await client.get("/api/operator/continuation")
            assert unselected.status_code == 200,unselected.text
            assert unselected.json()["task_next_actions"]["items"] == []
            selection = RecoveryRequest(selections=[{"kind":"task","record_id":task_id}])
            review = await preview(receiving,selection)
            journal = await confirm(receiving,RecoveryConfirmRequest(**selection.model_dump(),
                preview_digest=review["preview_digest"],idempotency_key="home-task-only",acknowledge_read_only=True))
            result = await client.get("/api/operator/continuation")
            assert result.status_code == 200,result.text
            assert result.json()["active_goals"]["items"] == []
            tasks = result.json()["task_next_actions"]["items"]
            assert len(tasks) == 1 and tasks[0]["task_id"] == task_id
            assert tasks[0]["ownership_access"] == "recovered_read_only"
            assert "Historical private" not in result.text
            await rollback(receiving,journal["journal_id"])
            removed = await client.get("/api/operator/continuation")
            assert removed.status_code == 200 and removed.json()["task_next_actions"]["items"] == []
    finally:
        home_projection.stop()
