"""GitHub activity routes and effective grants (#1264 slice 1): real login, fake gh runner, no network."""
from __future__ import annotations

import socket
from datetime import datetime, timezone

import pytest

from config.settings import settings
from src.integrations import github_activity
from src.integrations.github_activity import GRANT_SENTENCE, GitHubActivityService
from src.utils import clock as clock_module
from src.utils.clock import FakeClock
from tests.github_activity_fixture import FakeGhRunner, FakeGitHubWorld, make_reader, user
from tests.support.clock import install_clock
from tests.test_operator_identity import HEADERS, auth, enroll, login  # noqa: F401

NOW = datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc)
REPO = "seraph-quest/seraph"


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    connect = socket.socket.connect

    def denied(sock, address):
        if isinstance(address, tuple):
            raise AssertionError("Every network socket is forbidden")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", denied)


@pytest.fixture
def clock():
    fake = FakeClock(start=NOW)
    previous = install_clock(fake)
    try:
        yield fake
    finally:
        clock_module.set_clock(previous)


@pytest.fixture
def world():
    value = FakeGitHubWorld()
    value.repo(REPO, 1)
    value.commit(REPO, "a" * 40, datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc))
    return value


@pytest.fixture
def runner(world):
    return FakeGhRunner(world)


@pytest.fixture
def gh_service(tmp_path, runner, monkeypatch):
    # The real live-root check runs: the source is bound to this login.
    monkeypatch.setattr(settings, "user_timezone", "Europe/Warsaw")
    reader = make_reader(tmp_path, runner)
    monkeypatch.setattr(github_activity, "_service", lambda: GitHubActivityService(reader=reader))
    return reader


SCOPE = {"discover_from_activity": False, "repositories": [REPO]}


async def owner_login(client):
    """A password login with enrolled ownership: the identity that owns the source."""
    session = await login(client)
    enrolled = await enroll(client)
    return {**session, "identity_id": enrolled["operator_identity_id"]}


async def collect_as(identity_id):
    return await github_activity._service().collect_day(
        owner_identity_id=identity_id, report_day=NOW.date(), tz=github_activity.user_timezone(),
        trigger="scheduled")


async def check_and_enable(client, *, allow_model_context=False):
    checked = await client.post("/api/capabilities/github/activity/gh/check", headers=HEADERS,
                                json={"acknowledge_gh_login_check": True})
    assert checked.status_code == 200, checked.text
    value = checked.json()
    enabled = await client.put("/api/capabilities/github/activity/source", headers=HEADERS, json={
        "expected_revision": 0, "scope": SCOPE, "acknowledge_gh_ambient_login": True,
        "expected_identity_id": value["identity_id"], "expected_gh_entry_path": value["gh_entry_path"],
        "allow_model_context": allow_model_context})
    assert enabled.status_code == 200, enabled.text
    return value, enabled.json()


@pytest.mark.asyncio
async def test_panel_load_starts_nothing_and_reports_off(client, async_db, clock, gh_service, runner):
    await login(client)
    status = (await client.get("/api/capabilities/github/activity")).json()
    assert status["source"]["state"] == "not_connected" and status["gh"]["available"] is True
    assert status["credential"] == {"kind": "local_gh_cli_login", "scope": "full_gh_scope",
                                    "seraph_requests": "get_only_allowlist"}
    assert status["grant_sentence"] == GRANT_SENTENCE
    assert all(endpoint.startswith("GET /") for endpoint in status["endpoints"])
    assert runner.calls == []


@pytest.mark.asyncio
async def test_check_requires_its_acknowledgement(client, async_db, clock, gh_service, runner):
    await login(client)
    for body in ({}, {"acknowledge_gh_login_check": False}):
        response = await client.post("/api/capabilities/github/activity/gh/check", headers=HEADERS, json=body)
        assert response.status_code == 422
    assert runner.calls == []


