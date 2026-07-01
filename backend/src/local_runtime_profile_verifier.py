"""Empirical verifier for Seraph local Gemma runtime profiles."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from config.settings import settings
from src.local_runtime_profiles import local_runtime_chat_payload, local_runtime_profile


PROFILE_VERIFIER_VERSION = "seraph.local_runtime_profiles.proof.v1"
_PROFILES_TO_VERIFY = ("screenshot_fast", "report_thinking", "chat_thinking")


async def verify_local_runtime_profiles(
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    output_dir: Path | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    """Verify the live local gateway profile behavior and write a safe receipt."""
    resolved_base_url = (base_url or settings.local_llm_api_base or settings.local_vlm_base_url).strip()
    if not resolved_base_url:
        raise ValueError("local runtime verifier requires LOCAL_LLM_API_BASE or LOCAL_VLM_BASE_URL")
    resolved_model = (model or settings.local_model or settings.local_vlm_model or settings.default_model).strip()
    if not resolved_model:
        raise ValueError("local runtime verifier requires a model id")
    resolved_api_key = api_key if api_key is not None else settings.local_llm_api_key or settings.local_vlm_api_key

    started_at = datetime.now(timezone.utc)
    receipt: dict[str, Any] = {
        "schema_version": PROFILE_VERIFIER_VERSION,
        "receipt_id": uuid4().hex,
        "started_at": started_at.isoformat(),
        "base_url": _safe_base_url(resolved_base_url),
        "model": resolved_model,
        "profile_contract_sha256": local_runtime_profile_contract_hash(),
        "profiles": [],
        "conclusion": {
            "profile_requests_completed": False,
            "per_request_reasoning_control": "unverified",
            "safe_for_single_backend_profile_routing": False,
            "notes": [],
        },
    }

    headers = {"Content-Type": "application/json"}
    if resolved_api_key:
        headers["Authorization"] = "Bearer [redacted]"
    transport_headers = {"Content-Type": "application/json"}
    if resolved_api_key:
        transport_headers["Authorization"] = f"Bearer {resolved_api_key}"

    async with httpx.AsyncClient(timeout=max(timeout_seconds, 1)) as client:
        receipt["backend"] = await _probe_backend(client, resolved_base_url, transport_headers)
        for profile_id in _PROFILES_TO_VERIFY:
            profile_result = await _verify_profile(
                client,
                base_url=resolved_base_url,
                model=resolved_model,
                profile_id=profile_id,
                headers=headers,
                transport_headers=transport_headers,
            )
            receipt["profiles"].append(profile_result)

    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    receipt["conclusion"] = _build_conclusion(receipt["profiles"])
    receipt["sha256"] = _receipt_hash(receipt)

    path = _write_receipt(receipt, output_dir=output_dir)
    receipt["receipt_path"] = str(path)
    return receipt


def verify_local_runtime_profiles_sync(**kwargs: Any) -> dict[str, Any]:
    """Synchronous wrapper for scripts and local operator commands."""
    return asyncio.run(verify_local_runtime_profiles(**kwargs))


def local_runtime_profile_receipt_dir() -> Path:
    """Return the local runtime profile proof receipt directory."""
    return Path(settings.workspace_dir).expanduser().resolve() / "local-runtime-profile-receipts"


def latest_local_runtime_profile_proof(
    *,
    receipt_dir: Path | None = None,
    expected_base_url: str | None = None,
    expected_model: str | None = None,
) -> dict[str, Any]:
    """Return a sanitized status summary for the latest local profile proof receipt."""
    root = receipt_dir or local_runtime_profile_receipt_dir()
    receipts = sorted(root.glob("*.json")) if root.exists() else []
    if not receipts:
        return {
            "status": "missing",
            "safe_for_single_backend_profile_routing": False,
            "notes": ["no local runtime profile proof receipt found"],
        }

    latest = receipts[-1]
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "status": "invalid",
            "receipt_path": str(latest),
            "safe_for_single_backend_profile_routing": False,
            "notes": ["latest local runtime profile proof receipt is unreadable"],
        }

    schema_ok = payload.get("schema_version") == PROFILE_VERIFIER_VERSION
    stored_hash = str(payload.get("sha256") or "")
    hash_ok = bool(stored_hash) and stored_hash == _receipt_hash(payload)
    conclusion = payload.get("conclusion") if isinstance(payload.get("conclusion"), dict) else {}
    receipt_base_url = str(payload.get("base_url") or "")
    receipt_model = str(payload.get("model") or "")
    receipt_contract_hash = str(payload.get("profile_contract_sha256") or "")
    expected_safe_base_url = _safe_base_url(expected_base_url) if expected_base_url else None
    expected_contract_hash = local_runtime_profile_contract_hash()
    safe = bool(
        schema_ok
        and hash_ok
        and receipt_contract_hash == expected_contract_hash
        and conclusion.get("profile_requests_completed") is True
        and conclusion.get("safe_for_single_backend_profile_routing") is True
        and conclusion.get("per_request_reasoning_control") == "verified"
    )
    notes = list(conclusion.get("notes") or []) if isinstance(conclusion.get("notes"), list) else []
    if not schema_ok:
        notes.append("latest local runtime profile proof receipt has an unsupported schema")
    if not hash_ok:
        notes.append("latest local runtime profile proof receipt hash did not verify")
    if receipt_contract_hash != expected_contract_hash:
        safe = False
        notes.append("latest local runtime profile proof receipt does not match the current profile contract")
    if expected_safe_base_url and receipt_base_url != expected_safe_base_url:
        safe = False
        notes.append("latest local runtime profile proof receipt does not match the configured local base URL")
    if expected_model and receipt_model != expected_model:
        safe = False
        notes.append("latest local runtime profile proof receipt does not match the configured local model")
    return {
        "status": "safe" if safe else "unsafe",
        "receipt_path": str(latest),
        "sha256": stored_hash or None,
        "finished_at": payload.get("finished_at"),
        "base_url": receipt_base_url or None,
        "model": receipt_model or None,
        "profile_contract_sha256": receipt_contract_hash or None,
        "per_request_reasoning_control": conclusion.get("per_request_reasoning_control"),
        "profile_requests_completed": bool(conclusion.get("profile_requests_completed")),
        "safe_for_single_backend_profile_routing": safe,
        "notes": notes,
    }


def latest_local_runtime_profile_proof_is_safe(*, receipt_dir: Path | None = None) -> bool:
    """Return whether the latest local profile proof receipt allows shared routing."""
    return bool(
        latest_local_runtime_profile_proof(receipt_dir=receipt_dir).get(
            "safe_for_single_backend_profile_routing"
        )
    )


def local_runtime_profile_contract_hash() -> str:
    """Return a stable hash for the current Seraph local runtime profile contract."""
    contract = [
        _contract_payload_summary(profile_id)
        for profile_id in _PROFILES_TO_VERIFY
    ]
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


async def _verify_profile(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    model: str,
    profile_id: str,
    headers: dict[str, str],
    transport_headers: dict[str, str],
) -> dict[str, Any]:
    profile = local_runtime_profile(profile_id)
    payload = local_runtime_chat_payload(profile_id, model=model)
    payload["messages"] = [
        {
            "role": "system",
            "content": (
                "You are verifying a local Seraph runtime profile. "
                "Do not reveal private chain-of-thought. Return the requested final text only."
            ),
        },
        {
            "role": "user",
            "content": _verification_prompt(profile_id),
        },
    ]
    request_summary = {
        "url": _chat_completions_url(base_url),
        "headers": headers,
        "payload": _safe_payload_summary(payload),
    }
    started = datetime.now(timezone.utc)
    try:
        response = await client.post(
            _chat_completions_url(base_url),
            headers=transport_headers,
            json=payload,
        )
        elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        response_text = response.text
        response.raise_for_status()
        response_payload = response.json()
        markers = _reasoning_markers(response_payload)
        content = _message_content(response_payload)
        return {
            "profile": profile.id,
            "runtime_path": profile.runtime_path,
            "priority": profile.priority,
            "requested_reasoning": profile.reasoning,
            "status": "ok",
            "elapsed_ms": elapsed_ms,
            "request": request_summary,
            "response": {
                "status_code": response.status_code,
                "content_preview": content[:500],
                "response_keys": sorted(response_payload.keys()) if isinstance(response_payload, dict) else [],
                "usage": response_payload.get("usage") if isinstance(response_payload, dict) else None,
                "reasoning_markers": markers,
                "raw_sha256": hashlib.sha256(response_text.encode("utf-8")).hexdigest(),
            },
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "profile": profile.id,
            "runtime_path": profile.runtime_path,
            "priority": profile.priority,
            "requested_reasoning": profile.reasoning,
            "status": "failed",
            "request": request_summary,
            "error": _safe_error(exc),
        }


async def _probe_backend(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
) -> dict[str, Any]:
    probes: list[dict[str, Any]] = []
    for url in (_health_url(base_url), _models_url(base_url)):
        try:
            response = await client.get(url, headers=headers)
            probes.append({
                "url": _safe_url(url),
                "status_code": response.status_code,
                "ok": response.is_success,
                "body_sha256": hashlib.sha256(response.text.encode("utf-8")).hexdigest(),
                "body_preview": response.text[:500] if response.is_success else "",
            })
        except httpx.HTTPError as exc:
            probes.append({"url": _safe_url(url), "ok": False, "error": _safe_error(exc)})
    return {"probes": probes}


def _build_conclusion(profile_results: list[dict[str, Any]]) -> dict[str, Any]:
    completed = all(item.get("status") == "ok" for item in profile_results)
    notes: list[str] = []
    fast = next((item for item in profile_results if item.get("profile") == "screenshot_fast"), {})
    thinking = [
        item
        for item in profile_results
        if item.get("profile") in {"report_thinking", "chat_thinking"}
    ]
    fast_markers = fast.get("response", {}).get("reasoning_markers", {})
    thinking_markers = [
        item.get("response", {}).get("reasoning_markers", {})
        for item in thinking
    ]
    fast_visible_reasoning = bool(fast_markers.get("visible_reasoning"))
    fast_structured_reasoning = bool(fast_markers.get("structured_reasoning_field"))
    thinking_visible_reasoning = any(bool(item.get("visible_reasoning")) for item in thinking_markers)
    structured_reasoning_field = any(
        bool(item.get("structured_reasoning_field"))
        for item in [fast_markers, *thinking_markers]
    )

    if not completed:
        notes.append("one or more profile requests failed")
    if fast_visible_reasoning:
        notes.append("screenshot_fast emitted visible reasoning markers")
    if fast_structured_reasoning:
        notes.append("screenshot_fast exposed structured reasoning metadata")
    if not structured_reasoning_field:
        notes.append("backend did not expose structured reasoning metadata; per-request reasoning control is ambiguous")
    if not thinking_visible_reasoning and not structured_reasoning_field:
        notes.append("thinking profiles responded, but no observable reasoning signal was returned")

    if completed and not fast_visible_reasoning and not fast_structured_reasoning and structured_reasoning_field:
        control = "verified"
        safe = True
    elif completed and not fast_visible_reasoning and not fast_structured_reasoning:
        control = "ambiguous_no_visible_reasoning"
        safe = False
    else:
        control = "failed"
        safe = False

    return {
        "profile_requests_completed": completed,
        "per_request_reasoning_control": control,
        "safe_for_single_backend_profile_routing": safe,
        "notes": notes,
    }


def _verification_prompt(profile_id: str) -> str:
    if profile_id == "screenshot_fast":
        return (
            'Return exactly this JSON object and nothing else: {"profile":"screenshot_fast","ok":true}'
        )
    if profile_id == "report_thinking":
        return "Use the report_thinking profile. Final answer must be exactly: REPORT_PROFILE_OK"
    return "Use the chat_thinking profile. Final answer must be exactly: CHAT_PROFILE_OK"


def _message_content(payload: Any) -> str:
    try:
        content = payload["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, sort_keys=True)[:1000]


def _reasoning_markers(payload: Any) -> dict[str, Any]:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False) if isinstance(payload, dict) else str(payload)
    lower = serialized.lower()
    structured = any(
        marker in lower
        for marker in (
            "reasoning_content",
            '"reasoning"',
            "reasoning_tokens",
            "reasoning_details",
        )
    )
    visible = any(
        marker in lower
        for marker in (
            "<think>",
            "</think>",
            "<|channel>thought",
            "<channel|>",
            "chain-of-thought",
            "reasoning:",
        )
    )
    return {
        "structured_reasoning_field": structured,
        "visible_reasoning": visible,
    }


def _safe_payload_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": payload.get("model"),
        "temperature": payload.get("temperature"),
        "max_tokens": payload.get("max_tokens"),
        "reasoning": payload.get("reasoning"),
        "reasoning_format": payload.get("reasoning_format"),
        "chat_template_kwargs": payload.get("chat_template_kwargs"),
        "metadata": payload.get("metadata"),
        "message_count": len(payload.get("messages") or []),
    }


def _contract_payload_summary(profile_id: str) -> dict[str, Any]:
    profile = local_runtime_profile(profile_id)
    payload = _safe_payload_summary(local_runtime_chat_payload(profile_id, model="[model]"))
    payload.pop("model", None)
    return {
        "profile": profile.id,
        "runtime_path": profile.runtime_path,
        "priority": profile.priority,
        "reasoning": profile.reasoning,
        "payload": payload,
    }


def _write_receipt(receipt: dict[str, Any], *, output_dir: Path | None) -> Path:
    root = output_dir or Path(settings.workspace_dir).expanduser().resolve() / "local-runtime-profile-receipts"
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / f"{timestamp}-{receipt['receipt_id']}.json"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _receipt_hash(receipt: dict[str, Any]) -> str:
    payload = {key: value for key, value in receipt.items() if key not in {"sha256", "receipt_path"}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _chat_completions_url(base_url: str) -> str:
    clean = base_url.rstrip("/")
    if clean.endswith("/chat/completions"):
        return clean
    if clean.endswith("/v1"):
        return f"{clean}/chat/completions"
    return f"{clean}/v1/chat/completions"


def _models_url(base_url: str) -> str:
    clean = base_url.rstrip("/")
    if clean.endswith("/chat/completions"):
        return clean.rsplit("/chat/completions", 1)[0] + "/models"
    if clean.endswith("/v1"):
        return f"{clean}/models"
    return f"{clean}/v1/models"


def _health_url(base_url: str) -> str:
    clean = base_url.rstrip("/")
    if clean.endswith("/v1"):
        return clean[:-3] + "/health"
    if clean.endswith("/chat/completions"):
        return clean.rsplit("/v1/chat/completions", 1)[0] + "/health"
    return f"{clean}/health"


def _safe_base_url(base_url: str) -> str:
    return _safe_url(base_url.rstrip("/"))


def _safe_url(url: str) -> str:
    return _replace_non_empty(_replace_non_empty(url, settings.local_llm_api_key), settings.local_vlm_api_key)


def _safe_error(exc: Exception) -> str:
    return _replace_non_empty(
        _replace_non_empty(str(exc), settings.local_llm_api_key),
        settings.local_vlm_api_key,
    )[:1000]


def _replace_non_empty(value: str, secret: str) -> str:
    return value.replace(secret, "[redacted]") if secret else value
