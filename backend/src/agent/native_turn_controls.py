"""App-owned original native-turn resources; locators never reconstruct authority."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json

from src.agent.turn_execution import NativeTurnBlocked, _digest

MAX_RESOURCES = 32
CANCEL_METHODS = frozenset({"conversation.cancel", "agent-loop.cancelTurn"})
CANCEL_ID = "conversation:cancel"
CLOSURE_ID = "conversation:callback-closure"
_FACTORY_SEAL = object()


def _now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


@dataclass(eq=False)
class _TurnResource:
    owner: object
    admission: object
    execution: object = None
    worker: object = None
    purpose: object = None
    post_cancel_digest: str | None = None
    post_closure_digest: str | None = None
    observing: bool = False
    published: bool = False
    cancel_published: bool = False
    registration: object = None


@dataclass(frozen=True, eq=False)
class _SourceRegistration:
    execution: object
    claim: object
    host: object
    scope: object
    admission: object
    boot: str


@dataclass(frozen=True, eq=False)
class NativeTurnCancelPurpose:
    resource: _TurnResource
    method: str
    payload_json: str
    deadline_at: int
    claim_digest: str
    _seal: object = field(repr=False)


class NativeTurnResourceOwner:
    """One app's bounded source resources, including unresolved original debt."""
    def __init__(self):
        self.admitting = True
        self._entries = {}

    def _reap(self):
        for key, resource in list(self._entries.items()):
            if (resource.post_closure_digest is not None and resource.purpose is not None
                    and resource.purpose.deadline_at <= _now_ms()):
                del self._entries[key]

    def reserve(self, admission):
        from src.agent.turn_execution import NativeTurnAdmission
        if type(admission) is not NativeTurnAdmission or not self.admitting:
            raise NativeTurnBlocked("native_turn_resource_owner_unavailable")
        self._reap()
        prior = self._entries.get(admission.job_id)
        if prior is not None:
            incoming, original = admission.ingress.model_dump(mode="json"), prior.admission.ingress.model_dump(mode="json")
            incoming.pop("received_at")
            original.pop("received_at")
            if original != incoming:
                raise NativeTurnBlocked("native_turn_resource_identity_occupied")
            return prior
        if len(self._entries) >= MAX_RESOURCES:
            raise NativeTurnBlocked("native_turn_resource_capacity")
        resource = _TurnResource(self, admission)
        self._entries[admission.job_id] = resource
        return resource

    def release_unclaimed(self, resource):
        self._check(resource)
        if resource.execution is not None:
            raise NativeTurnBlocked("native_turn_original_resource_retained")
        del self._entries[resource.admission.job_id]

    def _check(self, resource):
        if (type(resource) is not _TurnResource or resource.owner is not self
                or self._entries.get(resource.admission.job_id) is not resource):
            raise NativeTurnBlocked("native_turn_original_resource_missing")
        if resource.execution is not None:
            registered = resource.registration
            execution = resource.execution
            if (type(registered) is not _SourceRegistration or registered.execution is not execution
                    or registered.claim is not execution.claim or registered.host is not execution.host
                    or registered.scope is not execution.scope or registered.admission is not execution.admission
                    or registered.admission is not resource.admission
                    or registered.boot != execution.scope.host_boot_nonce):
                raise NativeTurnBlocked("native_turn_original_factory_changed")

    def register(self, resource, execution, *, seal):
        from src.agent.turn_execution import NativeTurnExecution
        self._check(resource)
        if (seal is not _FACTORY_SEAL or type(execution) is not NativeTurnExecution
                or resource.execution is not None or execution.admission is not resource.admission
                or execution.claim._host is not execution.host
                or execution.claim.job["job_id"] != resource.admission.job_id
                or execution.scope.host_boot_nonce != execution.host.boot_nonce):
            raise NativeTurnBlocked("native_turn_original_factory_changed")
        resource.execution = execution
        resource.registration = _SourceRegistration(execution, execution.claim, execution.host,
            execution.scope, execution.admission, execution.scope.host_boot_nonce)
        execution._native_resource = resource

    def attach_worker(self, execution, worker):
        resource = getattr(execution, "_native_resource", None)
        self._check(resource)
        if (resource.execution is not execution or execution.worker is not worker
                or resource.worker is not None or not isinstance(worker, asyncio.Future)):
            raise NativeTurnBlocked("native_turn_original_worker_changed")
        resource.worker = worker
        worker.add_done_callback(lambda done: self.schedule_observation(resource))

    def schedule_observation(self, resource):
        self._check(resource)
        if resource.purpose is None and not resource.published:
            return
        if resource.purpose is not None and not resource.cancel_published:
            return
        if not resource.observing:
            task = asyncio.create_task(self.observe(resource))
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)

    async def observe(self, resource):
        self._check(resource)
        if resource.observing or resource.worker is None or not resource.worker.done() or resource.worker.cancelled():
            return
        resource.observing = True
        try:
            from src.workflows.job_runtime import durable_job_repository as jobs
            async with jobs._writer_session() as db:
                await observe_native_turn_closure(jobs, db, resource)
        except Exception:
            # Observation grants no replay. Missing proof retains the original obligation.
            return
        finally:
            resource.observing = False

    def locate(self, turn_ref, operator):
        self._reap()
        resource = self._entries.get(turn_ref)
        self._check(resource)
        ingress = resource.admission.ingress
        if (resource.execution is None or ingress.principal_id != operator.principal.principal_id
                or ingress.operator_session_id != operator.session_id):
            raise NativeTurnBlocked("native_turn_original_operator_changed")
        return resource

    async def control(self, turn_ref, operator, method, payload):
        from src.runtime_plugins.contracts import validate_request
        resource = self.locate(turn_ref, operator)
        payload = validate_request(method, payload)
        execution = resource.execution
        if method == "conversation.read":
            if payload["conversation_ref"] != resource.admission.ingress.conversation_id:
                raise NativeTurnBlocked("native_turn_conversation_reference_changed")
            return await execution.forward(method, payload)
        if method not in CANCEL_METHODS or payload["turn_ref"] != turn_ref:
            raise NativeTurnBlocked("native_turn_control_method_changed")
        from src.workflows.job_runtime import durable_job_repository as jobs
        from src.agent.turn_execution import validate_native_turn_owner
        async with jobs._writer_session() as db:
            await validate_native_turn_owner(db, resource.admission)
        if resource.purpose is None:
            resource.purpose = NativeTurnCancelPurpose(resource, method,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                min(_now_ms() + 5000, execution.scope.deadline_at),
                _digest(dict(execution.scope.witness)), _FACTORY_SEAL)
        purpose = resource.purpose
        validate_forward_cancel_purpose(purpose, execution.host, execution.scope, method, payload)
        result = await execution.host.request_service(method, payload,
            original_scope=execution.scope, native_cancel_purpose=purpose)
        resource.cancel_published = result.get("status") == "succeeded" and resource.post_cancel_digest is not None
        await self.observe(resource)
        return result

    async def shutdown(self):
        self.admitting = False
        for resource in self._entries.values():
            if resource.execution is not None:
                resource.execution.close_transport()
        tasks = [asyncio.create_task(self.observe(resource)) for resource in list(self._entries.values())]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)


