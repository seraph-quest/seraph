"""Private, finite original-owner candidates for the three Memory mutations.

The candidate is data, never authority. Admission/claim/dispatch must retain its
exact bytes on the one original job and publish the effect result in the same
writer. Only opaque references enter the stock service frame.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import threading
import weakref
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import select

from .dispatch import NativeServiceBlocked

MEMORY_JOB_KIND = "runtime_service_memory_v1"
MEMORY_METHODS = frozenset({"memory.propose", "memory.applyReviewed", "memory.forget"})
MEMORY_CONTEXT_TAG = "native-memory-mutation.v1"
MAX_CANDIDATE_BYTES = 16384
METADATA_SOURCE_CAPABILITIES = frozenset({"workflow.goal-snapshot-to-file"})
REPORT_SOURCE_CAPABILITY = "work.local-evidence-report.v1"
_MEMORY_SOURCE_SEAL = object()
_MEMORY_SOURCES = weakref.WeakValueDictionary()
_MEMORY_OPERATIONS = weakref.WeakValueDictionary()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class MemorySourceBinding(_Closed):
    task_id: str = Field(min_length=1, max_length=512)
    expected_task_revision: int = Field(ge=1)
    attempt_id: str = Field(min_length=1, max_length=512)
    attempt_fence: int = Field(ge=1)
    workflow_run_id: str = Field(min_length=1, max_length=512)
    workflow_run_revision: int = Field(ge=0)
    goal_id: str = Field(min_length=1, max_length=512)
    goal_revision: int = Field(ge=1)
    capability_id: str = Field(min_length=1, max_length=512)
    capability_version: str = Field(min_length=1, max_length=128)
    typed_input_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    readback_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class _PreparedText(_Closed):
    original_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    sanitized_text: str = Field(min_length=1, max_length=2000)
    vault_rows_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class _Candidate(_Closed):
    schema_version: Literal[1]
    operator_principal_id: str = Field(min_length=1, max_length=512)
    operator_session_id: str = Field(min_length=1, max_length=512)
    opaque_ref: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    idempotency_key: str = Field(pattern=r"^[\x21-\x7e]{1,128}$")
    original_deadline: str
    host_boot_nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    composition_binding_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def deadline(self):
        parsed = datetime.fromisoformat(self.original_deadline)
        if parsed.tzinfo != timezone.utc or parsed.isoformat() != self.original_deadline:
            raise ValueError("original deadline must be canonical UTC")
        return self


class _Propose(_Candidate):
    method: Literal["memory.propose"]
    source: MemorySourceBinding
    prepared_text: _PreparedText


class _Review(_Candidate):
    method: Literal["memory.applyReviewed"]
    source: MemorySourceBinding
    proposal_id: str = Field(min_length=1, max_length=512)
    proposal_schema: Literal["memory_proposal.v1"]
    action: Literal["accept", "edit_accept"]
    expected_revision: int = Field(ge=1)
    expected_preview_text_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    edited_text: str | None = Field(max_length=2000)
    decision_effect: Literal["none", "require_operator_confirmation"] | None
    preferred_capability_id: str | None = Field(max_length=160)
    corrects_memory_id: str | None = Field(max_length=255)
    reason: str | None = Field(max_length=500)
    proposal_expires_at: str
    prepared_text: _PreparedText

    @model_validator(mode="after")
    def review_shape(self):
        if self.action == "accept" and self.edited_text is not None:
            raise ValueError("edited text requires edit_accept")
        expires = datetime.fromisoformat(self.proposal_expires_at)
        if (expires.tzinfo != timezone.utc or expires.isoformat() != self.proposal_expires_at
            or datetime.fromisoformat(self.original_deadline) > expires):
            raise ValueError("review deadline exceeds original expiry")
        return self


class _Forget(_Candidate):
    method: Literal["memory.forget"]
    record_ref: str = Field(min_length=1, max_length=128)
    mode: Literal["archive", "redact"]
    privacy_boundary: Literal["operator_visible", "private", "sensitive", "source_bound"]
    reason: str | None = Field(max_length=500)
    prepared_reason: _PreparedText | None

    @model_validator(mode="after")
    def reason_binding(self):
        if (self.reason is None) != (self.prepared_reason is None):
            raise ValueError("forget reason requires its original Vault staging")
        return self


_CANDIDATE = TypeAdapter(_Propose | _Review | _Forget)


def validate_memory_candidate(value):
    try:
        if type(value) is not dict or type(value.get("schema_version")) is not int:
            raise ValueError("closed JSON candidate required")
        candidate = _CANDIDATE.validate_python(value).model_dump(mode="json")
        if len(_canonical(candidate).encode("utf-8")) > MAX_CANDIDATE_BYTES:
            raise ValueError("private Memory candidate exceeds UTF-8 bound")
        return candidate
    except (ValueError, TypeError) as exc:
        raise NativeServiceBlocked("native_memory_candidate_invalid") from exc


@dataclass(frozen=True)
class NativeMemoryMutationAdmission:
    candidate_json: str
    candidate_digest: str
    report_source: object = field(default=None, repr=False, compare=False)
    header_budget: object = field(default=None, repr=False, compare=False)

    @classmethod
    def from_candidate(cls, value):
        candidate = validate_memory_candidate(value)
        return cls(_canonical(candidate), _digest(candidate))

    def candidate(self):
        try:
            value = validate_memory_candidate(json.loads(self.candidate_json))
            if _canonical(value) != self.candidate_json or _digest(value) != self.candidate_digest:
                raise ValueError("candidate bytes changed")
            return value
        except (ValueError, TypeError) as exc:
            raise NativeServiceBlocked("native_memory_candidate_changed") from exc

    def wire_inputs(self):
        candidate = self.candidate()
        key = "review_ref" if candidate["method"] == "memory.applyReviewed" else "request_ref"
        return {key: candidate["opaque_ref"]}


@dataclass(frozen=True, eq=False)
class _NativeMemoryDispatchSource:
    """Actual inline claim/candidate/operator objects; never a JSON grant."""
    claim: object = field(repr=False)
    admission: NativeMemoryMutationAdmission = field(repr=False)
    operator: object = field(repr=False)
    host: object = field(repr=False)
    report_source: object = field(repr=False)
    _live: object = field(default_factory=threading.Event, init=False, repr=False)
    _seal: object = field(default=None, init=False, repr=False)
    _issued_id: int = field(default=0, init=False, repr=False)
    _scope: object = field(default=None, init=False, repr=False)

    def close(self):
        self._live.clear()
        _MEMORY_SOURCES.pop(id(self), None)


def _issue_memory_dispatch_source(claim, admission, operator, host):
    # Only execute_native_memory transports the actual claim return here.
    from src.auth.service import AuthenticatedOperator
    from src.workflows.job_runtime import NativeServiceClaim
    from .bridge import CordisHost
    from .dispatch import capture_original_scope
    if (type(claim) is not NativeServiceClaim or type(admission) is not NativeMemoryMutationAdmission
            or type(operator) is not AuthenticatedOperator or type(host) is not CordisHost
            or claim._host is not host or not host.admitting or host.reviewed is None):
        raise NativeServiceBlocked("native_memory_actual_claim_source_required")
    candidate = admission.candidate()
    original = capture_original_scope(claim, host)
    witness = dict(original.witness)
    if (witness["origin_method"] != candidate["method"]
            or witness["input_digest"] != admission.candidate_digest
            or witness["composition_binding_digest"] != candidate["composition_binding_digest"]
            or witness["host_boot_nonce"] != candidate["host_boot_nonce"]
            or operator.session_id != candidate["operator_session_id"]
            or operator.principal.principal_id != candidate["operator_principal_id"]):
        raise NativeServiceBlocked("native_memory_actual_claim_source_changed")
    source = _NativeMemoryDispatchSource(claim, admission, operator, host, admission.report_source)
    object.__setattr__(source, "_seal", _MEMORY_SOURCE_SEAL)
    object.__setattr__(source, "_issued_id", id(source))
    source._live.set()
    _MEMORY_SOURCES[id(source)] = source
    scope = replace(original, native_memory_report_source=admission.report_source, native_memory_source=source)
    object.__setattr__(source, "_scope", scope)
    return scope


def memory_dispatch_header_budget(*, dispatcher, scope, called_scope, invocation_ref, method):
    """Return numeric capacity only from the actual unresolved Source call."""
    from .bridge import CordisHost, Pending, cordis_host
    from .dispatch import OriginalServiceInvocation, CalledServiceInvocation, NativeServiceDispatcher
    from src.workflows.job_runtime import NativeServiceClaim
    from src.memory.header_bounds import HeaderReadBudget
    if (method not in MEMORY_METHODS or type(scope) is not OriginalServiceInvocation
            or type(called_scope) is not CalledServiceInvocation or called_scope.original is not scope
            or type(dispatcher) is not NativeServiceDispatcher):
        raise NativeServiceBlocked("native_memory_actual_source_required")
    source = scope.native_memory_source
    if (type(source) is not _NativeMemoryDispatchSource or source._seal is not _MEMORY_SOURCE_SEAL
            or source._issued_id != id(source) or _MEMORY_SOURCES.get(id(source)) is not source
            or not source._live.is_set() or source._scope is not scope
            or source.report_source is not scope.native_memory_report_source
            or type(source.claim) is not NativeServiceClaim or type(source.host) is not CordisHost
            or source.host is not cordis_host or source.claim._host is not source.host
            or source.host.service_dispatch is not dispatcher):
        raise NativeServiceBlocked("native_memory_actual_source_unavailable")
    host, claim = source.host, source.claim
    pending = host._pending.get(called_scope.parent_request_id)
    if (not host.admitting or host.reviewed is None
            or claim.host_boot_nonce != host.boot_nonce or scope.host_boot_nonce != host.boot_nonce
            or called_scope.host_boot_nonce != host.boot_nonce
            or host.reviewed.package_digest != scope.witness["package_digest"]
            or host.reviewed.composition_digest != scope.witness["host_composition_digest"]
            or scope.binding is not claim.binding or scope.binding.origin_method != method
            or scope.witness["origin_method"] != method or called_scope.method != method
            or scope.witness["invocation_ref"] != invocation_ref or claim.job["job_id"] != invocation_ref
            or invocation_ref not in host._native_inflight
            or type(pending) is not Pending or pending.future.done()
            or pending.native_binding is not called_scope
            or pending.frame["request_id"] != called_scope.parent_request_id
            or pending.frame["invocation_ref"] != invocation_ref or pending.frame["method"] != method
            or pending.frame["deadline_at"] != called_scope.deadline_at
            or called_scope.deadline_at > scope.deadline_at
            or called_scope.deadline_at <= int(datetime.now(timezone.utc).timestamp() * 1000)):
        raise NativeServiceBlocked("native_memory_actual_source_call_changed")
    budget = source.admission.header_budget
    if type(budget) is not HeaderReadBudget:
        raise NativeServiceBlocked("native_memory_source_budget_unavailable")
    return budget


async def _validate_memory_dispatch_source(db, run, scope):
    """Current SQL writer checks remain required after private issuance."""
    from src.auth.ownership import _current_root
    from .dispatch import OriginalServiceInvocation, _witness
    from .ownership import validate_invocation
    if type(scope) is not OriginalServiceInvocation:
        raise NativeServiceBlocked("native_memory_actual_source_required")
    source = scope.native_memory_source
    if (type(source) is not _NativeMemoryDispatchSource or source._seal is not _MEMORY_SOURCE_SEAL
            or source._issued_id != id(source) or _MEMORY_SOURCES.get(id(source)) is not source
            or not source._live.is_set() or source._scope is not scope
            or source.report_source is not scope.native_memory_report_source):
        raise NativeServiceBlocked("native_memory_actual_source_unavailable")
    host, claim = source.host, source.claim
    if (not host.admitting or host.reviewed is None or claim._host is not host
            or host.boot_nonce != scope.host_boot_nonce or claim.host_boot_nonce != host.boot_nonce
            or host.reviewed.package_digest != scope.witness["package_digest"]
            or host.reviewed.composition_digest != scope.witness["host_composition_digest"]):
        raise NativeServiceBlocked("native_memory_actual_source_host_changed")
    current = _witness(run, scope)
    candidate = source.admission.candidate()
    context = memory_context(run)
    if (current != dict(scope.witness) or context["candidate"] != candidate
            or run.run_identity != claim.job["job_id"] or run.status != "running"
            or run.lease_owner != current["lease_owner"] or run.fencing_token != current["fencing_token"]
            or run.attempt_count != current["attempt_count"] or run.input_digest != source.admission.candidate_digest
            or _utc(run.deadline_at) <= datetime.now(timezone.utc)
            or run.composition_binding_json != claim.binding.to_json()):
        raise NativeServiceBlocked("native_memory_actual_source_claim_changed")
    from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, OPERATOR_SESSION
    budget = source.admission.header_budget
    if type(budget) is not HeaderReadBudget:
        raise NativeServiceBlocked("native_memory_source_budget_unavailable")
    try:
        await budget.certify(db, OPERATOR_SESSION, (source.operator.session_id,))
    except HeaderBoundsError as error:
        raise NativeServiceBlocked("native_memory_source_bound_not_certified") from error
    await _current_root(db, source.operator)
    await validate_invocation(db, claim.binding, header_budget=budget)
    return source


@dataclass(frozen=True, eq=False)
class _MemoryOwnerSourceOperation:
    """One genuine owner transaction, with an actual returned effect identity."""
    source: _NativeMemoryDispatchSource = field(repr=False)
    scope: object = field(repr=False)
    db: object = field(repr=False)
    transaction: object = field(repr=False)
    driver: object = field(repr=False)
    run: object = field(repr=False)
    _seal: object = field(default=None, init=False, repr=False)
    _issued_id: int = field(default=0, init=False, repr=False)
    _effect: object = field(default=None, init=False, repr=False)
    _prepared: object = field(default=None, init=False, repr=False)


async def _begin_memory_owner_source(db, run, scope, *, prepared=None):
    from src.memory.header_bounds import _connection_state
    source = await _validate_memory_dispatch_source(db, run, scope)
    transaction, driver, _changes = await _connection_state(db)
    operation = _MemoryOwnerSourceOperation(source, scope, db, transaction, driver, run)
    if prepared is not None:
        from src.memory.m5 import validate_native_memory_mutation_plan
        await validate_native_memory_mutation_plan(prepared, db=db,
            candidate_digest=source.admission.candidate_digest)
        object.__setattr__(operation, "_prepared", prepared)
    object.__setattr__(operation, "_seal", _MEMORY_SOURCE_SEAL)
    object.__setattr__(operation, "_issued_id", id(operation))
    _MEMORY_OPERATIONS[id(operation)] = operation
    return operation


async def _validate_memory_owner_source(db, run, operation, *, effect=None):
    from src.memory.header_bounds import _connection_state
    if (type(operation) is not _MemoryOwnerSourceOperation or operation._seal is not _MEMORY_SOURCE_SEAL
            or operation._issued_id != id(operation) or _MEMORY_OPERATIONS.get(id(operation)) is not operation
            or operation.db is not db or operation.run is not run):
        raise NativeServiceBlocked("native_memory_actual_operation_unavailable")
    transaction, driver, _changes = await _connection_state(db)
    if transaction is not operation.transaction or driver is not operation.driver:
        raise NativeServiceBlocked("native_memory_actual_operation_stale")
    source = await _validate_memory_dispatch_source(db, run, operation.scope)
    if source is not operation.source or (effect is not None and operation._effect is not effect):
        raise NativeServiceBlocked("native_memory_actual_effect_unavailable")
    return source


async def _validate_memory_lifecycle_source(db, run, original_claim):
    """Resolve the still-live original dispatcher issuer, never a copied claim.

    execute_native_memory retains this registered source through completion and
    closes it in finally. Restart/recovery cannot reconstruct that registration.
    """
    from src.workflows.job_runtime import NativeServiceClaim
    if type(original_claim) is not NativeServiceClaim:
        raise NativeServiceBlocked("native_memory_actual_claim_source_required")
    sources = tuple(source for source in _MEMORY_SOURCES.values()
                    if source.claim is original_claim and source._live.is_set())
    if len(sources) != 1:
        raise NativeServiceBlocked("native_memory_actual_claim_source_unavailable")
    source = await _validate_memory_dispatch_source(db, run, sources[0]._scope)
    if source is not sources[0]:
        raise NativeServiceBlocked("native_memory_actual_claim_source_changed")
    return source


async def source_binding(db, *, principal_id, session_id, task_id, revision, attempt_id, header_budget=None):
    from src.db.models import Goal, WorkBoardTask
    from src.memory import m5
    from sqlalchemy import text
    from src.memory.header_bounds import (
        HeaderReadBudget, HeaderBoundsError, WORK_BOARD_TASK, WORK_BOARD_ATTEMPT,
        GOAL, WRS_BY_RUN, validate_certificate,
    )
    budget = header_budget if header_budget is not None else HeaderReadBudget()
    if type(budget) is not HeaderReadBudget:
        raise NativeServiceBlocked("native_memory_source_budget_unavailable")
    connection = await db.connection()
    driver = (await connection.get_raw_connection()).driver_connection
    if not driver.in_transaction:
        # The preparatory phase needs a real read snapshot on this exact
        # connection. It remains separate from the later original writer.
        await db.execute(text("BEGIN"))
    from src.memory.composition_headers import locate_exact_rows
    # Certify the exact original source rows before even their bounded
    # relationship projections. The later full-body appearance is charged
    # separately below on this same frame.
    try:
        for descriptor, identity in ((WORK_BOARD_TASK, task_id), (WORK_BOARD_ATTEMPT, attempt_id)):
            if not await locate_exact_rows(db, descriptor, identity, budget):
                raise NativeServiceBlocked("native_memory_original_source_changed")
            await budget.certify(db, descriptor, (identity,))
    except HeaderBoundsError as error:
        raise NativeServiceBlocked("native_memory_source_bound_not_certified") from error
    # Locators are selected without Task/Attempt JSON or free text. Every full
    # source row is certified before the first ORM body/proof reader.
    def bounded(column):
        return (f"CASE WHEN typeof({column})='text' THEN CASE WHEN "
                f"octet_length({column}) BETWEEN 1 AND 512 THEN {column} END END")
    goal_id = await db.scalar(text(f"SELECT {bounded('goal_id')} FROM work_board_tasks "
                                  "WHERE task_id=:id LIMIT 1"), {"id": task_id})
    run_id = await db.scalar(text(f"SELECT {bounded('workflow_run_id')} FROM work_board_attempts "
                                 "WHERE attempt_id=:id LIMIT 1"), {"id": attempt_id})
    if type(goal_id) is not str or type(run_id) is not str:
        raise NativeServiceBlocked("native_memory_original_source_changed")
    certificates = []
    try:
        for descriptor, identity in ((WORK_BOARD_TASK, task_id), (WORK_BOARD_ATTEMPT, attempt_id),
                                     (GOAL, goal_id), (WRS_BY_RUN, run_id)):
            certificates.append(await budget.certify(db, descriptor, (identity,)))
        for certificate in certificates:
            await validate_certificate(db, certificate)
    except HeaderBoundsError as error:
        raise NativeServiceBlocked("native_memory_source_bound_not_certified") from error
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
    if (task is None or task.owner_principal_id != principal_id or task.owner_session_id != session_id
        or task.task_revision != revision):
        raise NativeServiceBlocked("native_memory_original_source_changed")
    if task.capability_id not in METADATA_SOURCE_CAPABILITIES | {REPORT_SOURCE_CAPABILITY}:
        # Other existing source owners reopen private files or require their
        # own staged source token. Their legacy routes retain that behavior.
        raise NativeServiceBlocked("native_memory_source_profile_unsupported")
    goal = await db.get(Goal, task.goal_id)
    if (goal is None or goal.owner_principal_id != principal_id or goal.owner_session_id != session_id
        or goal.revision != task.goal_revision or goal.status != "active"):
        raise NativeServiceBlocked("native_memory_original_goal_changed")
    if task.capability_id == REPORT_SOURCE_CAPABILITY:
        from src.db.models import WorkBoardAttempt, WorkflowRunState
        from src.work_board.review import native_report_memory_metadata
        attempt = await db.get(WorkBoardAttempt, attempt_id, populate_existing=True)
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity ==
            (attempt.workflow_run_id if attempt is not None else "")).execution_options(populate_existing=True))
        if attempt is None or run is None:
            raise NativeServiceBlocked("native_memory_original_source_changed")
        readback = await native_report_memory_metadata(db, task, attempt, run)
        proof = m5._source_proof_from_rows(task, attempt, run, readback)
    else:
        proof = await m5._verified_source(db, task, requested_attempt_id=attempt_id)
    return MemorySourceBinding(task_id=task.task_id, expected_task_revision=task.task_revision,
        attempt_id=proof.attempt.attempt_id, attempt_fence=proof.attempt.fencing_token,
        workflow_run_id=proof.attempt.workflow_run_id, workflow_run_revision=proof.run.revision,
        goal_id=task.goal_id, goal_revision=task.goal_revision, capability_id=task.capability_id,
        capability_version=proof.capability_version, typed_input_digest=task.typed_input_digest,
        source_context_digest=proof.source_context_digest, evidence_digest=proof.evidence_digest,
        readback_digest=m5._proof_digest(proof.readback)).model_dump(mode="json"), proof


@dataclass(frozen=True)
class NativeMemoryReportSourceWitness:
    """Original private staging identity, not a serializable action grant."""
    candidate_digest: str
    row_tokens: tuple
    proof_json: str
    output_reference: str
    output_digest: str
    byte_count: int
    typed_input_json: str
    _identity: object = field(default=None, init=False, repr=False, compare=False)
    _consumed: bool = field(default=False, init=False, repr=False, compare=False)


@dataclass(frozen=True)
class _MemoryReportSourceIssuance:
    source_identity: int
    content_digest: str


def _memory_report_source_digest(source):
    return _digest({name: getattr(source, name) for name in ("candidate_digest", "row_tokens", "proof_json",
        "output_reference", "output_digest", "byte_count", "typed_input_json")})


@dataclass(frozen=True)
class _MemoryReportReadContext:
    source: NativeMemoryReportSourceWitness
    admission: NativeMemoryMutationAdmission
    writer: object
    authority_check: object


async def _memory_report_rows(db, proof, *, header_budget):
    from src.db.models import WorkBoardInputArtifact, Goal
    from src.work_board.pipelines import row_token
    task, attempt, run = proof.task, proof.attempt, proof.run
    from src.memory.header_bounds import INPUT_ARTIFACT, GOAL
    await header_budget.certify(db, INPUT_ARTIFACT, (task.input_artifact_id,))
    await header_budget.certify(db, GOAL, (task.goal_id,))
    artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id, populate_existing=True)
    goal = await db.get(Goal, task.goal_id, populate_existing=True)
    if artifact is None or goal is None:
        raise NativeServiceBlocked("native_memory_original_source_changed")
    return tuple((row.__tablename__, str(getattr(row, "task_id", None) if row is task else
        getattr(row, "attempt_id", None) if row is attempt else getattr(row, "run_identity", None)
        if row is run else getattr(row, "artifact_id", None) if row is artifact else row.id), row_token(row))
        for row in (task, attempt, run, artifact, goal))


async def stage_memory_report_source(db, admission):
    """Actual source/input/file reads occur before admission BEGIN."""
    if db.info.get("native_writer_started"):
        raise NativeServiceBlocked("native_memory_original_ingress_required")
    value = admission.candidate()
    if value["method"] == "memory.forget":
        return admission
    old = value["source"]
    if old["capability_id"] != REPORT_SOURCE_CAPABILITY:
        raise NativeServiceBlocked("native_memory_source_profile_unsupported")
    current, proof = await source_binding(db, principal_id=value["operator_principal_id"],
        session_id=value["operator_session_id"], task_id=old["task_id"],
        revision=old["expected_task_revision"], attempt_id=old["attempt_id"], header_budget=admission.header_budget)
    if current != old:
        raise NativeServiceBlocked("native_memory_original_source_changed")
    from src.work_board.dispatcher import _parse_typed_input
    from src.work_board.pipeline_cpu import read_output
    from src.runtime_plugins.task_capability import read_report_candidate, REPORT_CHECKPOINT_IDS, _journal
    metadata = read_report_candidate(proof.run)
    invoked = next(item["payload"] for item in _journal(proof.run.checkpoint_receipts_json)
        if item["checkpoint_id"] == REPORT_CHECKPOINT_IDS[2])
    inputs = _parse_typed_input(proof.task)
    from src.workflows.job_runtime import _digest as input_digest
    from src.work_board.pipeline_contracts import EvidenceConsumerInput
    handoffs = json.loads(proof.attempt.parent_handoff_context_json)
    safe_inputs = {"input": EvidenceConsumerInput.model_validate(dict(inputs)).model_dump(mode="json"),
        "parent_handoff_context": handoffs, "parent_handoff_digest": proof.attempt.parent_handoff_digest}
    if input_digest(safe_inputs) != metadata["input_digest"]:
        raise NativeServiceBlocked("native_memory_original_source_changed")
    raw = read_output(invoked["output_reference"], invoked["output_sha256"], max_bytes=65536,
                      header_budget=admission.header_budget)
    if len(raw) != invoked["size_bytes"]:
        raise NativeServiceBlocked("native_memory_original_source_changed")
    source = NativeMemoryReportSourceWitness(admission.candidate_digest,
        await _memory_report_rows(db, proof, header_budget=admission.header_budget),
        _canonical(proof.readback), invoked["output_reference"], invoked["output_sha256"], len(raw), _canonical(inputs))
    object.__setattr__(source, "_identity", _MemoryReportSourceIssuance(id(source), _memory_report_source_digest(source)))
    return replace(admission, report_source=source)


async def recheck_memory_report_source(db, task, attempt, context, *, read_current):
    """One adopted 64KiB private report read; no other writer file access."""
    if (type(context) is not _MemoryReportReadContext or context.writer is not db
        or type(context.source) is not NativeMemoryReportSourceWitness
        or type(context.source._identity) is not _MemoryReportSourceIssuance
        or context.source._identity.source_identity != id(context.source)
        or context.source._identity.content_digest != _memory_report_source_digest(context.source)
        or context.admission.report_source is not context.source
        or context.source.candidate_digest != context.admission.candidate_digest):
        raise NativeServiceBlocked("native_memory_original_report_witness_required")
    source = context.source
    value = context.admission.candidate()
    old = value["source"]
    if (task.task_id != old["task_id"] or attempt.attempt_id != old["attempt_id"]
        or old["capability_id"] != REPORT_SOURCE_CAPABILITY):
        raise NativeServiceBlocked("native_memory_original_source_changed")
    await context.authority_check(db)
    current, proof = await source_binding(db, principal_id=value["operator_principal_id"],
        session_id=value["operator_session_id"], task_id=old["task_id"],
        revision=old["expected_task_revision"], attempt_id=old["attempt_id"],
        header_budget=context.admission.header_budget)
    if (current != old or await _memory_report_rows(db, proof,
            header_budget=context.admission.header_budget) != source.row_tokens
        or _canonical(proof.readback) != source.proof_json
        or datetime.now(timezone.utc) >= datetime.fromisoformat(value["original_deadline"])):
        raise NativeServiceBlocked("native_memory_original_source_changed")
    if read_current:
        if source._consumed or not db.info.get("native_writer_started") or not db.in_transaction():
            raise NativeServiceBlocked("native_memory_original_report_witness_required")
        object.__setattr__(source, "_consumed", True)
        if not source.output_reference.startswith("artifacts/work-board/evidence/"):
            raise NativeServiceBlocked("native_memory_source_profile_unsupported")
        from src.work_board.pipeline_cpu import read_output
        import time
        began = time.monotonic()
        try:
            raw = read_output(source.output_reference, source.output_digest, max_bytes=65536,
                              header_budget=context.admission.header_budget)
        except (ValueError, OSError) as exc:
            raise NativeServiceBlocked("native_memory_report_readback_changed") from exc
        finally:
            db.info["native_memory_report_read_seconds"] = time.monotonic() - began
        if len(raw) != source.byte_count:
            raise NativeServiceBlocked("native_memory_report_readback_changed")
        await context.authority_check(db)
        if datetime.now(timezone.utc) >= datetime.fromisoformat(value["original_deadline"]):
            raise NativeServiceBlocked("native_memory_original_cutoff_expired")
    return dict(proof.readback)


async def _prepare_original_memory_text(db, value, budget):
    from src.memory import m5
    from src.memory.header_bounds import SECRET
    # The existing owner reads the complete Vault inventory before redaction,
    # in readonly redaction, and again to bind the unchanged inventory.
    for _ in range(3):
        await budget.certify_all(db, SECRET)
    return await m5.prepare_m5_text(db, value, header_budget=budget)


async def _consume_original_memory_text(db, value, prepared, budget):
    from src.memory import m5
    from src.memory.header_bounds import SECRET
    await budget.certify_all(db, SECRET)
    return await m5._consume_prepared_m5_text(db, value, prepared)


async def prepare_memory_admission(db, *, operator, method, request, idempotency_key,
                                   host_boot_nonce, composition_binding_digest, original_deadline,
                                   header_budget=None):
    """Authenticated ingress stages only data before its original admission writer."""
    from src.auth.service import AuthenticatedOperator
    from src.auth.ownership import _current_root
    from src.memory import m5
    from src.db.models import MemoryProposal, MemoryProposalStatus
    from dataclasses import asdict
    if (type(operator) is not AuthenticatedOperator or method not in MEMORY_METHODS
        or db.info.get("native_writer_started") or type(request) is not dict):
        raise NativeServiceBlocked("native_memory_original_ingress_required")
    from src.memory.header_bounds import HeaderReadBudget, OPERATOR_SESSION, MEMORY_DESCRIPTORS
    budget = header_budget if header_budget is not None else HeaderReadBudget()
    if type(budget) is not HeaderReadBudget:
        raise NativeServiceBlocked("native_memory_source_budget_unavailable")
    connection = await db.connection()
    if not (await connection.get_raw_connection()).driver_connection.in_transaction:
        from sqlalchemy import text
        await db.execute(text("BEGIN"))
    await budget.certify(db, OPERATOR_SESSION, (operator.session_id,))
    await _current_root(db, operator)
    if (original_deadline.tzinfo != timezone.utc or not 0 <
        (original_deadline - datetime.now(timezone.utc)).total_seconds() <= 30):
        raise NativeServiceBlocked("native_memory_original_cutoff_invalid")
    principal, session = operator.principal.principal_id, operator.session_id
    common = {"schema_version": 1, "method": method, "operator_principal_id": principal,
        "operator_session_id": session, "opaque_ref": "native-memory:" + _digest(
            {"principal": principal, "session": session, "method": method, "key": idempotency_key}),
        "idempotency_key": idempotency_key, "original_deadline": original_deadline.isoformat(),
        "host_boot_nonce": host_boot_nonce, "composition_binding_digest": composition_binding_digest}
    if method == "memory.forget":
        if set(request) != {"record_ref", "mode", "privacy_boundary", "reason"}:
            raise NativeServiceBlocked("native_memory_candidate_invalid")
        reason = request["reason"]
        if reason is not None and (type(reason) is not str or len(reason) > 500):
            raise NativeServiceBlocked("native_memory_candidate_invalid")
        prepared = None if reason is None else asdict(await _prepare_original_memory_text(db, reason, budget))
        admission = NativeMemoryMutationAdmission.from_candidate({**common, **request, "prepared_reason": prepared})
    else:
        if method == "memory.propose":
            if set(request) != {"task_id", "expected_task_revision", "attempt_id"}:
                raise NativeServiceBlocked("native_memory_candidate_invalid")
            source, proof = await source_binding(db, principal_id=principal, session_id=session,
                task_id=request["task_id"], revision=request["expected_task_revision"], attempt_id=request["attempt_id"], header_budget=budget)
            if source["capability_id"] != REPORT_SOURCE_CAPABILITY:
                raise NativeServiceBlocked("native_memory_source_profile_unsupported")
            admission = NativeMemoryMutationAdmission.from_candidate({**common, "source": source,
                "prepared_text": asdict(await _prepare_original_memory_text(db, m5._structured_source_candidate(proof), budget))})
        else:
            keys = {"proposal_id", "action", "expected_revision", "expected_preview_text_digest",
                "expected_task_revision", "expected_goal_revision", "edited_text", "decision_effect",
                "preferred_capability_id", "corrects_memory_id", "reason"}
            if set(request) != keys:
                raise NativeServiceBlocked("native_memory_candidate_invalid")
            await budget.certify(db, MEMORY_DESCRIPTORS["memory_proposals"], (request["proposal_id"],))
            proposal = await db.get(MemoryProposal, request["proposal_id"])
            if (proposal is None or proposal.owner_principal_id != principal or proposal.owner_session_id != session
                or proposal.schema_version != "memory_proposal.v1" or proposal.status != MemoryProposalStatus.proposed
                or proposal.revision != request["expected_revision"] or proposal.expires_at is None
                or proposal.goal_revision != request["expected_goal_revision"]
                or proposal.preview_text_digest != request["expected_preview_text_digest"]):
                raise NativeServiceBlocked("native_memory_original_review_changed")
            source, _ = await source_binding(db, principal_id=principal, session_id=session,
                task_id=proposal.source_task_id, revision=request["expected_task_revision"], attempt_id=proposal.source_attempt_id, header_budget=budget)
            if source["capability_id"] != REPORT_SOURCE_CAPABILITY:
                raise NativeServiceBlocked("native_memory_source_profile_unsupported")
            expires = m5._utc(proposal.expires_at)
            common["original_deadline"] = min(original_deadline, expires).isoformat()
            text = request["edited_text"] if request["edited_text"] is not None else proposal.preview_text or ""
            values = {key: item for key, item in request.items() if key not in {"expected_task_revision", "expected_goal_revision"}}
            admission = NativeMemoryMutationAdmission.from_candidate({**common, **values, "source": source,
                "proposal_schema": proposal.schema_version, "proposal_expires_at": expires.isoformat(),
                "prepared_text": asdict(await _prepare_original_memory_text(db, text, budget))})
    admission = replace(admission, header_budget=budget)
    await validate_original_memory_owner(db, admission)
    return await stage_memory_report_source(db, admission)


async def validate_original_memory_owner(db, admission):
    """Metadata-only recheck in the original authority/effect writer."""
    from src.memory import m5
    from src.db.models import Memory, MemoryProposal, MemoryProposalStatus, MemoryTombstone
    from src.memory.repository import _canonical_memory_deletion_marker
    if type(admission) is not NativeMemoryMutationAdmission:
        raise NativeServiceBlocked("native_memory_original_candidate_required")
    from src.memory.header_bounds import HeaderReadBudget, SESSION, MEMORY_DESCRIPTORS
    budget = admission.header_budget
    if type(budget) is not HeaderReadBudget:
        raise NativeServiceBlocked("native_memory_source_budget_unavailable")
    value = admission.candidate()
    await budget.certify(db, SESSION, (value["operator_session_id"],))
    await m5._require_original_m5_session(db, value["operator_principal_id"], value["operator_session_id"])
    if datetime.now(timezone.utc) >= datetime.fromisoformat(value["original_deadline"]):
        raise NativeServiceBlocked("native_memory_original_cutoff_expired")
    if value["method"] != "memory.forget":
        old = value["source"]
        current, proof = await source_binding(db, principal_id=value["operator_principal_id"],
            session_id=value["operator_session_id"], task_id=old["task_id"],
            revision=old["expected_task_revision"], attempt_id=old["attempt_id"], header_budget=admission.header_budget)
        if current != old:
            raise NativeServiceBlocked("native_memory_original_source_changed")
        original = m5._structured_source_candidate(proof)
        if value["method"] == "memory.applyReviewed":
            await budget.certify(db, MEMORY_DESCRIPTORS["memory_proposals"], (value["proposal_id"],))
            proposal = await db.get(MemoryProposal, value["proposal_id"])
            if (proposal is None or proposal.owner_principal_id != value["operator_principal_id"]
                or proposal.owner_session_id != value["operator_session_id"]
                or proposal.schema_version != value["proposal_schema"]
                or proposal.status != MemoryProposalStatus.proposed
                or proposal.revision != value["expected_revision"]
                or proposal.preview_text_digest != value["expected_preview_text_digest"]
                or proposal.expires_at is None
                or m5._utc(proposal.expires_at).isoformat() != value["proposal_expires_at"]
                or proposal.source_task_id != old["task_id"] or proposal.source_attempt_id != old["attempt_id"]):
                raise NativeServiceBlocked("native_memory_original_review_changed")
            original = value["edited_text"] if value["edited_text"] is not None else proposal.preview_text or ""
        await _consume_original_memory_text(db, original, m5.PreparedM5Text(**value["prepared_text"]), budget)
    else:
        from src.memory.composition_headers import locate_exact_rows
        if not await locate_exact_rows(db, MEMORY_DESCRIPTORS["memories"], value["record_ref"], budget):
            raise NativeServiceBlocked("native_memory_original_record_changed")
        await budget.certify(db, MEMORY_DESCRIPTORS["memories"], (value["record_ref"],))
        record = await db.get(Memory, value["record_ref"])
        from src.memory.composition_headers import locate_tombstones
        tombstone_ids = await locate_tombstones(db, value["record_ref"], budget)
        if tombstone_ids:
            await budget.certify(db, MEMORY_DESCRIPTORS["memory_tombstones"], tombstone_ids)
        if (record is None or record.source_session_id != value["operator_session_id"]
            or _canonical_memory_deletion_marker(record) is not None
            or await db.scalar(select(MemoryTombstone).where(MemoryTombstone.memory_id == record.id)) is not None):
            raise NativeServiceBlocked("native_memory_original_record_changed")
        if value["prepared_reason"] is not None:
            await _consume_original_memory_text(db, value["reason"], m5.PreparedM5Text(**value["prepared_reason"]), budget)
    return value


@dataclass(frozen=True)
class MemoryOwnerEffect:
    """Actual owner result; caller must seal it before committing this writer."""
    method: str
    candidate_digest: str
    status: str
    value_json: str | None
    audit_event_id: str | None
    proposal_id: str | None
    record_id: str | None


async def perform_memory_mutation(db, admission, *, authority_check, source_operation=None,
                                  native_prepared_mutation=None, native_memory_report_source=None):
    """No nested writer, Vault key/decrypt, inference, or public success adoption."""
    from src.memory import m5
    from src.memory.control import _forget_memory_in_session
    from src.audit.repository import audit_repository
    if not db.in_transaction() or not db.info.get("native_writer_started"):
        raise NativeServiceBlocked("native_memory_original_writer_required")
    if source_operation is not None:
        source = await _validate_memory_owner_source(db, source_operation.run, source_operation)
        if (source.admission is not admission or source_operation._effect is not None):
            raise NativeServiceBlocked("native_memory_actual_candidate_unavailable")
    if native_prepared_mutation is not None:
        if source_operation is None or source_operation._prepared is not native_prepared_mutation:
            raise NativeServiceBlocked("native_memory_actual_preparation_unavailable")
        await m5.validate_native_memory_mutation_plan(native_prepared_mutation, db=db,
            candidate_digest=admission.candidate_digest)
    await authority_check(db)
    value = await validate_original_memory_owner(db, admission)
    method = value["method"]
    native_report_source = native_memory_report_source
    if value.get("source", {}).get("capability_id") == REPORT_SOURCE_CAPABILITY:
        if (type(admission.report_source) is not NativeMemoryReportSourceWitness
            or admission.report_source.candidate_digest != admission.candidate_digest):
            raise NativeServiceBlocked("native_memory_original_report_witness_required")
        if native_report_source is None:
            native_report_source = _MemoryReportReadContext(admission.report_source, admission, db, authority_check)
        elif (type(native_report_source) is not _MemoryReportReadContext
              or native_report_source.source is not admission.report_source
              or native_report_source.admission is not admission or native_report_source.writer is not db):
            raise NativeServiceBlocked("native_memory_actual_report_context_unavailable")
    elif native_report_source is not None:
        raise NativeServiceBlocked("native_memory_actual_report_context_unavailable")
    principal, session = value["operator_principal_id"], value["operator_session_id"]
    proposal_id = record_id = audit_id = None
    result_value = None
    if method == "memory.propose":
        source = value["source"]
        result = await m5._create_memory_proposal_in_session(db, owner_principal_id=principal,
            owner_session_id=session, task_id=source["task_id"],
            expected_task_revision=source["expected_task_revision"], attempt_id=source["attempt_id"],
            prepared_text=m5.PreparedM5Text(**value["prepared_text"]), require_original_session=True,
            native_memory_report_source=native_report_source,
            native_prepared_mutation=native_prepared_mutation)
        proposal_id = result["proposal_id"]
        event = await audit_repository._log_event_in_session(db, actor=principal, session_id=session,
            event_type="memory_learning_proposed", tool_name="memory_control", policy_mode="operator_controlled",
            summary="Operator requested verified work-board memory",
            details={"proposal_id": proposal_id, "source_task_id": source["task_id"],
                "source_attempt_id": source["attempt_id"], "status": result["status"]},
            **({"_prepared_event": native_prepared_mutation.audit_event}
               if native_prepared_mutation is not None else {}))
        audit_id = event.id
        if result["status"] == "proposed":
            result_value = {"proposal_ref": proposal_id, "revision": result["revision"], "state": "proposed"}
    elif method == "memory.applyReviewed":
        source = value["source"]
        result = await m5._apply_memory_proposal_action_in_session(db, owner_principal_id=principal,
            owner_session_id=session, proposal_id=value["proposal_id"], action=value["action"],
            expected_revision=value["expected_revision"], expected_preview_text_digest=value["expected_preview_text_digest"],
            expected_task_revision=source["expected_task_revision"], expected_goal_revision=source["goal_revision"],
            edited_text=value["edited_text"], decision_effect=value["decision_effect"], reason=value["reason"],
            corrects_memory_id=value["corrects_memory_id"], preferred_capability_id=value["preferred_capability_id"],
            prepared_text=m5.PreparedM5Text(**value["prepared_text"]), require_original_session=True,
            native_memory_report_source=native_report_source,
            native_prepared_mutation=native_prepared_mutation)
        proposal_id = result["proposal_id"]
        audit_id = result.get("audit_event_id")
        if result["status"] == "accepted" and audit_id is not None and not result.get("idempotent_replay"):
            record_id = result["accepted_memory_id"]
            result_value = {"record_ref": record_id}
    else:
        # The genuine sealed preparation already consumed/checked this exact
        # reason with the original Vault/read budget. Its original owner apply
        # branch ignores this argument and uses the same prepared audit/update.
        reason = None if native_prepared_mutation is not None or value["prepared_reason"] is None else await _consume_original_memory_text(
            db, value["reason"], m5.PreparedM5Text(**value["prepared_reason"]), admission.header_budget)
        result = await _forget_memory_in_session(db, owner_session_id=session, actor=principal,
            memory_id=value["record_ref"], mode=value["mode"], reason=reason,
            privacy_boundary=value["privacy_boundary"],
            native_memory_report_source=native_report_source,
            native_prepared_mutation=native_prepared_mutation)
        record_id = result["memory"]["id"]
        audit_id = result["audit_event_id"]
        result_value = {"record_ref": record_id}
    # Current original authority/cutoff wins even after the owner effect. Caller
    # commits only together with its protected operation result/retention seal.
    await authority_check(db)
    if native_prepared_mutation is not None:
        await m5.validate_native_memory_mutation_plan(native_prepared_mutation, db=db,
            candidate_digest=admission.candidate_digest, consumed=True)
    if datetime.now(timezone.utc) >= datetime.fromisoformat(value["original_deadline"]):
        raise NativeServiceBlocked("native_memory_original_cutoff_expired")
    effect = MemoryOwnerEffect(method, admission.candidate_digest,
        "succeeded" if result_value is not None else "blocked",
        None if result_value is None else _canonical(result_value), audit_id, proposal_id, record_id)
    if source_operation is not None:
        await _validate_memory_owner_source(db, source_operation.run, source_operation)
        object.__setattr__(source_operation, "_effect", effect)
    return effect


def candidate_context(admission):
    if type(admission) is not NativeMemoryMutationAdmission:
        raise NativeServiceBlocked("native_memory_original_candidate_required")
    return _canonical({"schema_version": 1, "context_tag": MEMORY_CONTEXT_TAG,
        "candidate": admission.candidate(), "candidate_digest": admission.candidate_digest, "result": None})


def memory_context(run):
    from .ownership import RuntimeCompositionBinding
    try:
        from src.memory.header_bounds import strict_json_loads
        context = strict_json_loads(run.checkpoint_context_json)
        if (type(context) is not dict or set(context) != {"schema_version", "context_tag", "candidate", "candidate_digest", "result"}
            or type(context["schema_version"]) is not int or context["schema_version"] != 1
            or context["context_tag"] != MEMORY_CONTEXT_TAG):
            raise ValueError("context")
        admission = NativeMemoryMutationAdmission.from_candidate(context["candidate"])
        candidate = admission.candidate()
        binding = RuntimeCompositionBinding.from_json(run.composition_binding_json)
        if (run.job_kind != MEMORY_JOB_KIND or run.capability_version != "1" or run.max_attempts != 1
            or run.owner_kind != "user" or run.parent_job_id is not None or run.source_task_id is not None
            or run.owner_principal_id != candidate["operator_principal_id"]
            or run.operator_session_id != candidate["operator_session_id"] or run.session_id != run.operator_session_id
            or run.conversation_id != run.operator_session_id
            or context["candidate_digest"] != admission.candidate_digest or run.input_digest != admission.candidate_digest
            or binding.origin_method != candidate["method"] or binding.native_branch != "base"
            or binding.binding_digest != candidate["composition_binding_digest"]
            or _utc(run.deadline_at).isoformat() != candidate["original_deadline"]
            or (run.goal_id, run.goal_revision) != ((candidate["source"]["goal_id"], candidate["source"]["goal_revision"])
                if "source" in candidate else (None, None))):
            raise ValueError("binding")
        result = context["result"]
        if result is not None:
            from .contracts import validate_result
            if (type(result) is not dict or set(result) != {"invocation_ref", "claim_ref", "candidate_digest", "result_digest", "payload", "effect", "retention"}
                or result["invocation_ref"] != run.run_identity or result["candidate_digest"] != admission.candidate_digest
                or result["result_digest"] != _digest(result["payload"])):
                raise ValueError("result")
            validate_result(candidate["method"], result["payload"])
            effect = MemoryOwnerEffect(**result["effect"])
            if effect.method != candidate["method"] or effect.candidate_digest != admission.candidate_digest:
                raise ValueError("effect")
            validate_memory_retention_locator(result["retention"], run.run_identity)
        return context
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise NativeServiceBlocked("native_memory_private_context_changed") from exc


def validate_memory_retention_locator(locator, invocation_ref):
    """Closed durable address only; resolving it never recreates a live Source."""
    from src.workspace.accounting_witness import MEMORY_REFERENCE_PROFILE, MEMORY_ORIGINAL_CHECKPOINT
    if (type(locator) is not dict
        or set(locator) != {"profile", "invocation_ref", "original_checkpoint_id", "original_checkpoint_digest"}
        or locator["profile"] != MEMORY_REFERENCE_PROFILE
        or locator["invocation_ref"] != invocation_ref
        or locator["original_checkpoint_id"] != MEMORY_ORIGINAL_CHECKPOINT
        or type(locator["original_checkpoint_digest"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", locator["original_checkpoint_digest"]) is None):
        raise ValueError("native_memory_retention_locator_invalid")
    return locator


def _utc(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


async def validate_memory_spec(db, spec, admission):
    from src.auth.service import authenticate_principal
    from .ownership import method_dependencies
    candidate = await validate_original_memory_owner(db, admission)
    await certify_original_memory_principal(db, candidate["operator_principal_id"], admission.header_budget)
    principal = await authenticate_principal(candidate["operator_principal_id"], db=db)
    binding = spec.composition_binding
    goals = (candidate["source"]["goal_id"], candidate["source"]["goal_revision"]) if "source" in candidate else (None, None)
    if (spec.inputs != candidate or spec.identity.job_kind != MEMORY_JOB_KIND or spec.identity.capability_version != "1"
        or spec.identity.owner_kind != "user" or spec.identity.owner_principal_id != candidate["operator_principal_id"]
        or spec.session_id != candidate["operator_session_id"] or spec.operator_session_id != spec.session_id
        or spec.conversation_id != spec.session_id or spec.max_attempts != 1
        or spec.parent_job_id is not None or spec.source_task_id is not None
        or (spec.goal_id, spec.goal_revision) != goals or spec.dependencies or spec.resource_claims
        or binding is None or binding.origin_method != candidate["method"] or binding.native_branch != "base"
        or binding.binding_digest != candidate["composition_binding_digest"] or binding.host_package_digest is None
        or tuple(item.runtime_domain for item in binding.dependency_vector) != method_dependencies(candidate["method"], goal_bound=goals[0] is not None)
        or _utc(spec.deadline_at).isoformat() != candidate["original_deadline"]
        or _utc(spec.deadline_at) > datetime.now(timezone.utc) + timedelta(seconds=30)
        or not db.info.get("native_writer_started") or db.info.get("composition_writer_owner") != "durable_jobs"
        or spec.declared_authority != {"principal": candidate["operator_principal_id"], "owner_kind": "user",
            "session_id": candidate["operator_session_id"],
            "grants": sorted(str(getattr(grant, "value", grant)) for grant in principal.principal.grants)}):
        raise NativeServiceBlocked("native_memory_original_spec_changed")


async def native_memory_spec(db, *, admission, operator, binding):
    from src.auth.ownership import _current_root
    from src.auth.service import authenticate_principal
    from src.workflows.job_runtime import DurableJobSpec, DurableJobIdentity
    from src.memory.header_bounds import OPERATOR_SESSION
    await admission.header_budget.certify(db, OPERATOR_SESSION, (operator.session_id,))
    await _current_root(db, operator)
    candidate = admission.candidate()
    await certify_original_memory_principal(db, operator.principal.principal_id, admission.header_budget)
    principal = await authenticate_principal(operator.principal.principal_id, db=db)
    if (operator.principal.principal_id != candidate["operator_principal_id"]
        or operator.session_id != candidate["operator_session_id"] or principal.principal.grants != operator.principal.grants):
        raise NativeServiceBlocked("native_memory_original_owner_changed")
    job_id = memory_job_id(operator, candidate["method"], candidate["idempotency_key"])
    goal = candidate.get("source", {})
    spec = DurableJobSpec(identity=DurableJobIdentity(job_id=job_id, owner_kind="user",
        owner_principal_id=operator.principal.principal_id, job_kind=MEMORY_JOB_KIND, capability_version="1",
        idempotency_scope="native-memory-mutation", idempotency_key=candidate["idempotency_key"]),
        inputs=candidate, session_id=operator.session_id, operator_session_id=operator.session_id,
        conversation_id=operator.session_id, composition_binding=binding,
        goal_id=goal.get("goal_id"), goal_revision=goal.get("goal_revision"),
        declared_authority={"principal": operator.principal.principal_id, "owner_kind": "user", "session_id": operator.session_id,
            "grants": sorted(str(getattr(grant, "value", grant)) for grant in principal.principal.grants)},
        deadline_at=datetime.fromisoformat(candidate["original_deadline"]), max_attempts=1,
        run_fingerprint=_digest({"candidate": admission.candidate_digest, "binding": binding.binding_digest}))
    await validate_memory_spec(db, spec, admission)
    return spec


def memory_job_id(operator, method, key):
    return "native-memory:" + _digest({"principal": operator.principal.principal_id,
        "session": operator.session_id, "method": method, "key": key})


async def certify_original_memory_principal(db, principal_id, budget):
    """Numeric cover for the original principal and reverse-link projections.

    Conservative full OperatorSession inventory avoids a second authentication
    selector or authority path. Original authenticate_principal still decides
    which live root and ownership metadata qualify.
    """
    from src.memory.header_bounds import HeaderReadBudget, OPERATOR_SESSION
    if (type(budget) is not HeaderReadBudget or type(principal_id) is not str
            or not 1 <= len(principal_id.encode("utf-8")) <= 512):
        raise NativeServiceBlocked("native_memory_source_budget_unavailable")
    await budget.certify_all(db, OPERATOR_SESSION)
    # _ownership_metadata retrieves at most two non-tombstone ids and one
    # impossible tombstone id. Reserve their complete JSON string appearances
    # independently of the full root projection certified above.
    budget.debit(3 * (6 * 512 + 2) + 2)


def original_request(candidate):
    """Compare a retry's original authenticated input without renewing staging."""
    method = candidate["method"]
    if method == "memory.propose":
        return {key: candidate["source"][key] for key in ("task_id", "expected_task_revision", "attempt_id")}
    if method == "memory.forget":
        return {key: candidate[key] for key in ("record_ref", "mode", "privacy_boundary", "reason")}
    return {**{key: candidate[key] for key in ("proposal_id", "action", "expected_revision",
        "expected_preview_text_digest", "edited_text", "decision_effect", "preferred_capability_id", "corrects_memory_id", "reason")},
        "expected_task_revision": candidate["source"]["expected_task_revision"],
        "expected_goal_revision": candidate["source"]["goal_revision"]}


