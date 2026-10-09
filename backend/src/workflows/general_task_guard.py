"""Fixed native tool-child authority; generic children retain their live fence."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, false, func, or_, select, update
from sqlalchemy.orm import aliased

from src.db.models import ApprovalRequest, OperatorSession, WorkBoardAttempt, WorkBoardInputArtifact, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.work_board.contracts import (
    GENERAL_TASK_MANIFEST_KEY, GENERAL_TASK_NATIVE_CHILD_KIND,
    GeneralTaskCurrentManifestV1, GeneralTaskNativeChildBindingV1,
    GeneralTaskApprovalTransitionV1, GeneralTaskToolClosureV1,
    GeneralTaskCheckpointReservationV1, GeneralTaskNativeCancelChildV1, GeneralTaskNativeCancelV1,
    RepositoryNativeStopClosureV1,
)

_NATIVE_CHECKPOINT_BYTES = 4 * 1024 * 1024
_NATIVE_PAYLOAD_BYTES = 65536
_REPOSITORY_CHECKPOINT_PREFIX = "repository:"
_REPOSITORY_CHILD_WAIT_PREFIX = "repository:child-wait:"
_REPOSITORY_CHILD_FINAL_PREFIX = "repository:child-final:"
_REPOSITORY_WITNESS_SEAL = object()


@dataclass(frozen=True, slots=True)
class RepositoryChildWaitWitness:
    """Source-issued proof for a contacted repository child wait.

    The repository producer keeps the canonical row/physical snapshots in the
    private ``_source_binding`` object.  A public projection deliberately
    omits that object and the guard seal, so copying JSON cannot authorize a
    wait or a later wake.  This witness describes callback transport
    quiescence only; it is not a subprocess or repair-success receipt.
    """

    native_binding: GeneralTaskNativeChildBindingV1
    repository_job_id: str
    repository_attempt_id: str
    repository_fence: int
    iteration_index: int
    iteration_id: str
    source_checkpoint_digest: str
    source_binding_digest: str
    request_body_digest: str
    response_readback_digest: str
    callback_quiescence_digest: str
    _source_binding: Any = field(default=None, repr=False, compare=False)
    _seal: object = field(default=None, repr=False, compare=False)

    def projection(self) -> dict[str, Any]:
        return {
            "schema_version": "repository.child_wait_witness.v1",
            "native_binding": self.native_binding.model_dump(mode="json"),
            "repository_job_id": self.repository_job_id,
            "repository_attempt_id": self.repository_attempt_id,
            "repository_fence": self.repository_fence,
            "iteration_index": self.iteration_index,
            "iteration_id": self.iteration_id,
            "source_checkpoint_digest": self.source_checkpoint_digest,
            "source_binding_digest": self.source_binding_digest,
            "request_body_digest": self.request_body_digest,
            "response_readback_digest": self.response_readback_digest,
            "callback_quiescence_digest": self.callback_quiescence_digest,
            "no_learning": True,
        }


@dataclass(frozen=True, slots=True)
class RepositoryChildFinalWitness:
    """Source-issued proof for final cumulative repository adoption."""

    wait_witness: RepositoryChildWaitWitness
    final_patch_digest: str
    final_manifest_digest: str
    final_readback_digest: str
    final_command_receipt_digest: str
    final_cleanup_digest: str
    final_accounting_digest: str
    final_artifact_id: str
    final_artifact_digest: str
    requested_check_exits_digest: str
    all_iteration_ids_digest: str
    no_learning: bool = True
    _source_binding: Any = field(default=None, repr=False, compare=False)
    _seal: object = field(default=None, repr=False, compare=False)

    def projection(self) -> dict[str, Any]:
        return {
            "schema_version": "repository.child_final_witness.v1",
            "wait_witness": self.wait_witness.projection(),
            "final_patch_digest": self.final_patch_digest,
            "final_manifest_digest": self.final_manifest_digest,
            "final_readback_digest": self.final_readback_digest,
            "final_command_receipt_digest": self.final_command_receipt_digest,
            "final_cleanup_digest": self.final_cleanup_digest,
            "final_accounting_digest": self.final_accounting_digest,
            "final_artifact_id": self.final_artifact_id,
            "final_artifact_digest": self.final_artifact_digest,
            "requested_check_exits_digest": self.requested_check_exits_digest,
            "all_iteration_ids_digest": self.all_iteration_ids_digest,
            "no_learning": self.no_learning,
        }


def _repository_digest(value: Any) -> str:
    from src.workflows.job_runtime import _digest
    return _digest(value)


def _repository_witness_source_digest(source_binding: Any) -> str:
    from src.workflows.job_runtime import DurableJobLeaseError
    try:
        from src.workflows.repo_repair_source import (
            _CanonicalRepositorySource,
            assert_repository_canonical_source,
        )
    except (ImportError, AttributeError) as exc:
        raise DurableJobLeaseError("repository canonical source issuer unavailable") from exc
    if type(source_binding) is not _CanonicalRepositorySource:
        raise DurableJobLeaseError("repository canonical source issuer required")
    try:
        valid = assert_repository_canonical_source(source_binding)
    except Exception as exc:
        raise DurableJobLeaseError("repository canonical source snapshot is invalid") from exc
    if valid is False:
        raise DurableJobLeaseError("repository canonical source snapshot is invalid")
    projection = getattr(source_binding, "projection", None)
    if not callable(projection):
        raise DurableJobLeaseError("repository source snapshot is unavailable")
    return _repository_digest(projection())


def issue_repository_child_wait_witness(*, native_binding, source_binding,
    repository_job_id: str, repository_attempt_id: str, repository_fence: int,
    iteration_index: int, iteration_id: str, source_checkpoint_digest: str,
    request_body_digest: str, response_readback_digest: str,
    callback_quiescence_digest: str) -> RepositoryChildWaitWitness:
    """Private source adapter factory; no request path calls this directly."""
    witness = RepositoryChildWaitWitness(
        native_binding=native_binding,
        repository_job_id=repository_job_id,
        repository_attempt_id=repository_attempt_id,
        repository_fence=repository_fence,
        iteration_index=iteration_index,
        iteration_id=iteration_id,
        source_checkpoint_digest=source_checkpoint_digest,
        source_binding_digest=_repository_witness_source_digest(source_binding),
        request_body_digest=request_body_digest,
        response_readback_digest=response_readback_digest,
        callback_quiescence_digest=callback_quiescence_digest,
        _source_binding=source_binding,
        _seal=_REPOSITORY_WITNESS_SEAL,
    )
    _assert_repository_child_wait_witness_shape(witness)
    return witness


def issue_repository_child_final_witness(*, wait_witness, source_binding,
    final_patch_digest: str, final_manifest_digest: str,
    final_readback_digest: str, final_command_receipt_digest: str,
    final_cleanup_digest: str, final_accounting_digest: str,
    final_artifact_id: str, final_artifact_digest: str,
    requested_check_exits_digest: str,
    all_iteration_ids_digest: str) -> RepositoryChildFinalWitness:
    witness = RepositoryChildFinalWitness(
        wait_witness=wait_witness,
        final_patch_digest=final_patch_digest,
        final_manifest_digest=final_manifest_digest,
        final_readback_digest=final_readback_digest,
        final_command_receipt_digest=final_command_receipt_digest,
        final_cleanup_digest=final_cleanup_digest,
        final_accounting_digest=final_accounting_digest,
        final_artifact_id=final_artifact_id,
        final_artifact_digest=final_artifact_digest,
        requested_check_exits_digest=requested_check_exits_digest,
        all_iteration_ids_digest=all_iteration_ids_digest,
        _source_binding=source_binding,
        _seal=_REPOSITORY_WITNESS_SEAL,
    )
    _assert_repository_child_final_witness_shape(witness)
    return witness


def _assert_repository_digest(value: Any, field_name: str) -> None:
    import re
    from src.workflows.job_runtime import DurableJobLeaseError
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise DurableJobLeaseError(f"repository {field_name} must be a lowercase SHA-256")


def _assert_repository_child_wait_witness_shape(witness: Any) -> None:
    from src.workflows.job_runtime import DurableJobLeaseError
    if (type(witness) is not RepositoryChildWaitWitness
        or witness._seal is not _REPOSITORY_WITNESS_SEAL
        or type(witness.native_binding) is not GeneralTaskNativeChildBindingV1
        or type(witness.repository_fence) is not int or witness.repository_fence < 1
        or type(witness.iteration_index) is not int or not 1 <= witness.iteration_index <= 3
        or not witness.repository_job_id or not witness.repository_attempt_id
        or witness.native_binding.invocation_id == ""
        or witness.native_binding.parent_job_id == ""
        or witness.native_binding.invocation_id == witness.repository_job_id):
        raise DurableJobLeaseError("sealed repository child wait witness required")
    for name in ("iteration_id", "source_checkpoint_digest", "source_binding_digest",
        "request_body_digest", "response_readback_digest", "callback_quiescence_digest"):
        _assert_repository_digest(getattr(witness, name), name)
    if witness._source_binding is None or _repository_witness_source_digest(witness._source_binding) != witness.source_binding_digest:
        raise DurableJobLeaseError("repository child wait source snapshot changed")


def _assert_repository_child_final_witness_shape(witness: Any) -> None:
    from src.workflows.job_runtime import DurableJobLeaseError
    if (type(witness) is not RepositoryChildFinalWitness
        or witness._seal is not _REPOSITORY_WITNESS_SEAL
        or witness.no_learning is not True
        or witness._source_binding is None):
        raise DurableJobLeaseError("sealed repository child final witness required")
    _assert_repository_child_wait_witness_shape(witness.wait_witness)
    for name in ("final_patch_digest", "final_manifest_digest", "final_readback_digest",
        "final_command_receipt_digest", "final_cleanup_digest", "final_accounting_digest",
        "final_artifact_digest", "requested_check_exits_digest", "all_iteration_ids_digest"):
        _assert_repository_digest(getattr(witness, name), name)
    if not witness.final_artifact_id or _repository_witness_source_digest(witness._source_binding) != witness.wait_witness.source_binding_digest:
        raise DurableJobLeaseError("repository child final source snapshot changed")


def cancel_checkpoint_id(parent_id, attempt_id):
    from src.workflows.job_runtime import _digest
    return "general:cancel:" + _digest([parent_id, attempt_id])


def repository_child_wait_checkpoint_id(binding, iteration_id: str) -> str:
    """Derive the source-owned wait identity from the original child only."""
    from src.workflows.job_runtime import DurableJobLeaseError, _digest
    if not isinstance(iteration_id, str) or not iteration_id:
        raise DurableJobLeaseError("repository iteration identity is required")
    return _REPOSITORY_CHILD_WAIT_PREFIX + _digest([
        binding.parent_job_id, binding.invocation_id, binding.input_digest, iteration_id,
    ])


def repository_child_final_checkpoint_id(binding, iteration_id: str) -> str:
    from src.workflows.job_runtime import DurableJobLeaseError, _digest
    if not isinstance(iteration_id, str) or not iteration_id:
        raise DurableJobLeaseError("repository iteration identity is required")
    return _REPOSITORY_CHILD_FINAL_PREFIX + _digest([
        binding.parent_job_id, binding.invocation_id, binding.input_digest, iteration_id,
    ])


def _repository_checkpoint_payload(witness, *, phase: str, checkpoint_id: str) -> dict[str, Any]:
    from src.workflows.job_runtime import DurableJobTransitionError, _digest
    projection = witness.projection()
    payload = {
        "schema_version": projection["schema_version"],
        "checkpoint_id": checkpoint_id,
        "phase": phase,
        "witness": projection,
        "witness_digest": _digest(projection),
        "no_learning": True,
    }
    if len(json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")) > _NATIVE_PAYLOAD_BYTES:
        raise DurableJobTransitionError("repository child checkpoint exceeds 64 KiB")
    return payload


def _check_reserved_capacity(history):
    """Existing bytes plus full reserved replacement records; never evict."""
    from src.workflows.job_runtime import _canonical, _digest, DurableJobTransitionError
    if len(history) > 50:
        raise DurableJobTransitionError("general task whole checkpoint count capacity reached")
    total = len(_canonical(history).encode("utf-8"))
    for record in history:
        payload = record.get("payload", {})
        schema = payload.get("schema_version") if isinstance(payload, dict) else None
        if schema == "general_task.checkpoint_reservation.v1":
            reservation = GeneralTaskCheckpointReservationV1.model_validate(payload)
            if (record.get("checkpoint_id") != reservation.checkpoint_id or record.get("safe") is not True
                or record.get("state_digest") != _digest(reservation.model_dump(mode="json"))):
                raise DurableJobTransitionError("native reservation key changed")
            maximum = _NATIVE_PAYLOAD_BYTES
        elif (record.get("checkpoint_id") == GENERAL_TASK_MANIFEST_KEY
            or (isinstance(record.get("checkpoint_id"), str)
                and record.get("checkpoint_id").startswith(_REPOSITORY_CHECKPOINT_PREFIX))
            or schema in {
                "general_task.native_cancel.v1", "general_task.tool_closure.v1",
                "general_task.native_approval_transition.v1",
                "repository.child_wait_witness.v1", "repository.child_final_witness.v1",
                "SpecialistDelegationCancel.v1", "SpecialistPartialDecision.v1", "SpecialistPartialResult.v1",
                "SpecialistPartialArtifactProof.v1"}):
            maximum = _NATIVE_PAYLOAD_BYTES
        else:
            continue
        size = len(_canonical(payload).encode("utf-8"))
        if size > maximum:
            raise DurableJobTransitionError("native closed checkpoint exceeds 64 KiB")
        # All JSON/key overhead of the actual record is already included above.
        # Replacement metadata has a conservative fixed 4-KiB ceiling.
        total += maximum - size + 4096
    if total > _NATIVE_CHECKPOINT_BYTES:
        raise DurableJobTransitionError("general task reserved checkpoint byte capacity reached")


def _reserve_native_capacity(parent, manifest, binding=None, *, envelope=None, service=None, capacity_witness=None):
    from src.workflows.job_runtime import _canonical, _digest, _utc_now
    from types import SimpleNamespace
    history = _history(parent)
    from src.work_board.general_task import GeneralTaskService
    if service is not None and (type(service) is not GeneralTaskService or not service.started):
        from src.workflows.job_runtime import DurableJobLeaseError
        raise DurableJobLeaseError("fixed active task service capacity owner required")
    identities = [(cancel_checkpoint_id(parent.run_identity, manifest.attempt_id), None)]
    if parent.parent_job_id is None and envelope is not None and any(
        descriptor.tool_id == "delegate_task" for descriptor in envelope.descriptors):
        from src.workflows.specialist_partial import partial_checkpoint_ids
        identities += [(identity,None) for identity in partial_checkpoint_ids(parent.run_identity,manifest.attempt_id)]
    group = envelope.proposal_group if envelope is not None else None
    trace_reachable = (envelope is None or (envelope.task_input.inference_egress_acknowledged
        and envelope.task_input.limits.max_inference_calls > 0 and envelope.task_input.limits.max_cost_microusd > 0
        and group is not None and group.max_inference_calls > 0 and group.max_cost_microusd > 0
        and (service is None or service.planner is not None)))
    if trace_reachable:
        identities.append(("general:continuation-budget:" + _digest([manifest.creation_digest, manifest.group_id]), None))
    mode, classifier_digest = "approval_capable", None
    if binding is not None:
        if capacity_witness is not None:
            from src.native_tools.task_adapters import verify_task_tool_capacity
            approval_possible, classifier_digest = verify_task_tool_capacity(capacity_witness,
                descriptor_digest=binding.descriptor_digest)
            if not approval_possible:
                mode = "no_approval"
        identities += [(cleanup_checkpoint_id(binding, 1), 1),
            ("general:artifact:" + binding.step_id, None), ("general:verified:" + binding.step_id, None)]
        if ((capacity_witness is not None and capacity_witness.descriptor.tool_id == "delegate_task")
            or (envelope is not None and envelope.plan is not None and any(
                step.step_id == binding.step_id and step.tool_id == "delegate_task" for step in envelope.plan.steps))):
            from src.workflows.specialist_stop import stop_checkpoint_id
            identities.append((stop_checkpoint_id(binding.invocation_id), None))
        if mode == "approval_capable":
            identities += [(approval_checkpoint_id(binding), None), (cleanup_checkpoint_id(binding, 2), 2)]
    present = {item["checkpoint_id"] for item in history}
    for identity, callback_fence in identities:
        if identity in present:
            continue
        reservation = GeneralTaskCheckpointReservationV1(parent_job_id=parent.run_identity,
            attempt_id=manifest.attempt_id, creation_digest=manifest.creation_digest,
            checkpoint_id=identity, invocation_id=binding.invocation_id if binding else None,
            binding_digest=_digest(binding.model_dump(mode="json")) if binding else None,
            callback_fence=callback_fence, capacity_mode=mode, classifier_digest=classifier_digest)
        payload = reservation.model_dump(mode="json")
        history.append({"checkpoint_id": identity, "safe": True, "payload": payload,
            "state_digest": _digest(payload), "state_keys": sorted(payload),
            "fencing_token": manifest.job_fence, "recorded_at": _utc_now().isoformat()})
    if len({GENERAL_TASK_MANIFEST_KEY, *(item["checkpoint_id"] for item in history
        if isinstance(item.get("checkpoint_id"), str)
        and item["checkpoint_id"].startswith(("general:", _REPOSITORY_CHECKPOINT_PREFIX)))}) > 50:
        from src.workflows.job_runtime import DurableJobTransitionError
        raise DurableJobTransitionError("general task reserved checkpoint count capacity reached")
    _check_reserved_capacity(history)
    return SimpleNamespace(run_identity=parent.run_identity, job_kind=parent.job_kind,
        checkpoint_receipts_json=_canonical(history), artifact_receipts_json=parent.artifact_receipts_json,
        authority_digest=parent.authority_digest, input_digest=parent.input_digest)


def _require_callback_reservation(parent, binding, fence):
    from src.workflows.job_runtime import _digest, DurableJobLeaseError
    identity = cleanup_checkpoint_id(binding, fence)
    records = [item for item in _history(parent) if item.get("checkpoint_id") == identity]
    if len(records) == 1 and records[0].get("payload", {}).get("schema_version") == "general_task.tool_closure.v1":
        closure = _protected_payload(parent, identity, GeneralTaskToolClosureV1)
        if (closure.original_binding_digest == _digest(binding.model_dump(mode="json"))
            and closure.invocation_id == binding.invocation_id and closure.child_fence == fence
            and closure.input_digest == binding.input_digest and closure.descriptor_digest == binding.descriptor_digest):
            return
        raise DurableJobLeaseError("original callback closure changed")
    if len(records)==1 and records[0].get("payload",{}).get("schema_version")=="SpecialistDelegationClosure.v1":
        from src.workflows.specialist_lifecycle import SpecialistDelegationClosureV1
        # The fixed writer binds this slot to the exact delegate input/Wait.
        # This synchronous reservation projection cannot grant contact.
        closure = _protected_payload(parent,identity,SpecialistDelegationClosureV1)
        if (closure.invocation_id==binding.invocation_id and closure.original_claim_fence==fence
            and closure.original_binding_digest==_digest(binding.model_dump(mode="json"))
            and closure.outcome=="durable_result_verified" and not closure.unresolved):
            return
        raise DurableJobLeaseError("original delegation closure changed")
    reservation = _protected_payload(parent, identity, GeneralTaskCheckpointReservationV1)
    if (reservation.parent_job_id != parent.run_identity or reservation.attempt_id != binding.attempt_id
        or reservation.creation_digest != binding.creation_digest or reservation.invocation_id != binding.invocation_id
        or reservation.binding_digest != _digest(binding.model_dump(mode="json"))
        or reservation.callback_fence != fence):
        raise DurableJobLeaseError("original callback closure reservation changed")


def read_native_checkpoint_reservation(parent, identity):
    """Pure typed placeholder projection; never output, closure or authority."""
    from src.workflows.job_runtime import DurableJobLeaseError
    manifest = read_manifest(parent)
    reservation = _protected_payload(parent, identity, GeneralTaskCheckpointReservationV1)
    if (manifest is None or identity not in manifest.required_checkpoint_ids
        or reservation.parent_job_id != manifest.run_id or reservation.attempt_id != manifest.attempt_id
        or reservation.creation_digest != manifest.creation_digest
        or (reservation.invocation_id is not None and reservation.invocation_id not in manifest.admitted_invocation_ids)):
        raise DurableJobLeaseError("native protected reservation scope changed")
    return reservation


def assert_native_callback_capacity(parent, binding, fence, *, capacity_witness):
    """Private current producer proof gates narrowed callback capacity."""
    from src.native_tools.task_adapters import verify_task_tool_capacity
    from src.workflows.job_runtime import DurableJobLeaseError
    _require_callback_reservation(parent, binding, fence)
    reservation = _protected_payload(parent, cleanup_checkpoint_id(binding, fence), GeneralTaskCheckpointReservationV1)
    if reservation.capacity_mode == "no_approval":
        approval_possible, classifier_digest = verify_task_tool_capacity(capacity_witness,
            descriptor_digest=binding.descriptor_digest)
        if approval_possible or reservation.classifier_digest is None or classifier_digest != reservation.classifier_digest:
            raise DurableJobLeaseError("original no-approval callback capacity policy changed")


async def _ensure_future_cancel_capacity(db, parent, task, attempt, manifest, *, candidate=None):
    """Size a conservative future witness; never publish synthetic evidence.

    The local dictionaries below exist only for counting UTF-8 bytes. All
    immutable bindings come from canonical rows/the fixed admission candidate.
    Future receipt references, counters and authentic closure metadata reserve
    their closed maxima before contact; no callback or authority is created.
    """
    from src.workflows.job_runtime import _canonical, _digest, DurableJobTransitionError
    rows = list((await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.parent_job_id == parent.run_identity))).scalars())
    bindings = {row.run_identity: child_binding(row) for row in rows}
    if candidate is not None:
        if candidate.invocation_id in bindings and bindings[candidate.invocation_id] != candidate:
            raise DurableJobTransitionError("future cancellation original binding changed")
        bindings[candidate.invocation_id] = candidate
    if set(bindings) != set(manifest.admitted_invocation_ids):
        raise DurableJobTransitionError("future cancellation admitted set changed")
    maximum_counter = 2 ** 63 - 1
    future_manifest = manifest.model_dump(mode="json")
    for field in ("task_revision", "manifest_revision", "phase_revision", "board_fence", "job_fence"):
        future_manifest[field] = maximum_counter
    future_manifest["required_checkpoint_ids"] = sorted({record["checkpoint_id"] for record in _history(parent)
        if record["checkpoint_id"].startswith("general:")})
    # Every admitted step may acquire/change a receipt before the next safe
    # assembly/revision writer. Reserve all of these metadata maxima now.
    steps = sorted({binding.step_id for binding in bindings.values()})
    future_manifest.update(step_ids=steps, step_receipt_artifact_ids=["x" * 128 for _ in steps],
        step_receipt_digests=["a" * 64 for _ in steps], step_receipt_schemas=["StepReceipt.v1" for _ in steps])
    entries = []
    for binding in bindings.values():
        closure = GeneralTaskToolClosureV1(original_binding_digest=_digest(binding.model_dump(mode="json")),
            invocation_id=binding.invocation_id, child_fence=maximum_counter,
            descriptor_digest=binding.descriptor_digest, input_digest=binding.input_digest,
            outcome="approval_precontact", approval_id="x" * 128, approval_fingerprint="a" * 64)
        entries.append({"original_binding": binding.model_dump(mode="json"),
            "original_binding_digest": _digest(binding.model_dump(mode="json")), "original_attempt_count": 1,
            "original_claim_fence": maximum_counter, "original_revision": maximum_counter,
            "current_child_fence": maximum_counter, "current_child_revision": maximum_counter,
            "effect_digest": "a" * 64, "artifact_digest": "a" * 64, "checkpoint_digest": "a" * 64,
            "closure": closure.model_dump(mode="json"), "effect_debt": False, "no_learning": True})
    prospective = {"schema_version": "general_task.native_cancel.v1", "original_manifest": future_manifest,
        "original_parent_authority_digest": parent.authority_digest, "original_parent_input_digest": parent.input_digest,
        "input_artifact_id": task.input_artifact_id, "typed_input_ref": task.typed_input_ref,
        "typed_input_digest": task.typed_input_digest, "goal_id": task.goal_id, "goal_revision": task.goal_revision,
        "task_revision": maximum_counter, "manifest_revision": maximum_counter, "phase_revision": maximum_counter,
        "phase_digest": "a" * 64, "board_fence": maximum_counter, "job_fence": maximum_counter,
        "phase": "unknown_recovery", "state": "callback_closed_outcome_debt", "children": entries, "no_learning": True}
    # Longer state/phase literals and record representation differences have a
    # fixed margin; all actual parent journals still use the separate 4-MiB cap.
    if len(_canonical(prospective).encode("utf-8")) + 512 > _NATIVE_PAYLOAD_BYTES:
        raise DurableJobTransitionError("general task future cancellation witness capacity reached")


def approval_checkpoint_id(binding):
    from src.work_board.general_task import digest
    return "general:approval:" + digest(binding.invocation_id)


def cleanup_checkpoint_id(binding, fence):
    from src.work_board.general_task import digest
    return "general:cleanup:" + digest([binding.invocation_id, fence])


def _protected_payload(parent, identity, model):
    from src.workflows.job_runtime import DurableJobLeaseError, _digest
    records = [record for record in _history(parent) if record.get("checkpoint_id") == identity]
    try:
        if len(records) != 1 or records[0].get("safe") is not True:
            raise ValueError()
        parsed = model.model_validate(records[0]["payload"])
        if records[0].get("state_digest") != _digest(parsed.model_dump(mode="json")):
            raise ValueError()
        return parsed
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableJobLeaseError("native protected transition evidence changed") from exc


@dataclass(frozen=True)
class EffectiveChildPhase:
    phase_revision: int
    phase_digest: str
    child_fence: int
    approval_binding_digest: str | None = None


_PHASE_SQL_SEAL = object()


@dataclass(frozen=True)
class _VerifiedParentJournal:
    child_id: str
    child_fence: int
    checkpoint_json: str
    authority_json: str
    _seal: object
    specialist_callback_id: str | None = None
    specialist_callback_fence: int | None = None
    specialist_parent_id: str | None = None
    specialist_parent_checkpoints: str | None = None
    specialist_callback_checkpoints: str | None = None
    specialist_callback_effects: str | None = None
    specialist_callback_waiting: bool = False


def assert_original_parent_authority(parent):
    """Require original declared bytes to match their immutable sealed digest."""
    from src.workflows.job_runtime import DurableJobLeaseError, _digest
    try:
        authority = json.loads(parent.declared_authority_json)
        if (not isinstance(authority, dict) or _digest(authority) != parent.authority_digest
            or authority.get("capability_id") != "agent.task.v1" or authority.get("capability_version") != "1"
            or authority.get("principal") != parent.owner_principal_id
            or authority.get("session_id") != parent.operator_session_id
            or authority.get("goal_owner_principal_id") != parent.owner_principal_id
            or authority.get("goal_owner_session_id") != parent.operator_session_id):
            raise ValueError()
    except (TypeError, ValueError) as exc:
        raise DurableJobLeaseError("original native parent declared authority changed") from exc


def approved_receipt_digest(approval, context_digest):
    """Immutable decision scope survives the sole approved->consumed write."""
    from src.work_board.general_task import digest
    # Existing single consumption also advances resolved_at. Neither status
    # nor that mutable timestamp can be part of immutable approved scope.
    return digest([approval.id, approval.fingerprint, str(approval.expires_at),
        approval.owner_principal_id, approval.operator_session_id,
        approval.session_id, approval.tool_name, approval.action, context_digest])


async def effective_child_phase(db, child, parent=None):
    from src.workflows.job_runtime import DurableJobLeaseError, _digest, _as_utc, _utc_now
    from src.work_board.general_task_runtime_artifacts import read_native_artifact_reference
    binding = child_binding(child)
    parent = parent or await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == binding.parent_job_id))
    manifest = read_manifest(parent) if parent is not None else None
    if manifest is None:
        raise DurableJobLeaseError("native original manifest is unavailable")
    if (manifest.phase == "native_wait" and manifest.phase_revision == binding.phase_revision
        and manifest.phase_digest == binding.phase_digest):
        return EffectiveChildPhase(binding.phase_revision, binding.phase_digest, child.fencing_token)
    witness, _approval = await verify_native_approval_transition(db, child, parent)
    if witness.phase != "native_wait":
        raise DurableJobLeaseError("native approval wait cannot authorize execution")
    return EffectiveChildPhase(witness.phase_revision, witness.phase_digest,
        witness.current_child_fence, _digest(witness.model_dump(mode="json")))


async def verify_native_approval_transition(db, child, parent):
    from src.workflows.job_runtime import DurableJobLeaseError, _digest, _as_utc, _utc_now
    from src.work_board.general_task_runtime_artifacts import read_native_artifact_reference
    assert_original_parent_authority(parent)
    binding = child_binding(child)
    manifest = read_manifest(parent)
    witness = _protected_payload(parent, approval_checkpoint_id(binding), GeneralTaskApprovalTransitionV1)
    payload = witness.model_dump(mode="json")
    approval = await db.get(ApprovalRequest, witness.approval_id)
    try:
        details = json.loads(approval.details_json) if approval is not None else None
        if (witness.original_binding != binding
            or witness.current_child_fence != child.fencing_token or child.attempt_count != 1
            or any(getattr(witness, field) != getattr(manifest, field) for field in
                ("phase", "phase_revision", "phase_digest", "task_revision", "board_fence", "job_fence"))
            or approval is None or approval.status not in (
                {"pending", "approved"} if witness.phase == "approval_wait" else {"approved", "consumed"})
            or approval.owner_principal_id != binding.owner_principal_id
            or approval.operator_session_id != binding.original_root_id
            or approval.session_id != binding.original_root_id
            or approval.fingerprint != witness.approval_fingerprint
            or _as_utc(approval.expires_at) is None or _as_utc(approval.expires_at) <= _utc_now()
            or not isinstance(details, dict) or details.get("general_task_wait_binding") != payload
            or _digest(details.get("approval_context")) != witness.approval_context_digest
            or details.get("approval_context", {}).get("workflow_run_identity") != child.run_identity
            or (witness.phase == "native_wait" and approved_receipt_digest(approval,
                witness.approval_context_digest) != witness.approved_receipt_digest)):
            raise ValueError()
        awaiting = read_native_artifact_reference(witness.awaiting_receipt,
            parent_job_id=parent.run_identity, creation_digest=binding.creation_digest)
        closure = _protected_payload(parent, cleanup_checkpoint_id(binding, witness.original_claim_fence), GeneralTaskToolClosureV1)
        if (awaiting.status != "awaiting_approval" or awaiting.approval_id != approval.id
            or awaiting.child_job_id != child.run_identity or awaiting.child_fence != witness.waiting_child_fence
            or awaiting.child_attempt_count != 1 or awaiting.invocation_id != binding.invocation_id
            or awaiting.task_id != binding.task_id or awaiting.attempt_id != binding.attempt_id
            or awaiting.input_digest != binding.input_digest or awaiting.descriptor_digest != binding.descriptor_digest
            or awaiting.selected_grant_digest != binding.selected_grant_digest
            or awaiting.parent_creation_digest != binding.creation_digest
            or awaiting.effect_receipt_digest != witness.no_contact_effect_digest
            or awaiting.cleanup_receipt_digest != witness.cleanup_receipt_digest
            or closure.outcome != "approval_precontact" or closure.approval_id != approval.id
            or closure.approval_fingerprint != witness.approval_fingerprint
            or closure.original_binding_digest != witness.original_binding_digest
            or closure.invocation_id != child.run_identity or closure.child_fence != witness.original_claim_fence
            or closure.input_digest != binding.input_digest or closure.descriptor_digest != binding.descriptor_digest
            or _digest(closure.model_dump(mode="json")) != witness.cleanup_receipt_digest):
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableJobLeaseError("native exact approved transition is unavailable") from exc
    return witness, approval


def read_manifest(run):
    """Never interpret damaged protected state as a fresh empty execution."""
    from src.workflows.job_runtime import DurableJobTransitionError, _digest
    try:
        records = json.loads(run.checkpoint_receipts_json or "[]")
        if not isinstance(records, list):
            raise ValueError()
        matching = [item for item in records if isinstance(item, dict)
                    and item.get("checkpoint_id") == GENERAL_TASK_MANIFEST_KEY]
        if not matching:
            return None
        if len(matching) != 1:
            raise ValueError()
        receipt = matching[0]
        manifest = GeneralTaskCurrentManifestV1.model_validate(receipt["payload"])
        if (receipt.get("safe") is not True
            or receipt.get("state_digest") != _digest(manifest.model_dump(mode="json"))):
            raise ValueError()
        return manifest
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableJobTransitionError("general task protected manifest is malformed") from exc


def protected_checkpoint_ids(history):
    """Protect native identities already committed by the fixed writer."""
    from types import SimpleNamespace
    from src.workflows.job_runtime import DurableJobTransitionError
    from src.workflows.job_runtime import _digest
    repository = [item for item in history if isinstance(item, dict)
        and isinstance(item.get("checkpoint_id"), str) and item["checkpoint_id"].startswith("repository:")]
    repository_ids = {item["checkpoint_id"] for item in repository}
    if (len(repository_ids) != len(repository) or len(repository_ids) > 50
            or any(item.get("safe") is not True or not isinstance(item.get("payload"), dict)
                or item.get("state_digest") != _digest(item["payload"]) for item in repository)):
        raise DurableJobTransitionError("repository protected checkpoint journal is malformed")
    from src.workflows.specialist_delegation import DELEGATION_KEY, read_reservation
    delegation = [item for item in history if isinstance(item, dict)
        and item.get("checkpoint_id") == DELEGATION_KEY]
    delegated_ids = set()
    if delegation:
        identity = delegation[0].get("payload", {}).get("delegation_invocation_id")
        original = SimpleNamespace(checkpoint_receipts_json=json.dumps(history), run_identity=identity)
        read_reservation(original)
        delegated_ids.add(DELEGATION_KEY)
        from src.workflows.specialist_lifecycle import (read_fact,CREATION_KEY,ADMISSION_KEY,WAIT_KEY,CLOSURE_KEY,
            SpecialistChildCreationV1,SpecialistChildAdmissionV1,SpecialistWaitV1,SpecialistDelegationClosureV1)
        known = {CREATION_KEY:SpecialistChildCreationV1,ADMISSION_KEY:SpecialistChildAdmissionV1,
            WAIT_KEY:SpecialistWaitV1,CLOSURE_KEY:SpecialistDelegationClosureV1}
        for record in history:
            key = record.get("checkpoint_id", "") if isinstance(record,dict) else ""
            if key.startswith("general:delegation:") and key != DELEGATION_KEY:
                if key not in known or read_fact(original,key,known[key]) is None:
                    raise DurableJobTransitionError("unknown protected specialist proof")
                delegated_ids.add(key)
        if ADMISSION_KEY in delegated_ids and CREATION_KEY not in delegated_ids:
            raise DurableJobTransitionError("specialist admission lacks original publication")
        if WAIT_KEY in delegated_ids and ADMISSION_KEY not in delegated_ids:
            raise DurableJobTransitionError("specialist wait lacks original admitted child")
    manifest = read_manifest(SimpleNamespace(checkpoint_receipts_json=json.dumps(history)))
    if manifest is None:
        return repository_ids | delegated_ids
    _check_reserved_capacity(history)
    protected = {GENERAL_TASK_MANIFEST_KEY, *manifest.required_checkpoint_ids, *delegated_ids, *repository_ids}
    from src.workflows.specialist_stop import SpecialistDelegationCancelV1, stop_checkpoint_id
    for record in history:
        key = record.get("checkpoint_id", "") if isinstance(record,dict) else ""
        if key.startswith("general:specialist-stop:"):
            payload = record.get("payload", {})
            if payload.get("schema_version") == "SpecialistDelegationCancel.v1":
                fact = _protected_payload(SimpleNamespace(checkpoint_receipts_json=json.dumps(history)), key, SpecialistDelegationCancelV1)
                if key != stop_checkpoint_id(fact.invocation_id):
                    raise DurableJobTransitionError("specialist cancellation key changed")
            elif payload.get("schema_version") != "general_task.checkpoint_reservation.v1":
                raise DurableJobTransitionError("unknown specialist stop proof")
            protected.add(key)
    present = {item.get("checkpoint_id") for item in history if isinstance(item, dict)}
    if not protected.issubset(present):
        raise DurableJobTransitionError("general task required checkpoint proof is missing")
    if len(protected) > 50:
        raise DurableJobTransitionError("general task protected checkpoint capacity reached")
    return protected


def requires_native_writer(run):
    return run.job_kind == GENERAL_TASK_NATIVE_CHILD_KIND or (
        run.job_kind == "agent.task.v1" and read_manifest(run) is not None)


async def verify_native_writer(jobs, db, run):
    """Fixed canonical owner check under the existing journal's SQL writer."""
    if run.job_kind == GENERAL_TASK_NATIVE_CHILD_KIND:
        await assert_general_task_child_current(db, run)
    elif run.job_kind == "agent.task.v1" and read_manifest(run) is not None:
        parent, task, attempt, manifest, _envelope = await _current(jobs, db, run.run_identity)
        _assert_joint_manifest(parent, task, attempt, manifest)
        if parent.status != "running" or manifest.phase not in {"native_ready", "assembly"}:
            from src.workflows.job_runtime import DurableJobLeaseError
            raise DurableJobLeaseError("general task final publication requires its joint assembly lease")