def validate_forward_cancel_purpose(purpose, host, scope, method, payload):
    if type(purpose) is not NativeTurnCancelPurpose or purpose._seal is not _FACTORY_SEAL:
        raise NativeTurnBlocked("native_turn_cancel_purpose_missing")
    resource = purpose.resource
    resource.owner._check(resource)
    execution = resource.execution
    if (resource.purpose is not purpose or execution is None or execution.host is not host
            or execution.scope is not scope or execution.claim._host is not host
            or method not in CANCEL_METHODS or purpose.method != method
            or purpose.payload_json != json.dumps(payload, sort_keys=True, separators=(",", ":"))
            or purpose.claim_digest != _digest(dict(scope.witness))
            or purpose.deadline_at <= _now_ms() or purpose.deadline_at > scope.deadline_at
            or host.boot_nonce != scope.host_boot_nonce or not host.admitting
            or host.reviewed is not execution.admission.reviewed_composition):
        raise NativeTurnBlocked("native_turn_cancel_purpose_changed")
    return purpose.deadline_at


def validate_called_cancel_purpose(purpose, host, called_scope, payload):
    deadline = validate_forward_cancel_purpose(purpose, host, called_scope.original, called_scope.method, payload)
    if called_scope.deadline_at > deadline or called_scope.host_boot_nonce != host.boot_nonce:
        raise NativeTurnBlocked("native_turn_cancel_frame_changed")


