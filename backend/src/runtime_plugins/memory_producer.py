"""Private, finite original-owner candidates for the three Memory mutations.

The candidate is data, never authority. Admission/claim/dispatch must retain its
exact bytes on the one original job and publish the effect result in the same
writer. Only opaque references enter the stock service frame.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import select

from .dispatch import NativeServiceBlocked

MEMORY_JOB_KIND = "runtime_service_memory_v1"
MEMORY_METHODS = frozenset({"memory.propose", "memory.applyReviewed", "memory.forget"})
MEMORY_CONTEXT_TAG = "native-memory-mutation.v1"
MAX_CANDIDATE_BYTES = 16384
METADATA_SOURCE_CAPABILITIES = frozenset({"workflow.goal-snapshot-to-file"})


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


async def source_binding(db, *, principal_id, session_id, task_id, revision, attempt_id):
    from src.db.models import Goal, WorkBoardTask
    from src.memory import m5
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
    if (task is None or task.owner_principal_id != principal_id or task.owner_session_id != session_id
        or task.task_revision != revision):
        raise NativeServiceBlocked("native_memory_original_source_changed")
    if task.capability_id not in METADATA_SOURCE_CAPABILITIES:
        # Other existing source owners reopen private files or require their
        # own staged source token. Their legacy routes retain that behavior.
        raise NativeServiceBlocked("native_memory_source_profile_unsupported")
    goal = await db.get(Goal, task.goal_id)
    if (goal is None or goal.owner_principal_id != principal_id or goal.owner_session_id != session_id
        or goal.revision != task.goal_revision or goal.status != "active"):
        raise NativeServiceBlocked("native_memory_original_goal_changed")
    proof = await m5._verified_source(db, task, requested_attempt_id=attempt_id)
    return MemorySourceBinding(task_id=task.task_id, expected_task_revision=task.task_revision,
        attempt_id=proof.attempt.attempt_id, attempt_fence=proof.attempt.fencing_token,
        workflow_run_id=proof.attempt.workflow_run_id, workflow_run_revision=proof.run.revision,
        goal_id=task.goal_id, goal_revision=task.goal_revision, capability_id=task.capability_id,
        capability_version=proof.capability_version, typed_input_digest=task.typed_input_digest,
        source_context_digest=proof.source_context_digest, evidence_digest=proof.evidence_digest,
        readback_digest=m5._proof_digest(proof.readback)).model_dump(mode="json"), proof


async def prepare_memory_admission(db, *, operator, method, request, idempotency_key,
                                   host_boot_nonce, composition_binding_digest, original_deadline):
    """Authenticated ingress stages only data before its original admission writer."""
    from src.auth.service import AuthenticatedOperator
    from src.auth.ownership import _current_root
    from src.memory import m5
    from src.db.models import MemoryProposal, MemoryProposalStatus
    from dataclasses import asdict
    if (type(operator) is not AuthenticatedOperator or method not in MEMORY_METHODS
        or db.info.get("native_writer_started") or type(request) is not dict):
        raise NativeServiceBlocked("native_memory_original_ingress_required")
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
        prepared = None if reason is None else asdict(await m5.prepare_m5_text(db, reason))
        admission = NativeMemoryMutationAdmission.from_candidate({**common, **request, "prepared_reason": prepared})
    else:
        if method == "memory.propose":
            if set(request) != {"task_id", "expected_task_revision", "attempt_id"}:
                raise NativeServiceBlocked("native_memory_candidate_invalid")
            source, proof = await source_binding(db, principal_id=principal, session_id=session,
                task_id=request["task_id"], revision=request["expected_task_revision"], attempt_id=request["attempt_id"])
            admission = NativeMemoryMutationAdmission.from_candidate({**common, "source": source,
                "prepared_text": asdict(await m5.prepare_m5_text(db, m5._structured_source_candidate(proof)))})
        else:
            keys = {"proposal_id", "action", "expected_revision", "expected_preview_text_digest",
                "expected_task_revision", "expected_goal_revision", "edited_text", "decision_effect",
                "preferred_capability_id", "corrects_memory_id", "reason"}
            if set(request) != keys:
                raise NativeServiceBlocked("native_memory_candidate_invalid")
            proposal = await db.get(MemoryProposal, request["proposal_id"])
            if (proposal is None or proposal.owner_principal_id != principal or proposal.owner_session_id != session
                or proposal.schema_version != "memory_proposal.v1" or proposal.status != MemoryProposalStatus.proposed
                or proposal.revision != request["expected_revision"] or proposal.expires_at is None
                or proposal.goal_revision != request["expected_goal_revision"]
                or proposal.preview_text_digest != request["expected_preview_text_digest"]):
                raise NativeServiceBlocked("native_memory_original_review_changed")
            source, _ = await source_binding(db, principal_id=principal, session_id=session,
                task_id=proposal.source_task_id, revision=request["expected_task_revision"], attempt_id=proposal.source_attempt_id)
            expires = m5._utc(proposal.expires_at)
            common["original_deadline"] = min(original_deadline, expires).isoformat()
            text = request["edited_text"] if request["edited_text"] is not None else proposal.preview_text or ""
            values = {key: item for key, item in request.items() if key not in {"expected_task_revision", "expected_goal_revision"}}
            admission = NativeMemoryMutationAdmission.from_candidate({**common, **values, "source": source,
                "proposal_schema": proposal.schema_version, "proposal_expires_at": expires.isoformat(),
                "prepared_text": asdict(await m5.prepare_m5_text(db, text))})
    await validate_original_memory_owner(db, admission)
    return admission


async def validate_original_memory_owner(db, admission):
    """Metadata-only recheck in the original authority/effect writer."""
    from src.memory import m5
    from src.db.models import Memory, MemoryProposal, MemoryProposalStatus, MemoryTombstone
    from src.memory.repository import _canonical_memory_deletion_marker
    if type(admission) is not NativeMemoryMutationAdmission:
        raise NativeServiceBlocked("native_memory_original_candidate_required")
    value = admission.candidate()
    await m5._require_original_m5_session(db, value["operator_principal_id"], value["operator_session_id"])
    if datetime.now(timezone.utc) >= datetime.fromisoformat(value["original_deadline"]):
        raise NativeServiceBlocked("native_memory_original_cutoff_expired")
    if value["method"] != "memory.forget":
        old = value["source"]
        current, proof = await source_binding(db, principal_id=value["operator_principal_id"],
            session_id=value["operator_session_id"], task_id=old["task_id"],
            revision=old["expected_task_revision"], attempt_id=old["attempt_id"])
        if current != old:
            raise NativeServiceBlocked("native_memory_original_source_changed")
        original = m5._structured_source_candidate(proof)
        if value["method"] == "memory.applyReviewed":
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
        await m5._consume_prepared_m5_text(db, original, m5.PreparedM5Text(**value["prepared_text"]))
    else:
        record = await db.get(Memory, value["record_ref"])
        if (record is None or record.source_session_id != value["operator_session_id"]
            or _canonical_memory_deletion_marker(record) is not None
            or await db.scalar(select(MemoryTombstone).where(MemoryTombstone.memory_id == record.id)) is not None):
            raise NativeServiceBlocked("native_memory_original_record_changed")
        if value["prepared_reason"] is not None:
            await m5._consume_prepared_m5_text(db, value["reason"], m5.PreparedM5Text(**value["prepared_reason"]))
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


async def perform_memory_mutation(db, admission, *, authority_check):
    """No nested writer, Vault key/decrypt, inference, or public success adoption."""
    from src.memory import m5
    from src.memory.control import _forget_memory_in_session
    from src.audit.repository import audit_repository
    if not db.in_transaction() or not db.info.get("native_writer_started"):
        raise NativeServiceBlocked("native_memory_original_writer_required")
    await authority_check(db)
    value = await validate_original_memory_owner(db, admission)
    method = value["method"]
    principal, session = value["operator_principal_id"], value["operator_session_id"]
    proposal_id = record_id = audit_id = None
    result_value = None
    if method == "memory.propose":
        source = value["source"]
        result = await m5._create_memory_proposal_in_session(db, owner_principal_id=principal,
            owner_session_id=session, task_id=source["task_id"],
            expected_task_revision=source["expected_task_revision"], attempt_id=source["attempt_id"],
            prepared_text=m5.PreparedM5Text(**value["prepared_text"]), require_original_session=True)
        proposal_id = result["proposal_id"]
        event = await audit_repository._log_event_in_session(db, actor=principal, session_id=session,
            event_type="memory_learning_proposed", tool_name="memory_control", policy_mode="operator_controlled",
            summary="Operator requested verified work-board memory",
            details={"proposal_id": proposal_id, "source_task_id": source["task_id"],
                "source_attempt_id": source["attempt_id"], "status": result["status"]})
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
            prepared_text=m5.PreparedM5Text(**value["prepared_text"]), require_original_session=True)
        proposal_id = result["proposal_id"]
        audit_id = result.get("audit_event_id")
        if result["status"] == "accepted" and audit_id is not None and not result.get("idempotent_replay"):
            record_id = result["accepted_memory_id"]
            result_value = {"record_ref": record_id}
    else:
        reason = None if value["prepared_reason"] is None else await m5._consume_prepared_m5_text(
            db, value["reason"], m5.PreparedM5Text(**value["prepared_reason"]))
        result = await _forget_memory_in_session(db, owner_session_id=session, actor=principal,
            memory_id=value["record_ref"], mode=value["mode"], reason=reason,
            privacy_boundary=value["privacy_boundary"])
        record_id = result["memory"]["id"]
        audit_id = result["audit_event_id"]
        result_value = {"record_ref": record_id}
    # Current original authority/cutoff wins even after the owner effect. Caller
    # commits only together with its protected operation result/retention seal.
    await authority_check(db)
    if datetime.now(timezone.utc) >= datetime.fromisoformat(value["original_deadline"]):
        raise NativeServiceBlocked("native_memory_original_cutoff_expired")
    return MemoryOwnerEffect(method, admission.candidate_digest,
        "succeeded" if result_value is not None else "blocked",
        None if result_value is None else _canonical(result_value), audit_id, proposal_id, record_id)


def candidate_context(admission):
    if type(admission) is not NativeMemoryMutationAdmission:
        raise NativeServiceBlocked("native_memory_original_candidate_required")
    return _canonical({"schema_version": 1, "context_tag": MEMORY_CONTEXT_TAG,
        "candidate": admission.candidate(), "candidate_digest": admission.candidate_digest, "result": None})


def memory_context(run):
    from .ownership import RuntimeCompositionBinding
    try:
        context = json.loads(run.checkpoint_context_json)
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
        return context
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise NativeServiceBlocked("native_memory_private_context_changed") from exc


def _utc(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


async def validate_memory_spec(db, spec, admission):
    from src.auth.service import authenticate_principal
    from .ownership import method_dependencies
    candidate = await validate_original_memory_owner(db, admission)
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
    await _current_root(db, operator)
    candidate = admission.candidate()
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
    from .dispatch import capture_original_scope
    if (method not in MEMORY_METHODS or not host.admitting or host.reviewed is None
        or type(idempotency_key) is not str or not re.fullmatch(r"[\x21-\x7e]{1,128}", idempotency_key)):
        raise NativeServiceBlocked("native_memory_original_host_unavailable")
    from src.workspace import accounting_witness
    if any(not callable(getattr(accounting_witness, name, None)) for name in
        ("begin_native_memory_effect", "capture_native_memory_retention", "validate_native_memory_retention")):
        raise NativeServiceBlocked("native_memory_retention_unavailable")
    reviewed, boot = host.reviewed, host.boot_nonce
    job_id = memory_job_id(operator, method, idempotency_key)
    async with get_session() as db:
        await _current_root(db, operator)
        prior = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
        if prior is not None:
            context = memory_context(prior)
            if (context["candidate"]["operator_principal_id"] != operator.principal.principal_id
                or context["candidate"]["operator_session_id"] != operator.session_id
                or original_request(context["candidate"]) != request):
                raise NativeServiceBlocked("native_memory_idempotency_input_changed")
            # Never private result release or another call from a fresh request.
            return {"job": await jobs.get_job(job_id), "replayed": True, "memory_status": "no_learning"}
        binding = await bind_invocation(db, method=method, native_branch="base",
            goal_bound=method != "memory.forget", reviewed_composition=reviewed)
        admission = await prepare_memory_admission(db, operator=operator, method=method, request=request,
            idempotency_key=idempotency_key, host_boot_nonce=boot, composition_binding_digest=binding.binding_digest,
            original_deadline=datetime.now(timezone.utc) + timedelta(seconds=30))
        await db.rollback()  # Staged Vault I/O is complete before BEGIN.
        await begin_native_writer(db, owner="durable_jobs")
        async def admission_check(writer, _run):
            await _current_root(writer, operator)
            await validate_original_memory_owner(writer, admission)
        spec = await native_memory_spec(db, admission=admission, operator=operator, binding=binding)
        job = await jobs._admit_in_session(db, spec, native_memory_admission=admission,
            native_memory_host=host, native_memory_host_boot_nonce=boot,
            admission_authority_check=admission_check)
    if job["status"] != "accepted":
        return {"job": job, "replayed": True, "memory_status": "no_learning"}
    claim = None
    try:
        await jobs.transition_job(job_id, "queued", expected_revision=job["revision"])
        claim = await jobs.claim_service_job(job_id, host=host, owner="native-memory:" + boot[:24],
            lease_seconds=30, claim_authority_check=admission_check)
        scope = capture_original_scope(claim, host)
        result = await host.request_service(method, admission.wire_inputs(), original_scope=scope)
        completed = await jobs.complete_native_memory(claim, result=result)
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


async def native_memory_http(**kwargs):
    from fastapi import HTTPException
    from src.workflows.job_runtime import DurableJobError
    from src.auth.service import AuthFailure
    from src.work_board.repository import BoardError
    from .ownership import CompositionBindingError
    try:
        return await execute_native_memory(**kwargs)
    except (NativeServiceBlocked, DurableJobError, AuthFailure, BoardError, CompositionBindingError, ValueError, PermissionError) as exc:
        raise HTTPException(status_code=409, detail={"code": getattr(exc, "reason_code", "native_memory_original_unavailable")}) from exc


async def dispatch_memory_mutation(dispatcher, db, run, witness, method, payload, original_scope):
    from src.workspace.accounting_witness import begin_native_memory_effect, capture_native_memory_retention
    from .contracts import succeeded, blocked
    from dataclasses import asdict
    context = memory_context(run)
    admission = NativeMemoryMutationAdmission.from_candidate(context["candidate"])
    if (context["result"] is not None or method != context["candidate"]["method"]
        or payload != admission.wire_inputs()):
        raise NativeServiceBlocked("native_memory_original_inputs_changed")
    async def authority(writer):
        from .dispatch import _witness, _ms
        from .ownership import validate_invocation
        from src.auth.service import authenticate_principal
        from src.db.models import OperatorSession
        from src.workflows.job_runtime import _assert_canonical_goal_fence
        current_witness = _witness(run, original_scope)
        if (current_witness != witness or dict(original_scope.witness) != witness
            or run.status != "running" or run.lease_owner != witness["lease_owner"]
            or run.fencing_token != witness["fencing_token"] or run.attempt_count != witness["attempt_count"]
            or run.input_digest != witness["input_digest"] or run.authority_digest != witness["authority_digest"]
            or run.run_fingerprint != witness["run_fingerprint"]
            or _ms(run.deadline_at) != witness["original_deadline_at"]):
            raise NativeServiceBlocked("native_memory_original_claim_changed")
        dispatcher.jobs._assert_lease(run, owner=witness["lease_owner"], fencing_token=witness["fencing_token"])
        await validate_invocation(writer, original_scope.binding)
        now = datetime.now(timezone.utc)
        root = await writer.scalar(select(OperatorSession).where(
            OperatorSession.id == run.operator_session_id, OperatorSession.principal_id == run.owner_principal_id,
            OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
            OperatorSession.is_bearer_tombstone.is_(False), OperatorSession.idle_expires_at > now,
            OperatorSession.absolute_expires_at > now).execution_options(populate_existing=True))
        current = await authenticate_principal(run.owner_principal_id, db=writer)
        if (root is None or context["candidate"]["host_boot_nonce"] != original_scope.host_boot_nonce
            or json.loads(run.declared_authority_json).get("grants") != sorted(
                str(getattr(grant, "value", grant)) for grant in current.principal.grants)):
            raise NativeServiceBlocked("native_memory_original_root_changed")
        await _assert_canonical_goal_fence(writer, goal_id=run.goal_id, goal_revision=run.goal_revision,
            owner_kind=run.owner_kind, owner_principal_id=run.owner_principal_id,
            session_id=run.session_id, authority=run.declared_authority_json)
    await begin_native_memory_effect(db, run, witness["claim_ref"], admission)
    effect = await perform_memory_mutation(db, admission, authority_check=authority)
    capture = await capture_native_memory_retention(db, run, witness["claim_ref"], effect)
    retention = capture.payload()
    if effect.status == "succeeded":
        value = json.loads(effect.value_json)
        if method != "memory.propose":
            value["receipt_ref"] = "native-memory-receipt:" + _digest({"invocation": run.run_identity,
                "claim": witness["claim_ref"], "candidate": admission.candidate_digest})
        result = succeeded(method, value)
    else:
        result = blocked("native_memory_owner_outcome_unsupported")
    context["result"] = {"invocation_ref": run.run_identity, "claim_ref": witness["claim_ref"],
        "candidate_digest": admission.candidate_digest, "result_digest": _digest(result), "payload": result,
        "effect": asdict(effect), "retention": retention}
    run.checkpoint_context_json = _canonical(context)
    db.info["composition_native_memory_result"] = capture
    await authority(db)
    await db.flush()
    return result


@dataclass(frozen=True)
class NativeMemoryOwnerReference:
    table: str
    row_ref: str


async def memory_owner_references(db, admission, effect):
    """Select causal existing owners only; never consume caller row locators."""
    from src.db.models import (Goal, Memory, MemoryEntity, MemoryProposal, MemorySource,
        MemoryEdge, MemoryTombstone, WorkBoardProposal, WorkBoardDecisionReceipt, WorkBoardDecisionReceiptStage)
    from sqlalchemy import or_
    if type(admission) is not NativeMemoryMutationAdmission or type(effect) is not MemoryOwnerEffect:
        raise NativeServiceBlocked("native_memory_retention_original_effect_required")
    value = admission.candidate()
    if effect.method != value["method"] or effect.candidate_digest != admission.candidate_digest:
        raise NativeServiceBlocked("native_memory_retention_original_effect_changed")
    references = set()
    def add(table, key):
        if not isinstance(key, str) or not key or ((table, key) not in references and len(references) >= 128):
            raise NativeServiceBlocked("native_memory_retention_bound_exceeded")
        references.add((table, key))
    if "source" in value:
        goal_id = value["source"]["goal_id"]
        seen = set()
        while goal_id is not None:
            if goal_id in seen:
                raise NativeServiceBlocked("native_memory_goal_ancestry_changed")
            seen.add(goal_id)
            goal = await db.get(Goal, goal_id)
            if (goal is None or goal.owner_principal_id != value["operator_principal_id"]
                or goal.owner_session_id != value["operator_session_id"]):
                raise NativeServiceBlocked("native_memory_goal_ancestry_changed")
            add("goals", goal.id)
            goal_id = goal.parent_id
    memory_ids = set()
    if effect.proposal_id is not None:
        proposal = await db.get(MemoryProposal, effect.proposal_id)
        if (proposal is None or proposal.owner_principal_id != value["operator_principal_id"]
            or proposal.owner_session_id != value["operator_session_id"]):
            raise NativeServiceBlocked("native_memory_retention_proposal_changed")
        add("memory_proposals", proposal.proposal_id)
        if proposal.recovered_from_proposal_id is not None:
            raise NativeServiceBlocked("native_memory_recovery_profile_unsupported")
        if proposal.proposal_job_id is not None:
            board = list((await db.execute(select(WorkBoardProposal).where(
                WorkBoardProposal.admission_job_id == proposal.proposal_job_id).limit(2))).scalars())
            if (len(board) != 1 or board[0].owner_principal_id != value["operator_principal_id"]
                or board[0].owner_session_id != value["operator_session_id"]):
                raise NativeServiceBlocked("native_memory_retention_board_proposal_changed")
            add("work_board_proposals", board[0].proposal_id)
        baselines = list((await db.execute(select(WorkBoardDecisionReceipt).where(
            WorkBoardDecisionReceipt.source_proposal_id == proposal.proposal_id,
            WorkBoardDecisionReceipt.receipt_stage == WorkBoardDecisionReceiptStage.source_baseline).limit(2))).scalars())
        if effect.status == "succeeded" and len(baselines) != 1:
            raise NativeServiceBlocked("native_memory_retention_baseline_changed")
        if len(baselines) > 1:
            raise NativeServiceBlocked("native_memory_retention_baseline_changed")
        for row in baselines:
            add("work_board_decision_receipts", row.receipt_id)
        if proposal.corrects_memory_id:
            memory_ids.add(proposal.corrects_memory_id)
    if effect.record_id is not None:
        memory_ids.add(effect.record_id)
    for key in sorted(memory_ids):
        record = await db.get(Memory, key)
        if record is None or record.source_session_id != value["operator_session_id"]:
            raise NativeServiceBlocked("native_memory_retention_record_changed")
        add("memories", record.id)
        for entity_id in (record.subject_entity_id, record.project_entity_id):
            if entity_id is not None:
                entity = await db.get(MemoryEntity, entity_id)
                if entity is None:
                    raise NativeServiceBlocked("native_memory_retention_entity_changed")
                add("memory_entities", entity.id)
        for cls, table in ((MemorySource, "memory_sources"), (MemoryTombstone, "memory_tombstones")):
            rows = list((await db.execute(select(cls).where(cls.memory_id == key).limit(129))).scalars())
            for row in rows:
                add(table, row.id)
    if value["method"] == "memory.applyReviewed" and effect.record_id is not None:
        edges = list((await db.execute(select(MemoryEdge).where(or_(
            MemoryEdge.from_memory_id == effect.record_id, MemoryEdge.to_memory_id == effect.record_id)).limit(129))).scalars())
        for row in edges:
            if row.from_memory_id not in memory_ids or row.to_memory_id not in memory_ids:
                raise NativeServiceBlocked("native_memory_retention_edge_changed")
            add("memory_edges", row.id)
    return tuple(NativeMemoryOwnerReference(table, key) for table, key in sorted(references))
