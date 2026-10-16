"""Governed capability and inference services for Cordis consumers (#1018).

A Cordis provider never executes a tool, holds a credential or opens a cost
lane: ``capabilities.invoke`` and ``inference.request`` forward to these Python
owners, which run the existing governed executors (``CapabilityExecutionHost``
and the model-fabric streaming path behind the single
``remote_inference_admission_broker``) for one canonical job/attempt/lease with
an exact input digest. Each call records an intent before the effect and seals
Python's own result; the child's answer is accepted only as an echo of it.
Outputs are bounded and Vault-redacted before they leave Python.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import time

from .contracts import succeeded
from .dispatch import (NativeServiceBlocked, _append_receipt, _canonical, _digest, _run_candidate, _writer, cas_run,
                       read_context)

GOVERNED_JOB_KIND = "runtime_service_governed_v1"
INFERENCE_ROUTES = {"interactive": "cordis_interactive_inference", "background": "cordis_background_inference"}
MAX_OUTPUT_BYTES = 16384
FRAME_BYTES = 1024
MAX_MESSAGES = 4
MAX_MESSAGE_BYTES = 4096
MAX_INFERENCE_TOKENS = 1024
# Executed without an approval prompt only when the executor's effect is read-only.
READ_ONLY_CAPABILITIES = frozenset({"read_file", "list_processes", "read_process_output", "preview_workspace_patch"})
# Tools that return or manage Vault material are never Cordis capabilities.
SECRET_TOOLS = frozenset({"get_secret", "get_secret_ref", "list_secrets", "store_secret", "delete_secret"})
# Finite adapter operations per connection service. None is composed yet: a
# valid current request fails closed with ADAPTER_NOT_COMPOSED (follow-up).
ADAPTER_OPERATIONS = {"calendar_readonly": frozenset({"calendar.read_events"}),
                      "gmail_readonly": frozenset({"gmail.read_messages"})}
ADAPTER_NOT_COMPOSED = "native_adapter_operation_not_composed"
_JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list}


# ---------------------------------------------------------------- descriptors

def _effect_class(name: str) -> str:
    if name in READ_ONLY_CAPABILITIES:
        return "read_only"
    if name in {"write_file", "apply_workspace_patch"}:
        return "workspace_write"
    if name in {"run_command", "start_process", "stop_process"}:
        return "process_execution"
    return "external_or_unknown"


def capability_descriptors() -> dict[str, dict]:
    """Closed descriptors from the governed tool surface (``factory.get_tools``).

    Only adopted, read-only ``CapabilityExecutionHost`` capabilities are
    invocable; MCP, approval-gated, ungoverned and secret tools are listed as
    blocked with their reason, never dispatched by name.
    """
    from src.agent.factory import get_tools
    from src.extensions.capability_contract import build_native_tool_contract
    from src.extensions.capability_execution import _ADOPTED_CAPABILITIES, CapabilityExecutionLimits
    from src.native_tools.registry import TOOL_METADATA
    from src.tools.mcp_manager import mcp_manager
    limits = CapabilityExecutionLimits()
    mcp_names = {getattr(tool, "name", "") for tool in (getattr(mcp_manager, "get_tools", lambda: [])() or [])}
    descriptors = {}
    for tool in get_tools():
        name = tool.name
        metadata = TOOL_METADATA.get(name, {})
        if name in mcp_names:
            executor, version, reason = "mcp_manager", "mcp-v1", "mcp_invoke_not_bound"
        else:
            executor = "capability_execution_host" if name in _ADOPTED_CAPABILITIES else "native_tool"
            version = build_native_tool_contract(
                name=name, description=metadata.get("description", ""), policy_modes=list(metadata.get("policy_modes", [])),
                risk_level=metadata.get("risk_level", "low"), execution_boundaries=list(metadata.get("execution_boundaries", [])),
                availability="ready", blocked_reason=None, execution=metadata.get("execution"))["version"]
            reason = ("vault_secret_tool_denied" if name in SECRET_TOOLS
                      else "native_tool_not_governed" if name not in _ADOPTED_CAPABILITIES
                      else "capability_requires_approval" if name not in READ_ONLY_CAPABILITIES else None)
        inputs = {key: {"type": str(spec.get("type", "any")), "nullable": bool(spec.get("nullable", False))}
                  for key, spec in sorted((getattr(tool, "inputs", {}) or {}).items())}
        descriptors["native_tool:" + name] = {
            "capability_id": "native_tool:" + name, "version": str(version), "executor": executor, "tool": name,
            "input_schema": inputs, "output_type": str(getattr(tool, "output_type", "string")),
            "effect_class": _effect_class(name), "permissions": ["capability_execute"],
            "limits": {"max_output_bytes": min(MAX_OUTPUT_BYTES, limits.output_bytes), "cpu_seconds": limits.cpu_seconds,
                       "memory_bytes": limits.memory_bytes, "process_count": limits.process_count,
                       "deadline_seconds": limits.deadline_seconds},
            "state": "available" if reason is None else "blocked", "reason_code": reason}
    return descriptors


def validate_arguments(descriptor: dict, arguments) -> dict:
    """Closed input schema: no unknown fields, required fields present, exact JSON types."""
    if type(arguments) is not dict or len(json.dumps(arguments, ensure_ascii=False).encode()) > 8192:
        raise NativeServiceBlocked("native_capability_schema_invalid")
    schema = descriptor["input_schema"]
    if set(arguments) - set(schema) or any(key not in arguments for key, spec in schema.items() if not spec["nullable"]):
        raise NativeServiceBlocked("native_capability_schema_invalid")
    for key, value in arguments.items():
        expected = _JSON_TYPES.get(schema[key]["type"])
        if value is None and schema[key]["nullable"]:
            continue
        if expected is not None and (not isinstance(value, expected) or (expected is int and type(value) is bool)):
            raise NativeServiceBlocked("native_capability_schema_invalid")
    return arguments


def validate_governed_candidate(candidate) -> dict:
    if type(candidate) is not dict or candidate.get("schema_version") != 1:
        raise NativeServiceBlocked("native_governed_candidate_invalid")
    method = candidate.get("method")
    if method == "capabilities.invoke":
        if set(candidate) != {"schema_version", "method", "capability_id", "version", "arguments", "arguments_digest"}:
            raise NativeServiceBlocked("native_governed_candidate_invalid")
        descriptor = capability_descriptors().get(candidate["capability_id"])
        if descriptor is None:
            raise NativeServiceBlocked("native_capability_unknown")
        if descriptor["state"] != "available":
            raise NativeServiceBlocked(descriptor["reason_code"])
        if candidate["version"] != descriptor["version"]:
            raise NativeServiceBlocked("native_capability_version_changed")
        validate_arguments(descriptor, candidate["arguments"])
        if candidate["arguments_digest"] != _digest(candidate["arguments"]):
            raise NativeServiceBlocked("native_capability_input_digest_changed")
        return dict(candidate)
    if method == "inference.request":
        if set(candidate) != {"schema_version", "method", "purpose", "messages", "max_tokens"}:
            raise NativeServiceBlocked("native_governed_candidate_invalid")
        messages = candidate["messages"]
        if (candidate["purpose"] not in INFERENCE_ROUTES or type(messages) is not list or not 1 <= len(messages) <= MAX_MESSAGES
                or any(type(item) is not dict or set(item) != {"role", "content"} or item["role"] not in {"system", "user"}
                       or type(item["content"]) is not str or not 0 < len(item["content"].encode()) <= MAX_MESSAGE_BYTES
                       for item in messages)
                or type(candidate["max_tokens"]) is not int or not 1 <= candidate["max_tokens"] <= MAX_INFERENCE_TOKENS):
            raise NativeServiceBlocked("native_inference_candidate_invalid")
        return dict(candidate)
    if method == "connections.invokeBoundAdapter":
        if (set(candidate) != {"schema_version", "method", "connection_ref", "expected_connection_revision", "operation"}
                or type(candidate["connection_ref"]) is not str or not 0 < len(candidate["connection_ref"]) <= 128
                or type(candidate["expected_connection_revision"]) is not int or candidate["expected_connection_revision"] < 1
                or not any(candidate["operation"] in ops for ops in ADAPTER_OPERATIONS.values())):
            raise NativeServiceBlocked("native_adapter_operation_unknown")
        return dict(candidate)
    raise NativeServiceBlocked("native_governed_candidate_invalid")


def wire_inputs(job_id: str, method: str = "capabilities.invoke") -> dict:
    """The exact plugin-visible request: only the canonical job reference."""
    return {"operation_ref": job_id} if method == "connections.invokeBoundAdapter" else {"request_ref": job_id}


async def current_connection(db, run, candidate) -> None:
    """The connection must be the caller's current, ready, unchanged row; installation alone authorizes nothing."""
    from sqlalchemy import select
    from src.db.models import GoogleServiceConnection
    row = (await db.execute(select(GoogleServiceConnection.revision, GoogleServiceConnection.state, GoogleServiceConnection.service)
                            .where(GoogleServiceConnection.connection_id == candidate["connection_ref"],
                                   GoogleServiceConnection.owner_principal_id == run.owner_principal_id,
                                   GoogleServiceConnection.owner_session_id == run.operator_session_id))).one_or_none()
    if row is None or row.revision != candidate["expected_connection_revision"]:
        raise NativeServiceBlocked("native_connection_candidate_changed")
    if row.state != "ready":
        raise NativeServiceBlocked("native_connection_unavailable")
    if candidate["operation"] not in ADAPTER_OPERATIONS.get(row.service, frozenset()):
        raise NativeServiceBlocked("native_adapter_operation_unknown")


