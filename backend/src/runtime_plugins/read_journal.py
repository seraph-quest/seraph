"""Private finite read candidates in the existing canonical job journal."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import hashlib
from dataclasses import dataclass

from sqlalchemy import select

from .read_admission import NativeServiceReadAdmission, READ_JOB_KIND
from .dispatch import NativeServiceBlocked

CONTEXT_TAG = "native-service-read.v1"
ARTIFACT_METHODS = ("artifacts.stage", "artifacts.adopt", "artifacts.read", "audit.append")


def artifact_profile():
    from src.security.trust_contract import TRUST_SCHEMA_VERSION
    return {"schema_version": 1, "source_kind": "native_read_result", "max_bytes": 65536,
        "max_artifacts": 1, "methods": list(ARTIFACT_METHODS),
        "media_type": "application/json; charset=utf-8", "egress": "local_only",
        "policy_version": TRUST_SCHEMA_VERSION, "no_learning": True}


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def candidate_context(admission, *, host_boot_nonce, native_branch="base"):
    if type(admission) is not NativeServiceReadAdmission:
        raise NativeServiceBlocked("native_read_admission_required")
    value = {"schema_version": 1, "context_tag": CONTEXT_TAG,
        "candidate": admission.candidate(), "candidate_digest": admission.candidate_digest,
        "host_boot_nonce": host_boot_nonce, "result": None}
    if native_branch == "artifact":
        from src.workflows.job_runtime import _digest
        value.update(artifact_profile=artifact_profile(), artifact_profile_digest=_digest(artifact_profile()), operations=[])
    elif native_branch != "base":
        raise NativeServiceBlocked("native_read_branch_unsupported")
    return _canonical(value)


def read_context(run):
    """Decode only the closed original private context; never a public job dict."""
    from .ownership import RuntimeCompositionBinding
    try:
        value = json.loads(run.checkpoint_context_json)
        binding = RuntimeCompositionBinding.from_json(run.composition_binding_json)
        fields = {"schema_version", "context_tag", "candidate", "candidate_digest", "host_boot_nonce", "result"}
        if binding.native_branch == "artifact":
            fields |= {"artifact_profile", "artifact_profile_digest", "operations"}
        if (type(value) is not dict or set(value) != fields
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or value["context_tag"] != CONTEXT_TAG or type(value["host_boot_nonce"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", value["host_boot_nonce"])):
            raise ValueError("context")
        admission = NativeServiceReadAdmission.from_candidate(value["candidate"])
        binding = RuntimeCompositionBinding.from_json(run.composition_binding_json)
        if (run.job_kind != READ_JOB_KIND or run.capability_version != "1"
            or admission.candidate_digest != value["candidate_digest"]
            or run.input_digest != admission.candidate_digest
            or binding.origin_method != value["candidate"]["method"] or binding.native_branch not in {"base", "artifact"}
            or run.max_attempts != 1 or run.parent_job_id is not None or run.source_task_id is not None):
            raise ValueError("binding")
        if value["result"] is not None:
            from .contracts import validate_result
            from src.workflows.job_runtime import _digest
            result = value["result"]
            if (type(result) is not dict or set(result) != {"invocation_ref", "claim_ref", "candidate_digest", "result_digest", "payload"}
                or result["invocation_ref"] != run.run_identity
                or result["candidate_digest"] != admission.candidate_digest
                or _digest(result["payload"]) != result["result_digest"]
                or result["payload"].get("status") != "succeeded"):
                raise ValueError("result")
            validate_result(binding.origin_method, result["payload"])
            _result_witness(run, result["claim_ref"])
        if binding.native_branch == "artifact":
            _validate_artifact_context(run, value)
        return value
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise NativeServiceBlocked("native_read_private_context_changed") from exc


def read_candidate(run):
    return read_context(run)["candidate"]


async def validate_read_policy(db, run):
    """Recheck original native grants before any private projection bytes."""
    from src.auth.service import authenticate_principal
    operator = await authenticate_principal(run.owner_principal_id, db=db)
    if json.loads(run.declared_authority_json).get("grants") != sorted(
        str(getattr(grant, "value", grant)) for grant in operator.principal.grants):
        raise NativeServiceBlocked("native_read_original_policy_changed")
    if read_candidate(run)["method"].startswith("capabilities."):
        from src.security.trust_contract import AuthorityGrant
        if AuthorityGrant.CAPABILITY_EXECUTE not in operator.principal.grants:
            raise NativeServiceBlocked("native_read_capability_grant_required")
    if "artifact_profile" in read_context(run):
        from src.security.trust_contract import AuthorityGrant
        if not {AuthorityGrant.CAPABILITY_EXECUTE, AuthorityGrant.ARTIFACT_TRANSFER} <= set(operator.principal.grants):
            raise NativeServiceBlocked("native_read_artifact_grants_required")


def _result_witness(run, claim_ref, *, require_current=False):
    from .dispatch import _receipt_witness, _ms
    witnesses = json.loads(run.checkpoint_receipts_json or "[]")
    selected = [item for item in witnesses if item.get("checkpoint_id") == "runtime-service-invocation:" + claim_ref]
    if len(selected) != 1:
        raise NativeServiceBlocked("native_read_result_claim_changed")
    witness = _receipt_witness(selected[0])
    deadline = run.deadline_at
    if type(deadline) is str:
        deadline = datetime.fromisoformat(deadline)
    if (witness["invocation_ref"] != run.run_identity or witness["claim_ref"] != claim_ref
        or witness["input_digest"] != run.input_digest or witness["authority_digest"] != run.authority_digest
        or witness["run_fingerprint"] != run.run_fingerprint or witness["original_deadline_at"] != _ms(deadline)
        or witness["host_boot_nonce"] != json.loads(run.checkpoint_context_json)["host_boot_nonce"]):
        raise NativeServiceBlocked("native_read_result_claim_changed")
    if require_current and (witness["attempt_count"] != run.attempt_count or witness["fencing_token"] != run.fencing_token):
        raise NativeServiceBlocked("native_read_result_claim_changed")
    return witness


async def native_read_spec(db, *, admission, operator, reviewed_composition, idempotency_key):
    """Original authenticated ingress calls this inside its complete writer."""
    from src.auth.ownership import _current_root
    from src.auth.service import AuthenticatedOperator, authenticate_principal
    from src.db.models import WorkflowRunState
    from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec, _digest
    from .composition import ReviewedComposition
    from .ownership import bind_invocation
    if (type(admission) is not NativeServiceReadAdmission or type(operator) is not AuthenticatedOperator
        or type(reviewed_composition) is not ReviewedComposition
        or type(idempotency_key) is not str or not re.fullmatch(r"[\x21-\x7e]{1,128}", idempotency_key)
        or not db.info.get("native_writer_started")):
        raise NativeServiceBlocked("native_read_original_admission_unavailable")
    candidate = admission.candidate()
    await _current_root(db, operator)
    current = await authenticate_principal(operator.principal.principal_id, db=db)
    if current.principal.grants != operator.principal.grants:
        raise NativeServiceBlocked("native_read_original_policy_changed")
    if candidate["method"].startswith("capabilities."):
        from src.security.trust_contract import AuthorityGrant
        if AuthorityGrant.CAPABILITY_EXECUTE not in current.principal.grants:
            raise NativeServiceBlocked("native_read_capability_grant_required")
    from src.security.trust_contract import AuthorityGrant
    if not {AuthorityGrant.CAPABILITY_EXECUTE, AuthorityGrant.ARTIFACT_TRANSFER} <= set(current.principal.grants):
        raise NativeServiceBlocked("native_read_artifact_grants_required")
    binding = await bind_invocation(db, method=candidate["method"], native_branch="artifact",
        reviewed_composition=reviewed_composition)
    job_id = "native-read:" + _digest({"root": operator.session_id, "method": candidate["method"], "key": idempotency_key})
    prior = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    if prior is not None:
        if read_candidate(prior) != candidate:
            raise NativeServiceBlocked("native_read_idempotency_candidate_changed")
        deadline = prior.deadline_at.replace(tzinfo=prior.deadline_at.tzinfo or timezone.utc)
    authority = {"principal": operator.principal.principal_id, "owner_kind": "user",
        "session_id": operator.session_id,
        "grants": sorted(str(getattr(grant, "value", grant)) for grant in current.principal.grants)}
    from types import SimpleNamespace
    from .read_artifacts import artifact_trust_request
    from src.security.trust_contract import evaluate_trust
    original = SimpleNamespace(run_identity=job_id, session_id=operator.session_id,
        operator_session_id=operator.session_id, deadline_at=deadline, attempt_count=0)
    request = artifact_trust_request(original, content_digest=admission.candidate_digest,
        request_ref=_operation_ref(original, "artifacts.stage"), principal=current.principal)
    if not evaluate_trust(request).allowed:
        raise NativeServiceBlocked("native_read_artifact_policy_denied")
    return DurableJobSpec(identity=DurableJobIdentity(job_id=job_id, owner_kind="user",
        owner_principal_id=operator.principal.principal_id, job_kind=READ_JOB_KIND,
        capability_version="1", idempotency_scope="native-service-read", idempotency_key=idempotency_key),
        inputs=candidate, session_id=operator.session_id, operator_session_id=operator.session_id,
        conversation_id=operator.session_id, composition_binding=binding,
        declared_authority=authority, deadline_at=deadline, max_attempts=1,
        run_fingerprint=_digest({"candidate_digest": admission.candidate_digest,
            "root": operator.session_id, "deadline": deadline.isoformat(), "binding": binding.binding_digest,
            "artifact_profile_digest": _digest(artifact_profile())}))


async def validate_read_spec(db, spec, admission):
    from src.auth.service import authenticate_principal
    from src.db.models import OperatorSession
    now = datetime.now(timezone.utc)
    if type(admission) is not NativeServiceReadAdmission:
        raise NativeServiceBlocked("native_read_admission_required")
    admission.candidate()
    binding = spec.composition_binding
    root = await db.scalar(select(OperatorSession).where(
        OperatorSession.id == spec.operator_session_id,
        OperatorSession.principal_id == spec.identity.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False), OperatorSession.idle_expires_at > now,
        OperatorSession.absolute_expires_at > now).execution_options(populate_existing=True))
    if root is None:
        raise NativeServiceBlocked("native_read_original_root_inactive")
    principal = await authenticate_principal(spec.identity.owner_principal_id, db=db)
    deadline = spec.deadline_at
    if deadline is not None:
        deadline = deadline.replace(tzinfo=deadline.tzinfo or timezone.utc)
    if (spec.inputs != admission.candidate()
        or spec.identity.job_kind != READ_JOB_KIND or spec.identity.capability_version != "1"
        or spec.identity.owner_kind != "user" or spec.session_id != spec.operator_session_id
        or spec.conversation_id != spec.operator_session_id or spec.max_attempts != 1
        or spec.parent_job_id is not None or spec.goal_id is not None or spec.source_task_id is not None
        or spec.dependencies or spec.resource_claims or not db.info.get("native_writer_started")
        or db.info.get("composition_writer_owner") != "durable_jobs"
        or binding is None or binding.origin_method != spec.inputs["method"] or binding.native_branch not in {"base", "artifact"}
        or binding.host_package_digest is None or deadline is None or deadline > now + timedelta(seconds=30)
        or spec.declared_authority != {"principal": spec.identity.owner_principal_id, "owner_kind": "user",
            "session_id": spec.operator_session_id,
            "grants": sorted(str(getattr(grant, "value", grant)) for grant in principal.principal.grants)}):
        raise NativeServiceBlocked("native_read_original_spec_changed")
    if binding.native_branch == "artifact":
        from src.security.trust_contract import AuthorityGrant
        if not {AuthorityGrant.CAPABILITY_EXECUTE, AuthorityGrant.ARTIFACT_TRANSFER} <= set(principal.principal.grants):
            raise NativeServiceBlocked("native_read_artifact_grants_required")


def seal_read_result(db, run, result, *, invocation_ref, claim_ref):
    """Called only after the real same-writer native projection, never child echo."""
    from .contracts import validate_result
    from src.workflows.job_runtime import _digest
    context = read_context(run)
    validate_result(context["candidate"]["method"], result)
    guard = db.info.get("composition_guard")
    now = datetime.now(timezone.utc)
    if (guard is None or not db.info.get("native_writer_started") or run.status != "running"
        or invocation_ref != run.run_identity or context["result"] is not None
        or run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or timezone.utc) <= now
        or run.lease_expires_at is None or run.lease_expires_at.replace(tzinfo=run.lease_expires_at.tzinfo or timezone.utc) <= now
        or result["status"] != "succeeded"):
        raise NativeServiceBlocked("native_read_result_seal_unavailable")
    witness = _result_witness(run, claim_ref, require_current=True)
    if witness["lease_owner"] != run.lease_owner:
        raise NativeServiceBlocked("native_read_result_claim_changed")
    context["result"] = {"invocation_ref": invocation_ref, "claim_ref": claim_ref,
        "candidate_digest": context["candidate_digest"], "result_digest": _digest(result), "payload": result}
    encoded = _canonical(context)
    db.info["composition_native_read_context"] = (run.run_identity, encoded)
    run.checkpoint_context_json = encoded
    return context["result"]["result_digest"]


def artifact_source_bytes(context):
    if context.get("result") is None:
        raise NativeServiceBlocked("native_read_artifact_source_missing")
    raw = _canonical(context["result"]["payload"]).encode("utf-8")
    if not 1 <= len(raw) <= 65536:
        raise NativeServiceBlocked("native_read_artifact_source_overflow")
    return raw


def _operation_ref(run, method):
    return "native-read-op:" + hashlib.sha256((run.run_identity + ":" + method).encode()).hexdigest()


def _operation_candidate(run, context, method):
    from src.workflows.job_runtime import _digest
    raw = artifact_source_bytes(context)
    common = {"schema_version": 1, "method": method, "no_learning": True,
        "content_digest": hashlib.sha256(raw).hexdigest(), "profile_digest": context["artifact_profile_digest"]}
    slots = context["operations"]
    if method == "artifacts.stage":
        return {**common, "request_ref": _operation_ref(run, method),
            "original_read_method": context["candidate"]["method"],
            "sealed_result_digest": context["result"]["result_digest"], "size_bytes": len(raw)}
    if method == "artifacts.adopt":
        stage = slots[0]
        return {**common, "request_ref": _operation_ref(run, method),
            "stage_candidate_digest": stage["candidate_digest"], "artifact_ref": stage["result"]["value"]["artifact_ref"],
            "size_bytes": len(raw), "stage_readback_ref": stage["effect_receipt"]["readback_id"],
            "stage_readback_digest": _digest(stage["effect_receipt"])}
    if method == "artifacts.read":
        adopt = slots[1]
        return {**common, "artifact_ref": adopt["result"]["value"]["artifact_ref"],
            "adoption_receipt_ref": adopt["receipt_ref"], "adoption_receipt_digest": _digest(adopt["result"]),
            "size_bytes": len(raw), "max_bytes": 65536}
    if method == "audit.append":
        readback = slots[2]
        return {**common, "event_ref": _operation_ref(run, method), "event_type": "runtime_service_observed",
            "original_read_method": context["candidate"]["method"],
            "sealed_result_digest": context["result"]["result_digest"],
            "artifact_ref": readback["result"]["value"]["artifact_ref"],
            "artifact_readback_ref": readback["effect_receipt"]["readback_id"],
            "artifact_readback_digest": _digest(readback["effect_receipt"])}
    raise NativeServiceBlocked("native_read_artifact_method_unsupported")


_OPERATION_SEAL = object()


@dataclass(frozen=True)
class NativeReadArtifactOperation:
    run_identity: str
    candidate_json: str
    candidate_digest: str
    _seal: object

    def candidate(self):
        if self._seal is not _OPERATION_SEAL:
            raise NativeServiceBlocked("native_read_artifact_operation_required")
        return json.loads(self.candidate_json)

    def wire_inputs(self):
        value = self.candidate()
        fields = {"artifacts.stage": ("request_ref",), "artifacts.adopt": ("request_ref",),
            "artifacts.read": ("artifact_ref", "max_bytes"), "audit.append": ("event_ref",)}[value["method"]]
        return {key: value[key] for key in fields}


def _validate_artifact_context(run, context):
    from src.workflows.job_runtime import _digest
    from .contracts import validate_result
    if context["artifact_profile"] != artifact_profile() or context["artifact_profile_digest"] != _digest(artifact_profile()):
        raise NativeServiceBlocked("native_read_artifact_profile_changed")
    operations = context["operations"]
    if type(operations) is not list or len(operations) > 4 or (operations and context["result"] is None):
        raise NativeServiceBlocked("native_read_artifact_operations_changed")
    for index, slot in enumerate(operations):
        if (type(slot) is not dict or set(slot) != {"method", "candidate", "candidate_digest", "state", "call_ref", "result",
            "receipt_ref", "revision", "artifact_record", "effect_receipt", "audit_event_ref", "audit_event_digest"}
            or slot["method"] != ARTIFACT_METHODS[index] or slot["state"] not in {"prepared", "intent", "settled"}
            or (index < len(operations) - 1 and slot["state"] != "settled")):
            raise NativeServiceBlocked("native_read_artifact_operations_changed")
        expected = _operation_candidate(run, context, slot["method"])
        if slot["candidate"] != expected or slot["candidate_digest"] != _digest(expected) or len(_canonical(expected).encode()) > 4096:
            raise NativeServiceBlocked("native_read_artifact_candidate_changed")
        if slot["state"] == "prepared":
            if any(slot[field] is not None for field in ("call_ref", "result", "receipt_ref", "revision", "artifact_record", "effect_receipt", "audit_event_ref", "audit_event_digest")):
                raise NativeServiceBlocked("native_read_artifact_prepared_changed")
        elif type(slot["call_ref"]) is not str or not re.fullmatch(r"[\x21-\x7e]{1,128}", slot["call_ref"]):
            raise NativeServiceBlocked("native_read_artifact_call_changed")
        if slot["state"] == "intent":
            if any(slot[field] is not None for field in ("result", "receipt_ref", "revision", "artifact_record", "effect_receipt", "audit_event_ref", "audit_event_digest")):
                raise NativeServiceBlocked("native_read_artifact_intent_changed")
        if slot["state"] == "settled":
            validate_result(slot["method"], slot["result"])
            if (slot["result"]["status"] != "succeeded" or slot["receipt_ref"] != _operation_ref(run, slot["method"])
                or type(slot["revision"]) is not int or slot["revision"] < 1):
                raise NativeServiceBlocked("native_read_artifact_receipt_changed")
            if index < 3:
                record, effect = slot["artifact_record"], slot["effect_receipt"]
                if (type(record) is not dict or record.get("run_id") != run.run_identity or record.get("producer") != run.job_kind
                    or record.get("session_id") != run.session_id or record.get("content_sha256") != expected["content_digest"]
                    or record.get("size_bytes") != len(artifact_source_bytes(context))
                    or record.get("artifact_id") != slot["result"]["value"]["artifact_ref"]
                    or slot["result"]["value"].get("digest") != expected["content_digest"]
                    or slot["result"]["value"].get("size_bytes") != record["size_bytes"]
                    or type(effect) is not dict or effect.get("receipt_kind") != "readback" or effect.get("status") != "succeeded"
                    or effect.get("content_sha256") != expected["content_digest"] or not effect.get("readback_id")):
                    raise NativeServiceBlocked("native_read_artifact_owner_receipt_changed")
                artifacts = json.loads(run.artifact_receipts_json or "[]")
                selected = [item for item in artifacts if type(item) is dict and item.get("artifact_id") == record["artifact_id"]]
                if (len(artifacts) != 1 or len(selected) != 1 or any(selected[0].get(key) != record.get(key)
                    for key in ("artifact_id", "artifact_type", "file_path", "producer", "content_sha256", "size_bytes", "exists"))
                    or effect not in json.loads(run.effect_receipts_json or "[]")):
                    raise NativeServiceBlocked("native_read_artifact_canonical_receipt_changed")
                if index == 2 and slot["result"]["value"]["content"].encode("utf-8") != artifact_source_bytes(context):
                    raise NativeServiceBlocked("native_read_artifact_readback_changed")
            elif (slot["audit_event_ref"] != expected["event_ref"] or type(slot["audit_event_digest"]) is not str
                  or not re.fullmatch(r"[0-9a-f]{64}", slot["audit_event_digest"])
                  or slot["result"]["value"]["revision"] != slot["revision"] or slot["result"]["value"]["receipt_ref"] != slot["receipt_ref"]):
                raise NativeServiceBlocked("native_read_audit_receipt_changed")


def artifact_context(run):
    context = read_context(run)
    if "artifact_profile" not in context:
        raise NativeServiceBlocked("native_read_artifact_branch_required")
    return context


def _write_context(db, run, context):
    encoded = _canonical(context)
    db.info["composition_native_read_context"] = (run.run_identity, encoded)
    run.checkpoint_context_json = encoded
    run.revision += 1
    run.updated_at = datetime.now(timezone.utc)


def _artifact_writer(db, run):
    now = datetime.now(timezone.utc)
    context = artifact_context(run)
    if (db.info.get("composition_guard") is None or not db.info.get("native_writer_started")
        or db.info.get("composition_writer_owner") != "finite_service" or run.status != "running"
        or run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or timezone.utc) <= now
        or run.lease_expires_at is None or run.lease_expires_at.replace(tzinfo=run.lease_expires_at.tzinfo or timezone.utc) <= now):
        raise NativeServiceBlocked("native_read_artifact_writer_unavailable")
    _result_witness(run, context["result"]["claim_ref"], require_current=True)
    return context


def prepare_operation(db, run, method):
    from src.workflows.job_runtime import _digest
    context = _artifact_writer(db, run)
    slots = context["operations"]
    if len(slots) >= 4 or method != ARTIFACT_METHODS[len(slots)] or (slots and slots[-1]["state"] != "settled"):
        raise NativeServiceBlocked("native_read_artifact_operation_replay_denied")
    candidate = _operation_candidate(run, context, method)
    slot = {"method": method, "candidate": candidate, "candidate_digest": _digest(candidate), "state": "prepared",
        **{key: None for key in ("call_ref", "result", "receipt_ref", "revision", "artifact_record", "effect_receipt", "audit_event_ref", "audit_event_digest")}}
    slots.append(slot)
    _write_context(db, run, context)
    return NativeReadArtifactOperation(run.run_identity, _canonical(candidate), _digest(candidate), _OPERATION_SEAL)


def operation_for_wire(run, method, inputs):
    context = artifact_context(run)
    if not context["operations"]:
        raise NativeServiceBlocked("native_read_artifact_candidate_missing")
    slot = context["operations"][-1]
    operation = NativeReadArtifactOperation(run.run_identity, _canonical(slot["candidate"]), slot["candidate_digest"], _OPERATION_SEAL)
    if slot["method"] != method or operation.wire_inputs() != inputs or slot["state"] != "prepared":
        raise NativeServiceBlocked("native_read_artifact_wire_changed")
    return operation


def validate_operation(run, operation, *, call_ref):
    from src.workflows.job_runtime import _digest
    if type(operation) is not NativeReadArtifactOperation or operation._seal is not _OPERATION_SEAL or operation.run_identity != run.run_identity:
        raise NativeServiceBlocked("native_read_artifact_operation_required")
    context = artifact_context(run)
    slot = context["operations"][-1]
    if (slot["candidate"] != operation.candidate() or slot["candidate_digest"] != operation.candidate_digest
        or operation.candidate_digest != _digest(operation.candidate()) or slot["state"] != "intent" or slot["call_ref"] != call_ref):
        raise NativeServiceBlocked("native_read_artifact_operation_changed")
    return context


def start_operation(db, run, operation, *, call_ref):
    context = _artifact_writer(db, run)
    slot = context["operations"][-1]
    if (type(operation) is not NativeReadArtifactOperation or operation._seal is not _OPERATION_SEAL
        or operation.run_identity != run.run_identity or slot["candidate"] != operation.candidate()
        or slot["candidate_digest"] != operation.candidate_digest or slot["state"] != "prepared"
        or type(call_ref) is not str or not re.fullmatch(r"[\x21-\x7e]{1,128}", call_ref)):
        raise NativeServiceBlocked("native_read_artifact_operation_replay_denied")
    slot.update(state="intent", call_ref=call_ref)
    _write_context(db, run, context)


def seal_operation(db, run, operation, *, call_ref, result, artifact_record=None, effect_receipt=None, audit_event=None):
    from sqlalchemy import inspect
    from src.workflows.job_runtime import _digest
    context = _artifact_writer(db, run)
    validate_operation(run, operation, call_ref=call_ref)
    slot = context["operations"][-1]
    revision = run.revision + 1
    if slot["method"] == "audit.append":
        from src.db.models import AuditEvent
        if type(audit_event) is not AuditEvent or inspect(audit_event).session is not db.sync_session or audit_event.id != slot["candidate"]["event_ref"] or audit_event.session_id != run.session_id:
            raise NativeServiceBlocked("native_read_audit_owner_required")
        audit_digest = _digest(audit_event_body(audit_event))
        db.info["composition_native_read_audit"] = (audit_event.id, audit_digest)
    else:
        audit_digest = None
    slot.update(state="settled", result=result, receipt_ref=_operation_ref(run, slot["method"]), revision=revision,
        artifact_record=artifact_record, effect_receipt=effect_receipt,
        audit_event_ref=audit_event.id if audit_event is not None else None, audit_event_digest=audit_digest)
    _validate_artifact_context(run, context)
    if artifact_record is not None:
        artifacts = json.loads(run.artifact_receipts_json or "[]")
        if not any(item.get("artifact_id") == artifact_record["artifact_id"] and item.get("content_sha256") == artifact_record["content_sha256"] for item in artifacts):
            raise NativeServiceBlocked("native_read_artifact_canonical_receipt_missing")
        effects = json.loads(run.effect_receipts_json or "[]")
        if effect_receipt not in effects:
            raise NativeServiceBlocked("native_read_artifact_canonical_readback_missing")
    _write_context(db, run, context)
    return slot


def audit_event_body(event):
    fields = ("id", "session_id", "actor", "event_type", "tool_name", "risk_level", "policy_mode", "summary", "details_json")
    return {key: event[key] if type(event) is dict else getattr(event, key) for key in fields}


def validate_context_transition(previous, current):
    immutable = set(previous) - {"result", "operations"}
    if ({key: previous[key] for key in immutable} != {key: current.get(key) for key in immutable}
        or (previous["result"] is not None and current["result"] != previous["result"])):
        raise NativeServiceBlocked("native_read_private_context_changed")
    before, after = previous.get("operations", []), current.get("operations", [])
    if before == after:
        return
    if previous["result"] is None or current["result"] != previous["result"]:
        raise NativeServiceBlocked("native_read_artifact_transition_changed")
    if len(after) == len(before) + 1 and after[:-1] == before and after[-1]["state"] == "prepared":
        return
    if len(after) != len(before) or not before or after[:-1] != before[:-1]:
        raise NativeServiceBlocked("native_read_artifact_transition_changed")
    old, new = before[-1], after[-1]
    if (old["candidate"] != new["candidate"] or old["candidate_digest"] != new["candidate_digest"]
        or (old["state"], new["state"]) not in {("prepared", "intent"), ("intent", "settled")}
        or (old["state"] == "intent" and old["call_ref"] != new["call_ref"])):
        raise NativeServiceBlocked("native_read_artifact_transition_changed")


async def assert_artifact_completion(db, run):
    context = artifact_context(run)
    if len(context["operations"]) != 4 or any(slot["state"] != "settled" for slot in context["operations"]):
        raise NativeServiceBlocked("native_read_artifact_completion_unproven")
    from .read_artifacts import assert_artifact_owner_readback
    await assert_artifact_owner_readback(db, run, context)