async def execute_native_memory(*, operator, method, request, idempotency_key):
    from src.db.engine import get_session
    from src.db.models import WorkflowRunState
    from src.auth.ownership import _current_root
    from src.workflows.job_runtime import durable_job_repository as jobs, DurableJobError
    from .bridge import cordis_host as host, HostBlocked
    from .ownership import begin_native_writer, bind_invocation
    if (method not in MEMORY_METHODS or not host.admitting or host.reviewed is None
        or type(idempotency_key) is not str or not re.fullmatch(r"[\x21-\x7e]{1,128}", idempotency_key)):
        raise NativeServiceBlocked("native_memory_original_host_unavailable")
    from src.workspace import accounting_witness
    if any(not callable(getattr(accounting_witness, name, None)) for name in
        ("begin_native_memory_effect", "capture_native_memory_retention", "validate_native_memory_retention")):
        raise NativeServiceBlocked("native_memory_retention_unavailable")
    reviewed, boot = host.reviewed, host.boot_nonce
    job_id = memory_job_id(operator, method, idempotency_key)
    from src.memory.header_bounds import HeaderReadBudget, OPERATOR_SESSION, WRS_BY_RUN
    from src.memory.composition_headers import locate_exact_rows
    budget = HeaderReadBudget()
    async with get_session() as db:
        from sqlalchemy import text
        connection = await db.connection()
        if not (await connection.get_raw_connection()).driver_connection.in_transaction:
            await db.execute(text("BEGIN"))
        await budget.certify(db, OPERATOR_SESSION, (operator.session_id,))
        await _current_root(db, operator)
        prior_ids = await locate_exact_rows(db, WRS_BY_RUN, job_id, budget)
        prior = None
        if prior_ids:
            await budget.certify(db, WRS_BY_RUN, prior_ids)
            prior = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
        if prior is not None:
            context = memory_context(prior)
            if (context["candidate"]["operator_principal_id"] != operator.principal.principal_id
                or context["candidate"]["operator_session_id"] != operator.session_id
                or original_request(context["candidate"]) != request):
                raise NativeServiceBlocked("native_memory_idempotency_input_changed")
            # Never private result release or another call from a fresh request.
            return {"job": await jobs.get_job(job_id, header_budget=budget), "replayed": True, "memory_status": "no_learning"}
        binding = await bind_invocation(db, method=method, native_branch="base",
            goal_bound=method != "memory.forget", reviewed_composition=reviewed, header_budget=budget)
        admission = await prepare_memory_admission(db, operator=operator, method=method, request=request,
            idempotency_key=idempotency_key, host_boot_nonce=boot, composition_binding_digest=binding.binding_digest,
            original_deadline=datetime.now(timezone.utc) + timedelta(seconds=30), header_budget=budget)
        await db.rollback()  # Staged Vault I/O is complete before BEGIN.
        await begin_native_writer(db, owner="durable_jobs", header_budget=budget)
        async def admission_check(writer, _run):
            await admission.header_budget.certify(writer, OPERATOR_SESSION, (operator.session_id,))
            await _current_root(writer, operator)
            await validate_original_memory_owner(writer, admission)
        spec = await native_memory_spec(db, admission=admission, operator=operator, binding=binding)
        job = await jobs._admit_in_session(db, spec, native_memory_admission=admission,
            native_memory_host=host, native_memory_host_boot_nonce=boot,
            admission_authority_check=admission_check)
    if job["status"] != "accepted":
        return {"job": job, "replayed": True, "memory_status": "no_learning"}
    claim = None
    scope = None
    try:
        await jobs.transition_job(job_id, "queued", expected_revision=job["revision"], header_budget=budget)
        claim = await jobs.claim_service_job(job_id, host=host, owner="native-memory:" + boot[:24],
            lease_seconds=30, claim_authority_check=admission_check, header_budget=budget,
            native_memory_admission=admission)
        scope = _issue_memory_dispatch_source(claim, admission, operator, host)
        result = await host.request_service(method, admission.wire_inputs(), original_scope=scope)
        completed = await jobs.complete_native_memory(claim, result=result, header_budget=budget)
        return {"job": completed, "result": result, "replayed": False, "memory_status": result["memory_status"]}
    except (NativeServiceBlocked, HostBlocked, DurableJobError) as exc:
        if claim is not None:
            witness = claim.checkpoint["payload"]
            try:
                await jobs.transition_job(job_id, "blocked", owner=witness["lease_owner"],
                    fencing_token=witness["fencing_token"], reason=getattr(exc, "reason_code", "native_memory_execution_blocked"))
            except DurableJobError:
                pass
        raise
    finally:
        if scope is not None:
            scope.native_memory_source.close()


