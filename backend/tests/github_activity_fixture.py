"""Fakes for the GitHub activity source (#1264): no real gh, no network.

``FakeGitHubWorld`` is an in-memory GitHub serving the reader's GET
endpoints with real pagination and ``since``/``until`` filtering.
``FakeGhRunner`` is the injected runner seam: it asserts the exact argv and
environment shape and answers in ``gh api -i`` form. ``fake_gh_executable``
writes a real executable for the few tests that go through the process path.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

from src.extensions.github_followthrough import GITHUB_API_VERSION
from src.integrations.github_cli import GhAccount, GhCliReader
from src.tools.process_tools import OwnedRunResult

ALLOWED_ENV_KEYS = {"HOME", "USER", "LOGNAME", "PATH", "LANG", "TMPDIR", "GH_PROMPT_DISABLED",
                    "GH_NO_UPDATE_NOTIFIER", "GH_NO_EXTENSION_UPDATE_NOTIFIER", "NO_COLOR", "CLICOLOR",
                    "XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
FIXED_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1",
             "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1", "NO_COLOR": "1", "CLICOLOR": "0"}
API_PREFIX = ["api", "--method", "GET", "--hostname", "github.com", "-i",
              "-H", "Accept: application/vnd.github+json", "-H", f"X-GitHub-Api-Version: {GITHUB_API_VERSION}"]


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def user(user_id: int, login: str) -> dict[str, Any]:
    return {"id": user_id, "login": login}


def http_output(status: int, body: Any, headers: dict[str, str] | None = None) -> str:
    reason = {200: "OK", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 429: "Too Many Requests"}.get(status, "X")
    lines = [f"HTTP/2.0 {status} {reason}", "Content-Type: application/json; charset=utf-8",
             "X-Github-Request-Id: FAKE", "X-Ratelimit-Remaining: 4999", "X-Ratelimit-Reset: 1790000000"]
    for key, value in (headers or {}).items():
        lines = [line for line in lines if not line.lower().startswith(key.lower() + ":")]
        lines.append(f"{key}: {value}")
    return "\r\n".join(lines) + "\r\n\r\n" + (body if isinstance(body, str) else json.dumps(body))


class FakeGitHubWorld:
    def __init__(self, *, user_id: int = 101, login: str = "octo") -> None:
        self.user = user(user_id, login)
        self.repos: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.issues: dict[str, list[dict[str, Any]]] = {}
        self.commits: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.pull_commits: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self.overrides: dict[str, tuple[int, Any, dict[str, str]]] = {}

    # -- building -----------------------------------------------------------

    def repo(self, name: str, repo_id: int, *, default_branch: str = "main", private: bool = False) -> None:
        self.repos[name.lower()] = {"id": repo_id, "full_name": name, "default_branch": default_branch,
                                    "private": private}
        self.issues.setdefault(name.lower(), [])

    def event(self, repo: str, at: datetime, *, kind: str = "PushEvent", actor: dict | None = None,
              ref: str | None = "refs/heads/main") -> None:
        payload = {"ref": ref} if ref else {}
        self.events.append({"id": str(len(self.events) + 1), "type": kind, "actor": actor or self.user,
                            "repo": {"name": repo}, "payload": payload, "created_at": iso(at)})
        self.events.sort(key=lambda item: item["created_at"], reverse=True)

    def issue(self, repo: str, number: int, *, author: dict | None = None, created: datetime,
              updated: datetime | None = None, closed: datetime | None = None, pull: bool = False,
              merged: datetime | None = None, title: str | None = None) -> dict[str, Any]:
        item = {"number": number, "title": title or f"Item {number}", "user": author or self.user,
                "created_at": iso(created), "updated_at": iso(updated or closed or merged or created),
                "closed_at": iso(closed) if closed else None, "state": "closed" if closed else "open",
                "html_url": "https://evil.example/not-used"}
        if pull:
            item["pull_request"] = {"merged_at": iso(merged) if merged else None}
        self.issues.setdefault(repo.lower(), []).append(item)
        return item

    def commit(self, repo: str, sha: str, at: datetime, *, branch: str = "main", author: dict | None = None,
               message: str = "change", pull: int | None = None) -> dict[str, Any]:
        linked = self.user if author is None else author
        item = {"sha": sha, "author": linked or None, "committer": user(19864447, "web-flow"),
                "commit": {"message": message, "author": {"date": iso(at)}, "committer": {"date": iso(at)}},
                "html_url": "https://evil.example/not-used"}
        if pull is None:
            self.commits.setdefault((repo.lower(), branch), []).append(item)
        else:
            self.pull_commits.setdefault((repo.lower(), pull), []).append(item)
        return item

    def override(self, path: str, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        self.overrides[path] = (status, body, headers or {})

    # -- serving ------------------------------------------------------------

    @staticmethod
    def _page(rows: list[Any], query: dict[str, list[str]]) -> list[Any]:
        per_page = int(query.get("per_page", ["30"])[0])
        page = int(query.get("page", ["1"])[0])
        return rows[(page - 1) * per_page: page * per_page]

    def respond(self, endpoint: str) -> tuple[int, Any, dict[str, str]]:
        parts = urlsplit(endpoint)
        path, query = parts.path, parse_qs(parts.query)
        if path in self.overrides:
            return self.overrides[path]
        segments = path.strip("/").split("/")
        if path == "/user":
            return 200, dict(self.user), {}
        if segments[0] == "users" and len(segments) == 3 and segments[2] == "events":
            return 200, self._page(self.events, query), {}
        if segments[0] != "repos" or len(segments) < 3:
            return 404, {"message": "Not Found"}, {}
        name = f"{segments[1]}/{segments[2]}".lower()
        repo = self.repos.get(name)
        if repo is None:
            return 404, {"message": "Not Found"}, {}
        rest = segments[3:]
        if not rest:
            return 200, dict(repo), {}
        if rest == ["issues"]:
            since = query["since"][0]
            rows = sorted((item for item in self.issues.get(name, []) if item["updated_at"] >= since),
                          key=lambda item: item["updated_at"], reverse=True)
            return 200, self._page(rows, query), {}
        if rest == ["commits"]:
            branch = unquote(query["sha"][0])
            login = query["author"][0]
            since, until = query["since"][0], query["until"][0]
            rows = [item for item in self.commits.get((name, branch), [])
                    if (item["author"] or {}).get("login") == login
                    and since <= item["commit"]["committer"]["date"] <= until]
            rows.sort(key=lambda item: item["commit"]["committer"]["date"], reverse=True)
            return 200, self._page(rows, query), {}
        if len(rest) == 3 and rest[0] == "pulls" and rest[2] == "commits":
            return 200, self._page(self.pull_commits.get((name, int(rest[1])), []), query), {}
        return 404, {"message": "Not Found"}, {}


@dataclass
class Outcome:
    """An injected result for one call instead of the world's answer."""
    exit_code: int | None = 0
    stdout: str = ""
    stdout_truncated: bool = False
    timed_out: bool = False
    cleanup_status: str = "not_requested"
    stderr: str = "fake gh stderr token-like-ghp_secret must never surface"