async def _redacted(text: str) -> str:
    from src.vault.redaction import redact_secrets_in_text
    return await redact_secrets_in_text(text, fail_closed=True)


def _frames(text: str) -> list[str]:
    frames, current = [], ""
    for char in text:
        if len((current + char).encode()) > FRAME_BYTES:
            frames.append(current)
            current = ""
        current += char
    return [*frames, current] if current else frames


# ---------------------------------------------------------------- effects

async def _invoke_capability(run, candidate) -> dict:
    from src.approval.runtime import reset_runtime_context, reset_runtime_fencing_token, set_runtime_context, set_runtime_fencing_token
    from src.extensions.capability_execution import CapabilityExecutionError, build_capability_request, current_capability_execution_host
    from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
    tool = candidate["capability_id"].split(":", 1)[1]
    principal = TrustPrincipal(principal_id=run.owner_principal_id, principal_type=PrincipalType.OPERATOR,
                               grants=(AuthorityGrant.CAPABILITY_EXECUTE,), session_id=run.session_id,
                               job_id=run.run_identity, operator_session_id=run.operator_session_id)
    tokens = set_runtime_context(run.session_id, "off", trust_principal=principal)
    fence = set_runtime_fencing_token(str(run.fencing_token))
    try:
        request = build_capability_request(capability_id=tool, arguments=candidate["arguments"],
                                           idempotency_key=run.run_identity)
        host = current_capability_execution_host()
        receipt = await asyncio.to_thread(host.execute, request)
    except CapabilityExecutionError as exc:
        raise NativeServiceBlocked("native_capability_denied") from exc
    finally:
        reset_runtime_fencing_token(fence)
        reset_runtime_context(tokens)
    output = receipt.result if isinstance(receipt.result, str) else json.dumps(receipt.result, ensure_ascii=False, default=str)
    output = await _redacted(output)
    if len(output.encode()) > MAX_OUTPUT_BYTES:
        raise NativeServiceBlocked("native_capability_output_oversize")
    return {"output": output, "receipt": {"effect_id": receipt.effect_id, "state": receipt.state,
                                          "output_digest": receipt.output_digest}}


