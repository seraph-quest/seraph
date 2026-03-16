import asyncio
import logging
from time import perf_counter

from config.settings import settings
from src.agent.session import session_manager
from src.audit.runtime import log_background_task_event
from src.llm_runtime import completion_with_fallback
from src.memory.soul import read_soul, update_soul_section
from src.memory.vector_store import add_memory

logger = logging.getLogger(__name__)

_CONSOLIDATION_PROMPT = """Analyze this conversation and extract key information to remember long-term.

Return a JSON object with these fields:
- "facts": list of factual statements learned about the user (name, role, preferences, etc.)
- "patterns": list of behavioral patterns observed
- "goals": list of goals or intentions the user mentioned
- "reflections": list of insights or decisions made
- "soul_updates": dict of soul sections to update (only if significant new identity/goal info). Keys are section names like "Identity", "Values", "Goals". Values are the new content. Return empty dict if no updates needed.

Be selective — only extract things worth remembering across future conversations.
If the conversation is trivial small talk with nothing worth remembering, return all empty lists and empty dict.

Conversation:
{conversation}

Current soul file:
{soul}

Return ONLY valid JSON, no markdown fences."""


async def consolidate_session(session_id: str) -> None:
    """Extract long-term memories from a conversation session.

    Runs as a background task after each conversation.
    """
    started_at = perf_counter()
    try:
        history = await session_manager.get_history_text(session_id, limit=30)
        if not history or len(history) < 50:
            await log_background_task_event(
                task_name="session_consolidation",
                outcome="skipped",
                session_id=session_id,
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": "insufficient_history",
                    "history_length": len(history),
                },
            )
            return

        soul = read_soul()
        prompt = _CONSOLIDATION_PROMPT.format(conversation=history, soul=soul)

        try:
            response = await completion_with_fallback(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1024,
                timeout=settings.consolidation_llm_timeout,
                runtime_path="session_consolidation",
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Consolidation LLM timed out after %ds for session %s",
                settings.consolidation_llm_timeout,
                session_id[:8],
            )
            await log_background_task_event(
                task_name="session_consolidation",
                outcome="timed_out",
                session_id=session_id,
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "timeout_seconds": settings.consolidation_llm_timeout,
                    "history_length": len(history),
                },
            )
            return

        text = response.choices[0].message.content.strip()

        # Parse JSON response
        import json

        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text[:-3]

        data = json.loads(text)

        # Store memories by category
        stored = 0
        for category in ["facts", "patterns", "goals", "reflections"]:
            items = data.get(category, [])
            singular = category.rstrip("s") if category != "reflections" else "reflection"
            for item in items:
                if isinstance(item, str) and len(item) > 10:
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(
                                add_memory,
                                text=item,
                                category=singular,
                                source_session_id=session_id,
                            ),
                            timeout=10,
                        )
                        stored += 1
                    except asyncio.TimeoutError:
                        logger.warning("add_memory timed out for session %s", session_id[:8])

        # Apply soul updates if any
        soul_updates = data.get("soul_updates", {})
        for section, content in soul_updates.items():
            if isinstance(content, str) and content.strip():
                update_soul_section(section, content)
                logger.info("Soul updated: section '%s'", section)

        logger.info(
            "Consolidated session %s: %d memories stored, %d soul updates",
            session_id[:8],
            stored,
            len(soul_updates),
        )
        await log_background_task_event(
            task_name="session_consolidation",
            outcome="succeeded",
            session_id=session_id,
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "history_length": len(history),
                "stored_memory_count": stored,
                "soul_update_count": len(soul_updates),
            },
        )

    except Exception as exc:
        await log_background_task_event(
            task_name="session_consolidation",
            outcome="failed",
            session_id=session_id,
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": str(exc),
            },
        )
        logger.exception("Memory consolidation failed for session %s", session_id[:8])
