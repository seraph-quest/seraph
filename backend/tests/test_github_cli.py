"""The gh reader (#1264): fixed GET argv, scratch environment, trusted binary, typed errors.

No test starts the real gh or reaches the network. Most use the injected
FakeGhRunner; the process-path tests run a fake gh executable in tmp_path.
"""
from __future__ import annotations

import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.integrations import github_cli
from src.integrations.github_cli import OPS, GhCliError, build_endpoint, parse_api_output
from src.tools import process_tools
from tests.github_activity_fixture import (
    API_PREFIX, FakeGhRunner, FakeGitHubWorld, Outcome, fake_gh_executable, http_output, install_gh_file,
    make_reader, read_calls,
)

SINCE = datetime(2026, 11, 16, 23, 0, tzinfo=timezone.utc)
UNTIL = datetime(2026, 11, 17, 20, 0, tzinfo=timezone.utc)
VALUES = {"repository": "seraph-quest/seraph", "login": "octo", "number": 7, "branch": "feat/x",
          "since": SINCE, "until": UNTIL}


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


async def allow(op, repository):
    return None


def op_values(op):
    template, query, _, _ = OPS[op]
    wanted = {}
    if "{repository}" in template:
        wanted["repository"] = VALUES["repository"]
    if "{login}" in template or "author" in query:
        wanted["login"] = VALUES["login"]
    if "{number}" in template:
        wanted["number"] = VALUES["number"]
    if "sha" in query:
        wanted["branch"] = VALUES["branch"]
    if "since" in query:
        wanted["since"] = SINCE
    if "until" in query:
        wanted["until"] = UNTIL
    return wanted


@pytest.mark.asyncio
@pytest.mark.parametrize("op", sorted(OPS))
async def test_argv_is_the_fixed_get_shape_for_every_op(tmp_path, op):
    world = FakeGitHubWorld()
    world.repo("seraph-quest/seraph", 1)
    runner = FakeGhRunner(world)
    reader = make_reader(tmp_path, runner)
    binary = reader.locate()
    await reader.read(binary, op=op, authority_check=allow, **op_values(op))
    (call,) = runner.calls
    assert call["argv"][0] == binary.real_path
    assert call["argv"][1:-1] == API_PREFIX
    assert call["argv"][-1] == build_endpoint(op, **op_values(op))


def test_endpoints_are_built_from_the_closed_table():
    assert build_endpoint("identity") == "/user"
    assert build_endpoint("events", login="octo", page=2) == "/users/octo/events?per_page=50&page=2"
    assert build_endpoint("commits", **op_values("commits")) == (
        "/repos/seraph-quest/seraph/commits?sha=feat%2Fx&author=octo&since=2026-11-16T23:00:00Z"
        "&until=2026-11-17T20:00:00Z&per_page=50&page=1")
    assert build_endpoint("issues", **op_values("issues")) == (
        "/repos/seraph-quest/seraph/issues?state=all&sort=updated&direction=desc"
        "&since=2026-11-16T23:00:00Z&per_page=50&page=1")


@pytest.mark.parametrize("field,value", [
    ("repository", "seraph quest/seraph"), ("repository", "../etc"), ("repository", "a/b?x=1"),
    ("repository", "a/{repo}"), ("repository", "-a/b"), ("repository", "a/b&c"),
    ("login", "oc to"), ("login", "-octo"), ("login", "oc{to}"), ("login", "a" * 40),
    ("branch", "a..b"), ("branch", "a b"), ("branch", "{branch}"), ("branch", "-x"), ("branch", "a?b"),
    ("branch", "a&b"), ("branch", "$(id)"), ("branch", "a;b"),
    ("number", 0), ("number", -1), ("number", "7"), ("number", True),
])
def test_endpoint_values_are_validated_and_encoded(field, value):
    op = {"repository": "pull_commits", "login": "commits", "branch": "commits", "number": "pull_commits"}[field]
    values = op_values(op)
    values[field] = value
    with pytest.raises(GhCliError) as raised:
        build_endpoint(op, **values)
    assert raised.value.code == "github_activity_value_invalid"


@pytest.mark.parametrize("page", [0, 7, "1"])
def test_pages_outside_the_table_are_refused(page):
    with pytest.raises(GhCliError):
        build_endpoint("events", login="octo", page=page)


def test_unknown_op_is_refused():
    with pytest.raises(GhCliError) as raised:
        build_endpoint("graphql")
    assert raised.value.code == "github_activity_op_unknown"