async def _request_inference(jobs, run, candidate) -> dict:
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.llm_runtime import stream_completion_with_fallback
    from src.model_fabric.caller_context import build_canonical_inference_context
    from src.model_fabric.remote_inference_admission import bind_remote_inference_receipt
    from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
    from config.settings import settings
    route = INFERENCE_ROUTES[candidate["purpose"]]
    remaining = run.deadline_at.replace(tzinfo=run.deadline_at.tzinfo or timezone.utc).timestamp() - time.time()
    if remaining <= 0:
        raise NativeServiceBlocked("native_original_attempt_changed")
    principal = TrustPrincipal(principal_id=run.owner_principal_id, principal_type=PrincipalType.OPERATOR,
                               grants=(AuthorityGrant.MODEL_INFERENCE,), session_id=run.session_id,
                               job_id=run.run_identity, operator_session_id=run.operator_session_id)
    # The broker keys a job-bound operation as ``remote:<job_id>`` (stable across restarts).
    operation_id = "cordis-inference:" + run.run_identity
    messages = [dict(item) for item in candidate["messages"]]
    tokens = set_runtime_context(run.session_id, "off", trust_principal=principal)
    parts: list[str] = []
    try:
        try:
            from dataclasses import replace
            from src.model_fabric.configuration import read_model_fabric_configuration
            setup = read_model_fabric_configuration().openrouter_setup
            if setup is None:
                raise PermissionError("no operator inference route")
            # The route's latency bound is the operator setup's; the job's original deadline still cuts it.
            context = build_canonical_inference_context(route, payload=messages, output_tokens=candidate["max_tokens"],
                timeout_seconds=setup.timeout_seconds, principal=principal, session_id=run.session_id,
                job_id=run.run_identity, request_id=operation_id, streaming=True)
            context = replace(context, deadline_at=min(context.deadline_at, time.time() + remaining))
        except (PermissionError, ValueError) as exc:
            raise NativeServiceBlocked("inference_route_unavailable") from exc
        with bind_remote_inference_receipt(repository=jobs, job_id=run.run_identity, owner=run.lease_owner,
                                           fencing_token=run.fencing_token):
            async for delta in stream_completion_with_fallback(messages, settings.model_temperature, candidate["max_tokens"],
                                                               route, context, request_id=operation_id):
                parts.append(delta)
                if sum(len(part.encode()) for part in parts) > MAX_OUTPUT_BYTES:
                    raise NativeServiceBlocked("inference_output_oversize")
    except NativeServiceBlocked:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        from src.llm_runtime import ProviderProfileConfigurationError
        from src.model_fabric.contracts import NoCompliantModelRouteError
        from src.model_fabric.gpu_admission import GpuAdmissionUncertainError
        if isinstance(exc, (NoCompliantModelRouteError, ProviderProfileConfigurationError)):
            raise NativeServiceBlocked("inference_route_unavailable") from exc
        if isinstance(exc, GpuAdmissionUncertainError):
            raise NativeServiceBlocked("inference_cost_unresolved") from exc
        raise NativeServiceBlocked("inference_request_failed") from exc
    finally:
        reset_runtime_context(tokens)
    text = await _redacted("".join(parts))
    snapshot = await jobs.inference_accounting_snapshot(job_id=run.run_identity)
    usage = [{key: row.get(key) for key in ("operation_id", "state", "reserved_microusd", "charged_microusd", "runtime_path")}
             for row in snapshot.get("operations", []) if row.get("operation_id") == "remote:" + run.run_identity]
    return {"output": text, "frames": _frames(text), "usage": usage, "route": route}


