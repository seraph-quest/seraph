"""Typed general-task adapters over the current governed tool owners.

No connection, queue, host, grant or credential is created here. Lifecycle is
owned by the application; descriptor snapshots are data, never authority.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

from src.work_board.contracts import ToolDescriptor


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


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
                        "reason": "trusted_typed_contract_or_policy_unavailable"})
        return sorted(blocked, key=lambda item: item["tool_id"])

    async def invoke(self, descriptor, inputs, *, principal, job_id, fencing_token):
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
        # ContextVars are copied by to_thread. Existing wrappers remain the
        # last authority/approval/audit/secret boundary, including MCP calls.
        return await asyncio.to_thread(self._invoke_sync, descriptor, inputs,
            principal, job_id, fencing_token)

    def _invoke_sync(self, descriptor, inputs, principal, job_id, fencing_token):
        from src.approval.runtime import (set_runtime_context, reset_runtime_context,
            set_runtime_fencing_token, reset_runtime_fencing_token)
        from src.tools.approval import wrap_tools_for_approval, wrap_tools_with_forced_approval
        from src.tools.audit import wrap_tools_for_audit
        from src.tools.secret_ref_tools import wrap_tools_for_secret_refs
        from src.tools.policy import get_current_mcp_policy_mode
        from src.work_board.general_task import canonical, validate_schema
        current = self._entries().get(descriptor.tool_id)
        if current is None or current[0].model_dump(mode="json") != descriptor.model_dump(mode="json"):
            raise PermissionError("task tool contract changed before execution")
        _, tool, is_mcp = current
        tools = wrap_tools_for_audit(wrap_tools_for_secret_refs([tool]), treat_all_as_mcp=is_mcp)
        wrapper = (wrap_tools_with_forced_approval if is_mcp and get_current_mcp_policy_mode() == "approval"
                   else wrap_tools_for_approval)(tools, treat_all_as_mcp=is_mcp)[0]
        tokens = set_runtime_context(principal.session_id, "high_risk", trust_principal=principal)
        fence = set_runtime_fencing_token(str(fencing_token))
        try:
            raw = wrapper(**inputs)
            if is_mcp:
                result = json.loads(raw) if isinstance(raw, str) else raw
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
