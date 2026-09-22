"""Bounded, owner-controlled GitHub publication for guardian dossiers.

This adapter owns only two external mutations: creating an issue and adding a
comment to an existing issue or pull request.  It deliberately keeps the
existing MCP connector routes intact.  A publication is admitted through the
canonical durable job row, held behind the existing approval repository, sent
once through the pinned HTTPS transport, and accepted only after an independent
destination readback.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from dataclasses import replace as replace_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from src.approval.repository import approval_repository, fingerprint_tool_call
from src.auth.service import AuthFailure, authenticate_session
from src.db import engine as db_engine
from src.db.models import (
    Goal,
    GuardianDecisionPacket,
    GuardianSourceWatch,
    GitHubFollowthroughConnection,
)
from src.security.http_transport import (
    PinnedTransportError,
    PinnedResponse,
    request_pinned_https,
)
from src.security.trust_contract import AuthorityGrant
from src.guardian.source_watch import _goal_admission
from src.tools.filesystem_tool import (
    _read_workspace_text_bounded,
    _safe_resolve,
    _write_workspace_text_bounded,
)
from src.vault.repository import vault_repository
from src.workflows.job_runtime import (
    DurableJobError,
    DurableJobIdentity,
    DurableJobSpec,
    DurableJobTransitionError,
    durable_job_repository,
)


CAPABILITY_ID = "work.github-followthrough.v1"
CAPABILITY_VERSION = "1"
JOB_KIND = "github_followthrough_v1"
CONNECTION_MODE_DISABLED = "disabled"
CONNECTION_MODE_ACTIVE = "active"
CONNECTION_MODE_RECONCILE_ONLY = "reconcile_only"
CONNECTION_MODES = frozenset(
    {CONNECTION_MODE_DISABLED, CONNECTION_MODE_ACTIVE, CONNECTION_MODE_RECONCILE_ONLY}
)
ACTION_CREATE_ISSUE = "create_issue"
ACTION_CREATE_COMMENT = "create_comment"
ACTIONS = frozenset({ACTION_CREATE_ISSUE, ACTION_CREATE_COMMENT})
GITHUB_ORIGIN = "https://api.github.com"
GITHUB_HOST = "api.github.com"
GITHUB_API_VERSION = "2026-03-10"
PREPARE_DEADLINE_SECONDS = 600
EXECUTION_DEADLINE_SECONDS = 120
APPROVAL_TTL_SECONDS = 5 * 60
MAX_BODY_BYTES = 20_000
MAX_RESPONSE_BYTES = 1_024 * 1_024
MAX_ARTIFACT_BYTES = 96 * 1024
READBACK_ATTEMPTS = 3
RUNNER_PREFIX = "github-followthrough:"
MARKER_PREFIX = "<!-- seraph-operation:"
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE_VAULT_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class GitHubFollowthroughError(ValueError):
    """Stable operator-facing error for this bounded adapter."""

    def __init__(self, code: str, message: str | None = None, *, status_code: int = 409):
        self.code = code
        self.status_code = status_code
        super().__init__(message or code)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed


def _queued_approval_is_current(current: Mapping[str, Any]) -> bool:
    """Require the durable approval-resume receipt before claiming queued work."""
    authority = current.get("declared_authority")
    approval_id = _text(authority.get("approval_id")) if isinstance(authority, Mapping) else ""
    if not approval_id:
        return False
    now = _now().timestamp()
    for item in current.get("effects") or []:
        if not isinstance(item, Mapping) or item.get("kind") != "approval_resume":
            continue
        if item.get("status") != "approved" or _text(item.get("approval_id")) != approval_id:
            continue
        try:
            expires_at = float(item.get("expires_at"))
        except (TypeError, ValueError):
            continue
        if expires_at > now:
            return True
    return False


def _text(value: Any) -> str:
    return str(value or "").strip()


def _repository(value: Any) -> str:
    repository = _text(value)
    if not repository or len(repository) > 200 or not _REPOSITORY_RE.fullmatch(repository):
        raise GitHubFollowthroughError("repository_invalid", status_code=422)
    owner, repo = repository.split("/", 1)
    if owner in {".", ".."} or repo in {".", ".."}:
        raise GitHubFollowthroughError("repository_invalid", status_code=422)
    return f"{owner}/{repo}"


def _vault_key(value: Any) -> str:
    key = _text(value)
    if not key or not _SAFE_VAULT_KEY_RE.fullmatch(key):
        raise GitHubFollowthroughError("vault_key_invalid", status_code=422)
    return key


def _positive_id(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GitHubFollowthroughError(f"{field}_invalid", status_code=422)
    return value


def _canonical_path(repository: str, action: str, issue_number: int | None, remote_id: int | None = None) -> str:
    owner, repo = repository.split("/", 1)
    if action == ACTION_CREATE_ISSUE:
        number = issue_number if issue_number is not None else remote_id
        if number is None:
            raise GitHubFollowthroughError("issue_number_missing")
        return f"/repos/{owner}/{repo}/issues/{number}"
    if remote_id is None:
        raise GitHubFollowthroughError("remote_id_missing")
    return f"/repos/{owner}/{repo}/issues/comments/{remote_id}"


def _browser_link(repository: str, action: str, issue_number: int) -> str:
    return f"https://github.com/{repository}/issues/{issue_number}"


def _operation_id(owner_principal_id: str, idempotency_key: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"seraph:github-followthrough:{owner_principal_id}:{idempotency_key}",
    )


def _marker(operation_id: uuid.UUID) -> str:
    return f"{MARKER_PREFIX}{operation_id} -->"


def _final_body(body: Any, operation_id: uuid.UUID) -> str:
    text = str(body or "")
    if not text.strip():
        raise GitHubFollowthroughError("body_required", status_code=422)
    if MARKER_PREFIX in text:
        raise GitHubFollowthroughError("reserved_marker_spoof", status_code=422)
    marker = _marker(operation_id)
    result = f"{text.rstrip()}\n\n{marker}\n"
    if len(result.encode("utf-8")) > MAX_BODY_BYTES:
        raise GitHubFollowthroughError("body_too_large", status_code=422)
    return result


def _uuid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise GitHubFollowthroughError("idempotency_key_invalid", status_code=422) from exc


def _safe_exception_code(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "transport_timeout"
    if isinstance(exc, PinnedTransportError):
        return "transport_blocked"
    return "transport_error"


def _operator(request: Request):
    from src.api.capabilities import _require_authenticated_capability_operator

    return _require_authenticated_capability_operator(request)


def _principal_id(operator: Any) -> str:
    value = _text(getattr(getattr(operator, "principal", None), "principal_id", None))
    if not value:
        raise GitHubFollowthroughError("authentication_required", status_code=401)
    return value


def _session_id(operator: Any) -> str:
    value = _text(getattr(operator, "session_id", None))
    if not value:
        raise GitHubFollowthroughError("authentication_required", status_code=401)
    return value


async def _require_job_session(job_id: str, operator: Any) -> None:
    current = await durable_job_repository.get_job(job_id)
    if current is None:
        raise GitHubFollowthroughError("job_not_found", status_code=404)
    authority = current.get("declared_authority") if isinstance(current.get("declared_authority"), Mapping) else {}
    if str(authority.get("session_id") or "") != _session_id(operator):
        raise GitHubFollowthroughError("job_session_mismatch", status_code=403)


def _has_grant(operator: Any, grant: AuthorityGrant) -> bool:
    grants = {
        str(getattr(item, "value", item))
        for item in getattr(getattr(operator, "principal", None), "grants", ())
    }
    return grant.value in grants


async def _require_live_owner_session(
    *,
    owner_principal_id: str,
    owner_session_id: str,
    external_mutation_granted: bool,
) -> None:
    """Enforce the dispatch boundary inside the adapter service.

    HTTP routes perform the same checks for operator ergonomics, but routine
    and recovery callers reach this service directly.  A missing explicit
    grant therefore fails closed even when a durable job carries old
    permissions, and the persisted owner session must still be live.
    """

    if not external_mutation_granted:
        raise GitHubFollowthroughError("external_mutation_grant_required", status_code=403)
    if not _text(owner_session_id):
        raise GitHubFollowthroughError("owner_session_required", status_code=403)
    try:
        operator = await authenticate_session(owner_session_id, touch=False)
    except AuthFailure as exc:
        raise GitHubFollowthroughError("owner_session_invalid", status_code=403) from exc
    principal_id = _text(getattr(getattr(operator, "principal", None), "principal_id", None))
    if _text(getattr(operator, "session_id", None)) != _text(owner_session_id) or principal_id != _text(owner_principal_id):
        raise GitHubFollowthroughError("owner_session_mismatch", status_code=403)
    # The caller's boolean is only an admission hint.  Re-read the current
    # authenticated principal at the adapter boundary so a grant revoked
    # after preview/approval cannot still authorize an external POST.
    if not _has_grant(operator, AuthorityGrant.EXTERNAL_MUTATION):
        raise GitHubFollowthroughError("external_mutation_grant_required", status_code=403)


def _connection_payload(row: GitHubFollowthroughConnection | None) -> dict[str, Any]:
    if row is None:
        return {
            "id": None,
            "repository": None,
            "revision": 0,
            "mode": CONNECTION_MODE_DISABLED,
            "credential_configured": False,
            "active_job_id": None,
            "active_fence": None,
        }
    return {
        "id": row.id,
        "repository": row.repository,
        "revision": int(row.revision or 0),
        "mode": row.mode,
        "credential_configured": bool(row.vault_key),
        "active_job_id": row.active_job_id,
        "active_fence": row.active_fence,
    }


@dataclass(frozen=True)
class PreparedPublication:
    operation_id: uuid.UUID
    job_id: str
    owner_principal_id: str
    owner_session_id: str
    conversation_id: str
    goal_id: str
    goal_revision: int
    source_watch_id: str
    plan_revision: int
    dossier_artifact_id: str
    dossier_sha256: str
    connection_id: str
    connection_revision: int
    repository: str
    action: str
    issue_number: int | None
    title: str | None
    body: str
    body_sha256: str
    operation_marker: str
    payload_path: str
    payload_sha256: str

    @property
    def client_idempotency_key(self) -> str:
        return str(self.operation_id)

    def as_private_payload(self) -> dict[str, Any]:
        return {
            "schema": "seraph.github-followthrough.input.v1",
            "operation_id": str(self.operation_id),
            "job_id": self.job_id,
            "owner_principal_id": self.owner_principal_id,
            "owner_session_id": self.owner_session_id,
            "conversation_id": self.conversation_id,
            "goal_id": self.goal_id,
            "goal_revision": self.goal_revision,
            "source_watch_id": self.source_watch_id,
            "plan_revision": self.plan_revision,
            "dossier_artifact_id": self.dossier_artifact_id,
            "dossier_sha256": self.dossier_sha256,
            "connection_id": self.connection_id,
            "connection_revision": self.connection_revision,
            "repository": self.repository,
            "action": self.action,
            "issue_number": self.issue_number,
            "title": self.title,
            "body": self.body,
            "body_sha256": self.body_sha256,
            "operation_marker": self.operation_marker,
        }


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=3, max_length=200)
    vault_key: str = Field(min_length=1, max_length=160)
    mode: str = CONNECTION_MODE_DISABLED
    expected_revision: int = Field(ge=0)


class PrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str = Field(min_length=1, max_length=200)
    goal_id: str = Field(min_length=1, max_length=200)
    goal_revision: int = Field(gt=0)
    dossier_artifact_id: str = Field(min_length=1, max_length=200)
    dossier_sha256: str = Field(min_length=64, max_length=64)
    connection_revision: int = Field(gt=0)
    action: str
    title: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=MAX_BODY_BYTES)
    issue_number: int | None = Field(default=None, gt=0)
    idempotency_key: str = Field(min_length=1, max_length=80)


class ReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remote_id: int | None = Field(default=None, gt=0)


class GitHubFollowthroughService:
    """Durable publication service; only this class may speak GitHub."""

    def __init__(
        self,
        *,
        resolver: Callable[..., Any] | None = None,
        transport: Any | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._resolver = resolver
        self._transport = transport
        self._sleep = sleep

    async def _request(
        self,
        url: str,
        *,
        method: str,
        token: str | None = None,
        json_body: dict[str, Any] | None = None,
        timeout_seconds: float = 20.0,
    ) -> PinnedResponse:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        kwargs: dict[str, Any] = {
            "method": method,
            "headers": headers,
            "json_body": json_body,
            "timeout_seconds": timeout_seconds,
            "connect_timeout_seconds": min(5.0, float(timeout_seconds)),
            "max_bytes": MAX_RESPONSE_BYTES,
        }
        if self._resolver is not None:
            kwargs["resolver"] = self._resolver
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return await request_pinned_https(f"{GITHUB_ORIGIN}{url}", **kwargs)

    async def get_connection(self, owner_principal_id: str) -> dict[str, Any]:
        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(GitHubFollowthroughConnection).where(
                        GitHubFollowthroughConnection.owner_principal_id == owner_principal_id
                    )
                )
            ).scalars().first()
            if row is not None:
                db.expunge(row)
        return _connection_payload(row)

    async def put_connection(
        self,
        *,
        owner_principal_id: str,
        repository: str,
        vault_key: str,
        mode: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        repository = _repository(repository)
        vault_key = _vault_key(vault_key)
        if mode not in CONNECTION_MODES:
            raise GitHubFollowthroughError("connection_mode_invalid", status_code=422)
        if mode == CONNECTION_MODE_ACTIVE and not _text(owner_principal_id):
            raise GitHubFollowthroughError("owner_required", status_code=401)
        if mode != CONNECTION_MODE_DISABLED and not await vault_repository.exists(vault_key):
            raise GitHubFollowthroughError("credential_not_configured", status_code=409)
        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(GitHubFollowthroughConnection).where(
                        GitHubFollowthroughConnection.owner_principal_id == owner_principal_id
                    )
                )
            ).scalars().first()
            if row is None:
                if expected_revision != 0:
                    raise GitHubFollowthroughError("connection_revision_stale")
                row = GitHubFollowthroughConnection(
                    owner_principal_id=owner_principal_id,
                    repository=repository,
                    vault_key=vault_key,
                    mode=mode,
                    revision=1,
                )
                db.add(row)
                await db.flush()
            else:
                if int(row.revision or 0) != int(expected_revision):
                    raise GitHubFollowthroughError("connection_revision_stale")
                # A live publication owns the complete connection binding
                # until its dispatch fence is released.  Allowing a mode,
                # repository, or vault-key update while that fence is held
                # would let the final handoff validate one revision and send
                # bytes under another.
                if row.active_job_id:
                    raise GitHubFollowthroughError("connection_reserved", status_code=409)
                # The read above is only for a useful error classification. A
                # reservation can commit after that read and before this
                # mutation, so the write itself must carry the reservation
                # fence. Never overwrite an active job on a stale snapshot.
                updated = await db.execute(
                    update(GitHubFollowthroughConnection)
                    .where(
                        GitHubFollowthroughConnection.id == row.id,
                        GitHubFollowthroughConnection.owner_principal_id == owner_principal_id,
                        GitHubFollowthroughConnection.revision == int(expected_revision),
                        GitHubFollowthroughConnection.active_job_id.is_(None),
                    )
                    .values(
                        repository=repository,
                        vault_key=vault_key,
                        mode=mode,
                        revision=GitHubFollowthroughConnection.revision + 1,
                        updated_at=_now(),
                    )
                )
                if updated.rowcount != 1:
                    latest = (
                        await db.execute(
                            select(GitHubFollowthroughConnection).where(
                                GitHubFollowthroughConnection.id == row.id,
                                GitHubFollowthroughConnection.owner_principal_id == owner_principal_id,
                            )
                        )
                    ).scalars().first()
                    if latest is not None and latest.active_job_id:
                        raise GitHubFollowthroughError("connection_reserved", status_code=409)
                    raise GitHubFollowthroughError("connection_revision_stale")
                row = (
                    await db.execute(
                        select(GitHubFollowthroughConnection).where(
                            GitHubFollowthroughConnection.id == row.id,
                            GitHubFollowthroughConnection.owner_principal_id == owner_principal_id,
                        )
                    )
                ).scalars().first()
                if row is None:
                    raise GitHubFollowthroughError("connection_missing", status_code=404)
                await db.flush()
            db.expunge(row)
        return _connection_payload(row)

    async def _get_connection_row(self, owner_principal_id: str) -> GitHubFollowthroughConnection | None:
        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(GitHubFollowthroughConnection).where(
                        GitHubFollowthroughConnection.owner_principal_id == owner_principal_id
                    )
                )
            ).scalars().first()
            if row is not None:
                db.expunge(row)
            return row

    async def _load_dossier(
        self,
        *,
        owner_principal_id: str,
        request: PrepareRequest,
    ) -> tuple[GuardianDecisionPacket, GuardianSourceWatch, Goal, str]:
        async with db_engine.get_session() as db:
            packet = (
                await db.execute(
                    select(GuardianDecisionPacket).where(
                        GuardianDecisionPacket.dossier_artifact_id == request.dossier_artifact_id,
                        GuardianDecisionPacket.dossier_sha256 == request.dossier_sha256,
                        GuardianDecisionPacket.goal_id == request.goal_id,
                        GuardianDecisionPacket.goal_revision == request.goal_revision,
                        GuardianDecisionPacket.status == "succeeded",
                        GuardianDecisionPacket.verification_status == "passed",
                    )
                )
            ).scalars().first()
            if packet is None:
                raise GitHubFollowthroughError("dossier_not_verified", status_code=404)
            watch = (
                await db.execute(
                    select(GuardianSourceWatch).where(
                        GuardianSourceWatch.id == packet.source_watch_id,
                        GuardianSourceWatch.owner_principal_id == owner_principal_id,
                    )
                )
            ).scalars().first()
            goal = (
                await db.execute(select(Goal).where(Goal.id == packet.goal_id))
            ).scalars().first()
            if watch is None or goal is None:
                raise GitHubFollowthroughError("dossier_owner_mismatch", status_code=404)
            if _text(goal.owner_principal_id) != owner_principal_id:
                raise GitHubFollowthroughError("dossier_owner_mismatch", status_code=404)
            if _text(watch.owner_session_id) != request.conversation_id:
                raise GitHubFollowthroughError("conversation_owner_mismatch", status_code=403)
            if int(goal.revision or 0) != request.goal_revision:
                raise GitHubFollowthroughError("goal_revision_stale")
            if _text(goal.owner_session_id) != request.conversation_id:
                raise GitHubFollowthroughError("conversation_owner_mismatch", status_code=403)
            if watch.state != "active":
                raise GitHubFollowthroughError("source_grant_inactive", status_code=403)
            if int(packet.plan_revision or 0) != int(watch.plan_revision or 0):
                raise GitHubFollowthroughError("source_plan_revision_stale")
            admitted, admission_reason, budget = _goal_admission(goal)
            if not admitted or budget is None:
                status_code = 403 if admission_reason in {
                    "goal_owner_binding_missing",
                    "goal_budget_missing_reviewed_grant",
                    "goal_budget_timezone_invalid",
                } else 409
                raise GitHubFollowthroughError(
                    f"goal_admission_{admission_reason}",
                    status_code=status_code,
                )
            write_authority = _load(watch.write_authority_json, {})
            if not isinstance(write_authority, Mapping):
                raise GitHubFollowthroughError("source_write_authority_invalid", status_code=403)
            if watch.write_mode == "standing_reviewed" and _text(write_authority.get("grant_id")) != _text(budget.grant_id):
                raise GitHubFollowthroughError("source_grant_stale", status_code=403)
            dossier_path = _text(packet.dossier_path)
            db.expunge(packet)
            db.expunge(watch)
            db.expunge(goal)
        if not dossier_path:
            raise GitHubFollowthroughError("dossier_artifact_missing", status_code=409)
        try:
            resolved = _safe_resolve(dossier_path)
            if (
                not resolved.is_file()
                or resolved.is_symlink()
                or resolved.stat().st_nlink != 1
            ):
                raise GitHubFollowthroughError("dossier_artifact_missing", status_code=409)
            raw, truncated = _read_workspace_text_bounded(resolved, max_bytes=MAX_ARTIFACT_BYTES)
        except GitHubFollowthroughError:
            raise
        except (OSError, ValueError) as exc:
            raise GitHubFollowthroughError("dossier_artifact_unreadable", status_code=409) from exc
        if truncated or _sha(raw) != request.dossier_sha256:
            raise GitHubFollowthroughError("dossier_artifact_digest_mismatch", status_code=409)
        return packet, watch, goal, raw

    async def _prepare_payload_file(self, prepared: PreparedPublication) -> str:
        owner_digest = _sha(prepared.owner_principal_id)[:24]
        path = f"github/followthrough/{owner_digest}/{prepared.job_id}.json"
        payload = _dump(prepared.as_private_payload()) + "\n"
        try:
            _write_workspace_text_bounded(_safe_resolve(path), payload, max_bytes=MAX_ARTIFACT_BYTES)
            stored, truncated = _read_workspace_text_bounded(
                _safe_resolve(path), max_bytes=MAX_ARTIFACT_BYTES
            )
        except (OSError, ValueError) as exc:
            raise GitHubFollowthroughError("input_artifact_write_failed", status_code=500) from exc
        if truncated or stored != payload:
            raise GitHubFollowthroughError("input_artifact_readback_failed", status_code=500)
        return path

    async def _checkpoint_payload(self, job_id: str, *, phase: str, payload: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
        lease = current.get("lease") or {}
        return await durable_job_repository.record_checkpoint(
            job_id,
            checkpoint_id=f"github-followthrough:{phase}",
            state={"phase": phase, **dict(payload)},
            checkpoint_payload={"phase": phase, **dict(payload)},
            owner=str(lease.get("owner") or ""),
            fencing_token=int(lease.get("fencing_token") or 0),
            expected_revision=int(current.get("revision") or 0),
        )

    async def _acquire_dispatch_guard(
        self,
        prepared: PreparedPublication,
        *,
        current: Mapping[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        """Fence the single POST with the canonical leased checkpoint CAS."""
        if self._checkpoint(current, "github-followthrough:dispatch_guard") is not None:
            return False, dict(current)
        try:
            guarded = await self._checkpoint_payload(
                prepared.job_id,
                phase="dispatch_guard",
                payload={
                    "operation_id": str(prepared.operation_id),
                    "body_sha256": prepared.body_sha256,
                    "target_repository": prepared.repository,
                    "action": prepared.action,
                },
                current=current,
            )
            return True, guarded
        except DurableJobError:
            latest = await durable_job_repository.get_job(prepared.job_id) or dict(current)
            if self._checkpoint(latest, "github-followthrough:dispatch_guard") is not None:
                return False, latest
            raise

    @staticmethod
    def _checkpoint(current: Mapping[str, Any], checkpoint_id: str) -> dict[str, Any] | None:
        for item in reversed(current.get("checkpoints") or []):
            if isinstance(item, Mapping) and item.get("checkpoint_id") == checkpoint_id:
                payload = item.get("payload")
                return dict(payload) if isinstance(payload, Mapping) else dict(item)
        return None

    async def _read_prepared(self, current: Mapping[str, Any]) -> PreparedPublication:
        checkpoint = self._checkpoint(current, "github-followthrough:prepared")
        if not checkpoint:
            raise GitHubFollowthroughError("prepared_checkpoint_missing")
        path = _text(checkpoint.get("payload_path"))
        expected_sha = _text(checkpoint.get("payload_sha256"))
        if not path or not expected_sha:
            raise GitHubFollowthroughError("prepared_checkpoint_incomplete")
        try:
            raw, truncated = _read_workspace_text_bounded(
                _safe_resolve(path), max_bytes=MAX_ARTIFACT_BYTES
            )
        except (OSError, ValueError) as exc:
            raise GitHubFollowthroughError("prepared_input_missing") from exc
        if truncated or _sha(raw) != expected_sha:
            raise GitHubFollowthroughError("prepared_input_digest_mismatch")
        value = _load(raw, None)
        if not isinstance(value, Mapping):
            raise GitHubFollowthroughError("prepared_input_malformed")
        try:
            operation_id = _uuid(value.get("operation_id"))
            payload_job_id = _text(value.get("job_id"))
            owner_principal_id = _text(value.get("owner_principal_id"))
            owner_session_id = _text(value.get("owner_session_id"))
            conversation_id = _text(value.get("conversation_id"))
            goal_id = _text(value.get("goal_id"))
            goal_revision = int(value["goal_revision"])
            source_watch_id = _text(value.get("source_watch_id"))
            plan_revision = int(value["plan_revision"])
            dossier_artifact_id = _text(value.get("dossier_artifact_id"))
            dossier_sha256 = _text(value.get("dossier_sha256"))
            connection_id = _text(value.get("connection_id"))
            connection_revision = int(value["connection_revision"])
            repository = _repository(value.get("repository"))
            action = _text(value.get("action"))
            issue_number = value.get("issue_number")
            issue_number = _positive_id(issue_number, field="issue_number") if issue_number is not None else None
            title = value.get("title")
            title = str(title) if title is not None else None
            body = str(value["body"])
            body_sha256 = _text(value.get("body_sha256"))
            operation_marker = _text(value.get("operation_marker"))
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubFollowthroughError("prepared_input_malformed") from exc
        current_job_id = _text(current.get("job_id"))
        if (
            not current_job_id
            or payload_job_id != current_job_id
            or current_job_id != f"ghfollow_{operation_id.hex}"
        ):
            raise GitHubFollowthroughError("prepared_job_identity_mismatch")
        if (
            not owner_principal_id
            or not owner_session_id
            or not conversation_id
            or not goal_id
            or goal_revision < 1
            or plan_revision < 1
            or not dossier_artifact_id
            or len(dossier_sha256) != 64
            or not connection_id
            or connection_revision < 1
            or action not in ACTIONS
            or not body_sha256
            or _sha(body) != body_sha256
            or operation_marker != _marker(operation_id)
            or operation_marker not in body
        ):
            raise GitHubFollowthroughError("prepared_input_binding_invalid")
        if action == ACTION_CREATE_ISSUE and (not title or issue_number is not None):
            raise GitHubFollowthroughError("prepared_input_binding_invalid")
        if action == ACTION_CREATE_COMMENT and (title is not None or issue_number is None):
            raise GitHubFollowthroughError("prepared_input_binding_invalid")
        return PreparedPublication(
            operation_id=operation_id,
            job_id=_text(current.get("job_id")),
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            conversation_id=conversation_id,
            goal_id=goal_id,
            goal_revision=goal_revision,
            source_watch_id=source_watch_id,
            plan_revision=plan_revision,
            dossier_artifact_id=dossier_artifact_id,
            dossier_sha256=dossier_sha256,
            connection_id=connection_id,
            connection_revision=connection_revision,
            repository=repository,
            action=action,
            issue_number=issue_number,
            title=title,
            body=body,
            body_sha256=body_sha256,
            operation_marker=operation_marker,
            payload_path=path,
            payload_sha256=expected_sha,
        )

    async def _reserve_connection(
        self,
        *,
        owner_principal_id: str,
        connection_id: str,
        expected_revision: int,
        job_id: str,
    ) -> int:
        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(GitHubFollowthroughConnection).where(
                        GitHubFollowthroughConnection.id == connection_id,
                        GitHubFollowthroughConnection.owner_principal_id == owner_principal_id,
                    )
                )
            ).scalars().first()
            if row is None:
                raise GitHubFollowthroughError("connection_not_found", status_code=404)
            if int(row.revision or 0) != int(expected_revision):
                raise GitHubFollowthroughError("connection_revision_stale")
            if row.mode != CONNECTION_MODE_ACTIVE:
                raise GitHubFollowthroughError("connection_not_active", status_code=403)
            if row.active_job_id and row.active_job_id != job_id:
                raise GitHubFollowthroughError("connection_busy")
            if row.active_job_id == job_id and row.active_fence:
                return int(row.active_fence)
            now = _now()
            result = await db.execute(
                update(GitHubFollowthroughConnection)
                .where(
                    GitHubFollowthroughConnection.id == connection_id,
                    GitHubFollowthroughConnection.owner_principal_id == owner_principal_id,
                    GitHubFollowthroughConnection.revision == expected_revision,
                    GitHubFollowthroughConnection.mode == CONNECTION_MODE_ACTIVE,
                    GitHubFollowthroughConnection.active_job_id.is_(None),
                )
                .values(
                    active_job_id=job_id,
                    active_fence=func.coalesce(GitHubFollowthroughConnection.active_fence, 0) + 1,
                    updated_at=now,
                )
            )
            if getattr(result, "rowcount", None) != 1:
                raise GitHubFollowthroughError("connection_busy")
            refreshed = (
                await db.execute(
                    select(GitHubFollowthroughConnection).where(
                        GitHubFollowthroughConnection.id == connection_id
                    )
                )
            ).scalars().first()
            if refreshed is None or not refreshed.active_fence:
                raise GitHubFollowthroughError("connection_reservation_missing")
            return int(refreshed.active_fence)

    async def _release_connection(
        self,
        *,
        connection_id: str,
        owner_principal_id: str,
        job_id: str,
        fence: int,
    ) -> bool:
        async with db_engine.get_session() as db:
            result = await db.execute(
                update(GitHubFollowthroughConnection)
                .where(
                    GitHubFollowthroughConnection.id == connection_id,
                    GitHubFollowthroughConnection.owner_principal_id == owner_principal_id,
                    GitHubFollowthroughConnection.active_job_id == job_id,
                    GitHubFollowthroughConnection.active_fence == fence,
                )
                .values(active_job_id=None, updated_at=_now())
            )
            return getattr(result, "rowcount", None) == 1

    async def _assert_dispatch_binding(
        self,
        *,
        owner_principal_id: str,
        connection_id: str,
        expected_revision: int,
        repository: str,
        vault_key: str,
        mode: str,
        job_id: str,
        fence: int,
    ) -> None:
        """Atomically fence the final connection-to-dispatch handoff.

        The live connection read and this compare-and-swap deliberately occur
        after the durable dispatch guard and immediately before resolving the
        vault reference.  The active reservation prevents a concurrent
        connection update from succeeding between this CAS and the POST.
        """

        async with db_engine.get_session() as db:
            result = await db.execute(
                update(GitHubFollowthroughConnection)
                .where(
                    GitHubFollowthroughConnection.id == connection_id,
                    GitHubFollowthroughConnection.owner_principal_id == owner_principal_id,
                    GitHubFollowthroughConnection.revision == int(expected_revision),
                    GitHubFollowthroughConnection.repository == repository,
                    GitHubFollowthroughConnection.vault_key == vault_key,
                    GitHubFollowthroughConnection.mode == mode,
                    GitHubFollowthroughConnection.active_job_id == job_id,
                    GitHubFollowthroughConnection.active_fence == int(fence),
                )
                .values(updated_at=_now())
            )
            if getattr(result, "rowcount", None) != 1:
                raise GitHubFollowthroughError("connection_dispatch_binding_stale", status_code=409)

    async def _repair_terminal_connection_reservation(self, current: Mapping[str, Any]) -> str:
        """Release a connection fence left by a crash after job finalization.

        The durable job transition and connection release are separate CAS
        writes.  A successful job is therefore allowed to be observed with a
        stale ``active_job_id``; every successful read/reconcile path retries
        the exact job/fence release before projecting the receipt.
        """

        if current.get("status") != "succeeded":
            return "not_required"
        authority = current.get("declared_authority") if isinstance(current.get("declared_authority"), Mapping) else {}
        connection_id = _text(authority.get("connection_id"))
        if not connection_id:
            try:
                prepared = await self._read_prepared(current)
            except GitHubFollowthroughError:
                prepared = None
            connection_id = _text(getattr(prepared, "connection_id", None))
        if not connection_id:
            return "not_required"
        owner = current.get("owner") if isinstance(current.get("owner"), Mapping) else {}
        owner_principal_id = _text(
            owner.get("principal_id")
            or authority.get("principal")
            or authority.get("owner_principal_id")
        )
        job_id = _text(current.get("job_id"))
        if not owner_principal_id or not job_id:
            return "pending"
        connection = await self._get_connection_row(owner_principal_id)
        if connection is None or connection.active_job_id != job_id:
            return "released"
        fence = int(connection.active_fence or 0)
        if fence <= 0:
            return "pending"
        released = await self._release_connection(
            connection_id=connection_id,
            owner_principal_id=owner_principal_id,
            job_id=job_id,
            fence=fence,
        )
        if released:
            return "released"
        latest = await self._get_connection_row(owner_principal_id)
        if latest is None or latest.active_job_id != job_id:
            return "released"
        return "pending"

    async def _prepare_job_response(
        self,
        current: Mapping[str, Any],
        *,
        prepared: PreparedPublication | None = None,
        approval: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        connection_release_status = "not_required"
        if current.get("status") == "succeeded":
            try:
                connection_release_status = await self._repair_terminal_connection_reservation(current)
            except Exception:
                connection_release_status = "pending"
        if prepared is None:
            try:
                prepared = await self._read_prepared(current)
            except GitHubFollowthroughError:
                prepared = None
        response: dict[str, Any] = {
            "job_id": current.get("job_id"),
            "operation_id": str(prepared.operation_id) if prepared else None,
            "status": current.get("status"),
            "goal_id": current.get("goal_id"),
            "goal_revision": current.get("goal_revision"),
            "plan_revision": current.get("plan_revision"),
            "approval_id": _text((current.get("declared_authority") or {}).get("approval_id")) or None,
            "approval_expires_at": None,
            "remote_id": None,
            "remote_url": None,
            "recovery_reason": current.get("failure_reason"),
            "artifacts": list(current.get("artifacts") or []),
            "effects": [
                {
                    "effect_id": item.get("effect_id"),
                    "effect_type": item.get("effect_type"),
                    "status": item.get("status"),
                    "target_path": item.get("target_path"),
                    "details": item.get("details"),
                }
                for item in current.get("effects") or []
                if isinstance(item, Mapping)
            ],
        }
        if connection_release_status == "pending":
            response["connection_release"] = {
                "status": "pending",
                "operator_action": "retry_reconcile",
            }
        if prepared is not None:
            response["preview"] = {
                "repository": prepared.repository,
                "action": prepared.action,
                "issue_number": prepared.issue_number,
                "title": prepared.title,
                "body": prepared.body,
                "body_sha256": prepared.body_sha256,
                "marker": prepared.operation_marker,
                "dossier_artifact_id": prepared.dossier_artifact_id,
                "dossier_sha256": prepared.dossier_sha256,
                "source_watch_id": prepared.source_watch_id,
                "connection_revision": prepared.connection_revision,
            }
        if approval:
            response["approval_id"] = approval.get("id") or approval.get("approval_id")
            expires = approval.get("expires_at")
            response["approval_expires_at"] = expires.isoformat() if isinstance(expires, datetime) else expires
        for item in reversed(current.get("effects") or []):
            if not isinstance(item, Mapping):
                continue
            details = item.get("details")
            if not isinstance(details, Mapping):
                continue
            remote_id = details.get("remote_id")
            if isinstance(remote_id, int) and remote_id > 0:
                response["remote_id"] = remote_id
                response["remote_url"] = details.get("browser_url")
                break
        return response

    async def prepare(
        self,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        external_mutation_granted: bool = False,
        request: PrepareRequest,
    ) -> dict[str, Any]:
        await _require_live_owner_session(
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            external_mutation_granted=external_mutation_granted,
        )
        if request.conversation_id != owner_session_id:
            raise GitHubFollowthroughError("conversation_owner_mismatch", status_code=403)
        if request.action not in ACTIONS:
            raise GitHubFollowthroughError("action_invalid", status_code=422)
        client_key = _uuid(request.idempotency_key)
        operation_id = _operation_id(owner_principal_id, client_key)
        body = _final_body(request.body, operation_id)
        title: str | None = None
        issue_number: int | None = None
        if request.action == ACTION_CREATE_ISSUE:
            title = _text(request.title)
            if not title or len(title) > 200:
                raise GitHubFollowthroughError("title_invalid", status_code=422)
            if request.issue_number is not None:
                raise GitHubFollowthroughError("issue_number_forbidden", status_code=422)
        else:
            if request.title is not None:
                raise GitHubFollowthroughError("title_forbidden", status_code=422)
            issue_number = _positive_id(request.issue_number, field="issue_number")

        connection = await self._get_connection_row(owner_principal_id)
        if connection is None:
            raise GitHubFollowthroughError("connection_missing", status_code=409)
        if int(connection.revision or 0) != int(request.connection_revision):
            raise GitHubFollowthroughError("connection_revision_stale")
        if connection.mode != CONNECTION_MODE_ACTIVE:
            raise GitHubFollowthroughError("connection_not_active", status_code=403)
        if not connection.vault_key or not await vault_repository.exists(connection.vault_key):
            raise GitHubFollowthroughError("credential_not_configured", status_code=409)
        packet, watch, goal, _dossier_text = await self._load_dossier(
            owner_principal_id=owner_principal_id,
            request=request,
        )
        if int(packet.plan_revision or 0) != int(watch.plan_revision):
            raise GitHubFollowthroughError("source_plan_revision_stale")
        input_fields = {
            "operation_id": str(operation_id),
            "repository": connection.repository,
            "connection_id": connection.id,
            "connection_revision": int(connection.revision),
            "action": request.action,
            "issue_number": issue_number,
            "title_sha256": _sha(title or ""),
            "body_sha256": _sha(body),
            "dossier_artifact_id": request.dossier_artifact_id,
            "dossier_sha256": request.dossier_sha256,
            "source_watch_id": watch.id,
            "goal_id": goal.id,
            "goal_revision": int(goal.revision),
            "plan_revision": int(watch.plan_revision),
        }
        input_digest = _sha(_dump(input_fields))
        job_id = f"ghfollow_{operation_id.hex}"
        existing = await durable_job_repository.get_job(job_id)
        if existing is not None:
            if existing.get("input_digest") != input_digest:
                raise GitHubFollowthroughError("idempotency_conflict")
            existing_authority = (
                existing.get("declared_authority")
                if isinstance(existing.get("declared_authority"), Mapping)
                else {}
            )
            existing_owner = existing.get("owner") if isinstance(existing.get("owner"), Mapping) else {}
            persisted_principal = _text(
                existing_owner.get("principal_id")
                or existing_authority.get("principal")
                or existing_authority.get("owner_principal_id")
            )
            persisted_session = _text(
                existing_authority.get("session_id")
                or existing.get("operator_session_id")
                or existing.get("session_id")
            )
            if persisted_principal != _text(owner_principal_id):
                raise GitHubFollowthroughError("job_owner_mismatch", status_code=403)
            if persisted_session != _text(owner_session_id):
                raise GitHubFollowthroughError("job_session_mismatch", status_code=403)
            return await self._prepare_job_response(existing)
        authority = {
            "principal": owner_principal_id,
            "owner_kind": "user",
            "session_id": owner_session_id,
            "capability_id": CAPABILITY_ID,
            "permissions": ["github_issue_create_or_comment"],
            "connection_id": connection.id,
            "connection_revision": int(connection.revision),
            "source_watch_id": watch.id,
            "dossier_artifact_id": request.dossier_artifact_id,
            "dossier_sha256": request.dossier_sha256,
            "budget_microusd": 0,
        }
        admitted = await durable_job_repository.admit_job(
            DurableJobSpec(
                identity=DurableJobIdentity(
                    job_id=job_id,
                    owner_kind="user",
                    owner_principal_id=owner_principal_id,
                    job_kind=JOB_KIND,
                    capability_version=CAPABILITY_VERSION,
                    idempotency_scope="github-followthrough",
                    idempotency_key=str(client_key),
                ),
                inputs=input_fields,
                session_id=owner_session_id,
                conversation_id=request.conversation_id,
                operator_session_id=owner_session_id,
                goal_id=goal.id,
                goal_revision=int(goal.revision),
                plan_revision=int(watch.plan_revision),
                declared_authority=authority,
                deadline_at=_now() + timedelta(seconds=PREPARE_DEADLINE_SECONDS),
                max_attempts=1,
                priority=50,
                run_fingerprint=input_digest,
                budget_microusd=0,
            )
        )
        if admitted.get("receipt", {}).get("status") == "deduped" or admitted.get("status") != "accepted":
            return await self._prepare_job_response(admitted)
        queued = await durable_job_repository.queue_job(job_id, expected_revision=admitted.get("revision"))
        runner = RUNNER_PREFIX + job_id
        current = await durable_job_repository.claim_job(
            job_id,
            owner=runner,
            expected_revision=queued.get("revision"),
            expected_fencing_token=(queued.get("lease") or {}).get("fencing_token"),
            lease_seconds=PREPARE_DEADLINE_SECONDS,
        )
        if current.get("status") != "running":
            raise GitHubFollowthroughError("durable_job_not_running")
        prepared = PreparedPublication(
            operation_id=operation_id,
            job_id=job_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            conversation_id=request.conversation_id,
            goal_id=goal.id,
            goal_revision=int(goal.revision),
            source_watch_id=watch.id,
            plan_revision=int(watch.plan_revision),
            dossier_artifact_id=request.dossier_artifact_id,
            dossier_sha256=request.dossier_sha256,
            connection_id=connection.id,
            connection_revision=int(connection.revision),
            repository=connection.repository,
            action=request.action,
            issue_number=issue_number,
            title=title,
            body=body,
            body_sha256=_sha(body),
            operation_marker=_marker(operation_id),
            payload_path="",
            payload_sha256="",
        )
        owner = str((current.get("lease") or {}).get("owner") or runner)
        fence = int((current.get("lease") or {}).get("fencing_token") or 0)
        path = f"github/followthrough/{_sha(owner_principal_id)[:24]}/{job_id}.json"
        payload_text = _dump(prepared.as_private_payload()) + "\n"
        try:
            _write_workspace_text_bounded(_safe_resolve(path), payload_text, max_bytes=MAX_ARTIFACT_BYTES)
            stored, truncated = _read_workspace_text_bounded(_safe_resolve(path), max_bytes=MAX_ARTIFACT_BYTES)
        except (OSError, ValueError) as exc:
            await durable_job_repository.transition_job(
                job_id,
                "failed",
                owner=owner,
                fencing_token=fence,
                expected_revision=current.get("revision"),
                reason="input_artifact_write_failed",
            )
            raise GitHubFollowthroughError("input_artifact_write_failed", status_code=500) from exc
        if truncated or stored != payload_text:
            raise GitHubFollowthroughError("input_artifact_readback_failed", status_code=500)
        prepared = replace_dataclass(
            prepared,
            payload_path=path,
            payload_sha256=_sha(payload_text),
        )
        artifact = await durable_job_repository.record_artifact(
            job_id,
            file_path=path,
            artifact_type="github_followthrough_input",
            content=payload_text,
            owner=owner,
            fencing_token=fence,
            expected_revision=current.get("revision"),
        )
        current = await self._checkpoint_payload(
            job_id,
            phase="prepared",
            payload={
                "operation_id": str(operation_id),
                "payload_path": path,
                "payload_sha256": prepared.payload_sha256,
                "input_artifact_id": (artifact.get("receipt") or {}).get("artifact_id"),
                "input_digest": input_digest,
                "connection_id": connection.id,
                "connection_revision": int(connection.revision),
                "source_watch_id": watch.id,
                "plan_revision": int(watch.plan_revision),
                "dossier_artifact_id": request.dossier_artifact_id,
                "dossier_sha256": request.dossier_sha256,
            },
            current=await durable_job_repository.get_job(job_id) or current,
        )
        current = await durable_job_repository.get_job(job_id) or current
        approval_fingerprint = fingerprint_tool_call(
            "github:followthrough",
            {
                "operation_id": str(operation_id),
                "connection_id": connection.id,
                "connection_revision": int(connection.revision),
                "action": request.action,
                "repository": connection.repository,
                "issue_number": issue_number,
                "title_sha256": _sha(title or ""),
                "body_sha256": _sha(body),
                "dossier_artifact_id": request.dossier_artifact_id,
                "dossier_sha256": request.dossier_sha256,
            },
        )
        approval = await approval_repository.get_or_create_pending(
            session_id=owner_session_id,
            tool_name="github:followthrough",
            risk_level="high",
            summary=f"Publish an approved GitHub {request.action} for goal {goal.id}",
            fingerprint=approval_fingerprint,
            details={
                "approval_operator_principal_id": owner_principal_id,
                "approval_owner_principal_id": owner_principal_id,
                "approval_owner_operator_session_id": owner_session_id,
                "operator_session_id": owner_session_id,
                "approval_conversation_id": request.conversation_id,
                "approval_execution_session_id": owner_session_id,
                "durable_job_id": job_id,
                "durable_owner_kind": "user",
                "durable_owner_principal_id": owner_principal_id,
                "durable_authority_digest": current.get("authority_digest"),
                "durable_goal_id": goal.id,
                "durable_goal_revision": int(goal.revision),
                "durable_plan_revision": int(watch.plan_revision),
                "durable_capability_version": CAPABILITY_VERSION,
                "durable_budget_digest": current.get("budget_digest"),
                "operation_id": str(operation_id),
                "source_watch_id": watch.id,
                "connection_id": connection.id,
                "connection_revision": int(connection.revision),
                "repository": connection.repository,
                "action": request.action,
                "issue_number": issue_number,
                "title_sha256": _sha(title or ""),
                "body_sha256": _sha(body),
                "dossier_artifact_id": request.dossier_artifact_id,
                "dossier_sha256": request.dossier_sha256,
                "approval_expires_at": (_now() + timedelta(seconds=APPROVAL_TTL_SECONDS)).timestamp(),
            },
        )
        current = await durable_job_repository.get_job(job_id) or current
        lease = current.get("lease") or {}
        bound = await durable_job_repository.bind_approval_id(
            job_id,
            approval.id,
            owner=str(lease.get("owner") or owner),
            fencing_token=int(lease.get("fencing_token") or fence),
            expected_revision=int(current.get("revision") or 0),
        )
        await approval_repository.update_pending_details(
            approval.id,
            owner_principal_id=owner_principal_id,
            operator_session_id=owner_session_id,
            updates={
                "durable_authority_digest": bound.get("authority_digest"),
                "authority_digest": bound.get("authority_digest"),
                "approval_expires_at": approval.expires_at.timestamp() if approval.expires_at else None,
            },
        )
        bound_lease = bound.get("lease") or {}
        held = await durable_job_repository.transition_job(
            job_id,
            "awaiting_approval",
            owner=str(bound_lease.get("owner") or owner),
            fencing_token=int(bound_lease.get("fencing_token") or fence),
            expected_revision=int(bound.get("revision") or 0),
            reason="github_followthrough_approval_required",
        )
        return await self._prepare_job_response(held, prepared=prepared, approval={
            "id": approval.id,
            "expires_at": approval.expires_at,
        })

    async def _verify_live_handoff(
        self,
        prepared: PreparedPublication,
        *,
        owner_principal_id: str,
        connection: GitHubFollowthroughConnection,
    ) -> None:
        if prepared.owner_principal_id != owner_principal_id:
            raise GitHubFollowthroughError("job_owner_mismatch", status_code=404)
        if connection.id != prepared.connection_id:
            raise GitHubFollowthroughError("connection_binding_changed")
        if int(connection.revision or 0) != prepared.connection_revision:
            raise GitHubFollowthroughError("connection_revision_stale")
        if connection.repository != prepared.repository:
            raise GitHubFollowthroughError("repository_binding_changed")
        if connection.mode != CONNECTION_MODE_ACTIVE:
            raise GitHubFollowthroughError("connection_not_active", status_code=403)
        if not connection.vault_key or not await vault_repository.exists(connection.vault_key):
            raise GitHubFollowthroughError("credential_not_configured", status_code=409)
        request = PrepareRequest(
            conversation_id=prepared.conversation_id,
            goal_id=prepared.goal_id,
            goal_revision=prepared.goal_revision,
            dossier_artifact_id=prepared.dossier_artifact_id,
            dossier_sha256=prepared.dossier_sha256,
            connection_revision=prepared.connection_revision,
            action=ACTION_CREATE_ISSUE,
            title="handoff-validation",
            body="handoff-validation",
            idempotency_key=str(prepared.operation_id),
        )
        packet, watch, goal, _ = await self._load_dossier(
            owner_principal_id=owner_principal_id,
            request=request,
        )
        if packet.source_watch_id != prepared.source_watch_id:
            raise GitHubFollowthroughError("source_watch_binding_changed")
        if int(packet.plan_revision or 0) != prepared.plan_revision:
            raise GitHubFollowthroughError("source_plan_revision_stale")
        if int(watch.plan_revision or 0) != prepared.plan_revision:
            raise GitHubFollowthroughError("source_plan_revision_stale")
        if int(goal.revision or 0) != prepared.goal_revision:
            raise GitHubFollowthroughError("goal_revision_stale")

    async def _load_token(self, connection: GitHubFollowthroughConnection) -> str:
        try:
            token = await vault_repository.get(connection.vault_key)
        except Exception as exc:
            raise GitHubFollowthroughError("credential_resolution_failed", status_code=409) from exc
        if not isinstance(token, str) or not token.strip():
            raise GitHubFollowthroughError("credential_not_configured", status_code=409)
        return token.strip()

    @staticmethod
    def _response_json(response: PinnedResponse) -> dict[str, Any] | None:
        try:
            value = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError):
            return None
        return dict(value) if isinstance(value, Mapping) else None

    async def _record_intent(
        self,
        prepared: PreparedPublication,
        *,
        current: Mapping[str, Any],
        owner: str,
        fence: int,
    ) -> dict[str, Any]:
        target_path = (
            f"/repos/{prepared.repository}/issues"
            if prepared.action == ACTION_CREATE_ISSUE
            else f"/repos/{prepared.repository}/issues/{prepared.issue_number}/comments"
        )
        return await durable_job_repository.record_effect(
            prepared.job_id,
            effect_type="github_publication",
            effect_id=f"github:{prepared.operation_id}",
            target_path=target_path,
            target_digest=prepared.body_sha256,
            approval_id=_text((current.get("declared_authority") or {}).get("approval_id")) or None,
            adapter_idempotency_key=str(prepared.operation_id),
            status="intent",
            details={
                "operation_id": str(prepared.operation_id),
                "repository": prepared.repository,
                "action": prepared.action,
                "issue_number": prepared.issue_number,
                "title_sha256": _sha(prepared.title or ""),
                "body_sha256": prepared.body_sha256,
                "marker": prepared.operation_marker,
                "source_watch_id": prepared.source_watch_id,
                "dossier_artifact_id": prepared.dossier_artifact_id,
                "dossier_sha256": prepared.dossier_sha256,
                "goal_id": prepared.goal_id,
                "goal_revision": prepared.goal_revision,
                "plan_revision": prepared.plan_revision,
                "dispatch_confirmed": False,
            },
            owner=owner,
            fencing_token=fence,
            expected_revision=int(current.get("revision") or 0),
        )

    async def _mark_unknown(
        self,
        prepared: PreparedPublication,
        *,
        reason: str,
        current: Mapping[str, Any],
        owner: str,
        fence: int,
        readback_observation: bool = False,
        target_path: str | None = None,
    ) -> dict[str, Any]:
        revision = int(current.get("revision") or 0)
        effect_id = f"github:{prepared.operation_id}"
        if readback_observation:
            observed = await durable_job_repository.record_readback(
                prepared.job_id,
                target_path=target_path or "github:unknown",
                effect_id=effect_id,
                effect_type="github_publication",
                target_digest=prepared.body_sha256,
                status="unknown",
                details={"verified": False, "reason_code": reason},
                owner=owner,
                fencing_token=fence,
                expected_revision=revision,
            )
        else:
            observed = await durable_job_repository.record_effect(
                prepared.job_id,
                effect_type="github_publication",
                effect_id=effect_id,
                target_path=target_path or "github:unknown",
                target_digest=prepared.body_sha256,
                status="unknown",
                details={
                    "operation_id": str(prepared.operation_id),
                    "reason_code": reason,
                    "dispatch_confirmed": False,
                },
                owner=owner,
                fencing_token=fence,
                expected_revision=revision,
            )
        latest = await durable_job_repository.get_job(prepared.job_id) or observed
        lease = latest.get("lease") or {}
        try:
            return await durable_job_repository.transition_job(
                prepared.job_id,
                "unknown_external_effect",
                owner=str(lease.get("owner") or owner),
                fencing_token=int(lease.get("fencing_token") or fence),
                expected_revision=int(latest.get("revision") or 0),
                reason=reason,
            )
        except DurableJobTransitionError:
            return await durable_job_repository.get_job(prepared.job_id) or latest

    async def _mark_blocked(
        self,
        prepared: PreparedPublication,
        *,
        reason: str,
        current: Mapping[str, Any],
        owner: str,
        fence: int,
        target_path: str,
        response: PinnedResponse,
    ) -> dict[str, Any]:
        """Record a provider rejection as a verified no-publication outcome."""
        observed = await durable_job_repository.record_readback(
            prepared.job_id,
            target_path=target_path,
            effect_id=f"github:{prepared.operation_id}",
            effect_type="github_publication",
            target_digest=prepared.body_sha256,
            content_sha256=_sha(response.content),
            status="succeeded",
            details={
                "verified": True,
                "provider_rejected": True,
                "response_status": response.status_code,
                "reason_code": reason,
            },
            owner=owner,
            fencing_token=fence,
            expected_revision=int(current.get("revision") or 0),
        )
        latest = await durable_job_repository.get_job(prepared.job_id) or observed
        lease = latest.get("lease") or {}
        try:
            return await durable_job_repository.transition_job(
                prepared.job_id,
                "blocked",
                owner=str(lease.get("owner") or owner),
                fencing_token=int(lease.get("fencing_token") or fence),
                expected_revision=int(latest.get("revision") or 0),
                reason=reason,
            )
        except DurableJobTransitionError:
            return await durable_job_repository.get_job(prepared.job_id) or latest

    async def _mark_no_dispatch(
        self,
        prepared: PreparedPublication,
        *,
        reason: str,
        current: Mapping[str, Any],
        owner: str,
        fence: int,
        target_path: str,
    ) -> dict[str, Any]:
        """Close a pre-send failure with explicit, secret-free no-dispatch evidence."""
        observed = await durable_job_repository.record_readback(
            prepared.job_id,
            target_path=target_path,
            effect_id=f"github:{prepared.operation_id}",
            effect_type="github_publication",
            target_digest=prepared.body_sha256,
            content_sha256=_sha(reason),
            status="succeeded",
            details={
                "verified": True,
                "no_dispatch": True,
                "reason_code": reason,
            },
            owner=owner,
            fencing_token=fence,
            expected_revision=int(current.get("revision") or 0),
        )
        latest = await durable_job_repository.get_job(prepared.job_id) or observed
        lease = latest.get("lease") or {}
        try:
            return await durable_job_repository.transition_job(
                prepared.job_id,
                "blocked",
                owner=str(lease.get("owner") or owner),
                fencing_token=int(lease.get("fencing_token") or fence),
                expected_revision=int(latest.get("revision") or 0),
                reason=reason,
            )
        except DurableJobTransitionError:
            return await durable_job_repository.get_job(prepared.job_id) or latest

    async def _handle_prepared_input_failure(
        self,
        current: Mapping[str, Any],
        *,
        reason: str,
    ) -> tuple[dict[str, Any], str]:
        """Close a missing/tampered prepared payload with an explicit receipt.

        The prepared payload is the durable handoff between approval and
        execution.  If it disappears, the worker must not leave the job in an
        approval-held or running state.  An existing publication liability is
        preserved as unknown; otherwise the operator can re-prepare the job.
        """

        job_id = _text(current.get("job_id"))
        latest = await durable_job_repository.get_job(job_id) or dict(current)
        effects = [item for item in latest.get("effects") or [] if isinstance(item, Mapping)]
        publication = next(
            (
                item
                for item in reversed(effects)
                if item.get("effect_type") == "github_publication"
                and item.get("status") in {"intent", "dispatched", "unknown"}
            ),
            None,
        )
        lease = latest.get("lease") if isinstance(latest.get("lease"), Mapping) else {}
        running_owner = str(lease.get("owner") or "")
        running_fence = int(lease.get("fencing_token") or 0)
        owner = running_owner if latest.get("status") == "running" and running_owner else None
        fence = running_fence if owner else None
        if publication is not None:
            target_path = str(publication.get("target_path") or "github:unknown")
            effect_id = str(publication.get("effect_id") or "") or None
            try:
                observed = await durable_job_repository.record_effect(
                    job_id,
                    effect_type="github_publication",
                    effect_id=effect_id,
                    target_path=target_path,
                    target_digest=str(publication.get("target_digest") or "") or None,
                    status="unknown",
                    details={
                        "reason_code": reason,
                        "prepared_input_unavailable": True,
                        "dispatch_confirmed": publication.get("status") == "dispatched",
                    },
                    owner=owner,
                    fencing_token=fence,
                    expected_revision=latest.get("revision"),
                )
                latest = observed
                refreshed_lease = latest.get("lease") if isinstance(latest.get("lease"), Mapping) else {}
                latest = await durable_job_repository.transition_job(
                    job_id,
                    "unknown_external_effect",
                    owner=str(refreshed_lease.get("owner") or owner or "") or None,
                    fencing_token=int(refreshed_lease.get("fencing_token") or fence or 0) or None,
                    expected_revision=latest.get("revision"),
                    reason=reason,
                    result={"recovery_action": "reconcile", "learning": "no_learning"},
                    result_summary="prepared publication input is unavailable; reconcile before retry",
                )
            except DurableJobError:
                latest = await durable_job_repository.get_job(job_id) or latest
            return latest, "reconcile"

        if latest.get("status") not in {"blocked", "unknown_external_effect", "cost_liability", "failed"}:
            try:
                latest = await durable_job_repository.transition_job(
                    job_id,
                    "blocked",
                    owner=owner,
                    fencing_token=fence,
                    expected_revision=latest.get("revision"),
                    reason=reason,
                    result={"recovery_action": "reprepare", "learning": "no_learning"},
                    result_summary="prepared publication input is unavailable; prepare again",
                )
            except DurableJobError:
                latest = await durable_job_repository.get_job(job_id) or latest
        return latest, "reprepare"

    async def _readback(
        self,
        prepared: PreparedPublication,
        *,
        token: str,
        remote_id: int,
        deadline: float,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        path = _canonical_path(prepared.repository, prepared.action, prepared.issue_number, remote_id)
        for attempt in range(READBACK_ATTEMPTS):
            remaining = deadline - _now().timestamp()
            if remaining <= 0:
                return False, "readback_deadline", None
            try:
                response = await self._request(
                    path,
                    method="GET",
                    token=token,
                    timeout_seconds=min(20.0, max(0.1, remaining)),
                )
            except Exception as exc:
                if attempt + 1 >= READBACK_ATTEMPTS:
                    return False, _safe_exception_code(exc), None
                delay = float(1 if attempt == 0 else 2)
                if delay >= remaining:
                    return False, "readback_deadline", None
                await self._sleep(delay)
                continue
            payload = self._response_json(response)
            if response.status_code == 200 and payload is not None:
                if prepared.action == ACTION_CREATE_ISSUE:
                    observed_number = payload.get("number")
                    observed_title = payload.get("title")
                    observed_body = payload.get("body")
                    if (
                        observed_number == remote_id
                        and observed_title == prepared.title
                        and observed_body == prepared.body
                    ):
                        return True, "readback_verified", payload
                    return False, "readback_conflict", payload
                observed_body = payload.get("body")
                observed_issue_url = _text(payload.get("issue_url"))
                expected_issue_url = f"{GITHUB_ORIGIN}/repos/{prepared.repository}/issues/{prepared.issue_number}"
                if (
                    payload.get("id") == remote_id
                    and observed_body == prepared.body
                    and observed_issue_url == expected_issue_url
                ):
                    return True, "readback_verified", payload
                return False, "readback_conflict", payload
            retryable = response.status_code == 404 or response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt + 1 >= READBACK_ATTEMPTS:
                return False, f"readback_http_{response.status_code}", payload
            retry_after = 0.0
            try:
                retry_after = max(0.0, float(response.headers.get("retry-after", "0")))
            except (TypeError, ValueError):
                retry_after = 0.0
            delay = max(retry_after, float(1 if attempt == 0 else 2))
            remaining = deadline - _now().timestamp()
            if delay >= remaining:
                return False, "readback_deferred_retry_after", None
            await self._sleep(delay)
        return False, "readback_exhausted", None

    async def _finalize_verified(
        self,
        prepared: PreparedPublication,
        *,
        current: Mapping[str, Any],
        owner: str,
        fence: int,
        remote_id: int,
        payload: Mapping[str, Any],
        readback_path: str,
    ) -> dict[str, Any]:
        verified_at = _now().isoformat()
        output = {
            "schema": "seraph.github-followthrough-result.v1",
            "operation_id": str(prepared.operation_id),
            "job_id": prepared.job_id,
            "goal_id": prepared.goal_id,
            "source_watch_id": prepared.source_watch_id,
            "dossier_artifact_id": prepared.dossier_artifact_id,
            "dossier_sha256": prepared.dossier_sha256,
            "repository": prepared.repository,
            "action": prepared.action,
            "issue_number": prepared.issue_number,
            "remote_id": remote_id,
            "browser_url": _browser_link(prepared.repository, prepared.action, prepared.issue_number or int(payload.get("number") or 0)),
            "payload_sha256": prepared.body_sha256,
            "readback_path": readback_path,
            "verified_at": verified_at,
            "verification": "passed",
            "usefulness": "unknown",
            "learning": "no_learning",
            "reason": "publication_verified_no_preference_inference",
        }
        path = f"github/followthrough/{_sha(prepared.owner_principal_id)[:24]}/{prepared.job_id}-result.json"
        text = _dump(output) + "\n"
        try:
            _write_workspace_text_bounded(_safe_resolve(path), text, max_bytes=MAX_ARTIFACT_BYTES)
            stored, truncated = _read_workspace_text_bounded(_safe_resolve(path), max_bytes=MAX_ARTIFACT_BYTES)
        except (OSError, ValueError) as exc:
            raise GitHubFollowthroughError("local_finalize_pending", status_code=500) from exc
        if truncated or stored != text:
            raise GitHubFollowthroughError("local_finalize_pending", status_code=500)
        revision = int(current.get("revision") or 0)
        artifact = await durable_job_repository.record_artifact(
            prepared.job_id,
            file_path=path,
            artifact_type="github_followthrough_result",
            content=text,
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
        )
        revision = int(artifact.get("revision") or revision)
        checkpoint = await durable_job_repository.record_checkpoint(
            prepared.job_id,
            checkpoint_id="github-followthrough:published_readback_verified",
            state={
                "phase": "published_readback_verified",
                "remote_id": remote_id,
                "payload_sha256": prepared.body_sha256,
                "result_artifact_id": (artifact.get("receipt") or {}).get("artifact_id"),
                "readback_path": readback_path,
            },
            checkpoint_payload={
                "phase": "published_readback_verified",
                "remote_id": remote_id,
                "result_artifact_id": (artifact.get("receipt") or {}).get("artifact_id"),
            },
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
        )
        revision = int(checkpoint.get("revision") or revision)
        done = await durable_job_repository.transition_job(
            prepared.job_id,
            "succeeded",
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
            result={
                "remote_id": remote_id,
                "result_artifact_id": (artifact.get("receipt") or {}).get("artifact_id"),
                "browser_url": output["browser_url"],
                "source_watch_id": prepared.source_watch_id,
                "dossier_artifact_id": prepared.dossier_artifact_id,
                "dossier_sha256": prepared.dossier_sha256,
                "goal_id": prepared.goal_id,
                "goal_revision": prepared.goal_revision,
                "plan_revision": prepared.plan_revision,
                "verification": "passed",
                "usefulness": "unknown",
                "learning": "no_learning",
            },
            result_summary="GitHub destination readback verified; no preference inferred",
        )
        connection = await self._get_connection_row(prepared.owner_principal_id)
        if connection is not None and connection.active_job_id == prepared.job_id:
            await self._release_connection(
                connection_id=prepared.connection_id,
                owner_principal_id=prepared.owner_principal_id,
                job_id=prepared.job_id,
                fence=int(connection.active_fence or 0),
            )
        return done

    async def _finalize_reconciled(
        self,
        prepared: PreparedPublication,
        *,
        current: Mapping[str, Any],
        remote_id: int,
        payload: Mapping[str, Any],
        readback_path: str,
    ) -> dict[str, Any]:
        """Persist the local result after a recovery-only verified readback."""
        output = {
            "schema": "seraph.github-followthrough-result.v1",
            "operation_id": str(prepared.operation_id),
            "job_id": prepared.job_id,
            "goal_id": prepared.goal_id,
            "source_watch_id": prepared.source_watch_id,
            "dossier_artifact_id": prepared.dossier_artifact_id,
            "dossier_sha256": prepared.dossier_sha256,
            "repository": prepared.repository,
            "action": prepared.action,
            "issue_number": prepared.issue_number,
            "remote_id": remote_id,
            "browser_url": _browser_link(
                prepared.repository,
                prepared.action,
                prepared.issue_number or int(payload.get("number") or 0),
            ),
            "payload_sha256": prepared.body_sha256,
            "readback_path": readback_path,
            "verified_at": _now().isoformat(),
            "verification": "passed",
            "usefulness": "unknown",
            "learning": "no_learning",
            "reason": "publication_verified_during_recovery_no_preference_inference",
        }
        path = f"github/followthrough/{_sha(prepared.owner_principal_id)[:24]}/{prepared.job_id}-result.json"
        content = _dump(output) + "\n"
        try:
            _write_workspace_text_bounded(_safe_resolve(path), content, max_bytes=MAX_ARTIFACT_BYTES)
            stored, truncated = _read_workspace_text_bounded(
                _safe_resolve(path), max_bytes=MAX_ARTIFACT_BYTES
            )
        except (OSError, ValueError) as exc:
            raise GitHubFollowthroughError("local_finalize_pending", status_code=500) from exc
        if truncated or stored != content:
            raise GitHubFollowthroughError("local_finalize_pending", status_code=500)
        revision = int(current.get("revision") or 0)
        artifact = await durable_job_repository.record_recovery_artifact(
            prepared.job_id,
            owner_kind="user",
            owner_principal_id=prepared.owner_principal_id,
            file_path=path,
            artifact_type="github_followthrough_result",
            content=content,
            expected_revision=revision,
        )
        revision = int(artifact.get("revision") or revision)
        checkpoint = await durable_job_repository.record_recovery_checkpoint(
            prepared.job_id,
            owner_kind="user",
            owner_principal_id=prepared.owner_principal_id,
            checkpoint_id="github-followthrough:published_readback_verified",
            state={
                "phase": "published_readback_verified",
                "remote_id": remote_id,
                "result_artifact_id": (artifact.get("receipt") or {}).get("artifact_id"),
                "readback_path": readback_path,
            },
            checkpoint_payload={
                "phase": "published_readback_verified",
                "remote_id": remote_id,
                "result_artifact_id": (artifact.get("receipt") or {}).get("artifact_id"),
            },
            expected_revision=revision,
        )
        finalized = await durable_job_repository.finalize_reconciled_job(
            prepared.job_id,
            owner_kind="user",
            owner_principal_id=prepared.owner_principal_id,
            expected_revision=int(checkpoint.get("revision") or revision),
            result={
                "remote_id": remote_id,
                "result_artifact_id": (artifact.get("receipt") or {}).get("artifact_id"),
                "browser_url": output["browser_url"],
                "source_watch_id": prepared.source_watch_id,
                "dossier_artifact_id": prepared.dossier_artifact_id,
                "dossier_sha256": prepared.dossier_sha256,
                "goal_id": prepared.goal_id,
                "goal_revision": prepared.goal_revision,
                "plan_revision": prepared.plan_revision,
                "verification": "passed",
                "usefulness": "unknown",
                "learning": "no_learning",
            },
            result_summary="GitHub destination readback reconciled; no preference inferred",
        )
        connection = await self._get_connection_row(prepared.owner_principal_id)
        if connection is not None and connection.active_job_id == prepared.job_id:
            await self._release_connection(
                connection_id=connection.id,
                owner_principal_id=prepared.owner_principal_id,
                job_id=prepared.job_id,
                fence=int(connection.active_fence or 0),
            )
        return finalized

    async def execute(
        self,
        *,
        owner_principal_id: str,
        job_id: str,
        owner_session_id: str | None = None,
        external_mutation_granted: bool = False,
    ) -> dict[str, Any]:
        await _require_live_owner_session(
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id or "",
            external_mutation_granted=external_mutation_granted,
        )
        current = await durable_job_repository.get_job(job_id)
        if current is None or current.get("owner", {}).get("principal_id") != owner_principal_id:
            raise GitHubFollowthroughError("job_not_found", status_code=404)
        if current.get("status") in {"succeeded", "cancelled"}:
            return await self._prepare_job_response(current)
        if current.get("status") == "queued" and not _queued_approval_is_current(current):
            raise GitHubFollowthroughError("approval_required", status_code=403)
        try:
            prepared = await self._read_prepared(current)
        except GitHubFollowthroughError as exc:
            recovered, recovery_action = await self._handle_prepared_input_failure(
                current,
                reason=exc.code,
            )
            result = await self._prepare_job_response(recovered)
            result["recovery_action"] = recovery_action
            result["reason_code"] = exc.code
            return result
        if current.get("status") == "awaiting_approval":
            approval_id = _text((current.get("declared_authority") or {}).get("approval_id"))
            approval = await approval_repository.get(approval_id)
            if approval is None:
                raise GitHubFollowthroughError("approval_missing")
            if approval.status != "approved":
                if approval.status in {"denied", "expired"}:
                    cancelled = await durable_job_repository.cancel_job(
                        job_id,
                        expected_revision=current.get("revision"),
                        reason=f"approval_{approval.status}",
                    )
                    return await self._prepare_job_response(cancelled)
                result = await self._prepare_job_response(current, prepared=prepared)
                result["approval_status"] = approval.status
                return result
            details = _load(approval.details_json, {})
            expires_at = float(details.get("approval_expires_at", details.get("expires_at", 0)))
            receipt = {
                "status": "approved",
                "authenticated": True,
                "operator_principal_id": owner_principal_id,
                "operator_session_id": str(approval.operator_session_id or approval.session_id or ""),
                "owner_kind": "user",
                "owner_principal_id": owner_principal_id,
                "service_id": None,
                "approval_id": approval.id,
                "authority_digest": current.get("authority_digest"),
                "goal_id": current.get("goal_id"),
                "goal_revision": current.get("goal_revision"),
                "plan_revision": current.get("plan_revision"),
                "capability_version": current.get("capability_version"),
                "budget_microusd": 0,
                "budget_digest": current.get("budget_digest"),
                "expires_at": expires_at,
            }
            resumed = await durable_job_repository.resume_approved_job(
                job_id,
                approval_receipt=receipt,
                approval_id=approval.id,
                authority_digest=str(current.get("authority_digest") or ""),
                goal_id=current.get("goal_id"),
                goal_revision=current.get("goal_revision"),
                plan_revision=current.get("plan_revision"),
                capability_version=str(current.get("capability_version") or CAPABILITY_VERSION),
                owner_kind="user",
                owner_principal_id=owner_principal_id,
                service_id=None,
                budget_microusd=0,
                budget_digest=str(current.get("budget_digest") or ""),
                operator_principal_id=owner_principal_id,
                operator_session_id=str(approval.operator_session_id or approval.session_id or ""),
                expires_at=expires_at,
                expected_revision=current.get("revision"),
            )
            current = await durable_job_repository.get_job(job_id) or resumed
        if current.get("status") == "queued":
            runner = RUNNER_PREFIX + job_id
            current = await durable_job_repository.claim_job(
                job_id,
                owner=runner,
                expected_revision=current.get("revision"),
                expected_fencing_token=(current.get("lease") or {}).get("fencing_token"),
                lease_seconds=EXECUTION_DEADLINE_SECONDS,
            )
        if current.get("status") != "running":
            return await self._prepare_job_response(current, prepared=prepared)
        lease = current.get("lease") or {}
        owner = str(lease.get("owner") or "")
        fence = int(lease.get("fencing_token") or 0)
        try:
            connection = await self._get_connection_row(owner_principal_id)
            if connection is None:
                raise GitHubFollowthroughError("connection_missing", status_code=409)
            reservation_fence = await self._reserve_connection(
                owner_principal_id=owner_principal_id,
                connection_id=prepared.connection_id,
                expected_revision=prepared.connection_revision,
                job_id=job_id,
            )
            await self._verify_live_handoff(
                prepared,
                owner_principal_id=owner_principal_id,
                connection=connection,
            )
            current = await durable_job_repository.get_job(job_id) or current
            lease = current.get("lease") or lease
            owner = str(lease.get("owner") or owner)
            fence = int(lease.get("fencing_token") or fence)
            intent = await self._record_intent(prepared, current=current, owner=owner, fence=fence)
            current = await durable_job_repository.get_job(job_id) or intent
            can_dispatch, current = await self._acquire_dispatch_guard(
                prepared,
                current=current,
            )
            if not can_dispatch:
                latest_lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
                latest_owner = str(latest_lease.get("owner") or owner)
                latest_fence = int(latest_lease.get("fencing_token") or fence)
                target_path = next(
                    (
                        str(item.get("target_path"))
                        for item in reversed(current.get("effects") or [])
                        if isinstance(item, Mapping)
                        and item.get("effect_type") == "github_publication"
                        and item.get("target_path")
                    ),
                    "github:dispatch-guard",
                )
                recovered = await self._mark_unknown(
                    prepared,
                    reason="dispatch_guard_recovery_required",
                    current=current,
                    owner=latest_owner,
                    fence=latest_fence,
                    target_path=target_path,
                )
                result = await self._prepare_job_response(recovered, prepared=prepared)
                result["dispatch"] = "recovery_required"
                result["recovery_action"] = "reconcile"
                return result
            # Re-read the approval, connection revision, and dossier handoff
            # after the dispatch fence and immediately before resolving the
            # credential or sending bytes.  Revocation or expiry therefore
            # closes the intent as a known no-dispatch outcome.
            approval_id = _text((current.get("declared_authority") or {}).get("approval_id"))
            approval = await approval_repository.get(approval_id)
            if approval is None or approval.status != "approved":
                raise GitHubFollowthroughError("approval_not_current")
            approval_session = _text(approval.operator_session_id or approval.session_id)
            if approval_session != prepared.owner_session_id:
                raise GitHubFollowthroughError("approval_owner_session_mismatch")
            approval_details = _load(approval.details_json, {})
            try:
                approval_expires_at = float(approval_details.get("approval_expires_at", approval_details.get("expires_at", 0)))
            except (TypeError, ValueError, OverflowError) as exc:
                raise GitHubFollowthroughError("approval_expiry_invalid") from exc
            if approval_expires_at <= _now().timestamp():
                raise GitHubFollowthroughError("approval_expired")
            live_connection = await self._get_connection_row(owner_principal_id)
            if live_connection is None:
                raise GitHubFollowthroughError("connection_missing", status_code=409)
            await self._verify_live_handoff(
                prepared,
                owner_principal_id=owner_principal_id,
                connection=live_connection,
            )
            await _require_live_owner_session(
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id or prepared.owner_session_id,
                external_mutation_granted=external_mutation_granted,
            )
            connection = live_connection
            await self._assert_dispatch_binding(
                owner_principal_id=owner_principal_id,
                connection_id=prepared.connection_id,
                expected_revision=prepared.connection_revision,
                repository=prepared.repository,
                vault_key=str(connection.vault_key),
                mode=str(connection.mode),
                job_id=job_id,
                fence=reservation_fence,
            )
            token = await self._load_token(connection)
            deadline = min(
                _now().timestamp() + EXECUTION_DEADLINE_SECONDS,
                datetime.fromisoformat(str(current["deadline_at"]).replace("Z", "+00:00")).timestamp()
                if current.get("deadline_at")
                else _now().timestamp() + EXECUTION_DEADLINE_SECONDS,
            )
            if prepared.action == ACTION_CREATE_ISSUE:
                post_path = f"/repos/{prepared.repository}/issues"
                request_body = {"title": prepared.title, "body": prepared.body}
            else:
                post_path = f"/repos/{prepared.repository}/issues/{prepared.issue_number}/comments"
                request_body = {"body": prepared.body}
            try:
                response = await self._request(
                    post_path,
                    method="POST",
                    token=token,
                    json_body=request_body,
                    timeout_seconds=min(20.0, max(0.1, deadline - _now().timestamp())),
                )
            except Exception as exc:
                unknown = await self._mark_unknown(
                    prepared,
                    reason=_safe_exception_code(exc),
                    current=await durable_job_repository.get_job(job_id) or current,
                    owner=owner,
                    fence=fence,
                    target_path=post_path,
                )
                return await self._prepare_job_response(unknown, prepared=prepared)
            response_payload = self._response_json(response)
            if response.status_code in {401, 403, 404, 422}:
                blocked = await self._mark_blocked(
                    prepared,
                    reason=f"github_post_{response.status_code}",
                    current=await durable_job_repository.get_job(job_id) or current,
                    owner=owner,
                    fence=fence,
                    target_path=post_path,
                    response=response,
                )
                connection_after_rejection = await self._get_connection_row(owner_principal_id)
                if connection_after_rejection is not None and connection_after_rejection.active_job_id == job_id:
                    await self._release_connection(
                        connection_id=connection_after_rejection.id,
                        owner_principal_id=owner_principal_id,
                        job_id=job_id,
                        fence=int(connection_after_rejection.active_fence or 0),
                    )
                return await self._prepare_job_response(blocked, prepared=prepared)
            if response.status_code < 200 or response.status_code >= 300 or response_payload is None:
                unknown = await self._mark_unknown(
                    prepared,
                    reason=f"github_post_{response.status_code if response.status_code else 'malformed'}",
                    current=await durable_job_repository.get_job(job_id) or current,
                    owner=owner,
                    fence=fence,
                    target_path=post_path,
                )
                return await self._prepare_job_response(unknown, prepared=prepared)
            if prepared.action == ACTION_CREATE_ISSUE:
                remote_id = response_payload.get("number")
            else:
                remote_id = response_payload.get("id")
            if isinstance(remote_id, bool) or not isinstance(remote_id, int) or remote_id <= 0:
                unknown = await self._mark_unknown(
                    prepared,
                    reason="github_post_missing_remote_id",
                    current=await durable_job_repository.get_job(job_id) or current,
                    owner=owner,
                    fence=fence,
                    target_path=post_path,
                )
                return await self._prepare_job_response(unknown, prepared=prepared)
            dispatch = await durable_job_repository.record_effect(
                job_id,
                effect_type="github_publication",
                effect_id=f"github:{prepared.operation_id}",
                target_path=post_path,
                target_digest=prepared.body_sha256,
                status="dispatched",
                details={
                    "operation_id": str(prepared.operation_id),
                    "remote_id": remote_id,
                    "response_status": response.status_code,
                    "repository": prepared.repository,
                    "action": prepared.action,
                    "issue_number": prepared.issue_number,
                    "dispatch_confirmed": True,
                    "browser_url": _browser_link(
                        prepared.repository,
                        prepared.action,
                        prepared.issue_number or int(response_payload.get("number") or remote_id),
                    ),
                },
                owner=owner,
                fencing_token=fence,
                expected_revision=int((await durable_job_repository.get_job(job_id) or current).get("revision") or 0),
            )
            latest = await durable_job_repository.get_job(job_id) or dispatch
            verified, reason, readback_payload = await self._readback(
                prepared,
                token=token,
                remote_id=remote_id,
                deadline=deadline,
            )
            readback_path = _canonical_path(prepared.repository, prepared.action, prepared.issue_number, remote_id)
            if not verified or readback_payload is None:
                unknown = await self._mark_unknown(
                    prepared,
                    reason=reason,
                    current=latest,
                    owner=owner,
                    fence=fence,
                    readback_observation=True,
                    target_path=readback_path,
                )
                return await self._prepare_job_response(unknown, prepared=prepared)
            readback = await durable_job_repository.record_readback(
                job_id,
                target_path=readback_path,
                effect_id=f"github:{prepared.operation_id}",
                effect_type="github_publication",
                target_digest=prepared.body_sha256,
                content_sha256=_sha(_dump(readback_payload)),
                status="succeeded",
                details={
                    "verified": True,
                    "remote_id": remote_id,
                    "repository": prepared.repository,
                    "action": prepared.action,
                    "body_sha256": prepared.body_sha256,
                    "title_sha256": _sha(prepared.title or ""),
                    "issue_url_matches": prepared.action == ACTION_CREATE_ISSUE
                    or _text(readback_payload.get("issue_url"))
                    == f"{GITHUB_ORIGIN}/repos/{prepared.repository}/issues/{prepared.issue_number}",
                },
                owner=owner,
                fencing_token=fence,
                expected_revision=int((await durable_job_repository.get_job(job_id) or latest).get("revision") or 0),
            )
            done = await self._finalize_verified(
                prepared,
                current=await durable_job_repository.get_job(job_id) or readback,
                owner=owner,
                fence=fence,
                remote_id=remote_id,
                payload=readback_payload,
                readback_path=readback_path,
            )
            return await self._prepare_job_response(done, prepared=prepared)
        except GitHubFollowthroughError as exc:
            latest = await durable_job_repository.get_job(job_id) or current
            if latest.get("status") == "running":
                try:
                    effects = [item for item in latest.get("effects") or [] if isinstance(item, Mapping)]
                    github_effect = next(
                        (
                            item
                            for item in reversed(effects)
                            if item.get("effect_type") == "github_publication"
                        ),
                        None,
                    )
                    effect_status = github_effect.get("status") if github_effect else None
                    if effect_status == "intent":
                        blocked = await self._mark_no_dispatch(
                            prepared,
                            reason=exc.code,
                            current=latest,
                            owner=owner,
                            fence=fence,
                            target_path=str(github_effect.get("target_path") or "github:no-dispatch"),
                        )
                    else:
                        blocked = await durable_job_repository.transition_job(
                            job_id,
                            "blocked",
                            owner=owner,
                            fencing_token=fence,
                            expected_revision=latest.get("revision"),
                            reason=exc.code,
                        )
                    if effect_status in {None, "intent"}:
                        connection_after_failure = await self._get_connection_row(owner_principal_id)
                        if connection_after_failure is not None and connection_after_failure.active_job_id == job_id:
                            await self._release_connection(
                                connection_id=connection_after_failure.id,
                                owner_principal_id=owner_principal_id,
                                job_id=job_id,
                                fence=int(connection_after_failure.active_fence or 0),
                            )
                except Exception:
                    blocked = latest
            else:
                blocked = latest
            return await self._prepare_job_response(blocked, prepared=prepared)

    async def get_job(self, *, owner_principal_id: str, job_id: str) -> dict[str, Any]:
        current = await durable_job_repository.get_job(job_id)
        if current is None or current.get("owner", {}).get("principal_id") != owner_principal_id:
            raise GitHubFollowthroughError("job_not_found", status_code=404)
        return await self._prepare_job_response(current)

    async def cancel(
        self,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        job_id: str,
    ) -> dict[str, Any]:
        current = await durable_job_repository.get_job(job_id)
        if current is None or current.get("owner", {}).get("principal_id") != owner_principal_id:
            raise GitHubFollowthroughError("job_not_found", status_code=404)
        authority = current.get("declared_authority") if isinstance(current.get("declared_authority"), Mapping) else {}
        persisted_session = _text(
            authority.get("session_id")
            or current.get("operator_session_id")
            or current.get("session_id")
        )
        if not _text(owner_session_id) or persisted_session != _text(owner_session_id):
            raise GitHubFollowthroughError("job_session_mismatch", status_code=403)
        if current.get("status") in {"succeeded", "cancelled"}:
            return await self._prepare_job_response(current)
        lease = current.get("lease") or {}
        try:
            cancelled = await durable_job_repository.cancel_job(
                job_id,
                owner=str(lease.get("owner")) if lease.get("owner") else None,
                fencing_token=int(lease.get("fencing_token")) if lease.get("owner") else None,
                expected_revision=current.get("revision"),
                reason="operator_cancelled",
            )
        except DurableJobError as exc:
            raise GitHubFollowthroughError("cancel_conflict") from exc
        # An approval-held or queued job has no external effect. A running job
        # with an intent is conservatively kept reserved by the connection
        # until the destination is reconciled.
        if cancelled.get("status") == "cancelled":
            prepared = None
            try:
                prepared = await self._read_prepared(cancelled)
            except GitHubFollowthroughError:
                pass
            if prepared is not None and not any(
                isinstance(item, Mapping)
                and item.get("status") in {"intent", "dispatched", "unknown"}
                and item.get("effect_type") == "github_publication"
                for item in cancelled.get("effects") or []
            ):
                connection = await self._get_connection_row(owner_principal_id)
                if connection and connection.active_job_id == job_id and connection.active_fence:
                    await self._release_connection(
                        connection_id=connection.id,
                        owner_principal_id=owner_principal_id,
                        job_id=job_id,
                        fence=int(connection.active_fence),
                    )
        return await self._prepare_job_response(cancelled)

    async def reconcile(
        self,
        *,
        owner_principal_id: str,
        job_id: str,
        request: ReconcileRequest,
    ) -> dict[str, Any]:
        current = await durable_job_repository.get_job(job_id)
        if current is None or current.get("owner", {}).get("principal_id") != owner_principal_id:
            raise GitHubFollowthroughError("job_not_found", status_code=404)
        if current.get("status") == "succeeded":
            return await self._prepare_job_response(current)
        prepared = await self._read_prepared(current)
        connection = await self._get_connection_row(owner_principal_id)
        if connection is None or connection.id != prepared.connection_id:
            raise GitHubFollowthroughError("connection_not_found", status_code=404)
        if connection.repository != prepared.repository:
            raise GitHubFollowthroughError("repository_binding_changed")
        if connection.mode not in {CONNECTION_MODE_RECONCILE_ONLY, CONNECTION_MODE_ACTIVE}:
            raise GitHubFollowthroughError("reconcile_grant_required", status_code=403)
        recorded_remote_id: int | None = None
        for item in reversed(current.get("effects") or []):
            if not isinstance(item, Mapping):
                continue
            details = item.get("details")
            if isinstance(details, Mapping) and isinstance(details.get("remote_id"), int) and not isinstance(details.get("remote_id"), bool):
                recorded_remote_id = int(details["remote_id"])
                break
        remote_id = request.remote_id
        if remote_id is not None and recorded_remote_id is not None and remote_id != recorded_remote_id:
            raise GitHubFollowthroughError("remote_id_binding_conflict", status_code=409)
        if remote_id is None:
            remote_id = recorded_remote_id
        if remote_id is None:
            raise GitHubFollowthroughError("remote_id_required", status_code=409)
        token = await self._load_token(connection)
        deadline = _now().timestamp() + EXECUTION_DEADLINE_SECONDS
        verified, reason, payload = await self._readback(
            prepared,
            token=token,
            remote_id=remote_id,
            deadline=deadline,
        )
        readback_path = _canonical_path(prepared.repository, prepared.action, prepared.issue_number, remote_id)
        if not verified or payload is None:
            # A failed readback is an observation only. It cannot clear the
            # original intent or authorize a new POST.
            try:
                observed = await durable_job_repository.record_readback(
                    job_id,
                    target_path=readback_path,
                    effect_id=f"github:{prepared.operation_id}",
                    effect_type="github_publication",
                    target_digest=prepared.body_sha256,
                    status="unknown",
                    details={
                        "verified": False,
                        "reason_code": reason,
                        "reconciliation_owner_id": owner_principal_id,
                    },
                    owner=None,
                    fencing_token=None,
                    expected_revision=current.get("revision"),
                )
            except DurableJobError:
                observed = current
            result = await self._prepare_job_response(observed, prepared=prepared)
            result["reconciliation"] = "unresolved"
            result["reason_code"] = reason
            return result
        readback = await durable_job_repository.record_readback(
            job_id,
            target_path=readback_path,
            effect_id=f"github:{prepared.operation_id}",
            effect_type="github_publication",
            target_digest=prepared.body_sha256,
            content_sha256=_sha(_dump(payload)),
            status="succeeded",
            details={
                "verified": True,
                "remote_id": remote_id,
                "repository": prepared.repository,
                "action": prepared.action,
                "reconciliation_owner_id": owner_principal_id,
            },
            owner=None,
            fencing_token=None,
            expected_revision=current.get("revision"),
        )
        finalized = await self._finalize_reconciled(
            prepared,
            current=readback,
            remote_id=remote_id,
            payload=payload,
            readback_path=readback_path,
        )
        return await self._prepare_job_response(finalized, prepared=prepared)


github_followthrough_service = GitHubFollowthroughService()


github_followthrough_router = APIRouter(prefix="/capabilities/github", tags=["github-followthrough"])


def _raise_http(exc: GitHubFollowthroughError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code})


@github_followthrough_router.get("/connection")
async def get_github_connection(request: Request):
    try:
        operator = _operator(request)
        return await github_followthrough_service.get_connection(_principal_id(operator))
    except GitHubFollowthroughError as exc:
        raise _raise_http(exc) from exc


@github_followthrough_router.put("/connection")
async def put_github_connection(req: ConnectionRequest, request: Request):
    try:
        operator = _operator(request)
        owner = _principal_id(operator)
        if req.mode == CONNECTION_MODE_ACTIVE and not _has_grant(operator, AuthorityGrant.EXTERNAL_MUTATION):
            raise GitHubFollowthroughError("external_mutation_grant_required", status_code=403)
        return await github_followthrough_service.put_connection(
            owner_principal_id=owner,
            repository=req.repository,
            vault_key=req.vault_key,
            mode=req.mode,
            expected_revision=req.expected_revision,
        )
    except GitHubFollowthroughError as exc:
        raise _raise_http(exc) from exc


@github_followthrough_router.post("/prepare")
async def prepare_github_followthrough(req: PrepareRequest, request: Request):
    try:
        operator = _operator(request)
        return await github_followthrough_service.prepare(
            owner_principal_id=_principal_id(operator),
            owner_session_id=_session_id(operator),
            external_mutation_granted=_has_grant(operator, AuthorityGrant.EXTERNAL_MUTATION),
            request=req,
        )
    except GitHubFollowthroughError as exc:
        raise _raise_http(exc) from exc
    except (DurableJobError, IntegrityError) as exc:
        raise HTTPException(status_code=409, detail={"code": "durable_job_conflict"}) from exc


@github_followthrough_router.get("/jobs/{job_id}")
async def get_github_followthrough_job(job_id: str, request: Request):
    try:
        operator = _operator(request)
        await _require_job_session(job_id, operator)
        return await github_followthrough_service.get_job(
            owner_principal_id=_principal_id(operator),
            job_id=job_id,
        )
    except GitHubFollowthroughError as exc:
        raise _raise_http(exc) from exc


@github_followthrough_router.post("/jobs/{job_id}/execute")
async def execute_github_followthrough_job(job_id: str, request: Request):
    try:
        operator = _operator(request)
        if not _has_grant(operator, AuthorityGrant.EXTERNAL_MUTATION):
            raise GitHubFollowthroughError("external_mutation_grant_required", status_code=403)
        await _require_job_session(job_id, operator)
        return await github_followthrough_service.execute(
            owner_principal_id=_principal_id(operator),
            job_id=job_id,
            owner_session_id=_session_id(operator),
            external_mutation_granted=_has_grant(operator, AuthorityGrant.EXTERNAL_MUTATION),
        )
    except GitHubFollowthroughError as exc:
        raise _raise_http(exc) from exc
    except (DurableJobError, IntegrityError) as exc:
        raise HTTPException(status_code=409, detail={"code": "durable_job_conflict"}) from exc


@github_followthrough_router.post("/jobs/{job_id}/cancel")
async def cancel_github_followthrough_job(job_id: str, request: Request):
    try:
        operator = _operator(request)
        await _require_job_session(job_id, operator)
        return await github_followthrough_service.cancel(
            owner_principal_id=_principal_id(operator),
            owner_session_id=_session_id(operator),
            job_id=job_id,
        )
    except GitHubFollowthroughError as exc:
        raise _raise_http(exc) from exc


@github_followthrough_router.post("/jobs/{job_id}/reconcile")
async def reconcile_github_followthrough_job(
    job_id: str,
    req: ReconcileRequest,
    request: Request,
):
    try:
        operator = _operator(request)
        await _require_job_session(job_id, operator)
        return await github_followthrough_service.reconcile(
            owner_principal_id=_principal_id(operator),
            job_id=job_id,
            request=req,
        )
    except GitHubFollowthroughError as exc:
        raise _raise_http(exc) from exc
    except DurableJobError as exc:
        raise HTTPException(status_code=409, detail={"code": "reconcile_conflict"}) from exc


__all__ = [
    "ACTION_CREATE_COMMENT",
    "ACTION_CREATE_ISSUE",
    "CAPABILITY_ID",
    "CAPABILITY_VERSION",
    "ConnectionRequest",
    "GitHubFollowthroughError",
    "GitHubFollowthroughService",
    "PrepareRequest",
    "ReconcileRequest",
    "github_followthrough_router",
    "github_followthrough_service",
]