# ---------------------------------------------------------------- callback owner

async def dispatch_governed(dispatcher, frame, payload, scope):
    """intent (writer) -> governed effect -> sealed result (writer), each fenced."""
    method = frame["method"]
    jobs = dispatcher.jobs
    if method != scope.binding.origin_method:
        raise NativeServiceBlocked("native_method_candidate_not_bound")
    async with jobs._session() as db:
        await _writer(db)
        run, _, _ = await dispatcher.current(db, scope, method)
        if run.job_kind != GOVERNED_JOB_KIND:
            raise NativeServiceBlocked("native_task_candidate_not_bound")
        candidate = _run_candidate(run)
        context = read_context(run)
        if payload != wire_inputs(run.run_identity, method) or candidate["method"] != method:
            raise NativeServiceBlocked("native_governed_reference_not_bound")
        if method == "connections.invokeBoundAdapter":
            # A current canonical connection and finite operation, then fail closed: no
            # adapter is composed in this slice, so no credential or effect is touched.
            await current_connection(db, run, candidate)
            raise NativeServiceBlocked(ADAPTER_NOT_COMPOSED)
        if context["operations"] or context["result"] is not None:
            # An interrupted effect stays an unresolved intent; it is never re-executed.
            raise NativeServiceBlocked("native_governed_replay_denied")
        context["operations"].append({"op": "intent", "method": method, "call_ref": frame["request_id"], "no_learning": True})
        await cas_run(db, run, checkpoint_context_json=_canonical(context))
        await db.commit()
        await db.refresh(run)
    if method == "capabilities.invoke":
        outcome = await _invoke_capability(run, candidate)
        result = succeeded(method, {"receipt_ref": "cordis-capability:" + hashlib.sha256(
            outcome["receipt"]["effect_id"].encode()).hexdigest()[:48], "artifact_refs": []})
    else:
        outcome = await _request_inference(jobs, run, candidate)
        output_digest = hashlib.sha256(outcome["output"].encode()).hexdigest()
        result = succeeded(method, {"receipt_ref": "cordis-inference:" + run.run_identity[-40:],
                                    "output_ref": "cordis-output:" + output_digest[:48]})
    async with jobs._session() as db:
        await _writer(db)
        # Late or revoked: a changed attempt, deadline or authority never seals output.
        run, _, _ = await dispatcher.current(db, scope, method)
        context = read_context(run)
        if len(context["operations"]) != 1 or context["operations"][0]["call_ref"] != frame["request_id"]:
            raise NativeServiceBlocked("native_governed_operation_changed")
        output_digest = hashlib.sha256(outcome["output"].encode()).hexdigest()
        context["operations"][0].update(op="settled", result_digest=_digest(result))
        context["result"] = {"result_digest": _digest(result), "payload": result, "outcome": outcome, "no_learning": True}
        now = datetime.now(timezone.utc).isoformat()
        readback = {"effect_id": "cordis-governed:" + run.run_identity, "receipt_kind": "readback",
                    "effect_type": "cordis_governed_" + method.split(".")[0], "status": "succeeded",
                    "target_path": "cordis-output:" + output_digest, "content_sha256": output_digest,
                    "target_digest": output_digest, "verified_at": now, "recorded_at": now,
                    "readback_id": "cordis-governed-readback:" + run.run_identity, "reconciled": True,
                    "reconciliation_status": "resolved", "fencing_token": run.fencing_token,
                    "details": {"verified": True, "no_learning": True}}
        await cas_run(db, run, checkpoint_context_json=_canonical(context),
                      effect_receipts_json=_append_receipt(run.effect_receipts_json, readback, key="effect_id"))
        await db.commit()
    return result