def child_binding(run):
    from src.workflows.job_runtime import DurableJobLeaseError, _digest
    try:
        authority = json.loads(run.declared_authority_json)
        binding = GeneralTaskNativeChildBindingV1.model_validate(authority["general_task_child_binding"])
        if (authority.get("capability_id") != "agent.native-tool-step.v1"
            or run.authority_digest != _digest(authority)
            or run.job_kind != GENERAL_TASK_NATIVE_CHILD_KIND or run.capability_version != "1"
            or (run.branch_depth != 1 and not (run.branch_depth == 3
                and authority.get("specialist_delegation_invocation_id")
                and authority.get("specialist_original_parent_id")))
            or run.owner_kind != "user"
            or run.parent_job_id != binding.parent_job_id
            or run.parent_run_identity != binding.parent_job_id
            or run.parent_fencing_token != binding.creation_job_fence
            or run.run_identity != binding.invocation_id
            or run.owner_principal_id != binding.owner_principal_id
            or run.operator_session_id != binding.original_root_id
            or run.session_id != binding.original_root_id
            or run.goal_id != binding.goal_id or run.goal_revision != binding.goal_revision
            or run.plan_revision != binding.plan_revision or run.max_attempts != 1):
            raise ValueError()
        return binding
    except (KeyError, TypeError, ValueError) as exc:
        raise DurableJobLeaseError("general task native child binding is unavailable") from exc


def append_general_task_root_gate(conditions, run, *, now):
    """Fence adopted native parent phases without changing legacy root jobs."""
    if run.job_kind != "agent.task.v1":
        return
    try:
        manifest = read_manifest(run)
    except ValueError:
        conditions.append(false())
        return
    if manifest is None:
        return
    task, attempt = aliased(WorkBoardTask), aliased(WorkBoardAttempt)
    input_ref = select(WorkBoardInputArtifact.artifact_id).where(
        WorkBoardInputArtifact.artifact_id == task.input_artifact_id,
        WorkBoardInputArtifact.typed_input_ref == task.typed_input_ref,
        WorkBoardInputArtifact.payload_sha256 == task.typed_input_digest,
        WorkBoardInputArtifact.owner_principal_id == task.owner_principal_id,
        WorkBoardInputArtifact.owner_session_id == task.owner_session_id,
        WorkBoardInputArtifact.goal_id == task.goal_id,
        WorkBoardInputArtifact.goal_revision == task.goal_revision,
        WorkBoardInputArtifact.capability_id == "agent.task.v1",
        WorkBoardInputArtifact.capability_version == "1",
        WorkBoardInputArtifact.bound_task_id == task.task_id).exists()
    root = select(OperatorSession.id).where(
        OperatorSession.id == manifest.original_root_id,
        OperatorSession.principal_id == manifest.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now).exists()
    conditions.extend([
        WorkflowRunState.deadline_at == manifest.native_deadline_at,
        WorkflowRunState.deadline_at > now,
        WorkflowRunState.fencing_token == manifest.job_fence,
        WorkflowRunState.input_digest == manifest.original_input_digest,
        select(task.task_id).join(attempt, attempt.task_id == task.task_id).where(
            task.task_id == manifest.task_id, task.task_revision == manifest.task_revision,
            task.owner_session_id == manifest.original_root_id,
            task.owner_principal_id == manifest.owner_principal_id,
            task.input_artifact_id == manifest.original_envelope_artifact_id,
            task.typed_input_digest == manifest.original_envelope_digest,
            attempt.attempt_id == manifest.attempt_id,
            attempt.workflow_run_id == manifest.run_id,
            attempt.fencing_token == manifest.board_fence,
            attempt.ended_at.is_(None), attempt.cancel_requested_at.is_(None), root, input_ref).exists(),
    ])


