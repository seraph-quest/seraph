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
from typing import Any, Awaitable, Callable, Literal, Mapping

from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from src.approval.repository import approval_repository, fingerprint_tool_call, _approval_expiry
from src.auth.service import AuthFailure, authenticate_session
from src.db import engine as db_engine
from src.db.models import (
    Goal,
    GuardianDecisionPacket,
    GuardianRoutine,
    GuardianRoutineVersion,
    GuardianSourceWatch,
    GitHubFollowthroughConnection,
    WorkflowRunState,
)
from src.extensions.capability_pack import CapabilityPackLifecycle
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
from src.extensions.github_consent import GitHubConsentRequest, GitHubReadbackAuthority, live_operator, issuance, require_consent, projection as consent_projection, mutation_action
from src.workflows.job_runtime import (
    DurableJobError,
    DurableJobIdentity,
    DurableJobRoutinePublicationAdmissionGuard,
    DurableJobSpec,
    DurableJobTransitionError,
    UNRESOLVED_EFFECT_STATUSES,
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
_SAFE_APPROVAL_CLEANUP_OUTCOMES = frozenset(
    {"denied", "not_bound", "missing", "approved", "consumed", "expired"}
)
ROUTINE_PUBLICATION_CHILD_JOB_KIND = "routine_github_followthrough_child"
ROUTINE_BINDING_KEYS = frozenset(
    {
        "routine_id",
        "routine_revision",
        "routine_version",
        "package_digest",
        "parent_invocation_job_id",
        "publication_child_job_id",
        "invocation_uuid",
        "owner_principal_id",
        "owner_session_id",
        "goal_id",
        "goal_revision",
        "source_watch_id",
        "connection_id",
        "connection_revision",
        "repository",
        "action",
        "operation_uuid",
    }
)
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


def _followthrough_approval_scope(
    *,
    operation_id: str,
    job_id: str,
    connection_id: str,
    connection_revision: int,
    repository: str,
    action: str,
    issue_number: int | None,
    title_sha256: str,
    body_sha256: str,
    source_watch_id: str,
    plan_revision: int,
    goal_id: str,
    goal_revision: int,
    dossier_artifact_id: str,
    dossier_sha256: str,
) -> dict[str, Any]:
    """Return safe, exact destination and payload identity for operator review.

    The approval surface must show which repository and operation will be
    written while keeping private task prose out of approval details. Digests
    bind the exact title/body, dossier, goal, connection, and durable job.
    """

    return {
        "action": action,
        "target": {
            "provider": "github",
            "repository": repository,
            "issue_number": issue_number,
        },
        "authority": {
            "operation_id": operation_id,
            "job_id": job_id,
            "connection_id": connection_id,
            "connection_revision": connection_revision,
            "source_watch_id": source_watch_id,
            "plan_revision": plan_revision,
            "goal_id": goal_id,
            "goal_revision": goal_revision,
            "dossier_artifact_id": dossier_artifact_id,
            "dossier_sha256": dossier_sha256,
        },
        "payload": {
            "title_sha256": title_sha256,
            "body_sha256": body_sha256,
        },
        "effects": [action],
    }


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


def _consumed_approval_resume_is_current(
    current: Mapping[str, Any], approval: Any
) -> bool:
    """Allow a consumed approval only through its exact durable resume receipt.

    Resolving an ApprovalRequest consumes that one-shot row while placing the
    already admitted workflow run on the durable queue. The row alone must not
    authorize dispatch: the run, owner/session, authority, goal, budget,
    approval identity, and expiry must all match the immutable receipt written
    by ``resume_approved_job``.
    """
    if _text(getattr(approval, "status", None)) != "consumed":
        return False
    if _text(current.get("status")) not in {"queued", "running"}:
        return False
    approval_id = _text(getattr(approval, "id", None))
    job_id = _text(current.get("job_id") or current.get("run_identity"))
    authority = current.get("declared_authority")
    authority = authority if isinstance(authority, Mapping) else {}
    owner = current.get("owner")
    owner = owner if isinstance(owner, Mapping) else {}
    details = _load(getattr(approval, "details_json", None), {})
    if not isinstance(details, Mapping):
        return False
    session_id = _text(current.get("operator_session_id") or current.get("session_id"))
    if (
        not approval_id
        or not job_id
        or _text(authority.get("approval_id")) != approval_id
        or _text(getattr(approval, "owner_principal_id", None)) != _text(owner.get("principal_id"))
        or _text(getattr(approval, "operator_session_id", None) or getattr(approval, "session_id", None)) != session_id
        or _text(details.get("durable_approval_id")) != approval_id
        or _text(details.get("durable_job_id")) != job_id
        or _text(details.get("durable_owner_kind")) != _text(owner.get("kind"))
        or _text(details.get("durable_owner_principal_id")) != _text(owner.get("principal_id"))
        or _text(details.get("operator_session_id")) != session_id
        or _text(details.get("durable_authority_digest")) != _text(current.get("authority_digest"))
        or _text(details.get("durable_goal_id")) != _text(current.get("goal_id"))
        or details.get("durable_goal_revision") != current.get("goal_revision")
        or details.get("durable_plan_revision") != current.get("plan_revision")
        or _text(details.get("durable_capability_version")) != _text(current.get("capability_version"))
        or _text(details.get("durable_budget_digest")) != _text(current.get("budget_digest"))
    ):
        return False
    try:
        approval_expiry = float(details.get("approval_expires_at", details.get("expires_at")))
    except (TypeError, ValueError, OverflowError):
        return False
    if not approval_expiry > _now().timestamp():
        return False
    for item in current.get("effects") or []:
        if not isinstance(item, Mapping):
            continue
        if (
            item.get("kind") != "approval_resume"
            or item.get("status") != "approved"
            or item.get("approval_request_status") != "consumed"
            or _text(item.get("approval_id")) != approval_id
            or _text(item.get("operator_principal_id")) != _text(owner.get("principal_id"))
            or _text(item.get("operator_session_id")) != session_id
            or _text(item.get("owner_kind")) != _text(owner.get("kind"))
            or _text(item.get("owner_principal_id")) != _text(owner.get("principal_id"))
            or _text(item.get("authority_digest")) != _text(current.get("authority_digest"))
            or _text(item.get("goal_id")) != _text(current.get("goal_id"))
            or item.get("goal_revision") != current.get("goal_revision")
            or item.get("plan_revision") != current.get("plan_revision")
            or _text(item.get("capability_version")) != _text(current.get("capability_version"))
            or _text(item.get("budget_digest")) != _text(current.get("budget_digest"))
        ):
            continue
        try:
            receipt_expiry = float(item.get("expires_at"))
        except (TypeError, ValueError, OverflowError):
            continue
        if receipt_expiry == approval_expiry and receipt_expiry > _now().timestamp():
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


def _routine_pack_id(routine_id: str, version: int) -> str:
    """Derive the server-owned package identity without importing routines.py.

    ``routines.py`` imports this adapter, so the execute-time package check
    cannot call ``RoutineService`` without creating an import cycle.  The
    package identity is intentionally a small, deterministic contract shared
    by the two services.
    """

    token = _text(routine_id).replace("-", "").lower()
    try:
        if len(token) != 32:
            raise ValueError
        uuid.UUID(hex=token)
        if int(version) < 1:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as exc:
        raise GitHubFollowthroughError("routine_binding_invalid") from exc
    return f"seraph.routine.{token}.v{int(version)}"


def _routine_child_job_id(invocation_uuid: str) -> str:
    """Return the fixed publication child identity used by RoutineService."""

    try:
        namespace = uuid.UUID(str(invocation_uuid))
    except (TypeError, ValueError, AttributeError) as exc:
        raise GitHubFollowthroughError("routine_binding_invalid") from exc
    child_uuid = uuid.uuid5(namespace, "seraph:guardian-routine:publication")
    return f"routine-child:{child_uuid.hex}"


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

    Routine and recovery callers reach this service directly. Identity must
    remain live; exact canonical connection consent is checked separately at
    prepare and every protected mutation handoff. The legacy Boolean argument
    is retained for call compatibility and supplies no authority.
    """

    if not _text(owner_session_id):
        raise GitHubFollowthroughError("owner_session_required", status_code=403)
    try:
        operator = await authenticate_session(owner_session_id, touch=False)
    except AuthFailure as exc:
        raise GitHubFollowthroughError("owner_session_invalid", status_code=403) from exc
    principal_id = _text(getattr(getattr(operator, "principal", None), "principal_id", None))
    if _text(getattr(operator, "session_id", None)) != _text(owner_session_id) or principal_id != _text(owner_principal_id):
        raise GitHubFollowthroughError("owner_session_mismatch", status_code=403)
    # This GitHub-only helper checks identity. Mutation authority is derived
    # independently from the exact canonical connection consent at handoff.


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


def _publication_effect_target_path(prepared: PreparedPublication) -> str:
    """Return the immutable approved write target for one publication."""
    if prepared.action == ACTION_CREATE_ISSUE:
        return f"/repos/{prepared.repository}/issues"
    return f"/repos/{prepared.repository}/issues/{prepared.issue_number}/comments"


def _publication_readback_id(prepared: PreparedPublication) -> str:
    return f"github-readback:{prepared.operation_id}"


class ConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    repository: str = Field(min_length=3, max_length=200)
    vault_key: str = Field(min_length=1, max_length=160)
    mode: str = CONNECTION_MODE_DISABLED
    expected_revision: int = Field(ge=0)
    consent: GitHubConsentRequest | None = None


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
    model_config = ConfigDict(extra="forbid", strict=True)

    remote_id: int | None = Field(default=None, gt=0)
    acknowledged_readback: Literal[True]
    expected_connection_revision: int = Field(gt=0)

    @field_validator("acknowledged_readback", mode="before")
    @classmethod
    def exact_read_ack(cls, value):
        if value is not True:
            raise ValueError("explicit original-root readback acknowledgment required")
        return value


from src.extensions.github_capacity_closure import LegacyCloseRequest


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
        self._last_verified_get = None

    async def _request(
        self,
        url: str,
        *,
        method: str,
        token: str | None = None,
        json_body: dict[str, Any] | None = None,
        timeout_seconds: float = 20.0,
        authority_check: Callable[[], Awaitable[None]] | None = None,
        github_consent_binding: dict[str, Any] | None = None,
        readback_authority=None,
        read_window=None,
    ) -> PinnedResponse:
        if read_window is not None:
            from src.extensions.github_capacity_closure import ReadWindow
            if type(read_window) is not ReadWindow or method != "GET" or readback_authority is None or authority_check is None:
                raise GitHubFollowthroughError("github_read_window_invalid", status_code=403)
            read_window.before_get()
            outer_read_check = authority_check
            async def bounded_read_check():
                read_window.remaining()
                await outer_read_check()
                read_window.remaining()
            authority_check = bounded_read_check
            timeout_seconds = min(timeout_seconds, read_window.remaining())
        if method == "POST":
            if github_consent_binding is None or authority_check is None:
                raise GitHubFollowthroughError("github_consent_transport_binding_required", status_code=403)
            outer_authority = authority_check
            async def scoped_handoff():
                await outer_authority()
                binding = github_consent_binding
                action = mutation_action(binding["repository"], method, url, json_body)
                row = await self._get_connection_row(binding["owner_principal_id"])
                await require_consent(row, principal=binding["owner_principal_id"], root=binding["consent_root_id"],
                    repository=binding["repository"], revision=binding["connection_revision"],
                    required_actions={action}, binding=binding)
            authority_check = scoped_handoff
            await authority_check()
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
        if authority_check is not None:
            kwargs["authority_check"] = authority_check
        if self._resolver is not None:
            kwargs["resolver"] = self._resolver
        if self._transport is not None:
            kwargs["transport"] = self._transport
        if readback_authority is not None:
            from src.extensions.github_consent import GitHubReadbackAuthority, GitHubClosedReadbackAuthority
            if method != "GET" or type(readback_authority) not in {GitHubReadbackAuthority, GitHubClosedReadbackAuthority}:
                raise GitHubFollowthroughError("github_readback_authority_invalid", status_code=403)
            await readback_authority.validate()
        response = await request_pinned_https(f"{GITHUB_ORIGIN}{url}", **kwargs)
        if read_window is not None:
            read_window.after_get(response.content)
        if method == "POST" and authority_check is not None:
            await authority_check()
        if readback_authority is not None and response.status_code == 200:
            from src.extensions.github_consent import digest
            await readback_authority.validate()
            await authority_check()
            self._last_verified_get = (url, hashlib.sha256(response.content).hexdigest(),
                digest(json.loads(response.content)), digest(readback_authority.__dict__), _now())
        return response

    async def verified_get_receipt(self, *, read_authority, path, payload, effect_identity,
                                   publication_inputs=None, read_window=None, publication_boundary=None):
        """Mint only actual GET bytes matching canonical original effect intent."""
        from src.extensions.github_consent import GitHubVerifiedReadback, _GET_RECEIPT_SEAL, digest
        await read_authority.validate()
        # Compare decoded canonical JSON because transport whitespace is not
        # publication identity. The actual raw digest is separately retained.
        captured = self._last_verified_get
        self._last_verified_get = None
        if captured is None or captured[0] != path or captured[2] != digest(payload) or captured[3] != digest(read_authority.__dict__) or (_now()-captured[4]).total_seconds() > 30:
            raise GitHubFollowthroughError("github_actual_get_proof_missing", status_code=409)
        current = await durable_job_repository.get_job(read_authority.job_id)
        if not current or current.get("owner", {}).get("kind") != "user" or current.get("owner", {}).get("principal_id") != read_authority.principal or current.get("operator_session_id") != read_authority.root or current.get("job_kind") != read_authority.capability:
            raise GitHubFollowthroughError("github_get_original_job_changed", status_code=409)
        prior = next((item for item in current.get("effects", []) if item.get("effect_id") == effect_identity.get("effect_id")), None)
        expected_identity = {"job_id": current["job_id"], "attempt_count": current["attempt_count"], "authority_digest": current["authority_digest"]}
        if prior:
            expected_identity.update({key: prior.get(key) for key in ("effect_id", "effect_type", "target_path", "target_digest", "adapter_idempotency_key")})
        if not prior or expected_identity != effect_identity:
            raise GitHubFollowthroughError("github_get_original_effect_changed", status_code=409)
        if current["job_kind"] == JOB_KIND:
            if (current.get("declared_authority") or {}).get("capability_id") != CAPABILITY_ID:
                raise GitHubFollowthroughError("github_get_native_capability_mismatch", status_code=409)
            prepared = await self._read_prepared(current)
            remote_id = _positive_id(payload.get("number") if prepared.action == ACTION_CREATE_ISSUE else payload.get("id"), field="remote_id")
            matches = path == _canonical_path(prepared.repository, prepared.action, prepared.issue_number, remote_id)
            matches = matches and prior.get("effect_type") == "github_publication" and prior.get("target_path") == _publication_effect_target_path(prepared) and prior.get("target_digest") == prepared.body_sha256
            matches = matches and payload.get("body") == prepared.body
            if prepared.action == ACTION_CREATE_ISSUE:
                matches = matches and payload.get("title") == prepared.title
            else:
                matches = matches and payload.get("issue_url") == f"{GITHUB_ORIGIN}/repos/{prepared.repository}/issues/{prepared.issue_number}"
            if not matches:
                raise GitHubFollowthroughError("github_get_original_payload_mismatch", status_code=409)
        elif current["job_kind"] == "engineering.repo-publication.v1":
            from pathlib import Path
            from config.settings import settings
            from src.workspace import canonical_workspace_root
            from src.workflows.repo_publication import RepoPublicationService
            from src.execution.repo_publication import file_manifest, equivalent
            service = RepoPublicationService(adapter=self)
            preview = service.preview(current)
            if publication_inputs is not None:
                from src.workflows.repo_publication_closure import _PublicationClosureInputs
                from src.extensions.github_capacity_closure import ReadWindow
                if type(publication_inputs) is not _PublicationClosureInputs or type(read_window) is not ReadWindow:
                    raise GitHubFollowthroughError("github_publication_closure_inputs_invalid", status_code=409)
                publication_inputs.validate_intent(current, prior, path, payload, service, read_window, publication_boundary)
            else:
                original = {"title": preview["title"], "body": preview["body"], "head": preview["branch_name"], "base": preview["base_branch"], "draft": False}
                remote_commit = next((item.get("details", {}).get("remote_identity") for item in reversed(current.get("effects", [])) if item.get("effect_type") == "repo_publication_commit" and item.get("receipt_kind") == "readback" and item.get("status") == "succeeded"), None)
                if prior.get("effect_type") != "repo_publication_pr" or prior.get("target_path") != f"/repos/{preview['repository']}/pulls" or prior.get("target_digest") != digest(original) or path != f"/repos/{preview['repository']}/pulls/{service.positive(payload.get('number'))}" or not remote_commit:
                    raise GitHubFollowthroughError("github_get_original_payload_mismatch", status_code=409)
                equivalent(file_manifest(Path(canonical_workspace_root(settings.workspace_dir)) / f"artifacts/repo-publication/{current['job_id']}/producer"), preview["tested_input"]["tested_files"])
                service.verify_pr(payload, preview, remote_commit)
        else:
            raise GitHubFollowthroughError("github_get_native_capability_mismatch", status_code=409)
        snapshot = await read_authority.validate()
        from src.extensions.github_recovery import capture_binding, check_binding, check_closed_binding
        from src.extensions.github_consent import GitHubClosedReadbackAuthority
        async with db_engine.get_session() as db:
            run = (await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.run_identity == read_authority.job_id))).scalars().one()
            if type(read_authority) is GitHubClosedReadbackAuthority:
                binding = await check_closed_binding(db, run, read_authority, snapshot=snapshot)
            else:
                binding = await capture_binding(db, run, read_authority, snapshot)
                await check_binding(db, run, binding)
            if run.revision != current["revision"]:
                raise GitHubFollowthroughError("github_get_original_job_changed", status_code=409)
        from dataclasses import replace
        from src.extensions.github_consent import _seal_verified_get
        verified = GitHubVerifiedReadback(read_authority.job_id, read_authority.root,
            read_authority.capability, path, captured[1], digest(read_authority.__dict__),
            digest(effect_identity), captured[4], _GET_RECEIPT_SEAL, captured[2], binding)
        return replace(verified, _seal=_seal_verified_get(verified))

    async def request_repo_publication(
        self, path: str, *, method: str, token: str, authority_check,
        json_body: dict[str, Any] | None = None, timeout_seconds: float = 20,
        consent_binding: dict[str, Any] | None = None,
        owner_principal_id: str | None = None, owner_session_id: str | None = None,
        readback_authority=None,
        read_window=None,
    ) -> PinnedResponse:
        """Exact Git Data/PR operation through the existing protected adapter.

        The reviewed final-DNS authority callback is a required dependency;
        older adapters cannot silently execute under weaker authority.
        """
        import inspect
        if "authority_check" not in inspect.signature(self._request).parameters:
            raise GitHubFollowthroughError("publication_transport_authority_unavailable")
        if consent_binding is None:
            raise GitHubFollowthroughError("publication_transport_consent_unavailable")
        if method not in {"GET", "POST"} or not re.fullmatch(
            r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:git/(?:blobs(?:/[0-9a-f]{40})?|trees(?:/[0-9a-f]{40}(?:\?recursive=1)?)?|commits(?:/[0-9a-f]{40})?|refs|ref/heads/[A-Za-z0-9_./-]+)|pulls(?:/[1-9][0-9]*(?:/files\?per_page=100&page=(?:[1-9]|1[0-9]|2[01]))?|\?state=all&head=[A-Za-z0-9_.-]+%3A[A-Za-z0-9_.%/-]+&base=[A-Za-z0-9_.%/-]+&per_page=100)?)", path
        ):
            raise GitHubFollowthroughError("publication_request_invalid", status_code=422)
        async def scoped_authority():
            await authority_check()
            if method == "POST":
                repository = consent_binding["repository"]
                action = mutation_action(repository, method, path, json_body)
                row = await self._get_connection_row(owner_principal_id)
                await require_consent(row, principal=owner_principal_id, root=owner_session_id,
                    repository=repository, revision=consent_binding["connection_revision"],
                    required_actions={action}, binding=consent_binding)
        await scoped_authority()
        response = await self._request(path, method=method, token=token, json_body=json_body,
                                       timeout_seconds=timeout_seconds, authority_check=scoped_authority,
                                       github_consent_binding=consent_binding, readback_authority=readback_authority,
                                       read_window=read_window)
        await scoped_authority()
        return response

    async def get_connection(self, owner_principal_id: str, owner_session_id: str | None = None) -> dict[str, Any]:
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
        value = _connection_payload(row)
        value["consent"] = await consent_projection(row, owner_session_id)
        return value

    async def revoke_connection(self, *, owner_principal_id: str, expected_revision: int) -> dict[str, Any]:
        """Fence publication locally while retaining any contacted liability."""
        async with db_engine.get_session() as db:
            result = await db.execute(
                update(GitHubFollowthroughConnection).where(
                    GitHubFollowthroughConnection.owner_principal_id == owner_principal_id,
                    GitHubFollowthroughConnection.revision == expected_revision,
                ).values(mode=CONNECTION_MODE_DISABLED, consent_revoked_at=_now(),
                    revision=GitHubFollowthroughConnection.revision + 1, updated_at=_now())
            )
            if result.rowcount != 1:
                raise GitHubFollowthroughError("connection_revision_stale", status_code=409)
        return await self.get_connection(owner_principal_id)

    async def put_connection(
        self,
        *,
        owner_principal_id: str,
        repository: str,
        vault_key: str,
        mode: str,
        expected_revision: int,
        owner_session_id: str | None = None,
        consent: GitHubConsentRequest | None = None,
    ) -> dict[str, Any]:
        repository = _repository(repository)
        vault_key = _vault_key(vault_key)
        if mode not in CONNECTION_MODES:
            raise GitHubFollowthroughError("connection_mode_invalid", status_code=422)
        if (mode == CONNECTION_MODE_ACTIVE) != (consent is not None):
            raise GitHubFollowthroughError("github_connection_explicit_consent_required", status_code=422)
        operator = await live_operator(owner_principal_id, owner_session_id) if consent is not None else None
        snapshot = await vault_repository.snapshot(vault_key, owner_principal_id=owner_principal_id) if consent is not None else None
        if consent is not None and snapshot is None:
            raise GitHubFollowthroughError("credential_not_configured", status_code=409)
        if mode == CONNECTION_MODE_ACTIVE and not _text(owner_principal_id):
            raise GitHubFollowthroughError("owner_required", status_code=401)
        if mode != CONNECTION_MODE_DISABLED and not await vault_repository.exists(vault_key, owner_principal_id=owner_principal_id):
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
                if consent is not None:
                    for key, value in issuance(row, operator, consent, snapshot.binding_digest, 1).items():
                        setattr(row, key, value)
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
                consent_fields = {key: None for key in ("consent_id", "consent_owner_session_id", "consent_actions_json", "consent_issued_at", "consent_expires_at", "consent_connection_revision", "consent_payload_digest", "consent_revoked_at")}
                if consent is not None:
                    from types import SimpleNamespace
                    selected = SimpleNamespace(id=row.id, owner_principal_id=owner_principal_id, repository=repository)
                    consent_fields = issuance(selected, operator, consent, snapshot.binding_digest, expected_revision + 1)
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
                        **consent_fields,
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
        value = _connection_payload(row)
        value["consent"] = await consent_projection(row, owner_session_id)
        return value

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

    async def _discover_routine_publication_binding(
        self,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        client_key: uuid.UUID,
        job_id: str,
        connection: GitHubFollowthroughConnection,
        request: PrepareRequest,
        watch: GuardianSourceWatch,
        goal: Goal,
    ) -> dict[str, Any] | None:
        """Recover the server-only routine identity for one M3 publication.

        ``RoutineService`` admits a fenced publication child before it calls
        this adapter.  The child and its invocation parent are therefore the
        only trusted source for routine identity; no routine fields are
        accepted from the HTTP request.  A standalone M3 operation has no
        matching child and retains its existing contract.
        """

        operation_uuid = str(client_key)
        expected_job_id = f"ghfollow_{_operation_id(owner_principal_id, client_key).hex}"
        if job_id != expected_job_id:
            return None

        try:
            async with db_engine.get_session() as db:
                child_rows = (
                    await db.execute(
                        select(WorkflowRunState).where(
                            WorkflowRunState.job_kind == ROUTINE_PUBLICATION_CHILD_JOB_KIND,
                            WorkflowRunState.owner_kind == "user",
                            WorkflowRunState.owner_principal_id == owner_principal_id,
                            WorkflowRunState.session_id == owner_session_id,
                        )
                    )
                ).scalars().all()
                matches: list[tuple[WorkflowRunState, Mapping[str, Any], Mapping[str, Any]]] = []
                for child in child_rows:
                    child_authority = _load(child.declared_authority_json, {})
                    if not isinstance(child_authority, Mapping):
                        continue
                    if _text(child_authority.get("m3_job_id")) != job_id:
                        continue
                    child_inputs = _load(child.arguments_json, {})
                    if not isinstance(child_inputs, Mapping):
                        raise GitHubFollowthroughError("routine_publication_binding_invalid")
                    matches.append((child, child_authority, child_inputs))
                if not matches:
                    return None
                if len(matches) != 1:
                    raise GitHubFollowthroughError("routine_publication_binding_conflict")

                child, child_authority, child_inputs = matches[0]
                parent_job_id = _text(
                    child_authority.get("routine_invocation_job_id")
                    or child_authority.get("parent_job_id")
                    or child.parent_job_id
                )
                if (
                    not parent_job_id
                    or _text(child.parent_job_id) != parent_job_id
                    or _text(child_authority.get("step_id")) != "github_followthrough"
                    or _text(child_authority.get("m3_job_id")) != expected_job_id
                    or _text(child_authority.get("publication_operation_uuid")) != operation_uuid
                ):
                    raise GitHubFollowthroughError("routine_publication_binding_invalid")

                parent = (
                    await db.execute(
                        select(WorkflowRunState).where(
                            WorkflowRunState.run_identity == parent_job_id,
                            WorkflowRunState.job_kind == "routine_invocation",
                            WorkflowRunState.owner_kind == "user",
                            WorkflowRunState.owner_principal_id == owner_principal_id,
                            WorkflowRunState.session_id == owner_session_id,
                        )
                    )
                ).scalars().first()
                if parent is None:
                    raise GitHubFollowthroughError("routine_publication_binding_invalid")
                parent_authority = _load(parent.declared_authority_json, {})
                if not isinstance(parent_authority, Mapping):
                    raise GitHubFollowthroughError("routine_publication_binding_invalid")

                invocation_uuid = _text(
                    child_inputs.get("invocation_uuid")
                    or parent_authority.get("invocation_uuid")
                )
                try:
                    invocation_uuid = str(uuid.UUID(invocation_uuid))
                except (TypeError, ValueError, AttributeError) as exc:
                    raise GitHubFollowthroughError("routine_publication_binding_invalid") from exc
                if (
                    _routine_child_job_id(invocation_uuid) != _text(child.run_identity)
                    or _text(parent_authority.get("invocation_uuid")) not in {"", invocation_uuid}
                    or _text(child_inputs.get("routine_invocation_job_id")) not in {"", parent_job_id}
                ):
                    raise GitHubFollowthroughError("routine_publication_binding_invalid")

                routine_id = _text(child_authority.get("routine_id"))
                parent_routine_id = _text(parent_authority.get("routine_id"))
                if not routine_id or parent_routine_id != routine_id:
                    raise GitHubFollowthroughError("routine_publication_binding_invalid")
                try:
                    routine_revision = int(child_authority.get("routine_revision"))
                    parent_routine_revision = int(parent_authority.get("routine_revision"))
                    routine_version = int(child_authority.get("routine_version"))
                    parent_routine_version = int(parent_authority.get("routine_version"))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise GitHubFollowthroughError("routine_publication_binding_invalid") from exc
                package_digest = _text(parent_authority.get("package_digest"))
                if (
                    routine_revision < 1
                    or routine_version < 1
                    or routine_revision != parent_routine_revision
                    or routine_version != parent_routine_version
                    or len(package_digest) != 64
                    or re.fullmatch(r"[0-9a-f]{64}", package_digest) is None
                ):
                    raise GitHubFollowthroughError("routine_publication_binding_invalid")

                source_watch_id = _text(parent_authority.get("source_watch_id"))
                connection_id = _text(parent_authority.get("github_connection_id"))
                repository = _text(parent_authority.get("github_repository"))
                action = _text(parent_authority.get("github_action"))
                try:
                    source_watch_revision = int(parent_authority.get("source_watch_revision"))
                    connection_revision = int(parent_authority.get("github_connection_revision"))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise GitHubFollowthroughError("routine_publication_binding_invalid") from exc
                if (
                    source_watch_id != _text(watch.id)
                    or source_watch_revision < 1
                    or connection_id != _text(connection.id)
                    or connection_revision != int(connection.revision)
                    or repository != _text(connection.repository)
                    or action != _text(request.action)
                    or parent.goal_id != goal.id
                    or int(parent.goal_revision or 0) != int(goal.revision or 0)
                    or int(parent.goal_revision or 0) != int(request.goal_revision)
                    or _text(parent.operator_session_id or parent.session_id) != owner_session_id
                ):
                    raise GitHubFollowthroughError("routine_publication_binding_invalid")

                return {
                    "routine_id": routine_id,
                    "routine_revision": routine_revision,
                    "routine_version": routine_version,
                    "package_digest": package_digest,
                    "parent_invocation_job_id": parent_job_id,
                    "publication_child_job_id": _text(child.run_identity),
                    "invocation_uuid": invocation_uuid,
                    "owner_principal_id": owner_principal_id,
                    "owner_session_id": owner_session_id,
                    "goal_id": _text(goal.id),
                    "goal_revision": int(goal.revision),
                    "source_watch_id": source_watch_id,
                    "connection_id": connection_id,
                    "connection_revision": connection_revision,
                    "repository": repository,
                    "action": action,
                    "operation_uuid": operation_uuid,
                }
        except GitHubFollowthroughError:
            raise
        except Exception as exc:
            raise GitHubFollowthroughError("routine_binding_unavailable", status_code=503) from exc

    async def _current_routine_publication_binding(
        self,
        current: Mapping[str, Any],
        *,
        owner_principal_id: str,
        owner_session_id: str,
        return_admission_guard: bool = False,
    ) -> dict[str, Any] | tuple[dict[str, Any], DurableJobRoutinePublicationAdmissionGuard] | None:
        """Require the exact live routine/package binding before M3 writes."""

        authority = current.get("declared_authority")
        authority = authority if isinstance(authority, Mapping) else {}
        binding = authority.get("routine_binding")
        if binding is None:
            return None
        if not isinstance(binding, Mapping) or set(binding) != set(ROUTINE_BINDING_KEYS):
            raise GitHubFollowthroughError("routine_binding_invalid")
        binding = dict(binding)
        if (
            _text(binding.get("owner_principal_id")) != owner_principal_id
            or _text(binding.get("owner_session_id")) != owner_session_id
        ):
            raise GitHubFollowthroughError("routine_binding_invalid")
        try:
            routine_revision = int(binding["routine_revision"])
            routine_version = int(binding["routine_version"])
            goal_revision = int(binding["goal_revision"])
            connection_revision = int(binding["connection_revision"])
            operation_uuid = uuid.UUID(str(binding["operation_uuid"]))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise GitHubFollowthroughError("routine_binding_invalid") from exc
        if routine_revision < 1 or routine_version < 1 or goal_revision < 1 or connection_revision < 1:
            raise GitHubFollowthroughError("routine_binding_invalid")
        if (
            _text(binding.get("parent_invocation_job_id")) == ""
            or _text(binding.get("publication_child_job_id")) == ""
            or _text(binding.get("routine_id")) == ""
            or _text(binding.get("package_digest")) == ""
            or _text(binding.get("goal_id")) == ""
            or _text(binding.get("source_watch_id")) == ""
            or _text(binding.get("connection_id")) == ""
            or _text(binding.get("repository")) == ""
            or _text(binding.get("action")) not in ACTIONS
            or len(_text(binding.get("package_digest"))) != 64
            or re.fullmatch(r"[0-9a-f]{64}", _text(binding.get("package_digest"))) is None
        ):
            raise GitHubFollowthroughError("routine_binding_invalid")
        expected_job_id = f"ghfollow_{_operation_id(owner_principal_id, operation_uuid).hex}"
        if _text(current.get("job_id")) != expected_job_id:
            raise GitHubFollowthroughError("routine_binding_invalid")
        if _routine_child_job_id(_text(binding["invocation_uuid"])) != _text(
            binding["publication_child_job_id"]
        ):
            raise GitHubFollowthroughError("routine_binding_invalid")

        # M3 is deliberately a separate durable job from the routine parent
        # and wrapper child.  The persisted binding alone must not let a board
        # caller execute an approved M3 after the invocation tree was
        # cancelled or its parent fence was replaced.  Re-read both rows on
        # every execute boundary and require the same live parent fence that
        # the wrapper child recorded when it was admitted.
        try:
            parent = await durable_job_repository.get_job(
                _text(binding["parent_invocation_job_id"])
            )
            child = await durable_job_repository.get_job(
                _text(binding["publication_child_job_id"])
            )
        except Exception as exc:
            raise GitHubFollowthroughError(
                "routine_parent_child_unavailable", status_code=503
            ) from exc
        parent_authority = (
            parent.get("declared_authority")
            if isinstance(parent, Mapping)
            and isinstance(parent.get("declared_authority"), Mapping)
            else {}
        )
        child_authority = (
            child.get("declared_authority")
            if isinstance(child, Mapping)
            and isinstance(child.get("declared_authority"), Mapping)
            else {}
        )
        parent_owner = parent.get("owner") if isinstance(parent, Mapping) and isinstance(parent.get("owner"), Mapping) else {}
        child_owner = child.get("owner") if isinstance(child, Mapping) and isinstance(child.get("owner"), Mapping) else {}
        parent_lease = parent.get("lease") if isinstance(parent, Mapping) and isinstance(parent.get("lease"), Mapping) else {}
        child_lease = child.get("lease") if isinstance(child, Mapping) and isinstance(child.get("lease"), Mapping) else {}
        try:
            parent_fence = int(parent_lease.get("fencing_token") or 0)
            child_parent_fence = int(child.get("parent_fencing_token") or 0) if isinstance(child, Mapping) else 0
            parent_routine_revision = int(parent_authority.get("routine_revision") or 0)
            parent_routine_version = int(parent_authority.get("routine_version") or 0)
            parent_goal_revision = int(parent.get("goal_revision") or 0) if isinstance(parent, Mapping) else 0
            child_goal_revision = int(child.get("goal_revision") or 0) if isinstance(child, Mapping) else 0
            child_parent_authority_fence = int(child_authority.get("parent_fencing_token") or 0)
            parent_connection_revision = int(parent_authority.get("github_connection_revision") or 0)
            child_connection_revision = int(child_authority.get("github_connection_revision") or 0)
            child_routine_revision = int(child_authority.get("routine_revision") or 0)
            child_routine_version = int(child_authority.get("routine_version") or 0)
            child_lease_fence = int(child_lease.get("fencing_token") or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise GitHubFollowthroughError("routine_parent_child_binding_invalid") from exc

        parent_status = _text(parent.get("status")) if isinstance(parent, Mapping) else ""
        child_status = _text(child.get("status")) if isinstance(child, Mapping) else ""
        parent_lease_owner = _text(parent_lease.get("owner"))
        child_lease_owner = _text(child_lease.get("owner"))
        child_lease_expiry = _text(child_lease.get("expires_at"))

        def _live_lease_expiry(lease: Mapping[str, Any]) -> bool:
            raw_expiry = lease.get("expires_at")
            if isinstance(raw_expiry, datetime):
                expiry = raw_expiry
            else:
                try:
                    expiry = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
                except (TypeError, ValueError, AttributeError):
                    return False
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            return expiry > _now()

        parent_lease_live = bool(parent_lease_owner) and _live_lease_expiry(parent_lease)
        child_running = (
            child_status == "running"
            and bool(child_lease_owner)
            and child_lease_fence > 0
            and _live_lease_expiry(child_lease)
        )
        # Approval is held in the canonical routine representation by settling
        # the wrapper child.  Its parent remains running, while the child is
        # blocked with no live lease and its persisted parent fence is rebound
        # to the current parent lease.  Keep this exact pair executable so a
        # resumed M3 can consume the already reviewed approval; a blocked child
        # with a lease or a stale parent fence remains fail-closed.
        child_approval_hold = (
            child_status == "blocked"
            and not child_lease_owner
            and not child_lease_expiry
            and child_parent_fence > 0
            and child_parent_fence == parent_fence
        )
        approval_id = _text(authority.get("approval_id"))
        child_approval_checkpoint: Mapping[str, Any] | None = None
        child_approval_checkpoint_id = ""
        if isinstance(child, Mapping):
            for item in reversed(child.get("checkpoints") or []):
                if not isinstance(item, Mapping) or item.get("checkpoint_id") not in {
                    "routine-child:adoption_pending",
                    "routine-child:prepared",
                }:
                    continue
                payload = item.get("payload")
                child_approval_checkpoint = payload if isinstance(payload, Mapping) else item
                child_approval_checkpoint_id = _text(item.get("checkpoint_id"))
                break
        if return_admission_guard:
            # Initial M3 admission happens after RoutineService has recorded
            # adoption_pending but before an approval exists.  The durable
            # admission guard binds that checkpoint to this deterministic M3;
            # execute/resume keeps the stricter prepared+approval requirement.
            approval_identity_ok = (
                child_approval_checkpoint_id
                in {"routine-child:adoption_pending", "routine-child:prepared"}
                and child_approval_checkpoint is not None
                and _text(child_approval_checkpoint.get("m3_job_id"))
                == _text(current.get("job_id"))
            )
        else:
            approval_identity_ok = bool(approval_id) and (
                child_approval_checkpoint_id == "routine-child:prepared"
                and child_approval_checkpoint is not None
                and _text(child_approval_checkpoint.get("m3_job_id"))
                == _text(current.get("job_id"))
                and _text(child_approval_checkpoint.get("approval_id")) == approval_id
            )
        if (
            not isinstance(parent, Mapping)
            or not isinstance(child, Mapping)
            or _text(parent.get("job_id") or parent.get("run_identity"))
            != _text(binding["parent_invocation_job_id"])
            or _text(child.get("job_id") or child.get("run_identity"))
            != _text(binding["publication_child_job_id"])
            or _text(parent.get("job_kind")) != "routine_invocation"
            or _text(child.get("job_kind")) != ROUTINE_PUBLICATION_CHILD_JOB_KIND
            or parent_status != "running"
            or not parent_lease_live
            or not (child_running or child_approval_hold)
            or _text(parent_owner.get("kind")) not in {"", "user"}
            or _text(parent_owner.get("principal_id")) != owner_principal_id
            or _text(child_owner.get("kind")) not in {"", "user"}
            or _text(child_owner.get("principal_id")) != owner_principal_id
            or _text(parent.get("operator_session_id") or parent.get("session_id")) != owner_session_id
            or _text(child.get("operator_session_id") or child.get("session_id")) != owner_session_id
            or parent_fence <= 0
            or child_parent_fence <= 0
            or child_parent_fence != parent_fence
            or not approval_identity_ok
            or _text(child.get("parent_job_id")) != _text(binding["parent_invocation_job_id"])
            or _text(child_authority.get("parent_job_id")) != _text(binding["parent_invocation_job_id"])
            or _text(child_authority.get("routine_invocation_job_id"))
            != _text(binding["parent_invocation_job_id"])
            or _text(child_authority.get("step_id")) != "github_followthrough"
            or _text(child_authority.get("m3_job_id")) != _text(current.get("job_id"))
            or _text(child_authority.get("publication_operation_uuid"))
            != _text(binding["operation_uuid"])
            or child_parent_authority_fence != parent_fence
            or _text(parent_authority.get("routine_id")) != _text(binding["routine_id"])
            or parent_routine_revision != routine_revision
            or parent_routine_version != routine_version
            or _text(parent_authority.get("package_digest")) != _text(binding["package_digest"])
            or _text(parent_authority.get("invocation_uuid")) != _text(binding["invocation_uuid"])
            or _text(parent_authority.get("source_watch_id")) != _text(binding["source_watch_id"])
            or _text(parent_authority.get("github_connection_id")) != _text(binding["connection_id"])
            or parent_connection_revision != connection_revision
            or _text(parent_authority.get("github_repository")) != _text(binding["repository"])
            or _text(parent_authority.get("github_action")) != _text(binding["action"])
            or _text(child_authority.get("routine_id")) != _text(binding["routine_id"])
            or child_routine_revision != routine_revision
            or child_routine_version != routine_version
            or _text(child_authority.get("package_digest")) != _text(binding["package_digest"])
            or _text(child_authority.get("invocation_uuid")) != _text(binding["invocation_uuid"])
            or _text(child_authority.get("source_watch_id")) != _text(binding["source_watch_id"])
            or _text(child_authority.get("github_connection_id")) != _text(binding["connection_id"])
            or child_connection_revision != connection_revision
            or _text(child_authority.get("github_repository")) != _text(binding["repository"])
            or _text(child_authority.get("github_action")) != _text(binding["action"])
            or _text(parent.get("goal_id")) != _text(binding["goal_id"])
            or parent_goal_revision != goal_revision
            or _text(child.get("goal_id")) != _text(binding["goal_id"])
            or child_goal_revision != goal_revision
        ):
            raise GitHubFollowthroughError("routine_parent_child_not_current")

        try:
            async with db_engine.get_session() as db:
                routine = (
                    await db.execute(
                        select(GuardianRoutine).where(
                            GuardianRoutine.id == _text(binding["routine_id"]),
                            GuardianRoutine.owner_principal_id == owner_principal_id,
                            GuardianRoutine.owner_session_id == owner_session_id,
                        )
                    )
                ).scalars().first()
                version = (
                    await db.execute(
                        select(GuardianRoutineVersion).where(
                            GuardianRoutineVersion.routine_id == _text(binding["routine_id"]),
                            GuardianRoutineVersion.version == routine_version,
                        )
                    )
                ).scalars().first()
        except GitHubFollowthroughError:
            raise
        except Exception as exc:
            raise GitHubFollowthroughError("routine_binding_unavailable", status_code=503) from exc

        if routine is None or version is None:
            raise GitHubFollowthroughError("package_review_required")
        routine_state = _text(routine.state)
        if routine_state == "revoked":
            raise GitHubFollowthroughError("routine_revoked_terminal")
        if routine_state != "active" or int(routine.revision or 0) != routine_revision:
            raise GitHubFollowthroughError("routine_not_active_or_stale")
        if int(routine.current_version or 0) != routine_version:
            raise GitHubFollowthroughError("routine_not_active_or_stale")
        package_digest = _text(binding["package_digest"])
        if _text(version.installed_package_digest) != package_digest:
            raise GitHubFollowthroughError("package_review_required")

        pack_id = _routine_pack_id(_text(binding["routine_id"]), routine_version)
        try:
            lifecycle = CapabilityPackLifecycle().status(
                pack_id,
                owner_principal_id=owner_principal_id,
                session_id=owner_session_id,
            )
        except Exception as exc:
            raise GitHubFollowthroughError("package_review_required") from exc
        active = lifecycle.get("active") if isinstance(lifecycle, Mapping) else None
        expected_pack_version = f"1.0.{routine_version}"
        if (
            not isinstance(active, Mapping)
            or _text(active.get("pack_id")) != pack_id
            or _text(active.get("status")) != "active"
            or _text(active.get("version")) != expected_pack_version
            or _text(active.get("digest")) != package_digest
            or package_digest in {
                _text(item)
                for item in (lifecycle.get("revoked_digests", []) if isinstance(lifecycle, Mapping) else [])
            }
        ):
            raise GitHubFollowthroughError("package_review_required")
        if return_admission_guard:
            return (
                binding,
                DurableJobRoutinePublicationAdmissionGuard(
                    routine_parent_job_id=str(binding["parent_invocation_job_id"]),
                    routine_parent_fencing_token=parent_fence,
                    publication_child_job_id=str(binding["publication_child_job_id"]),
                    publication_child_fencing_token=child_lease_fence,
                    publication_child_parent_fencing_token=child_parent_fence,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                ),
            )
        return binding

    async def _routine_publication_admission_guard(
        self,
        binding: Mapping[str, Any],
        *,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> DurableJobRoutinePublicationAdmissionGuard:
        """Capture the current wrapper fences for the final M3 CAS.

        ``_current_routine_publication_binding`` is an intentionally separate
        package/readback preflight.  The durable repository repeats the
        parent/child checks under one admission transaction, using these
        fences to reject a cancellation that wins after preflight.
        """

        try:
            parent = await durable_job_repository.get_job(
                str(binding["parent_invocation_job_id"])
            )
            child = await durable_job_repository.get_job(
                str(binding["publication_child_job_id"])
            )
            parent_lease = parent.get("lease") if isinstance(parent, Mapping) else {}
            child_lease = child.get("lease") if isinstance(child, Mapping) else {}
            parent_fence = int(parent_lease.get("fencing_token") or 0)
            child_fence = int(child_lease.get("fencing_token") or 0)
            child_parent_fence = int(child.get("parent_fencing_token") or 0) if isinstance(child, Mapping) else 0
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise GitHubFollowthroughError("routine_parent_child_binding_invalid") from exc
        if (
            not isinstance(parent, Mapping)
            or not isinstance(child, Mapping)
            or parent_fence <= 0
            or child_fence <= 0
            or child_parent_fence <= 0
            or parent_fence != child_parent_fence
            or _text(parent.get("job_id") or parent.get("run_identity"))
            != _text(binding.get("parent_invocation_job_id"))
            or _text(child.get("job_id") or child.get("run_identity"))
            != _text(binding.get("publication_child_job_id"))
        ):
            raise GitHubFollowthroughError("routine_parent_child_not_current")
        return DurableJobRoutinePublicationAdmissionGuard(
            routine_parent_job_id=str(binding["parent_invocation_job_id"]),
            routine_parent_fencing_token=parent_fence,
            publication_child_job_id=str(binding["publication_child_job_id"]),
            publication_child_fencing_token=child_fence,
            publication_child_parent_fencing_token=child_parent_fence,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )

    async def _close_invalid_routine_publication(
        self,
        current: Mapping[str, Any],
        *,
        prepared: PreparedPublication,
        reason: str,
        known_no_dispatch: bool = False,
        target_path: str = "github:routine-binding",
        owner: str | None = None,
        fence: int | None = None,
    ) -> dict[str, Any]:
        """Close a revoked routine without erasing an external-effect liability."""

        latest = await durable_job_repository.get_job(prepared.job_id) or dict(current)
        effects = [item for item in latest.get("effects") or [] if isinstance(item, Mapping)]
        unresolved = next(
            (
                item
                for item in reversed(effects)
                if _text(item.get("effect_type")) == "github_publication"
                and _text(item.get("status")) in UNRESOLVED_EFFECT_STATUSES
            ),
            None,
        )
        lease = latest.get("lease") if isinstance(latest.get("lease"), Mapping) else {}
        leased_owner = _text(owner or lease.get("owner")) or None
        leased_fence = int(fence if fence is not None else (lease.get("fencing_token") or 0)) or None
        if unresolved is not None:
            if (
                known_no_dispatch
                and _text(unresolved.get("status")) == "intent"
                and latest.get("status") == "running"
                and leased_owner
                and leased_fence
            ):
                return await self._mark_no_dispatch(
                    prepared,
                    reason=reason,
                    current=latest,
                    owner=leased_owner,
                    fence=leased_fence,
                    target_path=_text(unresolved.get("target_path")) or target_path,
                )
            if latest.get("status") == "running" and leased_owner and leased_fence:
                return await self._mark_unknown(
                    prepared,
                    reason=reason,
                    current=latest,
                    owner=leased_owner,
                    fence=leased_fence,
                    readback_observation=_text(unresolved.get("status")) == "dispatched",
                    target_path=_text(unresolved.get("target_path")) or target_path,
                )
            return latest

        status = _text(latest.get("status"))
        approval_cleanup = "not_bound"
        try:
            if status in {"accepted", "queued", "awaiting_approval"}:
                # A pending approval is a one-shot operator decision.  If its
                # routine package became invalid before resume, invalidate the
                # exact still-pending row as a companion to the durable job
                # cancellation. A consumed/approved row is left intact as
                # historical evidence; it cannot authorize a cancelled job.
                if status == "awaiting_approval":
                    approval_cleanup = await self._deny_pending_approval_for_cancelled_job(
                        latest,
                        owner_principal_id=prepared.owner_principal_id,
                        owner_session_id=prepared.owner_session_id,
                        job_id=prepared.job_id,
                    )
                cancelled = await durable_job_repository.cancel_job(
                    prepared.job_id,
                    expected_revision=latest.get("revision"),
                    reason=reason,
                )
                if approval_cleanup not in _SAFE_APPROVAL_CLEANUP_OUTCOMES:
                    return await self._cancel_cleanup_response(
                        cancelled,
                        cleanup_status=approval_cleanup,
                    )
                return cancelled
            if status == "running" and leased_owner and leased_fence:
                return await durable_job_repository.transition_job(
                    prepared.job_id,
                    "blocked",
                    owner=leased_owner,
                    fencing_token=leased_fence,
                    expected_revision=latest.get("revision"),
                    reason=reason,
                    result={"recovery_action": "restore_prerequisite", "learning": "no_learning"},
                    result_summary="routine package binding is no longer active; restore it before retry",
                )
        except DurableJobError:
            latest_after = await durable_job_repository.get_job(prepared.job_id) or latest
            if approval_cleanup not in _SAFE_APPROVAL_CLEANUP_OUTCOMES:
                return await self._cancel_cleanup_response(
                    latest_after,
                    cleanup_status=approval_cleanup,
                )
            return latest_after
        return latest

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

    async def _assert_prepare_current(
        self,
        current: Mapping[str, Any],
        *,
        owner_principal_id: str,
        owner_session_id: str,
        routine_binding: Mapping[str, Any] | None,
        phase: str,
    ) -> dict[str, Any]:
        """Re-read the leased M3 row before each prepare side effect.

        Preparation runs after admission but before the approval hold.  A
        routine pause/revoke can therefore win between any two durable CAS
        operations.  The latest row and its exact lease remain authoritative;
        a changed owner/fence is never adopted by this worker.
        """

        job_id = _text(current.get("job_id") or current.get("run_identity"))
        latest = await durable_job_repository.get_job(job_id) if job_id else None
        if not isinstance(latest, Mapping) or _text(latest.get("job_id") or latest.get("run_identity")) != job_id:
            raise GitHubFollowthroughError(f"prepare_{phase}_job_missing")
        if _text(latest.get("status")) != "running":
            raise GitHubFollowthroughError(f"prepare_{phase}_job_not_current")
        previous_lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
        latest_lease = latest.get("lease") if isinstance(latest.get("lease"), Mapping) else {}
        previous_owner = _text(previous_lease.get("owner"))
        previous_fence = int(previous_lease.get("fencing_token") or 0)
        latest_owner = _text(latest_lease.get("owner"))
        latest_fence = int(latest_lease.get("fencing_token") or 0)
        if (
            not latest_owner
            or latest_fence <= 0
            or (previous_owner and latest_owner != previous_owner)
            or (previous_fence and latest_fence != previous_fence)
        ):
            raise GitHubFollowthroughError(f"prepare_{phase}_lease_not_current")
        if routine_binding is not None:
            live_binding = await self._current_routine_publication_binding(
                latest,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                # The wrapper child is still adoption_pending until this
                # prepare call creates and binds the approval.  The admission
                # form validates the exact parent/child fence without
                # requiring an approval id that does not exist yet.
                return_admission_guard=True,
            )
            if isinstance(live_binding, tuple) and len(live_binding) == 2:
                live_binding = live_binding[0]
            if not isinstance(live_binding, Mapping) or dict(live_binding) != dict(routine_binding):
                raise GitHubFollowthroughError("routine_binding_invalid")
        return dict(latest)

    async def _cleanup_uncommitted_payload_file(self, *, job_id: str, path: str) -> None:
        """Remove a private payload only while no durable artifact owns it."""

        try:
            latest = await durable_job_repository.get_job(job_id)
        except Exception:
            # An unreadable durable projection cannot prove that artifact
            # admission did not occur.  Preserve the private file for the
            # operator's existing recovery/reconciliation path.
            return
        if not isinstance(latest, Mapping):
            # A successful but non-canonical response is not proof that this
            # path is unowned.  Cleanup is deliberately fail closed.
            return
        for artifact in latest.get("artifacts") or []:
            if isinstance(artifact, Mapping) and _text(artifact.get("file_path")) == _text(path):
                # The artifact receipt is canonical even if a later
                # checkpoint CAS lost.  Never delete a referenced file.
                return
        try:
            _safe_resolve(path).unlink(missing_ok=True)
        except (OSError, ValueError):
            # Cleanup is best effort; the durable row remains the recovery
            # authority and no provider request is made from this path.
            return

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
            closed = (await db.execute(select(WorkflowRunState.github_capacity_closure_json).where(
                WorkflowRunState.run_identity == job_id))).scalar_one_or_none()
            if closed:
                raise GitHubFollowthroughError("github_capacity_already_closed", status_code=409)
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
                    ~select(WorkflowRunState.id).where(WorkflowRunState.run_identity == job_id,
                        WorkflowRunState.github_capacity_closure_json.is_not(None)).exists(),
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
            closed = (await db.execute(select(WorkflowRunState.github_capacity_closure_json).where(
                WorkflowRunState.run_identity == job_id))).scalar_one_or_none()
            if closed:
                return False
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
            closed = (await db.execute(select(WorkflowRunState.github_capacity_closure_json).where(
                WorkflowRunState.run_identity == job_id))).scalar_one_or_none()
            if closed:
                raise GitHubFollowthroughError("github_capacity_already_closed", status_code=409)
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
        repair_connection: bool = True,
    ) -> dict[str, Any]:
        connection_release_status = "not_required"
        if repair_connection and current.get("status") == "succeeded":
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
            "revision": current.get("revision"),
            "github_capacity_closure": current.get("github_capacity_closure"),
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
        work_board_idempotency_key: str | None = None,
        work_board_task_id: str | None = None,
        work_board_parent_handoff_context: list[dict[str, Any]] | None = None,
        work_board_parent_handoff_digest: str | None = None,
        request: PrepareRequest,
    ) -> dict[str, Any]:
        if work_board_idempotency_key is not None and not str(work_board_idempotency_key).strip():
            raise GitHubFollowthroughError("work_board_binding_invalid", status_code=409)
        parent_handoff_context = list(work_board_parent_handoff_context or [])
        parent_handoff_digest = _text(work_board_parent_handoff_digest)
        if parent_handoff_context:
            encoded_handoffs = _dump(parent_handoff_context)
            if (
                work_board_idempotency_key is None
                or not work_board_task_id
                or len(encoded_handoffs.encode("utf-8")) > 32_768
                or _sha(encoded_handoffs) != parent_handoff_digest
                or any(
                    not isinstance(item, dict)
                    or item.get("status") != "verified"
                    or item.get("child_task_id") != work_board_task_id
                    for item in parent_handoff_context
                )
            ):
                raise GitHubFollowthroughError("work_board_handoff_binding_invalid", status_code=409)
        elif parent_handoff_digest:
            raise GitHubFollowthroughError("work_board_handoff_binding_invalid", status_code=409)
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
        if not connection.vault_key or not await vault_repository.exists(connection.vault_key, owner_principal_id=owner_principal_id):
            raise GitHubFollowthroughError("credential_not_configured", status_code=409)
        consent_binding = await require_consent(connection, principal=owner_principal_id, root=owner_session_id,
            repository=connection.repository, revision=request.connection_revision,
            required_actions={"github_issue_write" if request.action == ACTION_CREATE_ISSUE else "github_comment_write"})
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
        routine_binding = await self._discover_routine_publication_binding(
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            client_key=client_key,
            job_id=f"ghfollow_{operation_id.hex}",
            connection=connection,
            request=request,
            watch=watch,
            goal=goal,
        )
        if routine_binding is not None:
            input_fields["routine_binding"] = dict(routine_binding)
        if parent_handoff_context:
            # The supplied context is already verified against this digest.
            # Bind only the digest into the durable input receipt so raw
            # handoff summaries stay out of the durable job projection.
            input_fields["parent_handoff_digest"] = parent_handoff_digest
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
            # Accepted/queued rows are the crash window between durable
            # admission and preparation.  Fall through to the canonical
            # admission/preparation path so the same binding is queued,
            # claimed, and receives its existing approval record.  Every
            # later state is returned as a read-only durable projection.
            if str(existing.get("status") or "") not in {"accepted", "queued"}:
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
            "github_consent": consent_binding,
        }
        if routine_binding is not None:
            authority["routine_binding"] = dict(routine_binding)
        if parent_handoff_context:
            authority["parent_handoff_digest"] = parent_handoff_digest
        # The routine child is admitted before this M3 prepare call.  Re-read
        # the canonical routine/package rows immediately before admitting the
        # publication itself so a revoke/quarantine racing preparation cannot
        # leave a stale provider job behind.  Standalone M3 has no binding and
        # keeps its existing path.
        routine_publication_admission_guard = None
        if routine_binding is not None:
            live_binding = await self._current_routine_publication_binding(
                {
                    "job_id": job_id,
                    "declared_authority": {"routine_binding": routine_binding},
                },
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                return_admission_guard=True,
            )
            if (
                isinstance(live_binding, tuple)
                and len(live_binding) == 2
                and isinstance(live_binding[0], Mapping)
                and isinstance(live_binding[1], DurableJobRoutinePublicationAdmissionGuard)
            ):
                if dict(live_binding[0]) != routine_binding:
                    raise GitHubFollowthroughError("routine_binding_invalid")
                routine_publication_admission_guard = live_binding[1]
            else:
                # Keep test doubles and older internal adapters fail-closed:
                # a binding without the exact preflight fences must make one
                # final owner/row read before admission.
                routine_publication_admission_guard = await self._routine_publication_admission_guard(
                    routine_binding,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                )
        admitted = await durable_job_repository.admit_job(
            DurableJobSpec(
                identity=DurableJobIdentity(
                    job_id=job_id,
                    owner_kind="user",
                    owner_principal_id=owner_principal_id,
                    job_kind=JOB_KIND,
                    capability_version=CAPABILITY_VERSION,
                    idempotency_scope=("work-board-attempt" if work_board_idempotency_key else "github-followthrough"),
                    idempotency_key=(str(work_board_idempotency_key) if work_board_idempotency_key else str(client_key)),
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
                # The first claim prepares and pauses at the independent
                # publication approval gate; the approved effect/readback is
                # a second explicit claim, not an automatic retry.
                max_attempts=2,
                priority=50,
                run_fingerprint=input_digest,
                budget_microusd=0,
                routine_publication_admission_guard=routine_publication_admission_guard,
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
        current = await self._assert_prepare_current(
            current,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            routine_binding=routine_binding,
            phase="payload",
        )
        owner = str((current.get("lease") or {}).get("owner") or owner)
        fence = int((current.get("lease") or {}).get("fencing_token") or fence)
        try:
            _write_workspace_text_bounded(_safe_resolve(path), payload_text, max_bytes=MAX_ARTIFACT_BYTES)
            stored, truncated = _read_workspace_text_bounded(_safe_resolve(path), max_bytes=MAX_ARTIFACT_BYTES)
        except (OSError, ValueError) as exc:
            await self._cleanup_uncommitted_payload_file(job_id=job_id, path=path)
            try:
                await durable_job_repository.transition_job(
                    job_id,
                    "failed",
                    owner=owner,
                    fencing_token=fence,
                    expected_revision=current.get("revision"),
                    reason="input_artifact_write_failed",
                )
            except DurableJobError:
                pass
            raise GitHubFollowthroughError("input_artifact_write_failed", status_code=500) from exc
        if truncated or stored != payload_text:
            await self._cleanup_uncommitted_payload_file(job_id=job_id, path=path)
            raise GitHubFollowthroughError("input_artifact_readback_failed", status_code=500)
        prepared = replace_dataclass(
            prepared,
            payload_path=path,
            payload_sha256=_sha(payload_text),
        )
        try:
            current = await self._assert_prepare_current(
                current,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                routine_binding=routine_binding,
                phase="artifact",
            )
            owner = str((current.get("lease") or {}).get("owner") or owner)
            fence = int((current.get("lease") or {}).get("fencing_token") or fence)
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
            current = await self._assert_prepare_current(
                current,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                routine_binding=routine_binding,
                phase="approval",
            )
        except Exception:
            await self._cleanup_uncommitted_payload_file(job_id=job_id, path=path)
            raise
        approval_scope = _followthrough_approval_scope(
            operation_id=str(operation_id),
            job_id=job_id,
            connection_id=connection.id,
            connection_revision=int(connection.revision),
            repository=connection.repository,
            action=request.action,
            issue_number=issue_number,
            title_sha256=_sha(title or ""),
            body_sha256=_sha(body),
            source_watch_id=watch.id,
            plan_revision=int(watch.plan_revision),
            goal_id=goal.id,
            goal_revision=int(goal.revision),
            dossier_artifact_id=request.dossier_artifact_id,
            dossier_sha256=request.dossier_sha256,
        )
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
            approval_context=approval_scope,
        )
        approval = await approval_repository.get_or_create_pending(
            session_id=owner_session_id,
            tool_name="github:followthrough",
            risk_level="high",
            summary=f"Publish an approved GitHub {request.action} for goal {goal.id}",
            fingerprint=approval_fingerprint,
            details={
                "approval_scope": approval_scope,
                "approval_context": approval_scope,
                "approval_operator_principal_id": owner_principal_id,
                "approval_owner_principal_id": owner_principal_id,
                "approval_owner_operator_session_id": owner_session_id,
                "operator_session_id": owner_session_id,
                "approval_conversation_id": request.conversation_id,
                "approval_execution_session_id": owner_session_id,
                "durable_job_id": job_id,
                "durable_owner_kind": "user",
                "durable_owner_principal_id": owner_principal_id,
                "durable_service_id": None,
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
        created_approval_id = _text(getattr(approval, "id", None))
        if not created_approval_id:
            raise GitHubFollowthroughError("approval_creation_invalid")
        try:
            # Cancellation may win after the approval row is created but
            # before its id is durably bound.  Recheck first, then use the
            # exact newly-created id for the cleanup CAS on every later error.
            current = await self._assert_prepare_current(
                current,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                routine_binding=routine_binding,
                phase="approval_bind",
            )
            lease = current.get("lease") or {}
            bound = await durable_job_repository.bind_approval_id(
                job_id,
                created_approval_id,
                owner=str(lease.get("owner") or owner),
                fencing_token=int(lease.get("fencing_token") or fence),
                expected_revision=int(current.get("revision") or 0),
            )
            await approval_repository.update_pending_details(
                created_approval_id,
                owner_principal_id=owner_principal_id,
                operator_session_id=owner_session_id,
                updates={
                    "durable_authority_digest": bound.get("authority_digest"),
                    "authority_digest": bound.get("authority_digest"),
                    "approval_expires_at": _approval_expiry(approval.expires_at).timestamp() if _approval_expiry(approval.expires_at) is not None else None,
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
        except Exception:
            try:
                latest = await durable_job_repository.get_job(job_id) or current
            except Exception:
                latest = current
            try:
                await self._deny_pending_approval_for_cancelled_job(
                    latest,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                    job_id=job_id,
                    approval_id_override=created_approval_id,
                )
            except Exception:
                # Preserve the original durable CAS error.  The exact
                # approval helper is retry-safe and the next cancellation or
                # recovery pass will attempt the same idempotent cleanup.
                pass
            raise
        return await self._prepare_job_response(held, prepared=prepared, approval={
            "id": created_approval_id,
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
        if not connection.vault_key or not await vault_repository.exists(connection.vault_key, owner_principal_id=owner_principal_id):
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
            token = await vault_repository.get(connection.vault_key, owner_principal_id=connection.owner_principal_id)
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
        target_path = _publication_effect_target_path(prepared)
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
            effect_target_path = _publication_effect_target_path(prepared)
            readback_details: dict[str, Any] = {
                "verified": False,
                "reason_code": reason,
            }
            if target_path and target_path != effect_target_path:
                readback_details["readback_path"] = target_path
            observed = await durable_job_repository.record_readback(
                prepared.job_id,
                target_path=effect_target_path,
                effect_id=effect_id,
                effect_type="github_publication",
                target_digest=prepared.body_sha256,
                status="unknown",
                details=readback_details,
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
        authority_check: Callable[[], Awaitable[None]] | None = None,
        readback_authority=None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        path = _canonical_path(prepared.repository, prepared.action, prepared.issue_number, remote_id)
        for attempt in range(READBACK_ATTEMPTS):
            remaining = deadline - _now().timestamp()
            if remaining <= 0:
                return False, "readback_deadline", None
            try:
                if authority_check is not None:
                    await authority_check()
                response = await self._request(
                    path,
                    method="GET",
                    token=token,
                    timeout_seconds=min(20.0, max(0.1, remaining)),
                    **({"authority_check": authority_check} if authority_check is not None else {}),
                    **({"readback_authority": readback_authority} if readback_authority is not None else {}),
                )
            except Exception as exc:
                if attempt + 1 >= READBACK_ATTEMPTS:
                    return False, _safe_exception_code(exc), None
                delay = float(1 if attempt == 0 else 2)
                if delay >= remaining:
                    return False, "readback_deadline", None
                await self._sleep(delay)
                continue
            if authority_check is not None:
                await authority_check()
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
        await self._require_current_goal(current)
        connection = await self._get_connection_row(prepared.owner_principal_id)
        if connection is None or connection.revision != prepared.connection_revision or connection.mode != CONNECTION_MODE_ACTIVE:
            return await self._mark_unknown(prepared,reason="connection_revoked_before_adoption",current=current,
                owner=owner,fence=fence,target_path=_publication_effect_target_path(prepared),readback_observation=True)
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
        await self._require_current_goal(current)
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

    async def _require_current_goal(self, current):
        from src.workflows.job_runtime import _assert_canonical_goal_fence, DurableJobTransitionError
        async with db_engine.get_session() as db:
            try:
                await _assert_canonical_goal_fence(db, goal_id=current.get("goal_id"),
                    goal_revision=current.get("goal_revision"), owner_kind=current["owner"]["kind"],
                    owner_principal_id=current["owner"]["principal_id"], session_id=current["session_id"],
                    authority=current.get("declared_authority"))
            except DurableJobTransitionError as exc:
                raise GitHubFollowthroughError("github_current_goal_changed", status_code=409) from exc

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
        try:
            await self._current_routine_publication_binding(
                current,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id or prepared.owner_session_id,
            )
        except GitHubFollowthroughError as exc:
            closed = await self._close_invalid_routine_publication(
                current,
                prepared=prepared,
                reason=exc.code,
            )
            result = await self._prepare_job_response(closed, prepared=prepared)
            result["reason_code"] = exc.code
            result["recovery_action"] = "restore_prerequisite"
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
            # Approval consumption only moves the durable job to the queue; it
            # does not grant a stale routine package a provider dispatch.  The
            # package/version binding is checked again after that CAS and
            # before claim, reservation, or effect intent.
            try:
                await self._current_routine_publication_binding(
                    current,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id or prepared.owner_session_id,
                )
            except GitHubFollowthroughError as exc:
                closed = await self._close_invalid_routine_publication(
                    current,
                    prepared=prepared,
                    reason=exc.code,
                )
                result = await self._prepare_job_response(closed, prepared=prepared)
                result["reason_code"] = exc.code
                result["recovery_action"] = "restore_prerequisite"
                return result
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
            if approval is None or (
                approval.status != "approved"
                and not _consumed_approval_resume_is_current(current, approval)
            ):
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
            post_path = _publication_effect_target_path(prepared)
            try:
                await self._current_routine_publication_binding(
                    current,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id or prepared.owner_session_id,
                )
            except GitHubFollowthroughError as exc:
                closed = await self._close_invalid_routine_publication(
                    current,
                    prepared=prepared,
                    reason=exc.code,
                    known_no_dispatch=True,
                    target_path=post_path,
                    owner=owner,
                    fence=fence,
                )
                if closed.get("status") in {"blocked", "cancelled"}:
                    connection_after_binding_failure = await self._get_connection_row(owner_principal_id)
                    if (
                        connection_after_binding_failure is not None
                        and connection_after_binding_failure.active_job_id == job_id
                    ):
                        await self._release_connection(
                            connection_id=connection_after_binding_failure.id,
                            owner_principal_id=owner_principal_id,
                            job_id=job_id,
                            fence=reservation_fence,
                        )
                result = await self._prepare_job_response(closed, prepared=prepared)
                result["reason_code"] = exc.code
                result["recovery_action"] = "restore_prerequisite"
                return result
            consent_binding = (current.get("declared_authority") or {}).get("github_consent")
            if not isinstance(consent_binding, dict):
                raise GitHubFollowthroughError("github_connection_needs_consent")
            snapshot = await vault_repository.snapshot(connection.vault_key, owner_principal_id=owner_principal_id)
            if snapshot is None or snapshot.binding_digest != consent_binding.get("vault_binding_digest"):
                raise GitHubFollowthroughError("github_consent_credential_or_metadata_changed")
            token = snapshot.value
            current_deadline = _now().timestamp() + EXECUTION_DEADLINE_SECONDS
            if current.get("deadline_at"):
                persisted_deadline = datetime.fromisoformat(
                    str(current["deadline_at"]).replace("Z", "+00:00")
                )
                # SQLite may deserialize a UTC DATETIME without its timezone
                # marker. Treat that naive value as UTC, matching the durable
                # job repository, instead of interpreting it in the host's
                # local timezone and expiring the run early.
                if persisted_deadline.tzinfo is None:
                    persisted_deadline = persisted_deadline.replace(tzinfo=timezone.utc)
                else:
                    persisted_deadline = persisted_deadline.astimezone(timezone.utc)
                current_deadline = min(current_deadline, persisted_deadline.timestamp())
            deadline = current_deadline
            if prepared.action == ACTION_CREATE_ISSUE:
                request_body = {"title": prepared.title, "body": prepared.body}
            else:
                request_body = {"body": prepared.body}
            async def final_authority():
                latest_connection = await self._get_connection_row(owner_principal_id)
                if latest_connection is None:
                    raise GitHubFollowthroughError("connection_missing")
                await self._verify_live_handoff(prepared,owner_principal_id=owner_principal_id,connection=latest_connection)
                await _require_live_owner_session(owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id or prepared.owner_session_id,
                    external_mutation_granted=external_mutation_granted)
            try:
                response = await self._request(
                    post_path,
                    method="POST",
                    token=token,
                    json_body=request_body,
                    timeout_seconds=min(20.0, max(0.1, deadline - _now().timestamp())),
                    authority_check=final_authority,
                    github_consent_binding=consent_binding,
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
            # A revoke after contact cannot undo the remote effect. Keep its
            # reservation and unknown outcome; do not adopt it as success.
            observed_connection = await self._get_connection_row(owner_principal_id)
            if (observed_connection is None
                    or observed_connection.revision != prepared.connection_revision
                    or observed_connection.mode != CONNECTION_MODE_ACTIVE):
                unknown = await self._mark_unknown(prepared, reason="connection_revoked_after_contact",
                    current=await durable_job_repository.get_job(job_id) or current,
                    owner=owner, fence=fence, target_path=post_path)
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
            try:
                verified, reason, readback_payload = await self._readback(
                    prepared,
                    token=token,
                    remote_id=remote_id,
                    deadline=deadline,
                    authority_check=final_authority,
                )
            except GitHubFollowthroughError as exc:
                unknown = await self._mark_unknown(prepared,reason=exc.code,current=latest,
                    owner=owner,fence=fence,target_path=post_path,readback_observation=True)
                return await self._prepare_job_response(unknown,prepared=prepared)
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
                target_path=post_path,
                effect_id=f"github:{prepared.operation_id}",
                effect_type="github_publication",
                target_digest=prepared.body_sha256,
                content_sha256=_sha(_dump(readback_payload)),
                readback_id=_publication_readback_id(prepared),
                verified_at=_now().isoformat(),
                status="succeeded",
                details={
                    "verified": True,
                    "remote_id": remote_id,
                    "repository": prepared.repository,
                    "action": prepared.action,
                    "body_sha256": prepared.body_sha256,
                    "readback_path": readback_path,
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

    async def _deny_pending_approval_for_cancelled_job(
        self,
        current: Mapping[str, Any],
        *,
        owner_principal_id: str,
        owner_session_id: str,
        job_id: str,
        approval_id_override: str | None = None,
    ) -> str:
        """Invalidate only the exact still-pending approval for one M3 job.

        The durable job transition is authoritative.  Approval resolution is a
        companion CAS: an already approved or consumed row remains historical,
        while a pending row is denied only after its server-owned job, owner,
        session, authority, goal, and capability bindings match this job.
        """

        authority = current.get("declared_authority")
        authority = authority if isinstance(authority, Mapping) else {}
        approval_id = _text(approval_id_override or authority.get("approval_id"))
        if not approval_id:
            return "not_bound"
        try:
            approval = await approval_repository.get(approval_id)
        except Exception:
            # Cancellation has already been durably recorded.  A later retry
            # of this same cancellation can safely retry the companion CAS.
            return "unavailable"
        if approval is None:
            return "missing"
        approval_status = _text(getattr(approval, "status", None))
        if approval_status != "pending":
            # Approved/consumed/expired/denied rows are historical and must not
            # be rewritten as a side effect of cancellation.
            return approval_status or "unknown"

        owner = current.get("owner")
        owner = owner if isinstance(owner, Mapping) else {}
        details = _load(getattr(approval, "details_json", None), {})
        if not isinstance(details, Mapping):
            return "binding_mismatch"
        if (
            _text(getattr(approval, "id", None)) != approval_id
            or _text(getattr(approval, "tool_name", None)) != "github:followthrough"
            or _text(getattr(approval, "session_id", None)) != _text(owner_session_id)
            or _text(getattr(approval, "operator_session_id", None)) != _text(owner_session_id)
            or _text(getattr(approval, "owner_principal_id", None)) != _text(owner_principal_id)
            or _text(details.get("durable_approval_id")) != approval_id
            or _text(details.get("approval_id")) != approval_id
            or _text(details.get("durable_job_id")) != _text(job_id)
            or _text(details.get("durable_owner_kind")) != _text(owner.get("kind"))
            or _text(details.get("durable_owner_principal_id")) != _text(owner_principal_id)
            or _text(details.get("operator_session_id")) != _text(owner_session_id)
            or _text(details.get("durable_authority_digest")) != _text(current.get("authority_digest"))
            or _text(details.get("durable_goal_id")) != _text(current.get("goal_id"))
            or details.get("durable_goal_revision") != current.get("goal_revision")
            or details.get("durable_plan_revision") != current.get("plan_revision")
            or _text(details.get("durable_capability_version"))
            != _text(current.get("capability_version"))
            or _text(details.get("durable_budget_digest")) != _text(current.get("budget_digest"))
        ):
            return "binding_mismatch"
        try:
            resolved = await approval_repository.resolve(approval_id, "denied")
        except Exception:
            return "unavailable"
        if resolved is None:
            return "missing"
        return _text(getattr(resolved, "status", None)) or "unknown"

    async def _cancel_cleanup_response(
        self,
        current: Mapping[str, Any],
        *,
        cleanup_status: str,
    ) -> dict[str, Any]:
        """Expose an unresolved approval cleanup without hiding cancellation."""

        response = await self._prepare_job_response(current)
        if cleanup_status in _SAFE_APPROVAL_CLEANUP_OUTCOMES:
            return response
        response.update(
            {
                "status": "blocked",
                "durable_status": _text(current.get("status")) or "cancelled",
                "approval_cleanup": cleanup_status,
                "reason_code": "approval_cleanup_required",
                "recovery_action": "reconcile_or_cancel",
                "operator_action": "reconcile_or_cancel",
                "operator_visible": True,
            }
        )
        return response

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
            if current.get("status") == "cancelled":
                cleanup_status = await self._deny_pending_approval_for_cancelled_job(
                    current,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                    job_id=job_id,
                )
                return await self._cancel_cleanup_response(
                    current,
                    cleanup_status=cleanup_status,
                )
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
        cleanup_status = "not_bound"
        if cancelled.get("status") == "cancelled":
            cleanup_status = await self._deny_pending_approval_for_cancelled_job(
                cancelled,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                job_id=job_id,
            )
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
        if cancelled.get("status") == "cancelled":
            return await self._cancel_cleanup_response(
                cancelled,
                cleanup_status=cleanup_status,
            )
        return await self._prepare_job_response(cancelled)

    async def close_capacity(self, *, owner_principal_id, owner_session_id, job_id, request):
        from pathlib import Path
        from config.settings import settings
        from src.workspace import canonical_workspace_root
        from src.execution.repo_publication_supervisor import guard
        from src.extensions.github_capacity_closure import (ReadWindow, original_effects, stage_observation_inventory,
            effect_identity, _mint_complete_proof)
        current = await durable_job_repository.get_job(job_id)
        if current is None or current.get("job_kind") != JOB_KIND or current.get("owner", {}).get("principal_id") != owner_principal_id or current.get("operator_session_id") != owner_session_id or current.get("declared_authority", {}).get("capability_id") != CAPABILITY_ID:
            raise GitHubFollowthroughError("job_not_found", status_code=404)
        repeated = await durable_job_repository.get_github_capacity_closure(job_id,
            request=request, principal=owner_principal_id, root=owner_session_id)
        if repeated is not None:
            return await self._prepare_job_response(repeated)
        if current["revision"] != request.expected_job_revision or current["status"] not in {"unknown_external_effect", "blocked", "failed"} or current["lease"].get("owner") or current["lease"].get("expires_at"):
            raise GitHubFollowthroughError("github_capacity_close_unleased_revision_required", status_code=409)
        prepared = await self._read_prepared(current)
        await stage_observation_inventory(current)
        effects = original_effects(current)
        prior = effects[0]
        recorded_id = (prior.get("details") or {}).get("remote_id")
        if recorded_id is not None:
            recorded_id = _positive_id(recorded_id, field="remote_id")
        remote_id = request.remote_id if request.remote_id is not None else recorded_id
        if remote_id is None:
            raise GitHubFollowthroughError("remote_id_required", status_code=409)
        if recorded_id is not None and recorded_id != remote_id:
            raise GitHubFollowthroughError("remote_id_binding_conflict", status_code=409)
        authority = GitHubReadbackAuthority(owner_principal_id, owner_session_id, job_id, JOB_KIND,
            request.expected_connection_revision, request.expected_connection_fence,
            current["declared_authority"].get("github_consent") or {})
        # Legacy has no local producer; the same private per-job guard bounds
        # simultaneous close requests, never serving as absence/process proof.
        stage = Path(canonical_workspace_root(settings.workspace_dir)) / f"artifacts/github-capacity-closures/{job_id}/legacy"
        with guard(stage) as (_, _, guard_fd):
            snapshot = await authority.validate()
            window = ReadWindow(legacy=True)
            path = _canonical_path(prepared.repository, prepared.action, prepared.issue_number, remote_id)
            response = await self._request(path, method="GET", token=snapshot.value,
                authority_check=authority.validate, readback_authority=authority, read_window=window)
            if response.status_code != 200:
                raise GitHubFollowthroughError("github_capacity_positive_readback_required", status_code=409)
            payload = self._response_json(response)
            if not isinstance(payload, dict):
                raise GitHubFollowthroughError("github_capacity_positive_readback_required", status_code=409)
            window.decoded(len(response.content))
            identity = effect_identity(current, prior)
            verified = await self.verified_get_receipt(read_authority=authority,
                path=path, payload=payload, effect_identity=identity)
            proof = _mint_complete_proof(current=current, binding=verified.canonical_binding,
                positive_gets=[(verified, identity)], guard_fd=guard_fd, window=window)
            closed = await durable_job_repository.record_github_capacity_closure(job_id,
                request=request, read_authority=authority, proof=proof)
        return await self._prepare_job_response(closed, prepared=prepared)

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
        if current.get("github_capacity_closure"):
            return await self.observe_closed(current=current, request=request)
        if current.get("status") == "succeeded":
            return await self._prepare_job_response(current)
        prepared = await self._read_prepared(current)
        connection = await self._get_connection_row(owner_principal_id)
        if connection is None or connection.id != prepared.connection_id:
            raise GitHubFollowthroughError("connection_not_found", status_code=404)
        if connection.repository != prepared.repository:
            raise GitHubFollowthroughError("repository_binding_changed")
        authority = current.get("declared_authority") or {}
        if current.get("status") not in {"unknown_external_effect", "blocked", "failed", "cost_liability"} or (current.get("lease") or {}).get("owner"):
            raise GitHubFollowthroughError("reconcile_unleased_job_required", status_code=409)
        read_authority = GitHubReadbackAuthority(owner_principal_id, authority.get("session_id"),
            job_id, "github_followthrough_v1", request.expected_connection_revision,
            connection.active_fence, authority.get("github_consent") or {})
        snapshot = await read_authority.validate()
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
        token = snapshot.value
        deadline = _now().timestamp() + EXECUTION_DEADLINE_SECONDS
        verified, reason, payload = await self._readback(
            prepared,
            token=token,
            remote_id=remote_id,
            deadline=deadline,
            authority_check=read_authority.validate, readback_authority=read_authority,
        )
        readback_path = _canonical_path(prepared.repository, prepared.action, prepared.issue_number, remote_id)
        effect_target_path = _publication_effect_target_path(prepared)
        if not verified or payload is None:
            # A failed readback is an observation only. It cannot clear the
            # original intent or authorize a new POST.
            try:
                observed = await durable_job_repository.record_readback(
                    job_id,
                    target_path=effect_target_path,
                    effect_id=f"github:{prepared.operation_id}",
                    effect_type="github_publication",
                    target_digest=prepared.body_sha256,
                    status="unknown",
                    details={
                        "verified": False,
                        "reason_code": reason,
                        "readback_path": readback_path,
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
        prior = next((item for item in current.get("effects", []) if item.get("effect_id") == f"github:{prepared.operation_id}"), None)
        if prior is None:
            raise GitHubFollowthroughError("github_prior_effect_unproven", status_code=409)
        verified_at = _now().isoformat()
        identity = {"job_id": job_id, "attempt_count": current["attempt_count"], "authority_digest": current["authority_digest"],
            "effect_id": prior["effect_id"], "effect_type": prior["effect_type"], "target_path": prior["target_path"],
            "target_digest": prior["target_digest"], "adapter_idempotency_key": prior.get("adapter_idempotency_key")}
        verified_get = await self.verified_get_receipt(read_authority=read_authority, path=readback_path, payload=payload, effect_identity=identity)
        observation = {"schema": "seraph.github-effect-observation.v1", **identity,
            "readback_id": _publication_readback_id(prepared), "verified_at": verified_at,
            "remote_readback": {"remote_id": remote_id, "readback_path": readback_path, "payload_sha256": verified_get.payload_sha256}}
        content = _dump(observation).encode()
        current = await durable_job_repository.record_github_recovery_observation(job_id,
            read_authority=read_authority, verified_readback=verified_get,
            expected_revision=current["revision"], expected_attempt_count=current["attempt_count"],
            expected_authority_digest=current["authority_digest"], effect_id=prior["effect_id"],
            effect_type=prior["effect_type"], target_path=prior["target_path"], target_digest=prior["target_digest"],
            adapter_idempotency_key=prior.get("adapter_idempotency_key"), readback_id=observation["readback_id"],
            verified_at=verified_at, artifact_content=content, artifact_sha256=hashlib.sha256(content).hexdigest())
        if current["receipt"].get("blocked_current_goal"):
            result = await self._prepare_job_response(current, prepared=prepared)
            result.update(reason_code="github_current_goal_changed", observation_only=True)
            return result
        readback = await durable_job_repository.record_readback(
            job_id,
            target_path=effect_target_path,
            effect_id=f"github:{prepared.operation_id}",
            effect_type="github_publication",
            target_digest=prepared.body_sha256,
            content_sha256=_sha(_dump(payload)),
            readback_id=_publication_readback_id(prepared),
            verified_at=verified_at,
            status="succeeded",
            details={
                "verified": True,
                "remote_id": remote_id,
                "repository": prepared.repository,
                "action": prepared.action,
                "readback_path": readback_path,
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

    async def observe_closed(self, *, current, request):
        from src.extensions.github_recovery import closed_authority, record_closed_observation
        from src.extensions.github_capacity_closure import ReadWindow, original_effects, effect_identity, stage_observation_inventory
        authority = await closed_authority(current, expected_revision=request.expected_connection_revision)
        prepared = await self._read_prepared(current)
        async with db_engine.get_session() as db:
            run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == current["job_id"]))).scalars().one()
            history = _load(run.github_capacity_closure_json, {})
        await stage_observation_inventory(current)
        prior = original_effects(current)[0]
        positive = next((item for item in history.get("positive_gets", []) if item["identity"]["effect_id"] == prior["effect_id"]), None)
        if positive is None:
            raise GitHubFollowthroughError("github_closed_readback_positive_history_missing", status_code=409)
        remote_id = _positive_id(int(positive["path"].rsplit("/", 1)[1]), field="remote_id")
        if request.remote_id is not None and request.remote_id != remote_id:
            raise GitHubFollowthroughError("remote_id_binding_conflict", status_code=409)
        window = ReadWindow(legacy=True)
        snapshot = await authority.validate()
        response = await self._request(positive["path"], method="GET", token=snapshot.value,
            authority_check=authority.validate, readback_authority=authority, read_window=window)
        if response.status_code != 200:
            raise GitHubFollowthroughError("github_closed_readback_positive_required", status_code=409)
        payload = self._response_json(response)
        if not isinstance(payload, dict):
            raise GitHubFollowthroughError("github_closed_readback_positive_required", status_code=409)
        identity = effect_identity(current, prior)
        actual = await self.verified_get_receipt(read_authority=authority,
            path=positive["path"], payload=payload, effect_identity=identity)
        observed = await record_closed_observation(durable_job_repository, current, authority, actual, identity)
        result = await self._prepare_job_response(observed, prepared=prepared)
        result.update(observation_only=True, reconciliation="capacity_closed_observation",
            reason_code="github_capacity_closed_observation_only")
        return result


github_followthrough_service = GitHubFollowthroughService()


github_followthrough_router = APIRouter(prefix="/capabilities/github", tags=["github-followthrough"])


def _raise_http(exc: GitHubFollowthroughError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code})


@github_followthrough_router.get("/connection")
async def get_github_connection(request: Request):
    try:
        operator = _operator(request)
        return await github_followthrough_service.get_connection(_principal_id(operator), _session_id(operator))
    except GitHubFollowthroughError as exc:
        raise _raise_http(exc) from exc


@github_followthrough_router.put("/connection")
async def put_github_connection(req: ConnectionRequest, request: Request):
    try:
        operator = _operator(request)
        owner = _principal_id(operator)
        return await github_followthrough_service.put_connection(
            owner_principal_id=owner,
            repository=req.repository,
            vault_key=req.vault_key,
            mode=req.mode,
            expected_revision=req.expected_revision,
            owner_session_id=_session_id(operator), consent=req.consent,
        )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail={"code": "connection_revision_stale"}) from exc
    except GitHubFollowthroughError as exc:
        raise _raise_http(exc) from exc


class ConnectionRevokeRequest(BaseModel):
    expected_revision: int = Field(ge=1)


@github_followthrough_router.post("/connection/revoke")
async def revoke_github_connection(req: ConnectionRevokeRequest, request: Request):
    try:
        operator = _operator(request)
        return await github_followthrough_service.revoke_connection(
            owner_principal_id=_principal_id(operator), expected_revision=req.expected_revision)
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
async def get_github_followthrough_job(job_id: str, request: Request,
                                      pending_capacity_close: str | None = Query(default=None, min_length=2, max_length=2048)):
    try:
        operator = _operator(request)
        await _require_job_session(job_id, operator)
        if pending_capacity_close is not None:
            from src.extensions.github_capacity_closure import LegacyCloseRequest
            try:
                pending = LegacyCloseRequest.model_validate_json(pending_capacity_close)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail={"code": "pending_capacity_close_invalid"}) from exc
            current = await durable_job_repository.inspect_github_capacity_close(job_id, request=pending,
                principal=_principal_id(operator), root=_session_id(operator), native_kind=JOB_KIND)
            return {**await github_followthrough_service._prepare_job_response(current, repair_connection=False),
                    "pending_capacity_close": current["pending_capacity_close"]}
        return await github_followthrough_service.get_job(
            owner_principal_id=_principal_id(operator),
            job_id=job_id,
        )
    except GitHubFollowthroughError as exc:
        raise _raise_http(exc) from exc
    except (DurableJobError, ValueError) as exc:
        raise HTTPException(status_code=409, detail={"code": "pending_capacity_close_unavailable"}) from exc


@github_followthrough_router.post("/jobs/{job_id}/execute")
async def execute_github_followthrough_job(job_id: str, request: Request):
    try:
        operator = _operator(request)
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


@github_followthrough_router.post("/jobs/{job_id}/close-capacity")
async def close_github_followthrough_capacity(job_id: str, req: LegacyCloseRequest, request: Request):
    try:
        operator = _operator(request)
        await _require_job_session(job_id, operator)
        return await github_followthrough_service.close_capacity(
            owner_principal_id=_principal_id(operator), owner_session_id=_session_id(operator),
            job_id=job_id, request=req)
    except GitHubFollowthroughError as exc:
        raise _raise_http(exc) from exc
    except DurableJobError as exc:
        raise HTTPException(status_code=409, detail={"code": "github_capacity_close_conflict"}) from exc
    except ValueError as exc:
        code = str(exc).split(":", 1)[0]
        if re.fullmatch(r"(?:github|publication)_[a-z_]{1,110}", code) is None:
            code = "github_capacity_close_unproven"
        raise HTTPException(status_code=409, detail={"code": code}) from exc
    except OSError as exc:
        raise HTTPException(status_code=409, detail={"code": "github_capacity_private_artifact_unavailable"}) from exc


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
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=409, detail={"code": "github_reconcile_proof_unavailable"}) from exc


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
