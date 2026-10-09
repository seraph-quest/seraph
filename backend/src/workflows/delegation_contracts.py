"""Finite specialist handoffs behind the current task/job authority owners.

These contracts and checks do not admit, queue or grant a child.  Lifecycle
writers supply their canonical parent and retained children in the same writer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from collections.abc import Sequence
from dataclasses import dataclass
import json
import re
from typing import Literal
from weakref import WeakKeyDictionary

from pydantic import Field, field_validator, model_validator

from src.work_board.contracts import ClosedTaskModel, TaskLimits, _safe_reference
from src.work_board.repository import BoardError

HANDOFF_MAX_BYTES = 32 * 1024
MAX_CHILDREN = 4
MAX_ACTIVE_CHILDREN = 2
_FINISHED = frozenset({"succeeded", "failed", "cancelled"})
_PRIVATE_KEYS = frozenset({"password", "secret", "secrets", "secret_ref", "credential",
    "credentials", "credential_ref", "credential_refs", "api_key", "access_token",
    "refresh_token", "authorization", "cookie", "cookies", "session_id",
    "conversation_id", "operator_session_id", "chat_history", "messages"})
_SECRET_TEXT = re.compile(r"(?:secret-ref:|secret://|vault://|Bearer\s+[A-Za-z0-9._~+/=-]+|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"(?:api[_ -]?key|password|access[_ -]?token)\s*[:=]\s*\S+)", re.IGNORECASE)
_EVIDENCE_SOURCES = WeakKeyDictionary()


def _deny(code, message):
    raise BoardError(code, message, status_code=409)


class DelegateLimits(ClosedTaskModel):
    max_steps: int = Field(default=4, ge=1, le=16)
    max_inference_calls: int = Field(default=0, ge=0, le=12)
    wall_seconds: int = Field(default=300, ge=1, le=900)
    depth: Literal[1] = 1
    max_outstanding_children: Literal[0] = 0
    max_cost_microusd: int = Field(default=0, ge=0)

    @field_validator("depth", "max_outstanding_children", mode="before")
    @classmethod
    def exact_counter(cls, value):
        if type(value) is not int:
            raise ValueError("literal delegation counters must be integers")
        return value


class DelegateRequest(ClosedTaskModel):
    parent_task_id: str = Field(min_length=1, max_length=128)
    step_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    role: Literal["research", "files", "engineering", "connected_work"]
    instruction: str = Field(min_length=1, max_length=8192)
    evidence_refs: list[str] = Field(default_factory=list, max_length=12)
    allowed_tool_ids: list[str] = Field(min_length=1, max_length=16)
    limits: DelegateLimits = Field(default_factory=DelegateLimits)

    @field_validator("parent_task_id")
    @classmethod
    def parent_ref(cls, value):
        if _safe_reference(value, field_name="parent_task_id") != value or "/" in value:
            raise ValueError("parent task identity must be exact and opaque")
        return value

    @field_validator("instruction")
    @classmethod
    def bounded_instruction(cls, value):
        if not value.strip() or len(value.encode()) > 8192 or _SECRET_TEXT.search(value):
            raise ValueError("bounded credential-free instruction required")
        return value

    @field_validator("evidence_refs", "allowed_tool_ids")
    @classmethod
    def exact_refs(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("duplicate handoff references")
        for item in value:
            if _safe_reference(item, field_name="handoff reference") != item:
                raise ValueError("handoff references must be exact")
        return value

    @field_validator("allowed_tool_ids")
    @classmethod
    def no_recursive_tool(cls, value):
        if any(not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", item)
            or item == "delegate_task" for item in value):
            raise ValueError("registered nondelegating tools required")
        return value


class ChildResult(ClosedTaskModel):
    child_id: str = Field(min_length=1, max_length=128)
    artifact_refs: list[str] = Field(default_factory=list, max_length=16)
    unresolved: list[str] = Field(default_factory=list, max_length=16)
    summary_ref: str | None = Field(default=None, max_length=512)

    @field_validator("child_id", "summary_ref")
    @classmethod
    def safe_ref(cls, value):
        if value is not None and _safe_reference(value, field_name="child result") != value:
            raise ValueError("exact result reference required")
        return value

    @field_validator("artifact_refs")
    @classmethod
    def artifacts(cls, value):
        if len(value) != len(set(value)) or any(
            _safe_reference(item, field_name="artifact") != item for item in value):
            raise ValueError("unique exact artifact references required")
        return value

    @field_validator("unresolved")
    @classmethod
    def finite_unresolved(cls, value):
        if any(not item.strip() or len(item.encode()) > 512 or _SECRET_TEXT.search(item) for item in value):
            raise ValueError("bounded credential-free unresolved facts required")
        return value

    @model_validator(mode="after")
    def summary_is_artifact(self):
        if self.summary_ref is not None and self.summary_ref not in self.artifact_refs:
            raise ValueError("summary must name a child artifact")
        return self


class EvidenceHandoff(ClosedTaskModel):
    reference: str
    producer_revision: int = Field(ge=1)
    producer_attempt_ref: str
    file_path: str
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=1, le=HANDOFF_MAX_BYTES)
    content: str


@dataclass(frozen=True, eq=False)
class DelegationEvidenceHandoff(Sequence):
    """Source-issued copied bytes; private staging metadata never goes on wire."""
    entries: tuple[EvidenceHandoff, ...]
    owner_principal_id: str
    original_root_id: str
    group_digest: str
    producer_tokens: tuple[str, ...]
    vault_state_digest: str

    def __len__(self):
        return len(self.entries)

    def __getitem__(self, index):
        return self.entries[index]


def _packet_snapshot(packet):
    from src.work_board.general_task import digest
    return (packet.owner_principal_id, packet.original_root_id, packet.group_digest,
        packet.producer_tokens, packet.vault_state_digest,
        digest([entry.model_dump(mode="json") for entry in packet.entries]))


async def _vault_state(db):
    from sqlalchemy import select
    from src.db.models import Secret
    from src.work_board.pipelines import row_token
    from src.work_board.general_task import digest
    rows = (await db.execute(select(Secret).execution_options(populate_existing=True))).scalars().all()
    return digest(sorted(row_token(row) for row in rows))


async def _current_owner(db, owner, parent_envelope):
    from sqlalchemy import select
    from src.auth.service import authenticate_principal
    from src.db.models import OperatorSession
    from src.work_board.contracts import GeneralTaskEnvelope
    from src.work_board.repository import WorkBoardRepository
    now = datetime.now(timezone.utc)
    if type(parent_envelope) is not GeneralTaskEnvelope:
        _deny("delegation_contract_invalid", "Original typed parent envelope required")
    group = parent_envelope.proposal_group
    if (group is None or group.goal_id != parent_envelope.task_input.goal_ref
        or group.owner_principal_id != owner.principal_id or group.owner_session_id != owner.session_id
        or group.original_deadline_at <= now):
        _deny("delegation_original_owner_changed", "Original delegation owner or deadline changed")
    root = await db.scalar(select(OperatorSession).where(
        OperatorSession.id == owner.session_id, OperatorSession.principal_id == owner.principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False), OperatorSession.idle_expires_at > now,
        OperatorSession.absolute_expires_at > now).execution_options(populate_existing=True))
    if root is None:
        _deny("delegation_root_revoked", "Original Root is no longer current")
    await authenticate_principal(owner.principal_id, db=db)
    await WorkBoardRepository._validate_goal(db, owner, goal_id=group.goal_id, goal_revision=group.goal_revision)
    return group


async def _producer_tokens(db, owner, references):
    """Canonical metadata only; never invoke a physical output verifier here."""
    from sqlalchemy import select
    from src.db.models import WorkBoardTask, WorkBoardAttempt, WorkflowRunState, WorkBoardInputArtifact
    from src.work_board.repository import WorkBoardRepository
    from src.work_board.pipelines import row_token
    from src.workflows.job_runtime import _effect_ledger_or_raise, _job_has_unsafe_effects
    repository, tokens = WorkBoardRepository(), []
    for reference in references:
        producer = await db.scalar(select(WorkBoardTask).where(
            WorkBoardTask.task_id == reference.removeprefix("board-output:"))
            .execution_options(populate_existing=True))
        if (producer is None or producer.owner_principal_id != owner.principal_id
            or producer.owner_session_id != owner.session_id):
            _deny("delegation_evidence_changed", "Original selected producer changed")
        goal = await repository.validate_task_goal(db, owner, producer)
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == producer.task_id)
            .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1)
            .execution_options(populate_existing=True))
        run = None if attempt is None else await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == attempt.workflow_run_id).execution_options(populate_existing=True))
        source = await db.get(WorkBoardInputArtifact, producer.input_artifact_id, populate_existing=True)
        if run is None or source is None or _job_has_unsafe_effects(_effect_ledger_or_raise(run.effect_receipts_json)):
            _deny("delegation_evidence_unresolved", "Unresolved producer effects cannot enter a handoff")
        tokens.extend(row_token(row) for row in (producer, attempt, run, goal, source))
    return tuple(tokens)


async def recheck_delegation_evidence(db, owner, parent_envelope, evidence_handoffs):
    """Consume authentic staged bytes under a short canonical writer, without IO."""
    from src.work_board.general_task import digest
    packet = evidence_handoffs
    if (type(packet) is not DelegationEvidenceHandoff
        or _EVIDENCE_SOURCES.get(packet) != _packet_snapshot(packet)):
        _deny("delegation_evidence_witness_denied", "Original source-issued evidence handoff required")
    group = await _current_owner(db, owner, parent_envelope)
    if ((packet.owner_principal_id, packet.original_root_id) != (owner.principal_id, owner.session_id)
        or packet.group_digest != digest(group.model_dump(mode="json"))):
        _deny("delegation_evidence_changed", "Original staged scope changed")
    references = [entry.reference for entry in packet]
    original = {item["reference"]: item for item in parent_envelope.evidence}
    if any(entry.model_dump(exclude={"content"}) != original.get(entry.reference) for entry in packet):
        _deny("delegation_evidence_changed", "Original selected evidence changed")
    if (await _producer_tokens(db, owner, references) != packet.producer_tokens
        or await _vault_state(db) != packet.vault_state_digest):
        _deny("delegation_evidence_changed", "Staged producer or credential state changed")
    return packet


def verify_delegate_request(request, *, parent_task_id, parent_limits,
                            parent_allowed_tool_ids, parent_evidence_refs,
                            existing_children=(), parent_is_child=False):
    """Check narrowing only; callers still prove canonical authority and CAS."""
    if type(request) is not DelegateRequest or not isinstance(parent_limits, TaskLimits):
        _deny("delegation_contract_invalid", "Canonical typed delegation bounds required")
    request = DelegateRequest.model_validate(request.model_dump(mode="python"))
    parent_limits = TaskLimits.model_validate(parent_limits.model_dump(mode="python"))
    if parent_is_child or parent_limits.depth != 0 or request.parent_task_id != parent_task_id:
        _deny("delegation_depth_denied", "Only one original parent may delegate")
    if not set(request.allowed_tool_ids) <= set(parent_allowed_tool_ids):
        _deny("delegation_tool_scope_denied", "Child tools exceed the original parent")
    if not set(request.evidence_refs) <= set(parent_evidence_refs):
        _deny("delegation_evidence_scope_denied", "Child evidence exceeds explicit parent selection")
    children = list(existing_children)
    retained_requests = [DelegateRequest.model_validate(child["request"]) for child in children]
    if any(child.parent_task_id != parent_task_id for child in retained_requests):
        _deny("delegation_child_owner_changed", "Retained child belongs to a different original parent")
    if request.step_id in {child.step_id for child in retained_requests}:
        _deny("delegation_child_already_retained", "Recover the original child instead of admitting another")
    if len(children) >= MAX_CHILDREN:
        _deny("delegation_total_limit", "Original parent already retained four children")
    active = sum(child["status"] not in _FINISHED for child in children)
    if active >= min(MAX_ACTIVE_CHILDREN, parent_limits.max_outstanding_children):
        _deny("delegation_simultaneous_limit", "Original parent has no free child slot")
    for field in ("max_steps", "max_inference_calls", "max_cost_microusd"):
        retained = sum(getattr(child.limits, field) for child in retained_requests)
        if retained + getattr(request.limits, field) > getattr(parent_limits, field):
            _deny("delegation_budget_denied", "Child allocation exceeds original retained bounds")
    if request.limits.wall_seconds > parent_limits.wall_seconds:
        _deny("delegation_deadline_denied", "Child cannot extend the original clock")
    return request


def verify_child_result(result, *, child_id, verified_artifact_refs):
    """Only independently read-back artifacts may enter a parent's synthesis."""
    if type(result) is not ChildResult or result.child_id != child_id:
        _deny("delegation_result_changed", "Original child result identity changed")
    result = ChildResult.model_validate(result.model_dump(mode="python"))
    if not set(result.artifact_refs) <= set(verified_artifact_refs):
        _deny("delegation_result_unverified", "Child artifact requires physical readback")
    return result