def append_general_task_parent_gate(conditions, run, *, now):
    """The sole paused-parent exception is an exact declared native wait."""
    if run.job_kind != GENERAL_TASK_NATIVE_CHILD_KIND:
        return False
    try:
        binding = child_binding(run)
        from src.work_board.pipelines import root_binding
        from src.work_board.pipeline_contracts import digest
        if binding.live_root_digest != digest(root_binding()):
            raise ValueError()
    except (ValueError, RuntimeError):
        conditions.append(false())
        return True
    parent, task, attempt = aliased(WorkflowRunState), aliased(WorkBoardTask), aliased(WorkBoardAttempt)
    input_ref = select(WorkBoardInputArtifact.artifact_id).where(
        WorkBoardInputArtifact.artifact_id == task.input_artifact_id,
        WorkBoardInputArtifact.typed_input_ref == task.typed_input_ref,
        WorkBoardInputArtifact.payload_sha256 == task.typed_input_digest,
        WorkBoardInputArtifact.owner_principal_id == task.owner_principal_id,
        WorkBoardInputArtifact.owner_session_id == task.owner_session_id,
        WorkBoardInputArtifact.goal_id == task.goal_id,
        WorkBoardInputArtifact.goal_revision == task.goal_revision,
        WorkBoardInputArtifact.capability_id == "agent.task.v1",
        WorkBoardInputArtifact.capability_version == "1",
        WorkBoardInputArtifact.bound_task_id == task.task_id).exists()
    verified = getattr(run, "_general_task_verified_parent_journal", None)
    if (type(verified) is not _VerifiedParentJournal or verified._seal is not _PHASE_SQL_SEAL
        or verified.child_id != run.run_identity or verified.child_fence != run.fencing_token):
        conditions.append(false())
        return True
    delegated = run.branch_depth == 3
    if delegated:
        authority = json.loads(run.declared_authority_json)
        if (verified.specialist_callback_id != authority.get("specialist_delegation_invocation_id")
            or verified.specialist_parent_id != authority.get("specialist_original_parent_id")
            or not verified.specialist_callback_id or not verified.specialist_parent_id):
            conditions.append(false())
            return True
        callback, original = aliased(WorkflowRunState), aliased(WorkflowRunState)
        conditions.append(select(callback.id).join(original,
            original.run_identity == verified.specialist_parent_id).where(
            callback.run_identity == verified.specialist_callback_id,
            callback.parent_job_id == original.run_identity,
            or_(and_(callback.status == "running",callback.lease_owner.is_not(None),callback.lease_expires_at > now),
                and_(callback.status == "paused",callback.failure_reason == "specialist_wait",
                    callback.lease_owner.is_(None),callback.lease_expires_at.is_(None))) if verified.specialist_callback_waiting
                else and_(callback.status == "running",callback.lease_owner.is_not(None),callback.lease_expires_at > now),
            callback.checkpoint_receipts_json == verified.specialist_callback_checkpoints,
            callback.effect_receipts_json == verified.specialist_callback_effects,
            callback.deadline_at > now,
            callback.fencing_token == verified.specialist_callback_fence,
            original.status == "paused", original.failure_reason == "general_task_native_wait",
            original.checkpoint_receipts_json == verified.specialist_parent_checkpoints).exists())
    checkpoints = func.json_each(parent.checkpoint_receipts_json).table_valued("key", "value").alias()
    payload = lambda field: func.json_extract(checkpoints.c.value, "$.payload." + field)
    invocations = func.json_each(payload("admitted_invocation_ids")).table_valued("key", "value").alias()
    native_invocation = select(invocations.c.key).where(invocations.c.value == binding.invocation_id).exists()
    from src.workflows.job_runtime import _canonical
    transitions = func.json_each(parent.checkpoint_receipts_json).table_valued("key", "value").alias()
    transition = lambda field: func.json_extract(transitions.c.value, "$.payload." + field)
    approval = aliased(ApprovalRequest)
    resumed_phase = select(transitions.c.key).select_from(transitions).join(approval,
        approval.id == transition("approval_id")).where(
        func.json_extract(transitions.c.value, "$.checkpoint_id") == approval_checkpoint_id(binding),
        func.json_extract(transitions.c.value, "$.safe") == 1,
        transition("schema_version") == "general_task.native_approval_transition.v1",
        transition("original_binding") == _canonical(binding.model_dump(mode="json")),
        transition("phase") == "native_wait", transition("positive_attempt_count") == 1,
        transition("current_child_fence") == WorkflowRunState.fencing_token,
        WorkflowRunState.attempt_count == 1,
        transition("current_child_fence") > transition("waiting_child_fence"),
        transition("waiting_child_fence") >= transition("original_claim_fence"),
        transition("phase_revision") == payload("phase_revision"),
        transition("phase_digest") == payload("phase_digest"),
        transition("task_revision") == task.task_revision,
        transition("job_fence") == parent.fencing_token,
        transition("board_fence") == attempt.fencing_token,
        transition("approved_receipt_digest").is_not(None),
        approval.status.in_(("approved", "consumed")), approval.expires_at > now,
        approval.owner_principal_id == binding.owner_principal_id,
        approval.operator_session_id == binding.original_root_id,
        approval.session_id == binding.original_root_id,
        approval.fingerprint == transition("approval_fingerprint"),
        func.json_extract(approval.details_json, "$.approval_context.workflow_run_identity") == run.run_identity,
        func.json_extract(approval.details_json, "$.general_task_wait_binding") ==
            func.json_extract(transitions.c.value, "$.payload"),
    ).exists()
    manifest = select(checkpoints.c.key).where(
        func.json_extract(checkpoints.c.value, "$.checkpoint_id") == GENERAL_TASK_MANIFEST_KEY,
        func.json_extract(checkpoints.c.value, "$.safe") == 1,
        payload("schema_version") == "general_task.current_manifest.v1",
        payload("task_id") == binding.task_id, payload("attempt_id") == binding.attempt_id,
        payload("run_id") == parent.run_identity,
        payload("original_root_id") == binding.original_root_id,
        payload("owner_principal_id") == binding.owner_principal_id,
        payload("original_envelope_digest") == binding.original_envelope_digest,
        payload("original_envelope_artifact_id") == task.input_artifact_id,
        payload("original_input_digest") == parent.input_digest,
        payload("native_deadline_at") == binding.model_dump(mode="json")["native_deadline_at"],
        payload("creation_digest") == binding.creation_digest,
        payload("job_fence") == parent.fencing_token,
        payload("board_fence") == attempt.fencing_token,
        payload("task_revision") == task.task_revision,
        payload("phase") == "native_wait", or_(and_(payload("phase_revision") == binding.phase_revision,
            payload("phase_digest") == binding.phase_digest), resumed_phase),
        payload("plan_revision") == binding.plan_revision,
        payload("current_plan_digest") == binding.plan_digest,
        payload("selected_grant_digest") == binding.selected_grant_digest,
        native_invocation,
    ).exists()
    original_root = select(OperatorSession.id).where(
        OperatorSession.id == binding.original_root_id,
        OperatorSession.principal_id == binding.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now).exists()
    conditions.append(select(parent.id).join(task, task.task_id == binding.task_id)
        .join(attempt, and_(attempt.attempt_id == binding.attempt_id, attempt.task_id == task.task_id))
        .where(parent.run_identity == binding.parent_job_id,
            parent.job_kind == "agent.task.v1", parent.capability_version == "1",
            parent.branch_depth == (2 if delegated else 0),
            parent.parent_job_id == verified.specialist_callback_id if delegated else parent.parent_job_id.is_(None),
            parent.root_run_identity == run.root_run_identity,
            parent.owner_kind == "user", parent.owner_principal_id == binding.owner_principal_id,
            parent.authority_digest == binding.parent_authority_digest,
            parent.declared_authority_json == verified.authority_json,
            parent.checkpoint_receipts_json == verified.checkpoint_json,
            parent.operator_session_id == binding.original_root_id, parent.session_id == binding.original_root_id,
            parent.goal_id == binding.goal_id, parent.goal_revision == binding.goal_revision,
            parent.deadline_at == binding.native_deadline_at,
            parent.deadline_at <= binding.original_deadline_at, parent.deadline_at > now,
            WorkflowRunState.deadline_at <= binding.native_deadline_at,
            parent.status == "paused", parent.failure_reason == "general_task_native_wait",
            parent.lease_owner.is_(None), parent.lease_expires_at.is_(None),
            task.capability_id == "agent.task.v1", task.owner_principal_id == binding.owner_principal_id,
            task.owner_session_id == binding.original_root_id,
            task.goal_id == binding.goal_id, task.goal_revision == binding.goal_revision,
            task.status == WorkBoardStatus.blocked, task.block_reason == "general_task_native_wait",
            attempt.workflow_run_id == parent.run_identity, attempt.ended_at.is_(None),
            attempt.cancel_requested_at.is_(None), attempt.lease_owner.is_(None),
            attempt.lease_expires_at.is_(None), original_root, input_ref, manifest).exists())
    return True


async def assert_general_task_child_phase_current(db, run):
    """Strict canonical check before any native tool contact or adoption."""
    from src.workflows.job_runtime import DurableJobLeaseError, _append_goal_fence_condition, _utc_now
    binding = child_binding(run)
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.parent_job_id))
    if parent is None or read_manifest(parent) is None:
        raise DurableJobLeaseError("general task original manifest is unavailable")
    assert_original_parent_authority(parent)
    effective = await effective_child_phase(db, run, parent)
    specialist = None
    if run.branch_depth == 3:
        from src.workflows.specialist_delegation import assert_specialist_root_current
        specialist = await assert_specialist_root_current(db, parent)
    object.__setattr__(run, "_general_task_verified_parent_journal", _VerifiedParentJournal(
        run.run_identity, run.fencing_token, parent.checkpoint_receipts_json,
        parent.declared_authority_json, _PHASE_SQL_SEAL,
        specialist_callback_id=specialist.callback.run_identity if specialist else None,
        specialist_callback_fence=specialist.callback.fencing_token if specialist else None,
        specialist_parent_id=specialist.parent.run_identity if specialist else None,
        specialist_parent_checkpoints=specialist.parent.checkpoint_receipts_json if specialist else None,
        specialist_callback_checkpoints=specialist.callback.checkpoint_receipts_json if specialist else None,
        specialist_callback_effects=specialist.callback.effect_receipts_json if specialist else None,
        specialist_callback_waiting=bool(specialist and specialist.callback.status == "paused")))
    conditions = [WorkflowRunState.run_identity == run.run_identity]
    _append_goal_fence_condition(conditions, run)
    append_general_task_parent_gate(conditions, run, now=_utc_now())
    if await db.scalar(select(WorkflowRunState.id).where(*conditions)) is None:
        raise DurableJobLeaseError("general task original native phase is unavailable")
    return effective


def _step_receipt(manifest, step_id):
    from src.work_board.contracts import GeneralTaskArtifactRef
    from src.work_board.general_task_runtime_artifacts import read_native_artifact_reference
    from src.workflows.job_runtime import DurableJobLeaseError
    try:
        index = manifest.step_ids.index(step_id)
    except ValueError as exc:
        raise DurableJobLeaseError("positive original native claim receipt is required before contact") from exc
    return read_native_artifact_reference(GeneralTaskArtifactRef(
        artifact_id=manifest.step_receipt_artifact_ids[index], digest=manifest.step_receipt_digests[index],
        schema_version="StepReceipt.v1"), parent_job_id=manifest.run_id, creation_digest=manifest.creation_digest)


async def assert_general_task_child_current(db, run):
    """Contact requires the durable original positive claim, never admission0."""
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _utc_now
    effective = await assert_general_task_child_phase_current(db, run)
    binding = child_binding(run)
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.parent_job_id))
    _require_callback_reservation(parent, binding, run.fencing_token)
    receipt = _step_receipt(read_manifest(parent), binding.step_id)
    if (run.status != "running" or not run.lease_owner or _as_utc(run.lease_expires_at) is None
        or _as_utc(run.lease_expires_at) <= _utc_now() or _as_utc(run.deadline_at) <= _utc_now()
        or run.attempt_count != 1 or run.fencing_token <= 0 or receipt.child_attempt_count != run.attempt_count
        or receipt.child_fence != run.fencing_token or receipt.child_job_id != run.run_identity
        or receipt.invocation_id != binding.invocation_id or receipt.input_digest != binding.input_digest
        or receipt.descriptor_digest != binding.descriptor_digest or receipt.phase_digest != effective.phase_digest
        or receipt.approval_binding_digest != effective.approval_binding_digest
        or receipt.status != "running" or receipt.contact_state not in {"not_contacted", "contact_started"}):
        raise DurableJobLeaseError("general task original positive child claim changed")


async def assert_general_task_child_terminal_current(jobs, db, run):
    """Adoption consumes verified readback; it never grants renewed contact."""
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _utc_now, _digest, _verified_readback_exists, _job_has_unsafe_effects
    from src.work_board.general_task import digest, validate_schema
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.work_board.general_task_native import current_plan
    from src.artifacts.registry import artifact_id_for
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    effective = await assert_general_task_child_phase_current(db, run)
    binding = child_binding(run)
    _parent, _task, _attempt, manifest, envelope = await _current(jobs, db, binding.parent_job_id)
    receipt = _step_receipt(manifest, binding.step_id)
    effects = json.loads(run.effect_receipts_json or "[]")
    from src.workflows.specialist_lifecycle import read_fact,CLOSURE_KEY,SpecialistDelegationClosureV1
    delegation_closed = read_fact(run,CLOSURE_KEY,SpecialistDelegationClosureV1) is not None
    if delegation_closed:
        from src.workflows.specialist_result import verify_full_delegation_result
        await verify_full_delegation_result(db,_parent,run,receipt)
    lease_invalid = (run.status != "succeeded" or run.lease_owner is not None or run.lease_expires_at is not None) if delegation_closed else (
        run.status != "running" or not run.lease_owner
        or _as_utc(run.lease_expires_at) is None or _as_utc(run.lease_expires_at) <= _utc_now())
    if (lease_invalid
        or run.attempt_count != 1 or run.fencing_token <= 0
        or receipt.child_attempt_count != run.attempt_count or receipt.child_fence != run.fencing_token
        or receipt.child_job_id != run.run_identity or receipt.invocation_id != binding.invocation_id
        or receipt.input_digest != binding.input_digest or receipt.descriptor_digest != binding.descriptor_digest
        or receipt.selected_grant_digest != binding.selected_grant_digest
        or receipt.parent_creation_digest != binding.creation_digest or receipt.phase_digest != effective.phase_digest
        or receipt.approval_binding_digest != effective.approval_binding_digest
        or receipt.status != "verified" or receipt.contact_state != "settled"
        or receipt.effect_receipt_digest != _digest(effects)
        or not _verified_readback_exists(effects) or _job_has_unsafe_effects(effects)):
        raise DurableJobLeaseError("native terminal adoption requires original verified settled readback")
    refs = [ref for ref in receipt.artifact_refs if ref.schema_version == "GeneralTaskOutput.v1"]
    if len(refs) != 1 or len(receipt.artifact_refs) != 1:
        raise DurableJobLeaseError("native terminal adoption requires one exact output artifact")
    ref = refs[0]
    path = f"artifacts/work-board/general-tasks/{digest([run.run_identity, binding.plan_digest, binding.step_id])}-{ref.digest}.json"
    records = [item for item in json.loads(run.artifact_receipts_json or "[]")
        if item.get("artifact_id") == ref.artifact_id and item.get("content_sha256") == ref.digest]
    if (len(records) != 1 or records[0].get("file_path") != path
        or type(records[0].get("size_bytes")) is not int or not 0 < records[0]["size_bytes"] <= 65536
        or ref.artifact_id != artifact_id_for(file_path=path, artifact_type="general_task_step",
            producer=run.job_kind, run_id=run.run_identity, content_sha256=ref.digest)
        or not any(item.get("effect_type") == "general_tool_call" and item.get("status") == "succeeded"
            and item.get("content_sha256") == ref.digest and item.get("details", {}).get("verified") is True
            and item.get("details", {}).get("step_id") == binding.step_id for item in effects)):
        raise DurableJobLeaseError("native terminal output identity or readback changed")
    body = json.loads(_safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / path,
        expected_digest=ref.digest, expected_size=records[0]["size_bytes"]))
    step = next((item for item in current_plan(manifest, envelope).steps if item.step_id == binding.step_id), None)
    tool_id = json.loads(run.arguments_json)["tool_id"]
    descriptor = next((item for item in envelope.descriptors if item.tool_id == tool_id), None)
    if (set(body) != {"step_id", "output"} or body["step_id"] != binding.step_id or step is None
        or descriptor is None or digest(descriptor.model_dump(mode="json")) != binding.descriptor_digest):
        raise DurableJobLeaseError("native terminal descriptor or output contract changed")
    validate_schema(descriptor.output_schema, body["output"])
    validate_schema(step.output_contract, body["output"])
    assert_child_closed(_parent, run, receipt)
    if delegation_closed:
        from src.workflows.specialist_result import read_full_delegation_closure
        closure = read_full_delegation_closure(_parent,run,binding)
    else:
        closure = _protected_payload(_parent, cleanup_checkpoint_id(binding, run.fencing_token), GeneralTaskToolClosureV1)
    if closure.outcome != ("durable_result_verified" if delegation_closed else "returned") or closure.output_digest != digest(body["output"]):
        raise DurableJobLeaseError("native terminal output must match original callback return")


async def _current(jobs, db, parent_id, *, manifest=None):
    from src.workflows.job_runtime import (
        DurableJobLeaseError, _as_utc, _assert_canonical_goal_fence, _utc_now,
    )
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
    now = _utc_now()
    parent = await jobs._fetch(db, parent_id)
    assert_original_parent_authority(parent)
    selected = manifest or read_manifest(parent)
    if selected is None:
        raise DurableJobLeaseError("general task original manifest is required")
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == selected.task_id))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == selected.attempt_id))
    from src.workflows.specialist_delegation import is_specialist_root, assert_specialist_root_current
    specialist = is_specialist_root(parent)
    if specialist:
        await assert_specialist_root_current(db, parent)
    if (parent.job_kind != "agent.task.v1" or parent.capability_version != "1"
        or parent.owner_kind != "user" or (not specialist and (parent.branch_depth != 0 or parent.parent_job_id))
        or task is None or attempt is None or attempt.ended_at or attempt.cancel_requested_at
        or attempt.task_id != task.task_id or attempt.workflow_run_id != parent_id
        or parent.session_id != parent.operator_session_id
        or parent.owner_principal_id != task.owner_principal_id
        or parent.session_id != task.owner_session_id
        or _as_utc(parent.deadline_at) != selected.native_deadline_at
        or selected.native_deadline_at > selected.original_deadline_at
        or _as_utc(parent.deadline_at) <= now):
        raise DurableJobLeaseError("general task original parent binding is unavailable")
    active_root = await db.scalar(select(OperatorSession.id).where(
        OperatorSession.id == parent.operator_session_id,
        OperatorSession.principal_id == parent.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now))
    if active_root is None:
        raise DurableJobLeaseError("general task original Root is inactive")
    await _assert_canonical_goal_fence(db, goal_id=parent.goal_id, goal_revision=parent.goal_revision,
        owner_kind=parent.owner_kind, owner_principal_id=parent.owner_principal_id,
        session_id=parent.session_id, authority=parent.declared_authority_json)
    envelope = await verify_general_task_manifest(db, parent, task, attempt, selected)
    if envelope.strategy.status == "active":
        from src.memory.task_methods import current_method
        from src.work_board.contracts import WorkBoardOwner
        await current_method.validate_pinned(WorkBoardOwner(principal_id=task.owner_principal_id,
            session_id=task.owner_session_id), envelope.task_input.goal_ref, envelope.strategy, db=db)
    return parent, task, attempt, selected, envelope


async def _repository_current_pure(jobs, db, parent_id, *, manifest=None,
    source_binding=None):
    """Load the repository transition rows without physical/source reads.

    The repository producer stages and validates the physical source snapshot
    before entering these writers.  This loader retains the ordinary SQL,
    authority, Goal/Root, deadline, and manifest shape fences while avoiding
    ``verify_general_task_manifest`` (which can read private artifacts).
    """
    from src.workflows.job_runtime import (
        DurableJobLeaseError, _as_utc, _assert_canonical_goal_fence, _digest, _utc_now,
    )
    envelope = None
    if source_binding is not None:
        try:
            from src.workflows.repo_repair_source import (
                assert_repository_canonical_source,
            )
            from src.work_board.contracts import GeneralTaskEnvelope
            assert_repository_canonical_source(source_binding)
            envelope = GeneralTaskEnvelope.model_validate(
                json.loads(source_binding.parent_envelope_json))
        except Exception as exc:
            raise DurableJobLeaseError(
                "repository canonical parent envelope snapshot is invalid") from exc
    now = _utc_now()
    parent = await jobs._fetch(db, parent_id)
    assert_original_parent_authority(parent)
    selected = manifest or read_manifest(parent)
    if selected is None:
        raise DurableJobLeaseError("general task original manifest is required")
    task = await db.scalar(select(WorkBoardTask).where(
        WorkBoardTask.task_id == selected.task_id))
    attempt = await db.scalar(select(WorkBoardAttempt).where(
        WorkBoardAttempt.attempt_id == selected.attempt_id))
    if (parent.job_kind != "agent.task.v1" or parent.capability_version != "1"
        or parent.owner_kind != "user" or parent.branch_depth != 0 or parent.parent_job_id
        or task is None or attempt is None or attempt.ended_at or attempt.cancel_requested_at
        or attempt.task_id != task.task_id or attempt.workflow_run_id != parent_id
        or parent.session_id != parent.operator_session_id
        or parent.owner_principal_id != task.owner_principal_id
        or parent.session_id != task.owner_session_id
        or _as_utc(parent.deadline_at) != selected.native_deadline_at
        or selected.native_deadline_at > selected.original_deadline_at
        or _as_utc(parent.deadline_at) <= now):
        raise DurableJobLeaseError("general task original parent binding is unavailable")
    if (envelope is not None
        and _digest({"schema_version": 1, "capability_id": "agent.task.v1",
            "input": envelope.model_dump(mode="json", exclude_none=True)}) != selected.original_envelope_digest):
        raise DurableJobLeaseError("repository canonical parent envelope changed")
    active_root = await db.scalar(select(OperatorSession.id).where(
        OperatorSession.id == parent.operator_session_id,
        OperatorSession.principal_id == parent.owner_principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False),
        OperatorSession.idle_expires_at > now, OperatorSession.absolute_expires_at > now))
    if active_root is None:
        raise DurableJobLeaseError("general task original Root is inactive")
    await _assert_canonical_goal_fence(db, goal_id=parent.goal_id,
        goal_revision=parent.goal_revision, owner_kind=parent.owner_kind,
        owner_principal_id=parent.owner_principal_id,
        session_id=parent.session_id, authority=parent.declared_authority_json)
    return parent, task, attempt, selected, envelope


def _history(run):
    from src.workflows.job_runtime import DurableJobTransitionError
    try:
        history = json.loads(run.checkpoint_receipts_json or "[]")
        if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
            raise ValueError()
        return history
    except (TypeError, ValueError) as exc:
        raise DurableJobTransitionError("general task checkpoint history is malformed") from exc


def _next_manifest(parent, previous, proposed, *, task, attempt):
    from src.workflows.job_runtime import DurableJobLeaseError
    if type(proposed) is not GeneralTaskCurrentManifestV1:
        raise DurableJobLeaseError("closed native manifest required")
    # Internal model_copy does not run Pydantic bounds or closed validators.
    # Revalidate before any writer can publish a successor.
    GeneralTaskCurrentManifestV1.model_validate(proposed.model_dump(mode="json"))
    if proposed.task_revision != task.task_revision or proposed.board_fence != attempt.fencing_token or proposed.job_fence != parent.fencing_token:
        raise DurableJobLeaseError("general task manifest counters changed")
    if previous is None:
        if (proposed.manifest_revision != 1 or proposed.plan_revision != 1
            or proposed.phase_revision != 1 or proposed.phase != "native_ready"
            or proposed.admitted_invocation_ids or proposed.step_ids):
            raise DurableJobLeaseError("general task initial manifest is not a fresh native binding")
        return
    immutable = ("task_id", "original_root_id", "owner_principal_id", "attempt_id", "run_id",
        "original_envelope_artifact_id", "original_envelope_digest", "original_input_digest",
        "selected_grant_digest", "group_id", "group_digest", "original_limits_digest",
        "creation_digest", "original_deadline_at", "native_deadline_at")
    if (proposed.manifest_revision != previous.manifest_revision + 1
        or any(getattr(proposed, name) != getattr(previous, name) for name in immutable)
        or proposed.plan_revision not in {previous.plan_revision, previous.plan_revision + 1}
        or proposed.admitted_invocation_ids[:len(previous.admitted_invocation_ids)] != previous.admitted_invocation_ids):
        raise DurableJobLeaseError("general task immutable manifest binding changed")
    for field in ("revision_numbers", "revision_artifact_ids", "revision_artifact_digests", "revision_artifact_schemas"):
        if getattr(proposed, field)[:len(getattr(previous, field))] != getattr(previous, field):
            raise DurableJobLeaseError("general task immutable revision history changed")
    if not set(previous.step_ids).issubset(proposed.step_ids):
        raise DurableJobLeaseError("general task admitted step evidence cannot disappear")
    phase_fields = ("phase", "plan_revision", "current_plan_digest", "admitted_invocation_ids", "job_fence", "board_fence")
    phase_changed = any(getattr(previous, name) != getattr(proposed, name) for name in phase_fields)
    if proposed.phase_revision != previous.phase_revision + int(phase_changed):
        raise DurableJobLeaseError("general task native phase revision changed")


