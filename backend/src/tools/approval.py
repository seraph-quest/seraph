"""Approval wrappers for high-risk tool invocations."""

import asyncio
import json
import time
from typing import Any
from uuid import uuid4

from smolagents import Tool

from src.approval.exceptions import ApprovalRequired
from src.approval.identity import (
    approval_owner_operator_session_id,
    build_approval_owner_details,
)
from src.approval.repository import approval_repository, fingerprint_tool_call
from src.approval.runtime import (
    get_current_approval_mode,
    get_current_session_id,
    get_current_trust_principal,
)
from src.auth.cancellation import assert_runtime_not_revoked
from src.audit.formatting import format_tool_call_summary, redact_for_audit
from src.extensions.capability_execution import (
    _ADOPTED_CAPABILITIES,
    build_capability_request,
    current_capability_execution_host,
)
from src.security.trust_contract import (
    AuthorityGrant,
    ContentOrigin,
    DestinationClass,
    EgressClass,
    NO_OBJECT,
    NO_RESOURCE_LIMITS,
    NO_SECRET_SCOPE,
    NO_TRANSFORMATION,
    PrincipalType,
    TrustDestination,
    TrustOperation,
    TrustPrincipal,
    TrustProvenance,
    TrustRequest,
    TrustResource,
    authority_scope_digest,
    canonical_digest,
    evaluate_trust,
)
from src.tools.policy import (
    get_tool_approval_behavior,
    get_tool_execution_boundaries,
    get_tool_risk_level,
)


def _run_async(coro):
    return asyncio.run(coro)


def _canonical_metadata_digest(value: Any) -> str:
    normalized = json.loads(json.dumps(value, sort_keys=True, default=str))
    return canonical_digest(normalized)


def _tool_approval_context(tool: Tool, arguments: dict[str, Any]) -> dict[str, Any] | None:
    hook = getattr(tool, "get_approval_context", None)
    if not callable(hook):
        return None
    payload = hook(arguments)
    if isinstance(payload, dict) and payload:
        return payload
    return None


