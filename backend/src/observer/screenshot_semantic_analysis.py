"""Provider-backed semantic analysis for screenshot-folder images."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
import time
from typing import Any
from datetime import datetime, timezone

import httpx

from config.settings import settings
from src.llm_runtime import provider_profiles
from src.model_fabric import (
    NoCompliantModelRouteError,
    PersistedRouteReceiptHooks,
    ProviderProfile,
    bind_final_inference_payload,
    candidate_from_profile,
    model_fabric_repository,
    run_preflighted_adapter,
    select_route,
    finalized_openai_compatible_body,
)
from src.model_fabric.caller_context import build_canonical_inference_context
from src.model_fabric.configuration import effective_workload_policy
from src.model_fabric.proofs import proof_is_fresh
from src.model_fabric.remote_inference_admission import (
    RemoteInferenceAdmissionError,
    remote_inference_admission_broker,
)
from src.security.trust_contract import EgressClass
from src.observer.screen_analysis_settings import (
    effective_screen_analysis_enabled,
    effective_screen_analysis_model,
    effective_screen_analysis_provider,
)
from src.observer.screenshot_analysis_contract import (
    ScreenshotAnalysis,
    ScreenshotAnalysisContractError,
    SCREENSHOT_ANALYSIS_PROMPT_VERSION,
    SCREENSHOT_ANALYSIS_SCHEMA_VERSION,
    parse_screenshot_analysis_output,
    screenshot_analysis_prompt,
)
from src.vlm_runtime import (
    SCREENSHOT_VLM_PROFILE_ID,
)

logger = logging.getLogger(__name__)

SCREENSHOT_ANALYSIS_DETAIL_PREFIX = "screenshot_analysis:"
SCREENSHOT_ANALYSIS_ERROR_DETAIL_PREFIX = "screenshot_analysis_error:"
SCREENSHOT_ANALYSIS_STATUS_DETAIL_PREFIX = "screenshot_analysis_status:"
REANALYSIS_REASONS = {
    "prompt_version_changed",
    "model_version_changed",
    "provider_failure_retry",
    "manual_operator_request",
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
REMOTE_SCREENSHOT_TIMEOUT_SECONDS = 120


class ScreenshotSemanticAnalysisError(RuntimeError):
    """Raised when the configured semantic screenshot analyzer fails."""


def screenshot_semantic_analysis_enabled() -> bool:
    """Return true only when remote vision is explicitly configured and allowed.
    """
    if not effective_screen_analysis_enabled():
        return False
    provider = effective_screen_analysis_provider().lower()
    if provider != "openrouter":
        return False
    if not effective_screen_analysis_model() or not settings.openrouter_api_key.strip():
        return False
    if not bool(getattr(settings, "openrouter_provider_only", True)):
        return False
    if not str(getattr(settings, "openrouter_allowed_upstreams", "") or "").strip():
        return False
    if bool(getattr(settings, "openrouter_allow_fallbacks", False)):
        return False
    if bool(getattr(settings, "openrouter_require_parameters", True)) is not True:
        return False
    if str(getattr(settings, "openrouter_data_collection", "deny") or "deny") != "deny":
        return False
    if not bool(getattr(settings, "openrouter_zero_data_retention", False)):
        return False
    try:
        policy = effective_workload_policy("screenshot_image_analysis")
    except Exception:
        return False
    now = time.time()
    if (
        policy.egress_class is not EgressClass.CLOUD_ALLOWED_FULL
        or not bool(policy.cloud_egress_acknowledged)
        or policy.fallback_allowed
        or policy.max_cost_microusd is None
        or set(policy.allowed_provider_kinds) != {"openrouter"}
    ):
        return False
    profile = provider_profiles().get(SCREENSHOT_VLM_PROFILE_ID)
    if profile is None or profile.provider_kind != "openrouter":
        return False
    if (
        "vision" not in profile.capabilities
        or "structured_output" not in profile.capabilities
        or profile.context_window_tokens is None
        or profile.max_output_tokens is None
        or profile.max_latency_ms is None
        or profile.cost_microusd is None
        or not profile.cost_source
        or profile.cost_source_updated_at is None
        or profile.cost_source_updated_at < now - 2_592_000
        or profile.cost_source_updated_at > now
        or profile.cost_microusd > policy.max_cost_microusd
        or profile.max_output_tokens < 64
        or profile.max_latency_ms <= 0
    ):
        return False
    provider_options = profile.options.get("provider") if isinstance(profile.options, dict) else None
    return isinstance(provider_options, dict) and bool(provider_options.get("only"))


async def screenshot_semantic_analysis_ready(*, timeout_seconds: float = 2.0) -> bool:
    """Return true when remote vision is configured without paid probing."""
    if not screenshot_semantic_analysis_enabled():
        return False
    return await _openrouter_profile_proofs_ready(timeout_seconds=timeout_seconds)


async def screenshot_semantic_analysis_accepting_background_work(*, timeout_seconds: float = 2.0) -> bool:
    """Return true when the VLM wrapper can accept one background image job."""
    return await screenshot_semantic_analysis_background_slots(timeout_seconds=timeout_seconds) > 0


async def screenshot_semantic_analysis_background_slots(*, timeout_seconds: float = 2.0) -> int:
    """Return one bounded remote-inference feeder slot when queue capacity exists."""
    if not screenshot_semantic_analysis_enabled():
        return 0
    proofs_ready = await _openrouter_profile_proofs_ready(timeout_seconds=timeout_seconds)
    try:
        status = await remote_inference_admission_broker.status()
    except Exception:
        return 0
    capacity = status.get("capacity") if isinstance(status, dict) else None
    if not isinstance(capacity, dict):
        return 0
    try:
        available = int(capacity.get("available", 0))
    except (TypeError, ValueError):
        return 0
    if available <= 0:
        return 0
    # A configured profile without a fresh proof gets one bounded worker slot
    # solely to persist a blocked/degraded observation receipt.  It never
    # reaches the provider: _run_governed_vlm_adapter still fails closed at
    # selector preflight.  Returning zero here would strand rows forever in
    # ``pending`` with no operator-visible reason.
    if not proofs_ready:
        logger.info("screenshot semantic analysis has no fresh capability proof; admitting one blocked-receipt attempt")
    return 1


async def _screenshot_semantic_analysis_health(*, timeout_seconds: float = 2.0) -> dict[str, Any] | None:
    """Return operator-safe configuration health without contacting a provider."""
    if not screenshot_semantic_analysis_enabled():
        return None
    return {"provider": "openrouter", "configured": True, "paid_probe": False}


async def _screenshot_semantic_analysis_queue_status(*, timeout_seconds: float = 2.0) -> dict[str, Any] | None:
    """Return the process-local remote admission state."""
    if not screenshot_semantic_analysis_enabled():
        return None
    return await remote_inference_admission_broker.status()


async def _screenshot_semantic_analysis_backend_ready(*, timeout_seconds: float = 2.0) -> bool:
    """Return whether remote vision is configured; dispatch proves reachability."""
    if not screenshot_semantic_analysis_enabled():
        return False
    return True


async def _openrouter_profile_proofs_ready(*, timeout_seconds: float = 2.0) -> bool:
    """Check persisted vision/response proofs before admitting background work."""
    profile = provider_profiles().get(SCREENSHOT_VLM_PROFILE_ID)
    if profile is None:
        return False
    candidate = candidate_from_profile(profile)
    required = {"vision", "structured_output", "latency_ms", "health"}
    deadline = time.monotonic() + max(float(timeout_seconds), 0.1)
    for capability in sorted(required):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            proof = await asyncio.wait_for(
                model_fabric_repository.latest_capability_proof(
                    profile_schema_version=profile.schema_version,
                    profile_contract_hash=profile.contract_hash,
                    profile_id=profile.id,
                    model=profile.model,
                    endpoint=candidate.endpoint,
                    endpoint_class=candidate.endpoint_class,
                    adapter=candidate.adapter,
                    capability=capability,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False
        if proof is None or not proof_is_fresh(proof):
            return False
        if (
            proof.profile_schema_version != profile.schema_version
            or proof.profile_contract_hash != profile.contract_hash
            or proof.profile_id != profile.id
            or proof.model != profile.model
            or proof.endpoint != candidate.endpoint
            or proof.endpoint_class != candidate.endpoint_class
            or proof.adapter != candidate.adapter
        ):
            return False
        if capability == "health" and proof.proven_value != "healthy":
            return False
        if capability == "latency_ms":
            try:
                if int(proof.proven_value) > int(profile.max_latency_ms or 0):
                    return False
            except (TypeError, ValueError):
                return False
    return True


async def analyze_screenshot_image(image_path: Path, artifacts: dict[str, Any]) -> ScreenshotAnalysis | None:
    """Analyze one screenshot image through the governed OpenRouter vision route."""
    if not screenshot_semantic_analysis_enabled():
        return None
    try:
        return await _analyze_with_openrouter(image_path, artifacts)
    except NoCompliantModelRouteError as exc:
        raise ScreenshotSemanticAnalysisError("remote_inference_blocked:no_compliant_route") from exc
    except RemoteInferenceAdmissionError as exc:
        code = str(getattr(exc, "code", "admission_failed") or "admission_failed")
        raise ScreenshotSemanticAnalysisError(f"remote_inference_blocked:{_bounded_reason(code)}") from exc
    except (PermissionError, ValueError) as exc:
        # Canonical context construction and route binding fail closed before
        # any provider call. Translate those denials into the same bounded
        # observation-level status that the folder worker persists.
        raise ScreenshotSemanticAnalysisError(
            f"remote_inference_blocked:{_bounded_reason(str(exc))}"
        ) from exc


def screenshot_analysis_detail(analysis: ScreenshotAnalysis) -> str:
    """Serialize a validated screenshot analysis for ScreenObservation details."""
    payload = analysis.model_dump(mode="json")
    return SCREENSHOT_ANALYSIS_DETAIL_PREFIX + json.dumps(payload, sort_keys=True, separators=(",", ":"))


def screenshot_analysis_status_detail(
    status: str,
    *,
    reason: str | None = None,
    reanalysis_reason: str | None = None,
    attempts: int | None = None,
) -> str:
    """Serialize semantic analysis status for idempotency and reanalysis decisions."""
    payload = {
        "status": status,
        "provider": effective_screen_analysis_provider() or "not_configured",
        "model": effective_screen_analysis_model() or None,
        "schema_version": SCREENSHOT_ANALYSIS_SCHEMA_VERSION,
        "prompt_version": SCREENSHOT_ANALYSIS_PROMPT_VERSION,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if reason:
        payload["reason"] = _bounded_reason(reason)
    if reanalysis_reason:
        payload["reanalysis_reason"] = reanalysis_reason
    if attempts is not None:
        payload["attempts"] = max(int(attempts), 0)
    return SCREENSHOT_ANALYSIS_STATUS_DETAIL_PREFIX + json.dumps(payload, sort_keys=True, separators=(",", ":"))


def screenshot_analysis_error_detail(reason: str) -> str:
    """Serialize a bounded analyzer failure for ScreenObservation details."""
    return SCREENSHOT_ANALYSIS_ERROR_DETAIL_PREFIX + json.dumps(
        {"provider": effective_screen_analysis_provider() or "unknown", "reason": _bounded_reason(reason)},
        sort_keys=True,
        separators=(",", ":"),
    )


def semantic_analysis_from_details(details: list[Any]) -> dict[str, Any] | None:
    """Extract the persisted semantic analysis payload from observation details."""
    for item in details:
        if not isinstance(item, str) or not item.startswith(SCREENSHOT_ANALYSIS_DETAIL_PREFIX):
            continue
        try:
            payload = json.loads(item.removeprefix(SCREENSHOT_ANALYSIS_DETAIL_PREFIX))
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            return payload
    return None


def semantic_analysis_error_from_details(details: list[Any]) -> dict[str, Any] | None:
    """Extract the persisted semantic analysis failure payload from observation details."""
    for item in details:
        if not isinstance(item, str) or not item.startswith(SCREENSHOT_ANALYSIS_ERROR_DETAIL_PREFIX):
            continue
        try:
            payload = json.loads(item.removeprefix(SCREENSHOT_ANALYSIS_ERROR_DETAIL_PREFIX))
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            return payload
    return None


def semantic_analysis_status_from_details(details: list[Any]) -> dict[str, Any] | None:
    """Extract the latest semantic analysis status payload from observation details."""
    latest: dict[str, Any] | None = None
    for item in details:
        if not isinstance(item, str) or not item.startswith(SCREENSHOT_ANALYSIS_STATUS_DETAIL_PREFIX):
            continue
        try:
            payload = json.loads(item.removeprefix(SCREENSHOT_ANALYSIS_STATUS_DETAIL_PREFIX))
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            latest = payload
    return latest


def semantic_analysis_needs_reanalysis(details: list[Any]) -> bool:
    """Return true when stored analysis exists but belongs to an old prompt or model contract."""
    analysis = semantic_analysis_from_details(details)
    status = semantic_analysis_status_from_details(details)
    if analysis is None or status is None:
        return False
    return (
        analysis.get("prompt_version") != SCREENSHOT_ANALYSIS_PROMPT_VERSION
        or analysis.get("schema_version") != SCREENSHOT_ANALYSIS_SCHEMA_VERSION
        or status.get("model") != (effective_screen_analysis_model() or None)
    )


def validate_reanalysis_reason(reason: str) -> str:
    """Validate the explicit operator reason required before reanalysis."""
    normalized = str(reason or "").strip()
    if normalized not in REANALYSIS_REASONS:
        allowed = ", ".join(sorted(REANALYSIS_REASONS))
        raise ValueError(f"reanalysis_reason must be one of: {allowed}")
    return normalized


def replace_semantic_analysis_details(
    details: list[Any],
    *,
    analysis: ScreenshotAnalysis | None,
    error_reason: str | None,
    reanalysis_reason: str,
    status: str | None = None,
) -> list[str]:
    """Replace existing semantic analysis details while preserving capture metadata."""
    next_details = [
        item
        for item in details
        if not (
            isinstance(item, str)
            and (
                item.startswith(SCREENSHOT_ANALYSIS_DETAIL_PREFIX)
                or item.startswith(SCREENSHOT_ANALYSIS_ERROR_DETAIL_PREFIX)
                or item.startswith(SCREENSHOT_ANALYSIS_STATUS_DETAIL_PREFIX)
            )
        )
    ]
    if analysis is not None:
        next_details.append(screenshot_analysis_detail(analysis))
        next_details.append(
            screenshot_analysis_status_detail("succeeded", reanalysis_reason=reanalysis_reason)
        )
    else:
        reason = error_reason or "unknown"
        # Policy and admission denials are terminal until an operator changes
        # the governing configuration.  Preserve that distinction for the
        # explicit reanalysis endpoint as well as the scheduled worker; a
        # generic ``failed`` receipt would make a blocked item look retryable.
        resolved_status = status or (
            "blocked" if reason.startswith("remote_inference_blocked:") else "failed"
        )
        next_details.append(screenshot_analysis_error_detail(reason))
        next_details.append(
            screenshot_analysis_status_detail(
                resolved_status,
                reason=reason,
                reanalysis_reason=reanalysis_reason,
            )
        )
    return [str(item) for item in next_details if isinstance(item, str)]


async def _analyze_with_openrouter(image_path: Path, artifacts: dict[str, Any]) -> ScreenshotAnalysis:
    """Send one bounded, authorized image request to OpenRouter.

    The local path is intentionally represented as inline bytes in the
    governed request body.  Provider responses are treated as untrusted data
    and validated by the Seraph-owned screenshot contract before persistence.
    """
    metadata = {
        "captured_at": artifacts.get("created_at"),
        "source": "screenshot_folder",
        "filename": image_path.name,
        "image_sha256": artifacts.get("image_sha256"),
        "file_format": artifacts.get("file_format"),
        "width": artifacts.get("width"),
        "height": artifacts.get("height"),
    }
    prompt = screenshot_analysis_prompt(metadata)
    profile = provider_profiles().get(SCREENSHOT_VLM_PROFILE_ID)
    if profile is None:
        raise ScreenshotSemanticAnalysisError("openrouter screenshot profile is not configured")
    if profile.provider_kind != "openrouter" or profile.api_base != "https://openrouter.ai/api/v1":
        raise ScreenshotSemanticAnalysisError("local screenshot inference is disabled in the active phase")

    # Read a bounded artifact synchronously.  The folder analyser already
    # limits one image to ``MAX_IMAGE_BYTES`` and performs this work in its
    # bounded scheduler lane; using the default executor here would make
    # cancellation/restart recovery depend on an unbounded process-wide
    # thread pool.
    image_bytes = image_path.read_bytes()
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise ScreenshotSemanticAnalysisError("screenshot exceeds the 8 MiB remote-analysis limit")
    api_key = profile.api_key
    if not api_key:
        raise ScreenshotSemanticAnalysisError("OpenRouter credential is not configured")
    media_type = _image_media_type(image_path)
    image_data_url = f"data:{media_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        }
    ]
    transport_body = finalized_openai_compatible_body(
        model_id=profile.model,
        messages=messages,
        options=profile.options,
        temperature=0.0,
        max_tokens=1400,
    )
    context = build_canonical_inference_context(
        "screenshot_image_analysis",
        payload=transport_body,
        output_tokens=1400,
        timeout_seconds=min(max(int(settings.agent_chat_timeout), 1), REMOTE_SCREENSHOT_TIMEOUT_SECONDS),
    )
    context = bind_final_inference_payload(context, transport_body)

    async def _transport(candidate, follow_redirects: bool) -> ScreenshotAnalysis:
        if follow_redirects:
            raise ScreenshotSemanticAnalysisError("OpenRouter redirects are forbidden")
        endpoint = candidate.endpoint
        remaining_seconds = context.deadline_at - time.time()
        if remaining_seconds <= 0:
            raise ScreenshotSemanticAnalysisError("OpenRouter vision deadline expired")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(remaining_seconds),
            follow_redirects=False,
        ) as client:
            response = await client.post(
                endpoint,
                json=transport_body,
                headers=headers,
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Never copy provider bodies into operator receipts; status is
            # enough to classify the bounded failure.
            raise ScreenshotSemanticAnalysisError(
                f"OpenRouter vision request failed with HTTP {exc.response.status_code}"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ScreenshotSemanticAnalysisError("OpenRouter vision response was not JSON") from exc
        return parse_screenshot_analysis_output(_openrouter_analysis_payload(payload))

    try:
        result = await _run_governed_vlm_adapter(
            context=context,
            profile=profile,
            transport=_transport,
        )
        if not isinstance(result, ScreenshotAnalysis):
            raise ScreenshotSemanticAnalysisError("OpenRouter adapter returned an invalid analysis result")
        return result
    except (OSError, httpx.HTTPError, ValueError, ScreenshotAnalysisContractError) as exc:
        logger.warning("screenshot semantic analysis failed for %s: %s", image_path, exc)
        raise ScreenshotSemanticAnalysisError(str(exc)) from exc


async def _analyze_with_local_vlm(image_path: Path, artifacts: dict[str, Any]) -> ScreenshotAnalysis:
    """Retain the old symbol as a hard-failing migration diagnostic.

    Keeping this name avoids an import-time break for archived tooling while
    making the former local wrapper route impossible to execute.  Active
    screenshot analysis has one provider path: the governed OpenRouter route.
    """
    raise ScreenshotSemanticAnalysisError("local_vlm_disabled_openrouter_only")


async def _run_governed_vlm_adapter(*, context, profile: ProviderProfile, transport):
    """Preflight and receipt-wrap a governed OpenRouter chat vision transport."""
    candidate = candidate_from_profile(profile)
    if candidate.adapter != "openai_compatible_chat":
        raise ScreenshotSemanticAnalysisError(
            "screenshot profile requires the OpenRouter chat adapter"
        )
    capabilities = {*context.requirements.capabilities, "latency_ms", "health"}
    proofs = []
    for capability in sorted(capabilities):
        proof = await model_fabric_repository.latest_capability_proof(
            profile_schema_version=profile.schema_version,
            profile_contract_hash=profile.contract_hash,
            profile_id=profile.id,
            model=profile.model,
            endpoint=candidate.endpoint,
            endpoint_class=candidate.endpoint_class,
            adapter=candidate.adapter,
            capability=capability,
        )
        if proof is not None:
            proofs.append(proof)
    decision = select_route(context, (candidate,), tuple(proofs))
    hooks = PersistedRouteReceiptHooks(
        capability_proof_hashes=tuple(proof.proof_hash for proof in proofs),
    )
    result = await run_preflighted_adapter(
        context=context,
        decision=decision,
        adapter=transport,
        hooks=hooks,
    )
    persistence = await hooks.persistence_result(context.request_id)
    if persistence is None or not persistence.persisted:
        raise ScreenshotSemanticAnalysisError("VLM route receipt persistence failed")
    return result


def _openrouter_analysis_payload(payload: Any) -> str | dict[str, Any]:
    """Extract untrusted chat content without accepting tool authority."""
    if not isinstance(payload, dict):
        raise ScreenshotAnalysisContractError("OpenRouter vision response must be an object")
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ScreenshotAnalysisContractError("OpenRouter vision response has no assistant message") from exc
    if not isinstance(message, dict):
        raise ScreenshotAnalysisContractError("OpenRouter vision assistant message is invalid")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = [item.get("text") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
        if text_parts:
            return "".join(text_parts)
    raise ScreenshotAnalysisContractError("OpenRouter vision response contains no text content")


def _image_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    return "application/octet-stream"


def _bounded_reason(reason: str) -> str:
    return " ".join(str(reason or "unknown").strip().split())[:240] or "unknown"
