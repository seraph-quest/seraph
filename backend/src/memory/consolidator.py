import asyncio
import logging
from datetime import datetime, timezone
from time import perf_counter

from config.settings import settings
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.agent.session import session_manager
from src.audit.runtime import log_background_task_event
from src.llm_runtime import completion_with_fallback
from src.memory.linking import resolve_memory_links
from src.memory.repository import memory_repository
from src.memory.snapshots import refresh_bounded_guardian_snapshot
from src.memory.soul import read_soul, update_soul_section
from src.memory.types import parse_consolidated_memories
from src.memory.vector_store import add_memory

logger = logging.getLogger(__name__)

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

Be selective — only extract things worth remembering across future conversations.
If the conversation is trivial small talk with nothing worth remembering, return all empty lists and empty dict.

Conversation:
{conversation}

Current soul file:
{soul}

Return ONLY valid JSON, no markdown fences."""


def _summary_for_memory(text: str, *, max_chars: int = 140) -> str:
    normalized = " ".join(text.strip().split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


async def consolidate_session(
    session_id: str,
    *,
    trigger: str = "post_response",
    workflow_name: str | None = None,
    manager=None,
) -> None:
    """Extract long-term memories from a conversation session.

    Runs as a background task after each conversation.
    """
    started_at = perf_counter()
    session_manager_ref = manager or session_manager
    try:
        history = await session_manager_ref.get_history_text(
            session_id,
            limit=30,
            allow_memory_flush=False,
        )
        if not history or len(history) < 50:
            await log_background_task_event(
                task_name="session_consolidation",
                outcome="skipped",
                session_id=session_id,
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "reason": "insufficient_history",
                    "history_length": len(history),
                    "trigger": trigger,
                    "workflow_name": workflow_name,
                },
            )
            return

        soul = read_soul()
        prompt = _CONSOLIDATION_PROMPT.format(conversation=history, soul=soul)
        runtime_tokens = None

        try:
            runtime_tokens = set_runtime_context(session_id, "high_risk")
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
                    "trigger": trigger,
                    "workflow_name": workflow_name,
                },
            )
            return
        finally:
            if runtime_tokens is not None:
                reset_runtime_context(runtime_tokens)

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
        extracted_memories = parse_consolidated_memories(
            data,
            fallback_confirmed_at=datetime.now(timezone.utc),
        )
        stored = 0
        vector_stored = 0
        partial_write_count = 0
        write_failure_count = 0
        snapshot_refresh_failed = False
        for item in extracted_memories:
            embedding_id = ""
            vector_succeeded = False
            structured_succeeded = False
            try:
                embedding_id = await asyncio.wait_for(
                    asyncio.to_thread(
                        add_memory,
                        text=item.text,
                        category=item.category.value,
                        source_session_id=session_id,
                    ),
                    timeout=10,
                )
                vector_succeeded = isinstance(embedding_id, str) and bool(embedding_id.strip())
                if vector_succeeded:
                    vector_stored += 1
            except asyncio.TimeoutError:
                logger.warning("add_memory timed out for session %s", session_id[:8])
            except Exception:
                logger.exception("add_memory failed for session %s", session_id[:8])
            metadata = dict(item.metadata or {})
            metadata.update(
                {
                    "writer": "session_consolidation",
                    "source": "llm_extract",
                }
            )
            if item.subject_name:
                metadata["subject_name"] = item.subject_name
            if item.project_name:
                metadata["project_name"] = item.project_name
            try:
                link_resolution = await resolve_memory_links(item)
                await memory_repository.create_memory(
                    content=item.text,
                    category=item.category,
                    kind=item.kind,
                    source_session_id=session_id,
                    embedding_id=embedding_id or None,
                    summary=item.summary or _summary_for_memory(item.text),
                    confidence=item.confidence,
                    importance=item.importance,
                    subject_entity_id=link_resolution.subject_entity_id,
                    project_entity_id=link_resolution.project_entity_id,
                    metadata=metadata,
                    last_confirmed_at=item.last_confirmed_at,
                )
                structured_succeeded = True
                stored += 1
            except Exception:
                logger.exception(
                    "structured memory write failed for session %s", session_id[:8]
                )
            if vector_succeeded != structured_succeeded:
                partial_write_count += 1
            elif not vector_succeeded and not structured_succeeded:
                write_failure_count += 1

        # Apply soul updates if any
        soul_updates = data.get("soul_updates", {})
        for section, content in soul_updates.items():
            if isinstance(content, str) and content.strip():
                update_soul_section(section, content)
                logger.info("Soul updated: section '%s'", section)

        try:
            await refresh_bounded_guardian_snapshot(soul_context=read_soul())
        except Exception:
            snapshot_refresh_failed = True
            partial_write_count += 1
            logger.exception("bounded memory snapshot refresh failed for session %s", session_id[:8])

        logger.info(
            "Consolidated session %s: %d structured memories, %d vector memories, %d soul updates, %d partial writes, %d failed writes, snapshot_failed=%s",
            session_id[:8],
            stored,
            vector_stored,
            len(soul_updates),
            partial_write_count,
            write_failure_count,
            snapshot_refresh_failed,
        )
        outcome = (
            "partially_succeeded"
            if partial_write_count or write_failure_count
            else "succeeded"
        )
        await log_background_task_event(
            task_name="session_consolidation",
            outcome=outcome,
            session_id=session_id,
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "history_length": len(history),
                "stored_memory_count": stored,
                "vector_memory_count": vector_stored,
                "partial_write_count": partial_write_count,
                "write_failure_count": write_failure_count,
                "soul_update_count": len(soul_updates),
                "snapshot_refresh_failed": snapshot_refresh_failed,
                "trigger": trigger,
                "workflow_name": workflow_name,
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
                "trigger": trigger,
                "workflow_name": workflow_name,
            },
        )
        logger.exception("Memory consolidation failed for session %s", session_id[:8])