class FakeGhRunner:
    def __init__(self, world: FakeGitHubWorld, *, version: str = "2.89.0") -> None:
        self.world = world
        self.version = version
        self.calls: list[dict[str, Any]] = []
        # endpoint path -> Outcome, used once each (a list is consumed in order)
        self.outcomes: dict[str, list[Outcome]] = {}
        self.on_call: Callable[[dict[str, Any]], None] | None = None
        self.lock = threading.Lock()

    def inject(self, path: str, *outcomes: Outcome) -> None:
        self.outcomes.setdefault(path, []).extend(outcomes)

    @property
    def endpoints(self) -> list[str]:
        return [call["endpoint"] for call in self.calls if call["endpoint"] is not None]

    def __call__(self, argv: list[str], *, cwd: Path, env: dict[str, str], timeout_seconds: float,
                 cancel_event: threading.Event, stdout_limit: int, stderr_limit: int) -> OwnedRunResult:
        assert Path(argv[0]).is_absolute()
        assert set(env) <= ALLOWED_ENV_KEYS, sorted(set(env) - ALLOWED_ENV_KEYS)
        for key, value in FIXED_ENV.items():
            assert env[key] == value
        assert env["TMPDIR"] == str(cwd) and cwd.is_dir() and not any(cwd.iterdir())
        assert timeout_seconds == 30 and stdout_limit == 1024 * 1024 and stderr_limit == 16 * 1024
        if argv[1:] == ["--version"]:
            endpoint = None
        else:
            assert len(argv) == len(API_PREFIX) + 2, argv
            assert argv[1:-1] == API_PREFIX, argv
            endpoint = argv[-1]
            assert endpoint.startswith("/") and "{" not in endpoint and "}" not in endpoint
        call = {"argv": list(argv), "env": dict(env), "cwd": cwd, "endpoint": endpoint, "cancel_event": cancel_event}
        with self.lock:
            self.calls.append(call)
        if self.on_call is not None:
            self.on_call(call)
        if endpoint is None:
            return _result(Outcome(stdout=f"gh version {self.version} (2026-01-01)\nhttps://github.com/cli/cli\n"))
        path = urlsplit(endpoint).path
        queued = self.outcomes.get(path)
        if queued:
            return _result(queued.pop(0))
        status, body, headers = self.world.respond(endpoint)
        return _result(Outcome(exit_code=0 if status == 200 else 1, stdout=http_output(status, body, headers)))


