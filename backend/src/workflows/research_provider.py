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
    async def terminal_authority(db, current):
        from src.workflows.job_runtime import _serialize
        from src.workflows.research_guard import assert_research_parent_current
        await assert_research_parent_current(db, current)
        await verify_current_prompt_in_db(jobs, db, _serialize(current))
        if read(artifact["file_path"], artifact["content_sha256"], max_bytes=16384) != raw:
            raise ValueError("research actual child output changed before terminal adoption")
    await jobs.transition_job(child_id, "succeeded", owner=owner, fencing_token=fence,
        terminal_authority_check=terminal_authority,
        result={"output_sha256": artifact["content_sha256"], "operation_id": request.operation_id,
            "actual_cost_microusd": settled["actual_cost_microusd"], "no_learning": True},
        result_summary="Attributed research JSON with physical readback; no_learning")
    return artifact