def _publish(parent, manifest, *, staged_records=()):
    from src.workflows.job_runtime import _canonical, _digest, _github_recovery_history, _utc_now
    from src.work_board.general_task_runtime_artifacts import verify_staged_task_artifact
    # The fixed writer derives protection; no proposed list can grant retention
    # or silently drop an already committed original proof.
    history = _history(parent)
    required = sorted({item["checkpoint_id"] for item in history
        if isinstance(item.get("checkpoint_id"), str)
        and item["checkpoint_id"].startswith(("general:", _REPOSITORY_CHECKPOINT_PREFIX))})
    manifest = GeneralTaskCurrentManifestV1.model_validate(
        manifest.model_dump(mode="json") | {"required_checkpoint_ids": required})
    artifacts = json.loads(parent.artifact_receipts_json or "[]")
    for staged in staged_records:
        _payload, record = verify_staged_task_artifact(staged,
            parent_job_id=parent.run_identity, creation_digest=manifest.creation_digest)
        artifacts = [item for item in artifacts if item.get("artifact_id") != record["artifact_id"]]
        artifacts.append({**record, "recorded_at": _utc_now().isoformat()})
    payload = manifest.model_dump(mode="json")
    receipt = {"checkpoint_id": GENERAL_TASK_MANIFEST_KEY, "state_digest": _digest(payload),
        "state_keys": sorted(payload), "safe": True, "payload": payload,
        "fencing_token": manifest.job_fence, "recorded_at": _utc_now().isoformat()}
    history = [item for item in history if item.get("checkpoint_id") != GENERAL_TASK_MANIFEST_KEY] + [receipt]
    parent.checkpoint_receipts_json = _canonical(_github_recovery_history(parent, history, kind="checkpoint"))
    parent.artifact_receipts_json = _canonical(_github_recovery_history(parent, artifacts, kind="artifact"))
    return manifest


def _validate_staged_refs(previous, manifest, staged):
    from src.work_board.general_task_runtime_artifacts import verify_staged_task_artifact
    from src.workflows.job_runtime import DurableJobLeaseError
    def refs(value):
        if value is None:
            return set()
        return set(zip(value.revision_artifact_ids[1:], value.revision_artifact_digests[1:], value.revision_artifact_schemas[1:])) | set(zip(
            value.step_receipt_artifact_ids, value.step_receipt_digests, value.step_receipt_schemas))
    added = refs(manifest) - refs(previous)
    supplied = set()
    for item in staged:
        verify_staged_task_artifact(item, parent_job_id=manifest.run_id, creation_digest=manifest.creation_digest)
        supplied.add((item.reference.artifact_id, item.reference.digest, item.reference.schema_version))
    if added != supplied:
        raise DurableJobLeaseError("new native references require exact sealed staged artifacts")


def _published_values(parent, manifest, staged):
    from types import SimpleNamespace
    staged_parent = SimpleNamespace(run_identity=parent.run_identity, job_kind=parent.job_kind,
        checkpoint_receipts_json=parent.checkpoint_receipts_json,
        artifact_receipts_json=parent.artifact_receipts_json)
    manifest = _publish(staged_parent, manifest, staged_records=staged)
    return manifest, {"checkpoint_receipts_json": staged_parent.checkpoint_receipts_json,
        "artifact_receipts_json": staged_parent.artifact_receipts_json}


def _published_proofs(parent, manifest, staged, proofs):
    from types import SimpleNamespace
    from src.workflows.job_runtime import _canonical, _digest, _utc_now
    history = _history(parent)
    for identity, model in proofs:
        payload = model.model_dump(mode="json")
        record = {"checkpoint_id": identity, "safe": True, "payload": payload,
            "state_digest": _digest(payload), "state_keys": sorted(payload),
            "fencing_token": manifest.job_fence, "recorded_at": _utc_now().isoformat()}
        history = [item for item in history if item.get("checkpoint_id") != identity] + [record]
    temporary = SimpleNamespace(run_identity=parent.run_identity, job_kind=parent.job_kind,
        checkpoint_receipts_json=_canonical(history), artifact_receipts_json=parent.artifact_receipts_json)
    return _published_values(temporary, manifest, staged)


async def publish_tool_closure(jobs, child_id, *, owner, fencing_token,
    expected_parent_revision, producer_witness):
    """Only the actual original callback owner can publish closure evidence."""
    from src.native_tools.task_adapters import verify_task_tool_closure
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.job_runtime import DurableJobLeaseError, _serialize
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        child = await jobs._fetch(db, child_id)
        jobs._assert_lease(child, owner=owner, fencing_token=fencing_token)
        await assert_general_task_child_current(db, child)
        binding = child_binding(child)
        parent, task, attempt, previous, _ = await _current(jobs, db, binding.parent_job_id)
        _assert_joint_manifest(parent, task, attempt, previous)
        if parent.revision != expected_parent_revision:
            raise DurableJobLeaseError("native callback closure parent revision changed")
        closure = verify_task_tool_closure(producer_witness, binding=binding, fencing_token=fencing_token)
        identity = cleanup_checkpoint_id(binding, fencing_token)
        records = [item for item in _history(parent) if item.get("checkpoint_id") == identity]
        if len(records) == 1 and records[0].get("payload", {}).get("schema_version") == "general_task.tool_closure.v1":
            if _protected_payload(parent, identity, GeneralTaskToolClosureV1) != closure:
                raise DurableJobLeaseError("original native callback closure is immutable")
            return {"job": _serialize(parent), "closure": closure.model_dump(mode="json")}
        _require_callback_reservation(parent, binding, fencing_token)
        proposed = previous.model_copy(update={"manifest_revision": previous.manifest_revision + 1})
        published, values = _published_proofs(parent, proposed, (), ((identity, closure),))
        await _cas_parent(db, parent, values)
        return {"job": _serialize(await jobs._fetch(db, parent.run_identity)),
            "manifest": published.model_dump(mode="json"), "closure": closure.model_dump(mode="json")}


def assert_child_closed(parent, child, receipt):
    from src.workflows.job_runtime import DurableJobLeaseError, _digest
    binding = child_binding(child)
    if json.loads(child.arguments_json).get("tool_id") == "repository_work":
        # This proof is usable only after the source-specific final CAS has
        # closed the child. A contacted wait or provider return alone cannot
        # qualify for ordinary parent assembly or cancellation cleanup.
        finals = [item for item in _history(parent)
            if item.get("checkpoint_id", "").startswith(_REPOSITORY_CHILD_FINAL_PREFIX)
            and item.get("payload", {}).get("witness", {}).get("wait_witness", {}).get("native_binding")
                == binding.model_dump(mode="json")]
        if len(finals) != 1:
            raise DurableJobLeaseError("repository child requires its one original final closure")
        record = finals[0]
        payload, witness = record["payload"], record["payload"]["witness"]
        wait = witness["wait_witness"]
        wait_id = repository_child_wait_checkpoint_id(binding, wait["iteration_id"])
        wait_records = [item for item in _history(parent) if item.get("checkpoint_id") == wait_id]
        artifacts = json.loads(child.artifact_receipts_json)
        if (record.get("safe") is not True or record.get("state_digest") != _digest(payload)
                or payload.get("schema_version") != "repository.child_final_witness.v1"
                or payload.get("phase") != "final_verified" or payload.get("no_learning") is not True
                or payload.get("witness_digest") != _digest(witness)
                or record["checkpoint_id"] != repository_child_final_checkpoint_id(binding, wait["iteration_id"])
                or len(wait_records) != 1 or wait_records[0].get("safe") is not True
                or wait_records[0].get("state_digest") != _digest(wait_records[0].get("payload"))
                or wait_records[0]["payload"].get("witness") != wait
                or child.status != "succeeded" or child.lease_owner or child.lease_expires_at
                or child.result_digest != witness["final_artifact_digest"]
                or receipt.status != "verified" or receipt.contact_state != "settled"
                or receipt.child_job_id != child.run_identity or receipt.child_fence != child.fencing_token
                or receipt.cleanup_receipt_digest != witness["final_cleanup_digest"]
                or not any(ref.artifact_id == witness["final_artifact_id"]
                    and ref.digest == witness["final_artifact_digest"] for ref in receipt.artifact_refs)
                or not any(item.get("artifact_id") == witness["final_artifact_id"]
                    and item.get("content_sha256") == witness["final_artifact_digest"] for item in artifacts)):
            raise DurableJobLeaseError("repository original final callback/process closure changed")
        return
    records = [item for item in _history(parent) if item.get("checkpoint_id")==cleanup_checkpoint_id(binding,child.fencing_token)]
    if len(records)==1 and records[0].get("payload",{}).get("schema_version")=="SpecialistDelegationClosure.v1":
        from src.workflows.specialist_result import read_full_delegation_closure
        closure = read_full_delegation_closure(parent,child,binding)
        if (receipt.child_job_id!=child.run_identity or receipt.child_fence!=child.fencing_token
            or receipt.cleanup_receipt_digest!=_digest(closure.model_dump(mode="json"))):
            raise DurableJobLeaseError("native delegation requires original actual result closure")
        return
    closure = _protected_payload(parent, cleanup_checkpoint_id(binding, child.fencing_token), GeneralTaskToolClosureV1)
    if (closure.invocation_id != child.run_identity or closure.child_fence != child.fencing_token
        or closure.original_binding_digest != _digest(binding.model_dump(mode="json"))
        or closure.input_digest != binding.input_digest or closure.descriptor_digest != binding.descriptor_digest
        or closure.outcome not in {"returned", "approval_precontact"}
        or receipt.child_job_id != child.run_identity or receipt.child_fence != child.fencing_token
        or receipt.cleanup_receipt_digest != _digest(closure.model_dump(mode="json"))):
        raise DurableJobLeaseError("native child requires canonical original callback closure")


async def _validate_repository_source_witness(db, witness, *, phase, parent,
    task, attempt, child, manifest, staged_artifact=None, receipt=None):
    """Invoke the source owner's SQL-only witness recheck inside this writer."""
    from src.workflows.job_runtime import DurableJobLeaseError, _digest
    binding = child_binding(child)
    if phase == "wait":
        _assert_repository_child_wait_witness_shape(witness)
        source_validator_name = "validate_repository_child_wait_witness"
        base_witness = witness
        witness_binding = witness.native_binding
    elif phase == "final":
        _assert_repository_child_final_witness_shape(witness)
        source_validator_name = "validate_repository_child_final_witness"
        base_witness = witness.wait_witness
        witness_binding = witness.wait_witness.native_binding
    else:
        raise DurableJobLeaseError("unknown repository witness phase")
    if (witness_binding != binding
        or base_witness.repository_job_id == child.run_identity
        or base_witness.repository_fence < 1
        or base_witness.iteration_id == ""):
        raise DurableJobLeaseError("repository witness original child binding changed")
    try:
        from src.workflows import repo_repair_source
        validator = getattr(repo_repair_source, source_validator_name, None)
    except Exception as exc:
        raise DurableJobLeaseError("repository source witness owner unavailable") from exc
    if not callable(validator):
        raise DurableJobLeaseError("repository source witness validator unavailable")
    result = validator(db, witness, parent=parent, task=task, attempt=attempt,
        child=child, manifest=manifest, staged_artifact=staged_artifact,
        receipt=receipt, phase=phase)
    if hasattr(result, "__await__"):
        result = await result
    if result is False or result is None:
        raise DurableJobLeaseError("repository source witness canonical recheck failed")
    return result


def _publish_repository_checkpoint(parent, manifest, *, checkpoint_id, payload,
    staged_artifacts=()):
    """Stage one source-owned repository proof through the native manifest writer."""
    from types import SimpleNamespace
    from src.workflows.job_runtime import DurableJobTransitionError, _canonical, _digest, _utc_now
    history = _history(parent)
    existing = [item for item in history if item.get("checkpoint_id") == checkpoint_id]
    if existing:
        if len(existing) != 1 or existing[0].get("safe") is not True:
            raise DurableJobTransitionError("repository protected checkpoint changed")
        if existing[0].get("state_digest") != _digest(existing[0].get("payload")):
            raise DurableJobTransitionError("repository protected checkpoint digest changed")
    record = {"checkpoint_id": checkpoint_id, "safe": True, "payload": payload,
        "state_digest": _digest(payload), "state_keys": sorted(payload),
        "fencing_token": manifest.job_fence, "recorded_at": _utc_now().isoformat()}
    history = [item for item in history if item.get("checkpoint_id") != checkpoint_id] + [record]
    _check_reserved_capacity(history)
    staged_parent = SimpleNamespace(run_identity=parent.run_identity, job_kind=parent.job_kind,
        checkpoint_receipts_json=_canonical(history), artifact_receipts_json=parent.artifact_receipts_json)
    return _published_values(staged_parent, manifest, staged_artifacts)


async def publish_repository_child_wait(jobs, child_id, *, owner, fencing_token,
    expected_parent_revision, producer_witness):
    """Persist a source-issued contacted wait without ordinary callback closure."""
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.job_runtime import DurableJobLeaseError, _serialize, _utc_now, _as_utc
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        child = await jobs._fetch(db, child_id)
        binding = child_binding(child)
        if child.status == "running":
            jobs._assert_lease(child, owner=owner, fencing_token=fencing_token)
            await assert_general_task_child_current(db, child)
        elif (child.status != "paused" or child.failure_reason != "repository_child_wait"
            or child.attempt_count != 1 or child.lease_owner != owner
            or child.fencing_token != fencing_token or child.lease_expires_at is None):
            raise DurableJobLeaseError("repository child wait requires the same running or paused child")
        else:
            # A repository wait retains the original owner and lease expiry;
            # replay must prove that exact unexpired lease rather than taking
            # an ownerless paused shortcut.
            jobs._assert_lease(child, owner=owner, fencing_token=fencing_token)
        _assert_repository_child_wait_witness_shape(producer_witness)
        parent, task, attempt, previous, _envelope = await _repository_current_pure(
            jobs, db, binding.parent_job_id,
            source_binding=producer_witness._source_binding)
        _assert_joint_manifest(parent, task, attempt, previous)
        if parent.revision != expected_parent_revision:
            raise DurableJobLeaseError("repository child wait parent revision changed")
        if (parent.status != "paused" or parent.failure_reason != "general_task_native_wait"
            or previous.phase != "native_wait"
            or task.status != WorkBoardStatus.blocked
            or task.block_reason != "general_task_native_wait"
            or attempt.ended_at is not None or attempt.cancel_requested_at is not None):
            raise DurableJobLeaseError("repository child wait requires the original native wait")
        await _validate_repository_source_witness(db, producer_witness, phase="wait",
            parent=parent, task=task, attempt=attempt, child=child, manifest=previous)
        slot = read_native_checkpoint_reservation(parent, cleanup_checkpoint_id(binding, child.fencing_token))
        if (slot.invocation_id != child.run_identity or slot.callback_fence != child.fencing_token):
            raise DurableJobLeaseError("repository child wait capacity reservation changed")
        checkpoint_id = repository_child_wait_checkpoint_id(binding, producer_witness.iteration_id)
        payload = _repository_checkpoint_payload(producer_witness, phase="contacted_wait",
            checkpoint_id=checkpoint_id)
        history = _history(parent)
        existing = [item for item in history if item.get("checkpoint_id") == checkpoint_id]
        if existing:
            if (len(existing) != 1 or existing[0].get("state_digest") != _repository_digest(existing[0].get("payload"))
                or existing[0].get("payload") != payload):
                raise DurableJobLeaseError("repository child wait is immutable")
            if child.status != "paused":
                raise DurableJobLeaseError("repository child wait requires paused child recovery")
            return {"child": _serialize(child), "job": _serialize(parent),
                "manifest": previous.model_dump(mode="json"), "wait": payload,
                "idempotent_replay": True}
        proposed = previous.model_copy(update={"manifest_revision": previous.manifest_revision + 1})
        _next_manifest(parent, previous, proposed, task=task, attempt=attempt)
        published, values = _publish_repository_checkpoint(parent, proposed,
            checkpoint_id=checkpoint_id, payload=payload)
        child_revision = child.revision
        now = _utc_now()
        child_update = await db.execute(update(WorkflowRunState).where(
            WorkflowRunState.run_identity == child.run_identity,
            WorkflowRunState.revision == child_revision,
            WorkflowRunState.status == "running",
            WorkflowRunState.fencing_token == child.fencing_token,
            WorkflowRunState.lease_owner == owner,
            WorkflowRunState.lease_expires_at > now,
        ).values(status="paused", failure_reason="repository_child_wait",
            lease_owner=owner, lease_expires_at=child.lease_expires_at, updated_at=now,
            heartbeat_at=now, finished_at=None,
            revision=WorkflowRunState.revision + 1).execution_options(synchronize_session=False))
        if child_update.rowcount != 1:
            raise DurableJobLeaseError("repository child wait child CAS changed")
        await _cas_parent(db, parent, values)
        return {"child": _serialize(await jobs._fetch(db, child_id)),
            "job": _serialize(await jobs._fetch(db, parent.run_identity)),
            "manifest": published.model_dump(mode="json"), "wait": payload}


