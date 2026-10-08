"""One original transport continuation; child RPCs never execute a model."""
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
import asyncio
import hashlib
import inspect
import json
import secrets
import time
from copy import deepcopy
from decimal import Decimal

_SEAL = object()
_route_scope = ContextVar("original_native_inference_route", default=None)
CANDIDATE_CHECKPOINT_ID = "inference:original-candidate.v1"
_CANDIDATE_FIELDS = set("schema_version producer operation_id accounting_job_id turn_job_id turn_claim_digest request_ref owner_id lease_owner attempt_count fencing_token reservation_sequence reservation_binding_digest payload_digest policy_digest profile_id runtime_path purpose_deadline_at operation_deadline_at turn_deadline_at host_boot_nonce composition_binding_digest mode".split())


def _deny(reason):
    from src.agent.native_turn_family import _deny
    _deny(reason)


def _bytes(value):
    nodes = 0
    def check(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 16 or nodes > 4096:
            _deny("native_inference_output_bounds")
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                _deny("native_inference_output_codec_unsupported")
            for child in item.values():
                check(child, depth + 1)
        elif type(item) is list:
            for child in item:
                check(child, depth + 1)
        elif type(item) is Decimal:
            if not item.is_finite():
                _deny("native_inference_output_codec_unsupported")
        elif item is not None and type(item) not in (str, int, float, bool):
            _deny("native_inference_output_codec_unsupported")
    check(value)
    try:
        def encode(item):
            if type(item) is dict:
                return "{" + ",".join(encode(key) + ":" + encode(item[key]) for key in sorted(item)) + "}"
            if type(item) is list:
                return "[" + ",".join(encode(child) for child in item) + "]"
            if type(item) in (Decimal, float):
                number = item if type(item) is Decimal else Decimal(str(item))
                if not number.is_finite():
                    _deny("native_inference_output_codec_unsupported")
                sign, digits, exponent = number.as_tuple()
                digits = list(digits)
                while len(digits) > 1 and digits[-1] == 0:
                    digits.pop()
                    exponent += 1
                prefix = "-" if sign else ""
                if not any(digits):
                    return prefix + "0.0"
                coefficient = "".join(str(digit) for digit in digits)
                # Exact context-free Decimal encoding: never normalize/round.
                # A decimal point keeps zero/integral numeric type on readback.
                return prefix + coefficient[0] + "." + (coefficient[1:] or "0") + "E" + str(exponent + len(digits) - 1)
            return json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        encoded = encode(value).encode("utf-8", errors="strict")
    except (ValueError, UnicodeError):
        _deny("native_inference_output_codec_unsupported")
    if len(encoded) > 1048576:
        _deny("native_inference_output_bounds")
    return encoded


def _route_binding(context, decision, session):
    principal = context.principal
    return (context.request_id, context.data_digest, context.deadline_at, context.session_id,
        context.job_id, principal, principal.principal_id, principal.session_id,
        principal.operator_session_id, decision.selected.profile.id, session._context.request_id)


class _OriginalRouteScope:
    def __init__(self, context, decision, session, mode):
        self.context, self.decision, self.session, self.mode = context, decision, session, mode
        self.binding = _route_binding(context, decision, session)


@contextmanager
def original_route_scope(context, decision, session, mode):
    from src.agent.controlled_origin import _current_execution
    from src.agent.native_turn_family import _current_call, _original_producer_stack
    from src.model_fabric.hooks import RouteReceiptSession
    execution, call = _current_execution.get(), _current_call.get()
    if execution is None:
        yield
        return
    if call is not None and call.operation is not None:
        _deny("native_inference_multiple_operations_per_call_unsupported")
    if (call is None or call.execution is not execution or not _original_producer_stack(call)
        or type(session) is not RouteReceiptSession or not decision.allowed or decision.selected is None
        or mode not in ("completion", "stream") or getattr(session, "_native_continuation", None) is not None):
        _deny("native_inference_original_route_missing")
    if decision.selected.adapter != "openai_compatible_chat":
        _deny("native_inference_output_codec_unsupported")
    scope = _OriginalRouteScope(context, decision, session, mode)
    token = _route_scope.set(scope)
    try:
        yield
    finally:
        _route_scope.reset(token)


class NativeInferenceResultWitness:
    def __init__(self, continuation, result, snapshot, codec, *, seal):
        if seal is not _SEAL:
            _deny("native_inference_result_unproven")
        self.continuation, self.result = continuation, result
        self.result_bytes = _bytes(snapshot)
        self.result_snapshot = deepcopy(snapshot)
        self.result_content_sha256 = hashlib.sha256(self.result_bytes).hexdigest()
        self.output_codec, self.result_type, self._seal = "native-inference-output-bytes.v1", codec, seal
        self._binding = (continuation, result, self.result_bytes, self.result_content_sha256, codec)


class NativeInferenceRouteWitness:
    def __init__(self, continuation, receipt, persistence, *, seal):
        if seal is not _SEAL:
            _deny("native_inference_route_unproven")
        self.continuation, self.receipt, self.persistence = continuation, receipt, persistence
        self.context, self.decision = continuation.route_scope.context, continuation.route_scope.decision
        self._seal = seal


class NativeInferenceContinuation:
    """Issued only after actual original broker prepare/bind; never from refs."""
    def __init__(self, broker, operation, handle, settlement_context, scope, *, seal):
        if seal is not _SEAL:
            _deny("native_inference_original_candidate_missing")
        self.broker, self.operation, self.handle = broker, operation, handle
        self.settlement_context, self.route_scope = settlement_context, scope
        self.operation_witness = handle.native_turn_operation_witness
        self.call, self.execution = self.operation_witness.call, self.operation_witness.execution
        self.host, self.original_scope = self.execution.host, self.execution.scope
        self._original_admission, self._original_claim = self.execution.admission, self.execution.claim
        self._original_principal = self.execution.admission.principal
        self._original_owner_binding = (self._original_principal.principal_id,
            self._original_principal.operator_session_id, self._original_principal.session_id)
        self.host_boot_nonce = self.original_scope.host_boot_nonce
        self.owner_loop = self.host.get_original_owner_loop()
        self.request_ref = "native-inference-" + secrets.token_hex(24)
        self.purpose_deadline_at = int(scope.context.deadline_at * 1000)
        self.operation_deadline_at = int(handle.request.deadline_at * 1000)
        self.turn_deadline_at = self.original_scope.deadline_at
        self.deadline_at = min(self.purpose_deadline_at, self.operation_deadline_at, self.turn_deadline_at)
        self._binding = (handle, handle.request, self.operation_witness, self.call, operation, scope,
            self.host, self.original_scope, self.owner_loop, self.request_ref, self.deadline_at)
        self._seal, self.state = seal, "prepared"
        self._permitted, self._ready = Future(), Future()
        self._rpc = None
        self.result_witness = self.route_witness = self.failure = None
        self.deltas = []
        self.output_refs = None

    def validate_host_scope(self, host, original_scope):
        from src.agent.native_turn_family import _handle_binding
        witness = self.operation_witness
        if (self._seal is not _SEAL or host is not self.host or original_scope is not self.original_scope
            or self.execution.admission is not self._original_admission or self.execution.claim is not self._original_claim
            or self.execution.admission.principal is not self._original_principal
            or self._original_owner_binding != (self._original_principal.principal_id,
                self._original_principal.operator_session_id, self._original_principal.session_id)
            or self._binding != (self.handle, self.handle.request, witness, self.call, self.operation,
                self.route_scope, self.host, self.original_scope, self.owner_loop, self.request_ref, self.deadline_at)
            or host.boot_nonce != self.host_boot_nonce or host.get_original_owner_loop() is not self.owner_loop
            or self.call.operation is not witness or witness.handle is not self.handle
            or witness._binding != _handle_binding(self.handle)
            or self.execution._family_witnesses.get(witness.operation_id) is not witness
            or getattr(self.handle, "native_inference_continuation", None) is not self
            or self.deadline_at <= int(time.time() * 1000)):
            _deny("native_inference_original_candidate_changed")
        scope = self.route_scope
        if scope.binding != _route_binding(scope.context, scope.decision, scope.session):
            _deny("native_inference_original_route_changed")

    def validate_called_scope(self, host, called_scope, request_ref):
        self.validate_host_scope(host, called_scope.original)
        if (called_scope.method != "inference.request" or request_ref != self.request_ref
            or called_scope.host_boot_nonce != self.host_boot_nonce or called_scope.deadline_at != self.deadline_at):
            _deny("native_inference_original_candidate_changed")

    def remaining(self):
        self.validate_host_scope(self.host, self.original_scope)
        return max(0, (self.deadline_at - int(time.time() * 1000)) / 1000)

    async def _request(self):
        return await self.host.request_service("inference.request", {"request_ref": self.request_ref},
            original_scope=self.original_scope, native_inference=self)

    def start(self):
        self.validate_host_scope(self.host, self.original_scope)
        if self.state != "prepared":
            _deny("native_inference_candidate_replayed")
        self.state = "requested"
        self._rpc = asyncio.run_coroutine_threadsafe(self._request(), self.owner_loop)
        def completed(done):
            if not self._permitted.done():
                try:
                    done.result()
                except BaseException as exc:
                    self._permitted.set_exception(exc)
                else:
                    self._permitted.set_exception(RuntimeError("native_inference_not_consumed"))
        self._rpc.add_done_callback(completed)

    async def permit(self):
        self.start()
        await asyncio.wait_for(asyncio.wrap_future(self._permitted), self.remaining())
        if self.state != "consumed":
            _deny("native_inference_not_consumed")

    def permit_sync(self):
        try:
            if asyncio.get_running_loop() is self.owner_loop:
                _deny("native_inference_sync_host_loop_unsupported")
        except RuntimeError:
            pass
        self.start()
        self._permitted.result(timeout=self.remaining())
        if self.state != "consumed":
            _deny("native_inference_not_consumed")

    def stage_result(self, result):
        from smolagents.models import ChatMessage
        from types import SimpleNamespace
        if self.state != "consumed" or type(result) is not tuple or len(result) != 2 or type(result[1]) is not dict:
            _deny("native_inference_output_codec_unsupported")
        from src.workflows.inference_accounting import account_charge_microusd
        cost = account_charge_microusd(self.settlement_context.usage)[0]
        if type(cost) is not int or cost < 0:
            _deny("native_inference_result_usage_unknown")
        message, raw = result
        codec = "sdk_chat_message" if type(message) is ChatMessage else "direct_chat_completion"
        if codec == "sdk_chat_message":
            if message.raw != raw:
                _deny("native_inference_result_changed")
        else:
            if (type(message) is not SimpleNamespace or type(message.choices) is not list
                or len(message.choices) != 1 or type(message.choices[0]) is not SimpleNamespace
                or type(message.choices[0].message) is not ChatMessage
                or message.choices[0].message.raw != raw):
                _deny("native_inference_output_codec_unsupported")
        snapshot = {"result_type": codec, "provider_response": raw}
        self.result_witness = NativeInferenceResultWitness(self, result, snapshot, codec, seal=_SEAL)
        self.state = "transport_finished"

    def stage_delta(self, item):
        if self.state != "consumed" or type(item) is not str:
            _deny("native_inference_output_codec_unsupported")
        self.deltas.append(item)
        _bytes(self.deltas)

    def stage_stream_close(self):
        if self.state != "consumed" or not self.deltas:
            _deny("native_inference_stream_closure_unproven")
        settled = self.settlement_context.settlement
        if settled is None or settled.get("state") != "settled" or type(settled.get("actual_cost_microusd")) is not int:
            _deny("native_inference_result_usage_unknown")
        snapshot = {"result_type": "direct_stream", "deltas": self.deltas.copy(),
            "terminal_usage": self.settlement_context.usage.copy()}
        self.result_witness = NativeInferenceResultWitness(self, None, snapshot, "direct_stream", seal=_SEAL)
        self.state = "transport_finished"

    async def finish_failure(self, error):
        if self.state in ("sealed", "acknowledged", "released"):
            _deny("native_inference_failure_state_invalid")
        self.failure, self.state = error, "failed"
        snapshot = await self.handle.repository.inference_accounting_snapshot(job_id=self.handle.job_id)
        operations = [row for row in snapshot.get("operations", ()) if row.get("operation_id") == self.handle.request.operation_id]
        if snapshot.get("accounting_continuity_verified") is not True or len(operations) != 1:
            _deny("native_inference_failure_readback_missing")
        self.failure_accounting = snapshot
        if self.route_scope.mode == "stream":
            await complete_original_failed_attempt(self.route_scope.session)


def prepare_original_continuation(broker, operation, handle, settlement_context):
    scope = _route_scope.get()
    if scope is None:
        return None
    from src.model_fabric.accounting import DurableInferenceBrokerMixin
    caller = inspect.currentframe().f_back
    if caller.f_code not in (DurableInferenceBrokerMixin.execute.__code__,
        DurableInferenceBrokerMixin.execute_sync.__code__, DurableInferenceBrokerMixin.stream.__code__):
        _deny("native_inference_original_candidate_missing")
    candidate = NativeInferenceContinuation(broker, operation, handle, settlement_context, scope, seal=_SEAL)
    handle.native_inference_continuation = candidate
    scope.session._native_continuation = candidate
    return candidate


async def finalize_original_route(session, receipt, persistence):
    candidate = getattr(session, "_native_continuation", None)
    if candidate is None:
        return
    if candidate.state == "failed":
        await complete_original_failed_attempt(session)
        return
    candidate.validate_host_scope(candidate.host, candidate.original_scope)
    if (candidate.route_scope.session is not session or candidate.state != "transport_finished"
        or receipt.outcome != "succeeded" or persistence.persisted is not True
        or persistence.receipt_id != receipt.receipt_id or persistence.receipt_hash != receipt.receipt_hash
        or receipt.request_id != candidate.route_scope.context.request_id):
        _deny("native_inference_route_readback_missing")
    actual = await session._repository.route_for_request(request_id=receipt.request_id, outcome="succeeded")
    if actual is None or actual.receipt_id != receipt.receipt_id or actual.receipt_hash != receipt.receipt_hash:
        _deny("native_inference_route_readback_missing")
    candidate.route_witness = NativeInferenceRouteWitness(candidate, actual, persistence, seal=_SEAL)
    candidate.state = "route_finalized"
    candidate._ready.set_result(None)
    response = await asyncio.wait_for(asyncio.wrap_future(candidate._rpc), candidate.remaining())
    if response.get("status") == "blocked":
        _deny(response["reason_code"])
    if (candidate.state != "sealed" or response.get("status") != "succeeded"
        or response.get("value") != candidate.output_refs):
        _deny("native_inference_output_ack_unproven")
    candidate.state = "released"


async def complete_original_failed_attempt(session):
    candidate = getattr(session, "_native_continuation", None)
    if candidate is None or candidate.state != "failed":
        return
    candidate.validate_host_scope(candidate.host, candidate.original_scope)
    if session is not candidate.route_scope.session or session._active is not None:
        _deny("native_inference_failed_attempt_unproven")
    if candidate.handle.contacted:
        completed = session._completed
        if not completed or completed[-1].receipt.outcome != "failed":
            _deny("native_inference_failed_attempt_unproven")
        candidate.failure_attempt = completed[-1].receipt
    else:
        candidate.failure_attempt = None
    if not candidate._ready.done():
        candidate._ready.set_result(None)
    if candidate._rpc is not None:
        response = await asyncio.wait_for(asyncio.wrap_future(candidate._rpc), candidate.remaining())
        if response.get("status") != "blocked":
            _deny("native_inference_failed_response_changed")
    # A later outer aggregate failure cannot redeem this consumed candidate.
    session._native_continuation = None


def validate_result_route_witnesses(result_witness, route_witness):
    if type(result_witness) is not NativeInferenceResultWitness or type(route_witness) is not NativeInferenceRouteWitness:
        _deny("native_inference_output_owner_unproven")
    candidate = result_witness.continuation
    candidate.validate_host_scope(candidate.host, candidate.original_scope)
    if (result_witness._seal is not _SEAL or route_witness._seal is not _SEAL
        or candidate.result_witness is not result_witness or candidate.route_witness is not route_witness
        or route_witness.continuation is not candidate or candidate.state != "route_finalized"
        or not candidate.operation_witness.consumed
        or result_witness._binding != (candidate, result_witness.result, result_witness.result_bytes,
            result_witness.result_content_sha256, result_witness.result_type)
        or hashlib.sha256(_bytes(result_witness.result_snapshot)).hexdigest() != result_witness.result_content_sha256):
        _deny("native_inference_output_owner_unproven")
    if result_witness.result is not None:
        if _bytes(result_witness.result[1]) != _bytes(result_witness.result_snapshot["provider_response"]):
            _deny("native_inference_result_changed")
    return candidate


async def dispatch_original_inference(dispatcher, frame, called_scope):
    from src.db.engine import get_session
    from src.runtime_plugins.contracts import blocked, succeeded
    from src.agent.turn_execution import NativeTurnBlocked
    resource = getattr(called_scope.original, "native_turn_resource", None)
    if resource is None:
        return blocked("native_inference_original_host_missing")
    resource.owner._check(resource)
    host = resource.execution.host
    candidate = host._native_inference_candidate(called_scope, frame["payload"]["request_ref"])
    try:
        async with get_session() as db:
            await dispatcher._current_in_db(db, frame["invocation_ref"], "inference.request", called_scope.original)
            if candidate.state != "requested":
                _deny("native_inference_candidate_replayed")
            await dispatcher.jobs.consume_native_inference_candidate_in_session(db, candidate=candidate)
        # Successful writer close precedes notification and all waiting.
        candidate.state = "consumed"
        candidate._permitted.set_result(None)
        await asyncio.wait_for(asyncio.wrap_future(candidate._ready), candidate.remaining())
        if candidate.state == "failed":
            return blocked("native_inference_original_transport_failed")
        validate_result_route_witnesses(candidate.result_witness, candidate.route_witness)
        refs = await dispatcher.jobs.seal_native_inference_output(result_witness=candidate.result_witness,
            route_witness=candidate.route_witness)
        candidate.output_refs, candidate.state = refs, "sealed"
        return succeeded("inference.request", refs)
    except NativeTurnBlocked as error:
        return blocked(error.reason_code)


def validate_consumption_candidate(candidate):
    if type(candidate) is not NativeInferenceContinuation or candidate._seal is not _SEAL:
        _deny("native_inference_original_candidate_missing")
    candidate.validate_host_scope(candidate.host, candidate.original_scope)
    if (candidate.state != "requested" or candidate.call.finished or candidate.operation_witness.consumed
        or candidate.route_scope.session._native_continuation is not candidate
        or candidate.handle.contacted):
        _deny("native_inference_candidate_replayed")
    return candidate


def validate_candidate_payload(payload):
    import re
    if (type(payload) is not dict or set(payload) != _CANDIDATE_FIELDS
        or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
        or payload["producer"] != "original-native-inference.v1" or payload["mode"] not in {"completion", "stream"}):
        _deny("native_inference_candidate_codec_invalid")
    for key in ("attempt_count", "fencing_token", "reservation_sequence", "purpose_deadline_at",
        "operation_deadline_at", "turn_deadline_at"):
        if type(payload[key]) is not int or not 1 <= payload[key] <= 9007199254740991:
            _deny("native_inference_candidate_codec_invalid")
    for key in ("turn_claim_digest", "reservation_binding_digest", "payload_digest", "policy_digest",
        "host_boot_nonce", "composition_binding_digest"):
        if type(payload[key]) is not str or re.fullmatch("[0-9a-f]{64}", payload[key]) is None:
            _deny("native_inference_candidate_codec_invalid")
    for key in ("operation_id", "accounting_job_id", "turn_job_id", "request_ref", "owner_id", "lease_owner",
        "profile_id", "runtime_path"):
        value = payload[key]
        if type(value) is not str or not 1 <= len(value) <= 256 or any(ord(char) < 32 for char in value):
            _deny("native_inference_candidate_codec_invalid")
    if len(_bytes(payload)) > 8192:
        _deny("native_inference_candidate_codec_invalid")
    return payload


def build_candidate_payload(candidate, operation_run, reservation):
    from src.agent.native_turn_family import _reservation_digest, _digest
    validate_consumption_candidate(candidate)
    handle = candidate.handle
    if (operation_run.status != "running" or operation_run.lease_owner != handle.owner
        or operation_run.fencing_token != handle.fence or reservation.state != "reserved"
        or reservation.contact_started_at is not None or reservation.operation_id != handle.request.operation_id
        or reservation.job_id != handle.job_id or reservation.sequence != handle.sequence
        or reservation.payload_digest != candidate.route_scope.context.data_digest
        or reservation.owner_id != candidate.execution.admission.principal.principal_id):
        _deny("native_inference_candidate_owner_changed")
    payload = {"schema_version": 1, "producer": "original-native-inference.v1",
        "operation_id": reservation.operation_id, "accounting_job_id": operation_run.run_identity,
        "turn_job_id": candidate.execution.admission.job_id, "turn_claim_digest": _digest(dict(candidate.original_scope.witness)),
        "request_ref": candidate.request_ref, "owner_id": reservation.owner_id, "lease_owner": handle.owner,
        "attempt_count": operation_run.attempt_count, "fencing_token": handle.fence,
        "reservation_sequence": reservation.sequence, "reservation_binding_digest": _reservation_digest(reservation),
        "payload_digest": reservation.payload_digest, "policy_digest": reservation.policy_digest,
        "profile_id": reservation.profile_id, "runtime_path": reservation.runtime_path,
        "purpose_deadline_at": candidate.purpose_deadline_at, "operation_deadline_at": candidate.operation_deadline_at,
        "turn_deadline_at": candidate.turn_deadline_at, "host_boot_nonce": candidate.host_boot_nonce,
        "composition_binding_digest": candidate.original_scope.binding.binding_digest, "mode": candidate.route_scope.mode}
    return validate_candidate_binding(payload, operation_run, reservation)


def validate_candidate_binding(payload, operation_run, reservation):
    from src.agent.native_turn_family import _reservation_digest, _utc
    from datetime import datetime
    validate_candidate_payload(payload)
    if (operation_run.run_identity != payload["accounting_job_id"]
        or operation_run.job_kind != "model_inference_ephemeral_v1"
        or operation_run.owner_principal_id != payload["owner_id"]
        or operation_run.attempt_count != payload["attempt_count"]
        or operation_run.fencing_token < payload["fencing_token"]
        or reservation.job_fencing_token != payload["fencing_token"]
        or reservation.operation_id != payload["operation_id"] or reservation.job_id != payload["accounting_job_id"]
        or reservation.owner_id != payload["owner_id"] or reservation.sequence != payload["reservation_sequence"]
        or reservation.payload_digest != payload["payload_digest"] or reservation.policy_digest != payload["policy_digest"]
        or reservation.profile_id != payload["profile_id"] or reservation.runtime_path != payload["runtime_path"]
        or _reservation_digest(reservation) != payload["reservation_binding_digest"]
        or int(datetime.fromisoformat(_utc(reservation.deadline_at)).timestamp() * 1000) != payload["operation_deadline_at"]):
        _deny("native_inference_candidate_owner_changed")
    return payload
