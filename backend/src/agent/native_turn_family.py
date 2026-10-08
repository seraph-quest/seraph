"""Private bounded correlation of original turn calls with existing owner rows."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import inspect
import json
import re

FAMILY_CHECKPOINT_ID = "conversation:operation-family"
_VERSION = "native-turn-operation-family.v1"
_SEAL = object()
_current_call = ContextVar("native_turn_original_inference_call", default=None)
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_FAMILY_FIELDS = {"schema_version", "family_version", "turn_job_ref", "input_message_ref", "native_route",
    "original_claim_digest", "original_deadline", "sdk_steps", "max_inference_operations", "max_encoded_bytes", "operations"}
_OP_FIELDS = {"kind", "operation_id", "job_id", "owner_id", "lease_owner", "attempt_count", "fencing_token",
    "reservation_sequence", "payload_digest", "policy_digest", "profile_id", "runtime_path", "operation_deadline",
    "reservation_binding_digest"}
_RESERVATION_FIELDS = ("operation_id", "deployment_id", "job_id", "owner_id", "goal_id", "goal_revision",
    "payload_digest", "policy_digest", "runtime_path", "profile_id", "period_id", "settings_revision",
    "ceiling_microusd", "bound_microusd", "owner_ceiling_microusd", "sequence", "priority", "deadline_at",
    "job_fencing_token", "created_at")


def _deny(reason):
    from src.agent.turn_execution import NativeTurnBlocked
    from src.agent.controlled_origin import _current_execution
    error = NativeTurnBlocked(reason)
    execution = _current_execution.get()
    if execution is not None and getattr(execution, "_family_failure_exception", None) is None:
        execution._family_failure_exception = error
    raise error


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _utc(value):
    # Canonical SQLite closure rows retain the ORM's naive UTC storage text.
    # The public payload validator separately requires canonical aware text.
    if type(value) is str:
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            _deny("native_turn_family_datetime_invalid")
    if not isinstance(value, datetime):
        _deny("native_turn_family_datetime_invalid")
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()


def _typed_date(value):
    if type(value) is not str:
        _deny("native_turn_family_datetime_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _deny("native_turn_family_datetime_invalid")
    if parsed.tzinfo is None or _utc(parsed) != value:
        _deny("native_turn_family_datetime_invalid")


def _ref(value, bound=256):
    if type(value) is not str or not 1 <= len(value) <= bound or any(ord(c) < 32 for c in value):
        _deny("native_turn_family_identity_invalid")


def _positive(value):
    if type(value) is not int or value < 1:
        _deny("native_turn_family_counter_invalid")


def validate_family_payload(payload):
    if type(payload) is not dict or set(payload) != _FAMILY_FIELDS:
        _deny("native_turn_family_codec_invalid")
    if (type(payload["schema_version"]) is not int or payload["schema_version"] != 1
        or payload["family_version"] != _VERSION or payload["native_route"] not in {"direct_turn", "generic_turn"}
        or type(payload["sdk_steps"]) is not int or type(payload["max_inference_operations"]) is not int
        or type(payload["max_encoded_bytes"]) is not int or payload["max_encoded_bytes"] != 65536):
        _deny("native_turn_family_codec_invalid")
    steps = payload["sdk_steps"]
    if ((payload["native_route"] == "direct_turn" and (steps != 0 or payload["max_inference_operations"] != 1))
        or (payload["native_route"] == "generic_turn" and (not 1 <= steps <= 64
            or payload["max_inference_operations"] != steps + 1))):
        _deny("native_turn_family_budget_invalid")
    _ref(payload["turn_job_ref"])
    _ref(payload["input_message_ref"], 128)
    if type(payload["original_claim_digest"]) is not str or not _SHA.fullmatch(payload["original_claim_digest"]):
        _deny("native_turn_family_digest_invalid")
    _typed_date(payload["original_deadline"])
    operations = payload["operations"]
    if type(operations) is not list or len(operations) > payload["max_inference_operations"]:
        _deny("native_turn_family_budget_exceeded")
    ids, jobs = set(), set()
    for operation in operations:
        if type(operation) is not dict or set(operation) != _OP_FIELDS or operation["kind"] != "openrouter-accounting.v1":
            _deny("native_turn_family_operation_invalid")
        for key in ("operation_id", "job_id", "owner_id", "lease_owner"):
            _ref(operation[key])
        for key in ("profile_id", "runtime_path"):
            _ref(operation[key], 128)
        for key in ("attempt_count", "fencing_token", "reservation_sequence"):
            _positive(operation[key])
        for key in ("payload_digest", "policy_digest", "reservation_binding_digest"):
            if type(operation[key]) is not str or not _SHA.fullmatch(operation[key]):
                _deny("native_turn_family_digest_invalid")
        _typed_date(operation["operation_deadline"])
        if operation["operation_id"] in ids or operation["job_id"] in jobs:
            _deny("native_turn_family_duplicate_operation")
        ids.add(operation["operation_id"])
        jobs.add(operation["job_id"])
    if len(_canonical(payload).encode()) > payload["max_encoded_bytes"]:
        _deny("native_turn_family_encoded_budget_exceeded")
    return payload


def validate_family_transition(before, after):
    validate_family_payload(after)
    if before is None:
        if after["operations"]:
            _deny("native_turn_family_initial_operations_denied")
        return
    validate_family_payload(before)
    if ({key: value for key, value in before.items() if key != "operations"}
        != {key: value for key, value in after.items() if key != "operations"}
        or after["operations"][:len(before["operations"])] != before["operations"]
        or len(after["operations"]) != len(before["operations"]) + 1):
        _deny("native_turn_family_prefix_changed")


def validate_family_binding(payload, run, claim_payload=None):
    validate_family_payload(payload)
    arguments = json.loads(run.arguments_json)
    claims = [item["payload"] for item in json.loads(run.checkpoint_receipts_json)
        if type(item) is dict and str(item.get("checkpoint_id", "")).startswith("runtime-service-invocation:")]
    if len(claims) != 1 or (claim_payload is not None and claims[0] != claim_payload):
        _deny("native_turn_family_claim_changed")
    if (run.job_kind != "conversation_turn_v1" or payload["turn_job_ref"] != run.run_identity
        or payload["input_message_ref"] != arguments.get("message_ref")
        or payload["native_route"] != arguments.get("native_route")
        or payload["original_deadline"] != _utc(run.deadline_at)
        or payload["original_claim_digest"] != _digest(claims[0])):
        _deny("native_turn_family_binding_changed")
    return payload


def build_initial_family_payload(native_execution):
    from src.workflows.job_runtime import _native_plain
    validate_original_execution(native_execution)
    admission = native_execution.admission
    payload = {"schema_version": 1, "family_version": _VERSION, "turn_job_ref": admission.job_id,
        "input_message_ref": admission.ingress.message_id, "native_route": admission.native_route,
        "original_claim_digest": _digest(_native_plain(native_execution.claim.checkpoint)["payload"]),
        "original_deadline": _utc(admission.deadline_at), "sdk_steps": native_execution._family_steps,
        "max_inference_operations": native_execution._family_steps + 1, "max_encoded_bytes": 65536, "operations": []}
    return validate_family_payload(payload)


def _reservation_digest(row):
    binding = {key: (_utc(getattr(row, key)) if key in {"deadline_at", "created_at"} else getattr(row, key))
        for key in _RESERVATION_FIELDS}
    return _digest(binding)


def validate_family_operation_reference(operation, operation_run, reservation):
    """Recorded closure identity; a stopped recovery may fence the owner higher."""
    if type(operation) is not dict or set(operation) != _OP_FIELDS:
        _deny("native_turn_family_operation_invalid")
    from src.workflows.job_runtime import _digest as owner_digest
    if (operation["operation_id"] != reservation.operation_id or operation["job_id"] != reservation.job_id
        or operation_run.run_identity != reservation.job_id or operation_run.job_kind != "model_inference_ephemeral_v1"
        or operation_run.owner_kind != "user" or operation_run.owner_principal_id != operation["owner_id"]
        or reservation.owner_id != operation["owner_id"] or operation_run.attempt_count != operation["attempt_count"]
        or type(operation_run.fencing_token) is not int or operation_run.fencing_token < operation["fencing_token"]
        or reservation.job_fencing_token != operation["fencing_token"]
        or reservation.sequence != operation["reservation_sequence"]
        or reservation.payload_digest != operation["payload_digest"] or reservation.policy_digest != operation["policy_digest"]
        or reservation.profile_id != operation["profile_id"] or reservation.runtime_path != operation["runtime_path"]
        or operation_run.goal_id != reservation.goal_id or operation_run.goal_revision != reservation.goal_revision
        or operation_run.input_digest != owner_digest({"payload_digest": reservation.payload_digest,
            "runtime_path": reservation.runtime_path})
        or _utc(reservation.deadline_at) != operation["operation_deadline"]
        or _utc(operation_run.deadline_at) != operation["operation_deadline"]
        or _reservation_digest(reservation) != operation["reservation_binding_digest"]):
        _deny("native_turn_family_owner_binding_changed")
    return operation


class NativeOperationWitness:
    """One identity issued from the original captured invocation and broker handle."""
    __slots__ = ("execution", "call", "handle", "operation_id", "_seal", "consumed", "_binding")
    def __init__(self, execution, call, handle, *, seal):
        if seal is not _SEAL:
            _deny("native_turn_family_producer_missing")
        self.execution, self.call, self.handle = execution, call, handle
        self.operation_id, self._seal, self.consumed = handle.request.operation_id, seal, False
        self._binding = _handle_binding(handle)


def _handle_binding(handle):
    request = handle.request
    return (request.operation_id, handle.job_id, handle.owner, handle.fence, handle.policy_digest,
        handle.sequence, handle.ephemeral, request.owner_id, request.session_id, request.data_digest,
        request.runtime_path, request.deadline_at)


def operation_payload_from_witness(witness, operation_run, reservation):
    if (type(witness) is not NativeOperationWitness or witness._seal is not _SEAL or witness.consumed
        or witness.execution._family_witnesses.get(witness.operation_id) is not witness
        or _current_call.get() is not witness.call or witness.call.operation is not witness or witness.call.finished):
        _deny("native_turn_family_producer_missing")
    validate_original_execution(witness.execution)
    handle = witness.handle
    if (witness._binding != _handle_binding(handle) or not handle.ephemeral or handle.request.operation_id != reservation.operation_id
        or reservation.operation_id != witness.operation_id or handle.job_id != reservation.job_id
        or operation_run.run_identity != reservation.job_id or operation_run.job_kind != "model_inference_ephemeral_v1"
        or operation_run.owner_principal_id != reservation.owner_id
        or reservation.owner_id != witness.execution.admission.principal.principal_id
        or operation_run.lease_owner != handle.owner or operation_run.fencing_token != handle.fence
        or reservation.job_fencing_token != handle.fence or reservation.sequence != handle.sequence
        or reservation.payload_digest != handle.request.data_digest or reservation.policy_digest != handle.policy_digest
        or operation_run.status != "running" or reservation.state != "reserved"):
        _deny("native_turn_family_owner_binding_changed")
    payload = {"kind": "openrouter-accounting.v1", "operation_id": reservation.operation_id,
        "job_id": operation_run.run_identity, "owner_id": reservation.owner_id, "lease_owner": handle.owner,
        "attempt_count": operation_run.attempt_count, "fencing_token": operation_run.fencing_token,
        "reservation_sequence": reservation.sequence, "payload_digest": reservation.payload_digest,
        "policy_digest": reservation.policy_digest, "profile_id": reservation.profile_id, "runtime_path": reservation.runtime_path,
        "operation_deadline": _utc(reservation.deadline_at), "reservation_binding_digest": _reservation_digest(reservation)}
    witness.consumed = True
    return payload


def validate_original_execution(execution):
    from src.agent.turn_execution import NativeTurnExecution
    if type(execution) is not NativeTurnExecution or getattr(execution, "_family_seal", None) is not _SEAL:
        _deny("native_turn_family_execution_missing")
    execution.remaining()
    if getattr(execution, "_family_failure_exception", None) is not None:
        raise execution._family_failure_exception
    if execution.admission.native_route == "generic_turn":
        agent = execution._family_agent
        _validate_original_step_callbacks(execution, agent)
        if (agent.model is not execution._family_model or agent.max_steps != execution._family_steps
            or type(agent.max_steps) is not int or agent.tools is not execution._family_registry
            or len(agent.tools) != len(execution._family_registry_snapshot)
            or any(agent.tools.get(name) is not tool for name, tool in execution._family_registry_snapshot)
            or agent.model.generate is not execution._family_generate_wrapper):
            _deny("native_turn_family_original_model_changed")
        for path in execution._family_tool_paths:
            for outer, inner in zip(path, path[1:]):
                if outer.wrapped_tool is not inner:
                    _deny("native_turn_family_original_registry_changed")
        for tool, name, owner, function in execution._family_tool_methods:
            actual_owner, actual_function = _original_tool_method(tool, name)
            if actual_owner is not owner or actual_function is not function:
                _deny("native_turn_family_original_registry_changed")
    return execution


def prepare_generic_family(execution, agent):
    from smolagents import ToolCallingAgent
    from smolagents.agents import MultiStepAgent
    from src.agent.controlled_origin import _registered_path
    if (type(agent) is not ToolCallingAgent or type(agent.max_steps) is not int or not 1 <= agent.max_steps <= 64
        or agent.planning_interval is not None or agent.stream_outputs):
        _deny("native_turn_family_sdk_budget_unsupported")
    paths = tuple(tuple(_registered_path(tool)) for tool in agent.tools.values())
    _validate_registry_paths(paths)
    _capture_original_step_callbacks(execution, agent)
    execution._family_seal, execution._family_steps = _SEAL, agent.max_steps
    execution._family_agent, execution._family_model = agent, agent.model
    execution._family_agent_run = agent.run
    execution._family_registry, execution._family_registry_snapshot = agent.tools, tuple(agent.tools.items())
    execution._family_tool_paths, execution._family_witnesses, execution._family_calls = paths, {}, []
    execution._family_tool_methods = tuple((item, name, *_original_tool_method(item, name))
        for path in paths for item in path for name in ("__call__", "forward"))
    original = agent.model.generate
    def original_generate(*args, **kwargs):
        try:
            validate_original_execution(execution)
            if not getattr(execution, "_family_callback_active", False):
                _deny("native_turn_family_original_callback_missing")
            caller = inspect.currentframe().f_back
            if (caller.f_code not in {ToolCallingAgent._step_stream.__code__, MultiStepAgent.provide_final_answer.__code__}
                or caller.f_locals.get("self") is not agent):
                _deny("native_turn_family_indirect_model_call")
            frame = caller.f_back
            for _ in range(96):
                if frame is None or frame.f_code is MultiStepAgent._run_stream.__code__:
                    break
                frame = frame.f_back
            if (frame is None or frame.f_code is not MultiStepAgent._run_stream.__code__
                or frame.f_locals.get("self") is not agent):
                _deny("native_turn_family_original_callback_missing")
            with _original_model_call(execution, original, caller):
                result = original(*args, **kwargs)
                validate_original_execution(execution)
                return result
        except BaseException:
            try:
                # A raised original model error still reaches the stock SDK's
                # finalize path. Validate replacement identities before that
                # path, while preserving ordinary unchanged model errors.
                validate_original_execution(execution)
            finally:
                agent.step_callbacks = execution._family_step_registry
                agent.step_callbacks.callback = execution._family_step_dispatch
            raise
        finally:
            if getattr(execution, "_family_failure_exception", None) is not None:
                # The stock SDK catches model Exceptions and dispatches its
                # actual step callbacks. Keep that denial unwind on the
                # original gated dispatcher; never run a substituted one.
                agent.step_callbacks = execution._family_step_registry
                agent.step_callbacks.callback = execution._family_step_dispatch
    execution._family_generate_wrapper = original_generate
    agent.model.generate = original_generate


def _capture_original_step_callbacks(execution, agent):
    from smolagents.memory import ActionStep, CallbackRegistry
    from smolagents.monitoring import Monitor
    registry = agent.step_callbacks
    if (type(registry) is not CallbackRegistry or type(registry._callbacks) is not dict
        or tuple(registry._callbacks) != (ActionStep,)
        or type(registry._callbacks[ActionStep]) is not list
        or len(registry._callbacks[ActionStep]) != 2
        or type(agent.monitor) is not Monitor or type(agent.final_answer_checks) is not list
        or agent.final_answer_checks):
        _deny("native_turn_family_callback_registry_unsupported")
    metrics, controlled = registry._callbacks[ActionStep]
    if (getattr(metrics, "__self__", None) is not agent.monitor
        or getattr(metrics, "__func__", None) is not Monitor.update_metrics
        or controlled is not getattr(execution, "_controlled_callback", None)):
        _deny("native_turn_family_callback_registry_unsupported")
    execution._family_step_registry, execution._family_step_mapping = registry, registry._callbacks
    execution._family_step_callbacks = registry._callbacks[ActionStep]
    execution._family_step_metrics, execution._family_step_controlled = metrics, controlled
    execution._family_monitor, execution._family_final_checks = agent.monitor, agent.final_answer_checks
    execution._family_registry_method = _original_tool_method(registry, "callback")
    original_dispatch = registry.callback
    def original_step_dispatch(memory_step, **kwargs):
        from smolagents.agents import MultiStepAgent
        from src.agent.controlled_origin import _current_execution
        caller = inspect.currentframe().f_back
        if (caller.f_code is not MultiStepAgent._finalize_step.__code__
            or caller.f_locals.get("self") is not agent
            or caller.f_locals.get("memory_step") is not memory_step
            or kwargs.get("agent") is not agent or _current_execution.get() is not execution):
            _deny("native_turn_family_original_callback_missing")
        validate_original_execution(execution)
        return original_dispatch(memory_step, **kwargs)
    execution._family_step_dispatch = original_step_dispatch
    registry.callback = original_step_dispatch


def _validate_original_step_callbacks(execution, agent):
    from smolagents.memory import ActionStep
    registry = agent.step_callbacks
    if (registry is not execution._family_step_registry
        or registry._callbacks is not execution._family_step_mapping
        or tuple(registry._callbacks) != (ActionStep,)
        or registry._callbacks[ActionStep] is not execution._family_step_callbacks
        or len(registry._callbacks[ActionStep]) != 2
        or registry._callbacks[ActionStep][0] is not execution._family_step_metrics
        or registry._callbacks[ActionStep][1] is not execution._family_step_controlled
        or execution._controlled_callback is not execution._family_step_controlled
        or agent.monitor is not execution._family_monitor
        or agent.final_answer_checks is not execution._family_final_checks
        or agent.final_answer_checks):
        _deny("native_turn_family_original_callback_changed")
    from smolagents.memory import CallbackRegistry
    if (registry.callback is not execution._family_step_dispatch
        or execution._family_registry_method[0] is not registry
        or execution._family_registry_method[1] is not CallbackRegistry.callback):
        _deny("native_turn_family_original_callback_changed")


@contextmanager
def original_generic_callback(execution, callback, args):
    """Bind the existing REST/WS callback before its original SDK run."""
    from smolagents.agents import MultiStepAgent
    from src.agent.turn_execution import NativeTurnExecution
    from src.api.ws import _run_agent_to_queue
    caller = inspect.currentframe().f_back.f_back
    validate_original_execution(execution)
    agent = execution._family_agent
    bound = execution._family_agent_run
    if (caller.f_code is not NativeTurnExecution.run_callback.__code__
        or getattr(bound, "__self__", None) is not agent
        or getattr(bound, "__func__", None) is not MultiStepAgent.run
        or getattr(agent.run, "__func__", None) is not bound.__func__
        or getattr(agent.run, "__self__", None) is not agent
        or not ((getattr(callback, "__self__", None) is agent
                 and getattr(callback, "__func__", None) is bound.__func__)
                or (callback is _run_agent_to_queue and args and args[0] is agent))
        or getattr(execution, "_family_callback_started", False)):
        _deny("native_turn_family_original_callback_missing")
    execution._family_callback_started = True
    execution._family_callback_active = True
    execution._family_original_callback = callback
    try:
        yield
    finally:
        execution._family_callback_active = False


def _validate_registry_paths(paths):
    from smolagents.default_tools import FinalAnswerTool
    from src.tools.approval import ApprovalTool, AuthorityTool
    from src.tools.audit import AuditedTool
    from src.tools.clarify_tool import clarify
    for path in paths:
        if (path[-1] is not clarify and type(path[-1]) is not FinalAnswerTool
            and not any(type(item) in (AuditedTool, ApprovalTool, AuthorityTool) for item in path)):
            _deny("native_turn_family_tool_registry_unsupported")
        for item in path:
            for name in ("__call__", "forward"):
                _original_tool_method(item, name)


def _original_tool_method(tool, name):
    """A trusted path object must retain its class-defined execution method."""
    declared = inspect.getattr_static(type(tool), name)
    if isinstance(declared, staticmethod):
        function = declared.__func__
        actual = getattr(tool, name)
        if actual is not function:
            _deny("native_turn_family_original_registry_changed")
        return None, function
    if not inspect.isfunction(declared):
        _deny("native_turn_family_original_registry_changed")
    actual = getattr(tool, name)
    if getattr(actual, "__self__", None) is not tool or getattr(actual, "__func__", None) is not declared:
        _deny("native_turn_family_original_registry_changed")
    return tool, declared


def prepare_direct_family(execution):
    execution._family_seal, execution._family_steps = _SEAL, 0
    execution._family_witnesses, execution._family_calls = {}, []


class _OriginalModelCall:
    __slots__ = ("execution", "producer", "caller", "operation", "seal", "finished", "bridge_proof")
    def __init__(self, execution, producer, caller):
        self.execution, self.producer, self.caller = execution, producer, caller
        self.operation, self.seal = None, _SEAL
        self.finished = False
        self.bridge_proof = None


@contextmanager
def _original_model_call(execution, producer, caller):
    from src.agent.controlled_origin import _current_execution
    validate_original_execution(execution)
    if _current_execution.get() is not execution or _current_call.get() is not None:
        _deny("native_turn_family_nested_model_call")
    if not getattr(execution, "_family_initialized", False):
        _deny("native_turn_family_initial_receipt_missing")
    if len(execution._family_calls) >= execution._family_steps + 1:
        _deny("native_turn_family_budget_exceeded")
    call = _OriginalModelCall(execution, producer, caller)
    execution._family_calls.append(call)
    token = _current_call.set(call)
    try:
        yield
    finally:
        call.finished = True
        _current_call.reset(token)


def original_direct_completion(callback, *args, **kwargs):
    from src.agent.controlled_origin import _current_execution
    from src.agent.direct_chat import run_direct_local_chat
    execution = _current_execution.get()
    if execution is None:
        return callback(*args, **kwargs)
    # This private callback is passed to to_thread by that exact captured
    # direct owner, whose context is established before starting the worker.
    if (execution.admission.native_route != "direct_turn"
        or execution._family_direct_callback is not callback
        or execution._family_direct_producer is not run_direct_local_chat):
        _deny("native_turn_family_direct_callback_changed")
    with _original_model_call(execution, callback, execution._family_direct_frame):
        return callback(*args, **kwargs)


@contextmanager
def original_direct_stream(callback):
    from src.agent.controlled_origin import _current_execution
    from src.agent.direct_chat import stream_direct_local_chat
    execution = _current_execution.get()
    if execution is None:
        yield
        return
    caller = inspect.currentframe().f_back.f_back
    if caller.f_code is not stream_direct_local_chat.__code__ or execution.admission.native_route != "direct_turn":
        _deny("native_turn_family_direct_callback_changed")
    if getattr(execution, "_family_direct_callback", callback) is not callback:
        _deny("native_turn_family_direct_callback_changed")
    execution._family_direct_callback = callback
    with _original_model_call(execution, callback, caller):
        yield


def capture_direct_completion(callback):
    from src.agent.controlled_origin import _current_execution
    from src.agent.direct_chat import run_direct_local_chat
    execution = _current_execution.get()
    if execution is None:
        return callback
    caller = inspect.currentframe().f_back
    if caller.f_code is not run_direct_local_chat.__code__ or execution.admission.native_route != "direct_turn":
        _deny("native_turn_family_direct_callback_changed")
    if getattr(execution, "_family_direct_callback", callback) is not callback:
        _deny("native_turn_family_direct_callback_changed")
    execution._family_direct_callback, execution._family_direct_producer = callback, run_direct_local_chat
    execution._family_direct_frame = caller
    return original_direct_completion


def bind_original_operation(handle):
    """Called only by the actual broker after its existing owner admission."""
    from src.agent.controlled_origin import _current_execution
    from src.model_fabric.accounting import DurableInferenceBrokerMixin
    execution = _current_execution.get()
    call = _current_call.get()
    if execution is None and call is None:
        return None
    caller = inspect.currentframe().f_back
    if (execution is None or type(call) is not _OriginalModelCall or call.seal is not _SEAL
        or call.execution is not execution or call.operation is not None
        or caller.f_code not in {DurableInferenceBrokerMixin.execute.__code__, DurableInferenceBrokerMixin.execute_sync.__code__,
                                DurableInferenceBrokerMixin.stream.__code__}):
        _deny("native_turn_family_original_call_missing")
    validate_original_execution(execution)
    witness = NativeOperationWitness(execution, call, handle, seal=_SEAL)
    if witness.operation_id in execution._family_witnesses:
        _deny("native_turn_family_duplicate_operation")
    if len(execution._family_witnesses) >= execution._family_steps + 1:
        _deny("native_turn_family_budget_exceeded")
    call.operation = witness
    execution._family_witnesses[witness.operation_id] = witness
    return witness


def require_original_accounting(enabled):
    from src.agent.controlled_origin import _current_execution
    execution = _current_execution.get()
    if execution is None:
        return
    call = _current_call.get()
    if (enabled is not True or type(call) is not _OriginalModelCall or call.execution is not execution
        or call.seal is not _SEAL or call.operation is not None or call.finished):
        _deny("native_turn_family_original_call_missing")
    validate_original_execution(execution)
    if not _original_producer_stack(call):
        proof = call.bridge_proof
        if type(proof) is not _OriginalBridgeProof or proof.call is not call or proof.seal is not _SEAL:
            _deny("native_turn_family_indirect_model_call")


def _original_producer_stack(call):
    from smolagents.models import Model
    expected_model = getattr(call.execution, "_family_model", None)
    producer = getattr(call.producer, "__func__", call.producer)
    code = getattr(producer, "__code__", None)
    present = False
    frame = inspect.currentframe().f_back
    for _ in range(96):
        if frame is None:
            return present
        instance = frame.f_locals.get("self")
        if isinstance(instance, Model) and frame.f_code.co_name in {"generate", "generate_stream"}:
            if instance is not expected_model:
                _deny("native_turn_family_indirect_model_call")
        if frame.f_code is code:
            if expected_model is not None and instance is not expected_model:
                _deny("native_turn_family_indirect_model_call")
            present = True
        frame = frame.f_back
    _deny("native_turn_family_producer_stack_bounds")


class _OriginalBridgeProof:
    __slots__ = ("call", "seal")
    def __init__(self, call, seal):
        self.call, self.seal = call, seal


def verify_original_inference_bridge():
    """Called by the original thread bridge before it loses callback frames."""
    from src.model_fabric.execution import _run_awaitable_sync
    call = _current_call.get()
    if call is None:
        return
    if (type(call) is not _OriginalModelCall or call.seal is not _SEAL or call.finished
        or inspect.currentframe().f_back.f_code is not _run_awaitable_sync.__code__
        or not _original_producer_stack(call)):
        _deny("native_turn_family_indirect_model_call")
    call.bridge_proof = _OriginalBridgeProof(call, _SEAL)


def require_original_tool(tool):
    from src.agent.controlled_origin import _current_execution
    from src.tools.clarify_tool import clarify
    execution = _current_execution.get()
    if execution is None:
        return
    validate_original_execution(execution)
    if execution.admission.native_route != "generic_turn":
        _deny("native_turn_family_executable_tool_unsupported")
    paths = [path for path in execution._family_tool_paths if any(item is tool for item in path)]
    if len(paths) != 1 or paths[0][-1] is not clarify:
        _deny("native_turn_family_executable_tool_unsupported")


def original_family_failure(exception, execution=None):
    from src.agent.controlled_origin import _current_execution
    execution = execution or _current_execution.get()
    failure = getattr(execution, "_family_failure_exception", None)
    current = exception
    for _ in range(64):
        if current is None:
            return exception
        if failure is not None and current is failure:
            return failure
        current = current.__cause__
    return exception


async def assert_family_owner_readback(repository, db, run, payload, native_execution=None):
    validate_family_binding(payload, run)
    if native_execution is not None:
        validate_original_execution(native_execution)
        if native_execution.worker is None or not native_execution.worker.done() or native_execution.worker.cancelled():
            _deny("native_turn_family_callback_completion_unproven")
        if (native_execution.admission.native_route == "generic_turn"
            and (not getattr(native_execution, "_family_callback_started", False)
                 or getattr(native_execution, "_family_callback_active", False))):
            _deny("native_turn_family_callback_completion_unproven")
        calls = native_execution._family_calls
        if (native_execution.admission.native_route == "direct_turn"
            and (len(calls) != 1 or calls[0].producer is not getattr(native_execution, "_family_direct_callback", None))):
            _deny("native_turn_family_callback_completion_unproven")
        if (any(not call.finished or type(call.operation) is not NativeOperationWitness
            or not call.operation.consumed for call in calls)
            or [call.operation.operation_id for call in calls] != [item["operation_id"] for item in payload["operations"]]):
            _deny("native_turn_family_owner_reference_missing")
    if not payload["operations"]:
        return
    from pathlib import Path
    from config.settings import settings
    from src.workflows.job_runtime import _job_has_unsafe_effects, _effect_ledger_or_raise
    from src.workflows.inference_accounting import _continuity_lock
    account, rows = await repository._accounting_rows(db)
    with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
        repository._assert_accounting_continuity(workspace, account, rows)
    by_id = {row.operation_id: row for row in rows}
    for operation in payload["operations"]:
        row = by_id.get(operation["operation_id"])
        if row is None or row.job_id != operation["job_id"] or _reservation_digest(row) != operation["reservation_binding_digest"]:
            _deny("native_turn_family_owner_binding_changed")
        owner_run = await repository._fetch(db, row.job_id)
        validate_family_operation_reference(operation, owner_run, row)
        if (owner_run.attempt_count != operation["attempt_count"] or owner_run.fencing_token != operation["fencing_token"]
            or owner_run.owner_principal_id != operation["owner_id"] or owner_run.job_kind != "model_inference_ephemeral_v1"):
            _deny("native_turn_family_owner_binding_changed")
        if (row.state != "settled" or type(row.actual_cost_microusd) is not int or row.actual_cost_microusd < 0
            or owner_run.status != "succeeded" or row.actual_cost_microusd > row.bound_microusd
            or owner_run.lease_owner is not None or owner_run.lease_expires_at is not None):
            _deny("native_turn_family_cost_liability")
        receipts = _effect_ledger_or_raise(owner_run.effect_receipts_json)
        if _job_has_unsafe_effects(receipts):
            _deny("native_turn_family_unknown_external_effect")
        if not any(item.get("receipt_kind") == "readback" and item.get("effect_type") == "inference_accounting"
            and item.get("status") == "succeeded"
            and item.get("target_path") == "inference_accounting:" + row.operation_id
            and item.get("target_digest") == hashlib.sha256(str(row.actual_cost_microusd).encode()).hexdigest()
            and item.get("details", {}).get("verified") is True
            and item.get("details", {}).get("operation_id") == row.operation_id
            and item.get("details", {}).get("actual_cost_microusd") == row.actual_cost_microusd for item in receipts):
            _deny("native_turn_family_owner_readback_missing")