def _secret_ref_fields(arguments: dict[str, Any]) -> list[str]:
    def _contains_ref(value: Any) -> bool:
        if isinstance(value, str):
            return "secret://" in value
        if isinstance(value, dict):
            return any(_contains_ref(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(_contains_ref(item) for item in value)
        return False

    return sorted(str(key) for key, value in arguments.items() if _contains_ref(value))


def _capability_authority_request(
    *,
    session_id: str | None,
    principal: TrustPrincipal | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> TrustRequest:
    safe_arguments = redact_for_audit(arguments)
    data_digest = _canonical_metadata_digest(safe_arguments)
    secret_ref_fields = _secret_ref_fields(arguments)
    effective_principal = principal
    if effective_principal is None:
        effective_principal = TrustPrincipal(
            principal_id="unbound-runtime",
            principal_type=PrincipalType.ANONYMOUS,
            authenticated=False,
        )
    destination = TrustDestination(
        destination_id="seraph-capability-runtime",
        destination_class=DestinationClass.LOCAL_RUNTIME,
    )
    resource = TrustResource(
        resource_type="capability",
        resource_id=f"capability:{canonical_digest(tool_name)[:24]}",
        object_digest=NO_OBJECT,
    )
    request_session_id = session_id or ""
    request_job_id = effective_principal.job_id
    return TrustRequest(
        principal=effective_principal,
        provenance=(
            TrustProvenance(
                origin=ContentOrigin.PROVIDER_OUTPUT,
                source_id="provider-output",
                data_digest=data_digest,
                egress_class=EgressClass.LOCAL_ONLY,
                instruction_authority=False,
            ),
        ),
        destination=destination,
        operation=TrustOperation.CAPABILITY_CALL,
        required_grant=AuthorityGrant.CAPABILITY_EXECUTE,
        capability_id=tool_name,
        capability_version="legacy",
        data_digest=data_digest,
        secret_scope_digest=(
            canonical_digest({"secret_ref_fields": secret_ref_fields})
            if secret_ref_fields
            else NO_SECRET_SCOPE
        ),
        resource_limits_digest=NO_RESOURCE_LIMITS,
        transformation_digest=NO_TRANSFORMATION,
        authority_scope_digest=authority_scope_digest(
            required_grant=AuthorityGrant.CAPABILITY_EXECUTE,
            capability_id=tool_name,
            destination=destination,
            resource=resource,
        ),
        resource=resource,
        session_id=request_session_id,
        job_id=request_job_id,
        request_id=f"request:{uuid4().hex}",
        attempt_id=f"attempt:{uuid4().hex}",
        replay_id=f"replay:{uuid4().hex}",
        decision_expires_at=time.time() + 60.0,
        egress_class=EgressClass.LOCAL_ONLY,
    )


def _require_capability_authority(
    *,
    session_id: str | None,
    principal: TrustPrincipal | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    """Fail closed before a governed tool can reach its wrapped implementation."""
    if session_id is None or principal is None:
        raise PermissionError(
            f"Tool '{tool_name}' is blocked because runtime authority is unavailable."
        )
    authority_decision = evaluate_trust(
        _capability_authority_request(
            session_id=session_id,
            principal=principal,
            tool_name=tool_name,
            arguments=arguments,
        )
    )
    if not authority_decision.allowed:
        raise PermissionError(
            f"Tool '{tool_name}' is blocked because runtime authority is unavailable "
            f"({authority_decision.reason_code})."
        )


def require_capability_authority(
    *,
    session_id: str | None,
    principal: TrustPrincipal | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    """Apply the shared capability boundary to a non-Tool execution adapter.

    Some public or scheduled adapters return structured data rather than a
    smolagents ``Tool`` instance.  They still need the exact same fail-closed
    authority decision before reaching an external or stateful implementation.
    The arguments are used only for the canonical digest; denial messages stay
    content-free.
    """
    assert_runtime_not_revoked()
    _require_capability_authority(
        session_id=session_id,
        principal=principal,
        tool_name=tool_name,
        arguments=arguments,
    )
    # A revocation guard may change while the trust decision is being made;
    # mirror AuthorityTool's final check before the adapter crosses its
    # external or stateful execution boundary.
    assert_runtime_not_revoked()


class AuthorityTool(Tool):
    """Require the shared capability decision without creating an approval."""

    skip_forward_signature_validation = True

    def __init__(self, wrapped_tool: Tool):
        super().__init__()
        self.wrapped_tool = wrapped_tool
        self.name = str(getattr(wrapped_tool, "name", "wrapped_tool"))
        description = getattr(wrapped_tool, "description", "")
        self.description = description if isinstance(description, str) else ""
        inputs = getattr(wrapped_tool, "inputs", {})
        self.inputs = inputs if isinstance(inputs, dict) else {}
        output_type = getattr(wrapped_tool, "output_type", "string")
        self.output_type = output_type if isinstance(output_type, str) else "string"
        output_schema = getattr(wrapped_tool, "output_schema", None)
        self.output_schema = output_schema if isinstance(output_schema, dict) else None
        self.is_initialized = True

    def forward(self, *args, **kwargs):
        # ``Tool.forward`` is a public escape hatch used by a few internal
        # adapters. Route it through the same authority and adopted-capability
        # boundary as normal calls so a caller cannot bypass the final host by
        # selecting a different Tool entry point.
        return self.__call__(*args, **kwargs)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        assert_runtime_not_revoked()
        session_id = get_current_session_id()
        principal = get_current_trust_principal()
        arguments = self._normalize_invocation(args, kwargs)
        _require_capability_authority(
            session_id=session_id,
            principal=principal,
            tool_name=self.name,
            arguments=arguments,
        )
        # Revocation can change while the trust decision is being evaluated;
        # recheck immediately before crossing the wrapped execution boundary.
        assert_runtime_not_revoked()
        return _invoke_adopted_tool(
            wrapped_tool=self.wrapped_tool,
            tool_name=self.name,
            arguments=arguments,
            args=args,
            kwargs=kwargs,
            sanitize_inputs_outputs=sanitize_inputs_outputs,
            principal=principal,
            session_id=session_id,
        )

    def get_approval_context(self, arguments: dict[str, Any]) -> dict[str, Any] | None:
        hook = getattr(self.wrapped_tool, "get_approval_context", None)
        if callable(hook):
            return hook(arguments)
        return None

    def _normalize_invocation(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if len(args) == 1 and not kwargs and isinstance(args[0], dict):
            return args[0]
        if kwargs:
            return kwargs
        input_names = list(self.inputs.keys())
        return {
            name: args[idx]
            for idx, name in enumerate(input_names)
            if idx < len(args)
        }


_NON_GOVERNED_BOUNDARIES = frozenset({"conversation", "conversation_state"})


def _requires_authority_gate(tool: Tool, *, is_mcp: bool) -> bool:
    """Gate executable/data-bound tools while preserving pure conversation helpers."""
    if is_mcp:
        return True
    boundaries = get_tool_execution_boundaries(tool.name, tool=tool)
    return bool(boundaries) and any(
        boundary not in _NON_GOVERNED_BOUNDARIES for boundary in boundaries
    )


class ApprovalTool(Tool):
    """Tool wrapper that pauses high-risk actions pending approval."""

    skip_forward_signature_validation = True

    def __init__(
        self,
        wrapped_tool: Tool,
        *,
        force_approval: bool = False,
        is_mcp: bool = False,
        risk_level_override: str | None = None,
    ):
        super().__init__()
        self.wrapped_tool = wrapped_tool
        self.force_approval = force_approval
        self.is_mcp = is_mcp
        self.risk_level_override = risk_level_override if isinstance(risk_level_override, str) else None
        self.name = str(getattr(wrapped_tool, "name", "wrapped_tool"))
        description = getattr(wrapped_tool, "description", "")
        self.description = description if isinstance(description, str) else ""
        inputs = getattr(wrapped_tool, "inputs", {})
        self.inputs = inputs if isinstance(inputs, dict) else {}
        output_type = getattr(wrapped_tool, "output_type", "string")
        self.output_type = output_type if isinstance(output_type, str) else "string"
        output_schema = getattr(wrapped_tool, "output_schema", None)
        self.output_schema = output_schema if isinstance(output_schema, dict) else None
        self.is_initialized = True

    def forward(self, *args, **kwargs):
        return self.__call__(*args, **kwargs)

    def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
        assert_runtime_not_revoked()
        approval_mode = get_current_approval_mode()
        session_id = get_current_session_id()
        arguments = self._normalize_invocation(args, kwargs)
        principal = get_current_trust_principal()
        _require_capability_authority(
            session_id=session_id,
            principal=principal,
            tool_name=self.name,
            arguments=arguments,
        )
        assert_runtime_not_revoked()

        approval_behavior = "always" if self.force_approval else get_tool_approval_behavior(self.name, is_mcp=self.is_mcp)
        if approval_behavior != "always" and approval_mode != "high_risk":
            return _invoke_adopted_tool(
                wrapped_tool=self.wrapped_tool,
                tool_name=self.name,
                arguments=arguments,
                args=args,
                kwargs=kwargs,
                sanitize_inputs_outputs=sanitize_inputs_outputs,
                principal=principal,
                session_id=session_id,
            )

        approval_context = _tool_approval_context(self.wrapped_tool, arguments)
        fingerprint = fingerprint_tool_call(
            self.name,
            arguments,
            approval_context=approval_context,
        )
        consumed_approval = _run_async(
            approval_repository.consume_approved(
                session_id=session_id,
                tool_name=self.name,
                fingerprint=fingerprint,
                owner_operator_session_id=approval_owner_operator_session_id(
                    session_id=session_id,
                    principal=principal,
                ),
                owner_principal_id=(
                    str(principal.principal_id).strip()
                    if principal is not None and principal.principal_id
                    else None
                ),
                approval_binding=approval_context,
            )
        )
        if consumed_approval:
            assert_runtime_not_revoked()
            approval_binding = consumed_approval if isinstance(consumed_approval, dict) else None
            return _invoke_adopted_tool(
                wrapped_tool=self.wrapped_tool,
                tool_name=self.name,
                arguments=arguments,
                args=args,
                kwargs=kwargs,
                sanitize_inputs_outputs=sanitize_inputs_outputs,
                principal=principal,
                session_id=session_id,
                approval_id=(
                    str(consumed_approval.get("approval_id") or "")
                    if isinstance(consumed_approval, dict)
                    else ""
                ),
                approval_digest=(
                    str(consumed_approval.get("fingerprint") or "")
                    if isinstance(consumed_approval, dict)
                    else ""
                ),
                approval_binding=approval_binding,
            )

        summary = format_tool_call_summary(self.name, arguments, set())
        risk_level = self.risk_level_override or get_tool_risk_level(self.name, is_mcp=self.is_mcp)
        # Pending approvals are durable capabilities, so they need a bounded
        # decision window even when the wrapped tool did not provide one.
        approval_expires_at = time.time() + 5 * 60.0
        request = _run_async(
            approval_repository.get_or_create_pending(
                session_id=session_id,
                tool_name=self.name,
                risk_level=risk_level,
                summary=summary,
                fingerprint=fingerprint,
                details={
                    "arguments": redact_for_audit(arguments),
                    **build_approval_owner_details(session_id=session_id, principal=principal),
                    **({"approval_context": approval_context} if approval_context else {}),
                    "approval_expires_at": approval_expires_at,
                    "expires_at": approval_expires_at,
                },
            )
        )
        raise ApprovalRequired(
            approval_id=request.id,
            session_id=session_id,
            tool_name=self.name,
            risk_level=request.risk_level,
            summary=summary,
        )

    def _normalize_invocation(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if len(args) == 1 and not kwargs and isinstance(args[0], dict):
            return args[0]
        if kwargs:
            return kwargs
        input_names = list(self.inputs.keys())
        return {
            name: args[idx]
            for idx, name in enumerate(input_names)
            if idx < len(args)
        }


def _invoke_adopted_tool(
    *,
    wrapped_tool: Tool,
    tool_name: str,
    arguments: dict[str, Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    sanitize_inputs_outputs: bool,
    principal: TrustPrincipal | None,
    session_id: str | None,
    approval_id: str = "",
    approval_digest: str = "",
    approval_binding: dict[str, Any] | None = None,
) -> Any:
    """Cross the durable host for adopted local filesystem/process tools."""
    if tool_name not in _ADOPTED_CAPABILITIES:
        return wrapped_tool(
            *args,
            sanitize_inputs_outputs=sanitize_inputs_outputs,
            **kwargs,
        )
    if not _is_native_adopted_tool(wrapped_tool, tool_name):
        raise PermissionError(
            f"Tool '{tool_name}' is blocked because its adapter is not registered as a native capability."
        )
    if principal is None or session_id is None:
        # The authority check above normally catches this; keeping the guard
        # here makes the host helper safe when called by a future wrapper.
        raise PermissionError(f"Tool '{tool_name}' is blocked because runtime authority is unavailable.")
    request = build_capability_request(
        capability_id=tool_name,
        arguments=arguments,
        owner_principal_id=principal.principal_id,
        session_id=session_id,
        job_id=principal.job_id,
        approval_id=approval_id,
        approval_digest=approval_digest,
        approval_binding=approval_binding,
    )
    host = current_capability_execution_host()
    _, receipt = host._execute_adopted(request)  # noqa: SLF001 - wrapper-owned adapter hook
    if receipt.state != "succeeded":
        raise PermissionError(f"Tool '{tool_name}' execution did not complete ({receipt.state}).")
    return receipt.result


def _is_native_adopted_tool(tool: Tool, tool_name: str) -> bool:
    """Recognize the exact bundled adapter through audit/secret wrappers."""
    from src.tools.filesystem_tool import (
        apply_workspace_patch,
        preview_workspace_patch,
        read_file,
        write_file,
    )
    from src.tools.process_tools import (
        list_processes,
        read_process_output,
        run_command,
        start_process,
        stop_process,
    )

    native = {
        "read_file": read_file,
        "write_file": write_file,
        "preview_workspace_patch": preview_workspace_patch,
        "apply_workspace_patch": apply_workspace_patch,
        "run_command": run_command,
        "start_process": start_process,
        "list_processes": list_processes,
        "read_process_output": read_process_output,
        "stop_process": stop_process,
    }.get(tool_name)
    if native is None:
        return False
    current: object | None = tool
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if current is native:
            return True
        current = getattr(current, "wrapped_tool", None)
    return False


def wrap_tools_for_approval(
    tools: list[Tool],
    *,
    treat_all_as_mcp: bool = False,
    risk_overrides: dict[str, str] | None = None,
) -> list[Tool]:
    """Wrap high-risk tools with approval checkpoints."""
    wrapped: list[Tool] = []
    for tool in tools:
        is_mcp = treat_all_as_mcp or tool.name.startswith("mcp_")
        risk_level = (risk_overrides or {}).get(
            tool.name,
            get_tool_risk_level(tool.name, is_mcp=is_mcp),
        )
        approval_behavior = get_tool_approval_behavior(tool.name, is_mcp=is_mcp)
        if risk_level == "high" or approval_behavior == "always":
            wrapped.append(ApprovalTool(tool, is_mcp=is_mcp, risk_level_override=risk_level))
        elif _requires_authority_gate(tool, is_mcp=is_mcp):
            wrapped.append(AuthorityTool(tool))
        else:
            wrapped.append(tool)
    return wrapped


def wrap_tools_with_forced_approval(
    tools: list[Tool],
    *,
    treat_all_as_mcp: bool = False,
    risk_overrides: dict[str, str] | None = None,
) -> list[Tool]:
    """Wrap a tool collection so every invocation pauses for approval."""
    wrapped: list[Tool] = []
    for tool in tools:
        wrapped.append(
            ApprovalTool(
                tool,
                force_approval=True,
                is_mcp=treat_all_as_mcp or tool.name.startswith("mcp_"),
                risk_level_override=(risk_overrides or {}).get(tool.name),
            )
        )
    return wrapped
