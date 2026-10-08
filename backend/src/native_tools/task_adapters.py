"""Typed general-task adapters over the current governed tool owners.

No connection, queue, host, grant or credential is created here. Lifecycle is
owned by the application; descriptor snapshots are data, never authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass

from smolagents import Tool
from src.approval.exceptions import ApprovalRequired

from src.work_board.contracts import ToolDescriptor


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class TaskToolApprovalBinding:
    descriptor_digest: str
    input_digest: str
    job_id: str
    fencing_token: int


_APPROVAL_FIELDS = (
    "approval_id", "session_id", "tool_name", "risk_level", "summary",
    "required_permissions", "local_host_execution_required", "executor_kind",
    "executor_profile", "executor_posture_digest", "preparation_ready",
    "execution_ready", "operator_visible", "expires_at",
)

_PRECONTACT_SEAL = object()
_CLOSURE_SEAL = object()


@dataclass(frozen=True)
class TaskToolClosureWitness:
    """Private evidence emitted after the original synchronous callback exits."""
    binding: TaskToolApprovalBinding
    outcome: str
    output_digest: str | None
    approval_id: str | None
    _seal: object


def verify_task_tool_closure(witness, *, binding, fencing_token):
    from src.work_board.contracts import GeneralTaskToolClosureV1, GeneralTaskNativeChildBindingV1
    if (type(witness) is not TaskToolClosureWitness or witness._seal is not _CLOSURE_SEAL
        or type(binding) is not GeneralTaskNativeChildBindingV1
        or witness.binding != TaskToolApprovalBinding(binding.descriptor_digest,
            binding.input_digest, binding.invocation_id, fencing_token)):
        raise PermissionError("original native callback closure witness required")
    return GeneralTaskToolClosureV1(original_binding_digest=_digest(binding.model_dump(mode="json")),
        invocation_id=binding.invocation_id, child_fence=fencing_token,
        descriptor_digest=binding.descriptor_digest, input_digest=binding.input_digest,
        outcome=witness.outcome, output_digest=witness.output_digest, approval_id=witness.approval_id)


@dataclass(frozen=True)
class _InvocationCompletion:
    output: object
    error: BaseException | None
    witness: TaskToolClosureWitness


class TaskToolInvocation:
    """Retains the original callback future; cancelling its waiter cannot close it."""
    def __init__(self, future):
        self._future = future

    @property
    def closed(self):
        return self._future.done() and not self._future.cancelled()

    @property
    def witness(self):
        if not self.closed:
            raise PermissionError("original native callback has not closed")
        return self._future.result().witness

    async def wait(self, *, timeout=None):
        waiting = asyncio.shield(self._future)
        completion = (await waiting if timeout is None
            else await asyncio.wait_for(waiting, timeout=timeout))
        if completion.error is not None:
            raise completion.error
        return completion.output


class TaskToolApprovalRequired(ApprovalRequired):
    """Existing wrapper approval with adapter-proven absence of tool contact."""

    def __init__(self, approval: ApprovalRequired, *, binding: TaskToolApprovalBinding,
                 _producer_seal=None):
        super().__init__(**{name: getattr(approval, name) for name in _APPROVAL_FIELDS})
        self._binding = binding
        self._producer_seal = _producer_seal

    @property
    def binding(self):
        return self._binding

    @property
    def precontact(self):
        return True


class _InvocationMarker(Tool):
    """One finite invocation-local proxy below the existing approval wrapper.

    Crossing this marker counts conservatively as contact, including entry
    into the existing audit/secret wrappers. Metadata hooks retain their exact
    existing payloads so approval fingerprints remain unchanged.
    """
    skip_forward_signature_validation = True

    def __init__(self, wrapped_tool):
        super().__init__()
        self.wrapped_tool = wrapped_tool
        self.name = wrapped_tool.name
        self.description = wrapped_tool.description
        self.inputs = wrapped_tool.inputs
        self.output_type = wrapped_tool.output_type
        self.output_schema = getattr(wrapped_tool, "output_schema", None)
        self.is_initialized = True
        self.contacted = False

    def __call__(self, *args, **kwargs):
        self.contacted = True
        return self.wrapped_tool(*args, **kwargs)

    def forward(self, *args, **kwargs):
        return self.__call__(*args, **kwargs)

    def get_approval_context(self, arguments):
        hook = getattr(self.wrapped_tool, "get_approval_context", None)
        return hook(arguments) if callable(hook) else None


class _MCPResultGuard(_InvocationMarker):
    """Bound the raw result before the existing audit/secret serializers."""
    def __call__(self, *args, **kwargs):
        from src.tools.mcp_manager import check_task_raw_output
        raw = super().__call__(*args, **kwargs)
        check_task_raw_output(raw)
        return raw

    def get_audit_call_payload(self, arguments):
        hook = getattr(self.wrapped_tool, "get_audit_call_payload", None)
        return hook(arguments) if callable(hook) else None

    def get_audit_result_payload(self, arguments, result):
        hook = getattr(self.wrapped_tool, "get_audit_result_payload", None)
        return hook(arguments, result) if callable(hook) else None

    def get_audit_failure_payload(self, arguments, error):
        hook = getattr(self.wrapped_tool, "get_audit_failure_payload", None)
        return hook(arguments, error) if callable(hook) else None


def _object(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": required or list(properties), "additionalProperties": False}


_PATH = {"type": "string", "minLength": 1, "maxLength": 1024}
_TEXT = {"type": "string", "maxLength": 60000}
_HASH = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
_TEXT_OUTPUT = _object({"content": _TEXT, "sha256": _HASH})
_NATIVE = {
    "read_file": (_object({"file_path": _PATH}), _TEXT_OUTPUT, ["workspace_read"], "workspace_readback.v1"),
    "write_file": (_object({"file_path": _PATH, "content": _TEXT}),
        _object({"file_path": _PATH, "content_sha256": _HASH,
                 "bytes_written": {"type": "integer", "minimum": 0, "maximum": 60000}}),
        ["workspace_write"], "workspace_write_readback.v1"),
    "web_search": (_object({"query": {"type": "string", "minLength": 1, "maxLength": 4096},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"]),
        _TEXT_OUTPUT, ["external_read"], "bounded_search_text.v1"),
    "browse_webpage": (_object({"url": {"type": "string", "minLength": 1,
        "maxLength": 4096, "pattern": "^https?://"},
        "action": {"type": "string", "enum": ["extract", "html"]}}, ["url"]),
        _TEXT_OUTPUT, ["external_read"], "bounded_browser_text.v1"),
}


class ToolRegistry:
    def __init__(self, *, mcp_runtime=None, extension_registry=None):
        self.mcp_runtime = mcp_runtime
        self.extension_registry = extension_registry
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def _entries(self):
        if not self.started:
            raise RuntimeError("task tool registry is inactive")
        from src.native_tools.registry import get_tool_metadata
        from src.tools.policy import get_task_policy_snapshot, is_tool_allowed
        from src.tools.filesystem_tool import read_file, write_file
        from src.tools.web_search_tool import web_search
        from src.tools.browser_tool import browse_webpage
        policy_snapshot = get_task_policy_snapshot()
        mode = policy_snapshot["tool_mode"]
        mcp_mode = policy_snapshot["mcp_mode"]
        entries = {}
        from src.work_board.document_preparation import descriptor as document_descriptor
        local_document = document_descriptor()
        if is_tool_allowed(local_document.tool_id, mode):
            entries[local_document.tool_id] = (local_document, None, False)
        for tool in (read_file, write_file, web_search, browse_webpage):
            if not is_tool_allowed(tool.name, mode):
                continue
            input_schema, output_schema, effects, verifier = _NATIVE[tool.name]
            policy = {"metadata": get_tool_metadata(tool.name), "policy": policy_snapshot}
            if tool.name in {"read_file", "write_file"}:
                from config.settings import settings
                policy["workspace"] = str(settings.workspace_dir)
            descriptor = ToolDescriptor(tool_id=tool.name, version="1", input_schema=input_schema,
                output_schema=output_schema, effects=effects, permissions=["capability_execute"],
                deadline=60, verifier=verifier, policy_digest=_digest(policy))
            entries[tool.name] = (descriptor, tool, False)
        if self.mcp_runtime is not None and self.extension_registry is not None and mcp_mode != "disabled":
            for descriptor, tool in self.mcp_runtime.task_tool_entries(self.extension_registry, mcp_mode):
                if descriptor.tool_id in entries:
                    raise ValueError("duplicate task tool identity")
                entries[descriptor.tool_id] = (descriptor, tool, True)
        return entries

    def descriptors(self):
        # Return detached models: callers cannot mutate the registry's schemas.
        return [ToolDescriptor.model_validate(item[0].model_dump(mode="json"))
                for _, item in sorted(self._entries().items())]

    def blocked_tools(self):
        """Visible exclusion receipts; configuration is never readiness proof."""
        active = self._entries()
        blocked = [{"tool_id": name, "reason": "tool_policy_denied"}
                   for name in _NATIVE if name not in active]
        if self.mcp_runtime is not None:
            from src.tools.policy import get_tool_source_context
            for tool in self.mcp_runtime.get_tools():
                source = get_tool_source_context(tool) or {}
                identity = f"mcp:{source.get('server_name', 'unknown')}:{tool.name}"
                if identity not in active:
                    blocked.append({"tool_id": identity,
                        "reason": self.mcp_runtime.task_tool_block_reason(source.get("server_name"))})
        return sorted(blocked, key=lambda item: item["tool_id"])

    def approval_context(self, descriptor, inputs, *, job_id):
        """Read the exact current wrapper fingerprint without consuming it.

        This invokes only the existing metadata hook, never the tool's
        execution entry point or the approval repository. Continuation still
        needs the authoritative owner/job/attempt checks and final wrapper.
        """
        from src.tools.approval import _tool_approval_context
        from src.approval.repository import fingerprint_tool_call
        from src.work_board.general_task import canonical, validate_data, validate_schema

        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("approval context requires a durable job identity")
        encoded_inputs = canonical(inputs)
        validate_data(inputs, dependencies=set())
        current = self._entries().get(descriptor.tool_id)
        if current is None or current[0].model_dump(mode="json") != descriptor.model_dump(mode="json"):
            raise PermissionError("task tool contract changed or unavailable")
        validate_schema(descriptor.input_schema, inputs)
        _, tool, _ = current
        arguments = json.loads(encoded_inputs)
        context = _tool_approval_context(tool, arguments)
        if canonical(arguments) != encoded_inputs:
            raise ValueError("approval metadata changed the step input")
        context = dict(context or {})
        # Match ApprovalTool's exact job-bound approval context convention.
        context.setdefault("workflow_run_identity", job_id.strip())
        context = json.loads(canonical(context))
        after = self._entries().get(descriptor.tool_id)
        if after is None or after[0].model_dump(mode="json") != descriptor.model_dump(mode="json"):
            raise PermissionError("task tool contract changed during approval metadata read")
        return {"tool_name": tool.name, "approval_context": context,
                "fingerprint": fingerprint_tool_call(tool.name, inputs, approval_context=context)}

    async def invoke(self, descriptor, inputs, *, principal, job_id, fencing_token):
        invocation = self.begin_invocation(descriptor, inputs, principal=principal,
            job_id=job_id, fencing_token=fencing_token)
        return await invocation.wait()

    def begin_invocation(self, descriptor, inputs, *, principal, job_id, fencing_token):
        if not principal or not principal.authenticated or principal.revoked or not principal.session_id:
            raise PermissionError("authenticated task principal is required")
        if principal.job_id != job_id or not job_id or type(fencing_token) is not int or fencing_token < 1:
            raise PermissionError("exact durable job and fence binding is required")
        from src.work_board.general_task import canonical, validate_data, validate_schema
        canonical(inputs)
        validate_data(inputs, dependencies=set())
        entry = self._entries().get(descriptor.tool_id)
        if entry is None or entry[0].model_dump(mode="json") != descriptor.model_dump(mode="json"):
            raise PermissionError("task tool contract changed or unavailable")
        validate_schema(descriptor.input_schema, inputs)
        if descriptor.tool_id == "write_file" and len(inputs["content"].encode()) > 60000:
            raise ValueError("workspace content exceeds task byte limit")
        if descriptor.tool_id == "document_prepare":
            from src.security.trust_contract import AuthorityGrant
            if AuthorityGrant.CAPABILITY_EXECUTE not in principal.grants:
                raise PermissionError("current capability execution permission is required")
            return TaskToolInvocation(asyncio.create_task(self._invoke_document_with_closure(
                descriptor, json.loads(canonical(inputs)), principal, job_id, fencing_token)))
        # ContextVars are copied by to_thread. Existing wrappers remain the
        # last authority/approval/audit/secret boundary, including MCP calls.
        # The handle belongs to the current native interpreter invocation.
        # A timed-out/cancelled waiter retains it until actual callback closure.
        future = asyncio.create_task(asyncio.to_thread(self._invoke_with_closure,
            descriptor, json.loads(canonical(inputs)), principal, job_id, fencing_token))
        return TaskToolInvocation(future)

    async def _invoke_document_with_closure(self, descriptor, inputs, principal, job_id, fencing_token):
        binding = TaskToolApprovalBinding(_digest(descriptor.model_dump(mode="json")),
            _digest(inputs), job_id, fencing_token)
        try:
            output = await self._invoke_document(descriptor, inputs, principal, job_id, fencing_token)
            witness = TaskToolClosureWitness(binding, "returned", _digest(output), None, _CLOSURE_SEAL)
            return _InvocationCompletion(output, None, witness)
        except BaseException as error:
            witness = TaskToolClosureWitness(binding, "unknown", None, None, _CLOSURE_SEAL)
            return _InvocationCompletion(None, error, witness)

    async def _invoke_document(self, descriptor, inputs, principal, job_id, fencing_token):
        from src.work_board.document_preparation import invoke
        from src.audit.repository import audit_repository
        # This one async native owner accepts only a hexadecimal digest,
        # has no credential fields and uses task-bound local consent. Keep
        # the existing audit owner without moving async SQL to a thread.
        async def audit(event_type, details):
            await audit_repository.log_event(session_id=principal.session_id,
                actor="agent", event_type=event_type, tool_name="document_prepare",
                risk_level="low", policy_mode=get_task_policy_snapshot()["tool_mode"],
                summary="Local document preparation " + event_type,
                details={"job_id": job_id, "fencing_token": fencing_token, "no_learning": True, **details})
        from src.tools.policy import get_task_policy_snapshot
        await audit("tool_call", {"input_digest": _digest(inputs)})
        try:
            result = await invoke(principal, job_id, fencing_token, inputs)
        except Exception as exc:
            await audit("tool_failed", {"error_type": type(exc).__name__})
            raise
        await audit("tool_result", {"result_digest": _digest(result), "provider_contacts": 0})
        return result

    def _invoke_with_closure(self, descriptor, inputs, principal, job_id, fencing_token):
        binding = TaskToolApprovalBinding(_digest(descriptor.model_dump(mode="json")),
            _digest(inputs), job_id, fencing_token)
        try:
            output = self._invoke_sync(descriptor, inputs, principal, job_id, fencing_token)
            witness = TaskToolClosureWitness(binding, "returned", _digest(output), None, _CLOSURE_SEAL)
            return _InvocationCompletion(output, None, witness)
        except BaseException as error:
            precontact = (type(error) is TaskToolApprovalRequired
                and error._producer_seal is _PRECONTACT_SEAL and error.binding == binding)
            witness = TaskToolClosureWitness(binding, "approval_precontact" if precontact else "unknown",
                None, error.approval_id if precontact else None, _CLOSURE_SEAL)
            return _InvocationCompletion(None, error, witness)

    def _invoke_sync(self, descriptor, inputs, principal, job_id, fencing_token):
        from src.approval.runtime import (set_runtime_context, reset_runtime_context,
            set_runtime_fencing_token, reset_runtime_fencing_token)
        from src.tools.approval import ApprovalTool, wrap_tools_for_approval, wrap_tools_with_forced_approval
        from src.extensions.capability_execution import _ADOPTED_CAPABILITIES
        from src.tools.audit import wrap_tools_for_audit
        from src.tools.secret_ref_tools import wrap_tools_for_secret_refs
        from src.tools.policy import get_current_mcp_policy_mode
        from src.work_board.general_task import canonical, validate_schema
        current = self._entries().get(descriptor.tool_id)
        if current is None or current[0].model_dump(mode="json") != descriptor.model_dump(mode="json"):
            raise PermissionError("task tool contract changed before execution")
        _, tool, is_mcp = current
        tools = wrap_tools_for_audit(wrap_tools_for_secret_refs([_MCPResultGuard(tool) if is_mcp else tool]),
                                    treat_all_as_mcp=is_mcp)
        marker = _InvocationMarker(tools[0])
        tools = [marker]
        wrapper = (wrap_tools_with_forced_approval if is_mcp and get_current_mcp_policy_mode() == "approval"
                   else wrap_tools_for_approval)(tools, treat_all_as_mcp=is_mcp)[0]
        tokens = set_runtime_context(principal.session_id, "high_risk", trust_principal=principal)
        fence = set_runtime_fencing_token(str(fencing_token))
        from contextlib import nullcontext
        try:
            output_scope = (self.mcp_runtime.task_output_scope(descriptor, inputs,
                job_id=job_id, fencing_token=fencing_token, tool_name=tool.name) if is_mcp else nullcontext())
            try:
                with output_scope:
                    raw = wrapper(**inputs)
            except ApprovalRequired as approval:
                # Adopted native effects dispatch through the durable host,
                # which intentionally bypasses the inner Tool chain. Without
                # a host contact receipt they cannot receive this proof type.
                origin = approval.__traceback__
                while origin is not None and origin.tb_next is not None:
                    origin = origin.tb_next
                wrapper_origin = (origin is not None
                    and origin.tb_frame.f_code is ApprovalTool.__call__.__code__
                    and origin.tb_frame.f_locals.get("self") is wrapper)
                if (isinstance(wrapper, ApprovalTool) and wrapper_origin and not marker.contacted
                    and tool.name not in _ADOPTED_CAPABILITIES):
                    raise TaskToolApprovalRequired(approval, binding=TaskToolApprovalBinding(
                        descriptor_digest=_digest(descriptor.model_dump(mode="json")),
                        input_digest=_digest(inputs), job_id=job_id,
                        fencing_token=fencing_token), _producer_seal=_PRECONTACT_SEAL) from approval
                if isinstance(approval, TaskToolApprovalRequired):
                    raise ApprovalRequired(**{name: getattr(approval, name)
                        for name in _APPROVAL_FIELDS}) from approval
                raise
            if is_mcp:
                from src.tools.mcp_manager import check_task_raw_output, check_task_output
                check_task_raw_output(raw)
                def non_json_constant(_constant):
                    raise ValueError("mcp_task_output_number_limit")
                result = json.loads(raw, parse_constant=non_json_constant) if type(raw) is str else raw
                check_task_output(result)
            else:
                result = self._native_output(descriptor.tool_id, inputs, raw)
            canonical(result)
            validate_schema(descriptor.output_schema, result)
            return result
        finally:
            reset_runtime_fencing_token(fence)
            reset_runtime_context(tokens)

    @staticmethod
    def _native_output(name, inputs, raw):
        if not isinstance(raw, str):
            raise ValueError("tool output must be text")
        if name in {"read_file", "write_file"}:
            from src.tools.filesystem_tool import (_assert_not_secret_like_path, _safe_resolve,
                _read_workspace_text_bounded)
            _assert_not_secret_like_path(inputs["file_path"], "task_verify")
            text, truncated = _read_workspace_text_bounded(_safe_resolve(inputs["file_path"]), max_bytes=60000)
            if truncated:
                raise ValueError("task filesystem result exceeds byte limit")
            if name == "write_file":
                expected = f"Successfully wrote {len(inputs['content'])} characters to {inputs['file_path']}"
                if raw != expected or text != inputs["content"]:
                    raise ValueError("workspace write readback failed")
                return {"file_path": inputs["file_path"], "content_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "bytes_written": len(text.encode())}
            if raw != text:
                raise ValueError("workspace read readback failed")
        else:
            if raw.startswith(("Error:", "Search error:", "Blocked:")):
                raise ValueError("external tool reported failure")
            text = raw
        if len(text.encode()) > 60000:
            raise ValueError("task tool output exceeds byte limit")
        return {"content": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}