async def native_memory_http(**kwargs):
    from fastapi import HTTPException
    from src.workflows.job_runtime import DurableJobError
    from src.auth.service import AuthFailure
    from src.work_board.repository import BoardError
    from .ownership import CompositionBindingError
    from src.memory.header_bounds import HeaderBoundsError
    try:
        return await execute_native_memory(**kwargs)
    except HeaderBoundsError as exc:
        raise HTTPException(status_code=409, detail={"code": "canonical_bound_not_certified"}) from exc
    except (NativeServiceBlocked, DurableJobError, AuthFailure, BoardError, CompositionBindingError, ValueError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail={"code": getattr(exc, "reason_code", "native_memory_original_unavailable")}) from exc


async def dispatch_memory_mutation(dispatcher, db, run, witness, method, payload, original_scope):
    from src.workspace.accounting_witness import begin_native_memory_effect, capture_native_memory_retention
    from .contracts import succeeded, blocked
    from dataclasses import asdict
    context = memory_context(run)
    source = await _validate_memory_dispatch_source(db, run, original_scope)
    # The original admission carries the operation's cumulative read budget
    # and staged producer. Candidate JSON cannot reconstruct either object.
    admission = source.admission
    if (context["result"] is not None or method != context["candidate"]["method"]
        or payload != admission.wire_inputs()):
        raise NativeServiceBlocked("native_memory_original_inputs_changed")
    async def authority(writer):
        from .dispatch import _witness, _ms
        from .bridge import cordis_host as current_host
        from .ownership import validate_invocation
        from src.auth.service import authenticate_principal
        from src.db.models import OperatorSession
        from src.workflows.job_runtime import _assert_canonical_goal_fence
        current_witness = _witness(run, original_scope)
        if (not current_host.admitting or current_host.reviewed is None
            or current_host.boot_nonce != original_scope.host_boot_nonce
            or current_host.reviewed.package_digest != witness["package_digest"]
            or current_host.reviewed.composition_digest != witness["host_composition_digest"]
            or current_witness != witness or dict(original_scope.witness) != witness
            or run.status != "running" or run.lease_owner != witness["lease_owner"]
            or run.fencing_token != witness["fencing_token"] or run.attempt_count != witness["attempt_count"]
            or run.input_digest != witness["input_digest"] or run.authority_digest != witness["authority_digest"]
            or run.run_fingerprint != witness["run_fingerprint"]
            or _ms(run.deadline_at) != witness["original_deadline_at"]
            or _ms(run.deadline_at) <= int(datetime.now(timezone.utc).timestamp() * 1000)):
            raise NativeServiceBlocked("native_memory_original_claim_changed")
        dispatcher.jobs._assert_lease(run, owner=witness["lease_owner"], fencing_token=witness["fencing_token"])
        await validate_invocation(writer, original_scope.binding, header_budget=admission.header_budget)
        from src.memory.header_bounds import OPERATOR_SESSION
        await admission.header_budget.certify(writer, OPERATOR_SESSION, (run.operator_session_id,))
        now = datetime.now(timezone.utc)
        root = await writer.scalar(select(OperatorSession).where(
            OperatorSession.id == run.operator_session_id, OperatorSession.principal_id == run.owner_principal_id,
            OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
            OperatorSession.is_bearer_tombstone.is_(False), OperatorSession.idle_expires_at > now,
            OperatorSession.absolute_expires_at > now).execution_options(populate_existing=True))
        await certify_original_memory_principal(writer, run.owner_principal_id, admission.header_budget)
        current = await authenticate_principal(run.owner_principal_id, db=writer)
        if (root is None or context["candidate"]["host_boot_nonce"] != original_scope.host_boot_nonce
            or json.loads(run.declared_authority_json).get("grants") != sorted(
                str(getattr(grant, "value", grant)) for grant in current.principal.grants)):
            raise NativeServiceBlocked("native_memory_original_root_changed")
        await _assert_canonical_goal_fence(writer, goal_id=run.goal_id, goal_revision=run.goal_revision,
            owner_kind=run.owner_kind, owner_principal_id=run.owner_principal_id,
            session_id=run.session_id, authority=run.declared_authority_json)
    from src.memory import m5
    await authority(db)
    report_context = None
    if admission.candidate().get("source", {}).get("capability_id") == REPORT_SOURCE_CAPABILITY:
        report_context = _MemoryReportReadContext(admission.report_source, admission, db, authority)
    prepared = await m5.prepare_native_memory_mutation(db, admission=admission,
        native_memory_report_source=report_context)
    operation = await _begin_memory_owner_source(db, run, original_scope, prepared=prepared)
    await begin_native_memory_effect(db, run, witness["claim_ref"], admission,
        source_operation=operation, prepared=prepared)
    effect = await perform_memory_mutation(db, admission, authority_check=authority,
        source_operation=operation, native_prepared_mutation=prepared,
        native_memory_report_source=report_context)
    if effect.status == "succeeded":
        value = json.loads(effect.value_json)
        if method != "memory.propose":
            value["receipt_ref"] = "native-memory-receipt:" + _digest({"invocation": run.run_identity,
                "claim": witness["claim_ref"], "candidate": admission.candidate_digest})
        result = succeeded(method, value)
    else:
        result = blocked("native_memory_owner_outcome_unsupported")
    # Capture stages the complete actual owner result before computing Current.
    # Appending this context after capture would immediately stale its own WRS.
    capture = await capture_native_memory_retention(db, run, witness["claim_ref"], effect,
        source_operation=operation, prepared=prepared, result=result)
    retention = validate_memory_retention_locator(capture.payload(), run.run_identity)
    expected = {"invocation_ref": run.run_identity, "claim_ref": witness["claim_ref"],
        "candidate_digest": admission.candidate_digest, "result_digest": _digest(result), "payload": result,
        "effect": asdict(effect), "retention": retention}
    if memory_context(run)["result"] != expected:
        raise NativeServiceBlocked("native_memory_retention_result_readback_changed")
    await authority(db)
    return result


