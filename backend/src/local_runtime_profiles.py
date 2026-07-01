"""Local Gemma runtime profile contract shared by Seraph callers."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from config.settings import settings


@dataclass(frozen=True)
class LocalRuntimeProfile:
    id: str
    runtime_path: str
    priority: str
    reasoning: str
    temperature: float
    max_tokens: int
    timeout_seconds: int
    summary: str
    options: dict[str, Any]


_LOCAL_RUNTIME_PROFILES: dict[str, LocalRuntimeProfile] = {
    "screenshot_fast": LocalRuntimeProfile(
        id="screenshot_fast",
        runtime_path="screenshot_image_analysis",
        priority="background",
        reasoning="off",
        temperature=0.0,
        max_tokens=1400,
        timeout_seconds=120,
        summary="Low-priority bounded screenshot analysis. Must not starve chat.",
        options={
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning": False,
            "reasoning_format": "none",
            "response_shape": "json_object",
        },
    ),
    "report_thinking": LocalRuntimeProfile(
        id="report_thinking",
        runtime_path="end_of_day_goal_report",
        priority="high",
        reasoning="on",
        temperature=0.2,
        max_tokens=4096,
        timeout_seconds=240,
        summary="Higher-priority synthesis profile for reports and screenshot digests.",
        options={
            "chat_template_kwargs": {"enable_thinking": True},
            "reasoning": True,
        },
    ),
    "chat_thinking": LocalRuntimeProfile(
        id="chat_thinking",
        runtime_path="chat_agent",
        priority="interactive",
        reasoning="on",
        temperature=0.7,
        max_tokens=4096,
        timeout_seconds=120,
        summary="Interactive chat profile. Must take priority over screenshot backlog.",
        options={
            "chat_template_kwargs": {"enable_thinking": True},
            "reasoning": True,
        },
    ),
}


def local_runtime_profiles() -> dict[str, LocalRuntimeProfile]:
    """Return Seraph's built-in local Gemma profile contract."""
    return dict(_LOCAL_RUNTIME_PROFILES)


def local_runtime_profile(profile_id: str) -> LocalRuntimeProfile:
    """Return one local runtime profile or raise a bounded configuration error."""
    normalized = str(profile_id or "").strip().lower().replace("-", "_")
    try:
        profile = _LOCAL_RUNTIME_PROFILES[normalized]
    except KeyError as exc:
        known = ", ".join(sorted(_LOCAL_RUNTIME_PROFILES))
        raise ValueError(f"unknown local runtime profile '{profile_id}'; expected one of: {known}") from exc
    return profile


def local_runtime_profile_statuses() -> list[dict[str, Any]]:
    """Return operator-safe profile metadata for settings/status surfaces."""
    return [
        {
            "id": profile.id,
            "runtime_path": profile.runtime_path,
            "priority": profile.priority,
            "reasoning": profile.reasoning,
            "temperature": _profile_temperature(profile),
            "max_tokens": _profile_max_tokens(profile),
            "timeout_seconds": _profile_timeout_seconds(profile),
            "summary": profile.summary,
            "options": profile.options,
        }
        for profile in _LOCAL_RUNTIME_PROFILES.values()
    ]


def local_runtime_profile_form_fields(profile_id: str) -> dict[str, str]:
    """Return multipart form fields for the VLM gateway profile contract."""
    profile = local_runtime_profile(profile_id)
    return {
        "runtime_profile": profile.id,
        "runtime_path": profile.runtime_path,
        "priority": profile.priority,
        "reasoning": profile.reasoning,
        "profile_options": json.dumps(profile.options, sort_keys=True, separators=(",", ":")),
    }


def local_runtime_profile_headers(profile_id: str) -> dict[str, str]:
    """Return headers for gateway implementations that prefer header metadata."""
    profile = local_runtime_profile(profile_id)
    return {
        "X-Seraph-Runtime-Profile": profile.id,
        "X-Seraph-Runtime-Path": profile.runtime_path,
        "X-Seraph-Priority": profile.priority,
        "X-Seraph-Reasoning": profile.reasoning,
    }


def local_runtime_chat_payload(profile_id: str, *, model: str | None = None) -> dict[str, Any]:
    """Return OpenAI-compatible request defaults for a profile verification request."""
    profile = local_runtime_profile(profile_id)
    payload: dict[str, Any] = {
        "model": model or settings.local_vlm_model or settings.local_model or settings.default_model,
        "temperature": _profile_temperature(profile),
        "max_tokens": _profile_max_tokens(profile),
        "metadata": {
            "runtime_profile": profile.id,
            "runtime_path": profile.runtime_path,
            "priority": profile.priority,
        },
    }
    payload.update(profile.options)
    return payload


def _profile_temperature(profile: LocalRuntimeProfile) -> float:
    if profile.id == "chat_thinking":
        return settings.model_temperature
    return profile.temperature


def _profile_max_tokens(profile: LocalRuntimeProfile) -> int:
    if profile.id == "chat_thinking":
        return settings.model_max_tokens
    return profile.max_tokens


def _profile_timeout_seconds(profile: LocalRuntimeProfile) -> int:
    if profile.id == "screenshot_fast":
        return max(settings.local_vlm_timeout_seconds, 1)
    if profile.id == "chat_thinking":
        return max(settings.agent_chat_timeout, 1)
    return profile.timeout_seconds
