import asyncio
import logging
from dataclasses import dataclass
from time import perf_counter

from config.settings import settings
from src.agent.session import session_manager
from src.audit.runtime import log_background_task_event
from src.llm_runtime import completion_with_fallback
from src.memory.linking import resolve_memory_links
from src.memory.decay import apply_memory_decay_policies
from src.memory.pipeline.capture import capture_session_memory
from src.memory.pipeline.extract import extract_session_memories
from src.memory.pipeline.merge import persist_extracted_memories
from src.memory.providers import writeback_additive_memory_providers
from src.memory.snapshots import refresh_bounded_guardian_snapshot
from src.memory.soul import render_soul_text
from src.memory.vector_store import add_memory
from src.profile.service import sync_soul_file_to_profile, update_profile_soul_section

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConsolidationResult:
    outcome: str
    should_cache_fingerprint: bool


async def consolidate_session(
    session_id: str,
    *,
    trigger: str = "post_response",
    workflow_name: str | None = None,
    manager=None,
) -> ConsolidationResult:
    """Extract long-term memories from a conversation session.

    Runs as a background task after each conversation.
    """
    started_at = perf_counter()
    session_manager_ref = manager or session_manager
    try:
        capture = await capture_session_memory(
            session_id,
            manager=session_manager_ref,
            history_limit=30,
        )
        history = capture.history_text
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
            return ConsolidationResult(
                outcome="skipped",
                should_cache_fingerprint=True,
            )

        soul = render_soul_text(await sync_soul_file_to_profile())

        try:
            extraction = await extract_session_memories(
                session_id=session_id,
                history_text=history,
                soul_context=soul,
                completion_fn=completion_with_fallback,
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
            return ConsolidationResult(
                outcome="timed_out",
                should_cache_fingerprint=False,
            )
        persist_result = await persist_extracted_memories(
            extracted_memories=extraction.memories,
            session_id=session_id,
            source_messages=capture.source_messages,
            vector_writer=add_memory,
            link_resolver=resolve_memory_links,
        )
        provider_writeback_result = await writeback_additive_memory_providers(
            memories=extraction.memories,
            session_id=session_id,
            trigger=trigger,
            workflow_name=workflow_name,
        )
        decay_result = None
        decay_maintenance_failed = False
        decay_partial_write_count = 0
        snapshot_refresh_failed = False
        snapshot_partial_write_count = 0

        refreshed_soul = soul

        # Apply soul updates if any
        for section, content in extraction.soul_updates.items():
            if isinstance(content, str) and content.strip():
                refreshed_soul = await update_profile_soul_section(section, content)
                logger.info("Soul updated: section '%s'", section)

        try:
            decay_result = await apply_memory_decay_policies()
        except Exception:
            decay_maintenance_failed = True
            decay_partial_write_count = 1
            logger.exception("memory decay maintenance failed for session %s", session_id[:8])

        try:
            await refresh_bounded_guardian_snapshot(soul_context=refreshed_soul)
        except Exception:
            snapshot_refresh_failed = True
            snapshot_partial_write_count = 1
            logger.exception("bounded memory snapshot refresh failed for session %s", session_id[:8])

        total_partial_write_count = (
            persist_result.partial_write_count
            + provider_writeback_result.partial_write_count
            + snapshot_partial_write_count
            + decay_partial_write_count
        )
        total_write_failure_count = (
            persist_result.write_failure_count + provider_writeback_result.write_failure_count
        )

        logger.info(
            "Consolidated session %s: %d stored memories (%d created, %d merged), %d vector memories, %d source links, %d provider writebacks, %d soul updates, %d contradictions, %d superseded, %d decayed, %d archived, %d partial writes, %d failed writes, snapshot_failed=%s, decay_failed=%s",
            session_id[:8],
            persist_result.stored_count,
            persist_result.created_count,
            persist_result.merged_count,
            persist_result.vector_stored,
            persist_result.source_link_count,
            sum(int(item.get("stored_count") or 0) for item in provider_writeback_result.diagnostics),
            len(extraction.soul_updates),
            decay_result.contradiction_count if decay_result is not None else 0,
            decay_result.superseded_count if decay_result is not None else 0,
            decay_result.decayed_count if decay_result is not None else 0,
            decay_result.archived_count if decay_result is not None else 0,
            total_partial_write_count,
            total_write_failure_count,
            snapshot_refresh_failed,
            decay_maintenance_failed,
        )
        outcome = (
            "partially_succeeded"
            if total_partial_write_count or total_write_failure_count
            else "succeeded"
        )
        await log_background_task_event(
            task_name="session_consolidation",
            outcome=outcome,
            session_id=session_id,
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "history_length": len(history),
                "captured_source_message_count": len(capture.source_messages),
                "stored_memory_count": persist_result.stored_count,
                "created_memory_count": persist_result.created_count,
                "merged_memory_count": persist_result.merged_count,
                "vector_memory_count": persist_result.vector_stored,
                "source_link_count": persist_result.source_link_count,
                "provider_writeback_count": sum(
                    int(item.get("stored_count") or 0) for item in provider_writeback_result.diagnostics
                ),
                "provider_writeback_partial_count": provider_writeback_result.partial_write_count,
                "provider_writeback_failure_count": provider_writeback_result.write_failure_count,
                "provider_writeback_diagnostics": list(provider_writeback_result.diagnostics),
                "partial_write_count": total_partial_write_count,
                "write_failure_count": total_write_failure_count,
                "soul_update_count": len(extraction.soul_updates),
                "contradiction_count": decay_result.contradiction_count if decay_result is not None else 0,
                "superseded_memory_count": decay_result.superseded_count if decay_result is not None else 0,
                "decayed_memory_count": decay_result.decayed_count if decay_result is not None else 0,
                "archived_memory_count": decay_result.archived_count if decay_result is not None else 0,
                "snapshot_refresh_failed": snapshot_refresh_failed,
                "decay_maintenance_failed": decay_maintenance_failed,
                "trigger": trigger,
                "workflow_name": workflow_name,
            },
        )
        return ConsolidationResult(
            outcome=outcome,
            should_cache_fingerprint=outcome in {"skipped", "succeeded"},
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
        return ConsolidationResult(
            outcome="failed",
            should_cache_fingerprint=False,
        )