@pytest.mark.asyncio
async def test_enable_requires_ambient_login_acknowledgement(client, async_db, clock, gh_service, runner):
    await login(client)
    for ack in (None, False):
        body = {"expected_revision": 0, "scope": SCOPE, "expected_identity_id": 101,
                "expected_gh_entry_path": "/opt/homebrew/bin/gh"}
        if ack is not None:
            body["acknowledge_gh_ambient_login"] = ack
        response = await client.put("/api/capabilities/github/activity/source", headers=HEADERS, json=body)
        assert response.status_code == 422
    assert runner.calls == []


@pytest.mark.asyncio
async def test_enable_collect_items_and_stale_revision(client, async_db, clock, gh_service, runner):
    await owner_login(client)
    checked, enabled = await check_and_enable(client)
    assert checked["identity_login"] == "octo"
    assert enabled["source"]["state"] == "active" and enabled["source"]["revision"] == 1
    assert enabled["source"]["identity_id"] == 101
    collected = await client.post("/api/capabilities/github/activity/collect", headers=HEADERS,
                                  json={"report_date": "2026-11-17"})
    assert collected.status_code == 200, collected.text
    assert collected.json()["status"] == "complete"
    view = (await client.get("/api/capabilities/github/activity/items?report_date=2026-11-17")).json()
    assert [item["url"] for item in view["items"]] == [f"https://github.com/{REPO}/commit/{'a' * 40}"]
    assert view["timezone"] == "Europe/Warsaw" and view["window_start_utc"] == "2026-11-16T23:00:00+00:00"
    stale = await client.post("/api/capabilities/github/activity/revoke", headers=HEADERS, json={"expected_revision": 0})
    assert stale.status_code == 409
    revoked = await client.post("/api/capabilities/github/activity/revoke", headers=HEADERS,
                                json={"expected_revision": 1})
    assert revoked.status_code == 200 and revoked.json()["source"]["state"] == "revoked"
    runner.calls.clear()
    after = await client.post("/api/capabilities/github/activity/collect", headers=HEADERS, json={})
    assert after.json()["status"] == "revoked" and runner.calls == []
    # Stored items stay for audit.
    view = (await client.get("/api/capabilities/github/activity/items?report_date=2026-11-17")).json()
    assert len(view["items"]) == 1 and view["status"] == "revoked"


@pytest.mark.asyncio
async def test_enable_refuses_a_different_identity(client, async_db, clock, gh_service, runner, world):
    await owner_login(client)
    checked = (await client.post("/api/capabilities/github/activity/gh/check", headers=HEADERS,
                                 json={"acknowledge_gh_login_check": True})).json()
    world.user = user(303, "intruder")
    response = await client.put("/api/capabilities/github/activity/source", headers=HEADERS, json={
        "expected_revision": 0, "scope": SCOPE, "acknowledge_gh_ambient_login": True,
        "expected_identity_id": checked["identity_id"], "expected_gh_entry_path": checked["gh_entry_path"]})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "gh_identity_changed"


@pytest.mark.asyncio
async def test_effective_grants_show_both_rows_and_revoke_switches_the_source_off(client, async_db, clock,
                                                                                  gh_service, runner):
    await owner_login(client)
    await check_and_enable(client, allow_model_context=False)
    view = (await client.get("/api/extensions/effective-grants")).json()
    rows = {row["kind"]: row for row in view["grants"] if row["kind"].startswith("github_activity")}
    assert set(rows) == {"github_activity", "github_activity_model"}
    observation, model = rows["github_activity"], rows["github_activity_model"]
    assert observation["boundary"] == "observation" and observation["state"] == "active"
    assert observation["source"] == "local_gh_cli_login" and observation["destination"] == "local_evidence"
    assert observation["capability_id"] == "github.activity-read.v1"
    assert observation["limits"]["statement"] == GRANT_SENTENCE
    assert observation["limits"]["login"] == "octo" and observation["controls"] == ["revoke"]
    assert model["boundary"] == "inference_egress" and model["state"] == "denied"
    body = {"grant_id": observation["grant_id"], "expected_revision": observation["revision"],
            "idempotency_key": "github-activity-off"}
    revoked = await client.post("/api/extensions/effective-grants/revoke", headers=HEADERS, json=body)
    assert revoked.status_code == 200, revoked.text
    status = (await client.get("/api/capabilities/github/activity")).json()
    assert status["source"]["state"] == "revoked"
    after = {row["kind"]: row for row in revoked.json()["readback"]["grants"] if row["kind"] == "github_activity"}
    assert after["github_activity"]["state"] == "revoked"


