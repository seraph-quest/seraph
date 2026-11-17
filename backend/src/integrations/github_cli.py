"""Read-only GitHub REST reads through the operator's local ``gh`` login (#1264).

This module is the only code in Seraph that starts ``gh``. It is authority
code: the operator's ``gh`` login usually carries full read and write access
to their account, and Seraph cannot narrow it. The only thing that limits
what happens is the argv built here, so every change to this file must be
reviewed as a change to an authority boundary.

What it does, and nothing else:

- It finds ``gh`` only at fixed candidate paths or one operator-confirmed
  absolute path, never through ``PATH``, and refuses a binary that fails the
  ownership and mode checks or has left its install prefix.
- It runs ``gh --version`` or one ``gh api --method GET`` request whose
  endpoint comes from a closed table (``OPS``) with validated values. It never
  uses ``-f``/``-F``/``--input``/``--paginate``/``graphql``, a full URL, or any
  other subcommand. It never runs ``gh auth`` and never reads ``gh``'s config,
  hosts file or keychain; only ``gh`` does.
- The child's environment is built from nothing (``HOME`` from the passwd
  database), so ``GH_TOKEN``/``GITHUB_TOKEN``/``GH_HOST``/``GH_CONFIG_DIR``,
  proxy, CA and loader variables from Seraph's own environment never reach it.
  Proxy variables are deliberately not passed: an operator behind a mandatory
  proxy sees ``gh_request_failed``.
- The child runs through ``process_tools.run_owned_argv`` (bounded capture,
  group kill and reap), with ``stdin`` at ``/dev/null`` and a new empty
  private working directory. stderr is bounded and dropped.

There is no retry and no sleep here. The 30 s per-call timeout is a liveness
bound on one child, not a validity gate.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import quote

from src.extensions.github_followthrough import GITHUB_API_VERSION
from src.tools import process_tools

GH_CANDIDATE_PATHS: tuple[str, ...] = (
    "/opt/homebrew/bin/gh",
    "/usr/local/bin/gh",
    "/usr/bin/gh",
    "/home/linuxbrew/.linuxbrew/bin/gh",
)
GH_CALL_TIMEOUT_SECONDS = 30
GH_STDOUT_LIMIT = 1024 * 1024
GH_STDERR_LIMIT = 16 * 1024
PER_PAGE = 50

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LOGIN = re.compile(r"^[A-Za-z0-9-]{1,39}$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_STATUS_LINE = re.compile(r"^HTTP/\d(?:\.\d)? (\d{3})")
_KEPT_HEADERS = ("x-ratelimit-remaining", "x-ratelimit-reset", "retry-after")
_VERSION = re.compile(r"^gh version ([0-9][0-9A-Za-z.+-]{0,40})")

# The closed endpoint table: op -> (path template, fixed query keys, max pages,
# expected JSON outer type). Values are substituted only after validation.
OPS: dict[str, tuple[str, tuple[str, ...], int, type]] = {
    "identity": ("/user", (), 1, dict),
    "events": ("/users/{login}/events", ("per_page", "page"), 6, list),
    "repository": ("/repos/{repository}", (), 1, dict),
    "issues": ("/repos/{repository}/issues",
               ("state", "sort", "direction", "since", "per_page", "page"), 3, list),
    "commits": ("/repos/{repository}/commits",
                ("sha", "author", "since", "until", "per_page", "page"), 3, list),
    "pull_commits": ("/repos/{repository}/pulls/{number}/commits", ("per_page", "page"), 2, list),
}


class GhCliError(Exception):
    """A typed, fixed-code failure. It never carries gh output or a token."""

    def __init__(self, code: str, *, exit_code: int | None = None, http_status: int | None = None,
                 headers: dict[str, str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.http_status = http_status
        self.headers = dict(headers or {})


@dataclass(frozen=True)
class GhAccount:
    home: str
    name: str


@dataclass(frozen=True)
class GhBinary:
    entry_path: str      # what the operator confirmed, e.g. /opt/homebrew/bin/gh
    real_path: str       # resolved now; this is what gets executed
    install_prefix: str  # the directory the resolved target must stay under
    sha256: str          # recorded on the run row for audit, not a gate


@dataclass(frozen=True)
class GhResponse:
    status: int
    body: Any
    headers: dict[str, str]


class GhRunner(Protocol):
    def __call__(self, argv: list[str], *, cwd: Path, env: dict[str, str], timeout_seconds: float,
                 cancel_event: threading.Event, stdout_limit: int,
                 stderr_limit: int) -> process_tools.OwnedRunResult: ...


def default_runner(argv: list[str], *, cwd: Path, env: dict[str, str], timeout_seconds: float,
                   cancel_event: threading.Event, stdout_limit: int,
                   stderr_limit: int) -> process_tools.OwnedRunResult:
    return process_tools.run_owned_argv(argv, cwd=cwd, env=env, timeout_seconds=timeout_seconds,
        cancel_event=cancel_event, stdout_limit=stdout_limit, stderr_limit=stderr_limit,
        stdin=subprocess.DEVNULL)


def current_account() -> GhAccount:
    # Home and name come from the passwd database, never from os.environ.
    entry = pwd.getpwuid(os.getuid())
    return GhAccount(home=entry.pw_dir, name=entry.pw_name)


def _default_run_directory() -> Path:
    return process_tools._worker_runtime_root("gh-" + uuid.uuid4().hex)


def install_prefix_for(entry_path: str) -> str:
    """The install prefix an entry path implies: the parent of its ``bin`` directory."""
    prefix = Path(entry_path).parent.parent
    return str(prefix)


def _trusted_owner(info: os.stat_result) -> bool:
    return info.st_uid in {os.getuid(), 0}


def _within(path: Path, prefix: Path) -> bool:
    try:
        path.relative_to(prefix)
    except ValueError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise GhCliError("github_activity_value_invalid")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_endpoint(op: str, *, repository: str | None = None, login: str | None = None,
                   number: int | None = None, branch: str | None = None,
                   since: datetime | None = None, until: datetime | None = None, page: int = 1) -> str:
    """Build one endpoint from the closed table; any invalid value is refused."""
    if op not in OPS:
        raise GhCliError("github_activity_op_unknown")
    template, query_keys, max_pages, _ = OPS[op]
    if type(page) is not int or not 1 <= page <= max_pages:
        raise GhCliError("github_activity_value_invalid")
    values: dict[str, str] = {}
    if "{repository}" in template:
        if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository) or ".." in repository:
            raise GhCliError("github_activity_value_invalid")
        owner, name = repository.split("/", 1)
        if owner.startswith(("-", ".")) or name.startswith(("-", ".")):
            raise GhCliError("github_activity_value_invalid")
        values["repository"] = repository
    if "{login}" in template or "author" in query_keys:
        if not isinstance(login, str) or not _LOGIN.fullmatch(login) or login.startswith("-"):
            raise GhCliError("github_activity_value_invalid")
        values["login"] = login
    if "{number}" in template:
        if type(number) is not int or number <= 0:
            raise GhCliError("github_activity_value_invalid")
        values["number"] = str(number)
    path = template.format(**values)
    query: list[tuple[str, str]] = []
    for key in query_keys:
        if key == "per_page":
            query.append((key, str(PER_PAGE)))
        elif key == "page":
            query.append((key, str(page)))
        elif key == "state":
            query.append((key, "all"))
        elif key == "sort":
            query.append((key, "updated"))
        elif key == "direction":
            query.append((key, "desc"))
        elif key == "since":
            if since is None:
                raise GhCliError("github_activity_value_invalid")
            query.append((key, format_timestamp(since)))
        elif key == "until":
            if until is None:
                raise GhCliError("github_activity_value_invalid")
            query.append((key, format_timestamp(until)))
        elif key == "author":
            query.append((key, values["login"]))
        elif key == "sha":
            if (not isinstance(branch, str) or not _BRANCH.fullmatch(branch) or ".." in branch
                    or branch.startswith(("-", "/"))):
                raise GhCliError("github_activity_value_invalid")
            query.append((key, quote(branch, safe="")))
    endpoint = path + ("?" + "&".join(f"{key}={value}" for key, value in query) if query else "")
    # gh api replaces {owner}, {repo} and {branch} from the working directory's
    # repository; an endpoint with braces is never handed to it.
    if not endpoint.startswith("/") or "{" in endpoint or "}" in endpoint or " " in endpoint:
        raise GhCliError("github_activity_value_invalid")
    return endpoint


def api_argv(real_path: str, endpoint: str) -> list[str]:
    """The one fixed argv shape for a read: GET, github.com, response headers on stdout."""
    return [real_path, "api", "--method", "GET", "--hostname", "github.com", "-i",
            "-H", "Accept: application/vnd.github+json",
            "-H", f"X-GitHub-Api-Version: {GITHUB_API_VERSION}",
            endpoint]


def parse_api_output(stdout: str) -> tuple[int, dict[str, str], str] | None:
    """Split ``gh api -i`` output into status, kept headers and body; None without a status line."""
    text = stdout.replace("\r\n", "\n")
    head, separator, body = text.partition("\n\n")
    lines = head.split("\n")
    match = _STATUS_LINE.match(lines[0]) if lines else None
    if match is None:
        return None
    if not separator:
        body = ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        key = name.strip().lower()
        if colon and key in _KEPT_HEADERS:
            headers[key] = value.strip()
    return int(match.group(1)), headers, body


def _rate_limited(status: int, headers: dict[str, str]) -> bool:
    return status == 429 or (status == 403 and (headers.get("x-ratelimit-remaining") == "0"
                                                or "retry-after" in headers))


class GhCliReader:
    def __init__(self, *, runner: GhRunner | None = None,
                 candidates: tuple[str, ...] = GH_CANDIDATE_PATHS,
                 account: Callable[[], GhAccount] | None = None,
                 run_directory: Callable[[], Path] | None = None) -> None:
        self._runner: GhRunner = runner or default_runner
        self._candidates = tuple(candidates)
        self._account = account or current_account
        self._run_directory = run_directory or _default_run_directory

    # -- finding gh -------------------------------------------------------

    def candidates(self) -> tuple[str, ...]:
        return self._candidates

    def locate(self, entry_path: str | None = None, *, install_prefix: str | None = None,
               digest: bool = True) -> GhBinary:
        """Find a trusted gh by stat and hash only; starts nothing.

        With no ``entry_path`` the first candidate that exists is used. A
        candidate that exists but fails the checks is refused rather than
        skipped, so a planted file cannot be silently bypassed to a later one.
        """
        if entry_path is None:
            for candidate in self._candidates:
                if os.path.lexists(candidate):
                    return self._trusted(candidate, install_prefix, digest)
            raise GhCliError("gh_unavailable")
        return self._trusted(entry_path, install_prefix, digest)

    def _trusted(self, entry_path: str, install_prefix: str | None, digest: bool = True) -> GhBinary:
        if not isinstance(entry_path, str) or not os.path.isabs(entry_path):
            raise GhCliError("gh_path_untrusted")
        entry = Path(entry_path)
        try:
            real = entry.resolve(strict=True)
            info = real.stat()
            directory_info = real.parent.stat()
            entry_directory_info = entry.parent.stat()
        except (OSError, RuntimeError):
            raise GhCliError("gh_unavailable") from None
        prefix = Path(install_prefix or install_prefix_for(entry_path))
        if str(prefix) in {"/", ""} or not prefix.is_absolute():
            raise GhCliError("gh_path_untrusted")
        if (not stat.S_ISREG(info.st_mode) or not os.access(real, os.X_OK) or not _trusted_owner(info)
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            raise GhCliError("gh_path_untrusted")
        for directory in (directory_info, entry_directory_info):
            if not _trusted_owner(directory) or directory.st_mode & stat.S_IWOTH:
                raise GhCliError("gh_path_untrusted")
        if not _within(real, prefix.resolve()):
            raise GhCliError("gh_path_untrusted")
        sha256 = ""
        if digest:
            try:
                sha256 = _sha256(real)
            except OSError:
                raise GhCliError("gh_unavailable") from None
        return GhBinary(entry_path=entry_path, real_path=str(real), install_prefix=str(prefix), sha256=sha256)

    # -- the child --------------------------------------------------------

    def environment(self, run_dir: Path) -> dict[str, str]:
        account = self._account()
        env = {
            "HOME": account.home,
            "USER": account.name,
            "LOGNAME": account.name,
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "TMPDIR": str(run_dir),
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
            "NO_COLOR": "1",
            "CLICOLOR": "0",
        }
        config_home = os.environ.get("XDG_CONFIG_HOME", "")
        if config_home and os.path.isabs(config_home) and _within(Path(os.path.normpath(config_home)),
                                                                  Path(account.home)):
            env["XDG_CONFIG_HOME"] = config_home
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
        if runtime_dir and os.path.isabs(runtime_dir):
            env["XDG_RUNTIME_DIR"] = runtime_dir
        bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
        if bus:
            env["DBUS_SESSION_BUS_ADDRESS"] = bus
        return env

    async def _run(self, argv: list[str]) -> process_tools.OwnedRunResult:
        run_dir = self._run_directory()
        cancel_event = threading.Event()
        try:
            env = self.environment(run_dir)
            worker = asyncio.ensure_future(asyncio.to_thread(
                self._runner, argv, cwd=run_dir, env=env, timeout_seconds=GH_CALL_TIMEOUT_SECONDS,
                cancel_event=cancel_event, stdout_limit=GH_STDOUT_LIMIT, stderr_limit=GH_STDERR_LIMIT))
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                # Stop the child, wait until it is killed and reaped, re-raise.
                cancel_event.set()
                try:
                    stopped = await worker
                except Exception:
                    stopped = None
                if stopped is not None and stopped.cleanup_status in {"not_requested", "stopped"}:
                    process_tools._delete_runtime_dir(run_dir)
                raise
        except OSError:
            process_tools._delete_runtime_dir(run_dir)
            raise GhCliError("gh_unavailable") from None
        if result.cleanup_status in {"not_requested", "stopped"}:
            process_tools._delete_runtime_dir(run_dir)
        if result.cleanup_status not in {"not_requested", "stopped"}:
            raise GhCliError("gh_cleanup_unproven")
        if result.timed_out:
            raise GhCliError("gh_timed_out")
        return result

    async def version(self, binary: GhBinary) -> str:
        result = await self._run([binary.real_path, "--version"])
        if result.exit_code != 0 or result.stdout_truncated:
            raise GhCliError("gh_request_failed", exit_code=result.exit_code)
        match = _VERSION.match(result.stdout.strip())
        if match is None:
            raise GhCliError("github_activity_response_invalid")
        return match.group(1)

    async def read(self, binary: GhBinary, *, op: str,
                   authority_check: Callable[[str, str | None], Awaitable[None]],
                   repository: str | None = None, login: str | None = None, number: int | None = None,
                   branch: str | None = None, since: datetime | None = None, until: datetime | None = None,
                   page: int = 1) -> GhResponse:
        endpoint = build_endpoint(op, repository=repository, login=login, number=number, branch=branch,
                                  since=since, until=until, page=page)
        await authority_check(op, repository)
        result = await self._run(api_argv(binary.real_path, endpoint))
        # stderr is bounded by the runner and dropped here: never stored,
        # logged or returned.
        if result.stdout_truncated:
            raise GhCliError("github_activity_response_invalid")
        parsed = parse_api_output(result.stdout)
        if parsed is None:
            if result.exit_code == 4:
                raise GhCliError("gh_not_logged_in", exit_code=4)
            raise GhCliError("gh_request_failed", exit_code=result.exit_code)
        status, headers, body = parsed
        if status == 401:
            raise GhCliError("github_activity_credential_rejected", http_status=status, headers=headers)
        if _rate_limited(status, headers):
            raise GhCliError("github_activity_rate_limited", http_status=status, headers=headers)
        if status == 403:
            raise GhCliError("github_activity_access_denied", http_status=status, headers=headers)
        if status == 404:
            raise GhCliError("github_activity_not_found", http_status=status, headers=headers)
        if status != 200 or result.exit_code != 0:
            raise GhCliError("github_activity_read_failed", http_status=status, headers=headers)
        try:
            value = json.loads(body)
        except ValueError:
            raise GhCliError("github_activity_response_invalid", headers=headers) from None
        if not isinstance(value, OPS[op][3]):
            raise GhCliError("github_activity_response_invalid", headers=headers)
        return GhResponse(status=status, body=value, headers=headers)
