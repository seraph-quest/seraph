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
from tests.test_operator_identity import HEADERS, auth, login  # noqa: F401

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
    await login(client)
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
    await login(client)
    checked = (await client.post("/api/capabilities/github/activity/gh/check", headers=HEADERS,
                                 json={"acknowledge_gh_login_check": True})).json()
    world.user = user(303, "intruder")
    response = await client.put("/api/capabilities/github/activity/source", headers=HEADERS, json={
        "expected_revision": 0, "scope": SCOPE, "acknowledge_gh_ambient_login": True,
        "expected_identity_id": checked["identity_id"], "expected_gh_entry_path": checked["gh_entry_path"]})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "gh_identity_changed"


@pytest.mark.asyncio
async def test_a_new_login_does_not_inherit_the_source_and_the_old_root_blocks(client, async_db, clock,
                                                                              gh_service, runner):
    first = await login(client)
    await check_and_enable(client)
    await client.post("/api/auth/logout", headers=HEADERS)
    await login(client)
    runner.calls.clear()
    result = (await client.post("/api/capabilities/github/activity/collect", headers=HEADERS, json={})).json()
    # A new password login is a new principal: it has no source of its own.
    assert result["status"] == "not_connected" and runner.calls == []
    # The enabling root is no longer live, so the old principal's source blocks before gh starts.
    old = await github_activity._service().collect_day(
        owner_principal_id=first["principal_id"], report_day=NOW.date(),
        tz=github_activity.user_timezone(), trigger="scheduled")
    assert old["status"] == "blocked" and old["reason_code"] == "owner_session_invalid"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_effective_grants_show_both_rows_and_revoke_switches_the_source_off(client, async_db, clock,
                                                                                  gh_service, runner):
    await login(client)
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
    async with db_engine.get_session() as db:
        row = await db.get(OperatorSession, session_id)
        assert row.revoked_at is not None


@pytest.mark.asyncio
async def test_an_idle_or_absolute_expired_login_does_not_stop_the_source(client, async_db, clock, gh_service, runner):
    """No time-based gate: only real changes stop the source (operator's standing rule)."""
    owner = await login(client)
    await check_and_enable(client)
    await _expire_session_row(owner["session_id"])
    runner.calls.clear()
    result = await github_activity._service().collect_day(
        owner_principal_id=owner["principal_id"], report_day=NOW.date(), tz=github_activity.user_timezone(),
        trigger="scheduled")
    assert result["status"] == "complete", result
    assert runner.endpoints[0] == "/user"


@pytest.mark.asyncio
async def test_an_explicit_logout_stops_the_source(client, async_db, clock, gh_service, runner):
    owner = await login(client)
    await check_and_enable(client)
    assert (await client.post("/api/auth/logout", headers=HEADERS)).status_code in {200, 204}
    runner.calls.clear()
    result = await github_activity._service().collect_day(
        owner_principal_id=owner["principal_id"], report_day=NOW.date(), tz=github_activity.user_timezone(),
        trigger="scheduled")
    assert result["status"] == "blocked" and result["reason_code"] == "owner_session_invalid"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_live_owner_session_is_keyed_to_real_changes(client, async_db, clock):
    from datetime import timedelta
    from sqlalchemy import update
    from src.db import engine as db_engine
    from src.db.models import OperatorSession
    from src.integrations.github_activity import GitHubActivityError, live_owner_session
    owner = await login(client)
    await live_owner_session(owner["session_id"], owner["principal_id"])
    for bad_principal in ("operator:root:someone-else", ""):
        with pytest.raises(GitHubActivityError):
            await live_owner_session(owner["session_id"], bad_principal)
    with pytest.raises(GitHubActivityError):
        await live_owner_session("no-such-session", owner["principal_id"])
    future = datetime.now(timezone.utc) + timedelta(days=1)
    async with db_engine.get_session() as db:
        await db.execute(update(OperatorSession).where(OperatorSession.id == owner["session_id"]).values(
            is_bearer_tombstone=True, idle_expires_at=future, absolute_expires_at=future))
    with pytest.raises(GitHubActivityError):
        await live_owner_session(owner["session_id"], owner["principal_id"])


@pytest.mark.asyncio
async def test_items_route_refuses_out_of_range_dates(client, async_db, clock, gh_service, runner):
    await login(client)
    await check_and_enable(client)
    assert (await client.get("/api/capabilities/github/activity/items?report_date=9999-12-31")).status_code == 422
    assert (await client.get("/api/capabilities/github/activity/items?report_date=2026-02-30")).status_code == 422
    collect = await client.post("/api/capabilities/github/activity/collect", headers=HEADERS,
                                json={"report_date": "9999-12-31"})
    assert collect.status_code == 422 and collect.json()["detail"]["code"] == "report_date_invalid"
