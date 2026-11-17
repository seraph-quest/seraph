"""The operator's own GitHub activity, read through their local ``gh`` login (#1264, slice 1).

Consent: the source is off until the operator checks the ``gh`` login,
acknowledges that ``gh`` holds their full GitHub access, and enables it in
Settings. It stops only on real changes: the operator's toggle or revoke, a
different ``gh`` account, or an explicit logout/revocation of the Seraph
login that enabled it. Nothing here expires by time: an idle or absolute
expiry of that login does not stop the source.

Collection reads only the GET endpoints of ``github_cli.OPS``, one child at a
time, at most ``MAX_CALLS_PER_RUN`` per run and ``MAX_REPOSITORIES``
repositories, with no retry and no sleep. Before every ``gh`` start the
source must still be active at the run's revision, the enabling root must
not have been revoked, and a ``/repos/...`` read must be inside the saved
scope; the first two are checked again before a result is kept. Items are
stored only by a run that was not blocked, in the same transaction as a guard
on the source row (still active at the run's revision). Items are
provenance-carrying rows whose URLs Seraph builds itself.

This slice collects commits, pull requests opened/merged/closed and issues
opened/closed. Reviews and comments, and the end-of-day report integration,
are later slices of #1264.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.db import engine as db_engine
from src.db.models import GitHubActivityItem, GitHubActivityRun, GitHubActivitySource
from src.integrations.github_cli import (
    OPS, PER_PAGE, GhBinary, GhCliError, GhCliReader, GhResponse, GhValueInvalid, valid_branch, valid_login,
    valid_repository,
)
from src.utils.clock import get_clock

logger = logging.getLogger(__name__)

MAX_CALLS_PER_RUN = 120
MAX_REPOSITORIES = 10
MAX_OWN_PULLS = 10
MAX_PUSHED_BRANCHES = 3
EVENTS_HORIZON_DAYS = 30
MAX_TITLE_CHARS = 300
MAX_ITEMS_RETURNED = 500
CREDENTIAL_KIND = "local_gh_cli_login"
GRANT_SENTENCE = "Uses the operator's local gh login (full gh scope; Seraph only issues read GETs)."
CREDENTIAL = {"kind": CREDENTIAL_KIND, "scope": "full_gh_scope", "seraph_requests": "get_only_allowlist"}

ActivityKind = Literal["commit", "pr_opened", "pr_merged", "pr_closed", "issue_opened", "issue_closed"]
ALL_KINDS: list[str] = ["commit", "pr_opened", "pr_merged", "pr_closed", "issue_opened", "issue_closed"]

_SHA = re.compile(r"^[0-9a-f]{40}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

# Codes that end a run as blocked rather than failed; every other gh error
# also ends the run at once, since a second child would only repeat it.
_BLOCKING_CODES = {"gh_not_logged_in"}
_REPOSITORY_CODES = {"github_activity_access_denied", "github_activity_not_found"}
# Limitations that are information only and do not make a run partial.
_INFORMATIONAL = {"events_discovery_may_lag", "gh_binary_changed_since_last_run", "unlinked_commit_emails"}


class GitHubActivityError(Exception):
    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class GitHubActivityScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    discover_from_activity: bool = True
    repositories: list[str] = Field(default_factory=list, max_length=20)
    owners: list[str] = Field(default_factory=list, max_length=10)
    exclude_repositories: list[str] = Field(default_factory=list, max_length=20)
    kinds: list[ActivityKind] = Field(default_factory=lambda: list(ALL_KINDS), min_length=1)

    @field_validator("repositories", "exclude_repositories")
    @classmethod
    def repository_names(cls, value: list[str]) -> list[str]:
        if any(not valid_repository(item) for item in value) or len({item.lower() for item in value}) != len(value):
            raise ValueError("repositories must be unique owner/name values")
        return value

    @field_validator("owners")
    @classmethod
    def owner_names(cls, value: list[str]) -> list[str]:
        if any(not valid_login(item) for item in value) or len({item.lower() for item in value}) != len(value):
            raise ValueError("owners must be unique GitHub account names")
        return value

    @field_validator("kinds")
    @classmethod
    def unique_kinds(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("kinds must be unique")
        return value

    @model_validator(mode="after")
    def something_to_read(self) -> "GitHubActivityScope":
        if not self.discover_from_activity and not self.repositories:
            raise ValueError("choose discovery from activity or at least one repository")
        return self

    def allows(self, repository: str, *, discovered: bool) -> bool:
        lowered = repository.lower()
        if lowered in {item.lower() for item in self.exclude_repositories}:
            return False
        if lowered in {item.lower() for item in self.repositories}:
            return True
        if not (discovered and self.discover_from_activity):
            return False
        owner = lowered.split("/", 1)[0]
        return not self.owners or owner in {item.lower() for item in self.owners}


def local_day_window(report_day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """The half-open UTC window ``[start, end)`` of one local calendar day."""
    start = datetime.combine(report_day, time.min, tzinfo=tz)
    end = datetime.combine(report_day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value is not None else None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _hhmm(value: datetime | None, tz: ZoneInfo | None) -> str:
    value = _aware(value)
    if value is None:
        return "an unknown time"
    return value.astimezone(tz or timezone.utc).strftime("%H:%M")


def activity_status_message(status: str, reason_code: str | None = None, *, login: str | None = None,
                            current_login: str | None = None, gh_path: str | None = None,
                            exit_code: int | None = None, item_count: int = 0, repository_count: int = 0,
                            limitations: list[dict[str, Any]] | None = None, at: datetime | None = None,
                            tz: ZoneInfo | None = None) -> str:
    """The one honest sentence for a source or run state (#1264 design section 5)."""
    if status == "not_connected":
        return "GitHub source not connected."
    if status == "revoked":
        return ("GitHub source was switched off by the operator. "
                "Stored activity from earlier collections is shown.")
    if status == "stopped":
        if reason_code == "gh_login_changed":
            now_as = f"@{current_login}" if current_login else "another account"
            return (f"GitHub source stopped: gh is now logged in as {now_as}, not @{login}. "
                    "Enable it again in Settings → GitHub to use this login.")
        return "GitHub source stopped. Enable it again in Settings → GitHub."
    if status == "blocked":
        if reason_code == "gh_unavailable":
            return ("GitHub source blocked: no gh command is available where Seraph's backend runs. "
                    "The local gh login only works when the backend runs directly on your computer.")
        if reason_code == "gh_path_untrusted":
            return (f"GitHub source blocked: the gh command at {gh_path or 'the confirmed path'} changed in a way "
                    "Seraph does not accept. Check it and enable the source again.")
        if reason_code == "gh_not_logged_in":
            return ("GitHub source blocked: gh is not logged in to github.com for this user. "
                    "Run gh auth login in your terminal.")
        if reason_code == "owner_session_invalid":
            return ("GitHub source blocked: the login that enabled it is no longer current. "
                    "Enable it again in Settings → GitHub.")
        if reason_code == "gh_login_changed":
            now_as = f"@{current_login}" if current_login else "another account"
            return (f"GitHub source stopped: gh is now logged in as {now_as}, not @{login}. "
                    "Enable it again in Settings → GitHub to use this login.")
        if reason_code in {"source_revoked", "source_changed"}:
            return "GitHub collection stopped: the source was switched off or changed while it ran."
        return f"GitHub source blocked: {reason_code or 'unknown reason'}."
    if status == "failed":
        if reason_code == "github_activity_credential_rejected":
            return "GitHub source failed: GitHub rejected gh's stored login. Run gh auth login in your terminal."
        if reason_code == "gh_timed_out":
            return ("GitHub source failed: gh did not answer within 30 s and was stopped. "
                    "On macOS a keychain prompt may be waiting.")
        if reason_code == "gh_request_failed":
            return (f"GitHub source failed: gh exited with code {exit_code if exit_code is not None else 'unknown'} "
                    "without reaching GitHub (usually no network).")
        if reason_code == "gh_cleanup_unproven":
            return ("GitHub source failed: a gh process could not be confirmed stopped. "
                    "Collection will not start another one in this run.")
        if reason_code == "github_activity_rate_limited":
            return ("GitHub source failed: GitHub rate limit reached for this login; "
                    f"GitHub reports it resets at {_hhmm(at, tz)}.")
        return f"GitHub source failed: {reason_code or 'unknown reason'}."
    if status == "partial":
        parts = []
        for item in (limitations or [])[:6]:
            if item.get("code") in _INFORMATIONAL:
                continue
            text = str(item.get("code", "")).replace("_", " ")
            parts.append(f"{item['repository']}: {text}" if item.get("repository") else text)
        return "GitHub activity is incomplete: " + ("; ".join(parts) or "some reads did not finish") + "."
    if status == "complete":
        if item_count == 0:
            return f"GitHub source on as @{login}; no activity found in the chosen repositories for this day."
        return (f"GitHub activity collected as @{login} up to {_hhmm(at, tz)}: {item_count} items in "
                f"{repository_count} repositories.")
    if status == "not_collected":
        return "GitHub source is on, but no collection ran for this day."
    if status == "running":
        return f"A GitHub collection started at {_hhmm(at, tz)} and did not record completion."
    if status == "unavailable":
        return "GitHub evidence could not be read from the local store."
    if status == "active":
        return f"GitHub source on · @{login} · uses your local gh login."
    return f"GitHub source state: {status}."


# -- normalisers ---------------------------------------------------------------


def _invalid() -> GhCliError:
    return GhCliError("github_activity_response_invalid")


def _parse_time(value: Any, *, required: bool = True) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise _invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _invalid() from None
    if parsed.tzinfo is None:
        raise _invalid()
    return parsed.astimezone(timezone.utc)


def _title(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    text = _CONTROL.sub(" ", text.split("\n", 1)[0] if "\n" in text else text).strip()
    return text[:MAX_TITLE_CHARS]


def _user(value: Any) -> tuple[int, str] | None:
    if not isinstance(value, dict):
        return None
    user_id, login = value.get("id"), value.get("login")
    if type(user_id) is not int or not isinstance(login, str):
        return None
    return user_id, login[:39]


@dataclass(frozen=True)
class RepositoryInfo:
    full_name: str
    repository_id: int
    default_branch: str
    private: bool


def repository_info(body: Any) -> RepositoryInfo:
    if not isinstance(body, dict):
        raise _invalid()
    name, repo_id, branch = body.get("full_name"), body.get("id"), body.get("default_branch")
    if (not valid_repository(name) or type(repo_id) is not int
            or not isinstance(branch, str) or not branch):
        raise _invalid()
    return RepositoryInfo(full_name=name, repository_id=repo_id, default_branch=branch,
                          private=bool(body.get("private") is True))


def _record(repo: RepositoryInfo, *, kind: str, dedupe_key: str, number: int | None, external_id: str | None,
            title: str, html_url: str, occurred_at: datetime, actor: tuple[int, str], detail: dict[str, Any],
            api_path: str) -> dict[str, Any]:
    record = {"dedupe_key": dedupe_key, "repository": repo.full_name, "repository_id": repo.repository_id,
              "repository_private": repo.private, "kind": kind, "number": number, "external_id": external_id,
              "title": title, "html_url": html_url, "occurred_at": occurred_at, "actor_id": actor[0],
              "actor_login": actor[1], "detail": detail, "api_path": api_path}
    record["content_digest"] = _digest({key: value for key, value in record.items()})
    return record


def commit_record(repo: RepositoryInfo, raw: Any, *, identity_id: int, api_path: str) -> dict[str, Any] | None:
    """A commit attributed to the identity by its linked GitHub author or committer; else None."""
    if not isinstance(raw, dict) or not isinstance(raw.get("commit"), dict):
        raise _invalid()
    sha = raw.get("sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise _invalid()
    commit = raw["commit"]
    committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else None
    author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
    if committer is None:
        raise _invalid()
    occurred = _parse_time(committer.get("date"))
    author_user, committer_user = _user(raw.get("author")), _user(raw.get("committer"))
    if author_user and author_user[0] == identity_id:
        actor = author_user
    elif committer_user and committer_user[0] == identity_id:
        actor = committer_user
    else:
        return None
    authored = _parse_time(author.get("date"), required=False)
    return _record(repo, kind="commit", dedupe_key=f"gh:{repo.repository_id}:commit:{sha}", number=None,
                   external_id=sha, title=_title(commit.get("message")),
                   html_url=f"https://github.com/{repo.full_name}/commit/{sha}", occurred_at=occurred, actor=actor,
                   detail={"author_date": _iso(authored)}, api_path=api_path)


def issue_records(repo: RepositoryInfo, raw: Any, *, identity_id: int, start: datetime, until: datetime,
                  api_path: str) -> list[dict[str, Any]]:
    """Opened/closed/merged records for one of the identity's own issues or pull requests."""
    if not isinstance(raw, dict):
        raise _invalid()
    number, user = raw.get("number"), _user(raw.get("user"))
    if type(number) is not int or number <= 0 or user is None:
        raise _invalid()
    if user[0] != identity_id:
        return []
    pull = raw.get("pull_request") if isinstance(raw.get("pull_request"), dict) else None
    created = _parse_time(raw.get("created_at"))
    closed = _parse_time(raw.get("closed_at"), required=False)
    merged = _parse_time(pull.get("merged_at"), required=False) if pull is not None else None
    title = _title(raw.get("title"))
    noun, path = ("pr", "pull") if pull is not None else ("issue", "issues")
    url = f"https://github.com/{repo.full_name}/{path}/{number}"
    records = []

    def inside(value: datetime | None) -> bool:
        return value is not None and start <= value < until

    def add(kind: str, key: str, occurred: datetime, detail: dict[str, Any]) -> None:
        records.append(_record(repo, kind=kind, dedupe_key=f"gh:{repo.repository_id}:{noun}:{number}:{key}",
                               number=number, external_id=None, title=title, html_url=url, occurred_at=occurred,
                               actor=user, detail=detail, api_path=api_path))

    if inside(created):
        add(f"{noun}_opened", "opened", created, {})
    if pull is not None and inside(merged):
        add("pr_merged", "merged", merged, {"merged": True})
    elif inside(closed):
        add(f"{noun}_closed", f"closed:{_iso(closed)}", closed, {"merged": False} if pull is not None else {})
    return records


# -- the service ---------------------------------------------------------------


class _RunEnded(Exception):
    def __init__(self, status: str, reason_code: str, *, exit_code: int | None = None,
                 current_login: str | None = None) -> None:
        super().__init__(reason_code)
        self.status = status
        self.reason_code = reason_code
        self.exit_code = exit_code
        self.current_login = current_login


class _RepositoryFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


SessionCheck = Callable[[str, str], Awaitable[None]]


def _aware_or_none(value: datetime | None) -> datetime | None:
    return _aware(value)


async def live_owner_session(owner_session_id: str, owner_principal_id: str) -> None:
    """The enabling root must not have been revoked by a real change.

    Keyed to real changes only (the operator's standing rule): an explicit
    logout or revocation of that root, a bearer replacement or tombstone, the
    local no-login root being retired or its mode switched off, or a different
    principal. Its idle or absolute expiry does NOT stop the source, so this
    reads the session row instead of ``authenticate_session`` (which enforces
    those expiries). No clock is read here.

    The auth owner marks an expired row by setting ``revoked_at`` when it next
    sees it, at or after the stored expiry. A revocation stamped before both
    stored expiries can only be an explicit one, which is what stops the
    source; comparing the two stored instants classifies the cause and is not
    a validity window.
    """
    from src.auth.service import LOCAL_ROOT_TOKEN_MARKER, _is_local_root, local_no_login_active
    from src.db.models import OperatorSession
    if not owner_session_id:
        raise GitHubActivityError("owner_session_invalid", status_code=403)
    async with db_engine.get_session() as db:
        record = await db.get(OperatorSession, owner_session_id)
        if record is not None:
            db.expunge(record)
    if (record is None or record.principal_id != owner_principal_id or record.is_bearer_tombstone is not False
            or record.replaced_by_id is not None):
        raise GitHubActivityError("owner_session_invalid", status_code=403)
    if _is_local_root(record) and (not local_no_login_active()
                                   or str(record.token_hash) != LOCAL_ROOT_TOKEN_MARKER):
        raise GitHubActivityError("owner_session_invalid", status_code=403)
    revoked = _aware_or_none(record.revoked_at)
    if revoked is not None:
        expiries = [value for value in (_aware_or_none(record.idle_expires_at),
                                        _aware_or_none(record.absolute_expires_at)) if value is not None]
        expired_then = bool(expiries) and revoked >= min(expiries)
        if not expired_then:
            raise GitHubActivityError("owner_session_invalid", status_code=403)


def _scope(row: GitHubActivitySource) -> GitHubActivityScope:
    return GitHubActivityScope.model_validate(json.loads(row.scope_json))


def source_payload(row: GitHubActivitySource | None) -> dict[str, Any]:
    if row is None:
        return {"id": None, "state": "not_connected", "revision": 0}
    return {"id": row.id, "state": row.state, "revision": int(row.revision), "stopped_reason": row.stopped_reason,
            "identity_id": row.identity_id, "identity_login": row.identity_login,
            "gh_entry_path": row.gh_entry_path, "gh_install_prefix": row.gh_install_prefix,
            "scope": json.loads(row.scope_json), "scope_digest": row.scope_digest,
            "model_context_allowed": bool(row.model_context_allowed),
            "acknowledged_gh_ambient_login": bool(row.acknowledged_gh_ambient_login),
            "credential_kind": row.credential_kind, "created_at": _iso(row.created_at),
            "updated_at": _iso(row.updated_at), "revoked_at": _iso(row.revoked_at)}


def run_payload(run: GitHubActivityRun | None, *, tz: ZoneInfo | None = None,
                source: GitHubActivitySource | None = None) -> dict[str, Any] | None:
    if run is None:
        return None
    limitations = json.loads(run.limitations_json or "[]")
    counts = json.loads(run.counts_json or "{}")
    repositories = json.loads(run.repositories_json or "{}")
    detail = repositories.get("detail") if isinstance(repositories.get("detail"), dict) else {}
    at = run.rate_limit_reset_at if run.reason_code == "github_activity_rate_limited" else (
        run.collected_until_utc if run.status == "complete" else run.started_at)
    return {"id": run.id, "status": run.status, "reason_code": run.reason_code, "trigger": run.trigger,
            "report_date": run.report_date, "timezone": run.timezone,
            "window_start_utc": _iso(run.window_start_utc), "window_end_utc": _iso(run.window_end_utc),
            "collected_until_utc": _iso(run.collected_until_utc), "limitations": limitations, "counts": counts,
            "repositories": {key: value for key, value in repositories.items() if key != "detail"},
            "call_count": run.call_count, "identity_login": run.identity_login,
            "gh_real_path": run.gh_real_path, "gh_sha256": run.gh_sha256,
            "rate_limit_remaining": run.rate_limit_remaining, "rate_limit_reset_at": _iso(run.rate_limit_reset_at),
            "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
            "message": activity_status_message(
                run.status, run.reason_code, login=(source.identity_login if source else run.identity_login),
                current_login=detail.get("current_login"), gh_path=run.gh_real_path,
                exit_code=detail.get("exit_code"), item_count=sum(int(v) for v in counts.values()),
                repository_count=len(repositories.get("read") or []), limitations=limitations, at=at, tz=tz)}


def item_payload(row: GitHubActivityItem) -> dict[str, Any]:
    return {"id": row.id, "repository": row.repository, "kind": row.kind, "number": row.number,
            "external_id": row.external_id, "title": row.title, "url": row.html_url,
            "occurred_at": _iso(row.occurred_at), "actor_login": row.actor_login,
            "detail": json.loads(row.detail_json or "{}"), "api_path": row.api_path,
            "first_run_id": row.first_run_id, "last_run_id": row.last_run_id}


@dataclass
class _Collection:
    service: "GitHubActivityService"
    source: GitHubActivitySource
    scope: GitHubActivityScope
    binary: GhBinary
    run_id: str
    start: datetime
    until: datetime
    now: datetime
    calls: int = 0
    identity_login: str | None = None
    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    limitations: list[dict[str, Any]] = field(default_factory=list)
    repositories_read: list[str] = field(default_factory=list)
    repositories_failed: list[dict[str, str]] = field(default_factory=list)
    repositories_skipped: list[str] = field(default_factory=list)
    rate_limit_remaining: int | None = None
    rate_limit_reset_at: datetime | None = None
    exhausted: bool = False

    def limit(self, code: str, repository: str | None = None) -> None:
        entry: dict[str, Any] = {"code": code}
        if repository:
            entry["repository"] = repository
        if entry not in self.limitations and len(self.limitations) < 50:
            self.limitations.append(entry)

    async def authority(self, op: str, repository: str | None, *, discovered: bool = True) -> None:
        current = await self.service._source_row(self.source.owner_principal_id)
        if current is None or current.state != "active" or current.id != self.source.id:
            raise _RunEnded("blocked", "source_revoked")
        if int(current.revision) != int(self.source.revision):
            raise _RunEnded("blocked", "source_changed")
        try:
            await self.service._session_check(current.owner_session_id, current.owner_principal_id)
        except GitHubActivityError as exc:
            raise _RunEnded("blocked", exc.code) from exc
        if repository is not None and not self.scope.allows(repository, discovered=discovered):
            raise _RunEnded("blocked", "repository_outside_scope")

    async def call(self, op: str, *, repository: str | None = None, discovered: bool = True,
                   **values: Any) -> GhResponse:
        if self.exhausted:
            raise _RunEnded("partial", "github_activity_rate_limited")
        if self.calls >= MAX_CALLS_PER_RUN:
            raise _RunEnded("partial", "call_budget_exhausted")

        async def before_start(checked_op: str, checked_repository: str | None) -> None:
            await self.authority(checked_op, checked_repository, discovered=discovered)
            self.calls += 1

        try:
            response = await self.service._reader.read(self.binary, op=op, authority_check=before_start,
                                                       repository=repository, login=values.get("login"),
                                                       number=values.get("number"), branch=values.get("branch"),
                                                       since=values.get("since"), until=values.get("until"),
                                                       page=values.get("page", 1))
        except GhValueInvalid:
            # Refused before any child started: the caller decides whether to skip.
            raise
        except GhCliError as exc:
            self._note_rate_limit(exc.headers)
            if exc.code in _REPOSITORY_CODES and repository is not None:
                raise _RepositoryFailed(exc.code) from None
            if exc.code == "github_activity_rate_limited":
                self.exhausted = True
            if exc.code in _BLOCKING_CODES:
                raise _RunEnded("blocked", exc.code, exit_code=exc.exit_code) from None
            raise _RunEnded("failed", exc.code, exit_code=exc.exit_code) from None
        # A toggle-off or root change during the call discards its result.
        await self.authority(op, None)
        self._note_rate_limit(response.headers)
        return response

    def _note_rate_limit(self, headers: dict[str, str]) -> None:
        remaining = _small_int(headers.get("x-ratelimit-remaining"))
        if remaining is not None:
            self.rate_limit_remaining = remaining
            if remaining == 0:
                self.exhausted = True
        reset = _small_int(headers.get("x-ratelimit-reset"))
        if reset is not None:
            try:
                self.rate_limit_reset_at = datetime.fromtimestamp(reset, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                self.rate_limit_reset_at = None

    def keep(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            if record["kind"] in self.scope.kinds and self.start <= record["occurred_at"] < self.until:
                self.items[record["dedupe_key"]] = record

    async def execute(self) -> None:
        identity = (await self.call("identity")).body
        identity_user = _user(identity)
        if identity_user is None:
            raise _RunEnded("failed", "github_activity_response_invalid")
        if identity_user[0] != self.source.identity_id:
            await self.service._stop_for_login_change(self.source)
            raise _RunEnded("blocked", "gh_login_changed", current_login=identity_user[1])
        self.identity_login = identity_user[1]
        discovered, branches = await self._discover()
        for repository in self._resolve(discovered):
            await self._read_repository(repository, branches.get(repository.lower(), []),
                                        explicit=repository.lower() in {r.lower() for r in self.scope.repositories})

    async def _discover(self) -> tuple[dict[str, int], dict[str, list[str]]]:
        counts: dict[str, int] = {}
        names: dict[str, str] = {}
        branches: dict[str, list[str]] = {}
        if not self.scope.discover_from_activity:
            return {}, {}
        if self.now - self.start > timedelta(days=EVENTS_HORIZON_DAYS):
            # GitHub keeps 30 days of events; discovery cannot see this day.
            self.limit("events_window_unavailable")
            return {}, {}
        self.limit("events_discovery_may_lag")
        max_pages = OPS["events"][2]
        for page in range(1, max_pages + 1):
            body = (await self.call("events", login=self.identity_login, page=page)).body
            oldest: datetime | None = None
            for event in body:
                if not isinstance(event, dict):
                    raise _invalid_end()
                created = _parse_event_time(event.get("created_at"))
                oldest = created if oldest is None or created < oldest else oldest
                actor = _user(event.get("actor"))
                repo = event.get("repo") if isinstance(event.get("repo"), dict) else {}
                name = repo.get("name")
                if (actor is None or actor[0] != self.source.identity_id or not (self.start <= created < self.until)
                        or not valid_repository(name)):
                    continue
                key = name.lower()
                names[key] = name
                counts[key] = counts.get(key, 0) + 1
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                ref = payload.get("ref")
                if event.get("type") == "PushEvent" and isinstance(ref, str) and ref.startswith("refs/heads/"):
                    branch = ref.removeprefix("refs/heads/")
                    if branch and branch not in branches.setdefault(key, []):
                        branches[key].append(branch)
            if len(body) < PER_PAGE or (oldest is not None and oldest < self.start):
                break
            if page == max_pages:
                self.limit("events_truncated")
        return {names[key]: count for key, count in counts.items()}, branches

    def _resolve(self, discovered: dict[str, int]) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for repository in self.scope.repositories:
            if self.scope.allows(repository, discovered=False) and repository.lower() not in seen:
                ordered.append(repository)
                seen.add(repository.lower())
        for repository, _ in sorted(discovered.items(), key=lambda item: (-item[1], item[0].lower())):
            if repository.lower() in seen:
                continue
            if not self.scope.allows(repository, discovered=True):
                continue
            ordered.append(repository)
            seen.add(repository.lower())
        if len(ordered) > MAX_REPOSITORIES:
            self.limit("repository_cap_reached")
            self.repositories_skipped.extend(ordered[MAX_REPOSITORIES:])
            ordered = ordered[:MAX_REPOSITORIES]
        return ordered

    async def _pages(self, op: str, repository: str, *, discovered: bool, stop: Callable[[list[Any]], bool] | None = None,
                     **values: Any) -> list[Any]:
        rows: list[Any] = []
        max_pages = OPS[op][2]
        for page in range(1, max_pages + 1):
            body = (await self.call(op, repository=repository, discovered=discovered, page=page, **values)).body
            rows.extend(body)
            if len(body) < PER_PAGE or (stop is not None and stop(body)):
                break
            if page == max_pages:
                self.limit(f"{op}_truncated", repository)
        return rows

    async def _read_repository(self, repository: str, pushed: list[str], *, explicit: bool) -> None:
        discovered = not explicit
        try:
            repo = repository_info((await self.call("repository", repository=repository, discovered=discovered)).body)
            identity = self.source.identity_id

            def older(body: list[Any]) -> bool:
                last = body[-1] if body else None
                return isinstance(last, dict) and _parse_event_time(last.get("updated_at")) < self.start

            issues = await self._pages("issues", repository, discovered=discovered, stop=older, since=self.start)
            own_pulls: list[int] = []
            api_path = OPS["issues"][0]
            for raw in issues:
                self.keep(issue_records(repo, raw, identity_id=identity, start=self.start, until=self.until,
                                        api_path=api_path))
                user = _user(raw.get("user")) if isinstance(raw, dict) else None
                if (isinstance(raw, dict) and isinstance(raw.get("pull_request"), dict) and user
                        and user[0] == identity and _parse_event_time(raw.get("updated_at")) >= self.start
                        and len(own_pulls) < MAX_OWN_PULLS):
                    own_pulls.append(raw["number"])
            if "commit" not in self.scope.kinds:
                return
            branches = [repo.default_branch] + [b for b in pushed if b != repo.default_branch][:MAX_PUSHED_BRANCHES]
            if len([b for b in pushed if b != repo.default_branch]) > MAX_PUSHED_BRANCHES:
                self.limit("pushed_branches_truncated", repository)
            for branch in branches:
                # A branch name Seraph cannot send is skipped and named, never fatal.
                if not valid_branch(branch):
                    self.limit("branch_name_skipped", repository)
                    continue
                try:
                    commits = await self._pages("commits", repository, discovered=discovered, branch=branch,
                                                login=self.identity_login, since=self.start, until=self.until)
                except GhValueInvalid:
                    self.limit("branch_name_skipped", repository)
                    continue
                self._keep_commits(repo, commits, OPS["commits"][0])
            for number in own_pulls:
                commits = await self._pages("pull_commits", repository, discovered=discovered, number=number)
                self._keep_commits(repo, commits, OPS["pull_commits"][0])
            self.repositories_read.append(repo.full_name)
        except _RepositoryFailed as exc:
            self.repositories_failed.append({"repository": repository, "code": exc.code})
            self.limit(exc.code.removeprefix("github_activity_"), repository)
        except GhCliError as exc:
            # A malformed record ends the run as response_invalid.
            raise _RunEnded("failed", exc.code) from None

    def _keep_commits(self, repo: RepositoryInfo, commits: list[Any], api_path: str) -> None:
        unlinked = False
        for raw in commits:
            record = commit_record(repo, raw, identity_id=self.source.identity_id, api_path=api_path)
            if record is None:
                if isinstance(raw, dict) and raw.get("author") is None:
                    unlinked = True
                continue
            self.keep([record])
        if unlinked:
            self.limit("unlinked_commit_emails", repo.full_name)


def _small_int(value: str | None) -> int | None:
    """An ASCII decimal header value of at most 12 digits, else None."""
    if not value or len(value) > 12 or not value.isascii() or not value.isdigit():
        return None
    return int(value)


def _invalid_end() -> _RunEnded:
    return _RunEnded("failed", "github_activity_response_invalid")


def _parse_event_time(value: Any) -> datetime:
    try:
        parsed = _parse_time(value)
    except GhCliError:
        raise _invalid_end() from None
    assert parsed is not None
    return parsed


class GitHubActivityService:
    def __init__(self, *, reader: GhCliReader | None = None, session_check: SessionCheck | None = None) -> None:
        self._reader = reader or GhCliReader()
        self._session_check = session_check or live_owner_session

    # -- storage ------------------------------------------------------------

    async def _source_row(self, owner_principal_id: str) -> GitHubActivitySource | None:
        async with db_engine.get_session() as db:
            row = (await db.execute(select(GitHubActivitySource).where(
                GitHubActivitySource.owner_principal_id == owner_principal_id))).scalars().first()
            if row is not None:
                db.expunge(row)
            return row

    async def _latest_run(self, *, owner_principal_id: str, report_date: str | None = None,
                          source_id: str | None = None) -> GitHubActivityRun | None:
        async with db_engine.get_session() as db:
            query = select(GitHubActivityRun).where(GitHubActivityRun.owner_principal_id == owner_principal_id)
            if report_date is not None:
                query = query.where(GitHubActivityRun.report_date == report_date)
            if source_id is not None:
                query = query.where(GitHubActivityRun.source_id == source_id)
            row = (await db.execute(query.order_by(GitHubActivityRun.started_at.desc(),
                                                   GitHubActivityRun.id.desc()).limit(1))).scalars().first()
            if row is not None:
                db.expunge(row)
            return row

    async def _stop_for_login_change(self, source: GitHubActivitySource) -> None:
        now = get_clock().now(timezone.utc)
        async with db_engine.get_session() as db:
            await db.execute(update(GitHubActivitySource).where(
                GitHubActivitySource.id == source.id, GitHubActivitySource.revision == source.revision,
                GitHubActivitySource.state == "active").values(
                state="stopped", stopped_reason="gh_login_changed", revision=GitHubActivitySource.revision + 1,
                updated_at=now))

    # -- status and consent ---------------------------------------------------

    def _gh_presence(self, row: GitHubActivitySource | None) -> dict[str, Any]:
        try:
            binary = self._reader.locate(row.gh_entry_path if row else None,
                                         install_prefix=row.gh_install_prefix if row else None, digest=False)
        except GhCliError as exc:
            return {"available": False, "reason_code": exc.code,
                    "entry_path": row.gh_entry_path if row else None}
        return {"available": True, "reason_code": None, "entry_path": binary.entry_path,
                "real_path": binary.real_path}

    async def status(self, *, owner_principal_id: str, tz: ZoneInfo | None = None) -> dict[str, Any]:
        row = await self._source_row(owner_principal_id)
        last = await self._latest_run(owner_principal_id=owner_principal_id,
                                      source_id=row.id if row else None) if row else None
        state = row.state if row else "not_connected"
        return {"source": source_payload(row), "gh": self._gh_presence(row), "credential": dict(CREDENTIAL),
                "grant_sentence": GRANT_SENTENCE, "candidate_paths": list(self._reader.candidates()),
                "endpoints": [f"GET {template}" for template, *_ in OPS.values()],
                "kinds": list(ALL_KINDS), "last_run": run_payload(last, tz=tz, source=row),
                "message": activity_status_message(state, row.stopped_reason if row else None,
                                                   login=row.identity_login if row else None)}

    async def check_gh_login(self, *, gh_entry_path: str | None = None) -> dict[str, Any]:
        """One operator-permitted use: gh --version and GET /user. Stores nothing."""
        binary = await self._locate_or_raise(gh_entry_path)

        async def permitted(op: str, repository: str | None) -> None:
            if op != "identity" or repository is not None:
                raise GitHubActivityError("github_activity_op_refused", status_code=403)

        try:
            version = await self._reader.version(binary)
            identity = _user((await self._reader.read(binary, op="identity", authority_check=permitted)).body)
        except GhCliError as exc:
            raise GitHubActivityError(exc.code, status_code=_check_status(exc.code)) from None
        if identity is None:
            raise GitHubActivityError("github_activity_response_invalid", status_code=502)
        return {"gh_version": version, "gh_entry_path": binary.entry_path, "gh_real_path": binary.real_path,
                "identity_id": identity[0], "identity_login": identity[1], "credential": dict(CREDENTIAL),
                "grant_sentence": GRANT_SENTENCE}

    async def _locate_or_raise(self, gh_entry_path: str | None, install_prefix: str | None = None) -> GhBinary:
        try:
            return await self._reader.alocate(gh_entry_path, install_prefix=install_prefix)
        except GhCliError as exc:
            raise GitHubActivityError(exc.code, status_code=409) from None

    async def enable_source(self, *, owner_principal_id: str, owner_session_id: str, expected_revision: int,
                            scope: GitHubActivityScope, acknowledged: bool, expected_identity_id: int,
                            expected_gh_entry_path: str, allow_model_context: bool) -> dict[str, Any]:
        if acknowledged is not True:
            raise GitHubActivityError("gh_ambient_login_acknowledgement_required", status_code=422)
        binary = await self._locate_or_raise(expected_gh_entry_path)

        async def permitted(op: str, repository: str | None) -> None:
            if op != "identity" or repository is not None:
                raise GitHubActivityError("github_activity_op_refused", status_code=403)

        try:
            identity = _user((await self._reader.read(binary, op="identity", authority_check=permitted)).body)
        except GhCliError as exc:
            raise GitHubActivityError(exc.code, status_code=_check_status(exc.code)) from None
        if identity is None:
            raise GitHubActivityError("github_activity_response_invalid", status_code=502)
        if identity[0] != expected_identity_id:
            raise GitHubActivityError("gh_identity_changed", status_code=409)
        scope_value = scope.model_dump(mode="json")
        now = get_clock().now(timezone.utc)
        values = dict(owner_session_id=owner_session_id, credential_kind=CREDENTIAL_KIND,
                      gh_entry_path=binary.entry_path, gh_install_prefix=binary.install_prefix, state="active",
                      stopped_reason=None, scope_json=json.dumps(scope_value, sort_keys=True),
                      scope_digest=_digest(scope_value), acknowledged_gh_ambient_login=True,
                      model_context_allowed=bool(allow_model_context), identity_id=identity[0],
                      identity_login=identity[1], updated_at=now, revoked_at=None)
        async with db_engine.get_session() as db:
            row = (await db.execute(select(GitHubActivitySource).where(
                GitHubActivitySource.owner_principal_id == owner_principal_id))).scalars().first()
            if row is None:
                if expected_revision != 0:
                    raise GitHubActivityError("github_activity_revision_conflict", status_code=409)
                db.add(GitHubActivitySource(owner_principal_id=owner_principal_id, revision=1, created_at=now,
                                            **values))
                try:
                    await db.flush()
                except IntegrityError:
                    # A concurrent first enable won the unique owner row.
                    raise GitHubActivityError("github_activity_revision_conflict", status_code=409) from None
            else:
                changed = await db.execute(update(GitHubActivitySource).where(
                    GitHubActivitySource.id == row.id,
                    GitHubActivitySource.revision == expected_revision).values(
                    revision=GitHubActivitySource.revision + 1, **values))
                if changed.rowcount != 1:
                    raise GitHubActivityError("github_activity_revision_conflict", status_code=409)
        return await self.status(owner_principal_id=owner_principal_id)

    async def revoke_source(self, *, owner_principal_id: str, expected_revision: int) -> dict[str, Any]:
        now = get_clock().now(timezone.utc)
        async with db_engine.get_session() as db:
            row = (await db.execute(select(GitHubActivitySource).where(
                GitHubActivitySource.owner_principal_id == owner_principal_id))).scalars().first()
            if row is None:
                raise GitHubActivityError("github_activity_source_not_found", status_code=404)
            changed = await db.execute(update(GitHubActivitySource).where(
                GitHubActivitySource.id == row.id, GitHubActivitySource.revision == expected_revision).values(
                state="revoked", revision=GitHubActivitySource.revision + 1, updated_at=now, revoked_at=now))
            if changed.rowcount != 1:
                raise GitHubActivityError("github_activity_revision_conflict", status_code=409)
        return await self.status(owner_principal_id=owner_principal_id)

    # -- collection -----------------------------------------------------------

    async def collect_day(self, *, owner_principal_id: str, report_day: date, tz: ZoneInfo,
                          trigger: Literal["scheduled", "manual_report", "operator"]) -> dict[str, Any]:
        source = await self._source_row(owner_principal_id)
        if source is None:
            return {"status": "not_connected", "reason_code": None, "run": None,
                    "message": activity_status_message("not_connected")}
        if source.state != "active":
            return {"status": source.state, "reason_code": source.stopped_reason, "run": None,
                    "message": activity_status_message(source.state, source.stopped_reason,
                                                       login=source.identity_login)}
        try:
            start, end = local_day_window(report_day, tz)
        except (OverflowError, ValueError):
            raise GitHubActivityError("report_date_invalid", status_code=422) from None
        now = get_clock().now(timezone.utc)
        until = min(end, now)
        if until <= start:
            raise GitHubActivityError("report_day_in_future", status_code=422)
        previous = await self._latest_run(owner_principal_id=owner_principal_id, source_id=source.id)
        run = GitHubActivityRun(owner_principal_id=owner_principal_id, source_id=source.id,
                                source_revision=int(source.revision), trigger=trigger,
                                report_date=report_day.isoformat(), timezone=str(tz.key), window_start_utc=start,
                                window_end_utc=end, collected_until_utc=until, status="running",
                                identity_id=source.identity_id, identity_login=source.identity_login,
                                started_at=now)
        async with db_engine.get_session() as db:
            db.add(run)
            await db.flush()
            run_id = run.id
            db.expunge(run)
        collection: _Collection | None = None
        detail: dict[str, Any] = {}
        try:
            status, code, binary = "complete", None, None
            try:
                binary = await self._reader.alocate(source.gh_entry_path, install_prefix=source.gh_install_prefix)
            except GhCliError as exc:
                status, code = "blocked", exc.code
            if binary is not None:
                try:
                    await self._session_check(source.owner_session_id, owner_principal_id)
                except GitHubActivityError as exc:
                    status, code = "blocked", exc.code
            if code is None and binary is not None:
                collection = _Collection(service=self, source=source, scope=_scope(source), binary=binary,
                                         run_id=run_id, start=start, until=until, now=now)
                if previous is not None and previous.gh_sha256 and previous.gh_sha256 != binary.sha256:
                    collection.limit("gh_binary_changed_since_last_run")
                try:
                    await collection.execute()
                except _RunEnded as ended:
                    status, code = ended.status, ended.reason_code
                    if ended.current_login:
                        detail["current_login"] = ended.current_login
                    if ended.exit_code is not None:
                        detail["exit_code"] = ended.exit_code
                incomplete = any(item["code"] not in _INFORMATIONAL for item in collection.limitations)
                if code is None:
                    status = "partial" if incomplete else "complete"
                elif status != "blocked":
                    if code == "call_budget_exhausted":
                        collection.limit("call_budget_exhausted")
                        status = "partial"
                    else:
                        if code == "github_activity_rate_limited":
                            collection.limit("rate_limited")
                        # Items already read are kept; the run says what stopped it.
                        status = "partial" if collection.items else "failed"
            if binary is not None:
                await self._record_binary(run_id, binary)
        except Exception as exc:
            # Never leave a run "running": an unexpected error ends it failed.
            logger.warning("GitHub activity collection failed (%s)", type(exc).__name__)
            status, code = "failed", "github_activity_internal_error"
            if collection is not None:
                collection.items.clear()
        return await self._finish(run_id, owner_principal_id, source, tz, status=status, reason_code=code,
                                  calls=collection.calls if collection else 0, collection=collection,
                                  detail=detail)

    async def _record_binary(self, run_id: str, binary: GhBinary) -> None:
        async with db_engine.get_session() as db:
            await db.execute(update(GitHubActivityRun).where(GitHubActivityRun.id == run_id).values(
                gh_real_path=binary.real_path, gh_sha256=binary.sha256))

    async def _finish(self, run_id: str, owner_principal_id: str, source: GitHubActivitySource, tz: ZoneInfo, *,
                      status: str, reason_code: str | None, calls: int, collection: _Collection | None,
                      detail: dict[str, Any] | None = None) -> dict[str, Any]:
        now = get_clock().now(timezone.utc)
        # A blocked run stores nothing: authority changed (or never held) while it ran.
        records = list(collection.items.values()) if collection and status != "blocked" else []
        repositories = {"read": collection.repositories_read if collection else [],
                        "failed": collection.repositories_failed if collection else [],
                        "skipped": collection.repositories_skipped if collection else [],
                        "detail": detail or {}}
        limitations = collection.limitations if collection else []
        counts: dict[str, int] = {}
        try:
            async with db_engine.get_session() as db:
                if records:
                    # Guard and upsert in one transaction. The no-op UPDATE takes
                    # the write lock, so a revoke or scope edit either commits
                    # before it (no match: nothing is stored) or waits for it.
                    guard = await db.execute(update(GitHubActivitySource).where(
                        GitHubActivitySource.id == source.id, GitHubActivitySource.state == "active",
                        GitHubActivitySource.revision == int(source.revision)).values(id=GitHubActivitySource.id))
                    if guard.rowcount != 1:
                        records = []
                        status, reason_code = "blocked", "source_changed"
                for record in records:
                    counts[record["kind"]] = counts.get(record["kind"], 0) + 1
                    values = {key: value for key, value in record.items() if key != "detail"}
                    values.update(id=_new_id(), owner_principal_id=owner_principal_id, source_id=source.id,
                                  detail_json=json.dumps(record["detail"], sort_keys=True),
                                  first_run_id=run_id, last_run_id=run_id, first_seen_at=now, last_seen_at=now)
                    statement = sqlite_insert(GitHubActivityItem).values(**values)
                    await db.execute(statement.on_conflict_do_update(
                        index_elements=["owner_principal_id", "dedupe_key"],
                        set_={"title": statement.excluded.title, "detail_json": statement.excluded.detail_json,
                              "content_digest": statement.excluded.content_digest,
                              "html_url": statement.excluded.html_url,
                              "last_run_id": statement.excluded.last_run_id,
                              "last_seen_at": statement.excluded.last_seen_at,
                              "repository": statement.excluded.repository}))
                await db.execute(update(GitHubActivityRun).where(GitHubActivityRun.id == run_id).values(
                    status=status, reason_code=reason_code, limitations_json=json.dumps(limitations),
                    counts_json=json.dumps(counts, sort_keys=True), repositories_json=json.dumps(repositories),
                    call_count=calls, finished_at=now,
                    identity_login=(collection.identity_login if collection and collection.identity_login
                                    else source.identity_login),
                    rate_limit_remaining=collection.rate_limit_remaining if collection else None,
                    rate_limit_reset_at=collection.rate_limit_reset_at if collection else None))
        except Exception as exc:
            logger.warning("GitHub activity storage failed (%s)", type(exc).__name__)
            status, reason_code = "failed", "github_activity_storage_error"
            try:
                # Record the failure on its own so the run is not left "running".
                async with db_engine.get_session() as db:
                    await db.execute(update(GitHubActivityRun).where(GitHubActivityRun.id == run_id).values(
                        status=status, reason_code=reason_code, call_count=calls, finished_at=now))
            except Exception as again:
                logger.warning("GitHub activity run finalisation failed (%s)", type(again).__name__)
            await self._audit(owner_principal_id, status, reason_code, calls, {})
            return {"status": status, "reason_code": reason_code, "run": None,
                    "message": activity_status_message(status, reason_code)}
        await self._audit(owner_principal_id, status, reason_code, calls, counts)
        run = await self._latest_run_by_id(run_id)
        payload = run_payload(run, tz=tz, source=source)
        return {"status": status, "reason_code": reason_code, "run": payload,
                "message": payload["message"] if payload else activity_status_message(status, reason_code)}

    async def _latest_run_by_id(self, run_id: str) -> GitHubActivityRun | None:
        async with db_engine.get_session() as db:
            row = await db.get(GitHubActivityRun, run_id)
            if row is not None:
                db.expunge(row)
            return row

    async def _audit(self, owner_principal_id: str, status: str, reason_code: str | None, calls: int,
                     counts: dict[str, int]) -> None:
        try:
            from src.audit.runtime import log_integration_event
            await log_integration_event(integration_type="github_activity", name="gh_cli", outcome=status,
                                        details={"reason_code": reason_code, "call_count": calls,
                                                 "counts": counts, "credential": CREDENTIAL_KIND},
                                        principal_id=owner_principal_id)
        except Exception as exc:
            logger.warning("GitHub activity audit failed (%s)", type(exc).__name__)

    async def activity_for_day(self, *, owner_principal_id: str, report_day: date, tz: ZoneInfo) -> dict[str, Any]:
        try:
            start, end = local_day_window(report_day, tz)
        except (OverflowError, ValueError):
            raise GitHubActivityError("report_date_invalid", status_code=422) from None
        source = await self._source_row(owner_principal_id)
        async with db_engine.get_session() as db:
            rows = (await db.execute(select(GitHubActivityItem).where(
                GitHubActivityItem.owner_principal_id == owner_principal_id,
                GitHubActivityItem.occurred_at >= start, GitHubActivityItem.occurred_at < end).order_by(
                GitHubActivityItem.occurred_at, GitHubActivityItem.dedupe_key).limit(MAX_ITEMS_RETURNED + 1)
            )).scalars().all()
        run = await self._latest_run(owner_principal_id=owner_principal_id, report_date=report_day.isoformat())
        payload = run_payload(run, tz=tz, source=source)
        if source is None:
            status, message = "not_connected", activity_status_message("not_connected")
        elif source.state != "active":
            status = source.state
            message = activity_status_message(source.state, source.stopped_reason, login=source.identity_login)
        elif payload is None:
            status, message = "not_collected", activity_status_message("not_collected")
        else:
            status, message = payload["status"], payload["message"]
        return {"report_date": report_day.isoformat(), "timezone": str(tz.key),
                "window_start_utc": _iso(start), "window_end_utc": _iso(end), "status": status,
                "message": message, "run": payload, "items": [item_payload(row) for row in rows[:MAX_ITEMS_RETURNED]],
                "items_truncated": len(rows) > MAX_ITEMS_RETURNED, "grant_sentence": GRANT_SENTENCE}


def _check_status(code: str) -> int:
    return 409 if code in {"gh_not_logged_in", "gh_unavailable", "gh_path_untrusted"} else 502


def _new_id() -> str:
    import uuid
    return uuid.uuid4().hex


def user_timezone() -> ZoneInfo:
    from config.settings import settings
    try:
        return ZoneInfo(settings.user_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


# -- router --------------------------------------------------------------------

github_activity_router = APIRouter(prefix="/capabilities/github/activity", tags=["github-activity"])


def _service() -> GitHubActivityService:
    return GitHubActivityService()


def _owner(request: Request) -> tuple[str, str]:
    from src.extensions.github_followthrough import GitHubFollowthroughError, _operator, _principal_id, _session_id
    try:
        operator = _operator(request)
        return _principal_id(operator), _session_id(operator)
    except GitHubFollowthroughError as exc:
        raise HTTPException(status_code=exc.status_code, detail={"code": exc.code}) from exc


def _http(exc: GitHubActivityError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code})


class GhLoginCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    acknowledge_gh_login_check: Literal[True]
    gh_entry_path: str | None = Field(default=None, min_length=1, max_length=512)


class GitHubActivityEnableRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    scope: GitHubActivityScope
    acknowledge_gh_ambient_login: Literal[True]
    expected_identity_id: int = Field(gt=0)
    expected_gh_entry_path: str = Field(min_length=1, max_length=512)
    allow_model_context: bool = False


class GitHubActivityRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)


class GitHubActivityCollectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    report_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


def _day(value: str | None, tz: ZoneInfo) -> date:
    if value is None:
        return get_clock().now(tz).date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "report_date_invalid"}) from exc


@github_activity_router.get("")
async def get_github_activity(request: Request):
    principal, _ = _owner(request)
    return await _service().status(owner_principal_id=principal, tz=user_timezone())


@github_activity_router.post("/gh/check")
async def check_github_activity_gh(body: GhLoginCheckRequest, request: Request):
    _owner(request)
    try:
        return await _service().check_gh_login(gh_entry_path=body.gh_entry_path)
    except GitHubActivityError as exc:
        raise _http(exc) from exc


@github_activity_router.put("/source")
async def enable_github_activity(body: GitHubActivityEnableRequest, request: Request):
    principal, session = _owner(request)
    try:
        return await _service().enable_source(
            owner_principal_id=principal, owner_session_id=session, expected_revision=body.expected_revision,
            scope=body.scope, acknowledged=body.acknowledge_gh_ambient_login,
            expected_identity_id=body.expected_identity_id, expected_gh_entry_path=body.expected_gh_entry_path,
            allow_model_context=body.allow_model_context)
    except GitHubActivityError as exc:
        raise _http(exc) from exc


@github_activity_router.post("/revoke")
async def revoke_github_activity(body: GitHubActivityRevokeRequest, request: Request):
    principal, _ = _owner(request)
    try:
        return await _service().revoke_source(owner_principal_id=principal, expected_revision=body.expected_revision)
    except GitHubActivityError as exc:
        raise _http(exc) from exc


@github_activity_router.post("/collect")
async def collect_github_activity(body: GitHubActivityCollectRequest, request: Request):
    principal, _ = _owner(request)
    tz = user_timezone()
    try:
        return await _service().collect_day(owner_principal_id=principal, report_day=_day(body.report_date, tz),
                                            tz=tz, trigger="operator")
    except GitHubActivityError as exc:
        raise _http(exc) from exc


@github_activity_router.get("/items")
async def get_github_activity_items(request: Request,
                                    report_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    principal, _ = _owner(request)
    tz = user_timezone()
    try:
        return await _service().activity_for_day(owner_principal_id=principal, report_day=_day(report_date, tz),
                                                 tz=tz)
    except GitHubActivityError as exc:
        raise _http(exc) from exc