async def dispatch_native_turn_cancel(dispatcher, db, frame, payload, call_scope, purpose):
    return await _cancel_in_writer(dispatcher, db, frame, payload, call_scope, purpose)


async def _cancel_in_writer(dispatcher, db, frame, payload, call_scope, purpose):
    from src.runtime_plugins.ownership import begin_native_writer, validate_invocation
    from src.runtime_plugins.dispatch import _witness, _ms
    from src.runtime_plugins.contracts import succeeded
    from src.agent.turn_execution import validate_native_turn_claim
    resource = purpose.resource
    execution = resource.execution
    validate_called_cancel_purpose(purpose, execution.host, call_scope, payload)
    await begin_native_writer(db, owner="finite_service")
    run = await dispatcher.jobs._fetch(db, resource.admission.job_id)
    await validate_native_turn_claim(db, run, resource.admission)
    await validate_invocation(db, execution.scope.binding)
    witness = _witness(run, execution.scope)
    if dict(execution.scope.witness) != witness:
        raise NativeTurnBlocked("native_turn_original_claim_changed")
    history = json.loads(run.checkpoint_receipts_json)
    original = _selected(history, CANCEL_ID)
    if original["payload"].get("producer") == "native-turn-cancel.v1":
        cancel = validate_cancel_payload(original["payload"])
        _assert_original(run, resource, cancel)
        if (cancel["control_method"] != purpose.method or cancel["original_revision"] != payload["expected_revision"]
                or cancel["action_deadline_at"] != purpose.deadline_at
                or _digest(history) not in {resource.post_cancel_digest, resource.post_closure_digest}):
            raise NativeTurnBlocked("native_turn_cancel_replay_changed")
        closure = _selected(history, CLOSURE_ID)
        if closure["payload"].get("producer") == "native-turn-cancel-closure.v1":
            value = validate_closure_payload(closure["payload"])
            if (run.status != "cancelled" or run.revision != value["terminal_revision"]
                    or run.fencing_token != value["terminal_fence"] or value["cancel_digest"] != _digest(cancel)
                    or resource.post_closure_digest != _digest(history)):
                raise NativeTurnBlocked("native_turn_cancel_closure_changed")
        elif (run.revision != cancel["current_revision"] or run.fencing_token != cancel["current_fence"]
                or run.status != cancel["state"]):
            raise NativeTurnBlocked("native_turn_cancel_current_changed")
        if run.lease_owner is not None or run.lease_expires_at is not None:
            raise NativeTurnBlocked("native_turn_cancel_lease_changed")
        return succeeded(purpose.method, {"job_ref": run.run_identity, "revision": run.revision, "state": run.status})
    if payload["expected_revision"] != run.revision:
        raise NativeTurnBlocked("native_turn_cancel_revision_changed")
    phase = "running"
    if run.status == "running":
        await dispatcher.jobs._validate_turn_completion_in_session(db, run,
            original_claim=execution.claim, authority_check=execution.authority_check,
            _family_phase="append")
    elif run.status in {"paused", "awaiting_approval"}:
        phase = "controlled"
        worker = resource.worker
        if worker is None or not worker.done() or worker.cancelled() or execution.worker is not worker:
            raise NativeTurnBlocked("native_turn_controlled_producer_missing")
        exception = worker.exception() or worker.result()
        execution.validate_completed_exception(exception)
        from src.agent.native_turn_family import validate_original_cleanup
        from src.agent.controlled_origin import validate_controlled_cleanup_origin
        cleanup = validate_original_cleanup(execution, worker)
        origin = validate_controlled_cleanup_origin(exception, cleanup_witness=cleanup)
        outcome = _selected(history, "conversation:controlled-outcome")["payload"]
        expected = "clarification_required" if origin.kind == "clarification" else "approval_required"
        if outcome.get("outcome") != expected or outcome.get("no_learning") is not True:
            raise NativeTurnBlocked("native_turn_controlled_publication_changed")
        if origin.kind == "approval":
            if origin.approval_snapshot is None or outcome.get("approval_ref") != origin.approval_snapshot.approval_id:
                raise NativeTurnBlocked("native_turn_controlled_publication_changed")
        else:
            from src.db.models import Message
            message = await db.get(Message, outcome.get("message_ref"), populate_existing=True)
            if (message is None or message.role != "assistant" or message.session_id != run.conversation_id
                    or message.owner_principal_id != run.owner_principal_id
                    or message.operator_session_id != run.operator_session_id
                    or message.conversation_id != run.conversation_id):
                raise NativeTurnBlocked("native_turn_controlled_publication_changed")
    else:
        raise NativeTurnBlocked("native_turn_cancel_phase_unsupported")
    state, _ = await dispatcher.jobs.native_turn_family_recovery_state_in_session(db, run)
    family = _selected(history, "conversation:operation-family")["payload"]
    original_revision, original_fence = run.revision, run.fencing_token
    value = {"schema_version": 1, "producer": "native-turn-cancel.v1", "job_ref": run.run_identity,
        "claim_ref": witness["claim_ref"], "owner_principal_id": run.owner_principal_id,
        "operator_session_id": run.operator_session_id, "conversation_ref": run.conversation_id,
        "origin_method": witness["origin_method"], "control_method": purpose.method,
        "original_claim_digest": purpose.claim_digest, "input_digest": run.input_digest,
        "authority_digest": run.authority_digest, "run_fingerprint": run.run_fingerprint,
        "binding_digest": witness["composition_binding_digest"], "package_digest": witness["package_digest"],
        "composition_digest": witness["host_composition_digest"], "method_manifest_digest": witness["method_manifest_digest"],
        "host_boot_nonce": witness["host_boot_nonce"], "original_deadline_at": _ms(run.deadline_at),
        "action_deadline_at": purpose.deadline_at, "original_revision": original_revision,
        "original_fence": original_fence, "attempt_count": 1, "current_revision": original_revision + 1,
        "current_fence": original_fence + 1, "state": state, "phase": phase,
        "effect_digest": _digest(json.loads(run.effect_receipts_json)),
        "artifact_digest": _digest(json.loads(run.artifact_receipts_json)),
        "checkpoint_digest": _digest([item for item in history if item["checkpoint_id"] not in {CANCEL_ID, CLOSURE_ID}]),
        "family_digest": _digest(family), "no_learning": True}
    validate_cancel_payload(value)
    receipt = _receipt(CANCEL_ID, value)
    replacement = [receipt if item["checkpoint_id"] == CANCEL_ID else item for item in history]
    preflight_control_journal(replacement)
    # Signal before the SQL CAS. Failure never restores execution admission.
    execution.close_transport()
    await _cas_control(db, run, resource, replacement, {"status": state, "revision": original_revision + 1,
        "fencing_token": original_fence + 1, "lease_owner": None, "lease_expires_at": None,
        "failure_reason": "native_turn_cancel_pending"}, (receipt,))
    resource.post_cancel_digest = _digest(replacement)
    return succeeded(purpose.method, {"job_ref": run.run_identity, "revision": original_revision + 1, "state": state})


