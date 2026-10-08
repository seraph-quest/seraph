"""Private inert origin of canonical controlled outcomes; never effect authority."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path

_current_execution = ContextVar("native_controlled_execution", default=None)


@dataclass(frozen=True, repr=False)
class ApprovalOriginSnapshot:
    approval_id: str
    tool_name: str
    fingerprint: str
    owner_principal_id: str
    operator_session_id: str
    conversation_id: str
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True, repr=False)
class CanonicalControlledOrigin:
    kind: str
    issued_exception: object
    producer: object
    execution: object
    approval_snapshot: ApprovalOriginSnapshot | None = None


def _utc(value):
    if not isinstance(value, datetime):
        raise ValueError("controlled_origin_datetime_invalid")
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@contextmanager
def original_execution_context(execution):
    from src.agent.turn_execution import NativeTurnExecution
    if type(execution) is not NativeTurnExecution:
        raise ValueError("controlled_origin_execution_invalid")
    execution.remaining()
    token = _current_execution.set(execution)
    try:
        yield
    finally:
        _current_execution.reset(token)


def issue_controlled_origin(exception, *, producer, approval_row=None):
    execution = _current_execution.get()
    if execution is None:
        return None  # Legacy producers preserve ordinary exceptions.
    from src.agent.exceptions import ClarificationRequired
    from src.approval.exceptions import ApprovalRequired
    execution.remaining()
    if type(exception) is ApprovalRequired and approval_row is not None:
        snapshot = ApprovalOriginSnapshot(approval_row.id, approval_row.tool_name,
            approval_row.fingerprint, approval_row.owner_principal_id,
            approval_row.operator_session_id, approval_row.conversation_id,
            _utc(approval_row.expires_at), _utc(approval_row.created_at))
        origin = CanonicalControlledOrigin("approval", exception, producer, execution, snapshot)
    elif type(exception) is ClarificationRequired and approval_row is None:
        origin = CanonicalControlledOrigin("clarification", exception, producer, execution)
    else:
        raise ValueError("controlled_origin_kind_invalid")
    exception._canonical_controlled_origin = origin
    return origin


def _frames(traceback):
    result = []
    while traceback is not None:
        if len(result) >= 32:
            raise ValueError("controlled_origin_traceback_bounds")
        result.append(traceback.tb_frame)
        traceback = traceback.tb_next
    return result


def _registered_path(tool):
    from src.tools.approval import ApprovalTool, AuthorityTool
    from src.tools.audit import AuditedTool
    from src.tools.secret_ref_tools import SecretRefResolvingTool
    allowed = (ApprovalTool, AuthorityTool, AuditedTool, SecretRefResolvingTool)
    path = []
    for _ in range(5):
        path.append(tool)
        if type(tool) not in allowed:
            return path
        tool = tool.wrapped_tool
    raise ValueError("controlled_origin_wrapper_bounds")


def validate_controlled_origin(exception, *, native_execution):
    from src.agent.turn_execution import NativeTurnExecution
    if type(native_execution) is not NativeTurnExecution:
        raise ValueError("controlled_origin_execution_invalid")
    native_execution.remaining()
    return _validate_controlled_origin_identity(exception, native_execution=native_execution)


def validate_controlled_cleanup_origin(exception, *, cleanup_witness):
    """Only the exact original finished source may attest a stopped origin."""
    from src.agent.native_turn_family import validate_original_cleanup
    execution = getattr(cleanup_witness, "execution", None)
    worker = getattr(cleanup_witness, "worker", None)
    if validate_original_cleanup(execution, worker) is not cleanup_witness:
        raise ValueError("controlled_origin_cleanup_unproven")
    if cleanup_witness.exception is not exception:
        raise ValueError("controlled_origin_cleanup_exception_changed")
    return _validate_controlled_origin_identity(exception, native_execution=execution)


def _validate_controlled_origin_identity(exception, *, native_execution):
    from smolagents import ToolCallingAgent
    from smolagents.utils import AgentToolExecutionError
    from src.agent.turn_execution import NativeTurnExecution
    from src.agent.exceptions import ClarificationRequired
    from src.approval.exceptions import ApprovalRequired
    from src.tools.approval import ApprovalTool
    from src.tools.clarify_tool import clarify
    if type(native_execution) is not NativeTurnExecution:
        raise ValueError("controlled_origin_execution_invalid")
    origin = getattr(exception, "_canonical_controlled_origin", None)
    if (type(origin) is not CanonicalControlledOrigin or origin.execution is not native_execution
        or origin.issued_exception is not exception):
        raise ValueError("controlled_origin_missing")
    agent = getattr(native_execution, "_controlled_agent", None)
    registry = getattr(native_execution, "_controlled_registry", None)
    snapshot = getattr(native_execution, "_controlled_registry_snapshot", ())
    error = getattr(native_execution, "_controlled_error", None)
    if (type(agent) is not ToolCallingAgent or agent.tools is not registry
        or len(registry) != len(snapshot)
        or any(registry.get(name) is not tool for name, tool in snapshot)
        or type(error) is not AgentToolExecutionError or error.__cause__ is not exception):
        raise ValueError("controlled_origin_registered_call_invalid")
    sdk_frames = [frame for frame in _frames(error.__traceback__)
        if frame.f_code is ToolCallingAgent.execute_tool_call.__code__]
    if len(sdk_frames) != 1:
        raise ValueError("controlled_origin_sdk_call_ambiguous")
    values = sdk_frames[0].f_locals
    name, tool = values.get("tool_name"), values.get("tool")
    if (values.get("self") is not agent or values.get("is_managed_agent") is not False
        or registry.get(name) is not tool or values.get("available_tools", {}).get(name) is not tool):
        raise ValueError("controlled_origin_indirect_call_denied")
    frames = _frames(exception.__traceback__)
    if not frames:
        raise ValueError("controlled_origin_raise_missing")
    frame = frames[-1]
    if (frame.f_locals.get("issued_exception") is not exception
        or frame.f_locals.get("producer") is not origin.producer
        or frame.f_locals.get("origin") is not origin):
        raise ValueError("controlled_origin_issue_identity_invalid")
    path = _registered_path(tool)
    if type(exception) is ApprovalRequired:
        if (origin.kind != "approval" or type(origin.producer) is not ApprovalTool
            or frame.f_code is not ApprovalTool.__call__.__code__
            or not any(item is origin.producer for item in path)
            or type(origin.approval_snapshot) is not ApprovalOriginSnapshot
            or exception.approval_id != origin.approval_snapshot.approval_id
            or exception.tool_name != origin.approval_snapshot.tool_name):
            raise ValueError("controlled_origin_approval_invalid")
    elif type(exception) is ClarificationRequired:
        if (origin.kind != "clarification" or origin.producer is not clarify
            or path[-1] is not clarify or origin.approval_snapshot is not None
            or frame.f_code is not clarify.forward.__wrapped__.__code__):
            raise ValueError("controlled_origin_clarification_invalid")
    else:
        raise ValueError("controlled_origin_kind_invalid")
    return origin


def install_controlled_callback(execution, agent):
    from smolagents import ToolCallingAgent
    from smolagents.memory import ActionStep
    from smolagents.utils import AgentToolExecutionError
    from src.agent.turn_execution import NativeTurnBlocked
    import smolagents.agents
    import smolagents.tools
    # The private traceback contract is supported only by the reviewed locked SDK.
    for module, expected in ((smolagents.agents, "2a3b6b09276204be9d03f88fea549fd06bf6811351c3f69597e1faf405e3dbf1"),
        (smolagents.tools, "1865faa13b1b8044b4b11dadfc73f8fe0c3470cd6c0c5ea9e6b325d4427e8d98")):
        if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != expected:
            raise NativeTurnBlocked("native_turn_controlled_sdk_unsupported")
    if type(agent) is not ToolCallingAgent:
        raise NativeTurnBlocked("native_turn_controlled_agent_unsupported")
    execution._controlled_agent = agent
    execution._controlled_registry = agent.tools
    execution._controlled_registry_snapshot = tuple(agent.tools.items())
    execution._controlled_error = None
    def callback(step, **kwargs):
        try:
            execution.remaining()
        except asyncio.TimeoutError as exc:
            raise NativeTurnBlocked("native_turn_original_deadline_expired") from exc
        error = getattr(step, "error", None)
        if type(error) is not AgentToolExecutionError:
            return
        cause = error.__cause__
        family_failure = getattr(execution, "_family_failure_exception", None)
        if family_failure is not None and cause is family_failure:
            # The actual pre-execution owner gate already denied the effect.
            # Propagate only its privately retained identical exception.
            raise family_failure
        from src.agent.exceptions import ClarificationRequired
        from src.approval.exceptions import ApprovalRequired
        if type(cause) not in (ClarificationRequired, ApprovalRequired):
            # Reject nested controlled causes; ordinary tool errors retain SDK behavior.
            nested = cause
            for _ in range(4):
                if nested is None:
                    return
                if type(nested) in (ClarificationRequired, ApprovalRequired):
                    raise NativeTurnBlocked("native_turn_controlled_indirect_outcome")
                nested = nested.__cause__
            if nested is not None:
                raise NativeTurnBlocked("native_turn_controlled_indirect_outcome")
            return
        execution._controlled_error = error
        try:
            validate_controlled_origin(cause, native_execution=execution)
        except (ValueError, asyncio.TimeoutError) as exc:
            raise NativeTurnBlocked("native_turn_controlled_origin_unproven") from exc
        raise cause
    execution._controlled_callback = callback
    agent.step_callbacks.register(ActionStep, callback)