def _result(outcome: Outcome) -> OwnedRunResult:
    return OwnedRunResult(exit_code=outcome.exit_code, stdout=outcome.stdout, stderr=outcome.stderr,
                          stdout_truncated=outcome.stdout_truncated, stderr_truncated=False,
                          timed_out=outcome.timed_out, cancelled=False, cleanup_status=outcome.cleanup_status,
                          remaining_descendants=0 if outcome.cleanup_status == "stopped" else None)


def install_gh_file(root: Path, *, content: str = "#!/bin/sh\nexit 0\n") -> Path:
    """A trusted-looking gh at ``root/prefix/bin/gh`` (owner-only writable)."""
    path = root / "gh-prefix" / "bin" / "gh"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def make_reader(tmp_path: Path, runner: Any, *, gh: Path | None = None) -> GhCliReader:
    gh = gh or install_gh_file(tmp_path)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    runs = tmp_path / "gh-runs"

    def run_directory() -> Path:
        path = runs / uuid.uuid4().hex
        path.mkdir(parents=True, mode=0o700)
        return path

    return GhCliReader(runner=runner, candidates=(str(gh),),
                       account=lambda: GhAccount(home=str(home), name="operator"), run_directory=run_directory)


FAKE_GH_SCRIPT = """#!{python}
import json, os, sys
from pathlib import Path
here = Path(__file__).resolve().parent
answers = json.loads((here / "world.json").read_text())
stdin_at_eof = sys.stdin.read() == "" if sys.stdin is not None else True
with open(here / "calls.jsonl", "a") as log:
    log.write(json.dumps({{"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd(),
        "cwd_entries": os.listdir("."), "stdin_at_eof": stdin_at_eof}}) + "\\n")
if sys.argv[1:] == ["--version"]:
    print("gh version 2.89.0 (2026-01-01)")
    sys.exit(0)
answer = answers.get(sys.argv[-1])
if answer is None:
    sys.stdout.write("HTTP/2.0 404 Not Found\\r\\n\\r\\n{{}}")
    sys.exit(1)
sys.stdout.write(answer)
sys.exit(0)
"""


def fake_gh_executable(tmp_path: Path, answers: dict[str, str]) -> Path:
    """A real gh stand-in: logs argv/env/cwd to calls.jsonl and prints canned -i output."""
    path = install_gh_file(tmp_path, content=FAKE_GH_SCRIPT.format(python=sys.executable))
    (path.parent / "world.json").write_text(json.dumps(answers), encoding="utf-8")
    assert not path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    return path


def read_calls(gh: Path) -> list[dict[str, Any]]:
    log = gh.parent / "calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def environ_snapshot() -> dict[str, str]:
    return dict(os.environ)