@dataclass(frozen=True)
class NativeMemoryOwnerReference:
    table: str
    row_ref: str


async def _memory_locator_rows(db, model, fields, *predicates, limit=129):
    """Closed owner locators only; never full proposal, alias or private bodies."""
    from sqlalchemy import case, func
    allowed = {
        "goals": {"id", "parent_id", "owner_principal_id", "owner_session_id"},
        "memory_proposals": {"proposal_id", "owner_principal_id", "owner_session_id",
                             "recovered_from_proposal_id", "proposal_job_id", "corrects_memory_id"},
        "work_board_proposals": {"proposal_id", "owner_principal_id", "owner_session_id"},
        "work_board_decision_receipts": {"receipt_id"},
        "memories": {"id", "source_session_id", "subject_entity_id", "project_entity_id"},
        "memory_entities": {"id"}, "memory_sources": {"id"}, "memory_tombstones": {"id"},
        "memory_edges": {"id", "from_memory_id", "to_memory_id"},
    }
    if (type(fields) is not tuple or not fields or not set(fields) <= allowed.get(model.__tablename__, set())
            or type(limit) is not int or not 1 <= limit <= 129):
        raise NativeServiceBlocked("native_memory_retention_locator_unavailable")
    columns = []
    for name in fields:
        column = getattr(model, name)
        bounded = case((func.typeof(column) == "text",
                        case((func.octet_length(column) <= 512, column))), else_=None)
        valid = case((column.is_(None), 1), (func.typeof(column) == "text",
                     case((func.octet_length(column) <= 512, 1), else_=0)), else_=0)
        columns.extend((bounded, valid))
    rows = list(await db.execute(select(*columns).where(*predicates).limit(limit)))
    result = []
    for row in rows:
        if any(row[index + 1] != 1 for index in range(0, len(row), 2)):
            raise NativeServiceBlocked("native_memory_retention_locator_bound")
        result.append(dict(zip(fields, row[::2])))
    return result


