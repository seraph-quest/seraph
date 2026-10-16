"""Reviewed agent presets (#1020).

A preset declares one kind of turn: its context purpose, prompt sections,
model parameters, step bound, tool policy and output contract. It is data and
grants nothing: the tool policy is intersected with the turn's original
authority, and every runtime value it names is today's legacy value.

This table is Python's authority. The reviewed Cordis copy
(``runtime/cordis/src/plugins/agent_presets/index.ts``) must be identical: each
step projection carries the preset id and this digest, and the Cordis
sequencers refuse a step whose preset is unknown, of the wrong kind or differs.
"""
from __future__ import annotations

import hashlib
import json
from types import MappingProxyType
from typing import Any

from config.settings import settings

SCHEMA_VERSION = 1
CHAT_SECTIONS = ("base_instructions", "guardian_state", "observer_context", "user_identity", "relevant_memories",
                 "active_skills", "conversation_history")
# Exactly the tools ``create_onboarding_agent`` exposes; an explicit-URL
# ``browse_webpage`` is added per message, scoped to the pasted URLs.
ONBOARDING_TOOLS = ("view_soul", "update_soul", "create_goal", "get_goals")


class PresetError(ValueError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _preset(preset_id, kind, context_purpose, runtime_path, temperature, max_tokens, max_steps, tools,
            prompt_sections, output_contract):
    return MappingProxyType({
        "schema_version": SCHEMA_VERSION, "preset_id": preset_id, "kind": kind, "context_purpose": context_purpose,
        "runtime_path": runtime_path, "temperature": temperature, "max_tokens": max_tokens, "max_steps": max_steps,
        "tools": tools, "prompt_sections": list(prompt_sections), "output_contract": output_contract})


def _guardian(preset_id, runtime_path, temperature, max_tokens, context_purpose="guardian_report"):
    return _preset(preset_id, "unattended", context_purpose, runtime_path, temperature, max_tokens, 1, "none",
                   ("job_prompt",), "text")


PRESETS = MappingProxyType({item["preset_id"]: item for item in (
    _preset("chat.agent", "interactive", "chat", "chat_agent", "settings", "settings", "settings.agent_max_steps",
            "chat_surface", CHAT_SECTIONS, "final_answer"),
    _preset("chat.direct", "interactive", "direct", "chat_agent", "settings", "min(settings,512)", 1, "none",
            ("direct_system", "task_continuity"), "text"),
    _preset("chat.onboarding", "interactive", "onboarding", "onboarding_agent", "0.8", "settings",
            "settings.agent_max_steps", "onboarding_surface", ("onboarding_instructions", "explicit_url_scope"),
            "final_answer"),
    _guardian("guardian.strategist", "strategist_agent", "0.2", "512", "guardian_decision"),
    _guardian("guardian.daily_briefing", "daily_briefing", "0.6", "512"),
    _guardian("guardian.evening_review", "evening_review", "0.6", "512"),
    _guardian("guardian.activity_digest", "activity_digest", "0.6", "768"),
    _guardian("guardian.weekly_activity_review", "weekly_activity_review", "0.6", "1024"),
    _guardian("guardian.end_of_day_goal_report", "end_of_day_goal_report", "0.4", "900"),
    _guardian("guardian.screenshot_observation_digest", "screenshot_observation_digest", "0.2", "800"),
    _guardian("guardian.memory_consolidation", "session_consolidation", "0.3", "1024", "memory"),
)})
# The chat turn profiles of #1019 are these presets.
PROFILE_PRESETS = MappingProxyType({"agent": "chat.agent", "direct": "chat.direct", "onboarding": "chat.onboarding"})


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def preset(preset_id: str) -> MappingProxyType:
    try:
        return PRESETS[preset_id]
    except KeyError as exc:
        raise PresetError("agent_preset_unavailable") from exc


def preset_digest(preset_id: str) -> str:
    value = preset(preset_id)
    return hashlib.sha256(canonical(dict(value)).encode("utf-8")).hexdigest()


def preset_max_steps(preset_id: str) -> int:
    bound = preset(preset_id)["max_steps"]
    if bound == "settings.agent_max_steps":
        return int(settings.agent_max_steps)
    if type(bound) is not int or bound < 1:
        raise PresetError("agent_preset_invalid")
    return bound


def preset_temperature(preset_id: str) -> float:
    value = preset(preset_id)["temperature"]
    return float(settings.model_temperature) if value == "settings" else float(value)


def preset_max_tokens(preset_id: str) -> int:
    value = preset(preset_id)["max_tokens"]
    if value == "settings":
        return int(settings.model_max_tokens)
    if value == "min(settings,512)":
        return min(int(settings.model_max_tokens), 512)
    return int(value)


def require_unattended(preset_id: str) -> MappingProxyType:
    """An unattended turn has no operator to approve anything, so it may hold no tool."""
    value = preset(preset_id)
    if value["kind"] != "unattended" or value["tools"] != "none" or value["max_steps"] != 1:
        raise PresetError("unattended_preset_tools_denied")
    return value


def require_call(preset_id: str, *, runtime_path: str, temperature: float, max_tokens: int) -> None:
    """The legacy call site's parameters must be exactly the preset's: a preset never widens a call."""
    value = preset(preset_id)
    if (value["runtime_path"] != runtime_path or preset_temperature(preset_id) != float(temperature)
            or preset_max_tokens(preset_id) != int(max_tokens)):
        raise PresetError("agent_preset_changed")
