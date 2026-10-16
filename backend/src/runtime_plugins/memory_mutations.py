"""Operator-admitted memory mutations through Cordis (#1064).

``memory.propose``, ``memory.applyReviewed`` and ``memory.forget`` follow the
native read journey: an authenticated operator Root admits one durable job,
a protected original witness is written under its lease, one crossing reaches
the child's memory facade, and the facade's callback performs the effect with
the existing Python owner inside the same immediate writer as the job CAS.

Candidates and sealed results hold ids, revisions and keyed MACs only, never
memory or preview text. Python originates every crossing: admission lives
in ``memory_admission`` and only operator HTTP handlers may import it, so no
agent tool, agent loop or child can admit an apply or a forget. The admission
itself is the operator's review. No inference is used anywhere on this path.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac

from sqlalchemy import select

from .contracts import blocked, enum, nullable_ref, object_fields, ref, sha, succeeded
from .dispatch import (NativeServiceBlocked, _append_receipt, _canonical, _writer, cas_run, checkpoint_receipt,
                       read_context)
from .protocol import ProtocolError, integer

MEMORY_JOB_KIND = "runtime_service_memory_v1"
MUTATION_METHODS = frozenset({"memory.propose", "memory.applyReviewed", "memory.forget"})
REVIEW_ACTIONS = frozenset({"accept", "reject"})
DECISION_EFFECTS = frozenset({"none", "require_operator_confirmation"})
OUTSTANDING_LIMIT = 4
RESULT_CHECKPOINT = "cordis-memory:result"
# The lance vector index keeps its own text copy; purging it is #1119. Every
# forget receipt says so instead of claiming full propagation.
DERIVED_VECTOR_PURGE = "not_covered"
FORGET_LABEL = "delete_permanently"
_FORGET_REASON = "cordis_operator_delete"
_REF_FIELD = {"memory.propose": "request_ref", "memory.applyReviewed": "review_ref", "memory.forget": "request_ref"}


def mutation_key() -> bytes:
    """Per-install MAC key for mutation candidates and sealed results.

    Derived from the vault master key, which backups never archive, so a
    retained MAC cannot confirm a guessed preview or forgotten text.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    try:
        from src.vault.crypto import vault_key_material
        material = vault_key_material()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise NativeServiceBlocked("native_memory_mutation_key_unavailable") from exc
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                info=b"seraph.cordis-memory-mutation.v1").derive(material)