def test_no_op_can_produce_post_fields_pagination_graphql_or_auth():
    forbidden = {"-f", "-F", "--field", "--raw-field", "--input", "--paginate", "--verbose", "--jq", "--template",
                 "--cache", "graphql", "auth", "token", "POST", "PUT", "PATCH", "DELETE"}
    for op in OPS:
        argv = github_cli.api_argv("/opt/homebrew/bin/gh", build_endpoint(op, **op_values(op)))
        assert not forbidden & set(argv), op
        assert argv[1:4] == ["api", "--method", "GET"]
        assert argv[-1].startswith("/") and "://" not in argv[-1]
    # Every path in the table is a read of the operator's own activity.
    assert {template for template, *_ in OPS.values()} == {
        "/user", "/users/{login}/events", "/repos/{repository}", "/repos/{repository}/issues",
        "/repos/{repository}/commits", "/repos/{repository}/pulls/{number}/commits"}


@pytest.mark.asyncio
async def test_environment_is_built_from_scratch(tmp_path, monkeypatch):
    for key, value in {"GH_TOKEN": "ghp_fixture", "GITHUB_TOKEN": "ghp_fixture", "GH_HOST": "evil.example",
                       "GH_CONFIG_DIR": "/tmp/evil", "GH_DEBUG": "api", "HTTPS_PROXY": "http://proxy.example",
                       "DYLD_INSERT_LIBRARIES": "/tmp/evil.dylib", "GH_REPO": "evil/repo",
                       "HOME": "/tmp/not-the-passwd-home", "GIT_DIR": "/tmp/evil"}.items():
        monkeypatch.setenv(key, value)
    runner = FakeGhRunner(FakeGitHubWorld())
    reader = make_reader(tmp_path, runner)
    await reader.read(reader.locate(), op="identity", authority_check=allow)
    env = runner.calls[0]["env"]
    assert env["HOME"] == str(tmp_path / "home")
    assert env["USER"] == env["LOGNAME"] == "operator"
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_CONFIG_DIR", "GH_DEBUG", "HTTPS_PROXY",
                "DYLD_INSERT_LIBRARIES", "GH_REPO", "GIT_DIR"):
        assert key not in env


def test_default_account_comes_from_the_passwd_database(monkeypatch):
    monkeypatch.setenv("HOME", "/tmp/not-the-passwd-home")
    account = github_cli.current_account()
    assert account.home != "/tmp/not-the-passwd-home"
    import pwd
    assert account.home == pwd.getpwuid(os.getuid()).pw_dir


@pytest.mark.asyncio
async def test_xdg_config_home_is_passed_only_inside_home(tmp_path, monkeypatch):
    runner = FakeGhRunner(FakeGitHubWorld())
    reader = make_reader(tmp_path, runner)
    binary = reader.locate()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    await reader.read(binary, op="identity", authority_check=allow)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "elsewhere"))
    await reader.read(binary, op="identity", authority_check=allow)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".." / "elsewhere"))
    await reader.read(binary, op="identity", authority_check=allow)
    assert runner.calls[0]["env"]["XDG_CONFIG_HOME"] == str(tmp_path / "home" / ".config")
    assert "XDG_CONFIG_HOME" not in runner.calls[1]["env"]
    assert "XDG_CONFIG_HOME" not in runner.calls[2]["env"]


def test_status_line_headers_and_body_are_parsed():
    status, headers, body = parse_api_output(http_output(200, {"id": 1}, {"Retry-After": "5"}))
    assert status == 200 and body == '{"id": 1}'
    assert headers == {"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "1790000000", "retry-after": "5"}
    assert parse_api_output("no status line\n\n{}") is None


def test_only_rate_limit_headers_are_kept():
    _, headers, _ = parse_api_output(http_output(200, [], {"Set-Cookie": "secret", "Authorization": "token x"}))
    assert set(headers) == {"x-ratelimit-remaining", "x-ratelimit-reset"}


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,code", [
    (Outcome(exit_code=1, stdout=http_output(401, {"message": "Bad credentials"})), "github_activity_credential_rejected"),
    (Outcome(exit_code=1, stdout=http_output(429, {})), "github_activity_rate_limited"),
    (Outcome(exit_code=1, stdout=http_output(403, {}, {"X-Ratelimit-Remaining": "0"})), "github_activity_rate_limited"),
    (Outcome(exit_code=1, stdout=http_output(403, {}, {"Retry-After": "60"})), "github_activity_rate_limited"),
    (Outcome(exit_code=1, stdout=http_output(403, {"message": "SSO"})), "github_activity_access_denied"),
    (Outcome(exit_code=1, stdout=http_output(404, {})), "github_activity_not_found"),
    (Outcome(exit_code=1, stdout=http_output(500, {})), "github_activity_read_failed"),
    (Outcome(exit_code=4, stdout=""), "gh_not_logged_in"),
    (Outcome(exit_code=1, stdout=""), "gh_request_failed"),
    (Outcome(exit_code=None, timed_out=True, cleanup_status="stopped"), "gh_timed_out"),
    (Outcome(exit_code=None, timed_out=True, cleanup_status="unknown"), "gh_cleanup_unproven"),
    (Outcome(stdout=http_output(200, {"id": 1})[:40], stdout_truncated=True), "github_activity_response_invalid"),
    (Outcome(stdout=http_output(200, "not json")), "github_activity_response_invalid"),
    (Outcome(stdout=http_output(200, [1, 2])), "github_activity_response_invalid"),
])
async def test_error_mapping(tmp_path, outcome, code):
    runner = FakeGhRunner(FakeGitHubWorld())
    runner.inject("/user", outcome)
    reader = make_reader(tmp_path, runner)
    with pytest.raises(GhCliError) as raised:
        await reader.read(reader.locate(), op="identity", authority_check=allow)
    assert raised.value.code == code
    assert "ghp_secret" not in repr(raised.value.__dict__)