async def observe_native_turn_closure(jobs, db, resource):
    run = await jobs._fetch(db, resource.admission.job_id)
    if resource.purpose is None:
        if run.status in {"succeeded", "failed"}:
            await jobs.assert_native_turn_family_terminal_in_session(db, run, native_execution=resource.execution)
            del resource.owner._entries[resource.admission.job_id]
        return
    history = json.loads(run.checkpoint_receipts_json)
    cancel = validate_cancel_payload(_selected(history, CANCEL_ID)["payload"])
    _assert_original(run, resource, cancel)
    existing = _selected(history, CLOSURE_ID)
    if existing["payload"].get("producer") == "native-turn-cancel-closure.v1":
        return
    if (run.revision != cancel["current_revision"] or run.fencing_token != cancel["current_fence"]
            or run.status != cancel["state"] or run.lease_owner is not None or run.lease_expires_at is not None
            or _digest(history) != resource.post_cancel_digest):
        raise NativeTurnBlocked("native_turn_cancel_current_changed")
    from src.agent.native_turn_family import assert_cancelled_family_owner_readback
    family = _selected(history, "conversation:operation-family")["payload"]
    readback = await assert_cancelled_family_owner_readback(db, run,
        native_execution=resource.execution, worker=resource.worker)
    from src.db.models import InferenceCostReservation
    from src.workflows.inference_accounting import _operation_payload
    owner_readbacks = []
    for operation in family["operations"]:
        reservation = await db.get(InferenceCostReservation, operation["operation_id"], populate_existing=True)
        owner_run = await jobs._fetch(db, operation["job_id"])
        owner_readbacks.append({"reservation": _operation_payload(reservation), "state": owner_run.status,
            "effects": json.loads(owner_run.effect_receipts_json), "artifacts": json.loads(owner_run.artifact_receipts_json)})
    if (_digest(family) != cancel["family_digest"]
            or _digest(json.loads(run.effect_receipts_json)) != cancel["effect_digest"]
            or _digest(json.loads(run.artifact_receipts_json)) != cancel["artifact_digest"]):
        raise NativeTurnBlocked("native_turn_cancel_original_readbacks_changed")
    outcome = "raised" if resource.worker.exception() is not None else "returned"
    value = {"schema_version": 1, "producer": "native-turn-cancel-closure.v1",
        "job_ref": run.run_identity, "claim_ref": cancel["claim_ref"],
        "original_claim_digest": cancel["original_claim_digest"], "binding_digest": cancel["binding_digest"],
        "cancel_digest": _digest(cancel), "family_digest": cancel["family_digest"], "effect_digest": cancel["effect_digest"],
        "accounting_readback_digest": _digest(owner_readbacks), "original_fence": cancel["original_fence"],
        "cancel_fence": cancel["current_fence"], "terminal_revision": run.revision + 1,
        "terminal_fence": run.fencing_token, "original_deadline_at": cancel["original_deadline_at"],
        "outcome": outcome, "physically_closed": True, "no_learning": True}
    validate_closure_payload(value)
    receipt = _receipt(CLOSURE_ID, value)
    replacement = [receipt if item["checkpoint_id"] == CLOSURE_ID else item for item in history]
    await _cas_control(db, run, resource, replacement, {"status": "cancelled", "revision": run.revision + 1,
        "finished_at": datetime.now(timezone.utc), "failure_reason": "native_turn_cancelled_physically_closed"}, (receipt,))
    resource.post_closure_digest = _digest(replacement)


