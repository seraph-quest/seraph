"""Finite GitHub authority on the existing connection row; no grant store."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.auth.service import AuthFailure, authenticate_session
from src.vault.repository import vault_repository

GitHubAction = Literal["github_issue_write", "github_comment_write", "github_git_objects_write", "github_new_branch_write", "github_ready_pr_write"]
ACTIONS = frozenset(GitHubAction.__args__)
PUBLICATION_ACTIONS = frozenset({"github_git_objects_write", "github_new_branch_write", "github_ready_pr_write"})


class GitHubConsentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    acknowledged: Literal[True]
    duration_seconds: int = Field(ge=60, le=3600)
    actions: list[GitHubAction] = Field(min_length=1, max_length=5)

    @field_validator("acknowledged", mode="before")
    @classmethod
    def exact_ack(cls, value):
        if value is not True:
            raise ValueError("explicit GitHub consent required")
        return value

    @field_validator("actions")
    @classmethod
    def unique_actions(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("duplicate GitHub actions")
        return sorted(value)


def deny(code):
    from src.extensions.github_followthrough import GitHubFollowthroughError
    raise GitHubFollowthroughError(code, status_code=403)


def utc(value):
    if not isinstance(value, datetime):
        deny("github_consent_expiry_invalid")
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def live_operator(principal, root):
    try:
        operator = await authenticate_session(root, touch=False)
    except AuthFailure:
        deny("owner_session_invalid")
    if operator.session_id != root or operator.principal.principal_id != principal:
        deny("owner_session_mismatch")
    return operator


def decoded_actions(row):
    try:
        actions = json.loads(row.consent_actions_json or "null")
    except (ValueError, TypeError):
        deny("github_consent_metadata_invalid")
    if not isinstance(actions, list) or not 1 <= len(actions) <= 5 or any(type(action) is not str or action not in ACTIONS for action in actions) or actions != sorted(set(actions)):
        deny("github_consent_metadata_invalid")
    return actions


def payload(row, vault_binding_digest):
    return {"schema": "seraph.github-connection-consent.v1", "consent_id": row.consent_id,
        "principal": row.owner_principal_id, "root": row.consent_owner_session_id,
        "connection_id": row.id, "connection_revision": row.consent_connection_revision,
        "repository": row.repository, "vault_binding_digest": vault_binding_digest,
        "actions": decoded_actions(row), "issued_at": utc(row.consent_issued_at).isoformat(),
        "expires_at": utc(row.consent_expires_at).isoformat()}


def issuance(row, operator, request, vault_binding_digest, revision):
    now = datetime.now(timezone.utc)
    expiry = min(now + timedelta(seconds=request.duration_seconds), utc(operator.absolute_expires_at), utc(operator.idle_expires_at))
    if expiry <= now:
        deny("owner_session_invalid")
    fields = {"consent_id": str(uuid.uuid4()), "consent_owner_session_id": operator.session_id,
        "consent_actions_json": json.dumps(request.actions), "consent_issued_at": now,
        "consent_expires_at": expiry, "consent_connection_revision": revision,
        "consent_revoked_at": None}
    # Caller writes this exact binding and its connection CAS atomically.
    from types import SimpleNamespace
    value = SimpleNamespace(id=row.id, owner_principal_id=row.owner_principal_id,
        repository=row.repository, **fields)
    fields["consent_payload_digest"] = digest(payload(value, vault_binding_digest))
    return fields


async def require_consent(row, *, principal, root, repository, revision, required_actions, binding=None, snapshot=None):
    operator = await live_operator(principal, root)
    if row is None or row.owner_principal_id != principal or row.repository != repository or row.revision != revision:
        deny("github_consent_binding_changed")
    if row.mode != "active" or not row.consent_id:
        deny("github_connection_needs_consent")
    if row.consent_owner_session_id != root:
        deny("github_consent_root_changed")
    if row.consent_revoked_at is not None:
        deny("github_consent_revoked")
    if type(row.consent_connection_revision) is not int or row.consent_connection_revision != revision:
        deny("github_consent_revision_changed")
    now = datetime.now(timezone.utc)
    issued, expiry = utc(row.consent_issued_at), utc(row.consent_expires_at)
    if issued > now or expiry <= now or not 0 < (expiry-issued).total_seconds() <= 3600 or expiry > utc(operator.absolute_expires_at):
        deny("github_consent_expired")
    actions = decoded_actions(row)
    if not required_actions or not set(required_actions) <= ACTIONS or not set(required_actions) <= set(actions):
        deny("github_consent_action_denied")
    snapshot = snapshot or await vault_repository.snapshot(row.vault_key, owner_principal_id=principal)
    if snapshot is None:
        deny("credential_not_configured")
    if not isinstance(row.consent_payload_digest, str) or re.fullmatch(r"[0-9a-f]{64}", row.consent_payload_digest) is None or digest(payload(row, snapshot.binding_digest)) != row.consent_payload_digest:
        deny("github_consent_credential_or_metadata_changed")
    canonical = {"consent_id": row.consent_id, "consent_payload_digest": row.consent_payload_digest,
        "owner_principal_id": principal,
        "consent_root_id": root, "connection_id": row.id, "connection_revision": revision,
        "repository": repository, "consent_actions_digest": digest(actions),
        "consent_expires_at": expiry.isoformat(), "vault_binding_digest": snapshot.binding_digest}
    if binding is not None and canonical != binding:
        deny("github_consent_binding_changed")
    return canonical


def mutation_action(repository, method, path, body):
    """Derive action from fixed remote operation, never a caller Boolean."""
    prefix = "/repos/" + repository
    if method != "POST" or not isinstance(body, dict):
        deny("github_consent_operation_invalid")
    if path == prefix + "/issues" and set(body) == {"title", "body"}:
        return "github_issue_write"
    if re.fullmatch(re.escape(prefix) + r"/issues/[1-9][0-9]*/comments", path) and set(body) == {"body"}:
        return "github_comment_write"
    if path == prefix + "/git/blobs" and set(body) == {"content", "encoding"} and body["encoding"] == "base64":
        return "github_git_objects_write"
    if path == prefix + "/git/trees" and set(body) == {"base_tree", "tree"}:
        return "github_git_objects_write"
    if path == prefix + "/git/commits" and set(body) == {"message", "tree", "parents", "author", "committer"}:
        return "github_git_objects_write"
    if path == prefix + "/git/refs" and set(body) == {"ref", "sha"} and isinstance(body["ref"], str) and re.fullmatch(r"refs/heads/(?:feat|fix)/[A-Za-z0-9_./-]+", body["ref"]):
        return "github_new_branch_write"
    if path == prefix + "/pulls" and set(body) == {"title", "body", "head", "base", "draft"} and body["draft"] is False:
        return "github_ready_pr_write"
    deny("github_consent_operation_invalid")


async def projection(row, root):
    value = {"state": "needs_consent", "expires_at": None, "actions": [], "root_bound": False,
        "credential_is_consent": False}
    if row is None or not row.consent_id:
        return value
    try:
        value.update(expires_at=utc(row.consent_expires_at).isoformat(), actions=decoded_actions(row), root_bound=row.consent_owner_session_id == root)
        await require_consent(row, principal=row.owner_principal_id, root=root,
            repository=row.repository, revision=row.revision, required_actions=value["actions"])
        value["state"] = "active"
    except Exception as exc:
        value["state"] = getattr(exc, "code", "github_consent_unavailable")
    return value


async def require_readback(row, *, principal, root, original_binding, expected_revision,
                           job_id, connection_fence, snapshot=None):
    """Ephemeral original-root GET authority; never refresh old write consent."""
    await live_operator(principal, root)
    if row is None or row.owner_principal_id != principal or row.id != original_binding.get("connection_id") or row.repository != original_binding.get("repository") or row.revision != expected_revision:
        deny("github_readback_connection_changed")
    if original_binding.get("owner_principal_id") != principal or original_binding.get("consent_root_id") != root:
        deny("github_readback_root_changed")
    if row.mode not in {"active", "disabled", "reconcile_only"} or row.active_job_id != job_id or row.active_fence != connection_fence:
        deny("github_readback_reservation_changed")
    snapshot = snapshot or await vault_repository.snapshot(row.vault_key, owner_principal_id=principal)
    if snapshot is None or snapshot.binding_digest != original_binding.get("vault_binding_digest"):
        deny("github_readback_credential_changed")
    return snapshot


async def require_followthrough_consent(*, principal, root, action, repository=None,
                                      revision=None, binding=None):
    permission = {"create_issue": "github_issue_write", "create_comment": "github_comment_write"}.get(action)
    if permission is None:
        deny("github_consent_action_denied")
    from src.extensions.github_followthrough import GitHubFollowthroughService
    row = await GitHubFollowthroughService()._get_connection_row(principal)
    if row is None:
        deny("github_connection_needs_consent")
    return await require_consent(row, principal=principal, root=root,
        repository=repository if repository is not None else row.repository,
        revision=revision if revision is not None else row.revision,
        required_actions={permission}, binding=binding)


@dataclass(frozen=True)
class GitHubReadbackAuthority:
    """Adapter-owned canonical GET validation, with no callable bypass."""
    principal: str
    root: str
    job_id: str
    capability: str
    connection_revision: int
    connection_fence: int
    original_binding: dict

    async def validate(self):
        from src.extensions.github_followthrough import GitHubFollowthroughService
        row = await GitHubFollowthroughService()._get_connection_row(self.principal)
        return await require_readback(row, principal=self.principal, root=self.root,
            original_binding=self.original_binding, expected_revision=self.connection_revision,
            job_id=self.job_id, connection_fence=self.connection_fence)


_GET_RECEIPT_SEAL = object()


@dataclass(frozen=True)
class _ProtectedGetSeal:
    origin: object
    identity: str


def _seal_verified_get(value):
    # A copied dataclass with changed fields cannot reuse the actual adapter
    # capture's seal. All authority-bearing nested dictionaries are bound.
    identity = {key: item.isoformat() if isinstance(item, datetime) else item
        for key, item in value.__dict__.items() if key != "_seal"}
    return _ProtectedGetSeal(_GET_RECEIPT_SEAL, digest(identity))


@dataclass(frozen=True)
class GitHubVerifiedReadback:
    """Opaque evidence minted by the adapter from an actual protected GET."""
    job_id: str
    root: str
    capability: str
    readback_path: str
    payload_sha256: str
    read_authority_digest: str
    effect_identity_digest: str
    observed_at: datetime
    _seal: object
    semantic_payload_sha256: str
    canonical_binding: dict

    def validates(self, authority, value, effect_identity):
        return type(self._seal) is _ProtectedGetSeal and self._seal.origin is _GET_RECEIPT_SEAL and self._seal.identity == _seal_verified_get(self).identity and self.job_id == authority.job_id and self.root == authority.root and self.capability == authority.capability and self.read_authority_digest == digest(authority.__dict__) and self.effect_identity_digest == digest(effect_identity) and 0 <= (datetime.now(timezone.utc)-self.observed_at).total_seconds() <= 30 and value.get("readback_path") == self.readback_path and value.get("payload_sha256") == self.payload_sha256