@pytest.mark.asyncio
async def test_truncated_output_is_never_parsed(tmp_path):
    runner = FakeGhRunner(FakeGitHubWorld())
    runner.inject("/user", Outcome(stdout=http_output(200, {"id": 1, "login": "octo"}), stdout_truncated=True))
    reader = make_reader(tmp_path, runner)
    with pytest.raises(GhCliError) as raised:
        await reader.read(reader.locate(), op="identity", authority_check=allow)
    assert raised.value.code == "github_activity_response_invalid"


@pytest.mark.asyncio
async def test_stderr_is_never_returned_stored_or_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    runner = FakeGhRunner(FakeGitHubWorld())
    runner.inject("/user", Outcome(exit_code=1, stdout=""))
    reader = make_reader(tmp_path, runner)
    binary = reader.locate()
    with pytest.raises(GhCliError) as raised:
        await reader.read(binary, op="identity", authority_check=allow)
    response = await reader.read(binary, op="identity", authority_check=allow)
    assert "ghp_secret" not in caplog.text
    assert "ghp_secret" not in repr(raised.value.__dict__) and "ghp_secret" not in repr(response)


@pytest.mark.asyncio
async def test_authority_check_runs_before_the_child_starts(tmp_path):
    runner = FakeGhRunner(FakeGitHubWorld())
    reader = make_reader(tmp_path, runner)

    async def refuse(op, repository):
        raise RuntimeError("refused")

    with pytest.raises(RuntimeError):
        await reader.read(reader.locate(), op="repository", repository="a/b", authority_check=refuse)
    assert runner.calls == []


# -- binary trust ---------------------------------------------------------------


def test_gh_is_never_looked_up_on_path(tmp_path, monkeypatch):
    planted = tmp_path / "venv-bin" / "gh"
    planted.parent.mkdir()
    planted.write_text("#!/bin/sh\n", encoding="utf-8")
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", f"{planted.parent}:{os.environ.get('PATH', '')}")
    reader = github_cli.GhCliReader(runner=FakeGhRunner(FakeGitHubWorld()), candidates=(str(tmp_path / "missing" / "gh"),))
    with pytest.raises(GhCliError) as raised:
        reader.locate()
    assert raised.value.code == "gh_unavailable"
    installed = install_gh_file(tmp_path)
    reader = github_cli.GhCliReader(runner=FakeGhRunner(FakeGitHubWorld()), candidates=(str(installed),))
    assert reader.locate().real_path == str(installed.resolve())


@pytest.mark.parametrize("case", ["relative", "missing", "directory", "not_executable", "group_writable",
                                  "world_writable", "world_writable_parent"])
def test_untrusted_binary_is_refused(tmp_path, case):
    gh = install_gh_file(tmp_path)
    entry = str(gh)
    if case == "relative":
        entry = "gh-prefix/bin/gh"
    elif case == "missing":
        gh.unlink()
    elif case == "directory":
        gh.unlink()
        gh.mkdir()
    elif case == "not_executable":
        gh.chmod(0o644)
    elif case == "group_writable":
        gh.chmod(0o775)
    elif case == "world_writable":
        gh.chmod(0o757)
    elif case == "world_writable_parent":
        gh.parent.chmod(0o777)
    reader = github_cli.GhCliReader(runner=FakeGhRunner(FakeGitHubWorld()), candidates=(entry,))
    try:
        with pytest.raises(GhCliError) as raised:
            reader.locate(entry)
        assert raised.value.code in {"gh_unavailable", "gh_path_untrusted"}
        # A candidate that exists but fails the checks is refused, not skipped.
        with pytest.raises(GhCliError):
            reader.locate()
    finally:
        gh.parent.chmod(0o755)