def _selected(history, identifier):
    found = [item for item in history if type(item) is dict and item.get("checkpoint_id") == identifier]
    if len(found) != 1 or found[0].get("safe") is not True or found[0].get("state_digest") != _digest(found[0].get("payload")):
        raise NativeTurnBlocked("native_turn_control_checkpoint_changed")
    return found[0]


def _receipt(identifier, value):
    result = {"checkpoint_id": identifier, "state_digest": _digest(value), "safe": True, "payload": value}
    if len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()) > 8192 or len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode()) > 8704:
        raise NativeTurnBlocked("native_turn_control_witness_byte_cap")
    return result


def validate_cancel_payload(value):
    from src.runtime_plugins.protocol import closed, integer
    from src.runtime_plugins.contracts import ref, sha
    refs = {"job_ref", "claim_ref", "owner_principal_id", "operator_session_id", "conversation_ref", "origin_method", "control_method"}
    digests = {"original_claim_digest", "input_digest", "authority_digest", "run_fingerprint", "binding_digest", "package_digest", "composition_digest", "method_manifest_digest", "host_boot_nonce", "effect_digest", "artifact_digest", "checkpoint_digest", "family_digest"}
    numbers = {"original_deadline_at", "action_deadline_at", "original_revision", "original_fence", "attempt_count", "current_revision", "current_fence"}
    closed(value, refs | digests | numbers | {"schema_version", "producer", "state", "phase", "no_learning"})
    integer(value["schema_version"], 1, 1)
    for name in refs: ref(value[name])
    for name in digests: sha(value[name])
    for name in numbers: integer(value[name], 1)
    if (value["schema_version"] != 1 or value["producer"] != "native-turn-cancel.v1" or value["no_learning"] is not True
            or value["control_method"] not in CANCEL_METHODS or value["state"] not in {"unknown_external_effect", "cost_liability", "cancelled"}
            or value["phase"] not in {"running", "controlled"} or value["attempt_count"] != 1
            or value["current_fence"] != value["original_fence"] + 1
            or value["current_revision"] != value["original_revision"] + 1
            or value["action_deadline_at"] > value["original_deadline_at"]):
        raise NativeTurnBlocked("native_turn_cancel_witness_invalid")
    _receipt(CANCEL_ID, value)
    return value