# ---------------------------------------------------------------- Python owners

async def _execute_governed(*, operator, candidate: dict, idempotency_key: str, host=None, jobs=None) -> dict:
    from .bridge import HostBlocked, cordis_host
    from .service_artifacts import assert_child_echo
    from .service_jobs import _block, _jobs, admit_service_job, claim_service_job, open_invocation
    host = host or cordis_host
    jobs = _jobs(jobs)
    candidate = validate_governed_candidate(candidate)
    method = candidate["method"]
    if not host.admitting or host.reviewed is None:
        raise NativeServiceBlocked("native_governed_host_unavailable")
    job, replayed = await admit_service_job(operator=operator, origin_method=method, native_branch="governed",
                                            candidate=candidate, idempotency_key=idempotency_key,
                                            reviewed_composition=host.reviewed, jobs=jobs)
    if job["status"] != "accepted":
        # An exact retry reuses the original job; it never re-executes.
        return {"job": job, "replayed": True, "memory_status": "no_learning"}
    owner, fence = await claim_service_job(job, host=host, jobs=jobs)
    scope = await open_invocation(job_id=job["job_id"], owner=owner, fence=fence, host=host,
                                  admission_replayed=replayed, candidate=candidate, jobs=jobs)
    try:
        result = await host.request_service(method, wire_inputs(job["job_id"], method), original_scope=scope)
        if result["status"] != "succeeded":
            raise NativeServiceBlocked(result["reason_code"])
        async with jobs._session() as db:
            run = await jobs._fetch(db, job["job_id"])
            assert_child_echo(run, method, result)
            sealed = read_context(run)["result"]
        completed = await jobs.transition_job(job["job_id"], "succeeded", owner=owner, fencing_token=fence,
            result={"no_learning": True, "method": method, "sealed_result_digest": sealed["result_digest"]},
            result_summary="Cordis governed " + method + " completed by its Python owner; no_learning")
        # Only Python-owned values leave: the sealed result, bounded redacted output and usage receipt.
        return {"job": completed, "result": sealed["payload"], **sealed["outcome"], "replayed": False,
                "memory_status": "no_learning"}
    except (NativeServiceBlocked, HostBlocked) as exc:
        reason = getattr(exc, "reason_code", None) or "native_governed_host_unavailable"
        await _block(jobs, scope, reason)
        raise NativeServiceBlocked(reason) from exc


async def execute_native_capability(*, operator, capability_id: str, arguments: dict, idempotency_key: str,
                                    host=None, jobs=None) -> dict:
    descriptor = capability_descriptors().get(capability_id)
    if descriptor is None:
        raise NativeServiceBlocked("native_capability_unknown")
    candidate = {"schema_version": 1, "method": "capabilities.invoke", "capability_id": capability_id,
                 "version": descriptor["version"], "arguments": arguments, "arguments_digest": _digest(arguments)}
    return await _execute_governed(operator=operator, candidate=candidate, idempotency_key=idempotency_key, host=host, jobs=jobs)


async def execute_native_connection_operation(*, operator, connection_ref: str, expected_connection_revision: int,
                                             operation: str, idempotency_key: str, host=None, jobs=None) -> dict:
    candidate = {"schema_version": 1, "method": "connections.invokeBoundAdapter", "connection_ref": connection_ref,
                 "expected_connection_revision": expected_connection_revision, "operation": operation}
    return await _execute_governed(operator=operator, candidate=candidate, idempotency_key=idempotency_key, host=host, jobs=jobs)


async def execute_native_inference(*, operator, purpose: str, messages: list[dict], max_tokens: int, idempotency_key: str,
                                   host=None, jobs=None) -> dict:
    candidate = {"schema_version": 1, "method": "inference.request", "purpose": purpose, "messages": messages,
                 "max_tokens": max_tokens}
    return await _execute_governed(operator=operator, candidate=candidate, idempotency_key=idempotency_key, host=host, jobs=jobs)