async def resume_repository_child_wait(jobs, child_id, *, owner,
    expected_parent_revision, expected_child_revision, producer_witness):
    """Wake the exact paused repository child without claim/fence renewal."""
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.job_runtime import DurableJobLeaseError, _serialize, _utc_now, _as_utc
    if not owner:
        raise DurableJobLeaseError("repository child wake owner is required")
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        child = await jobs._fetch(db, child_id)
        binding = child_binding(child)
        if (child.status != "paused" or child.failure_reason != "repository_child_wait"
            or child.attempt_count != 1 or child.lease_owner != owner
            or child.lease_expires_at is None or child.revision != expected_child_revision):
            raise DurableJobLeaseError("repository child wake requires the exact paused child")
        _assert_repository_child_wait_witness_shape(producer_witness)
        parent, task, attempt, previous, _envelope = await _repository_current_pure(
            jobs, db, binding.parent_job_id,
            source_binding=producer_witness._source_binding)
        _assert_joint_manifest(parent, task, attempt, previous)
        if (parent.revision != expected_parent_revision or parent.status != "paused"
            or parent.failure_reason != "general_task_native_wait"
            or previous.phase != "native_wait" or task.status != WorkBoardStatus.blocked
            or task.block_reason != "general_task_native_wait"):
            raise DurableJobLeaseError("repository child wake parent binding changed")
        await _validate_repository_source_witness(db, producer_witness, phase="wait",
            parent=parent, task=task, attempt=attempt, child=child, manifest=previous)
        checkpoint_id = repository_child_wait_checkpoint_id(binding, producer_witness.iteration_id)
        record = next((item for item in _history(parent) if item.get("checkpoint_id") == checkpoint_id), None)
        payload = _repository_checkpoint_payload(producer_witness, phase="contacted_wait",
            checkpoint_id=checkpoint_id)
        if (record is None or record.get("safe") is not True
            or record.get("payload") != payload
            or record.get("state_digest") != _repository_digest(payload)):
            raise DurableJobLeaseError("repository child wake source checkpoint changed")
        now = _utc_now()
        deadline = min(_as_utc(child.deadline_at), _as_utc(parent.deadline_at), producer_witness.native_binding.original_deadline_at)
        expiry = _as_utc(child.lease_expires_at)
        if (deadline is None or deadline <= now or expiry is None or expiry <= now
            or expiry > deadline):
            raise DurableJobLeaseError("repository child wake original cutoff expired")
        changed = await db.execute(update(WorkflowRunState).where(
            WorkflowRunState.run_identity == child.run_identity,
            WorkflowRunState.revision == child.revision,
            WorkflowRunState.status == "paused",
            WorkflowRunState.failure_reason == "repository_child_wait",
            WorkflowRunState.fencing_token == child.fencing_token,
            WorkflowRunState.lease_owner == owner,
            WorkflowRunState.lease_expires_at > now,
        ).values(status="running", failure_reason=None, lease_owner=owner,
            lease_expires_at=child.lease_expires_at, updated_at=now, heartbeat_at=now,
            finished_at=None, revision=WorkflowRunState.revision + 1
        ).execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise DurableJobLeaseError("repository child wake CAS changed")
        return {"child": _serialize(await jobs._fetch(db, child_id)),
            "job": _serialize(parent), "manifest": previous.model_dump(mode="json"),
            "wait": payload, "same_owner": True, "same_fence": True,
            "same_attempt": True, "lease_expires_at": child.lease_expires_at.isoformat()}


async def publish_repository_child_final(jobs, parent_id, *, staged_artifact,
    child_id, owner, fencing_token, expected_parent_revision,
    repository_final_witness, repository_final_authority_check=None,
    repository_final_authority_scope=None):
    """Adopt one source-verified cumulative repair and close the same child.

    The source witness owns physical/process/accounting evidence.  This
    writer only performs canonical SQL checks and the parent/child CAS; it
    never reads a repository, invokes a model, or manufactures callback
    closure.
    """
    from contextlib import AsyncExitStack
    from src.work_board.repository import _begin_sqlite_immediate
    from src.work_board.general_task_runtime_artifacts import verify_staged_task_artifact
    from src.work_board.contracts import GeneralTaskStepReceiptV1
    from src.workflows.job_runtime import DurableJobLeaseError, _digest, _serialize, _utc_now
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        child = await jobs._fetch(db, child_id)
        jobs._assert_lease(child, owner=owner, fencing_token=fencing_token)
        binding = child_binding(child)
        _assert_repository_child_final_witness_shape(repository_final_witness)
        parent, task, attempt, previous, _envelope = await _repository_current_pure(
            jobs, db, parent_id,
            source_binding=repository_final_witness._source_binding)
        _assert_joint_manifest(parent, task, attempt, previous)
        if (parent_id != binding.parent_job_id or parent.revision != expected_parent_revision
            or parent.status != "paused" or parent.failure_reason != "general_task_native_wait"
            or previous.phase != "native_wait"
            or task.status != WorkBoardStatus.blocked
            or task.block_reason != "general_task_native_wait"
            or child.status != "running" or child.attempt_count != 1
            or child.fencing_token != fencing_token):
            raise DurableJobLeaseError("repository final adoption requires the original native wait")
        wait_witness = repository_final_witness.wait_witness
        if (wait_witness.native_binding != binding
            or wait_witness.repository_fence < 1):
            raise DurableJobLeaseError("repository final witness child binding changed")
        receipt, _record = verify_staged_task_artifact(staged_artifact,
            parent_job_id=parent_id, creation_digest=previous.creation_digest)
        if (type(receipt) is not GeneralTaskStepReceiptV1
            or receipt.status != "verified" or receipt.contact_state != "settled"
            or receipt.child_job_id != child_id or receipt.invocation_id != child_id
            or receipt.child_attempt_count != child.attempt_count
            or receipt.child_fence != child.fencing_token
            or receipt.task_id != task.task_id or receipt.attempt_id != attempt.attempt_id
            or receipt.step_id != binding.step_id or receipt.plan_revision != binding.plan_revision
            or receipt.input_digest != binding.input_digest
            or receipt.descriptor_digest != binding.descriptor_digest
            or receipt.selected_grant_digest != binding.selected_grant_digest
            or receipt.parent_creation_digest != binding.creation_digest
            or receipt.phase_digest != binding.phase_digest
            or receipt.no_learning is not True):
            raise DurableJobLeaseError("repository final receipt is not the original verified child")
        if not any(ref.artifact_id == repository_final_witness.final_artifact_id
            and ref.digest == repository_final_witness.final_artifact_digest
            for ref in receipt.artifact_refs):
            raise DurableJobLeaseError("repository final artifact is not bound to the child receipt")
        await _validate_repository_source_witness(db, repository_final_witness,
            phase="final", parent=parent, task=task, attempt=attempt, child=child,
            manifest=previous, staged_artifact=staged_artifact, receipt=receipt)
        wait_id = repository_child_wait_checkpoint_id(binding, wait_witness.iteration_id)
        wait_payload = _repository_checkpoint_payload(wait_witness, phase="contacted_wait",
            checkpoint_id=wait_id)
        wait_record = next((item for item in _history(parent) if item.get("checkpoint_id") == wait_id), None)
        if (wait_record is None or wait_record.get("safe") is not True
            or wait_record.get("payload") != wait_payload
            or wait_record.get("state_digest") != _repository_digest(wait_payload)):
            raise DurableJobLeaseError("repository final adoption requires the original wait proof")
        final_id = repository_child_final_checkpoint_id(binding, wait_witness.iteration_id)
        final_payload = _repository_checkpoint_payload(repository_final_witness,
            phase="final_verified", checkpoint_id=final_id)
        existing_final = [item for item in _history(parent) if item.get("checkpoint_id") == final_id]
        if existing_final:
            if (len(existing_final) != 1 or existing_final[0].get("payload") != final_payload
                or existing_final[0].get("state_digest") != _repository_digest(final_payload)):
                raise DurableJobLeaseError("repository final proof is immutable")
            if child.status != "succeeded":
                raise DurableJobLeaseError("repository final proof exists before child closure")
            return {"child": _serialize(child), "job": _serialize(parent),
                "manifest": previous.model_dump(mode="json"), "receipt": receipt.model_dump(mode="json"),
                "final": final_payload, "idempotent_replay": True}
        if repository_final_authority_scope is not None:
            if not callable(repository_final_authority_scope):
                raise DurableJobLeaseError("repository final authority scope is not owned")
        if repository_final_authority_check is not None:
            if not callable(repository_final_authority_check):
                raise DurableJobLeaseError("repository final authority check is not owned")
        proposed = previous.model_copy(update={"manifest_revision": previous.manifest_revision + 1})
        refs = dict(zip(previous.step_ids, zip(previous.step_receipt_artifact_ids,
            previous.step_receipt_digests, previous.step_receipt_schemas)))
        refs[binding.step_id] = (staged_artifact.reference.artifact_id,
            staged_artifact.reference.digest, "StepReceipt.v1")
        steps = sorted(refs)
        proposed = proposed.model_copy(update={"step_ids": steps,
            "step_receipt_artifact_ids": [refs[key][0] for key in steps],
            "step_receipt_digests": [refs[key][1] for key in steps],
            "step_receipt_schemas": [refs[key][2] for key in steps]})
        _next_manifest(parent, previous, proposed, task=task, attempt=attempt)
        _validate_staged_refs(previous, proposed, (staged_artifact,))
        if repository_final_authority_scope is not None:
            scopes = AsyncExitStack()
            await scopes.__aenter__()
            try:
                await scopes.enter_async_context(repository_final_authority_scope())
            except BaseException as exc:
                await scopes.__aexit__(type(exc), exc, exc.__traceback__)
                raise
        else:
            scopes = None
        try:
            if repository_final_authority_check is not None:
                result = repository_final_authority_check(db)
                if hasattr(result, "__await__"):
                    await result
            published, values = _publish_repository_checkpoint(parent, proposed,
                checkpoint_id=final_id, payload=final_payload,
                staged_artifacts=(staged_artifact,))
            now = _utc_now()
            child_update = await db.execute(update(WorkflowRunState).where(
                WorkflowRunState.run_identity == child.run_identity,
                WorkflowRunState.revision == child.revision,
                WorkflowRunState.status == "running",
                WorkflowRunState.fencing_token == child.fencing_token,
                WorkflowRunState.lease_owner == owner,
                WorkflowRunState.lease_expires_at > now,
            ).values(status="succeeded", failure_reason=None,
                lease_owner=None, lease_expires_at=None, finished_at=now,
                result_digest=repository_final_witness.final_artifact_digest,
                result_summary="Repository cumulative repair physically verified",
                updated_at=now, heartbeat_at=now,
                revision=WorkflowRunState.revision + 1
            ).execution_options(synchronize_session=False))
            if child_update.rowcount != 1:
                raise DurableJobLeaseError("repository final child CAS changed")
            await _cas_parent(db, parent, values)
            # The source authority scope is released only after the durable
            # parent/child CAS has committed, as required by the C1 contract.
            await db.commit()
        except BaseException as exc:
            if scopes is not None:
                await scopes.__aexit__(type(exc), exc, exc.__traceback__)
                scopes = None
            raise
        else:
            if scopes is not None:
                await scopes.__aexit__(None, None, None)
                scopes = None
        return {"child": _serialize(await jobs._fetch(db, child_id)),
            "job": _serialize(await jobs._fetch(db, parent.run_identity)),
            "manifest": published.model_dump(mode="json"),
            "receipt": receipt.model_dump(mode="json"), "final": final_payload}


async def wait_native_approval(jobs, child_id, *, owner, fencing_token,
    expected_parent_revision, producer_witness, tool_name, approval_context):
    """Publish only the original callback's positively proven precontact wait."""
    from src.native_tools.task_adapters import verify_task_tool_closure
    from src.approval.repository import approval_repository
    from src.work_board.repository import _begin_sqlite_immediate
    from src.work_board.general_task_runtime_artifacts import stage_task_artifact
    from src.work_board.contracts import GeneralTaskStepReceiptV1
    from src.workflows.job_runtime import DurableJobLeaseError, _digest, _canonical, _serialize, _utc_now, _job_has_unsafe_effects
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        child = await jobs._fetch(db, child_id)
        jobs._assert_lease(child, owner=owner, fencing_token=fencing_token)
        await assert_general_task_child_current(db, child)
        binding = child_binding(child)
        parent, task, attempt, previous, _ = await _current(jobs, db, binding.parent_job_id)
        _assert_joint_manifest(parent, task, attempt, previous)
        closure = verify_task_tool_closure(producer_witness, binding=binding, fencing_token=fencing_token)
        approval_slot = read_native_checkpoint_reservation(parent, approval_checkpoint_id(binding))
        if (approval_slot.invocation_id != binding.invocation_id
            or approval_slot.binding_digest != _digest(binding.model_dump(mode="json"))):
            raise DurableJobLeaseError("original approval capacity reservation changed")
        _require_callback_reservation(parent, binding, fencing_token + 1)
        if (parent.revision != expected_parent_revision or closure.outcome != "approval_precontact"
            or closure.approval_id is None or not isinstance(approval_context, dict)
            or approval_context.get("workflow_run_identity") != child_id):
            raise DurableJobLeaseError("native approval requires exact original no-contact callback")
        approval = await db.get(ApprovalRequest, closure.approval_id)
        if approval is None or approval.fingerprint != closure.approval_fingerprint:
            raise DurableJobLeaseError("original native callback approval fingerprint changed")
        old_receipt = _step_receipt(previous, binding.step_id)
        effects = json.loads(child.effect_receipts_json or "[]")
        effect_id = "general:" + binding.step_id + ":" + str(fencing_token)
        intents = [item for item in effects if item.get("effect_id") == effect_id]
        if (len(intents) != 1 or intents[0].get("status") != "intent"
            or intents[0].get("receipt_kind") != "effect"
            or intents[0].get("effect_type") != "general_tool_call"
            or intents[0].get("details", {}).get("input_digest") != binding.input_digest
            or _job_has_unsafe_effects([item for item in effects if item.get("effect_id") != effect_id])):
            raise DurableJobLeaseError("native approval cannot settle contacted or foreign effects")
        closure_digest = _digest(closure.model_dump(mode="json"))
        no_contact = {**intents[0], "receipt_kind": "readback", "status": "succeeded",
            "content_sha256": closure_digest, "readback_id": "native-no-contact:" + _digest([child_id, fencing_token]),
            "verified_at": _utc_now().isoformat(), "recorded_at": _utc_now().isoformat(),
            "reconciled": True, "reconciliation_status": "resolved",
            "details": {**intents[0].get("details", {}), "never_contacted": True,
                "verified": True, "approval_precontact": True, "approval_id": closure.approval_id}}
        effects = [no_contact if item.get("effect_id") == effect_id else item for item in effects]
        receipt = GeneralTaskStepReceiptV1.model_validate(old_receipt.model_dump(mode="json") | {
            "status": "awaiting_approval", "contact_state": "not_contacted", "approval_id": closure.approval_id,
            "effect_receipt_digest": _digest(effects), "cleanup_receipt_digest": closure_digest})
        staged = stage_task_artifact(parent_job_id=parent.run_identity,
            creation_digest=binding.creation_digest, payload=receipt)
        proposed = _phase_successor(previous, phase="approval_wait", task_revision=task.task_revision + 1,
            job_fence=parent.fencing_token, board_fence=attempt.fencing_token)
        refs = dict(zip(previous.step_ids, zip(previous.step_receipt_artifact_ids,
            previous.step_receipt_digests, previous.step_receipt_schemas)))
        refs[binding.step_id] = (staged.reference.artifact_id, staged.reference.digest, "StepReceipt.v1")
        steps = sorted(refs)
        proposed = proposed.model_copy(update={"step_ids": steps,
            "step_receipt_artifact_ids": [refs[key][0] for key in steps],
            "step_receipt_digests": [refs[key][1] for key in steps],
            "step_receipt_schemas": [refs[key][2] for key in steps]})
        witness = GeneralTaskApprovalTransitionV1(original_binding=binding,
            original_binding_digest=_digest(binding.model_dump(mode="json")), original_claim_fence=fencing_token,
            waiting_child_fence=fencing_token, current_child_fence=fencing_token,
            approval_id=approval.id, approval_fingerprint=closure.approval_fingerprint,
            approval_context_digest=_digest(approval_context), no_contact_effect_digest=_digest(effects),
            awaiting_receipt=staged.reference, cleanup_receipt_digest=closure_digest,
            phase="approval_wait", phase_revision=proposed.phase_revision, phase_digest=proposed.phase_digest,
            manifest_revision=proposed.manifest_revision, task_revision=proposed.task_revision,
            board_fence=proposed.board_fence, job_fence=proposed.job_fence)
        _validate_staged_refs(previous, proposed, (staged,))
        published, values = _published_proofs(parent, proposed, (staged,), (
            (cleanup_checkpoint_id(binding, fencing_token), closure), (approval_checkpoint_id(binding), witness)))
        await _cas_board(db, task, attempt, status=WorkBoardStatus.blocked,
            reason="general_task_approval_required", owner=None, expiry=None, advance_fence=False)
        await _cas_parent(db, parent, {**values, "failure_reason": "general_task_approval_required"})
        await _cas_parent(db, child, {"status": "paused", "failure_reason": "general_task_approval_required",
            "lease_owner": None, "lease_expires_at": None, "effect_receipts_json": _canonical(effects)})
        attached = await approval_repository.attach_general_task_native_child_wait_binding_in_session(db,
            approval.id, binding=witness, tool_name=tool_name, approval_context=approval_context)
        if attached is None:
            raise DurableJobLeaseError("native approval scope changed before wait adoption")
        return {"child": _serialize(await jobs._fetch(db, child_id)),
            "job": _serialize(await jobs._fetch(db, parent.run_identity)),
            "manifest": published.model_dump(mode="json"), "transition": witness.model_dump(mode="json"),
            "receipt": receipt.model_dump(mode="json")}


async def resume_native_approval(jobs, child_id, *, operator_owner,
    expected_task_revision, expected_parent_revision, expected_manifest_revision, approval_id,
    service, request):
    """The sole same-attempt reclaim is bound to the canonical approved wait."""
    from src.work_board.repository import _begin_sqlite_immediate
    from src.work_board.contracts import WorkBoardOwner, GeneralTaskStepReceiptV1, GeneralTaskResume
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.general_task_runtime_artifacts import stage_task_artifact
    from src.work_board.general_task_runtime_artifacts import read_bound_native_tool_input
    from src.workflows.job_runtime import DurableJobLeaseError, _digest, _serialize, _utc_now, _job_has_unsafe_effects
    if type(service) is not GeneralTaskService or type(request) is not GeneralTaskResume or not service.started:
        raise DurableJobLeaseError("native resume requires the fixed active service and original request")
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        child = await jobs._fetch(db, child_id)
        binding = child_binding(child)
        parent, task, attempt, previous, envelope = await _current(jobs, db, binding.parent_job_id)
        _assert_joint_manifest(parent, task, attempt, previous)
        witness, approval = await verify_native_approval_transition(db, child, parent)
        effects = json.loads(child.effect_receipts_json or "[]")
        if (type(operator_owner) is not WorkBoardOwner
            or operator_owner.principal_id != binding.owner_principal_id
            or operator_owner.session_id != binding.original_root_id
            or parent.revision != expected_parent_revision or task.task_revision != expected_task_revision
            or previous.manifest_revision != expected_manifest_revision
            or request.child_job_id != child_id or request.workflow_run_id != parent.run_identity
            or request.attempt_id != attempt.attempt_id or request.fencing_token != attempt.fencing_token
            or request.expected_revision != expected_task_revision
            or request.workflow_revision != expected_parent_revision
            or request.expected_manifest_revision != expected_manifest_revision
            or request.expected_plan_revision != previous.plan_revision or request.approval_id != approval_id
            or witness.phase != "approval_wait" or witness.approval_id != approval_id
            or approval.status != "approved" or child.status != "paused"
            or child.failure_reason != "general_task_approval_required"
            or child.lease_owner or child.lease_expires_at or child.attempt_count != 1
            or parent.status != "paused" or parent.failure_reason != "general_task_approval_required"
            or parent.lease_owner or parent.lease_expires_at
            or task.status != WorkBoardStatus.blocked or task.block_reason != "general_task_approval_required"
            or attempt.lease_owner or attempt.lease_expires_at
            or _digest(effects) != witness.no_contact_effect_digest or _job_has_unsafe_effects(effects)):
            raise DurableJobLeaseError("native exact approved wait changed; never replay")
        read_bound_native_tool_input(child, binding)
        _require_callback_reservation(parent, binding, child.fencing_token + 1)
        await service.validate_native_resume(db, operator_owner, task, attempt, parent,
            previous, envelope, child, binding, request)
        next_fence = child.fencing_token + 1
        proposed = _phase_successor(previous, phase="native_wait", task_revision=task.task_revision + 1,
            job_fence=parent.fencing_token + 1, board_fence=attempt.fencing_token + 1)
        transition = GeneralTaskApprovalTransitionV1.model_validate(witness.model_dump(mode="json") | {
            "phase": "native_wait", "phase_revision": proposed.phase_revision,
            "phase_digest": proposed.phase_digest, "manifest_revision": proposed.manifest_revision,
            "task_revision": proposed.task_revision, "job_fence": proposed.job_fence,
            "board_fence": proposed.board_fence, "current_child_fence": next_fence,
            "approved_receipt_digest": approved_receipt_digest(approval, witness.approval_context_digest)})
        old = _step_receipt(previous, binding.step_id)
        receipt = GeneralTaskStepReceiptV1.model_validate(old.model_dump(mode="json") | {
            "status": "running", "contact_state": "not_contacted", "child_fence": next_fence,
            "phase_digest": proposed.phase_digest, "approval_binding_digest": _digest(transition.model_dump(mode="json")),
            "cleanup_receipt_digest": None})
        staged = stage_task_artifact(parent_job_id=parent.run_identity, creation_digest=binding.creation_digest, payload=receipt)
        refs = dict(zip(previous.step_ids, zip(previous.step_receipt_artifact_ids,
            previous.step_receipt_digests, previous.step_receipt_schemas)))
        refs[binding.step_id] = (staged.reference.artifact_id, staged.reference.digest, "StepReceipt.v1")
        steps = sorted(refs)
        proposed = proposed.model_copy(update={"step_ids": steps,
            "step_receipt_artifact_ids": [refs[key][0] for key in steps],
            "step_receipt_digests": [refs[key][1] for key in steps],
            "step_receipt_schemas": [refs[key][2] for key in steps]})
        _validate_staged_refs(previous, proposed, (staged,))
        published, values = _published_proofs(parent, proposed, (staged,), (
            (approval_checkpoint_id(binding), transition),))
        details = json.loads(approval.details_json)
        prior_details = approval.details_json
        details["general_task_wait_binding"] = transition.model_dump(mode="json")
        changed = await db.execute(update(ApprovalRequest).where(
            ApprovalRequest.id == approval.id, ApprovalRequest.status == "approved",
            ApprovalRequest.fingerprint == approval.fingerprint,
            ApprovalRequest.resolved_at == approval.resolved_at,
            ApprovalRequest.details_json == prior_details,
        ).values(details_json=json.dumps(details, sort_keys=True, separators=(",", ":")))
            .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise DurableJobLeaseError("native approval decision changed before reclaim")
        await _cas_board(db, task, attempt, status=WorkBoardStatus.blocked,
            reason="general_task_native_wait", owner=None, expiry=None, advance_fence=True)
        await _cas_parent(db, parent, {**values, "failure_reason": "general_task_native_wait",
            "fencing_token": parent.fencing_token + 1})
        runtime_owner = "general-task-native:" + child_id
        expiry = min(binding.native_deadline_at, _utc_now() + timedelta(seconds=30))
        await _cas_parent(db, child, {"status": "running", "failure_reason": None,
            "fencing_token": next_fence, "lease_owner": runtime_owner,
            "lease_expires_at": expiry, "heartbeat_at": _utc_now()})
        return {"child": _serialize(await jobs._fetch(db, child_id)),
            "job": _serialize(await jobs._fetch(db, parent.run_identity)),
            "manifest": published.model_dump(mode="json"), "transition": transition.model_dump(mode="json"),
            "receipt": receipt.model_dump(mode="json"), "runtime_owner": runtime_owner}


async def _cas_parent(db, parent, values):
    from src.workflows.job_runtime import DurableJobLeaseError, _utc_now
    values = {**values, "revision": parent.revision + 1, "updated_at": _utc_now()}
    changed = await db.execute(update(WorkflowRunState).where(
        WorkflowRunState.run_identity == parent.run_identity,
        WorkflowRunState.revision == parent.revision,
        WorkflowRunState.status == parent.status,
        WorkflowRunState.fencing_token == parent.fencing_token,
        WorkflowRunState.lease_owner == parent.lease_owner,
        WorkflowRunState.lease_expires_at == parent.lease_expires_at,
    ).values(**values).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DurableJobLeaseError("general task original parent CAS changed")


def _assert_joint_manifest(parent, task, attempt, manifest):
    from src.workflows.job_runtime import DurableJobLeaseError
    if (manifest.task_revision != task.task_revision or manifest.board_fence != attempt.fencing_token
        or manifest.job_fence != parent.fencing_token):
        raise DurableJobLeaseError("general task current joint phase counters changed")


def _cancel_state(children):
    if any(item.original_attempt_count and item.closure is None
           and item.repository_closure is None and item.delegation_closure_digest is None for item in children):
        return "pending"
    if any(item.effect_debt for item in children):
        return "callback_closed_outcome_debt"
    return "fully_cancelled"


def _cancel_witness(parent, task, attempt):
    from src.workflows.job_runtime import DurableJobLeaseError, _digest, _as_utc
    witness = _protected_payload(parent, cancel_checkpoint_id(parent.run_identity, attempt.attempt_id), GeneralTaskNativeCancelV1)
    manifest = read_manifest(parent)
    original = witness.original_manifest
    protected_checkpoint_ids(_history(parent))
    assert_original_parent_authority(parent)
    mutable = {"task_revision", "manifest_revision", "phase_revision", "phase_digest", "board_fence", "job_fence", "phase"}
    if (manifest is None or task.task_id != original.task_id or attempt.attempt_id != original.attempt_id
        or parent.run_identity != original.run_id or attempt.workflow_run_id != parent.run_identity
        or attempt.task_id != task.task_id or attempt.cancel_requested_at is None
        or parent.authority_digest != witness.original_parent_authority_digest
        or parent.input_digest != witness.original_parent_input_digest
        or task.owner_principal_id != original.owner_principal_id or parent.owner_principal_id != original.owner_principal_id
        or task.owner_session_id != original.original_root_id or parent.operator_session_id != original.original_root_id
        or parent.session_id != original.original_root_id or task.input_artifact_id != witness.input_artifact_id
        or task.typed_input_ref != witness.typed_input_ref or task.typed_input_digest != witness.typed_input_digest
        or task.goal_id != witness.goal_id or task.goal_revision != witness.goal_revision
        or _as_utc(parent.deadline_at) != original.native_deadline_at
        or manifest.creation_digest != original.creation_digest
        or manifest.admitted_invocation_ids != original.admitted_invocation_ids
        or any(getattr(manifest, field) != getattr(witness, field) for field in (
            "task_revision", "manifest_revision", "phase_revision", "phase_digest", "board_fence", "job_fence", "phase"))
        or task.task_revision != witness.task_revision or attempt.fencing_token != witness.board_fence
        or parent.fencing_token != witness.job_fence or witness.state != _cancel_state(witness.children)
        or witness.phase != ("cancelled" if witness.state == "fully_cancelled" else "unknown_recovery")
        or parent.status != ("cancelled" if witness.state == "fully_cancelled" else "blocked")
        or task.status != WorkBoardStatus.blocked or task.block_reason != "general_task_native_cancel_" + witness.state
        or bool(attempt.ended_at) != (witness.state == "fully_cancelled")
        or manifest.model_dump(mode="json", exclude=mutable) != original.model_dump(mode="json", exclude=mutable)
        or len({item.original_binding.invocation_id for item in witness.children}) != len(witness.children)
        or set(item.original_binding.invocation_id for item in witness.children) != set(original.admitted_invocation_ids)
        or parent.lease_owner is not None or parent.lease_expires_at is not None
        or attempt.lease_owner is not None or attempt.lease_expires_at is not None):
        raise DurableJobLeaseError("original native cancellation binding changed")
    for item in witness.children:
        binding = item.original_binding
        if (item.original_binding_digest != _digest(binding.model_dump(mode="json"))
            or binding.parent_job_id != parent.run_identity or binding.task_id != task.task_id
            or binding.attempt_id != attempt.attempt_id or binding.creation_digest != original.creation_digest
            or binding.original_root_id != original.original_root_id
            or binding.parent_authority_digest != witness.original_parent_authority_digest
            or binding.original_envelope_digest != original.original_envelope_digest
            or binding.selected_grant_digest != original.selected_grant_digest
            or binding.original_deadline_at != original.original_deadline_at
            or binding.native_deadline_at != original.native_deadline_at
            or binding.goal_id != witness.goal_id or binding.goal_revision != witness.goal_revision
            or (item.original_attempt_count == 0 and (item.original_claim_fence != 0
                or item.closure is not None or item.repository_closure is not None))
            or (item.original_attempt_count == 1 and item.original_claim_fence <= 0)):
            raise DurableJobLeaseError("original cancellation child binding changed")
        if item.closure is not None:
            closure = _protected_payload(parent, cleanup_checkpoint_id(binding, item.original_claim_fence), GeneralTaskToolClosureV1)
            if closure != item.closure or closure.original_binding_digest != item.original_binding_digest:
                raise DurableJobLeaseError("original cancellation callback closure changed")
        if item.repository_closure is not None:
            if (type(item.repository_closure) is not RepositoryNativeStopClosureV1
                    or item.closure is not None
                    or item.repository_closure.original_binding != binding
                    or item.repository_closure.original_input_digest != binding.input_digest):
                raise DurableJobLeaseError("original repository stop closure changed")
        if item.delegation_closure_digest is not None:
            from src.workflows.specialist_lifecycle import SpecialistDelegationClosureV1
            closure = _protected_payload(parent, cleanup_checkpoint_id(binding,item.original_claim_fence),SpecialistDelegationClosureV1)
            if (closure.outcome != "durable_result_verified" or closure.original_binding_digest != item.original_binding_digest
                or _digest(closure.model_dump(mode="json")) != item.delegation_closure_digest):
                raise DurableJobLeaseError("original full delegation closure changed")
        if item.delegation_stop_checkpoint is not None:
            from src.workflows.specialist_stop import SpecialistDelegationCancelV1, stop_checkpoint_id
            fact = _protected_payload(parent,item.delegation_stop_checkpoint,SpecialistDelegationCancelV1)
            if (item.delegation_stop_checkpoint != stop_checkpoint_id(binding.invocation_id)
                or fact.invocation_id != binding.invocation_id or fact.original_binding_digest != item.original_binding_digest
                or fact.parent_job_id != parent.run_identity or fact.parent_creation_digest != original.creation_digest
                or fact.stop_action != witness.stop_action):
                raise DurableJobLeaseError("original specialist cancellation lineage changed")
    return witness


def read_general_task_native_cancel(parent, task, attempt):
    """Owner-selected observation only; no lease, private bytes or authority."""
    witness = _cancel_witness(parent, task, attempt)
    return {"state": witness.state, "child_ids": [item.original_binding.invocation_id for item in witness.children],
        "callback_closed": witness.state != "pending", "effect_debt": any(item.effect_debt for item in witness.children),
        "reason": "general_task_native_cancel_" + witness.state, "stop_action": witness.stop_action}


async def _cancel_original(jobs, db, parent_id, *, observation=False):
    """Cancellation-only metadata compiler; expired execution clocks grant nothing."""
    from src.db.models import Goal
    from src.workflows.job_runtime import DurableJobLeaseError, _digest, _as_utc
    parent = await jobs._fetch(db, parent_id)
    assert_original_parent_authority(parent)
    manifest = read_manifest(parent)
    if manifest is None:
        raise DurableJobLeaseError("native cancellation original manifest unavailable")
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == manifest.task_id))
    attempt = await db.get(WorkBoardAttempt, manifest.attempt_id)
    if task is None or attempt is None:
        raise DurableJobLeaseError("native cancellation original task/attempt missing")
    artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
    goal = await db.get(Goal, task.goal_id)
    if (parent.job_kind != "agent.task.v1" or parent.capability_version != "1" or parent.parent_job_id
        or parent.owner_kind != "user" or parent.branch_depth != 0 or task.capability_id != "agent.task.v1"
        or attempt.task_id != task.task_id or attempt.workflow_run_id != parent_id
        or task.owner_principal_id != manifest.owner_principal_id or parent.owner_principal_id != task.owner_principal_id
        or parent.operator_session_id != task.owner_session_id or parent.session_id != task.owner_session_id
        or task.owner_session_id != manifest.original_root_id or _as_utc(parent.deadline_at) != manifest.native_deadline_at
        or task.input_artifact_id != manifest.original_envelope_artifact_id
        or task.typed_input_digest != manifest.original_envelope_digest
        or artifact is None or artifact.payload_sha256 != task.typed_input_digest
        or artifact.typed_input_ref != task.typed_input_ref or artifact.bound_task_id != task.task_id
        or artifact.owner_principal_id != task.owner_principal_id or artifact.owner_session_id != task.owner_session_id
        or artifact.goal_id != task.goal_id or artifact.goal_revision != task.goal_revision
        or artifact.capability_id != task.capability_id or artifact.capability_version != "1"
        or goal is None or (not observation and goal.revision != task.goal_revision) or goal.owner_principal_id != task.owner_principal_id
        or goal.owner_session_id != task.owner_session_id):
        raise DurableJobLeaseError("native cancellation original metadata changed")
    children = list((await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.parent_job_id == parent_id))).scalars().all())
    if observation:
        original_roots = {child_binding(child).live_root_digest for child in children}
        if len(original_roots) != 1:
            raise DurableJobLeaseError("original cancelled workspace Root seals disagree")
        root_digest = next(iter(original_roots))
    else:
        from src.work_board.pipelines import root_binding
        root_digest = _digest(root_binding())
    creation = _digest(["general-task.creation.v1", parent.run_identity, parent.input_digest,
        parent.authority_digest, task.task_id, attempt.attempt_id, task.owner_principal_id,
        task.owner_session_id, task.goal_id, task.goal_revision, task.input_artifact_id,
        task.typed_input_digest, manifest.group_id, manifest.group_digest, manifest.selected_grant_digest,
        root_digest, _as_utc(parent.deadline_at).isoformat(), manifest.original_deadline_at.isoformat()])
    if creation != manifest.creation_digest:
        raise DurableJobLeaseError("native cancellation original creation seal changed")
    _assert_joint_manifest(parent, task, attempt, manifest)
    if set(item.run_identity for item in children) != set(manifest.admitted_invocation_ids):
        raise DurableJobLeaseError("native cancellation admitted set changed")
    for child in children:
        binding = child_binding(child)
        if (binding.parent_job_id != parent_id or binding.task_id != task.task_id
            or binding.attempt_id != attempt.attempt_id or binding.creation_digest != manifest.creation_digest
            or binding.parent_authority_digest != parent.authority_digest
            or binding.original_envelope_digest != manifest.original_envelope_digest
            or binding.selected_grant_digest != manifest.selected_grant_digest
            or binding.original_root_id != task.owner_session_id or binding.owner_principal_id != task.owner_principal_id
            or binding.goal_id != task.goal_id or binding.goal_revision != task.goal_revision
            or binding.original_deadline_at != manifest.original_deadline_at
            or binding.native_deadline_at != manifest.native_deadline_at
            or child.attempt_count not in {0, 1}
            or (child.attempt_count == 0 and (child.fencing_token != 0 or child.lease_owner
                or child.lease_expires_at or json.loads(child.effect_receipts_json or "[]")
                or json.loads(child.checkpoint_receipts_json or "[]")))
            or (child.attempt_count == 1 and child.fencing_token <= 0)):
            raise DurableJobLeaseError("native cancellation original child changed")
    return parent, task, attempt, manifest, artifact, goal, children


