"""Bounded audio executor under the existing Python Workflow owner."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import base64
import hashlib
import json
import time
import uuid

from src.workflows.audio_native import AudioNativeAdmissionBindingV1, AudioTranscriptionResultV1, AUTHORITY_KEY, digest, utc


async def execute(worker, row, budget):
    from src.auth.service import authenticate_session
    from src.db.models import AudioConsentGrant, OperatorSession
    from src.llm_runtime import (_provider_profile, _profile_options, build_audio_chat_body,
        _governed_preflight_target_async, _governed_audio_chat_completion, _token_usage_from_payload)
    from src.approval.runtime import set_runtime_context, reset_runtime_context
    from src.model_fabric.hooks import RouteReceiptSession
    from src.model_fabric.audio_contracts import audio_route_witness, input_audio_payload
    from src.guardian.audio_ingress import OpenRouterInputAudio
    from src.model_fabric.effective_policy import current_inference_policy
    from src.model_fabric.caller_context import build_canonical_inference_context
    from src.model_fabric.remote_inference_admission import (GpuAdmissionRequest,
        bind_remote_inference_receipt, prepare_bound_remote_inference)
    from src.workflows.job_runtime import durable_job_repository as jobs, DurableJobSpec, DurableJobIdentity
    from src.guardian.audio_worker import AudioWorkerError, _decode_with_ffmpeg, _read_wav_and_write_normalized
    from pathlib import Path

    if type(budget) is not int or not 1 <= budget <= 1_000_000_000:
        raise AudioWorkerError("audio_positive_original_budget_required")
    operator = await authenticate_session(row.operator_session_id, touch=False)
    configured, policy = current_inference_policy()
    profile = _provider_profile("openrouter.audio")
    if profile is None:
        raise AudioWorkerError("audio_setup_v3_required")
    try:
        evidence, proof = await audio_route_witness(profile)
    except PermissionError as exc:
        raise AudioWorkerError(str(exc)) from exc
    if evidence.pricing.reserve_microusd > budget:
        raise AudioWorkerError("audio_original_budget_insufficient")
    job_id = "audio-transcription:" + hashlib.sha256(row.id.encode()).hexdigest()[:40]
    async with jobs._session() as db:
        root = await db.get(OperatorSession, row.operator_session_id)
        grants = (await db.execute(__import__("sqlmodel").select(AudioConsentGrant).where(
            AudioConsentGrant.reference.in_((row.capture_consent_reference, row.model_consent_reference))))).scalars().all()
        if root is None or len(grants) != 2:
            raise AudioWorkerError("audio_original_grant_invalid")
        deadline = min(utc(row.raw_audio_retention_deadline), utc(root.absolute_expires_at), *(utc(g.expires_at) for g in grants))
    binding = AudioNativeAdmissionBindingV1("audio-native-admission.v1", row.id, job_id,
        "remote:" + job_id, row.owner_principal_id, row.operator_session_id, row.session_id,
        row.request_digest, row.capture_consent_reference, row.model_consent_reference,
        profile.contract_hash, proof, policy, deadline.isoformat(), budget, 1)
    spec = DurableJobSpec(identity=DurableJobIdentity(job_id=job_id, job_kind="audio_transcription_v1",
        owner_kind="user", owner_principal_id=row.owner_principal_id, capability_version="audio-transcription-v1",
        idempotency_scope="audio-ingress", idempotency_key=row.request_id),
        inputs={"capture_binding_digest": row.request_digest}, session_id=row.session_id,
        operator_session_id=row.operator_session_id, resource_claims=("remote_inference",),
        priority=100, deadline_at=deadline, max_attempts=1, budget_microusd=budget,
        declared_authority={"principal": row.owner_principal_id, "owner_kind": "user",
            "session_id": row.session_id, "budget_microusd": budget, AUTHORITY_KEY: binding.payload()})
    admitted = await jobs.admit_job(spec, audio_admission=binding)
    row = await worker._job(row.request_id)
    if admitted["status"] not in {"accepted", "queued"}:
        return worker._snapshot(row)
    lease_owner = "audio-worker:" + uuid.uuid4().hex
    claimed = await jobs.audio_transition(job_id, expected_revision=admitted["revision"],
        expected_audio_revision=row.revision, status="running", audio_changes={"status": "processing"},
        owner=lease_owner, fencing_token=admitted["lease"]["fencing_token"])
    fence = claimed["lease"]["fencing_token"]
    try:
        raw_path, normalized = worker._validated_job_paths(row.raw_path, row.normalized_path)
        source = worker._read_quarantine_bytes(raw_path)
        if evidence.endpoint.input_format == "wav":
            if row.container == "wav":
                decoded = _read_wav_and_write_normalized(raw_path, normalized)
            else:
                decoded = await _decode_with_ffmpeg(raw_path, normalized,
                    (row.media_type, row.container, row.codec), original_deadline_at=deadline)
            payload = worker._read_quarantine_bytes(normalized)
            duration = decoded.normalized_duration_seconds
        else:
            # The existing bounded inspector/converter proves the compressed
            # source shape before the actual native bytes are selected.
            decoded = await _decode_with_ffmpeg(raw_path, normalized,
                (row.media_type, row.container, row.codec), original_deadline_at=deadline)
            if (row.container, row.codec) != ("m4a" if evidence.endpoint.container == "mp4" else evidence.endpoint.container, evidence.endpoint.codec):
                raise AudioWorkerError("audio_format_unverified")
            payload, duration = source, decoded.source_duration_seconds
        if duration * 1000 > evidence.endpoint.max_duration_millis:
            raise AudioWorkerError("audio_duration_witness_exceeded")
        encoded = base64.b64encode(payload).decode("ascii")
        input_audio_payload(encoded, evidence.endpoint.input_format, evidence.endpoint)
        part = OpenRouterInputAudio(data=encoded, format=evidence.endpoint.input_format)
        body = build_audio_chat_body(profile=profile, input_audio=part, evidence=evidence)
        principal = replace(operator.principal, session_id=row.session_id, job_id=job_id,
            operator_session_id=row.operator_session_id)
        context = build_canonical_inference_context("audio_transcription", payload=body,
            output_tokens=body["max_tokens"], timeout_seconds=max(0.001, deadline.timestamp()-time.time()),
            principal=principal, session_id=row.session_id, job_id=job_id, request_id=binding.operation_id)
        context = replace(context, deadline_at=deadline.timestamp(), estimated_cost_microusd=evidence.pricing.reserve_microusd,
            owner_budget_microusd=budget, requirements=replace(context.requirements, max_latency_ms=profile.max_latency_ms))
        target = {"profile": profile.id, "model_id": profile.routing_model or profile.model,
            "api_base": profile.api_base, "api_key": profile.api_key, "source": "primary", "options": _profile_options(profile.id)}
        decision, proofs = await _governed_preflight_target_async(target, context)
        if decision is None or not decision.allowed:
            raise AudioWorkerError("audio_route_not_ready")
        request = GpuAdmissionRequest.from_inference_context(context, operation_id=binding.operation_id, uncertain_on_error=True)
        hooks = RouteReceiptSession(context=context)
        started = False
        tokens = set_runtime_context(row.session_id, "high_risk", trust_principal=principal)
        try:
            with bind_remote_inference_receipt(repository=jobs, job_id=job_id, owner=lease_owner, fencing_token=fence):
                await prepare_bound_remote_inference(request, profile_id=profile.id)
                async def invoke():
                    nonlocal started
                    hooks.attempt_started(decision, capability_proof_hashes=proofs)
                    started = True
                    return await _governed_audio_chat_completion(decision=decision, context=context,
                        input_audio=part, evidence=evidence, api_key=profile.api_key)
                try:
                    response, _payload = await worker.admission_broker.execute(request, invoke)
                except BaseException:
                    if started:
                        hooks.attempt_finished(outcome="failed", error_code="audio_provider_incomplete", decision=decision)
                        await hooks.finalize(outcome="failed")
                    else:
                        await hooks.finalize_denied(decision=decision, reason_codes=("audio_contact_denied",))
                    try:
                        receipt = worker.admission_broker.receipt_for(request.operation_id)
                    except KeyError:
                        receipt = None
                    if receipt is not None:
                        await worker.admission_broker.persist_receipt(receipt, repository=jobs,
                            owner=lease_owner, fencing_token=fence)
                    raise
                hooks.attempt_finished(outcome="succeeded", error_code=None, decision=decision,
                    usage=_token_usage_from_payload(_payload))
                route = await hooks.finalize(outcome="succeeded")
                if not route.persisted:
                    raise AudioWorkerError("audio_route_receipt_unpersisted")
                await worker.admission_broker.persist_receipt(worker.admission_broker.receipt_for(request.operation_id),
                    repository=jobs, owner=lease_owner, fencing_token=fence)
        finally:
            reset_runtime_context(tokens)
        snapshot = await jobs.inference_accounting_snapshot(job_id=job_id)
        cost = next((op for op in snapshot["operations"] if op["operation_id"] == binding.operation_id), None)
        if not cost or cost["state"] != "settled" or cost["actual_cost_microusd"] is None:
            raise AudioWorkerError("audio_provider_cost_unknown")
        transcript = response.choices[0].message.content
        from src.model_fabric.audio_contracts import validate_audio_response
        text, generation, actual = validate_audio_response(_payload,
            model_id=evidence.endpoint.model_id, upstream=evidence.endpoint.upstream_endpoint_tag)
        result = AudioTranscriptionResultV1("audio-transcription-result.v1", row.id, binding.operation_id,
            text, hashlib.sha256(text.encode()).hexdigest(), evidence.endpoint.model_id,
            evidence.endpoint.upstream_endpoint_tag, generation, actual, "settled")
        current, audio = await jobs.get_job(job_id), await worker._job(row.request_id)
        await jobs.audio_transition(job_id, expected_revision=current["revision"], expected_audio_revision=audio.revision,
            status="succeeded", owner=lease_owner, fencing_token=fence,
            audio_result=result,
            audio_changes={"status": "transcript_ready", "transcript": transcript,
                "transcript_digest": hashlib.sha256(transcript.encode()).hexdigest(), "provider_status": "verified",
                "transport_status": "settled", "error_code": None})
        worker._set_review_transcript(await worker._job(row.request_id), transcript)
    except BaseException:
        current, audio = await jobs.get_job(job_id), await worker._job(row.request_id)
        if current["status"] == "running":
            await jobs.audio_transition(job_id, expected_revision=current["revision"], expected_audio_revision=audio.revision,
                status="failed", owner=lease_owner, fencing_token=fence, require_current=False,
                audio_changes={"status": "blocked", "error_code": "audio_execution_incomplete", "transcript": None})
        raise
    return worker._snapshot(await worker._job(row.request_id))