async def memory_owner_references(db, admission, effect):
    """Post-effect locators; the capture owner separately verifies its issuer."""
    if type(admission) is not NativeMemoryMutationAdmission or type(effect) is not MemoryOwnerEffect:
        raise NativeServiceBlocked("native_memory_retention_original_effect_required")
    if effect.method != admission.candidate()["method"] or effect.candidate_digest != admission.candidate_digest:
        raise NativeServiceBlocked("native_memory_retention_original_effect_changed")
    return await _memory_existing_reference_lookup(db, admission,
        proposal_id=effect.proposal_id, record_id=effect.record_id,
        require_baseline=effect.status == "succeeded")


async def prospective_memory_existing_references(db, run, source_operation):
    """Actual Source's existing selectors, without pretending an effect exists.

    Future owner rows/bytes still require their original pre-effect reservation.
    This helper alone cannot admit mutation or certify a prospective closure.
    """
    source = await _validate_memory_owner_source(db, run, source_operation)
    value = source.admission.candidate()
    references = await _memory_existing_reference_lookup(db, source.admission,
        proposal_id=value.get("proposal_id"),
        record_id=value.get("record_ref") or value.get("corrects_memory_id"),
        require_baseline=value["method"] == "memory.applyReviewed")
    return references


async def _memory_existing_reference_lookup(db, admission, *, proposal_id, record_id, require_baseline):
    from src.db.models import (Goal, Memory, MemoryEntity, MemoryProposal, MemorySource,
        MemoryEdge, MemoryTombstone, WorkBoardProposal, WorkBoardDecisionReceipt, WorkBoardDecisionReceiptStage)
    from sqlalchemy import or_
    value = admission.candidate()
    references = set()
    def add(table, key):
        if (type(key) is not str or not key or len(key.encode("utf-8")) > 512
                or ((table, key) not in references and len(references) >= 128)):
            raise NativeServiceBlocked("native_memory_retention_bound_exceeded")
        references.add((table, key))
    if "source" in value:
        goal_id = value["source"]["goal_id"]
        seen = set()
        while goal_id is not None:
            if goal_id in seen:
                raise NativeServiceBlocked("native_memory_goal_ancestry_changed")
            seen.add(goal_id)
            matches = await _memory_locator_rows(db, Goal,
                ("id", "parent_id", "owner_principal_id", "owner_session_id"), Goal.id == goal_id, limit=2)
            goal = matches[0] if len(matches) == 1 else None
            if (goal is None or goal["owner_principal_id"] != value["operator_principal_id"]
                or goal["owner_session_id"] != value["operator_session_id"]):
                raise NativeServiceBlocked("native_memory_goal_ancestry_changed")
            add("goals", goal["id"])
            goal_id = goal["parent_id"]
    memory_ids = set()
    if proposal_id is not None:
        matches = await _memory_locator_rows(db, MemoryProposal,
            ("proposal_id", "owner_principal_id", "owner_session_id", "recovered_from_proposal_id",
             "proposal_job_id", "corrects_memory_id"), MemoryProposal.proposal_id == proposal_id, limit=2)
        proposal = matches[0] if len(matches) == 1 else None
        if (proposal is None or proposal["owner_principal_id"] != value["operator_principal_id"]
            or proposal["owner_session_id"] != value["operator_session_id"]):
            raise NativeServiceBlocked("native_memory_retention_proposal_changed")
        add("memory_proposals", proposal["proposal_id"])
        if proposal["recovered_from_proposal_id"] is not None:
            raise NativeServiceBlocked("native_memory_recovery_profile_unsupported")
        if proposal["proposal_job_id"] is not None:
            board = await _memory_locator_rows(db, WorkBoardProposal,
                ("proposal_id", "owner_principal_id", "owner_session_id"),
                WorkBoardProposal.admission_job_id == proposal["proposal_job_id"], limit=2)
            if (len(board) != 1 or board[0]["owner_principal_id"] != value["operator_principal_id"]
                or board[0]["owner_session_id"] != value["operator_session_id"]):
                raise NativeServiceBlocked("native_memory_retention_board_proposal_changed")
            add("work_board_proposals", board[0]["proposal_id"])
        baselines = await _memory_locator_rows(db, WorkBoardDecisionReceipt, ("receipt_id",),
            WorkBoardDecisionReceipt.source_proposal_id == proposal["proposal_id"],
            WorkBoardDecisionReceipt.receipt_stage == WorkBoardDecisionReceiptStage.source_baseline, limit=2)
        if require_baseline and len(baselines) != 1:
            raise NativeServiceBlocked("native_memory_retention_baseline_changed")
        if len(baselines) > 1:
            raise NativeServiceBlocked("native_memory_retention_baseline_changed")
        for row in baselines:
            add("work_board_decision_receipts", row["receipt_id"])
        if proposal["corrects_memory_id"]:
            memory_ids.add(proposal["corrects_memory_id"])
    if record_id is not None:
        memory_ids.add(record_id)
    for key in sorted(memory_ids):
        matches = await _memory_locator_rows(db, Memory,
            ("id", "source_session_id", "subject_entity_id", "project_entity_id"), Memory.id == key, limit=2)
        record = matches[0] if len(matches) == 1 else None
        if record is None or record["source_session_id"] != value["operator_session_id"]:
            raise NativeServiceBlocked("native_memory_retention_record_changed")
        add("memories", record["id"])
        for entity_id in (record["subject_entity_id"], record["project_entity_id"]):
            if entity_id is not None:
                entities = await _memory_locator_rows(db, MemoryEntity, ("id",), MemoryEntity.id == entity_id, limit=2)
                if len(entities) != 1:
                    raise NativeServiceBlocked("native_memory_retention_entity_changed")
                add("memory_entities", entities[0]["id"])
        for cls, table in ((MemorySource, "memory_sources"), (MemoryTombstone, "memory_tombstones")):
            rows = await _memory_locator_rows(db, cls, ("id",), cls.memory_id == key)
            for row in rows:
                add(table, row["id"])
    if value["method"] == "memory.applyReviewed" and record_id is not None:
        edges = await _memory_locator_rows(db, MemoryEdge, ("id", "from_memory_id", "to_memory_id"), or_(
            MemoryEdge.from_memory_id == record_id, MemoryEdge.to_memory_id == record_id))
        for row in edges:
            if row["from_memory_id"] not in memory_ids or row["to_memory_id"] not in memory_ids:
                raise NativeServiceBlocked("native_memory_retention_edge_changed")
            add("memory_edges", row["id"])
    return tuple(NativeMemoryOwnerReference(table, key) for table, key in sorted(references))