def validate_closure_payload(value):
    from src.runtime_plugins.protocol import closed, integer
    from src.runtime_plugins.contracts import ref, sha
    refs = {"job_ref", "claim_ref"}
    digests = {"original_claim_digest", "binding_digest", "cancel_digest", "family_digest", "effect_digest", "accounting_readback_digest"}
    numbers = {"original_fence", "cancel_fence", "terminal_revision", "terminal_fence", "original_deadline_at"}
    closed(value, refs | digests | numbers | {"schema_version", "producer", "outcome", "physically_closed", "no_learning"})
    integer(value["schema_version"], 1, 1)
    for name in refs: ref(value[name])
    for name in digests: sha(value[name])
    for name in numbers: integer(value[name], 1)
    if (value["schema_version"] != 1 or value["producer"] != "native-turn-cancel-closure.v1"
            or value["outcome"] not in {"returned", "raised"} or value["physically_closed"] is not True
            or value["no_learning"] is not True or value["cancel_fence"] != value["original_fence"] + 1
            or value["terminal_fence"] != value["cancel_fence"]):
        raise NativeTurnBlocked("native_turn_cancel_closure_invalid")
    _receipt(CLOSURE_ID, value)
    return value


def _assert_original(run, resource, cancel):
    from src.runtime_plugins.dispatch import _ms, _witness
    witness = _witness(run, resource.execution.scope)
    if (cancel["job_ref"] != run.run_identity or cancel["claim_ref"] != witness["claim_ref"]
            or cancel["original_claim_digest"] != _digest(dict(resource.execution.scope.witness))
            or cancel["input_digest"] != run.input_digest or cancel["authority_digest"] != run.authority_digest
            or cancel["run_fingerprint"] != run.run_fingerprint or cancel["original_deadline_at"] != _ms(run.deadline_at)
            or cancel["binding_digest"] != resource.execution.scope.binding.binding_digest
            or cancel["original_fence"] != witness["fencing_token"]
            or run.attempt_count != 1 or run.max_attempts != 1
            or cancel["owner_principal_id"] != run.owner_principal_id
            or cancel["operator_session_id"] != run.operator_session_id
            or cancel["conversation_ref"] != run.conversation_id):
        raise NativeTurnBlocked("native_turn_cancel_original_changed")


def terminal_cancel_projection_valid(run, history):
    """Inspection-only closed canonical proof; this never rebuilds a resource."""
    try:
        cancel = validate_cancel_payload(_selected(history, CANCEL_ID)["payload"])
        closure = validate_closure_payload(_selected(history, CLOSURE_ID)["payload"])
        original = [item for item in history if item.get("checkpoint_id", "").startswith("runtime-service-invocation:")]
        if len(original) != 1:
            return False
        from src.runtime_plugins.dispatch import _receipt_witness, _ms
        witness = _receipt_witness(original[0])
        return (run.status == "cancelled" and run.lease_owner is None and run.lease_expires_at is None
            and run.revision == closure["terminal_revision"] and run.fencing_token == closure["terminal_fence"]
            and run.attempt_count == 1 and cancel["job_ref"] == run.run_identity
            and cancel["original_claim_digest"] == _digest(witness)
            and cancel["input_digest"] == run.input_digest and cancel["authority_digest"] == run.authority_digest
            and cancel["original_deadline_at"] == _ms(run.deadline_at)
            and closure["cancel_digest"] == _digest(cancel)
            and closure["original_claim_digest"] == cancel["original_claim_digest"]
            and closure["family_digest"] == _digest(_selected(history, "conversation:operation-family")["payload"])
            and closure["effect_digest"] == _digest(json.loads(run.effect_receipts_json)))
    except Exception:
        return False


def preflight_control_journal(history):
    from src.agent.native_turn_family import FAMILY_CHECKPOINT_ID
    if type(history) is not list or any(type(item) is not dict for item in history):
        raise NativeTurnBlocked("native_turn_control_history_invalid")
    identifiers = [item.get("checkpoint_id") for item in history]
    if len(set(identifiers)) != len(identifiers):
        raise NativeTurnBlocked("native_turn_control_history_ambiguous")
    foreseeable = {CANCEL_ID, CLOSURE_ID, FAMILY_CHECKPOINT_ID}
    ordinary = [item for item in history if item["checkpoint_id"] not in foreseeable]
    output_ids = {"conversation:assistant-message", "conversation:controlled-outcome"}
    missing_output = not any(item["checkpoint_id"] in output_ids for item in ordinary)
    if len(ordinary) + 3 + int(missing_output) > 50:
        raise NativeTurnBlocked("native_turn_control_checkpoint_capacity")
    projected = len(json.dumps(ordinary, ensure_ascii=False, separators=(",", ":")).encode()) + 2 * 8704 + 65536 + 512 + (2048 if missing_output else 0)
    if projected > 1048576:
        raise NativeTurnBlocked("native_turn_control_journal_byte_cap")
    for identifier in (CANCEL_ID, CLOSURE_ID):
        if identifier in identifiers:
            _receipt(identifier, _selected(history, identifier)["payload"])