def _mac(label: bytes, value, *, key: bytes | None = None) -> str:
    return hmac.new(key or mutation_key(), label + b"\0" + _canonical(value).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def sealed_mutation_digest(result: dict, *, key: bytes | None = None) -> str:
    return _mac(b"result", result, key=key)


# ---------------------------------------------------------------- candidates

def _true(value) -> None:
    if value is not True:
        raise ProtocolError("explicit acknowledgement required")


_CANDIDATE_FIELDS = {
    "memory.propose": {"task_id": ref, "attempt_id": ref, "expected_task_revision": lambda v: integer(v, 1)},
    "memory.applyReviewed": {
        "proposal_id": ref, "expected_revision": lambda v: integer(v, 1),
        "expected_task_revision": lambda v: integer(v, 1), "expected_goal_revision": lambda v: integer(v, 1),
        "action": lambda v: enum(v, REVIEW_ACTIONS), "decision_effect": lambda v: enum(v, DECISION_EFFECTS),
        "corrects_memory_id": nullable_ref, "preferred_capability_id": nullable_ref, "preview_mac": sha},
    "memory.forget": {"memory_id": ref, "mode": lambda v: enum(v, {"tombstone"}),
                      "acknowledge_irreversible": _true},
}


def validate_mutation_candidate(method: str, value) -> dict:
    """Closed, server-built candidates; none carries text."""
    if method not in MUTATION_METHODS or type(value) is not dict or value.get("method") != method:
        raise ProtocolError("unsupported memory mutation candidate")
    object_fields(value, {"schema_version": lambda v: integer(v, 1, 1), "method": lambda v: enum(v, {method}),
                          **_CANDIDATE_FIELDS[method]})
    return dict(value)


def wire_inputs(candidate: dict, job_id: str) -> dict:
    """The exact plugin-visible request: only the job reference crosses."""
    ref(job_id)
    return {_REF_FIELD[candidate["method"]]: job_id}


async def review_binding_mac(proposal, *, action: str, decision_effect: str, corrects_memory_id: str | None,
                             preferred_capability_id: str | None, key: bytes | None = None) -> str:
    """Keyed MAC over everything the operator reviewed for one apply.

    The fields mirror ``apply_memory_proposal_action``'s acceptance binding:
    the post-sanitisation accepted-text digest, correction target, preferred
    capability, decision effect and memory kind, bound to the proposal id,
    revision, preview digest and owner session.
    """
    from src.memory.m5 import _decode_object, m5_text_digest, sanitize_m5_memory_text_async
    try:
        accepted_text = await sanitize_m5_memory_text_async(proposal.preview_text or "")
    except ValueError as exc:
        raise NativeServiceBlocked("native_memory_redaction_unavailable") from exc
    capability = preferred_capability_id or str(
        _decode_object(proposal.memory_scope_json).get("preferred_capability_id") or "")
    kind = getattr(proposal.memory_kind, "value", proposal.memory_kind)
    return _mac(b"review", {
        "proposal_id": proposal.proposal_id, "revision": int(proposal.revision or 0),
        "owner_session_id": proposal.owner_session_id, "preview_text_digest": proposal.preview_text_digest or "",
        "accepted_text_digest": m5_text_digest(accepted_text), "action": action,
        "decision_effect": decision_effect, "corrects_memory_id": corrects_memory_id or "",
        "preferred_capability_id": capability, "memory_kind": kind or ""}, key=key)


def _assert_memory_owner(memory, session_id: str) -> None:
    """The memory routes' 403 contract, as typed reasons."""
    if memory is None:
        raise NativeServiceBlocked("native_memory_unknown")
    bound = str(memory.source_session_id or "").strip()
    if not bound:
        raise NativeServiceBlocked("memory_owner_session_unbound")
    if bound != session_id:
        raise NativeServiceBlocked("memory_owner_session_forbidden")


# ---------------------------------------------------------------- callback

_CORE_ERRORS = {"accepted_binding_unavailable", "decision_receipt_signing_unavailable", "proposal_expired"}


def _core_reason(exc: BaseException) -> str:
    from src.extensions.capability_execution import CapabilityJournalError
    if isinstance(exc, CapabilityJournalError):
        return "native_memory_accepted_binding_unavailable"
    if isinstance(exc, PermissionError):
        return "native_memory_owner_forbidden"
    code = "".join(char if char.isalnum() or char == "_" else "_" for char in str(exc).lower())[:80].strip("_")
    return "native_memory_" + (code or "mutation_rejected")


async def dispatch_memory_mutation(dispatcher, frame, payload, scope):
    """One callback: recheck, effect and sealed result in one writer."""
    method = frame["method"]
    post_commit = []
    async with dispatcher.jobs._session() as db:
        await _writer(db)
        run, binding, mode = await dispatcher.current(db, scope, method)
        if run.job_kind != MEMORY_JOB_KIND or binding.origin_method != method or mode != "operator-root":
            raise NativeServiceBlocked("native_memory_candidate_not_bound")
        if payload[_REF_FIELD[method]] != run.run_identity:
            raise NativeServiceBlocked("native_memory_reference_not_bound")
        context = read_context(run)
        if context["result"] is not None:
            raise NativeServiceBlocked("native_memory_replay_denied")
        try:
            candidate = validate_mutation_candidate(method, context["candidate"])
        except ProtocolError as exc:
            raise NativeServiceBlocked("native_candidate_changed") from exc
        try:
            async with db.begin_nested():
                outcome = await _EFFECTS[method](db, run, candidate, post_commit)
        except NativeServiceBlocked:
            raise
        except Exception as exc:  # noqa: BLE001 - every owner failure becomes a typed block
            from src.extensions.capability_execution import CapabilityJournalError
            if isinstance(exc, (ValueError, PermissionError, CapabilityJournalError)):
                raise NativeServiceBlocked(_core_reason(exc)) from exc
            raise
        result = (blocked(outcome["blocked_reason"]) if outcome.get("blocked_reason")
                  else succeeded(method, outcome["value"]))
        digest = sealed_mutation_digest(result)
        receipt_state = {"method": method, "result_digest": digest, "effect_committed": True,
                         "inference": "none", **outcome["receipt"]}
        context["result"] = {"result_digest": digest, "projection": result, "outcome": outcome["receipt"]}
        await cas_run(db, run, checkpoint_context_json=_canonical(context),
                      checkpoint_receipts_json=_append_receipt(
                          run.checkpoint_receipts_json,
                          checkpoint_receipt(RESULT_CHECKPOINT, receipt_state, run.fencing_token),
                          key="checkpoint_id"),
                      effect_receipts_json=_append_receipt(
                          run.effect_receipts_json, _effect_receipt(run, method, digest, readback=False),
                          key="effect_id"))
        await db.commit()
    for callback in post_commit:
        callback()
    return result


def _effect_receipt(run, method: str, digest: str, *, readback: bool) -> dict:
    """A local, atomic effect: committed with its sealed result, never unknown."""
    now = datetime.now(timezone.utc).isoformat()
    receipt = {"effect_id": f"cordis-memory:{method}:{'readback' if readback else 'effect'}",
               "receipt_kind": "readback" if readback else "effect", "effect_type": "cordis_memory_mutation",
               "target_path": f"memory-mutation:{run.run_identity}", "target_digest": digest,
               "content_sha256": digest, "status": "succeeded",
               "details": {"verified": readback, "effect_committed": True, "inference": "none"},
               "recorded_at": now, "fencing_token": run.fencing_token}
    if readback:
        receipt["readback_id"] = f"cordis-memory-readback:{run.run_identity[-48:]}"
        receipt["verified_at"] = now
    return receipt


def _audit_row(run, event_type: str, details: dict):
    from src.db.models import AuditEvent
    # Added in the job's own session: ``audit_repository.log_event`` opens a
    # second session and would wait on this writer.
    return AuditEvent(session_id=run.session_id, actor=run.owner_principal_id, event_type=event_type,
                      tool_name="cordis_memory_mutation", risk_level="medium", policy_mode="operator_controlled",
                      summary="Operator-admitted Cordis memory mutation",
                      details_json=_canonical({"job_id": run.run_identity, **details}))


async def _propose(db, run, candidate, _post_commit) -> dict:
    from src.memory.m5 import create_memory_proposal_in_session
    payload = await create_memory_proposal_in_session(
        db, owner_principal_id=run.owner_principal_id, owner_session_id=run.operator_session_id,
        task_id=candidate["task_id"], expected_task_revision=candidate["expected_task_revision"],
        attempt_id=candidate["attempt_id"])
    proposal_id, revision = payload["proposal_id"], int(payload["revision"])
    state = "proposed" if payload.get("status") == "proposed" else "blocked"
    db.add(_audit_row(run, "memory_proposal_created", {
        "proposal_id": proposal_id, "revision": revision, "status": payload.get("status"),
        "reason_code": payload.get("reason_code")}))
    receipt = {"proposal_id": proposal_id, "revision": revision, "status": payload.get("status"),
               "memory_status": "proposal_only"}
    if payload.get("error_code") in _CORE_ERRORS:
        return {"blocked_reason": "native_memory_" + payload["error_code"], "receipt": receipt}
    return {"value": {"proposal_ref": proposal_id, "revision": revision, "state": state}, "receipt": receipt}


async def _apply_reviewed(db, run, candidate, _post_commit) -> dict:
    from src.db.models import MemoryProposal
    from src.memory.m5 import apply_memory_proposal_action_in_session
    proposal = await db.scalar(select(MemoryProposal).where(
        MemoryProposal.proposal_id == candidate["proposal_id"],
        MemoryProposal.owner_principal_id == run.owner_principal_id,
        MemoryProposal.owner_session_id == run.operator_session_id).execution_options(populate_existing=True))
    if proposal is None:
        raise NativeServiceBlocked("native_memory_owner_forbidden")
    current_mac = await review_binding_mac(
        proposal, action=candidate["action"], decision_effect=candidate["decision_effect"],
        corrects_memory_id=candidate["corrects_memory_id"] or proposal.corrects_memory_id,
        preferred_capability_id=candidate["preferred_capability_id"])
    if not hmac.compare_digest(current_mac, candidate["preview_mac"]):
        raise NativeServiceBlocked("native_memory_proposal_changed")
    payload = await apply_memory_proposal_action_in_session(
        db, owner_principal_id=run.owner_principal_id, owner_session_id=run.operator_session_id,
        proposal_id=candidate["proposal_id"], action=candidate["action"],
        expected_revision=candidate["expected_revision"],
        expected_preview_text_digest=proposal.preview_text_digest,
        expected_task_revision=candidate["expected_task_revision"],
        expected_goal_revision=candidate["expected_goal_revision"],
        decision_effect=candidate["decision_effect"] if candidate["action"] == "accept" else None,
        corrects_memory_id=candidate["corrects_memory_id"],
        preferred_capability_id=candidate["preferred_capability_id"],
        reason="cordis_operator_rejected" if candidate["action"] == "reject" else None)
    status = payload.get("status")
    receipt = {"proposal_id": payload["proposal_id"], "revision": int(payload["revision"]), "status": status,
               "action": candidate["action"], "accepted_memory_id": payload.get("accepted_memory_id"),
               "memory_status": "reviewed_update" if status == "accepted" else "no_learning",
               "audit_event_ref": payload.get("audit_event_id")}
    if payload.get("error_code") in _CORE_ERRORS or (candidate["action"] == "accept" and status != "accepted"):
        # The owner recorded a recovery state (signing outage, expiry): keep
        # it committed and seal a typed block rather than claim an update.
        return {"blocked_reason": "native_memory_" + (payload.get("error_code") or payload.get("reason_code")
                                                      or "proposal_not_accepted"), "receipt": receipt}
    if candidate["action"] == "reject" and status != "rejected":
        return {"blocked_reason": "native_memory_proposal_not_rejectable", "receipt": receipt}
    record_ref = payload.get("accepted_memory_id") if status == "accepted" else payload["proposal_id"]
    receipt_ref = payload.get("audit_event_id") or ("cordis-memory:" + run.run_identity[-48:])
    return {"value": {"record_ref": record_ref, "receipt_ref": receipt_ref}, "receipt": receipt}


async def _forget(db, run, candidate, post_commit) -> dict:
    from src.db.models import Memory
    from src.memory.control import _normalize_privacy_boundary, _operator_metadata
    from src.memory.repository import (_CANONICAL_MEMORY_DELETE_EXPORT_REASON, _CANONICAL_MEMORY_REDACTED_STATE,
                                       memory_repository)
    from src.memory.snapshots import invalidate_bounded_guardian_snapshot_cache
    memory = await db.get(Memory, candidate["memory_id"], populate_existing=True)
    _assert_memory_owner(memory, run.operator_session_id)
    actor = run.owner_principal_id
    boundary = _normalize_privacy_boundary(None)
    # Same operator metadata as the native ``propagate_delete_export``.
    written = await memory_repository.mark_memory_tombstoned_in_session(
        db, candidate["memory_id"], actor=actor, reason=_FORGET_REASON,
        metadata_updates={
            **_operator_metadata(action="propagate_delete_export", actor=actor, privacy_boundary=boundary,
                                 reason=_FORGET_REASON,
                                 extra={"delete_export_state": _CANONICAL_MEMORY_REDACTED_STATE,
                                        "provider_propagation_state":
                                            "runtime_receipt_only_no_full_provider_parity_claim"}),
            "archived_reason": _CANONICAL_MEMORY_DELETE_EXPORT_REASON,
            "archived_at": datetime.now(timezone.utc).isoformat()})
    tombstone = written.tombstone
    db.add(_audit_row(run, "memory_forgotten", {
        "memory_id": candidate["memory_id"], "tombstone_id": tombstone.id, "mode": "tombstone",
        "created": written.created, "operator_label": FORGET_LABEL,
        "derived_vector_purge": DERIVED_VECTOR_PURGE}))
    post_commit.append(invalidate_bounded_guardian_snapshot_cache)
    receipt = {"memory_id": candidate["memory_id"], "tombstone_id": tombstone.id, "created": written.created,
               "mode": "tombstone", "operator_label": FORGET_LABEL, "memory_status": "forgotten",
               "derived_vector_purge": DERIVED_VECTOR_PURGE}
    return {"value": {"record_ref": candidate["memory_id"], "receipt_ref": tombstone.id}, "receipt": receipt}


_EFFECTS = {"memory.propose": _propose, "memory.applyReviewed": _apply_reviewed, "memory.forget": _forget}


# ---------------------------------------------------------------- completion

def sealed_result(run) -> dict | None:
    context = read_context(run)
    sealed = context["result"]
    if sealed is None:
        return None
    if (type(sealed) is not dict or set(sealed) != {"result_digest", "projection", "outcome"}
            or not hmac.compare_digest(sealed_mutation_digest(sealed["projection"]), sealed["result_digest"])):
        raise NativeServiceBlocked("native_private_context_changed")
    return sealed


async def canonical_readback(db, run, sealed: dict) -> dict:
    """Prove the committed effect from canonical rows, not from authority."""
    from src.db.models import Memory, MemoryProposal, MemoryTombstone
    from src.memory.repository import _CANONICAL_MEMORY_DELETE_CONTENT
    candidate = read_context(run)["candidate"]
    method, outcome = candidate["method"], sealed["outcome"]
    ok = False
    if method in {"memory.propose", "memory.applyReviewed"}:
        proposal = await db.get(MemoryProposal, outcome["proposal_id"], populate_existing=True)
        ok = (proposal is not None and proposal.owner_session_id == run.operator_session_id
              and int(proposal.revision or 0) >= int(outcome["revision"]))
        if ok and method == "memory.applyReviewed" and sealed["projection"]["status"] == "succeeded":
            if outcome["status"] == "accepted":
                memory = await db.get(Memory, outcome["accepted_memory_id"], populate_existing=True)
                ok = (proposal.accepted_memory_id == outcome["accepted_memory_id"] and memory is not None
                      and memory.source_session_id == run.operator_session_id)
            else:
                ok = getattr(proposal.status, "value", proposal.status) == "rejected"
    elif method == "memory.forget":
        tombstone = await db.scalar(select(MemoryTombstone).where(
            MemoryTombstone.memory_id == outcome["memory_id"]).execution_options(populate_existing=True))
        memory = await db.get(Memory, outcome["memory_id"], populate_existing=True)
        ok = (tombstone is not None and tombstone.id == outcome["tombstone_id"] and memory is not None
              and memory.content == _CANONICAL_MEMORY_DELETE_CONTENT)
    if not ok:
        raise NativeServiceBlocked("native_memory_readback_mismatch")
    return {"method": method, "verified": True, **{key: value for key, value in outcome.items()
                                                  if key not in {"audit_event_ref"}}}


async def _record_readback(jobs, run_id: str, owner: str, fence: int) -> None:
    """Append the positive readback receipt under the still-held lease."""
    async with jobs._session() as db:
        await _writer(db)
        run = await jobs._fetch(db, run_id)
        if run.lease_owner != owner or run.fencing_token != fence or run.status != "running":
            raise NativeServiceBlocked("native_original_attempt_changed")
        sealed = sealed_result(run)
        if sealed is None:
            raise NativeServiceBlocked("native_memory_result_missing")
        await canonical_readback(db, run, sealed)
        await cas_run(db, run, effect_receipts_json=_append_receipt(
            run.effect_receipts_json, _effect_receipt(run, read_context(run)["candidate"]["method"],
                                                      sealed["result_digest"], readback=True), key="effect_id"))
        await db.commit()


async def complete_mutation(jobs, run_id: str, owner: str, fence: int, method: str) -> dict:
    proof: dict = {}

    async def readback(db, run):
        sealed = sealed_result(run)
        if sealed is None:
            raise NativeServiceBlocked("native_memory_result_missing")
        proof["sealed"] = sealed
        proof["readback"] = await canonical_readback(db, run, sealed)

    async with jobs._session() as db:
        sealed = sealed_result(await jobs._fetch(db, run_id))
    blocked_reason = sealed and sealed["projection"].get("reason_code")
    if blocked_reason:
        await jobs.transition_job(run_id, "blocked", owner=owner, fencing_token=fence, reason=blocked_reason)
        raise NativeServiceBlocked(blocked_reason)
    await _record_readback(jobs, run_id, owner, fence)
    job = await jobs.transition_job(
        run_id, "succeeded", owner=owner, fencing_token=fence,
        result={"method": method, "memory_status": sealed["projection"]["memory_status"] if sealed else None,
                "sealed_result_digest": sealed["result_digest"] if sealed else None,
                "effect_committed": True, "inference": "none"},
        result_summary="Operator-admitted Cordis memory mutation verified by canonical readback",
        terminal_authority_check=readback)
    return {"job": job, "result": proof["sealed"]["projection"], "readback": proof["readback"],
            "effect_committed": True, "replayed": False}


__all__ = ["DERIVED_VECTOR_PURGE", "FORGET_LABEL", "MEMORY_JOB_KIND", "MUTATION_METHODS", "OUTSTANDING_LIMIT",
           "canonical_readback", "complete_mutation", "dispatch_memory_mutation", "review_binding_mac",
           "sealed_result", "validate_mutation_candidate", "wire_inputs"]
