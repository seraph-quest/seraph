"""Migration guard for removed command-backed external agent runtimes.

Seraph model providers are inference-only.  Legacy Codex CLI selectors remain
recognizable solely so configuration fails explicitly instead of falling
through to another model route.
"""

from __future__ import annotations

from typing import NoReturn


EXTERNAL_AGENT_RUNTIME_REMOVED = "external_agent_runtime_removed"
EXTERNAL_AGENT_MIGRATION_GUIDANCE = (
    "Command-backed external agent runtimes were removed. Select a local, "
    "OpenRouter, or generic OpenAI-compatible inference profile instead."
)
_LEGACY_EXTERNAL_AGENT_MODEL_IDS = {"codex", "codex-local", "local-codex"}


class ExternalAgentRuntimeRemovedError(RuntimeError):
    """Raised when configuration selects a removed external-agent runtime."""

    code = EXTERNAL_AGENT_RUNTIME_REMOVED

    def __init__(self, selection: str | None = None):
        self.selection = str(selection or "").strip()
        super().__init__(EXTERNAL_AGENT_MIGRATION_GUIDANCE)

    def payload(self) -> dict[str, str]:
        payload = {
            "code": self.code,
            "status": "removed",
            "migration_guidance": EXTERNAL_AGENT_MIGRATION_GUIDANCE,
        }
        if self.selection:
            payload["selection"] = self.selection
        return payload


def is_legacy_external_agent_model(model: str | None) -> bool:
    """Recognize legacy selectors without interpreting normal OpenAI models."""
    normalized = str(model or "").strip().lower()
    return normalized in _LEGACY_EXTERNAL_AGENT_MODEL_IDS or normalized.startswith("codex-local/")


def is_local_codex_model(model: str | None = None) -> bool:
    """Compatibility name for legacy-selection detection only."""
    return is_legacy_external_agent_model(model)


def reject_legacy_external_agent_model(model: str | None) -> None:
    if is_legacy_external_agent_model(model):
        raise ExternalAgentRuntimeRemovedError(model)


def removed_external_agent_payload(selection: str | None = None) -> dict[str, str]:
    return ExternalAgentRuntimeRemovedError(selection).payload()


async def run_local_codex(*args: object, **kwargs: object) -> NoReturn:
    """Fail closed for stale imports; this function never launches a process."""
    selection = kwargs.get("model")
    raise ExternalAgentRuntimeRemovedError(str(selection) if selection is not None else "codex-local")
