"""GitHub activity source (#1264 slice 1): window, attribution, collection, storage, consent, degraded states.

Every test injects FakeGhRunner (no real gh, no network) and a session check,
uses the ``db`` fixture, and moves time only through an installed FakeClock.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from src.db import engine as db_engine
from src.db.models import GitHubActivityItem, GitHubActivityRun, GitHubActivitySource
from src.integrations import github_activity
from src.integrations.github_activity import (
    GitHubActivityError, GitHubActivityScope, GitHubActivityService, local_day_window,
)
from src.integrations.github_cli import GhCliReader
from src.utils import clock as clock_module
from src.utils.clock import FakeClock
from tests.github_activity_fixture import FakeGhRunner, FakeGitHubWorld, Outcome, http_output, make_reader, user
from tests.support.clock import install_clock
from tests.support.db import install_session_factory
from tests.support.sync import Gate, Signal

WARSAW = ZoneInfo("Europe/Warsaw")
DAY = date(2026, 11, 17)
START = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)
END = datetime(2026, 11, 17, 23, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc)  # 21:00 in Warsaw
OWNER = "identity-a"
ROOT = "root-a"
REPO = "seraph-quest/seraph"
SHA = [f"{index:040x}" for index in range(1, 40)]


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex

    def denied(sock, address):
        if isinstance(address, tuple):
            raise AssertionError("Every network socket is forbidden")
        return connect(sock, address)

    def denied_ex(sock, address):
        if isinstance(address, tuple):
            raise AssertionError("Every network socket is forbidden")
        return connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied_ex)


@pytest.fixture
def clock():
    fake = FakeClock(start=NOW)
    previous = install_clock(fake)
    try:
        yield fake
    finally:
        clock_module.set_clock(previous)


class Sessions:
    """The injected owner check: the owner holds authority until the test revokes it."""

    def __init__(self) -> None:
        self.dead: set[str] = set()

    async def __call__(self, source) -> None:
        if source.owner_identity_id in self.dead or source.owner_session_id in self.dead:
            raise GitHubActivityError("owner_identity_revoked", status_code=403)


@pytest.fixture
def world():
    value = FakeGitHubWorld()
    value.repo(REPO, 1)
    return value


@pytest.fixture
def runner(world):
    return FakeGhRunner(world)


@pytest.fixture
def sessions():
    return Sessions()


@pytest.fixture
def service(tmp_path, runner, sessions):
    return GitHubActivityService(reader=make_reader(tmp_path, runner), session_check=sessions)


async def enable(service, *, scope=None, model_context=True, expected_revision=0):
    checked = await service.check_gh_login()
    return await service.enable_source(
        owner_identity_id=OWNER, enabled_principal_id="principal-a", owner_session_id=ROOT, expected_revision=expected_revision,
        scope=scope or GitHubActivityScope(), acknowledged=True, expected_identity_id=checked["identity_id"],
        expected_gh_entry_path=checked["gh_entry_path"], allow_model_context=model_context)


async def collect(service, day=DAY, tz=WARSAW):
    return await service.collect_day(owner_identity_id=OWNER, report_day=day, tz=tz, trigger="operator")


async def items():
    async with db_engine.get_session() as db:
        return (await db.execute(select(GitHubActivityItem).order_by(GitHubActivityItem.occurred_at))).scalars().all()


async def source_row():
    async with db_engine.get_session() as db:
        return (await db.execute(select(GitHubActivitySource))).scalars().first()


def at(hours: float) -> datetime:
    """An instant ``hours`` after the Warsaw day starts."""
    return START + timedelta(hours=hours)


# -- window ----------------------------------------------------------------------


def test_local_day_window_is_half_open_in_user_timezone():
    assert local_day_window(DAY, WARSAW) == (START, END)


def test_local_day_window_on_dst_days():
    start, end = local_day_window(date(2026, 3, 29), WARSAW)
    assert (start, end) == (datetime(2026, 3, 28, 23, tzinfo=timezone.utc), datetime(2026, 3, 29, 22, tzinfo=timezone.utc))
    assert end - start == timedelta(hours=23)
    start, end = local_day_window(date(2026, 10, 25), WARSAW)
    assert (start, end) == (datetime(2026, 10, 24, 22, tzinfo=timezone.utc), datetime(2026, 10, 25, 23, tzinfo=timezone.utc))
    assert end - start == timedelta(hours=25)


@pytest.mark.asyncio
async def test_items_at_the_day_boundaries_land_on_exactly_one_day(db, clock, service, world):
    clock.advance(timedelta(days=2).total_seconds())
    world.repo(REPO, 1)
    world.commit(REPO, SHA[0], START - timedelta(seconds=1))
    world.commit(REPO, SHA[1], START)
    world.commit(REPO, SHA[2], END - timedelta(seconds=1))
    world.commit(REPO, SHA[3], END)
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    for day in (DAY - timedelta(days=1), DAY, DAY + timedelta(days=1)):
        assert (await collect(service, day))["status"] == "complete"
    days = {}
    for day in (DAY - timedelta(days=1), DAY, DAY + timedelta(days=1)):
        view = await service.activity_for_day(owner_identity_id=OWNER, report_day=day, tz=WARSAW)
        days[day] = [item["external_id"] for item in view["items"]]
    assert days == {DAY - timedelta(days=1): [SHA[0]], DAY: [SHA[1], SHA[2]], DAY + timedelta(days=1): [SHA[3]]}


@pytest.mark.asyncio
async def test_collection_stops_at_now_for_the_current_day(db, clock, service, world, runner):
    world.commit(REPO, SHA[0], at(1))
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    result = await collect(service)
    assert result["run"]["collected_until_utc"] == NOW.isoformat()
    commits = [endpoint for endpoint in runner.endpoints if "/commits?" in endpoint]
    assert commits and all("until=2026-11-17T20:00:00Z" in endpoint for endpoint in commits)
    with pytest.raises(GitHubActivityError) as raised:
        await collect(service, DAY + timedelta(days=1))
    assert raised.value.code == "report_day_in_future"


@pytest.mark.asyncio
async def test_only_the_gh_account_is_attributed(db, clock, service, world):
    other = user(202, "someone")
    world.event(REPO, at(1))
    world.issue(REPO, 1, created=at(2), pull=True)
    world.issue(REPO, 2, author=other, created=at(2), pull=True, merged=at(3), closed=at(3))
    world.issue(REPO, 3, author=other, created=at(2))
    world.commit(REPO, SHA[0], at(1))
    world.commit(REPO, SHA[1], at(1), author=other)
    await enable(service)
    await collect(service)
    stored = await items()
    assert {(row.kind, row.number or row.external_id) for row in stored} == {("pr_opened", 1), ("commit", SHA[0])}
    assert {row.actor_id for row in stored} == {101}


# -- collection and storage --------------------------------------------------------


@pytest.mark.asyncio
async def test_collects_each_kind_with_constructed_urls_and_provenance(db, clock, service, world):
    world.event(REPO, at(1), ref="refs/heads/feat/x")
    world.issue(REPO, 10, created=at(2), title="Open PR\nwith a second line", pull=True)
    world.issue(REPO, 11, created=at(-48), pull=True, merged=at(3), closed=at(3))
    world.issue(REPO, 12, created=at(-48), pull=True, closed=at(4))
    world.issue(REPO, 13, created=at(5))
    world.issue(REPO, 14, created=at(-48), closed=at(6))
    world.commit(REPO, SHA[0], at(1), message="fix: thing\n\nbody")
    world.commit(REPO, SHA[1], at(2), branch="feat/x")
    world.commit(REPO, SHA[2], at(2.5), pull=10)
    await enable(service)
    result = await collect(service)
    assert result["status"] == "complete", result
    stored = {(row.kind, row.number or row.external_id): row for row in await items()}
    assert set(stored) == {("pr_opened", 10), ("pr_merged", 11), ("pr_closed", 12), ("issue_opened", 13),
                           ("issue_closed", 14), ("commit", SHA[0]), ("commit", SHA[1]), ("commit", SHA[2])}
    assert stored[("pr_opened", 10)].html_url == f"https://github.com/{REPO}/pull/10"
    assert stored[("pr_opened", 10)].title == "Open PR"
    assert stored[("issue_opened", 13)].html_url == f"https://github.com/{REPO}/issues/13"
    assert stored[("commit", SHA[0])].html_url == f"https://github.com/{REPO}/commit/{SHA[0]}"
    assert stored[("commit", SHA[0])].title == "fix: thing"
    assert all("evil.example" not in row.html_url for row in stored.values())
    commit = stored[("commit", SHA[0])]
    assert commit.repository_id == 1 and commit.api_path == "/repos/{repository}/commits"
    assert commit.first_run_id == result["run"]["id"] and len(commit.content_digest) == 64
    assert stored[("commit", SHA[2])].api_path == "/repos/{repository}/pulls/{number}/commits"
    assert result["run"]["counts"] == {"commit": 3, "issue_closed": 1, "issue_opened": 1, "pr_closed": 1,
                                       "pr_merged": 1, "pr_opened": 1}


@pytest.mark.asyncio
async def test_recollecting_a_day_is_idempotent_and_updates_titles(db, clock, service, world):
    item = world.issue(REPO, 10, created=at(2), pull=True, title="First title")
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    first = await collect(service)
    item["title"] = "Second title"
    clock.advance(60)
    second = await collect(service)
    (row,) = await items()
    assert row.title == "Second title"
    assert row.first_run_id == first["run"]["id"] and row.last_run_id == second["run"]["id"]
    assert row.last_seen_at > row.first_seen_at


@pytest.mark.asyncio
async def test_same_commit_on_default_and_pr_branch_is_stored_once(db, clock, service, world):
    world.event(REPO, at(1), ref="refs/heads/feat/x")
    world.issue(REPO, 10, created=at(-48), updated=at(2), pull=True)
    for branch in ("main", "feat/x"):
        world.commit(REPO, SHA[0], at(1), branch=branch)
    world.commit(REPO, SHA[0], at(1), pull=10)
    await enable(service)
    await collect(service)
    assert [row.external_id for row in await items()] == [SHA[0]]


@pytest.mark.asyncio
async def test_reopened_then_closed_item_keeps_both_closures(db, clock, service, world):
    item = world.issue(REPO, 14, created=at(-48), closed=at(2))
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    await collect(service)
    item.update(closed_at=at(5).strftime("%Y-%m-%dT%H:%M:%SZ"), updated_at=at(5).strftime("%Y-%m-%dT%H:%M:%SZ"))
    clock.advance(60)
    await collect(service)
    assert [row.kind for row in await items()] == ["issue_closed", "issue_closed"]


@pytest.mark.asyncio
async def test_default_scope_reads_only_repositories_with_operator_events(db, clock, service, world, runner):
    world.repo("seraph-quest/other", 2)
    world.event("seraph-quest/other", at(1), actor=user(202, "someone"))
    world.event(REPO, at(1))
    world.event("seraph-quest/old", at(-5))
    await enable(service)
    await collect(service)
    read = {endpoint.split("?")[0] for endpoint in runner.endpoints if endpoint.startswith("/repos/")}
    assert read == {f"/repos/{REPO}", f"/repos/{REPO}/issues", f"/repos/{REPO}/commits"}


@pytest.mark.asyncio
async def test_explicit_repository_is_read_without_any_event(db, clock, service, world, runner):
    world.repo("acme/tools", 3)
    world.commit("acme/tools", SHA[0], at(1))
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=["acme/tools"]))
    result = await collect(service)
    assert result["run"]["repositories"]["read"] == ["acme/tools"]
    assert not any("/events" in endpoint for endpoint in runner.endpoints)
    assert [row.repository for row in await items()] == ["acme/tools"]


@pytest.mark.asyncio
async def test_owner_filter_and_exclusions_apply_to_discovery(db, clock, service, world, runner):
    for index, name in enumerate(("seraph-quest/a", "seraph-quest/b", "elsewhere/c"), start=10):
        world.repo(name, index)
        world.event(name, at(1))
    await enable(service, scope=GitHubActivityScope(owners=["seraph-quest"], exclude_repositories=["seraph-quest/b"]))
    result = await collect(service)
    assert result["run"]["repositories"]["read"] == ["seraph-quest/a"]
    assert not any(name in endpoint for endpoint in runner.endpoints for name in ("/seraph-quest/b", "/elsewhere/c"))


@pytest.mark.asyncio
async def test_repository_cap_and_call_budget_end_partial_with_named_limitations(db, clock, service, world, monkeypatch):
    for index in range(12):
        name = f"seraph-quest/r{index:02d}"
        world.repo(name, 100 + index)
        world.event(name, at(1))
    await enable(service)
    result = await collect(service)
    assert result["status"] == "partial"
    assert {"code": "repository_cap_reached"} in result["run"]["limitations"]
    assert len(result["run"]["repositories"]["read"]) == 10
    assert len(result["run"]["repositories"]["skipped"]) == 2
    monkeypatch.setattr(github_activity, "MAX_CALLS_PER_RUN", 4)
    clock.advance(60)
    budget = await collect(service)
    assert budget["status"] == "partial" and budget["reason_code"] == "call_budget_exhausted"
    assert budget["run"]["call_count"] == 4
    assert {"code": "call_budget_exhausted"} in budget["run"]["limitations"]


@pytest.mark.asyncio
async def test_pagination_stops_at_the_window_and_reports_truncation(db, clock, service, world, runner):
    for index in range(160):
        world.commit(REPO, f"{index + 1:040x}", at(1) + timedelta(seconds=index))
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    result = await collect(service)
    pages = [endpoint for endpoint in runner.endpoints if "/commits?" in endpoint]
    assert len(pages) == 3
    assert result["status"] == "partial"
    assert {"code": "commits_truncated", "repository": REPO} in result["run"]["limitations"]
    assert len(await items()) == 150


@pytest.mark.asyncio
async def test_run_row_records_the_binary_and_flags_a_changed_hash_without_blocking(db, clock, tmp_path, service, world):
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    first = await collect(service)
    gh = tmp_path / "gh-prefix" / "bin" / "gh"
    assert first["run"]["gh_real_path"] == str(gh.resolve()) and len(first["run"]["gh_sha256"]) == 64
    gh.write_text("#!/bin/sh\n# upgraded\n", encoding="utf-8")
    clock.advance(60)
    second = await collect(service)
    assert second["status"] == "complete"
    assert {"code": "gh_binary_changed_since_last_run"} in second["run"]["limitations"]
    assert second["run"]["gh_sha256"] != first["run"]["gh_sha256"]


# -- consent and authority ----------------------------------------------------------


@pytest.mark.asyncio
async def test_source_is_off_by_default_and_nothing_starts_gh(db, clock, service, runner):
    status = await service.status(owner_identity_id=OWNER)
    assert status["source"]["state"] == "not_connected" and status["gh"]["available"] is True
    assert status["credential"] == {"kind": "local_gh_cli_login", "scope": "full_gh_scope",
                                    "seraph_requests": "get_only_allowlist"}
    result = await collect(service)
    assert result["status"] == "not_connected" and result["message"] == "GitHub source not connected."
    assert runner.calls == []


@pytest.mark.asyncio
async def test_check_stores_nothing(db, clock, service, runner):
    checked = await service.check_gh_login()
    assert checked["identity_login"] == "octo" and checked["gh_version"] == "2.89.0"
    assert [call["endpoint"] for call in runner.calls] == [None, "/user"]
    assert await source_row() is None


@pytest.mark.asyncio
async def test_enable_requires_the_ambient_login_acknowledgement(db, clock, service, runner):
    checked = await service.check_gh_login()
    with pytest.raises(GitHubActivityError) as raised:
        await service.enable_source(owner_identity_id=OWNER, enabled_principal_id="principal-a", owner_session_id=ROOT, expected_revision=0,
                                    scope=GitHubActivityScope(), acknowledged=False,
                                    expected_identity_id=checked["identity_id"],
                                    expected_gh_entry_path=checked["gh_entry_path"], allow_model_context=False)
    assert raised.value.code == "gh_ambient_login_acknowledgement_required"
    assert await source_row() is None


@pytest.mark.asyncio
async def test_enable_pins_the_identity_the_operator_was_shown(db, clock, service, world):
    checked = await service.check_gh_login()
    world.user = user(303, "intruder")
    with pytest.raises(GitHubActivityError) as raised:
        await service.enable_source(owner_identity_id=OWNER, enabled_principal_id="principal-a", owner_session_id=ROOT, expected_revision=0,
                                    scope=GitHubActivityScope(), acknowledged=True,
                                    expected_identity_id=checked["identity_id"],
                                    expected_gh_entry_path=checked["gh_entry_path"], allow_model_context=False)
    assert raised.value.code == "gh_identity_changed" and raised.value.status_code == 409
    assert await source_row() is None


@pytest.mark.asyncio
async def test_toggle_off_stops_all_gh_starts(db, clock, service, runner):
    status = await enable(service)
    await service.revoke_source(owner_identity_id=OWNER, expected_revision=status["source"]["revision"])
    runner.calls.clear()
    result = await collect(service)
    assert result["status"] == "revoked" and runner.calls == []
    assert (await source_row()).state == "revoked"


@pytest.mark.asyncio
async def test_toggle_off_during_a_call_discards_that_result(db, clock, service, world, runner):
    world.commit(REPO, SHA[0], at(1))
    # Read (and kept in memory) before the parked call: it must not be stored either.
    world.issue(REPO, 10, created=at(2), pull=True)
    status = await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    gate = Gate("commits call")

    def park(call):
        if call["endpoint"] and "/commits?" in call["endpoint"]:
            gate.arrive_blocking()

    runner.on_call = park
    task = asyncio.create_task(collect(service))
    await gate.wait_reached(task=task)
    await service.revoke_source(owner_identity_id=OWNER, expected_revision=status["source"]["revision"])
    gate.release()
    result = await task
    assert result["status"] == "blocked" and result["reason_code"] == "source_revoked"
    assert any(endpoint.startswith(f"/repos/{REPO}/issues") for endpoint in runner.endpoints)
    assert await items() == []
    assert result["run"]["counts"] == {}
    assert not any("/commits?" in endpoint for endpoint in runner.endpoints[runner.endpoints.index(
        next(e for e in runner.endpoints if "/commits?" in e)) + 1:])


@pytest.mark.asyncio
async def test_login_change_stops_the_source_and_later_runs_do_not_start_gh(db, clock, service, world, runner):
    await enable(service)
    world.user = user(303, "other-account")
    runner.calls.clear()
    result = await collect(service)
    assert result["status"] == "blocked" and result["reason_code"] == "gh_login_changed"
    assert runner.endpoints == ["/user"]
    assert "other-account" in result["message"] and "@octo" in result["message"]
    row = await source_row()
    assert (row.state, row.stopped_reason, row.revision) == ("stopped", "gh_login_changed", 2)
    runner.calls.clear()
    later = await collect(service)
    assert later["status"] == "stopped" and runner.calls == []


@pytest.mark.asyncio
async def test_same_account_logged_out_then_in_resumes_without_re_enabling(db, clock, service, runner):
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    runner.inject("/user", Outcome(exit_code=4, stdout=""))
    logged_out = await collect(service)
    assert logged_out["status"] == "blocked" and logged_out["reason_code"] == "gh_not_logged_in"
    assert "gh auth login" in logged_out["message"]
    clock.advance(60)
    assert (await collect(service))["status"] == "complete"


@pytest.mark.asyncio
async def test_dead_owner_session_blocks(db, clock, service, runner, sessions):
    await enable(service)
    sessions.dead.add(ROOT)
    runner.calls.clear()
    result = await collect(service)
    assert result["status"] == "blocked" and result["reason_code"] == "owner_identity_revoked"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_repository_outside_scope_is_refused_by_the_reader_call(db, clock, service, runner):
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    source = await source_row()
    collection = github_activity._Collection(service=service, source=source, scope=github_activity._scope(source),
                                             binary=service._reader.locate(), run_id="run", start=START, until=NOW,
                                             now=NOW)
    runner.calls.clear()
    with pytest.raises(github_activity._RunEnded) as raised:
        await collection.call("repository", repository="acme/not-chosen", discovered=False)
    assert raised.value.reason_code == "repository_outside_scope" and runner.calls == []
    with pytest.raises(github_activity._RunEnded):
        await collection.call("repository", repository="acme/not-chosen", discovered=True)
    assert runner.calls == []


@pytest.mark.asyncio
async def test_scope_edit_needs_the_acknowledgement_again_and_bumps_revision(db, clock, service):
    first = await enable(service)
    with pytest.raises(GitHubActivityError) as stale:
        await enable(service, scope=GitHubActivityScope(owners=["seraph-quest"]), expected_revision=0)
    assert stale.value.code == "github_activity_revision_conflict"
    second = await enable(service, scope=GitHubActivityScope(owners=["seraph-quest"]),
                          expected_revision=first["source"]["revision"])
    assert second["source"]["revision"] == first["source"]["revision"] + 1
    assert second["source"]["scope"]["owners"] == ["seraph-quest"]


def test_scope_needs_something_to_read():
    with pytest.raises(ValueError):
        GitHubActivityScope(discover_from_activity=False)
    with pytest.raises(ValueError):
        GitHubActivityScope(repositories=["not a repo"])


# -- degraded ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_trusted_gh_is_gh_unavailable(db, clock, tmp_path, runner, sessions):
    missing = GitHubActivityService(reader=GhCliReader(runner=runner, candidates=(str(tmp_path / "none" / "gh"),)),
                                    session_check=sessions)
    status = await missing.status(owner_identity_id=OWNER)
    assert status["gh"] == {"available": False, "reason_code": "gh_unavailable", "entry_path": None}
    with pytest.raises(GitHubActivityError) as raised:
        await missing.check_gh_login()
    assert raised.value.code == "gh_unavailable" and runner.calls == []


@pytest.mark.asyncio
async def test_default_reader_with_no_gh_starts_nothing(db, clock, tmp_path, monkeypatch):
    from src.integrations import github_cli
    monkeypatch.setattr(github_cli, "GH_CANDIDATE_PATHS", (str(tmp_path / "none" / "gh"),))
    started = []
    monkeypatch.setattr(github_cli.process_tools, "run_owned_argv", lambda *a, **k: started.append(a))
    service = GitHubActivityService(reader=GhCliReader(candidates=github_cli.GH_CANDIDATE_PATHS))
    assert (await service.status(owner_identity_id=OWNER))["gh"]["reason_code"] == "gh_unavailable"
    with pytest.raises(GitHubActivityError):
        await service.check_gh_login()
    assert started == []


@pytest.mark.asyncio
async def test_binary_removed_after_enable_blocks_without_a_start(db, clock, tmp_path, service, runner):
    await enable(service)
    (tmp_path / "gh-prefix" / "bin" / "gh").unlink()
    runner.calls.clear()
    result = await collect(service)
    assert result["status"] == "blocked" and result["reason_code"] == "gh_unavailable"
    assert "directly on your computer" in result["message"] and runner.calls == []


@pytest.mark.asyncio
async def test_401_fails_with_credential_rejected(db, clock, service, runner):
    await enable(service)
    runner.inject("/user", Outcome(exit_code=1, stdout=http_output(401, {"message": "Bad credentials"})))
    result = await collect(service)
    assert result["status"] == "failed" and result["reason_code"] == "github_activity_credential_rejected"
    assert "gh auth login" in result["message"]


@pytest.mark.asyncio
async def test_timeout_ends_the_run_without_a_second_call(db, clock, service, runner):
    await enable(service)
    runner.inject("/user", Outcome(exit_code=None, timed_out=True, cleanup_status="stopped"))
    runner.calls.clear()
    result = await collect(service)
    assert result["reason_code"] == "gh_timed_out" and len(runner.calls) == 1
    assert "30 s" in result["message"]


@pytest.mark.asyncio
async def test_unproven_cleanup_ends_the_run_and_is_reported(db, clock, service, runner):
    await enable(service)
    runner.inject("/user", Outcome(exit_code=None, timed_out=True, cleanup_status="unknown"))
    runner.calls.clear()
    result = await collect(service)
    assert result["reason_code"] == "gh_cleanup_unproven" and len(runner.calls) == 1


@pytest.mark.asyncio
async def test_rate_limit_keeps_items_read_so_far_and_does_not_retry(db, clock, service, world, runner):
    world.repo("acme/second", 2)
    world.commit(REPO, SHA[0], at(1))
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO, "acme/second"]))
    runner.inject("/repos/acme/second/issues", Outcome(exit_code=1, stdout=http_output(
        403, {}, {"X-Ratelimit-Remaining": "0", "X-Ratelimit-Reset": str(int((NOW + timedelta(hours=1)).timestamp()))})))
    result = await collect(service)
    assert result["status"] == "partial" and result["reason_code"] == "github_activity_rate_limited"
    assert [endpoint for endpoint in runner.endpoints if "acme/second/issues" in endpoint].__len__() == 1
    assert runner.endpoints[-1].startswith("/repos/acme/second/issues")
    assert [row.external_id for row in await items()] == [SHA[0]]
    assert result["run"]["rate_limit_reset_at"] == (NOW + timedelta(hours=1)).isoformat()


@pytest.mark.asyncio
async def test_remaining_zero_stops_before_the_next_call(db, clock, service, world, runner):
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    world.override(f"/repos/{REPO}", 200, world.repos[REPO.lower()], {"X-Ratelimit-Remaining": "0"})
    result = await collect(service)
    assert runner.endpoints[-1] == f"/repos/{REPO}"
    assert result["status"] == "failed" and result["reason_code"] == "github_activity_rate_limited"


@pytest.mark.asyncio
async def test_one_repository_denied_is_partial_and_names_it(db, clock, service, world, runner):
    world.repo("acme/secret", 2)
    world.commit(REPO, SHA[0], at(1))
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=["acme/secret", REPO]))
    runner.inject("/repos/acme/secret", Outcome(exit_code=1, stdout=http_output(403, {"message": "SSO"})))
    result = await collect(service)
    assert result["status"] == "partial"
    assert result["run"]["repositories"]["failed"] == [{"repository": "acme/secret",
                                                        "code": "github_activity_access_denied"}]
    assert "acme/secret: access denied" in result["message"]
    assert len(await items()) == 1


@pytest.mark.asyncio
async def test_malformed_page_fails_as_response_invalid(db, clock, service, world):
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    world.override(f"/repos/{REPO}/issues", 200, [{"number": "seven"}])
    result = await collect(service)
    assert result["status"] == "failed" and result["reason_code"] == "github_activity_response_invalid"


@pytest.mark.asyncio
async def test_storage_failure_is_reported_and_not_raised(db, clock, service, world, monkeypatch):
    world.commit(REPO, SHA[0], at(1))
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    real_factory = db_engine.async_session_factory
    fired = []

    class FailingItems:
        def __init__(self):
            self._context = real_factory()

        async def __aenter__(self):
            self._session = await self._context.__aenter__()
            real_execute = self._session.execute

            async def execute(statement, *args, **kwargs):
                if "INSERT INTO github_activity_items" in str(statement):
                    fired.append(True)
                    raise RuntimeError("injected storage failure")
                return await real_execute(statement, *args, **kwargs)

            self._session.execute = execute
            return self._session

        async def __aexit__(self, *exc):
            return await self._context.__aexit__(*exc)

    install_session_factory(monkeypatch, FailingItems)
    result = await collect(service)
    assert fired == [True]
    assert result["status"] == "failed" and result["reason_code"] == "github_activity_storage_error"


@pytest.mark.asyncio
async def test_interrupted_run_stays_running_and_is_reported_literally(db, clock, service):
    await enable(service)
    source = await source_row()
    async with db_engine.get_session() as session:
        session.add(GitHubActivityRun(owner_identity_id=OWNER, source_id=source.id, source_revision=1,
                                      trigger="scheduled", report_date=DAY.isoformat(), timezone="Europe/Warsaw",
                                      window_start_utc=START, window_end_utc=END, collected_until_utc=NOW,
                                      started_at=NOW - timedelta(hours=1)))
    view = await service.activity_for_day(owner_identity_id=OWNER, report_day=DAY, tz=WARSAW)
    assert view["status"] == "running"
    assert view["message"] == "A GitHub collection started at 20:00 and did not record completion."


@pytest.mark.asyncio
async def test_not_collected_and_complete_zero_are_distinct(db, clock, service):
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    view = await service.activity_for_day(owner_identity_id=OWNER, report_day=DAY, tz=WARSAW)
    assert view["status"] == "not_collected"
    result = await collect(service)
    assert result["status"] == "complete" and "no activity found" in result["message"]


@pytest.mark.asyncio
async def test_cancelled_collection_kills_and_reaps_its_child(db, clock, service, runner):
    await enable(service)
    reached, released = Signal("runner parked"), threading.Event()
    seen = {}

    def park(call):
        if call["endpoint"] == "/user":
            seen["cancel_event"] = call["cancel_event"]
            reached.set()
            # Parked until the reader sets the cancel event (the child kill path).
            assert call["cancel_event"].wait(timeout=60)
            released.set()

    runner.calls.clear()
    runner.on_call = park
    task = asyncio.create_task(collect(service))
    await reached.wait(task=task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert released.is_set() and seen["cancel_event"].is_set()


@pytest.mark.asyncio
async def test_events_discovery_is_marked_as_possibly_lagging_and_old_days_are_partial(db, clock, service):
    await enable(service)
    today = await collect(service)
    assert {"code": "events_discovery_may_lag"} in today["run"]["limitations"]
    old = await collect(service, DAY - timedelta(days=40))
    assert old["status"] == "partial"
    assert {"code": "events_window_unavailable"} in old["run"]["limitations"]


@pytest.mark.asyncio
async def test_audit_event_is_written_per_run(db, clock, service, monkeypatch):
    recorded = []

    async def record(**kwargs):
        recorded.append(kwargs)
        return True

    monkeypatch.setattr("src.audit.runtime.log_integration_event", record)
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    await collect(service)
    (event,) = recorded
    assert event["integration_type"] == "github_activity" and event["name"] == "gh_cli"
    assert event["outcome"] == "complete" and event["details"]["credential"] == "local_gh_cli_login"
    assert "token" not in json.dumps(event).lower()


async def park_at(runner, endpoint_prefix):
    gate = Gate(endpoint_prefix)

    def park(call):
        if call["endpoint"] and call["endpoint"].split("?")[0] == endpoint_prefix:
            gate.arrive_blocking()

    runner.on_call = park
    return gate


def calls_after(runner, endpoint_prefix):
    endpoints = [endpoint.split("?")[0] for endpoint in runner.endpoints]
    return endpoints[endpoints.index(endpoint_prefix) + 1:]


@pytest.mark.asyncio
async def test_revoke_after_the_last_call_drops_items_at_the_storage_guard(db, clock, service, world, monkeypatch):
    world.commit(REPO, SHA[0], at(1))
    status = await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    real_record = service._record_binary

    async def revoke_then_record(run_id, binary):
        # Every gh call has finished and passed its checks; the source changes now.
        await service.revoke_source(owner_identity_id=OWNER, expected_revision=status["source"]["revision"])
        await real_record(run_id, binary)

    monkeypatch.setattr(service, "_record_binary", revoke_then_record)
    result = await collect(service)
    assert result["status"] == "blocked" and result["reason_code"] == "source_changed"
    assert await items() == []


@pytest.mark.asyncio
async def test_root_death_between_two_calls_stops_the_run_before_the_next_start(db, clock, service, world, runner,
                                                                                sessions):
    world.issue(REPO, 10, created=at(2), pull=True)
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))

    def kill_root(call):
        if call["endpoint"] == f"/repos/{REPO}":
            sessions.dead.add(ROOT)

    runner.on_call = kill_root
    runner.calls.clear()
    result = await collect(service)
    assert result["status"] == "blocked" and result["reason_code"] == "owner_identity_revoked"
    assert calls_after(runner, f"/repos/{REPO}") == []
    assert await items() == []


@pytest.mark.asyncio
async def test_pre_run_session_check_alone_blocks_before_any_start(db, clock, service, runner, sessions, monkeypatch):
    await enable(service)

    async def no_per_call_checks(self, op, repository, *, discovered=True):
        return None

    monkeypatch.setattr(github_activity._Collection, "authority", no_per_call_checks)
    sessions.dead.add(ROOT)
    runner.calls.clear()
    result = await collect(service)
    assert result["status"] == "blocked" and result["reason_code"] == "owner_identity_revoked"
    assert runner.calls == []


@pytest.mark.asyncio
async def test_scope_edit_between_two_calls_stops_the_run(db, clock, service, world, runner):
    world.commit(REPO, SHA[0], at(1))
    first = await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    gate = await park_at(runner, f"/repos/{REPO}")
    task = asyncio.create_task(collect(service))
    await gate.wait_reached(task=task)
    edited = await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO],
                                                             owners=["seraph-quest"]),
                          expected_revision=first["source"]["revision"])
    assert edited["source"]["state"] == "active"
    gate.release()
    result = await task
    assert result["status"] == "blocked" and result["reason_code"] == "source_changed"
    # The edit itself checked the login (/user); the run started nothing more.
    assert [endpoint for endpoint in calls_after(runner, f"/repos/{REPO}") if endpoint != "/user"] == []
    assert await items() == []


@pytest.mark.asyncio
async def test_unusual_branch_names_are_read_and_unsendable_ones_skipped(db, clock, service, world, runner):
    world.repo(REPO, 1, default_branch="główna")
    world.event(REPO, at(1), ref="refs/heads/fix/@scope+thing")
    world.event(REPO, at(1.5), ref="refs/heads/bad..name")
    world.commit(REPO, SHA[0], at(1), branch="główna")
    world.commit(REPO, SHA[1], at(2), branch="fix/@scope+thing")
    await enable(service)
    result = await collect(service)
    assert {row.external_id for row in await items()} == {SHA[0], SHA[1]}
    assert result["status"] == "partial" and result["reason_code"] is None
    assert {"code": "branch_name_skipped", "repository": REPO} in result["run"]["limitations"]
    assert any("sha=g%C5%82%C3%B3wna" in endpoint for endpoint in runner.endpoints)
    assert any("sha=fix%2F%40scope%2Bthing" in endpoint for endpoint in runner.endpoints)


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", ["\uff19\uff19", "9" * 40, "-1", "1e9", ""])
async def test_odd_rate_limit_headers_do_not_break_a_run(db, clock, service, world, reset):
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    world.override(f"/repos/{REPO}", 200, world.repos[REPO.lower()],
                   {"X-Ratelimit-Reset": reset, "X-Ratelimit-Remaining": "\uff10"})
    result = await collect(service)
    assert result["status"] == "complete"
    # The odd value is ignored; later well-formed answers still record theirs.
    assert result["run"]["rate_limit_reset_at"] == "2026-09-21T14:13:20+00:00"


@pytest.mark.asyncio
async def test_unexpected_error_ends_the_run_failed_never_running(db, clock, service, monkeypatch):
    await enable(service)
    recorded = []

    async def record(**kwargs):
        recorded.append(kwargs)
        return True

    async def explode(self):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("src.audit.runtime.log_integration_event", record)
    monkeypatch.setattr(github_activity._Collection, "execute", explode)
    result = await collect(service)
    assert result["status"] == "failed" and result["reason_code"] == "github_activity_internal_error"
    async with db_engine.get_session() as session:
        (run,) = (await session.execute(select(GitHubActivityRun))).scalars().all()
    assert run.status == "failed" and run.finished_at is not None
    assert [event["outcome"] for event in recorded] == ["failed"]


@pytest.mark.asyncio
async def test_concurrent_first_enables_leave_one_source_and_one_conflict(db, clock, service):
    results = await asyncio.gather(enable(service), enable(service), return_exceptions=True)
    errors = [value for value in results if isinstance(value, BaseException)]
    assert len(errors) == 1 and isinstance(errors[0], GitHubActivityError)
    assert errors[0].status_code == 409
    async with db_engine.get_session() as session:
        assert len((await session.execute(select(GitHubActivitySource))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_out_of_range_report_date_is_refused(db, clock, service):
    await enable(service)
    for day in (date.max, date.min):
        with pytest.raises(GitHubActivityError) as raised:
            await collect(service, day)
        assert raised.value.code in {"report_date_invalid", "report_day_in_future"}
        assert raised.value.status_code == 422
    with pytest.raises(GitHubActivityError) as raised:
        await service.activity_for_day(owner_identity_id=OWNER, report_day=date.max, tz=WARSAW)
    assert raised.value.code == "report_date_invalid"


@pytest.mark.asyncio
async def test_renamed_repository_updates_stored_urls(db, clock, service, world):
    world.commit(REPO, SHA[0], at(1))
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    await collect(service)
    world.repos[REPO.lower()]["full_name"] = "seraph-quest/renamed"
    clock.advance(60)
    await collect(service)
    (row,) = await items()
    assert row.repository == "seraph-quest/renamed"
    assert row.html_url == f"https://github.com/seraph-quest/renamed/commit/{SHA[0]}"


@pytest.mark.asyncio
async def test_a_refused_value_is_not_a_run_ending_failure(db, clock, service, runner):
    from src.integrations.github_cli import GhValueInvalid
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))
    source = await source_row()
    collection = github_activity._Collection(service=service, source=source, scope=github_activity._scope(source),
                                             binary=service._reader.locate(), run_id="run", start=START, until=NOW,
                                             now=NOW)
    runner.calls.clear()
    with pytest.raises(GhValueInvalid):
        await collection.call("commits", repository=REPO, discovered=False, branch="bad..name", login="octo",
                              since=START, until=NOW)
    assert runner.calls == [] and collection.calls == 0


@pytest.mark.asyncio
async def test_owner_revoked_during_the_last_call_discards_its_result(db, clock, service, world, runner, sessions):
    """Pins the post-call check: nothing later would catch a revoke during the final call."""
    world.commit(REPO, SHA[0], at(1))
    await enable(service, scope=GitHubActivityScope(discover_from_activity=False, repositories=[REPO]))

    def revoke_on_last(call):
        if call["endpoint"] and call["endpoint"].startswith(f"/repos/{REPO}/commits?"):
            sessions.dead.add(OWNER)

    runner.on_call = revoke_on_last
    result = await collect(service)
    assert runner.endpoints[-1].startswith(f"/repos/{REPO}/commits?")
    assert result["status"] == "blocked" and result["reason_code"] == "owner_identity_revoked"
    assert await items() == []


@pytest.mark.asyncio
async def test_an_unsendable_login_from_github_fails_as_value_invalid(db, clock, service, world, runner):
    await enable(service)
    world.user = user(101, "not a login")
    runner.calls.clear()
    result = await collect(service)
    assert result["status"] == "failed" and result["reason_code"] == "github_activity_value_invalid"
    assert runner.endpoints == ["/user"]


@pytest.mark.asyncio
async def test_a_lone_surrogate_branch_is_skipped(db, clock, service, world):
    world.event(REPO, at(1), ref="refs/heads/a\ud800b")
    world.commit(REPO, SHA[0], at(1))
    await enable(service)
    result = await collect(service)
    assert result["status"] == "partial" and result["reason_code"] is None
    assert {"code": "branch_name_skipped", "repository": REPO} in result["run"]["limitations"]
    assert [row.external_id for row in await items()] == [SHA[0]]


@pytest.mark.asyncio
async def test_an_unexpected_runner_error_is_an_internal_failure(db, clock, service, runner):
    await enable(service)
    runner.inject("/user", RuntimeError("unexpected runner bug"))
    result = await collect(service)
    assert result["status"] == "failed" and result["reason_code"] == "github_activity_internal_error"


@pytest.mark.asyncio
async def test_pre_identity_branch_tables_are_set_aside_with_their_rows(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlmodel import SQLModel
    from src.db.engine import _set_aside_pre_identity_github_activity
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.sqlite3'}")
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE github_activity_sources (id VARCHAR PRIMARY KEY, owner_principal_id VARCHAR, state VARCHAR)")
            await conn.exec_driver_sql("CREATE UNIQUE INDEX ix_github_activity_sources_owner_principal_id "
                                       "ON github_activity_sources (owner_principal_id)")
            await conn.exec_driver_sql("CREATE TABLE github_activity_items (id VARCHAR PRIMARY KEY, "
                                       "owner_principal_id VARCHAR, dedupe_key VARCHAR)")
            await conn.exec_driver_sql("CREATE UNIQUE INDEX ux_github_activity_items_dedupe "
                                       "ON github_activity_items (owner_principal_id, dedupe_key)")
            await conn.exec_driver_sql("INSERT INTO github_activity_sources VALUES ('s1', 'p1', 'active')")
        async with engine.begin() as conn:
            await _set_aside_pre_identity_github_activity(conn)
            await conn.run_sync(lambda sync: SQLModel.metadata.create_all(sync, tables=[
                GitHubActivitySource.__table__, GitHubActivityRun.__table__, GitHubActivityItem.__table__]))
            # Idempotent once the new shape exists.
            await _set_aside_pre_identity_github_activity(conn)
        async with engine.begin() as conn:
            kept = (await conn.exec_driver_sql("SELECT id, state FROM github_activity_sources_pre_identity")).fetchall()
            columns = {row[1] for row in (await conn.exec_driver_sql(
                "PRAGMA table_info(github_activity_sources)")).fetchall()}
            fresh = (await conn.exec_driver_sql("SELECT COUNT(*) FROM github_activity_sources")).fetchone()[0]
        assert kept == [("s1", "active")] and "owner_identity_id" in columns and fresh == 0
    finally:
        await engine.dispose()