async def _cancel_cas_job(db, row, values, *, child_entries=None, child_rows=()):
    from src.workflows.job_runtime import DurableJobLeaseError, _utc_now
    exact = ("revision", "fencing_token", "status", "attempt_count", "lease_owner", "lease_expires_at",
        "checkpoint_receipts_json", "artifact_receipts_json", "effect_receipts_json", "declared_authority_json",
        "authority_digest", "input_digest", "arguments_json", "deadline_at", "owner_principal_id",
        "operator_session_id", "goal_id", "goal_revision", "parent_job_id")
    conditions = [WorkflowRunState.run_identity == row.run_identity,
        *(getattr(WorkflowRunState, field) == getattr(row, field) for field in exact)]
    if child_entries is not None:
        sibling = aliased(WorkflowRunState)
        conditions.append(select(func.count(sibling.id)).where(sibling.parent_job_id == row.run_identity).scalar_subquery() == len(child_entries))
        for item in child_entries:
            original_child = next(child for child in child_rows if child.run_identity == item.original_binding.invocation_id)
            conditions.append(select(sibling.id).where(
                sibling.run_identity == original_child.run_identity, sibling.parent_job_id == row.run_identity,
                sibling.revision == item.current_child_revision, sibling.fencing_token == item.current_child_fence,
                sibling.attempt_count == item.original_attempt_count, sibling.lease_owner.is_(None), sibling.lease_expires_at.is_(None),
                sibling.effect_receipts_json == original_child.effect_receipts_json,
                sibling.artifact_receipts_json == original_child.artifact_receipts_json,
                sibling.checkpoint_receipts_json == original_child.checkpoint_receipts_json,
                sibling.declared_authority_json == original_child.declared_authority_json,
                sibling.input_digest == original_child.input_digest,
                sibling.arguments_json == original_child.arguments_json).exists())
    changed = await db.execute(update(WorkflowRunState).where(*conditions).values(
            **values, revision=row.revision + 1, updated_at=_utc_now()).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DurableJobLeaseError("native cancellation exact journal CAS changed")


async def _cancel_cas_board(db, task, attempt, artifact, goal, *, state, first):
    from src.db.models import Goal
    from src.workflows.job_runtime import DurableJobLeaseError, _utc_now
    terminal = state == "fully_cancelled"
    now = _utc_now()
    metadata = select(WorkBoardInputArtifact.artifact_id).where(
        WorkBoardInputArtifact.artifact_id == artifact.artifact_id,
        WorkBoardInputArtifact.revision == artifact.revision,
        WorkBoardInputArtifact.metadata_digest == artifact.metadata_digest,
        WorkBoardInputArtifact.typed_input_ref == task.typed_input_ref,
        WorkBoardInputArtifact.payload_sha256 == task.typed_input_digest,
        WorkBoardInputArtifact.owner_principal_id == task.owner_principal_id,
        WorkBoardInputArtifact.owner_session_id == task.owner_session_id,
        WorkBoardInputArtifact.bound_task_id == task.task_id).exists()
    current_goal = select(Goal.id).where(Goal.id == goal.id, Goal.revision == goal.revision,
        Goal.owner_principal_id == task.owner_principal_id, Goal.owner_session_id == task.owner_session_id).exists()
    changed = await db.execute(update(WorkBoardTask).where(
        WorkBoardTask.task_id == task.task_id, WorkBoardTask.task_revision == task.task_revision,
        WorkBoardTask.status == task.status, WorkBoardTask.owner_principal_id == task.owner_principal_id,
        WorkBoardTask.owner_session_id == task.owner_session_id, WorkBoardTask.input_artifact_id == task.input_artifact_id,
        WorkBoardTask.typed_input_ref == task.typed_input_ref, WorkBoardTask.typed_input_digest == task.typed_input_digest,
        metadata, current_goal).values(status=WorkBoardStatus.blocked,
        block_kind="needs_input" if terminal else "unknown_effect", block_reason="general_task_native_cancel_" + state,
        block_source_status="running", task_revision=task.task_revision + 1,
        updated_at=now).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DurableJobLeaseError("native cancellation exact Board CAS changed")
    changed = await db.execute(update(WorkBoardAttempt).where(
        WorkBoardAttempt.attempt_id == attempt.attempt_id, WorkBoardAttempt.task_id == task.task_id,
        WorkBoardAttempt.workflow_run_id == attempt.workflow_run_id,
        WorkBoardAttempt.fencing_token == attempt.fencing_token,
        WorkBoardAttempt.cancel_requested_at == attempt.cancel_requested_at,
        WorkBoardAttempt.ended_at == attempt.ended_at,
        WorkBoardAttempt.lease_owner == attempt.lease_owner,
        WorkBoardAttempt.lease_expires_at == attempt.lease_expires_at).values(
            cancel_requested_at=attempt.cancel_requested_at or now, fencing_token=attempt.fencing_token + int(first),
            lease_owner=None, lease_expires_at=None, ended_at=now if terminal else None,
            outcome="cancelled" if terminal else "general_task_native_cancel_" + state,
            updated_at=now).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DurableJobLeaseError("native cancellation exact attempt CAS changed")


async def _cancel_result(jobs, db, parent_id, task_id, attempt_id, event=None):
    parent = await jobs._fetch(db, parent_id)
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id).execution_options(populate_existing=True))
    attempt = await db.get(WorkBoardAttempt, attempt_id, populate_existing=True)
    from src.workflows.job_runtime import _serialize
    witness = _cancel_witness(parent,task,attempt)
    from src.workflows.specialist_stop import verify_specialist_stop
    for entry in witness.children:
        if entry.delegation_stop_checkpoint is not None:
            await verify_specialist_stop(db,parent,entry)
    return {"task": task, "attempt": attempt, "event": event,
        "cancellation": read_general_task_native_cancel(parent, task, attempt), "job": _serialize(parent)}


async def cancel_native_parent(jobs, parent_id, *, operator_owner, expected_task_revision,
    repository_stop_witness=None, stop_action="cancel", expected_revision=None,
    expected_manifest_revision=None):
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.repository import _begin_sqlite_immediate, WorkBoardRepository
    from src.workflows.job_runtime import DurableJobLeaseError, _digest, _utc_now, _as_utc
    if repository_stop_witness is not None:
        from src.workflows.repo_repair_source import assert_repository_stop_witness
        assert_repository_stop_witness(repository_stop_witness)
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        # A repository stop witness carries the source-owned original Root
        # binding that was staged before entering this writer.  Keep the
        # generic cancellation path unchanged, but use its observational
        # root-digest branch for the private source path so cancellation does
        # not call root_binding() (and therefore does no filesystem/provider
        # work) while the SQLite transaction is open.  The source validator
        # rechecks the current Goal and staged Root binding below.
        parent, task, attempt, previous, artifact, goal, children = await _cancel_original(
            jobs, db, parent_id, observation=repository_stop_witness is not None)
        if (type(operator_owner) is not WorkBoardOwner or operator_owner.principal_id != task.owner_principal_id
            or operator_owner.session_id != task.owner_session_id or task.task_revision != expected_task_revision
            or stop_action not in {"cancel","pause"}
            or (expected_revision is not None and parent.revision != expected_revision)
            or (expected_manifest_revision is not None and previous.manifest_revision != expected_manifest_revision)):
            raise DurableJobLeaseError("original native cancellation owner/revision changed")
        if attempt.cancel_requested_at:
            return await _cancel_result(jobs, db, parent_id, task.task_id, attempt.attempt_id)
        root = await db.get(OperatorSession, task.owner_session_id)
        if (root is None or root.principal_id != task.owner_principal_id or root.revoked_at or root.replaced_by_id
            or root.is_bearer_tombstone or _as_utc(root.idle_expires_at) <= _utc_now()
            or _as_utc(root.absolute_expires_at) <= _utc_now() or attempt.ended_at
            or previous.phase not in {"native_ready", "assembly", "native_wait", "approval_wait", "operator_paused", "unknown_recovery"}
            or task.status not in {WorkBoardStatus.running, WorkBoardStatus.blocked}):
            raise DurableJobLeaseError("original authenticated native cancellation scope unavailable")
        reservation = _protected_payload(parent, cancel_checkpoint_id(parent_id, attempt.attempt_id), GeneralTaskCheckpointReservationV1)
        if (reservation.parent_job_id != parent_id or reservation.attempt_id != attempt.attempt_id
            or reservation.creation_digest != previous.creation_digest or reservation.invocation_id is not None):
            raise DurableJobLeaseError("original cancellation capacity reservation changed")
        repository_closures = {}
        if repository_stop_witness is not None:
            # The source-private witness is validated inside this existing
            # writer.  A public projection, copied dataclass, or absent source
            # owner can never enter the generic cancellation path.
            try:
                from src.workflows.repo_repair_source import validate_repository_stop_witness
            except (ImportError, AttributeError) as exc:
                raise DurableJobLeaseError("repository stop witness validator unavailable") from exc
            repository_closures = await validate_repository_stop_witness(
                db,
                repository_stop_witness,
                parent=parent,
                task=task,
                attempt=attempt,
                children=children,
            )
            if type(repository_closures) is not dict or not repository_closures:
                raise DurableJobLeaseError("repository stop witness did not produce closed child evidence")
            child_ids = {child.run_identity for child in children}
            if not set(repository_closures).issubset(child_ids) or any(type(child_id) is not str or child_id not in child_ids
                   or type(closure) is not RepositoryNativeStopClosureV1
                   or closure.original_binding.invocation_id != child_id
                   or closure.original_binding != child_binding(next(child for child in children if child.run_identity == child_id))
                   or json.loads(next(child for child in children if child.run_identity == child_id).arguments_json).get("tool_id") != "repository_work"
                   for child_id, closure in repository_closures.items()):
                raise DurableJobLeaseError("repository stop witness child binding changed")
        from src.workflows.specialist_stop import (compile_specialist_stops,apply_specialist_stops,stop_checkpoint_id)
        specialist_stops = await compile_specialist_stops(jobs,db,parent,previous,children,action=stop_action)
        for item in specialist_stops:
            reserved = _protected_payload(parent,stop_checkpoint_id(item.fact.invocation_id),GeneralTaskCheckpointReservationV1)
            if (reserved.invocation_id != item.fact.invocation_id or reserved.parent_job_id != parent_id
                or reserved.creation_digest != previous.creation_digest):
                raise DurableJobLeaseError("specialist stop capacity reservation changed")
        entries = []
        for child in children:
            binding = child_binding(child)
            repository_closure = repository_closures.get(child.run_identity)
            if (repository_closure is not None
                    and (repository_closure.original_binding != binding
                         or repository_closure.original_input_digest != binding.input_digest)):
                raise DurableJobLeaseError("repository stop closure current binding changed")
            if repository_closure is not None:
                ordinary_cleanup = [item for item in _history(parent)
                    if item.get("checkpoint_id") == cleanup_checkpoint_id(
                        binding, repository_closure.original_claim_fence)]
                if any(item.get("payload", {}).get("schema_version") == "general_task.tool_closure.v1"
                       for item in ordinary_cleanup):
                    raise DurableJobLeaseError("repository stop closure conflicts with ordinary callback closure")
            claimed_fence = (
                repository_closure.original_claim_fence
                if repository_closure is not None
                else (child.fencing_token if child.attempt_count else 0)
            )
            if (repository_closure is not None
                    and ((child.attempt_count and claimed_fence != child.fencing_token)
                         or (not child.attempt_count and claimed_fence != 0))):
                raise DurableJobLeaseError("repository stop closure claim fence changed")
            closure = None
            delegation_closure_digest = None
            if child.attempt_count:
                # approval_wait fenced a positively closed precontact callback.
                if repository_closure is None and previous.phase == "approval_wait":
                    wait = _protected_payload(parent, approval_checkpoint_id(binding), GeneralTaskApprovalTransitionV1)
                    if (wait.original_binding != binding or wait.phase != "approval_wait"
                        or wait.current_child_fence != child.fencing_token
                        or any(getattr(wait, field) != getattr(previous, field) for field in (
                            "task_revision", "manifest_revision", "phase_revision", "phase_digest", "board_fence", "job_fence"))):
                        raise DurableJobLeaseError("original cancelled approval wait changed")
                    claimed_fence = wait.original_claim_fence
                _require_callback_reservation(parent, binding, claimed_fence)
                if repository_closure is None:
                    record = next(item for item in _history(parent)
                        if item["checkpoint_id"] == cleanup_checkpoint_id(binding, claimed_fence))
                    if record["payload"].get("schema_version") == "general_task.tool_closure.v1":
                        closure = _protected_payload(parent, record["checkpoint_id"], GeneralTaskToolClosureV1)
                    elif record["payload"].get("schema_version") == "SpecialistDelegationClosure.v1":
                        from src.workflows.specialist_result import verify_full_delegation_result
                        full = await verify_full_delegation_result(db,parent,child,_step_receipt(previous,binding.step_id))
                        delegation_closure_digest = _digest(full.model_dump(mode="json"))
                    if previous.phase == "approval_wait" and (closure is None
                        or closure.outcome != "approval_precontact" or closure.approval_id != wait.approval_id
                        or closure.approval_fingerprint != wait.approval_fingerprint
                        or _digest(closure.model_dump(mode="json")) != wait.cleanup_receipt_digest):
                        raise DurableJobLeaseError("original cancelled precontact closure changed")
            effects = json.loads(child.effect_receipts_json or "[]")
            safe_effects = bool(repository_closure or delegation_closure_digest or (closure and (closure.outcome == "approval_precontact" or
                (closure.outcome == "returned" and child.status in {"succeeded", "degraded"}
                 and effects and all(item.get("status") in {"succeeded", "cancelled", "failed"} for item in effects)))))
            immutable_completed = child.status in {"succeeded", "degraded"} and safe_effects
            entries.append(GeneralTaskNativeCancelChildV1(original_binding=binding,
                original_binding_digest=_digest(binding.model_dump(mode="json")), original_attempt_count=child.attempt_count,
                original_claim_fence=claimed_fence, original_revision=child.revision,
                current_child_fence=child.fencing_token + int(not immutable_completed),
                current_child_revision=child.revision + int(not immutable_completed),
                effect_digest=_digest(effects), artifact_digest=_digest(json.loads(child.artifact_receipts_json or "[]")),
                checkpoint_digest=_digest(json.loads(child.checkpoint_receipts_json or "[]")),
                closure=closure, repository_closure=repository_closure,
                delegation_closure_digest=delegation_closure_digest,
                delegation_stop_checkpoint=stop_checkpoint_id(child.run_identity) if any(
                    item.fact.invocation_id==child.run_identity for item in specialist_stops) else None,
                effect_debt=bool(child.attempt_count and (not safe_effects or any(
                    item.fact.cost_unresolved for item in specialist_stops)))))
        state = _cancel_state(entries)
        proposed = _phase_successor(previous, phase="cancelled" if state == "fully_cancelled" else "unknown_recovery",
            task_revision=task.task_revision + 1, job_fence=parent.fencing_token + 1,
            board_fence=attempt.fencing_token + 1)
        witness = GeneralTaskNativeCancelV1(original_manifest=previous,
            original_parent_authority_digest=parent.authority_digest, original_parent_input_digest=parent.input_digest,
            input_artifact_id=task.input_artifact_id, typed_input_ref=task.typed_input_ref,
            typed_input_digest=task.typed_input_digest, goal_id=task.goal_id, goal_revision=task.goal_revision,
            **{field: getattr(proposed, field) for field in ("task_revision", "manifest_revision", "phase_revision",
                "phase_digest", "board_fence", "job_fence", "phase")}, state=state, children=entries,stop_action=stop_action)
        proofs=tuple((stop_checkpoint_id(item.fact.invocation_id),item.fact) for item in specialist_stops)
        published, values = _published_proofs(parent, proposed, (),
            (*proofs,(cancel_checkpoint_id(parent_id, attempt.attempt_id), witness)))
        await apply_specialist_stops(db,specialist_stops)
        for child in children:
            entry = next(item for item in entries if item.original_binding.invocation_id == child.run_identity)
            if entry.current_child_revision == child.revision:
                continue
            await _cancel_cas_job(db, child, {"status": "cancelled" if state == "fully_cancelled" or not child.attempt_count else "blocked",
                "failure_reason": "general_task_native_cancel_" + state, "fencing_token": child.fencing_token + 1,
                "lease_owner": None, "lease_expires_at": None})
        await _cancel_cas_board(db, task, attempt, artifact, goal, state=state, first=True)
        await _cancel_cas_job(db, parent, {**values, "status": "cancelled" if state == "fully_cancelled" else "blocked",
            "failure_reason": "general_task_native_cancel_" + state, "fencing_token": proposed.job_fence,
            "lease_owner": None, "lease_expires_at": None}, child_entries=entries, child_rows=children)
        if repository_stop_witness is not None:
            try:
                from src.workflows.repo_repair_source import complete_repository_stop_in_writer
            except (ImportError, AttributeError) as exc:
                raise DurableJobLeaseError("repository stop completion unavailable") from exc
            # The source terminal Root/RepoTask mutation is part of this same
            # SQL transaction, after the existing C1 CAS and before commit.
            await complete_repository_stop_in_writer(db, repository_stop_witness, jobs=jobs)
        await db.refresh(task)
        event = await WorkBoardRepository._event(db, task, operator_owner, kind="attempt.cancel_requested",
            metadata={"attempt_id": attempt.attempt_id, "workflow_run_id": parent_id,
                "cancel_key": f"work-board-cancel:{task.task_id}:{attempt.attempt_id}",
                "cancellation_state": state, "no_learning": True})
        return await _cancel_result(jobs, db, parent_id, task.task_id, attempt.attempt_id, event)


def _cancel_returned_output_verified(child, entry, closure, output_root_witness):
    """Observe only the original output/readback; never authorize execution."""
    from src.workflows.job_runtime import _digest, _effect_ledger_or_raise, _job_has_unsafe_effects
    from src.work_board.general_task import digest
    from src.work_board.repository import BoardError
    if closure.outcome != "returned" or closure.output_digest is None or output_root_witness is None:
        return False
    try:
        from src.work_board.general_task_runtime_artifacts import (verify_native_cancel_output_witness,
            read_native_cancel_output_bytes)
        from src.artifacts.registry import artifact_id_for
        _original_root, intent = verify_native_cancel_output_witness(output_root_witness,
            binding=entry.original_binding, fencing_token=entry.original_claim_fence)
        binding, fence = entry.original_binding, entry.original_claim_fence
        effect_id = "general:" + binding.step_id + ":" + str(fence)
        target = "general-step:" + digest([binding.invocation_id, binding.step_id])
        if (intent.get("effect_id") != effect_id or intent.get("receipt_kind") != "effect"
            or intent.get("effect_type") != "general_tool_call" or intent.get("status") != "intent"
            or intent.get("fencing_token") != fence or intent.get("target_path") != target
            or intent.get("details", {}).get("step_id") != binding.step_id
            or intent.get("details", {}).get("input_digest") != binding.input_digest
            or intent.get("details", {}).get("no_learning") is not True):
            return False
        records = [item for item in _history(child) if item.get("checkpoint_id") == "general:artifact:" + binding.step_id]
        if len(records) != 1 or records[0].get("safe") is not True or records[0].get("fencing_token") != fence:
            return False
        artifact_binding = records[0].get("payload")
        if (type(artifact_binding) is not dict or records[0].get("state_digest") != _digest(artifact_binding)
            or set(artifact_binding) != {"schema_version", "producer_ref", "step_id", "plan_digest", "producer_fence",
                "file_path", "content_sha256", "size_bytes", "no_learning"}
            or artifact_binding["schema_version"] != 1 or artifact_binding["producer_ref"] != child.run_identity
            or artifact_binding["step_id"] != binding.step_id or artifact_binding["plan_digest"] != binding.plan_digest
            or artifact_binding["producer_fence"] != fence or artifact_binding["no_learning"] is not True):
            return False
        sha, size = artifact_binding["content_sha256"], artifact_binding["size_bytes"]
        key = digest([child.run_identity, binding.plan_digest, binding.step_id])
        path = f"artifacts/work-board/general-tasks/{key}-{sha}.json"
        if (type(sha) is not str or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha)
            or type(size) is not int or not 0 < size <= 65536 or artifact_binding["file_path"] != path):
            return False
        artifacts = [item for item in json.loads(child.artifact_receipts_json) if item.get("file_path") == path]
        expected_id = artifact_id_for(file_path=path, artifact_type="general_task_step", producer=child.job_kind,
            run_id=child.run_identity, content_sha256=sha)
        if (len(artifacts) != 1 or any(artifacts[0].get(field) != expected for field, expected in {
            "artifact_id": expected_id, "artifact_type": "general_task_step", "producer": child.job_kind,
            "file_path": path, "content_sha256": sha, "size_bytes": size}.items())):
            return False
        effects = _effect_ledger_or_raise(child.effect_receipts_json)
        if _job_has_unsafe_effects(effects):
            return False
        calls = [item for item in effects if item.get("effect_id") == effect_id]
        if (len(calls) != 1 or any(calls[0].get(field) != expected for field, expected in {
            "receipt_kind": "readback", "effect_type": "general_tool_call", "status": "succeeded",
            "target_path": target, "content_sha256": sha, "fencing_token": fence,
            "readback_id": "general-step-readback:" + digest([binding.invocation_id, binding.step_id])[:32],
            "reconciled": True, "reconciliation_status": "resolved"}.items())
            or any(calls[0].get("details", {}).get(field) != expected for field, expected in {
                "step_id": binding.step_id, "tool_id": intent["details"].get("tool_id"),
                "input_digest": binding.input_digest, "original_intent_digest": _digest(intent),
                "verified": True, "output_exists": True, "file_path": path, "no_learning": True}.items())):
            return False
        artifact_reads = [item for item in effects if item.get("readback_id") == "general-artifact:" + key[:32]]
        if (len(artifact_reads) != 1 or any(artifact_reads[0].get(field) != expected for field, expected in {
            "receipt_kind": "readback", "effect_type": "general_task_artifact_readback", "status": "succeeded",
            "target_path": path, "target_digest": sha, "content_sha256": sha, "fencing_token": fence,
            "reconciled": True, "reconciliation_status": "resolved"}.items())
            or artifact_reads[0].get("details", {}).get("verified") is not True
            or artifact_reads[0].get("details", {}).get("output_exists") is not True):
            return False
        body = json.loads(read_native_cancel_output_bytes(output_root_witness, binding=binding,
            fencing_token=fence, file_path=path, expected_digest=sha, expected_size=size))
        return (type(body) is dict and set(body) == {"step_id", "output"}
            and body["step_id"] == binding.step_id and digest(body["output"]) == closure.output_digest)
    except (BoardError, ValueError, TypeError, KeyError, PermissionError, OSError):
        # Physical closure is still real; unavailable outcome evidence remains debt.
        return False


