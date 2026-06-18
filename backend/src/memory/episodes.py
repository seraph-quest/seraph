from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.db.models import MemoryEpisodeType


def _flatten_text(value: str) -> str:
    return " ".join(value.strip().split())


def summarize_episode_text(content: str, *, max_chars: int = 160) -> str:
    normalized = _flatten_text(content)
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


@dataclass(frozen=True)
class EpisodeDraft:
    episode_type: MemoryEpisodeType
    summary: str
    content: str
    source_role: str | None = None
    source_tool_name: str | None = None
    salience: float = 0.5
    confidence: float = 0.6
    metadata: dict[str, Any] | None = None


def build_message_episode(
    *,
    role: str,
    content: str,
    tool_used: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> EpisodeDraft | None:
    normalized_role = role.strip().lower()
    normalized_content = _flatten_text(content)
    if not normalized_content:
        return None

    if normalized_role in {"user", "assistant"}:
        return EpisodeDraft(
            episode_type=MemoryEpisodeType.conversation,
            summary=summarize_episode_text(normalized_content, max_chars=140),
            content=normalized_content,
            source_role=normalized_role,
            salience=0.45 if normalized_role == "assistant" else 0.55,
            confidence=0.7,
            metadata={"source": "session_message"},
        )

    if normalized_role == "step" and tool_used:
        normalized_tool = tool_used.strip()
        if not normalized_tool:
            return None
        episode_type = (
            MemoryEpisodeType.workflow
            if normalized_tool == "workflow_runner" or normalized_tool.startswith("workflow_")
            else MemoryEpisodeType.tool
        )
        workflow_name = None
        if isinstance(metadata, dict):
            workflow_name = metadata.get("workflow_name")
        summary_prefix = (
            f"Workflow {workflow_name}"
            if isinstance(workflow_name, str) and workflow_name.strip()
            else normalized_tool
        )
        return EpisodeDraft(
            episode_type=episode_type,
            summary=summarize_episode_text(
                f"{summary_prefix}: {normalized_content}",
                max_chars=160,
            ),
            content=normalized_content,
            source_role=normalized_role,
            source_tool_name=normalized_tool,
            salience=0.7,
            confidence=0.8,
            metadata={
                "source": "session_step",
                **(metadata or {}),
            },
        )

    return None