@pytest.mark.asyncio
async def test_routes_require_an_authenticated_operator(client, async_db, clock, gh_service, runner):
    response = await client.get("/api/capabilities/github/activity")
    assert response.status_code in {401, 403}
    assert runner.calls == []


async def _expire_session_row(session_id):
    """Put both stored expiries in the past, then let the auth owner mark it expired."""
    from datetime import timedelta
    from sqlalchemy import update
    from src.auth.service import AuthFailure, authenticate_session
    from src.db import engine as db_engine
    from src.db.models import OperatorSession
    past = datetime.now(timezone.utc) - timedelta(days=3)
    async with db_engine.get_session() as db:
        await db.execute(update(OperatorSession).where(OperatorSession.id == session_id).values(
            idle_expires_at=past, absolute_expires_at=past + timedelta(hours=1)))
    with pytest.raises(AuthFailure) as raised:
        await authenticate_session(session_id, touch=False)
    assert raised.value.code == "session_expired"


@pytest.mark.asyncio
async def test_unenrolled_login_cannot_own_a_source(client, async_db, clock, gh_service, runner):
    await login(client)
    status = (await client.get("/api/capabilities/github/activity")).json()
    assert status["ownership_required"] is True and status["source"]["state"] == "not_connected"
    response = await client.put("/api/capabilities/github/activity/source", headers=HEADERS, json={
        "expected_revision": 0, "scope": SCOPE, "acknowledge_gh_ambient_login": True,
        "expected_identity_id": 101, "expected_gh_entry_path": "/opt/homebrew/bin/gh"})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "operator_identity_required"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_an_expired_login_does_not_stop_the_source(client, async_db, clock, gh_service, runner):
    """No time-based gate: session expiry never stops the source (operator's standing rule)."""
    owner = await owner_login(client)
    await check_and_enable(client)
    await _expire_session_row(owner["session_id"])
    runner.calls.clear()
    result = await collect_as(owner["identity_id"])
    assert result["status"] == "complete", result
    assert runner.endpoints[0] == "/user"


@pytest.mark.asyncio
async def test_after_expiry_a_new_login_sees_controls_and_revokes_the_source(client, async_db, clock, gh_service,
                                                                              runner):
    owner = await owner_login(client)
    await check_and_enable(client)
    await client.post("/api/capabilities/github/activity/collect", headers=HEADERS, json={})
    await _expire_session_row(owner["session_id"])
    # A new password login with the continuity cookie is the same identity.
    second = await login(client)
    assert second["session_id"] != owner["session_id"]
    status = (await client.get("/api/capabilities/github/activity")).json()
    assert status["source"]["state"] == "active" and status["ownership_required"] is False
    items = (await client.get(f"/api/capabilities/github/activity/items?report_date={NOW.date()}")).json()
    assert len(items["items"]) == 1
    grants = (await client.get("/api/extensions/effective-grants")).json()["grants"]
    row = next(grant for grant in grants if grant["kind"] == "github_activity")
    revoked = await client.post("/api/extensions/effective-grants/revoke", headers=HEADERS, json={
        "grant_id": row["grant_id"], "expected_revision": row["revision"], "idempotency_key": "after-expiry"})
    assert revoked.status_code == 200, revoked.text
    assert (await client.get("/api/capabilities/github/activity")).json()["source"]["state"] == "revoked"