def test_entry_symlink_retargeted_inside_prefix_is_accepted_and_outside_is_refused(tmp_path):
    prefix = tmp_path / "homebrew"
    old = prefix / "Cellar" / "gh" / "2.88.0" / "bin" / "gh"
    new = prefix / "Cellar" / "gh" / "2.89.0" / "bin" / "gh"
    outside = tmp_path / "elsewhere" / "gh"
    for target in (old, new, outside):
        target.parent.mkdir(parents=True)
        target.write_text(f"#!/bin/sh\n# {target}\n", encoding="utf-8")
        target.chmod(0o755)
    entry = prefix / "bin" / "gh"
    entry.parent.mkdir(parents=True)
    entry.symlink_to(old)
    reader = github_cli.GhCliReader(runner=FakeGhRunner(FakeGitHubWorld()), candidates=(str(entry),))
    first = reader.locate(str(entry))
    assert first.install_prefix == str(prefix)
    # brew upgrade: the entry path and prefix stay, the Cellar target moves.
    entry.unlink()
    entry.symlink_to(new)
    upgraded = reader.locate(str(entry), install_prefix=first.install_prefix)
    assert upgraded.real_path == str(new.resolve()) and upgraded.sha256 != first.sha256
    # Hijack: the entry now points outside the confirmed prefix.
    entry.unlink()
    entry.symlink_to(outside)
    with pytest.raises(GhCliError) as raised:
        reader.locate(str(entry), install_prefix=first.install_prefix)
    assert raised.value.code == "gh_path_untrusted"


@pytest.mark.asyncio
async def test_resolved_path_is_what_gets_executed(tmp_path):
    target = install_gh_file(tmp_path)
    link = tmp_path / "gh-prefix" / "bin" / "gh-link"
    link.symlink_to(target)
    runner = FakeGhRunner(FakeGitHubWorld())
    reader = make_reader(tmp_path, runner, gh=link)
    await reader.read(reader.locate(str(link)), op="identity", authority_check=allow)
    assert runner.calls[0]["argv"][0] == str(target.resolve())


def test_gh_is_not_an_agent_command():
    assert "gh" not in process_tools._COMMAND_NAME_ALLOWLIST
    with pytest.raises(ValueError):
        process_tools._normalize_command("gh")
    with pytest.raises(ValueError):
        process_tools._normalize_command("/opt/homebrew/bin/gh")


# -- through the real process path ------------------------------------------------


@pytest.mark.asyncio
async def test_real_child_receives_exact_argv_env_and_private_empty_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_fixture_never_passed")
    endpoint = "/user"
    gh = fake_gh_executable(tmp_path, {endpoint: http_output(200, {"id": 101, "login": "octo"})})
    reader = make_reader(tmp_path, github_cli.default_runner, gh=gh)
    binary = reader.locate()
    response = await reader.read(binary, op="identity", authority_check=allow)
    assert response.body == {"id": 101, "login": "octo"}
    (call,) = read_calls(gh)
    assert call["argv"] == [binary.real_path, *API_PREFIX, endpoint]
    assert call["cwd_entries"] == []
    assert Path(call["cwd"]).resolve() == Path(call["env"]["TMPDIR"]).resolve()
    assert call["env"]["HOME"] == str(tmp_path / "home")
    assert "GH_TOKEN" not in call["env"]
    # macOS may add its own __CF_* variable to every process; nothing else appears.
    extras = {key for key in call["env"] if not key.startswith("__CF")} - set(runner_env_keys())
    assert extras == set()


def runner_env_keys():
    from tests.github_activity_fixture import ALLOWED_ENV_KEYS
    return ALLOWED_ENV_KEYS


@pytest.mark.asyncio
async def test_child_gets_no_stdin_and_no_shell(tmp_path):
    endpoint = build_endpoint("commits", **op_values("commits"))
    gh = fake_gh_executable(tmp_path, {endpoint: http_output(200, [])})
    reader = make_reader(tmp_path, github_cli.default_runner, gh=gh)
    response = await reader.read(reader.locate(), op="commits", authority_check=allow, **op_values("commits"))
    assert response.body == []
    (call,) = read_calls(gh)
    assert call["stdin_at_eof"] is True
    # The endpoint arrives as one argv element, with '&' untouched by any shell.
    assert call["argv"][-1] == endpoint and "&" in endpoint


@pytest.mark.asyncio
async def test_child_is_reaped_and_run_directory_removed(tmp_path):
    gh = fake_gh_executable(tmp_path, {"/user": http_output(200, {"id": 101, "login": "octo"})})
    reader = make_reader(tmp_path, github_cli.default_runner, gh=gh)
    assert await reader.version(reader.locate()) == "2.89.0"
    await reader.read(reader.locate(), op="identity", authority_check=allow)
    assert list((tmp_path / "gh-runs").iterdir()) == []