async def observe_native_cancel_closure(jobs, child_id, *, producer_witness, output_root_witness=None):
    from src.native_tools.task_adapters import verify_task_tool_closure
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.job_runtime import DurableJobLeaseError, _digest
    async with jobs._session() as probe:
        original_child=await jobs._fetch(probe,child_id)
        nested=original_child.branch_depth==3 and original_child.job_kind==GENERAL_TASK_NATIVE_CHILD_KIND
    if nested:
        from src.workflows.specialist_stop import observe_specialist_stop_closure
        return await observe_specialist_stop_closure(jobs,child_id,producer_witness=producer_witness)
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        child = await jobs._fetch(db, child_id)
        binding = child_binding(child)
        parent, task, attempt, previous, artifact, goal, children = await _cancel_original(jobs, db, binding.parent_job_id, observation=True)
        witness = _cancel_witness(parent, task, attempt)
        entry = next((item for item in witness.children if item.original_binding.invocation_id == child_id), None)
        if entry is None or entry.original_attempt_count != 1 or child_binding(child) != entry.original_binding:
            raise DurableJobLeaseError("original cancelled callback not admitted")
        for item, row in ((item, next(row for row in children if row.run_identity == item.original_binding.invocation_id)) for item in witness.children):
            if (row.revision != item.current_child_revision or row.fencing_token != item.current_child_fence
                or row.attempt_count != item.original_attempt_count or row.lease_owner or row.lease_expires_at
                or _digest(json.loads(row.effect_receipts_json or "[]")) != item.effect_digest
                or _digest(json.loads(row.artifact_receipts_json or "[]")) != item.artifact_digest
                or _digest(json.loads(row.checkpoint_receipts_json or "[]")) != item.checkpoint_digest):
                raise DurableJobLeaseError("cancelled native child current fence/journal changed")
        closure = verify_task_tool_closure(producer_witness, binding=entry.original_binding,
            fencing_token=entry.original_claim_fence)
        output_verified = _cancel_returned_output_verified(child, entry, closure, output_root_witness)
        if entry.closure is not None:
            if entry.closure != closure:
                raise DurableJobLeaseError("original cancellation closure collision")
            if not entry.effect_debt or not output_verified:
                return await _cancel_result(jobs, db, parent.run_identity, task.task_id, attempt.attempt_id)
        _require_callback_reservation(parent, binding, entry.original_claim_fence)
        updated = entry.model_copy(update={"closure": closure,
            "effect_debt": not (output_verified or
                (closure.outcome == "approval_precontact" and closure.approval_fingerprint is not None))})
        entries = [updated if item.original_binding.invocation_id == child_id else item for item in witness.children]
        state = _cancel_state(entries)
        proposed = _phase_successor(previous, phase="cancelled" if state == "fully_cancelled" else "unknown_recovery",
            task_revision=task.task_revision + 1, job_fence=parent.fencing_token, board_fence=attempt.fencing_token)
        updated_witness = GeneralTaskNativeCancelV1.model_validate(witness.model_dump(mode="json") | {
            field: getattr(proposed, field) for field in ("task_revision", "manifest_revision", "phase_revision",
                "phase_digest", "board_fence", "job_fence", "phase")} | {"state": state,
            "children": [item.model_dump(mode="json") for item in entries]})
        proofs = [(cancel_checkpoint_id(parent.run_identity, attempt.attempt_id), updated_witness)]
        if entry.closure is None:
            proofs.insert(0, (cleanup_checkpoint_id(binding, entry.original_claim_fence), closure))
        _, values = _published_proofs(parent, proposed, (), tuple(proofs))
        await _cancel_cas_board(db, task, attempt, artifact, goal, state=state, first=False)
        await _cancel_cas_job(db, parent, {**values, "status": "cancelled" if state == "fully_cancelled" else "blocked",
            "failure_reason": "general_task_native_cancel_" + state}, child_entries=entries, child_rows=children)
        return await _cancel_result(jobs, db, parent.run_identity, task.task_id, attempt.attempt_id)


async def _cas_board(db, task, attempt, *, status, reason, owner, expiry, advance_fence):
    from src.workflows.job_runtime import DurableJobLeaseError, _utc_now
    now = _utc_now()
    changed = await db.execute(update(WorkBoardTask).where(
        WorkBoardTask.task_id == task.task_id, WorkBoardTask.task_revision == task.task_revision,
        WorkBoardTask.status == task.status, WorkBoardTask.owner_principal_id == task.owner_principal_id,
        WorkBoardTask.owner_session_id == task.owner_session_id,
    ).values(status=status, block_kind="needs_input" if reason else None,
        block_reason=reason, block_source_status=WorkBoardStatus.running.value if reason else None,
        task_revision=task.task_revision + 1, updated_at=now).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DurableJobLeaseError("general task original Board CAS changed")
    changed = await db.execute(update(WorkBoardAttempt).where(
        WorkBoardAttempt.attempt_id == attempt.attempt_id, WorkBoardAttempt.task_id == task.task_id,
        WorkBoardAttempt.workflow_run_id == attempt.workflow_run_id,
        WorkBoardAttempt.fencing_token == attempt.fencing_token,
        WorkBoardAttempt.ended_at.is_(None), WorkBoardAttempt.cancel_requested_at.is_(None),
        WorkBoardAttempt.lease_owner == attempt.lease_owner,
        WorkBoardAttempt.lease_expires_at == attempt.lease_expires_at,
    ).values(lease_owner=owner, lease_expires_at=expiry,
        fencing_token=attempt.fencing_token + int(advance_fence), outcome=reason,
        updated_at=now, heartbeat_at=now).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise DurableJobLeaseError("general task original attempt CAS changed")


async def replace_manifest(jobs, job_id, *, manifest, owner, fencing_token, expected_revision, staged_artifacts=(), service=None):
    from src.work_board.repository import _begin_sqlite_immediate
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
    from src.workflows.job_runtime import DurableJobLeaseError, _serialize, _utc_now, _as_utc
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        parent, task, attempt, _selected, _envelope = await _current(jobs, db, job_id, manifest=manifest)
        previous = read_manifest(parent)
        jobs._assert_lease(parent, owner=owner, fencing_token=fencing_token)
        if (parent.status != "running" or parent.revision != expected_revision
            or task.status != WorkBoardStatus.running
            or attempt.lease_owner is None or _as_utc(attempt.lease_expires_at) is None
            or _as_utc(attempt.lease_expires_at) <= _utc_now()
            or owner != attempt.lease_owner + ":" + attempt.attempt_id):
            raise DurableJobLeaseError("general task manifest requires the current joint running lease")
        _next_manifest(parent, previous, manifest, task=task, attempt=attempt)
        if manifest.phase not in {"native_ready", "assembly"}:
            raise DurableJobLeaseError("general task manifest transition requires its paired native owner")
        await verify_general_task_manifest(db, parent, task, attempt, manifest)
        if previous is not None:
            _assert_joint_manifest(parent, task, attempt, previous)
            if any(getattr(manifest, name) != getattr(previous, name) for name in (
                "admitted_invocation_ids", "step_ids", "step_receipt_artifact_ids",
                "step_receipt_digests", "step_receipt_schemas")):
                raise DurableJobLeaseError("native admission and receipts require their fixed paired writers")
        _validate_staged_refs(previous, manifest, staged_artifacts)
        capacity_parent = _reserve_native_capacity(parent, manifest, envelope=_envelope, service=service)
        await _ensure_future_cancel_capacity(db, capacity_parent, task, attempt, manifest)
        published, values = _published_values(capacity_parent, manifest, staged_artifacts)
        await _cas_parent(db, parent, values)
        return {"job": _serialize(await jobs._fetch(db, job_id)), "manifest": published.model_dump(mode="json")}


_ADMISSION_SEAL = object()


@dataclass(frozen=True)
class _ChildAdmission:
    jobs: object
    manifest: GeneralTaskCurrentManifestV1
    owner: str
    fencing_token: int
    expected_revision: int
    staged_input: object
    seal: object
    capacity_witness: object = None
    service: object = None

    async def __call__(self, db, child):
        from src.work_board.general_task_runtime_artifacts import (
            verify_general_task_manifest, read_native_artifact_reference, verify_staged_task_artifact,
            resolve_current_native_step_inputs,
        )
        from src.work_board.contracts import GeneralTaskArtifactRef
        from src.work_board.general_task import digest
        from src.workflows.job_runtime import DurableJobLeaseError, _as_utc
        if self.seal is not _ADMISSION_SEAL:
            raise DurableJobLeaseError("fixed native child admission proof required")
        binding = child_binding(child)
        parent, task, attempt, previous, envelope = await _current(self.jobs, db, binding.parent_job_id)
        self.jobs._assert_lease(parent, owner=self.owner, fencing_token=self.fencing_token)
        _assert_joint_manifest(parent, task, attempt, previous)
        if (parent.revision != self.expected_revision or parent.status != "running"
            or task.status != WorkBoardStatus.running or previous.phase not in {"native_ready", "assembly"}
            or attempt.lease_owner is None or attempt.lease_expires_at is None
            or _as_utc(attempt.lease_expires_at) <= _as_utc(child.started_at)
            or binding.invocation_id in previous.admitted_invocation_ids
            or self.manifest.phase != "native_wait"
            or self.manifest.admitted_invocation_ids != [*previous.admitted_invocation_ids, binding.invocation_id]
            or binding.creation_digest != previous.creation_digest
            or binding.parent_authority_digest != parent.authority_digest
            or binding.original_envelope_digest != previous.original_envelope_digest
            or binding.selected_grant_digest != previous.selected_grant_digest
            or binding.creation_job_fence != parent.fencing_token
            or binding.creation_board_fence != attempt.fencing_token
            or binding.plan_revision != previous.plan_revision or binding.plan_digest != previous.current_plan_digest
            or binding.phase_revision != self.manifest.phase_revision or binding.phase_digest != self.manifest.phase_digest
            or _as_utc(child.deadline_at) > previous.native_deadline_at
            or _as_utc(child.deadline_at) > _as_utc(parent.deadline_at)
            or _as_utc(child.deadline_at) <= _as_utc(child.started_at)):
            raise DurableJobLeaseError("general task native child admission binding changed")
        plan = envelope.plan
        if previous.plan_revision > 1:
            plan = read_native_artifact_reference(GeneralTaskArtifactRef(
                artifact_id=previous.current_plan_artifact_id,
                digest=previous.revision_artifact_digests[-1], schema_version="GeneralTaskPlanRevision.v1"),
                parent_job_id=parent.run_identity, creation_digest=previous.creation_digest).plan
        step = next((item for item in plan.steps if item.step_id == binding.step_id), None)
        descriptors = [item for item in envelope.descriptors if step is not None and item.tool_id == step.tool_id]
        if len(descriptors) != 1 or digest(descriptors[0].model_dump(mode="json")) != binding.descriptor_digest:
            raise DurableJobLeaseError("general task child descriptor is outside the original selected grant")
        if step.tool_id == "document_build":
            authority = json.loads(child.declared_authority_json)
            if (child.priority != task.priority or authority.get("document_build_priority") != task.priority
                    or authority.get("document_build_input_artifact_id") != task.input_artifact_id):
                raise DurableJobLeaseError("original document build priority/input binding changed")
        native_input, input_record = verify_staged_task_artifact(self.staged_input,
            parent_job_id=parent.run_identity, creation_digest=previous.creation_digest)
        resolved_inputs = await resolve_current_native_step_inputs(
            db, parent, task, attempt, previous, envelope, step)
        expected_inputs = {"step_id": binding.step_id, "tool_id": step.tool_id,
            "tool_input_digest": binding.input_digest, "descriptor_digest": binding.descriptor_digest,
            "typed_input_ref": "general-task-input:" + self.staged_input.reference.artifact_id,
            "typed_input_digest": self.staged_input.reference.digest}
        if (self.staged_input.reference.schema_version != "GeneralTaskToolInput.v1"
            or child.input_digest != digest(expected_inputs)
            or native_input.invocation_id != binding.invocation_id
            or native_input.tool_id != step.tool_id
            or native_input.descriptor_digest != binding.descriptor_digest
            or native_input.input_digest != binding.input_digest
            or digest(native_input.inputs) != binding.input_digest
            or native_input.inputs != resolved_inputs
            or digest(resolved_inputs) != binding.input_digest):
            raise DurableJobLeaseError("general task private native tool input binding changed")
        # Generic jobs retain only an input shape. This sealed owner admits
        # the closed six-key content-free private-artifact reference envelope.
        from src.workflows.job_runtime import _canonical
        child.arguments_json = _canonical(expected_inputs)
        # Any original admission freezes this step across every later revision.
        siblings = list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.parent_job_id == parent.run_identity))).scalars().all())
        if any(child_binding(item).step_id == binding.step_id for item in siblings):
            raise DurableJobLeaseError("general task admitted step cannot be repeated")
        if len(siblings) != len(previous.admitted_invocation_ids) or len(siblings) >= envelope.task_input.limits.max_steps:
            raise DurableJobLeaseError("general task original invocation allowance changed")
        proposed = self.manifest
        # Board's exact successor revision is part of the new wait manifest.
        view_task = type("TaskCounter", (), {"task_revision": task.task_revision + 1})()
        _next_manifest(parent, previous, proposed, task=view_task, attempt=attempt)
        _validate_staged_refs(previous, proposed, ())
        await verify_general_task_manifest(db, parent, task, attempt, proposed)
        capacity_parent = _reserve_native_capacity(parent, proposed, binding, envelope=envelope,
            service=self.service, capacity_witness=self.capacity_witness)
        await _ensure_future_cancel_capacity(db, capacity_parent, task, attempt, proposed, candidate=binding)
        published, values = _published_values(capacity_parent, proposed, ())
        await _cas_board(db, task, attempt, status=WorkBoardStatus.blocked,
            reason="general_task_native_wait", owner=None, expiry=None, advance_fence=False)
        await _cas_parent(db, parent, {**values, "status": "paused",
            "failure_reason": "general_task_native_wait", "lease_owner": None, "lease_expires_at": None})
        from src.workflows.job_runtime import _canonical, _utc_now
        child.artifact_receipts_json = _canonical([{**input_record, "recorded_at": _utc_now().isoformat()}])


def is_fixed_child_admission(value):
    return type(value) is _ChildAdmission and value.seal is _ADMISSION_SEAL


