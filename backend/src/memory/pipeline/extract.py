from __future__ import annotations
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from config.settings import settings
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.memory.types import ConsolidatedMemoryItem, parse_consolidated_memories


_CONSOLIDATION_PROMPT = """Analyze this conversation and extract key information to remember long-term.

Return a JSON object with these fields:
- "memories": list of memory objects. Each object should include:
  - "text": the memory statement
  - "kind": one of fact, preference, pattern, goal, reflection, project, collaborator, obligation, routine, timeline, commitment, communication_preference
  - "summary": short compressed form of the memory
  - "confidence": float from 0 to 1
  - "importance": float from 0 to 1
  - optional "subject" and "project" fields when obvious
  - optional "last_confirmed_at" ISO timestamp if the conversation makes timing explicit
- "facts": optional legacy list of factual statements learned about the user (keep empty if using typed memories)
- "patterns": optional legacy list of behavioral patterns observed
- "goals": optional legacy list of goals or intentions the user mentioned
- "reflections": optional legacy list of insights or decisions made
- "soul_updates": dict of soul sections to update (only if significant new identity/goal info). Keys are section names like "Identity", "Values", "Goals". Values are the new content. Return empty dict if no updates needed.

Be selective. Prefer durable facts, commitments, preferences, collaborators, obligations, routines, project state, and timeline milestones.
If the conversation is trivial small talk with nothing worth remembering, return all empty lists and empty dict.

Conversation:
{conversation}

Current soul file:
{soul}

Return ONLY valid JSON, no markdown fences."""


@dataclass(frozen=True)
class SessionMemoryExtraction:
    memories: tuple[ConsolidatedMemoryItem, ...]
    soul_updates: dict[str, str]
    raw_payload: dict[str, object]


async def extract_session_memories(
    *,
    session_id: str,
    history_text: str,
    soul_context: str,
    completion_fn,
) -> SessionMemoryExtraction:
    prompt = _CONSOLIDATION_PROMPT.format(conversation=history_text, soul=soul_context)
    runtime_tokens = None
    try:
        runtime_tokens = set_runtime_context(session_id, "high_risk")
        response = await completion_fn(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1024,
            timeout=settings.consolidation_llm_timeout,
            runtime_path="session_consolidation",
        )
    finally:
        if runtime_tokens is not None:
            reset_runtime_context(runtime_tokens)

    text = response.choices[0].message.content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3]

    data = json.loads(text)
    extracted_memories = parse_consolidated_memories(
        data,
        fallback_confirmed_at=datetime.now(timezone.utc),
    )
    soul_updates = data.get("soul_updates", {})
    normalized_soul_updates = {
        str(section).strip(): content
        for section, content in soul_updates.items()
        if isinstance(section, str) and str(section).strip() and isinstance(content, str)
    } if isinstance(soul_updates, dict) else {}
    return SessionMemoryExtraction(
        memories=tuple(extracted_memories),
        soul_updates=normalized_soul_updates,
        raw_payload=data,
    )
