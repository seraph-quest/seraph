"""One fixed child synthesis through the existing governed serial broker.

This module never selects another candidate, calls a model for the parent,
or retries a contacted request. Prompts and quoted sources remain private
artifacts; only exact digests enter the canonical checkpoints and cost rows.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import time

from src.work_board.research_artifacts import (
    json_bytes, prompt_messages, read, verified_child, write_verified,
)
from src.work_board.research_contracts import CHILD_KIND, PROMPT_READY
from src.workflows.research_native import checkpoint
from src.workflows.research_sources import current_inputs


def _timestamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).timestamp()


def _target():
    from src.llm_runtime import _provider_profile, _profile_options
    from src.model_fabric.effective_policy import current_inference_policy
    configured, policy_digest = current_inference_policy()
    setup = configured.openrouter_setup
    if setup.timeout_seconds > 45:
        raise ValueError("research requires a reviewed provider timeout at most 45 seconds")
    profile = _provider_profile(setup.profile_id)
    if profile is None or profile.provider_kind != "openrouter" or profile.transport_adapter != "openai_compatible_chat":
        raise ValueError("research requires its reviewed fixed OpenRouter chat profile")
    # Only the governed upstream policy accompanies this finite request. No
    # arbitrary profile fields may add tools, history, compression or retries.
    options = _profile_options(setup.profile_id)
    if set(options) - {"provider", "_seraph_openrouter"}:
        raise ValueError("research profile contains unsupported request options")
    target = {"profile": setup.profile_id, "model_id": profile.model,
        "api_base": profile.api_base, "api_key": profile.api_key, "source": "primary",
        "options": {"provider": options["provider"]} if "provider" in options else {}}
    return setup, policy_digest, target


async def _context(child, body, deadline):
    from src.auth.service import authenticate_session
    from src.model_fabric.caller_context import build_canonical_inference_context
    operator = await authenticate_session(child["operator_session_id"], touch=False)
    if operator.session_id != child["session_id"] or operator.principal.principal_id != child["owner"]["principal_id"]:
        raise PermissionError("research original operator session changed")
    remaining = deadline-time.time()
    if remaining <= 0:
        raise TimeoutError("research original contact deadline expired")
    principal = replace(operator.principal, session_id=child["session_id"],
        operator_session_id=child["operator_session_id"], job_id=child["job_id"])
    context = build_canonical_inference_context("readonly_research_child", payload=body,
        output_tokens=body["max_tokens"], timeout_seconds=remaining, principal=principal,
        session_id=child["session_id"], job_id=child["job_id"], request_id="remote:"+child["job_id"])
    # The constructor's relative time is never a renewed financial/contact
    # allowance: bind the original absolute deadline recorded below instead.
    # Route eligibility describes the original admitted policy ceiling, not
    # a renewed transfer timeout. Queue/local work consumes deadline_at; the
    # HTTP helper enforces the strictly smaller remaining absolute window.
    setup, _policy, _fixed_target = _target()
    return replace(context, deadline_at=deadline, requirements=replace(context.requirements,
        max_latency_ms=int(setup.timeout_seconds*1000)))


async def prepare_prompt(jobs, *, child_id, owner, fence, sources):
    """Persist exact bounded body and source manifest before paired funding."""
    from src.llm_runtime import _governed_preflight_target_async
    from src.model_fabric.contracts import finalized_openai_compatible_body
    from src.security.trust_contract import canonical_digest
    child = await jobs.get_job(child_id)
    inputs = await current_inputs(jobs, child["parent_job_id"])
    authority = child["declared_authority"]
    slot = authority["research_slot"]
    perspective = inputs.perspectives[slot]
    if child["job_kind"] != CHILD_KIND or child["status"] != "running":
        raise ValueError("research prompt requires its current native child lease")
    if [item["source_id"] for item in sources] != [f"source:{index}" for index in perspective.source_slots]:
        raise ValueError("research quoted sources differ from the admitted slots")
    setup, policy_digest, target = _target()
    if policy_digest != authority["model_policy_digest"]:
        raise ValueError("research original model policy changed")
    body = finalized_openai_compatible_body(model_id=target["model_id"],
        messages=prompt_messages(inputs.question, perspective.instruction, sources),
        options=target["options"], temperature=setup.temperature,
        max_tokens=min(1024, setup.max_output_tokens), stream=False)
    # The contact window starts with the exact prompt, includes funding and
    # queue wait, and survives restart without another 45-second allowance.
    deadline = min(_timestamp(child["deadline_at"]), time.time()+min(45, setup.timeout_seconds))
    context = await _context(child, body, deadline)
    decision, _proofs = await _governed_preflight_target_async(target, context)
    if decision is None or not decision.allowed:
        raise PermissionError("research fixed route lacks current governed capability proof")
    creation_digest = authority["parent_creation_digest"]
    source_artifact = await write_verified(jobs, job_id=child_id, owner=owner, fence=fence,
        creation_digest=creation_digest, slot=slot, kind="manifest", content=json_bytes(sources))
    artifact = await write_verified(jobs, job_id=child_id, owner=owner, fence=fence,
        creation_digest=creation_digest, slot=slot, kind="prompt", content=json_bytes(body))
    ready = {**artifact, "slot": slot, "creation_digest": creation_digest,
        "payload_digest": canonical_digest(body), "policy_digest": policy_digest,
        "prompt_ready_fence": fence, "contact_deadline_at": deadline,
        "source_manifest_path": source_artifact["file_path"],
        "source_manifest_sha256": source_artifact["content_sha256"], "no_learning": True}
    await current_inputs(jobs, child["parent_job_id"])
    await jobs.record_checkpoint(child_id, checkpoint_id="research:prompt-ready", state=ready,
        checkpoint_payload=ready, owner=owner, fencing_token=fence)
    await jobs.transition_job(child_id, "paused", reason=PROMPT_READY, owner=owner, fencing_token=fence)
    return ready


async def execute_funded_child(jobs, *, child_id, owner, fence):
    """Consume one paired reservation, one HTTP POST, and actual readback."""
    from src.approval.runtime import set_runtime_context, reset_runtime_context
    from src.llm_runtime import _governed_preflight_target_async, _governed_research_chat_completion, _token_usage_from_payload
    from src.model_fabric.hooks import RouteReceiptSession
    from src.model_fabric.remote_inference_admission import (
        GpuAdmissionRequest, bind_remote_inference_receipt,
        prepare_bound_remote_inference, remote_inference_admission_broker as broker,
    )
    from src.security.trust_contract import canonical_digest
    child = await jobs.get_job(child_id)
    await current_inputs(jobs, child["parent_job_id"])
    ready = checkpoint(child, "research:prompt-ready")
    from src.workflows.research_sources import verify_current_prompt_in_db
    async with jobs._session() as db:
        body, sources = await verify_current_prompt_in_db(jobs, db, child)
    setup, policy_digest, target = _target()
    if (canonical_digest(body) != ready["payload_digest"] or policy_digest != ready["policy_digest"]
        or body["model"] != target["model_id"] or body["stream"] is not False):
        raise ValueError("research funded prompt/profile binding changed")
    reserved_output = checkpoint(child, f"research:artifact:child:{ready['slot']}")
    if reserved_output is not None:
        # This exact slot was reserved only after the one actual callback and
        # settlement returned. Recovery adopts bytes, never calls the provider.
        snapshot = await jobs.inference_accounting_snapshot(job_id=child_id)
        settled = next((row for row in snapshot["operations"] if row["operation_id"] == "remote:"+child_id), None)
        if (snapshot.get("accounting_continuity_verified") is not True or not settled or settled["state"] != "settled"
            or settled["actual_cost_microusd"] is None or settled["contact_started_at"] is None
            or settled["payload_digest"] != ready["payload_digest"]
            or reserved_output["creation_digest"] != ready["creation_digest"]):
            raise ValueError("research reserved output lacks its original actual settled contact")
        raw = read(reserved_output["file_path"], reserved_output["content_sha256"], max_bytes=16384)
        verified_child(raw, sources)
        artifact = await write_verified(jobs, job_id=child_id, owner=owner, fence=fence,
            creation_digest=ready["creation_digest"], slot=ready["slot"], kind="child", content=raw, max_bytes=16384)
        await _adopt_child(jobs, child_id=child_id, owner=owner, fence=fence, artifact=artifact,
            raw=raw, actual_cost=settled["actual_cost_microusd"])
        return artifact
    context = await _context(child, body, ready["contact_deadline_at"])
    decision, proofs = await _governed_preflight_target_async(target, context)
    if decision is None or not decision.allowed:
        raise PermissionError("research funded route is no longer governed")
    hooks = RouteReceiptSession(context=context)
    request = GpuAdmissionRequest.from_inference_context(context,
        operation_id="remote:"+child_id, parent_job_id=child["parent_job_id"], uncertain_on_error=True)
    started = False
    tokens = set_runtime_context(child["session_id"], "high_risk", trust_principal=context.principal)
    try:
        with bind_remote_inference_receipt(repository=jobs, job_id=child_id, owner=owner, fencing_token=fence):
            await prepare_bound_remote_inference(request, profile_id=setup.profile_id)

            async def invoke():
                nonlocal started
                # The canonical broker's contact writer rechecks Root, Goal,
                # policy and overrun before this callback can issue its POST.
                hooks.attempt_started(decision, capability_proof_hashes=proofs)
                started = True
                return await _governed_research_chat_completion(decision=decision,
                    context=context, body=body, api_key=target["api_key"])

            try:
                response, payload = await broker.execute(request, invoke)
            except BaseException:
                if started:
                    hooks.attempt_finished(outcome="failed", error_code="research_provider_incomplete", decision=decision)
                    await hooks.finalize(outcome="failed")
                else:
                    await hooks.finalize_denied(decision=decision, reason_codes=("research_contact_denied",))
                try:
                    receipt = broker.receipt_for(request.operation_id)
                except KeyError:
                    receipt = None
                if receipt is not None:
                    await broker.persist_receipt(receipt, repository=jobs, owner=owner, fencing_token=fence)
                raise
            hooks.attempt_finished(outcome="succeeded", error_code=None, decision=decision,
                usage=_token_usage_from_payload(payload))
            route = await hooks.finalize(outcome="succeeded")
            if not route.persisted:
                raise RuntimeError("research route receipt persistence failed")
            await broker.persist_receipt(broker.receipt_for(request.operation_id), repository=jobs,
                owner=owner, fencing_token=fence)
    finally:
        reset_runtime_context(tokens)
    await current_inputs(jobs, child["parent_job_id"])
    if time.time() >= ready["contact_deadline_at"]:
        raise TimeoutError("research output arrived after the original contact deadline")
    raw = response.choices[0].message.content.encode("utf-8")
    verified_child(raw, sources)
    snapshot = await jobs.inference_accounting_snapshot(job_id=child_id)
    settled = next((row for row in snapshot["operations"] if row["operation_id"] == request.operation_id), None)
    if snapshot.get("accounting_continuity_verified") is not True or not settled or settled["state"] != "settled":
        raise RuntimeError("research output requires actual settled accounting readback")
    artifact = await write_verified(jobs, job_id=child_id, owner=owner, fence=fence,
        creation_digest=ready["creation_digest"], slot=ready["slot"], kind="child", content=raw, max_bytes=16384)
    await _adopt_child(jobs, child_id=child_id, owner=owner, fence=fence, artifact=artifact,
        raw=raw, actual_cost=settled["actual_cost_microusd"])
    return artifact


async def _adopt_child(jobs, *, child_id, owner, fence, artifact, raw, actual_cost):
    from src.workflows.research_sources import verify_current_prompt_in_db
    async def terminal_authority(db, current):
        from src.workflows.job_runtime import _serialize
        from src.workflows.research_guard import assert_research_parent_current
        await assert_research_parent_current(db, current)
        await verify_current_prompt_in_db(jobs, db, _serialize(current))
        if read(artifact["file_path"], artifact["content_sha256"], max_bytes=16384) != raw:
            raise ValueError("research actual child output changed before terminal adoption")
    await jobs.transition_job(child_id, "succeeded", owner=owner, fencing_token=fence,
        terminal_authority_check=terminal_authority,
        result={"output_sha256": artifact["content_sha256"], "operation_id": "remote:"+child_id,
            "actual_cost_microusd": actual_cost, "no_learning": True},
        result_summary="Attributed research JSON with physical readback; no_learning")


async def validated_discovery_strategy(binding):
    """Only exact accepted ResearchStrategy directives; never private evidence."""
    if binding.status == "none":
        return None
    if binding.status != "active":
        raise ValueError("programme_research_strategy_blocked")
    from src.memory.task_lessons import ResearchStrategy
    from src.memory.m5 import sanitize_m5_memory_text_async
    try:
        strategy = ResearchStrategy.model_validate(binding.typed_data)
        data = strategy.model_dump(mode="json")
        if data != binding.typed_data:
            raise ValueError("accepted strategy cannot be normalized implicitly")
        for field in ("query_templates", "draft_sections", "stop_conditions"):
            for text in data[field]:
                if await sanitize_m5_memory_text_async(text) != text:
                    raise ValueError("accepted strategy text changed during redaction")
    except ValueError as exc:
        code = "redaction_unavailable" if "unavailable" in str(exc) else "unsupported"
        raise ValueError("programme_research_strategy_" + code) from None
    if any(len(json_bytes(_discovery_strategy_projection(data, slot))) > 8192 for slot in range(3)):
        raise ValueError("programme_research_strategy_context_unsupported")
    return data


def _discovery_strategy_projection(data, slot):
    fields = {0: ("query_templates",), 1: ("source_preferences", "required_evidence_fields"),
        2: ("draft_sections", "required_evidence_fields", "stop_conditions")}
    if slot not in fields:
        raise ValueError("programme_research_strategy_stage_unsupported")
    return {"schema_version": data["schema_version"], **{field: data[field] for field in fields[slot]}}


async def discovery_strategy_inputs(binding, slot):
    data = await validated_discovery_strategy(binding)
    reference = {key: value for key, value in binding.model_dump(mode="json").items()
        if key in {"status", "method_id", "version", "digest"}}
    if data is None:
        return {"strategy_ref": reference} if slot in {0, 1} else {}
    return {"strategy_ref": reference, "research_strategy": _discovery_strategy_projection(data, slot)}


async def execute_discovery_request(jobs, *, job_id, owner, fence, slot, instruction, supplied):
    """One public programme request through the same serial broker and ledger.

    Only the server-owned native service selects this branch. The issuer is
    provenance, never a replacement authenticated browser Root.
    """
    import asyncio
    from src.workflows.research_sources import physical_discovery_inputs
    from src.workflows.research_guard import discovery_writer_scope
    from src.workflows.research_native import adopt_discovery_artifact
    from src.work_board.research_artifacts import stage_discovery_artifact
    from src.work_board.research_parent import DISCOVERY_SERVICE
    from src.approval.runtime import set_runtime_context, reset_runtime_context
    from src.security.trust_contract import TrustPrincipal, PrincipalType, AuthorityGrant
    from src.model_fabric.caller_context import build_canonical_inference_context
    from src.model_fabric.contracts import finalized_openai_compatible_body
    from src.model_fabric.hooks import RouteReceiptSession
    from src.model_fabric.remote_inference_admission import (GpuAdmissionRequest, bind_remote_inference_receipt,
        prepare_bound_remote_inference, remote_inference_admission_broker as broker)
    from src.llm_runtime import _governed_preflight_target_async, _governed_research_chat_completion, _token_usage_from_payload
    witness = await physical_discovery_inputs(jobs, job_id)
    if type(slot) is not int or not 0 <= slot < witness.plan.limits.max_inference_requests:
        raise ValueError("programme original inference request cap exceeded")
    if len(witness.public_brief.encode()) > 2048:
        raise ValueError("programme_public_brief_context_unsupported")
    expected_strategy = await discovery_strategy_inputs(witness.plan.strategy_binding, slot)
    for key in ("strategy_ref", "research_strategy"):
        if (key in supplied) != (key in expected_strategy) or supplied.get(key) != expected_strategy.get(key):
            raise ValueError("programme_research_strategy_original_input_changed")
    setup, policy_digest, target = _target()
    body = finalized_openai_compatible_body(model_id=target["model_id"],
        messages=[{"role": "system", "content": instruction},
            {"role": "user", "content": json_bytes({"public_brief": witness.public_brief,
                "untrusted_public_data": supplied, "no_learning": True}).decode()}],
        options=target["options"], temperature=setup.temperature,
        max_tokens=min(1024, setup.max_output_tokens), stream=False)
    raw_body = json_bytes(body)
    if len(raw_body) > 8192:
        raise ValueError("programme_serialized_prompt_context_unsupported")
    artifact = stage_discovery_artifact(programme_id=witness.plan.programme_id.hex,
        job_id=job_id, kind="prompt", slot=slot, content=raw_body)
    async with discovery_writer_scope(witness=witness):
        await adopt_discovery_artifact(jobs, job_id=job_id, owner=owner, fence=fence, artifact=artifact)
    witness = await physical_discovery_inputs(jobs, job_id)
    principal = TrustPrincipal(principal_id=DISCOVERY_SERVICE, principal_type=PrincipalType.SERVICE,
        grants=(AuthorityGrant.MODEL_INFERENCE,), job_id=job_id)
    remaining = witness.plan.deadline_at.timestamp() - time.time()
    if remaining <= 0:
        raise TimeoutError("programme original deadline expired")
    operation_id = "remote:" + job_id + ":" + str(slot)
    context = build_canonical_inference_context("readonly_research_child", payload=body,
        output_tokens=body["max_tokens"], timeout_seconds=remaining, principal=principal,
        job_id=job_id, request_id=operation_id)
    context = replace(context, deadline_at=witness.plan.deadline_at.timestamp(),
        owner_budget_microusd=witness.plan.limits.cost_limit_microusd,
        requirements=replace(context.requirements, max_latency_ms=int(setup.timeout_seconds * 1000)))
    decision, proofs = await _governed_preflight_target_async(target, context)
    if decision is None or not decision.allowed:
        raise PermissionError("programme fixed route lacks current governed capability proof")
    request = GpuAdmissionRequest.from_inference_context(context, operation_id=operation_id, uncertain_on_error=True)
    hooks = RouteReceiptSession(context=context)
    tokens = set_runtime_context(None, "high_risk", trust_principal=principal)
    started = False
    try:
        with bind_remote_inference_receipt(repository=jobs, job_id=job_id, owner=owner, fencing_token=fence):
            async with discovery_writer_scope(witness=witness):
                await prepare_bound_remote_inference(request, profile_id=setup.profile_id)

            async def invoke():
                nonlocal started
                # The serial broker may have waited since preparation. Reopen
                # the original inputs and validate their admitted method pin
                # before publishing a contact attempt or sending the request.
                await physical_discovery_inputs(jobs, job_id)
                hooks.attempt_started(decision, capability_proof_hashes=proofs)
                started = True
                remaining = context.deadline_at - time.time()
                async with asyncio.timeout(min(setup.timeout_seconds, remaining)):
                    return await _governed_research_chat_completion(decision=decision, context=context,
                        body=body, api_key=target["api_key"])

            try:
                response, payload = await broker.execute(request, invoke)
            except BaseException:
                if started:
                    hooks.attempt_finished(outcome="failed", error_code="programme_provider_incomplete", decision=decision)
                    await hooks.finalize(outcome="failed")
                else:
                    await hooks.finalize_denied(decision=decision, reason_codes=("programme_contact_denied",))
                try:
                    receipt = broker.receipt_for(operation_id)
                    witness = await physical_discovery_inputs(jobs, job_id)
                    async with discovery_writer_scope(witness=witness):
                        await broker.persist_receipt(receipt, repository=jobs, owner=owner, fencing_token=fence)
                except Exception:
                    pass  # Original unknown intent and ledger remain authoritative.
                raise
            hooks.attempt_finished(outcome="succeeded", error_code=None, decision=decision,
                usage=_token_usage_from_payload(payload))
            route = await hooks.finalize(outcome="succeeded")
            if not route.persisted:
                raise RuntimeError("programme route receipt persistence failed")
            witness = await physical_discovery_inputs(jobs, job_id)
            async with discovery_writer_scope(witness=witness):
                await broker.persist_receipt(broker.receipt_for(operation_id), repository=jobs,
                    owner=owner, fencing_token=fence)
    finally:
        reset_runtime_context(tokens)
    raw = response.choices[0].message.content.encode("utf-8")
    if not 0 < len(raw) <= 16384:
        raise ValueError("programme child output exceeds its original bound")
    snapshot = await jobs.inference_accounting_snapshot(job_id=job_id)
    settled = next((row for row in snapshot["operations"] if row["operation_id"] == operation_id), None)
    if snapshot.get("accounting_continuity_verified") is not True or not settled or settled["state"] != "settled":
        raise RuntimeError("programme output lacks actual original settlement")
    witness = await physical_discovery_inputs(jobs, job_id)
    artifact = stage_discovery_artifact(programme_id=witness.plan.programme_id.hex,
        job_id=job_id, kind="child", slot=slot, content=raw)
    async with discovery_writer_scope(witness=witness):
        await adopt_discovery_artifact(jobs, job_id=job_id, owner=owner, fence=fence, artifact=artifact)
    return json.loads(raw)