@pytest.mark.asyncio
async def test_identity_revoke_after_a_newer_login_stops_the_source(client, async_db, clock, gh_service, runner):
    """The review's reproduction: a newer login, the enabling row left expired, then ownership revoke.

    #1270 item 6: revoking the identity stops the source row itself (state
    ``stopped``, reason ``owner_identity_revoked``, new revision) in the same
    transaction, so no view shows it ``active`` and no later run starts gh.
    """
    owner = await owner_login(client)
    await check_and_enable(client)
    await _expire_session_row(owner["session_id"])
    await login(client)
    assert (await client.post("/api/auth/ownership/revoke", headers=HEADERS)).status_code == 204
    row = await _source_row(owner["identity_id"])
    assert (row.state, row.stopped_reason, row.revision) == ("stopped", "owner_identity_revoked", 2)
    runner.calls.clear()
    result = await collect_as(owner["identity_id"])
    assert result["status"] == "stopped" and result["reason_code"] == "owner_identity_revoked"
    assert "ownership identity that enabled it was revoked" in result["message"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_a_revoked_identity_row_still_blocks_each_run(client, async_db, clock, gh_service, runner):
    """An identity revoked outside the ownership route leaves the row active; every run blocks before gh."""
    from sqlalchemy import update
    from src.db import engine as db_engine
    from src.db.models import OperatorIdentity
    owner = await owner_login(client)
    await check_and_enable(client)
    async with db_engine.get_session() as db:
        await db.execute(update(OperatorIdentity).where(OperatorIdentity.id == owner["identity_id"]).values(
            revoked_at=datetime.now(timezone.utc)))
    runner.calls.clear()
    result = await collect_as(owner["identity_id"])
    assert result["status"] == "blocked" and result["reason_code"] == "owner_identity_revoked"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_an_explicit_logout_alone_does_not_stop_the_source(client, async_db, clock, gh_service, runner):
    owner = await owner_login(client)
    await check_and_enable(client)
    assert (await client.post("/api/auth/logout", headers=HEADERS)).status_code in {200, 204}
    runner.calls.clear()
    result = await collect_as(owner["identity_id"])
    assert result["status"] == "complete" and runner.endpoints[0] == "/user"


@pytest.mark.asyncio
async def test_workspace_restore_stops_the_source(client, async_db, clock, gh_service, runner):
    import sqlite3
    from contextlib import closing
    from src.workspace.production import _stop_github_activity_sources
    owner = await owner_login(client)
    await check_and_enable(client)
    with closing(sqlite3.connect(async_db.path)) as connection:
        assert _stop_github_activity_sources(connection) == 1
        connection.commit()
    runner.calls.clear()
    result = await collect_as(owner["identity_id"])
    assert result["status"] == "stopped" and result["reason_code"] == "workspace_restored"
    assert "workspace was restored" in result["message"] and runner.calls == []
    status = (await client.get("/api/capabilities/github/activity")).json()
    assert status["source"]["state"] == "stopped" and status["source"]["revision"] == 2


@pytest.mark.asyncio
async def test_live_owner_identity_checks_identity_and_local_root(client, async_db, clock, monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy import update
    from src.auth import service as auth_service
    from src.db import engine as db_engine
    from src.db.models import OperatorIdentity, OperatorSession
    from src.integrations.github_activity import GitHubActivityError, live_owner_identity
    owner = await owner_login(client)
    source = SimpleNamespace(owner_identity_id=owner["identity_id"], owner_session_id=owner["session_id"])
    await live_owner_identity(source)
    with pytest.raises(GitHubActivityError) as raised:
        await live_owner_identity(SimpleNamespace(owner_identity_id="no-such-identity",
                                                  owner_session_id=owner["session_id"]))
    assert raised.value.code == "owner_identity_revoked"
    # A local no-login root: live while the mode is on and the root unretired.
    root = OperatorSession(token_hash=auth_service.LOCAL_ROOT_TOKEN_MARKER, idle_expires_at=auth_service.LOCAL_ROOT_EXPIRY,
                           absolute_expires_at=auth_service.LOCAL_ROOT_EXPIRY,
                           operator_identity_id=owner["identity_id"])
    async with db_engine.get_session() as db:
        db.add(root)
        await db.flush()
        root_id = root.id
    local = SimpleNamespace(owner_identity_id=owner["identity_id"], owner_session_id=root_id)
    monkeypatch.setattr(auth_service, "local_no_login_active", lambda: True)
    await live_owner_identity(local)
    monkeypatch.setattr(auth_service, "local_no_login_active", lambda: False)
    with pytest.raises(GitHubActivityError) as raised:
        await live_owner_identity(local)
    assert raised.value.code == "local_root_retired"
    monkeypatch.setattr(auth_service, "local_no_login_active", lambda: True)
    async with db_engine.get_session() as db:
        await db.execute(update(OperatorSession).where(OperatorSession.id == root_id).values(
            token_hash=f"{auth_service.LOCAL_ROOT_TOKEN_MARKER}:retired:{root_id}"))
    with pytest.raises(GitHubActivityError) as raised:
        await live_owner_identity(local)
    assert raised.value.code == "local_root_retired"
    async with db_engine.get_session() as db:
        await db.execute(update(OperatorIdentity).where(OperatorIdentity.id == owner["identity_id"]).values(
            revoked_at=datetime.now(timezone.utc)))
    with pytest.raises(GitHubActivityError) as raised:
        await live_owner_identity(source)
    assert raised.value.code == "owner_identity_revoked"


@pytest.mark.asyncio
async def test_items_route_refuses_out_of_range_dates(client, async_db, clock, gh_service, runner):
    await owner_login(client)
    await check_and_enable(client)
    assert (await client.get("/api/capabilities/github/activity/items?report_date=9999-12-31")).status_code == 422
    assert (await client.get("/api/capabilities/github/activity/items?report_date=2026-02-30")).status_code == 422
    collect = await client.post("/api/capabilities/github/activity/collect", headers=HEADERS,
                                json={"report_date": "9999-12-31"})
    assert collect.status_code == 422 and collect.json()["detail"]["code"] == "report_date_invalid"


async def _source_row(identity_id):
    return await github_activity._service()._source_row(identity_id)


async def lose_identity(client):
    """A password login on a browser without the continuity cookie or a recovery code (#1270)."""
    client.cookies.clear()
    return await login(client)


NEUTRAL_KEYS = {"id", "state", "revision", "owner", "controls", "message"}


def _foreign_text_absent(value, *, login_name="octo", gh_path="/opt/homebrew/bin/gh"):
    import json
    text = json.dumps(value)
    return login_name not in text and gh_path not in text


@pytest.mark.asyncio
async def test_effective_grants_filter_rows_by_owning_identity(client, async_db, clock, gh_service, runner):
    """#1270 item 2: another identity never sees the owner's row, its GitHub login or gh path."""
    owner = await owner_login(client)
    await check_and_enable(client, allow_model_context=True)
    own = {row["kind"] for row in (await client.get("/api/extensions/effective-grants")).json()["grants"]}
    assert {"github_activity", "github_activity_model"} <= own
    assert "github_activity_other_identity" not in own
    await lose_identity(client)
    other = await enroll(client)
    assert other["operator_identity_id"] != owner["identity_id"]
    grants = [row for row in (await client.get("/api/extensions/effective-grants")).json()["grants"]
              if row["kind"].startswith("github_activity")]
    assert [row["kind"] for row in grants] == ["github_activity_other_identity"]
    (neutral,) = grants
    assert neutral["state"] == "active" and neutral["controls"] == ["revoke"]
    # Exactly these limits: nothing more about the foreign source can leak through a new key.
    assert set(neutral["limits"]) == {"owner", "credential", "statement"}
    assert neutral["limits"]["owner"] == "another_ownership_identity"
    assert _foreign_text_absent(grants)
    status = (await client.get("/api/capabilities/github/activity")).json()
    assert status["source"]["state"] == "not_connected" and status["ownership_required"] is False
    assert _foreign_text_absent(status["other_identity_sources"])
    (foreign,) = status["other_identity_sources"]
    assert set(foreign) == NEUTRAL_KEYS
    items = (await client.get(f"/api/capabilities/github/activity/items?report_date={NOW.date()}")).json()
    assert items["items"] == [] and items["status"] == "not_connected"


@pytest.mark.asyncio
async def test_a_lost_identity_login_sees_and_stops_the_old_source(client, async_db, clock, gh_service, runner,
                                                                  monkeypatch):
    """#1270 item 3: the earlier identity's source is visible and stoppable from a login without it."""
    recorded = []

    async def record(**kwargs):
        recorded.append(kwargs)
        return True

    monkeypatch.setattr("src.audit.runtime.log_integration_event", record)
    session = await login(client)
    proof = await enroll(client)
    await check_and_enable(client)
    lost = await lose_identity(client)
    status = (await client.get("/api/capabilities/github/activity")).json()
    assert status["ownership_required"] is True
    assert "recover that ownership first" in status["message"] and "recovery code" in status["message"]
    (other,) = status["other_identity_sources"]
    assert other["state"] == "active" and other["controls"] == ["stop"] and other["revision"] == 1
    assert _foreign_text_absent(other)
    stop = f"/api/capabilities/github/activity/other-identity-sources/{other['id']}/stop"
    stale = await client.post(stop, headers=HEADERS, json={"expected_revision": 0})
    assert stale.status_code == 409
    assert (await _source_row(proof["operator_identity_id"])).state == "active"
    stopped = await client.post(stop, headers=HEADERS, json={"expected_revision": 1})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["audit_recorded"] is True
    assert stopped.json()["other_identity_sources"][0]["state"] == "stopped"
    row = await _source_row(proof["operator_identity_id"])
    assert (row.state, row.stopped_reason, row.revision) == ("stopped", "stopped_by_another_identity", 2)
    event = next(item for item in recorded if item["name"] == "source_stop")
    assert event["principal_id"] == lost["principal_id"] and event["session_id"] == lost["session_id"]
    assert event["details"]["owner_identity_id"] == proof["operator_identity_id"]
    assert (await client.post(stop, headers=HEADERS, json={"expected_revision": 2})).status_code == 409
    runner.calls.clear()
    result = await collect_as(proof["operator_identity_id"])
    assert result["status"] == "stopped" and runner.calls == []
    # Recovering the ownership shows the owner what happened; the owner cannot use the foreign route.
    client.cookies.clear()
    recovered = await login(client, proof["recovery_code"])
    assert recovered["session_id"] not in {session["session_id"], lost["session_id"]}
    status = (await client.get("/api/capabilities/github/activity")).json()
    assert status["ownership_required"] is False and status["other_identity_sources"] == []
    assert status["source"]["state"] == "stopped"
    assert "another ownership identity" in status["message"]
    own = f"/api/capabilities/github/activity/other-identity-sources/{status['source']['id']}/stop"
    assert (await client.post(own, headers=HEADERS, json={"expected_revision": 2})).status_code == 404


@pytest.mark.asyncio
async def test_another_identity_stops_the_source_through_effective_grants(client, async_db, clock, gh_service,
                                                                          runner):
    owner = await owner_login(client)
    await check_and_enable(client)
    await lose_identity(client)
    await enroll(client)
    row = next(grant for grant in (await client.get("/api/extensions/effective-grants")).json()["grants"]
               if grant["kind"] == "github_activity_other_identity")
    revoked = await client.post("/api/extensions/effective-grants/revoke", headers=HEADERS, json={
        "grant_id": row["grant_id"], "expected_revision": row["revision"], "idempotency_key": "lost-identity-stop"})
    assert revoked.status_code == 200, revoked.text
    stored = await _source_row(owner["identity_id"])
    assert (stored.state, stored.stopped_reason) == ("stopped", "stopped_by_another_identity")


@pytest.mark.asyncio
async def test_an_operator_triggered_run_records_the_requesting_principal(client, async_db, clock, gh_service,
                                                                          runner, monkeypatch):
    """#1270 item 5: audit lineage names the login that asked; an unattended run names none."""
    recorded = []

    async def record(**kwargs):
        recorded.append(kwargs)
        return True

    monkeypatch.setattr("src.audit.runtime.log_integration_event", record)
    owner = await owner_login(client)
    await check_and_enable(client)
    collected = await client.post("/api/capabilities/github/activity/collect", headers=HEADERS, json={})
    assert collected.status_code == 200 and collected.json()["status"] == "complete"
    (event,) = [item for item in recorded if item["name"] == "gh_cli"]
    assert event["principal_id"] == owner["principal_id"] and event["session_id"] == owner["session_id"]
    assert event["actor"] == "operator" and event["details"]["trigger"] == "operator"
    assert event["details"]["run_id"] == collected.json()["run"]["id"]
    recorded.clear()
    await collect_as(owner["identity_id"])
    (event,) = [item for item in recorded if item["name"] == "gh_cli"]
    assert event["principal_id"] is None and event["session_id"] is None and event["actor"] == "system"
    assert event["details"]["trigger"] == "scheduled"


async def _foreign_rows(*states):
    """Sources of other identities, newest last in ``states`` order."""
    from datetime import timedelta
    from src.db import engine as db_engine
    from src.db.models import GitHubActivitySource
    async with db_engine.get_session() as db:
        for index, state in enumerate(states):
            db.add(GitHubActivitySource(
                owner_identity_id=f"foreign-{index}", enabled_principal_id="p", owner_session_id="s",
                gh_entry_path="/opt/homebrew/bin/gh", gh_install_prefix="/opt/homebrew", state=state,
                stopped_reason=None if state == "active" else "gh_login_changed", revision=1,
                scope_json='{"discover_from_activity": true}', scope_digest="d", identity_id=900 + index,
                identity_login="octo", created_at=NOW, updated_at=NOW + timedelta(minutes=index)))


@pytest.mark.asyncio
async def test_an_active_foreign_source_is_never_pushed_out_of_the_bounded_lists(client, async_db, clock,
                                                                               gh_service, runner, monkeypatch):
    """#1296 review: active rows sort first in the panel list and the effective-grants list."""
    from src.extensions import effective_grants
    await owner_login(client)
    # One active source, older than enough stopped ones to fill both bounded lists.
    await _foreign_rows("active", *["stopped"] * 3)
    monkeypatch.setattr(github_activity, "MAX_OTHER_SOURCES", 2)
    monkeypatch.setattr(effective_grants, "LIMIT", 2)
    status = (await client.get("/api/capabilities/github/activity")).json()
    assert [row["state"] for row in status["other_identity_sources"]] == ["active", "stopped"]
    assert set(status["other_identity_sources"][0]) == NEUTRAL_KEYS
    grants = [row for row in (await client.get("/api/extensions/effective-grants")).json()["grants"]
              if row["kind"] == "github_activity_other_identity"]
    assert [row["state"] for row in grants] == ["active", "stopped"]
    assert grants[0]["controls"] == ["revoke"]


@pytest.mark.asyncio
async def test_the_owners_own_switch_off_is_audited(client, async_db, clock, gh_service, runner, monkeypatch):
    """#1296 review: /revoke writes a source_revoke event naming the login, like a foreign stop."""
    recorded = []

    async def record(**kwargs):
        recorded.append(kwargs)
        return True

    monkeypatch.setattr("src.audit.runtime.log_integration_event", record)
    owner = await owner_login(client)
    await check_and_enable(client)
    revoked = await client.post("/api/capabilities/github/activity/revoke", headers=HEADERS,
                                json={"expected_revision": 1})
    assert revoked.status_code == 200 and revoked.json()["audit_recorded"] is True
    (event,) = [item for item in recorded if item["name"] == "source_revoke"]
    assert event["outcome"] == "revoked" and event["principal_id"] == owner["principal_id"]
    assert event["session_id"] == owner["session_id"]
    assert event["details"]["owner_identity_id"] == owner["identity_id"]
    assert event["details"]["reason_code"] == "revoked_by_owner"
    stale = await client.post("/api/capabilities/github/activity/revoke", headers=HEADERS,
                              json={"expected_revision": 1})
    assert stale.status_code == 409
    assert len([item for item in recorded if item["name"] == "source_revoke"]) == 1