async def admit_child(jobs, spec, *, manifest, owner, fencing_token, expected_revision, staged_input, capacity_witness=None, service=None):
    from src.workflows.job_runtime import DurableJobLeaseError
    if (type(manifest) is not GeneralTaskCurrentManifestV1
        or spec.identity.job_kind != GENERAL_TASK_NATIVE_CHILD_KIND
        or spec.identity.capability_version != "1" or spec.identity.owner_kind != "user"
        or spec.max_attempts != 1 or spec.max_outstanding_jobs is not None
        or spec.parent_job_id != manifest.run_id or spec.parent_fencing_token != fencing_token):
        raise DurableJobLeaseError("fixed native child spec is required")
    proof = _ChildAdmission(jobs, manifest, owner, fencing_token, expected_revision, staged_input, _ADMISSION_SEAL,
        capacity_witness=capacity_witness, service=service)
    return await jobs.admit_job(spec, admission_authority_check=proof)


async def publish_step_receipt(jobs, parent_id, *, staged_artifact, child_id, owner,
    fencing_token, expected_parent_revision, repository_final_witness=None,
    repository_final_authority_check=None, repository_final_authority_scope=None):
    if repository_final_witness is not None:
        return await publish_repository_child_final(jobs, parent_id,
            staged_artifact=staged_artifact, child_id=child_id, owner=owner,
            fencing_token=fencing_token,
            expected_parent_revision=expected_parent_revision,
            repository_final_witness=repository_final_witness,
            repository_final_authority_check=repository_final_authority_check,
            repository_final_authority_scope=repository_final_authority_scope)
    from src.work_board.repository import _begin_sqlite_immediate
    from src.work_board.general_task_runtime_artifacts import verify_staged_task_artifact, compile_phase_digest
    from src.work_board.contracts import GeneralTaskStepReceiptV1
    from src.workflows.job_runtime import DurableJobLeaseError, _digest, _serialize, _verified_readback_exists
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        parent, task, attempt, previous, _envelope = await _current(jobs, db, parent_id)
        _assert_joint_manifest(parent, task, attempt, previous)
        child = await jobs._fetch(db, child_id)
        jobs._assert_lease(child, owner=owner, fencing_token=fencing_token)
        effective = await assert_general_task_child_phase_current(db, child)
        binding = child_binding(child)
        receipt, _record = verify_staged_task_artifact(staged_artifact,
            parent_job_id=parent_id, creation_digest=previous.creation_digest)
        if (type(receipt) is not GeneralTaskStepReceiptV1 or parent.revision != expected_parent_revision
            or parent.status != "paused" or previous.phase != "native_wait" or child.status != "running"
            or child.attempt_count != 1 or child.fencing_token <= 0
            or receipt.child_attempt_count != child.attempt_count or receipt.child_fence != child.fencing_token
            or receipt.child_job_id != child_id or receipt.invocation_id != child_id
            or receipt.task_id != task.task_id or receipt.attempt_id != attempt.attempt_id
            or receipt.step_id != binding.step_id or receipt.plan_revision != binding.plan_revision
            or receipt.input_digest != binding.input_digest or receipt.descriptor_digest != binding.descriptor_digest
            or receipt.selected_grant_digest != binding.selected_grant_digest
            or receipt.phase_digest != effective.phase_digest
            or receipt.approval_binding_digest != effective.approval_binding_digest):
            raise DurableJobLeaseError("general task receipt requires the actual original positive child claim")
        effects = json.loads(child.effect_receipts_json or "[]")
        if binding.step_id not in previous.step_ids and (receipt.status != "running"
            or receipt.contact_state != "not_contacted" or effects):
            raise DurableJobLeaseError("the first native receipt must bind a positive claim before contact")
        if receipt.status == "verified":
            if (receipt.contact_state != "settled" or receipt.effect_receipt_digest != _digest(effects)
                or not _verified_readback_exists(effects)):
                raise DurableJobLeaseError("general task verified receipt requires canonical native readback")
            artifacts = json.loads(child.artifact_receipts_json or "[]")
            for ref in receipt.artifact_refs:
                if not any(item.get("artifact_id") == ref.artifact_id and item.get("content_sha256") == ref.digest for item in artifacts):
                    raise DurableJobLeaseError("general task result artifact is not canonical child output")
        elif receipt.status not in {"running", "awaiting_approval", "failed", "blocked", "cancelled", "unknown"}:
            raise DurableJobLeaseError("admitted receipt cannot replace an actual positive claim")
        elif receipt.contact_state == "not_contacted" and effects:
            raise DurableJobLeaseError("general task cannot erase prior contact evidence")
        refs = dict(zip(previous.step_ids, zip(previous.step_receipt_artifact_ids,
            previous.step_receipt_digests, previous.step_receipt_schemas)))
        if binding.step_id in refs:
            old = _step_receipt(previous, binding.step_id)
            if old.status in {"verified", "unknown"} or old.contact_state == "unknown":
                raise DurableJobLeaseError("verified or unresolved native step evidence is frozen")
        refs[binding.step_id] = (staged_artifact.reference.artifact_id, staged_artifact.reference.digest, "StepReceipt.v1")
        steps = sorted(refs)
        proposed = previous.model_copy(update={"manifest_revision": previous.manifest_revision + 1,
            "step_ids": steps, "step_receipt_artifact_ids": [refs[key][0] for key in steps],
            "step_receipt_digests": [refs[key][1] for key in steps], "step_receipt_schemas": [refs[key][2] for key in steps]})
        _next_manifest(parent, previous, proposed, task=task, attempt=attempt)
        _validate_staged_refs(previous, proposed, (staged_artifact,))
        published, values = _published_values(parent, proposed, (staged_artifact,))
        await _cas_parent(db, parent, values)
        return {"job": _serialize(await jobs._fetch(db, parent_id)), "manifest": published.model_dump(mode="json")}


def _phase_successor(previous, *, phase, task_revision, job_fence, board_fence):
    from src.work_board.general_task_runtime_artifacts import compile_phase_digest
    value = previous.model_copy(update={"manifest_revision": previous.manifest_revision + 1,
        "phase_revision": previous.phase_revision + 1, "phase": phase,
        "task_revision": task_revision, "job_fence": job_fence, "board_fence": board_fence})
    return value.model_copy(update={"phase_digest": compile_phase_digest(value)})


async def pause_parent(jobs, parent_id, *, operator_owner, expected_task_revision, expected_revision, expected_manifest_revision):
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.job_runtime import DurableJobLeaseError, _serialize, _job_has_unsafe_effects, _verified_readback_exists
    if type(operator_owner) is not WorkBoardOwner:
        raise DurableJobLeaseError("current typed operator owner required")
    # Active specialists cannot promise quiescent, resumable pause. Their exact
    # original stop is the same authenticated held cancellation owner, tagged
    # as pause; every binding is rechecked inside that writer before fencing.
    async with jobs._session() as probe:
        from src.workflows.specialist_delegation import read_reservation
        active_specialists = any(read_reservation(row) is not None and row.status not in {"succeeded","degraded"}
            for row in (await probe.execute(select(WorkflowRunState).where(
                WorkflowRunState.parent_job_id == parent_id))).scalars())
    if active_specialists:
        return await cancel_native_parent(jobs,parent_id,operator_owner=operator_owner,
            expected_task_revision=expected_task_revision,stop_action="pause",expected_revision=expected_revision,
            expected_manifest_revision=expected_manifest_revision)
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        parent, task, attempt, previous, _envelope = await _current(jobs, db, parent_id)
        _assert_joint_manifest(parent, task, attempt, previous)
        if (operator_owner.principal_id != task.owner_principal_id or operator_owner.session_id != task.owner_session_id
            or task.task_revision != expected_task_revision or parent.revision != expected_revision
            or previous.manifest_revision != expected_manifest_revision
            or previous.phase not in {"native_ready", "assembly", "native_wait"}
            or parent.status not in {"running", "paused"}
            or task.status not in {WorkBoardStatus.running, WorkBoardStatus.blocked}):
            raise DurableJobLeaseError("general task exact operator pause binding changed")
        children = list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.parent_job_id == parent_id))).scalars().all())
        if sorted(item.run_identity for item in children) != sorted(previous.admitted_invocation_ids):
            raise DurableJobLeaseError("general task original admitted child set changed")
        for child in children:
            binding = child_binding(child)
            effects = json.loads(child.effect_receipts_json or "[]")
            if (binding.creation_digest != previous.creation_digest
                or child.lease_owner or child.lease_expires_at
                or child.status not in {"succeeded", "degraded", "cancelled"}
                or _job_has_unsafe_effects(effects)):
                raise DurableJobLeaseError("native children must close under native_wait before paired pause; unknown remains in wait")
            if child.status in {"succeeded", "degraded"}:
                receipt = _step_receipt(previous, binding.step_id)
                if (receipt.status != "verified" or receipt.contact_state != "settled"
                    or receipt.child_job_id != child.run_identity
                    or receipt.child_fence != child.fencing_token
                    or receipt.child_attempt_count != child.attempt_count
                    or not _verified_readback_exists(effects)):
                    raise DurableJobLeaseError("native child closure requires original verified readback")
            if child.attempt_count > 0:
                assert_child_closed(parent, child, _step_receipt(previous, binding.step_id))
        proposed = _phase_successor(previous, phase="operator_paused", task_revision=task.task_revision + 1,
            job_fence=parent.fencing_token + 1, board_fence=attempt.fencing_token + 1)
        published, values = _published_values(parent, proposed, ())
        await _cas_board(db, task, attempt, status=WorkBoardStatus.blocked,
            reason="general_task_operator_paused", owner=None, expiry=None, advance_fence=True)
        await _cas_parent(db, parent, {**values, "status": "paused",
            "failure_reason": "general_task_operator_paused", "lease_owner": None, "lease_expires_at": None,
            "fencing_token": parent.fencing_token + 1})
        return {"job": _serialize(await jobs._fetch(db, parent_id)), "manifest": published.model_dump(mode="json")}


async def revise_operator_paused_parent(jobs, parent_id, *, operator_owner, request, service):
    """Edit remaining Plan data without reclaiming a paused original attempt."""
    from types import SimpleNamespace
    from src.work_board.contracts import PlanRevisionRequest, WorkBoardOwner
    from src.work_board.general_task import GeneralTaskService, digest
    from src.work_board.general_task_native import compile_paused_plan_revision
    from src.work_board.general_task_runtime_artifacts import compile_phase_digest, read_current_native_outputs
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.job_runtime import (
        DurableJobLeaseError, _serialize, _utc_now, _append_goal_fence_condition,
        _job_has_unsafe_effects, _verified_readback_exists,
    )
    if (type(operator_owner) is not WorkBoardOwner or type(request) is not PlanRevisionRequest
        or type(service) is not GeneralTaskService or not service.started):
        raise DurableJobLeaseError("fixed paused revision requires the current typed operator and service")
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        parent, task, attempt, previous, envelope = await _current(jobs, db, parent_id)
        _assert_joint_manifest(parent, task, attempt, previous)
        if (task.owner_principal_id != operator_owner.principal_id
            or task.owner_session_id != operator_owner.session_id
            or task.task_revision != request.expected_revision
            or previous.phase != "operator_paused" or parent.status != "paused"
            or parent.failure_reason != "general_task_operator_paused"
            or task.status != WorkBoardStatus.blocked or task.block_reason != "general_task_operator_paused"
            or parent.lease_owner or parent.lease_expires_at or attempt.lease_owner or attempt.lease_expires_at):
            raise DurableJobLeaseError("exact original operator-paused revision binding changed")
        source = await db.get(WorkBoardInputArtifact, task.input_artifact_id, populate_existing=True)
        children = list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.parent_job_id == parent_id))).scalars().all())
        if {row.run_identity for row in children} != set(previous.admitted_invocation_ids):
            raise DurableJobLeaseError("original paused admitted child set changed")
        for child in children:
            binding = child_binding(child)
            effects = json.loads(child.effect_receipts_json or "[]")
            if (binding.creation_digest != previous.creation_digest or child.lease_owner or child.lease_expires_at
                or child.status not in {"succeeded", "degraded", "cancelled"} or _job_has_unsafe_effects(effects)):
                raise DurableJobLeaseError("paused revision requires known-safe original callback closure")
            if child.status in {"succeeded", "degraded"}:
                receipt = _step_receipt(previous, binding.step_id)
                if (receipt.status != "verified" or receipt.contact_state != "settled"
                    or receipt.child_job_id != child.run_identity or receipt.child_fence != child.fencing_token
                    or receipt.child_attempt_count != child.attempt_count or not _verified_readback_exists(effects)):
                    raise DurableJobLeaseError("paused revision requires original settled child readback")
            if child.attempt_count > 0:
                assert_child_closed(parent, child, _step_receipt(previous, binding.step_id))
        await read_current_native_outputs(db, parent, task, attempt, previous, envelope,
            [child_binding(child).step_id for child in children if child.status in {"succeeded", "degraded"}])
        plan, staged = await compile_paused_plan_revision(service, db, parent, task,
            attempt, envelope, previous, request)
        if staged is None:
            return {"job": _serialize(parent), "manifest": previous.model_dump(mode="json"),
                "idempotent_replay": True}
        proposed = previous.model_copy(update={"task_revision": task.task_revision + 1,
            "manifest_revision": previous.manifest_revision + 1, "phase_revision": previous.phase_revision + 1,
            "plan_revision": plan.revision, "current_plan_artifact_id": staged.reference.artifact_id,
            "current_plan_digest": digest(plan.model_dump(mode="json")),
            "revision_numbers": [*previous.revision_numbers, plan.revision],
            "revision_artifact_ids": [*previous.revision_artifact_ids, staged.reference.artifact_id],
            "revision_artifact_digests": [*previous.revision_artifact_digests, staged.reference.digest],
            "revision_artifact_schemas": [*previous.revision_artifact_schemas, "GeneralTaskPlanRevision.v1"]})
        proposed = proposed.model_copy(update={"phase_digest": compile_phase_digest(proposed)})
        _next_manifest(parent, previous, proposed,
            task=SimpleNamespace(task_revision=task.task_revision + 1), attempt=attempt)
        _validate_staged_refs(previous, proposed, (staged,))
        await _ensure_future_cancel_capacity(db, parent, task, attempt, proposed)
        published, values = _published_values(parent, proposed, (staged,))
        now = _utc_now()
        changed = await db.execute(update(WorkBoardTask).where(
            WorkBoardTask.task_id == task.task_id, WorkBoardTask.task_revision == task.task_revision,
            WorkBoardTask.owner_principal_id == operator_owner.principal_id,
            WorkBoardTask.owner_session_id == operator_owner.session_id,
            WorkBoardTask.capability_id == "agent.task.v1", WorkBoardTask.status == WorkBoardStatus.blocked,
            WorkBoardTask.block_reason == "general_task_operator_paused",
            WorkBoardTask.input_artifact_id == task.input_artifact_id,
            WorkBoardTask.typed_input_ref == task.typed_input_ref,
            WorkBoardTask.typed_input_digest == task.typed_input_digest,
        ).values(task_revision=task.task_revision + 1, updated_at=now)
            .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise DurableJobLeaseError("paused revision original Task CAS changed")
        conditions = [WorkflowRunState.run_identity == parent_id, WorkflowRunState.revision == parent.revision,
            WorkflowRunState.status == "paused", WorkflowRunState.failure_reason == "general_task_operator_paused",
            WorkflowRunState.fencing_token == parent.fencing_token,
            WorkflowRunState.lease_owner.is_(None), WorkflowRunState.lease_expires_at.is_(None),
            WorkflowRunState.deadline_at == parent.deadline_at, WorkflowRunState.deadline_at > now,
            WorkflowRunState.declared_authority_json == parent.declared_authority_json,
            WorkflowRunState.authority_digest == parent.authority_digest,
            WorkflowRunState.input_digest == parent.input_digest,
            WorkflowRunState.checkpoint_receipts_json == parent.checkpoint_receipts_json,
            WorkflowRunState.artifact_receipts_json == parent.artifact_receipts_json,
            WorkflowRunState.effect_receipts_json == parent.effect_receipts_json,
            select(WorkBoardAttempt.attempt_id).where(WorkBoardAttempt.attempt_id == attempt.attempt_id,
                WorkBoardAttempt.task_id == task.task_id, WorkBoardAttempt.workflow_run_id == parent_id,
                WorkBoardAttempt.fencing_token == attempt.fencing_token,
                WorkBoardAttempt.ended_at.is_(None), WorkBoardAttempt.cancel_requested_at.is_(None),
                WorkBoardAttempt.lease_owner.is_(None), WorkBoardAttempt.lease_expires_at.is_(None)).exists(),
            select(OperatorSession.id).where(OperatorSession.id == previous.original_root_id,
                OperatorSession.principal_id == previous.owner_principal_id,
                OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
                OperatorSession.is_bearer_tombstone.is_(False), OperatorSession.idle_expires_at > now,
                OperatorSession.absolute_expires_at > now).exists(),
            select(WorkBoardInputArtifact.artifact_id).where(
                WorkBoardInputArtifact.artifact_id == task.input_artifact_id,
                WorkBoardInputArtifact.revision == source.revision,
                WorkBoardInputArtifact.state == source.state,
                WorkBoardInputArtifact.metadata_digest == source.metadata_digest,
                WorkBoardInputArtifact.typed_input_ref == task.typed_input_ref,
                WorkBoardInputArtifact.payload_sha256 == task.typed_input_digest,
                WorkBoardInputArtifact.bound_task_id == task.task_id,
                WorkBoardInputArtifact.owner_principal_id == operator_owner.principal_id,
                WorkBoardInputArtifact.owner_session_id == operator_owner.session_id,
                WorkBoardInputArtifact.goal_id == task.goal_id,
                WorkBoardInputArtifact.goal_revision == task.goal_revision,
                WorkBoardInputArtifact.capability_id == "agent.task.v1",
                WorkBoardInputArtifact.capability_version == "1",
                WorkBoardInputArtifact.expires_at > now).exists(),
        ]
        _append_goal_fence_condition(conditions, parent)
        sibling = aliased(WorkflowRunState)
        conditions.append(select(func.count(sibling.id)).where(sibling.parent_job_id == parent_id)
            .scalar_subquery() == len(children))
        for child in children:
            conditions.append(select(sibling.id).where(sibling.run_identity == child.run_identity,
                sibling.parent_job_id == parent_id, sibling.revision == child.revision,
                sibling.fencing_token == child.fencing_token, sibling.status == child.status,
                sibling.lease_owner.is_(None), sibling.lease_expires_at.is_(None),
                sibling.checkpoint_receipts_json == child.checkpoint_receipts_json,
                sibling.artifact_receipts_json == child.artifact_receipts_json,
                sibling.effect_receipts_json == child.effect_receipts_json).exists())
        changed = await db.execute(update(WorkflowRunState).where(*conditions).values(
            **values, revision=parent.revision + 1, updated_at=now)
            .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise DurableJobLeaseError("paused revision exact original journal CAS changed")
        return {"job": _serialize(await jobs._fetch(db, parent_id)), "manifest": published.model_dump(mode="json")}


async def resume_parent(jobs, parent_id, *, owner, expected_revision, expected_manifest_revision):
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _job_has_unsafe_effects, _serialize, _utc_now, _verified_readback_exists
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        parent, task, attempt, previous, _envelope = await _current(jobs, db, parent_id)
        _assert_joint_manifest(parent, task, attempt, previous)
        expected_reason = {"native_wait": "general_task_native_wait", "operator_paused": "general_task_operator_paused"}.get(previous.phase)
        if (not owner or parent.revision != expected_revision or previous.manifest_revision != expected_manifest_revision
            or expected_reason is None or parent.status != "paused" or parent.failure_reason != expected_reason
            or task.status != WorkBoardStatus.blocked or task.block_reason != expected_reason
            or parent.lease_owner or parent.lease_expires_at or attempt.lease_owner or attempt.lease_expires_at):
            raise DurableJobLeaseError("general task exact native wait changed")
        children = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == parent_id))).scalars().all())
        if sorted(item.run_identity for item in children) != sorted(previous.admitted_invocation_ids):
            raise DurableJobLeaseError("general task original admitted child set changed")
        for child in children:
            binding = child_binding(child)
            effects = json.loads(child.effect_receipts_json or "[]")
            if (binding.creation_digest != previous.creation_digest or child.lease_owner or child.lease_expires_at
                or child.status not in {"succeeded", "degraded", "failed", "blocked", "cancelled"}
                or _job_has_unsafe_effects(effects)):
                raise DurableJobLeaseError("general task admitted child requires exact recovery; never replay")
            if child.status in {"succeeded", "degraded"}:
                receipt = _step_receipt(previous, binding.step_id)
                if (receipt.status != "verified" or receipt.contact_state != "settled"
                    or receipt.child_job_id != child.run_identity or receipt.child_fence != child.fencing_token
                    or receipt.child_attempt_count != child.attempt_count or not _verified_readback_exists(effects)):
                    raise DurableJobLeaseError("general task successful child lacks original verified readback")
            if child.attempt_count > 0:
                receipt = _step_receipt(previous,binding.step_id)
                assert_child_closed(parent, child, receipt)
                from src.workflows.specialist_lifecycle import read_fact,CLOSURE_KEY,SpecialistDelegationClosureV1
                if read_fact(child,CLOSURE_KEY,SpecialistDelegationClosureV1) is not None:
                    from src.workflows.specialist_result import verify_full_delegation_result
                    await verify_full_delegation_result(db,parent,child,receipt)
        expiry = min(previous.original_deadline_at, _as_utc(parent.deadline_at), _utc_now() + timedelta(seconds=30))
        proposed = _phase_successor(previous, phase="assembly", task_revision=task.task_revision + 1,
            job_fence=parent.fencing_token + 1, board_fence=attempt.fencing_token + 1)
        published, values = _published_values(parent, proposed, ())
        # Board uses the dispatcher owner; durable Root uses its exact
        # attempt-qualified owner, preserving existing wrapper identity.
        runtime_owner = owner if owner.endswith(":" + attempt.attempt_id) else owner + ":" + attempt.attempt_id
        board_owner = runtime_owner[:-(len(attempt.attempt_id) + 1)]
        await _cas_board(db, task, attempt, status=WorkBoardStatus.running,
            reason=None, owner=board_owner, expiry=expiry, advance_fence=True)
        await _cas_parent(db, parent, {**values, "status": "running", "failure_reason": None,
            "lease_owner": runtime_owner, "lease_expires_at": expiry,
            "fencing_token": parent.fencing_token + 1, "heartbeat_at": _utc_now()})
        return {"job": _serialize(await jobs._fetch(db, parent_id)), "manifest": published.model_dump(mode="json")}