def _safe_evidence_text(text):
    if _SECRET_TEXT.search(text):
        _deny("delegation_credentials_denied", "Credentials cannot enter a specialist handoff")
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return
    def visit(item, depth=0):
        if depth > 32:
            _deny("delegation_evidence_complexity", "Evidence nesting exceeds its bound")
        if isinstance(item, dict):
            if any(str(key).lower().replace("-", "_") in _PRIVATE_KEYS for key in item):
                _deny("delegation_private_context_denied", "Credentials or conversations are not handoff evidence")
            for child in item.values():
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, str) and item.lstrip().startswith(("{", "[")):
            try:
                embedded = json.loads(item)
            except ValueError:
                return
            visit(embedded, depth + 1)
    visit(value)


async def validate_delegation_instruction(db, request):
    from src.vault.redaction import redact_secrets_in_text_readonly
    request = DelegateRequest.model_validate(request.model_dump(mode="python"))
    if await redact_secrets_in_text_readonly(db, request.instruction, fail_closed=True,
        minimum_secret_length=1) != request.instruction:
        _deny("delegation_credentials_denied", "Credentials cannot enter a specialist instruction")
    return request


async def resolve_delegation_evidence(service, db, owner, parent_envelope, evidence_refs):
    """Copy only selected original verified Work outputs through current owners."""
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.workspace import canonical_workspace_root
    from src.vault.redaction import redact_secrets_in_text_readonly
    from config.settings import settings
    from src.work_board.general_task import digest
    group = await _current_owner(db, owner, parent_envelope)
    refs = list(evidence_refs)
    if len(refs) != len(set(refs)) or not set(refs) <= set(parent_envelope.task_input.evidence_refs):
        _deny("delegation_evidence_scope_denied", "Only explicit original parent evidence may be copied")
    original = {item["reference"]: item for item in parent_envelope.evidence}
    if len(original) != len(parent_envelope.evidence) or any(ref not in original for ref in refs):
        _deny("delegation_evidence_changed", "Original evidence binding is unavailable")
    if any(not ref.startswith("board-output:") for ref in refs):
        _deny("delegation_private_context_denied", "Conversation and secret references are excluded")
    if sum(original[ref]["size_bytes"] for ref in refs) > HANDOFF_MAX_BYTES:
        _deny("delegation_handoff_limit", "Evidence handoff exceeds 32 KiB")
    tokens = await _producer_tokens(db, owner, refs)
    vault_state = await _vault_state(db)
    current = await service.evidence(db, owner, refs)
    if current != [original[ref] for ref in refs]:
        _deny("delegation_evidence_changed", "Original selected evidence changed")
    result = []
    for metadata in current:
        raw = _safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / metadata["file_path"],
            expected_digest=metadata["content_sha256"], expected_size=metadata["size_bytes"])
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise BoardError("delegation_evidence_encoding", "Evidence must be bounded UTF-8 text", status_code=409) from exc
        _safe_evidence_text(text)
        if await redact_secrets_in_text_readonly(db, text, fail_closed=True, minimum_secret_length=1) != text:
            _deny("delegation_credentials_denied", "Credentials cannot enter a specialist handoff")
        result.append(EvidenceHandoff(**metadata, content=text))
    encoded = json.dumps([item.model_dump(mode="json") for item in result],
        ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > HANDOFF_MAX_BYTES:
        _deny("delegation_handoff_limit", "Complete evidence handoff exceeds 32 KiB")
    if await _producer_tokens(db, owner, refs) != tokens or await _vault_state(db) != vault_state:
        _deny("delegation_evidence_changed", "Evidence changed during staging")
    packet = DelegationEvidenceHandoff(tuple(result), owner.principal_id, owner.session_id,
        digest(group.model_dump(mode="json")), tokens, vault_state)
    _EVIDENCE_SOURCES[packet] = _packet_snapshot(packet)
    return packet