@dataclass(eq=False)
class _ControlPublication:
    db: object
    execution: object
    receipts: tuple
    seal: object


def reserve_control_capacity(db, execution, history):
    receipts = tuple(_receipt(identifier, {"schema_version": 1,
        "producer": "native-turn-control-reservation.v1", "job_ref": execution.admission.job_id,
        "original_claim_digest": _digest(dict(execution.scope.witness)), "reserved_for": identifier,
        "no_learning": True}) for identifier in (CANCEL_ID, CLOSURE_ID))
    if any(item["checkpoint_id"] in {CANCEL_ID, CLOSURE_ID} for item in history):
        raise NativeTurnBlocked("native_turn_control_reservation_exists")
    result = [*history, *receipts]
    preflight_control_journal(result)
    db.info["composition_native_turn_control_publication"] = _ControlPublication(db, execution, receipts, _FACTORY_SEAL)
    return result


def validate_control_publication(db, before, after, *, run_id):
    permission = db.info.get("composition_native_turn_control_publication")
    if type(permission) is not _ControlPublication or permission.seal is not _FACTORY_SEAL or permission.db is not db:
        raise NativeTurnBlocked("native_turn_control_producer_missing")
    for receipt in permission.receipts:
        identifier = receipt["checkpoint_id"]
        if (after.get(identifier) != receipt or receipt["payload"]["job_ref"] != permission.execution.admission.job_id
                or permission.execution.admission.job_id != run_id):
            raise NativeTurnBlocked("native_turn_control_publication_changed")
        prior = before.get(identifier)
        if prior is not None and (prior["payload"].get("producer") != "native-turn-control-reservation.v1"
                or prior["payload"].get("reserved_for") != identifier
                or prior["payload"].get("original_claim_digest") != _digest(dict(permission.execution.scope.witness))):
            raise NativeTurnBlocked("native_turn_control_history_mutation_denied")
    return permission.receipts


async def _cas_control(db, run, resource, history, changes, receipts):
    from sqlalchemy import update
    from src.db.models import WorkflowRunState
    preflight_control_journal(history)
    db.info["composition_native_turn_control_publication"] = _ControlPublication(db, resource.execution, receipts, _FACTORY_SEAL)
    conditions = [WorkflowRunState.run_identity == run.run_identity, WorkflowRunState.revision == run.revision,
        WorkflowRunState.fencing_token == run.fencing_token, WorkflowRunState.status == run.status,
        WorkflowRunState.checkpoint_receipts_json == run.checkpoint_receipts_json,
        WorkflowRunState.effect_receipts_json == run.effect_receipts_json,
        WorkflowRunState.artifact_receipts_json == run.artifact_receipts_json,
        WorkflowRunState.input_digest == run.input_digest, WorkflowRunState.authority_digest == run.authority_digest,
        WorkflowRunState.composition_binding_json == run.composition_binding_json,
        WorkflowRunState.arguments_json == run.arguments_json, WorkflowRunState.run_fingerprint == run.run_fingerprint,
        WorkflowRunState.deadline_at == run.deadline_at, WorkflowRunState.attempt_count == 1,
        WorkflowRunState.owner_principal_id == run.owner_principal_id,
        WorkflowRunState.operator_session_id == run.operator_session_id,
        WorkflowRunState.lease_owner == run.lease_owner, WorkflowRunState.lease_expires_at == run.lease_expires_at]
    values = {**changes, "checkpoint_receipts_json": json.dumps(history, sort_keys=True, separators=(",", ":")),
        "updated_at": datetime.now(timezone.utc)}
    result = await db.execute(update(WorkflowRunState).where(*conditions)
        .execution_options(synchronize_session=False).values(**values))
    if result.rowcount != 1:
        raise NativeTurnBlocked("native_turn_control_cas_changed")
    await db.flush()
