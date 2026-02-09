import asyncio
import logging

from config.settings import settings
from src.agent.session import session_manager
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
    try:
        history = await session_manager.get_history_text(session_id, limit=30)
        if not history or len(history) < 50:
            return

        soul = read_soul()
        prompt = _CONSOLIDATION_PROMPT.format(conversation=history, soul=soul)

        # Use LiteLLM directly for the consolidation call (lighter than full agent)
        import litellm

        response = await asyncio.to_thread(
            litellm.completion,
            model=settings.default_model,
            messages=[{"role": "user", "content": prompt}],
            api_key=settings.openrouter_api_key,
            api_base="https://openrouter.ai/api/v1",
            temperature=0.3,
            max_tokens=1024,
        )

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
                    await asyncio.to_thread(
                        add_memory,
                        text=item,
                        category=singular,
                        source_session_id=session_id,
                    )
                    stored += 1

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

    except Exception:
        logger.exception("Memory consolidation failed for session %s", session_id[:8])
