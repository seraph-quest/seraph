import os
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

import lancedb
import pyarrow as pa

from config.settings import settings
from src.audit.runtime import log_integration_event_sync
from src.memory.embedder import embed

logger = logging.getLogger(__name__)

_LANCE_DIR = os.path.join(settings.workspace_dir, "lance")
_TABLE_NAME = "memories"

# Schema: 384 dimensions for all-MiniLM-L6-v2
_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("text", pa.string()),
    pa.field("category", pa.string()),
    pa.field("source_session_id", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 384)),
    pa.field("created_at", pa.string()),
])

_db: Optional[lancedb.DBConnection] = None
_db_lock = threading.Lock()


def _log_vector_store_event(outcome: str, details: dict | None = None) -> None:
    log_integration_event_sync(
        integration_type="vector_store",
        name=_TABLE_NAME,
        outcome=outcome,
        details=details,
    )


def _safe_query_length(query: object) -> int | None:
    try:
        return len(query)  # type: ignore[arg-type]
    except Exception:
        return None


def _get_db() -> lancedb.DBConnection:
    """Lazy-open the LanceDB connection (thread-safe)."""
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                os.makedirs(_LANCE_DIR, exist_ok=True)
                _db = lancedb.connect(_LANCE_DIR)
                logger.info("LanceDB connected at %s", _LANCE_DIR)
    return _db


def _get_or_create_table():
    """Get the memories table, creating it if it doesn't exist."""
    db = _get_db()
    if _TABLE_NAME in db.table_names():
        return db.open_table(_TABLE_NAME)
    return db.create_table(_TABLE_NAME, schema=_SCHEMA)


def add_memory(
    text: str,
    category: str = "fact",
    source_session_id: str = "",
) -> str:
    """Embed text and store as a memory. Returns the memory ID or empty string on failure."""
    try:
        table = _get_or_create_table()
        vector = embed(text)

        # Dedup: skip if a very similar memory already exists
        try:
            if table.count_rows() > 0:
                results = table.search(vector).limit(1).to_list()
                if results and results[0].get("_distance", 1.0) < 0.05:
                    logger.info(
                        "Skipping duplicate memory (distance=%.4f, existing=%s)",
                        results[0]["_distance"],
                        results[0]["id"][:8],
                    )
                    _log_vector_store_event(
                        "succeeded",
                        details={
                            "operation": "add",
                            "category": category,
                            "deduplicated": True,
                            "source_session_id": source_session_id or None,
                        },
                    )
                    return results[0]["id"]
        except Exception:
            logger.debug("Dedup check failed, proceeding with insert", exc_info=True)

        memory_id = uuid.uuid4().hex

        table.add([{
            "id": memory_id,
            "text": text,
            "category": category,
            "source_session_id": source_session_id,
            "vector": vector,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }])

        logger.info("Added memory %s (category=%s)", memory_id[:8], category)
        _log_vector_store_event(
            "succeeded",
            details={
                "operation": "add",
                "category": category,
                "deduplicated": False,
                "source_session_id": source_session_id or None,
            },
        )
        return memory_id
    except Exception as exc:
        logger.exception("Failed to add memory")
        _log_vector_store_event(
            "failed",
            details={
                "operation": "add",
                "category": category,
                "source_session_id": source_session_id or None,
                "error": str(exc),
            },
        )
        return ""


def search_with_status(
    query: str,
    top_k: int = 0,
    category_filter: Optional[str] = None,
) -> tuple[list[dict], bool]:
    """Search memories by semantic similarity.

    Returns list of dicts with: id, text, category, score, created_at.
    Returns `(results, degraded)` where degraded is true on fail-open fallback.
    """
    try:
        if top_k <= 0:
            top_k = settings.memory_search_top_k

        table = _get_or_create_table()

        if table.count_rows() == 0:
            _log_vector_store_event(
                "empty_result",
                details={
                    "operation": "search",
                    "reason": "empty_table",
                    "query_length": _safe_query_length(query),
                    "category_filter": category_filter,
                    "top_k": top_k,
                },
            )
            return [], False

        query_vector = embed(query)

        results = table.search(query_vector).limit(top_k)

        if category_filter:
            # Use parameterized filter to prevent injection
            allowed_categories = {"fact", "preference", "pattern", "goal", "reflection"}
            if category_filter not in allowed_categories:
                return []
            results = results.where(f"category = '{category_filter}'")

        rows = results.to_list()

        if not rows:
            _log_vector_store_event(
                "empty_result",
                details={
                    "operation": "search",
                    "reason": "no_match",
                    "query_length": _safe_query_length(query),
                    "category_filter": category_filter,
                    "top_k": top_k,
                },
            )
            return [], False

        _log_vector_store_event(
            "succeeded",
            details={
                "operation": "search",
                "query_length": _safe_query_length(query),
                "category_filter": category_filter,
                "top_k": top_k,
                "result_count": len(rows),
            },
        )

        return [
            {
                "id": r["id"],
                "text": r["text"],
                "category": r["category"],
                "score": r.get("_distance", 0.0),
                "created_at": r["created_at"],
            }
            for r in rows
        ], False
    except Exception as exc:
        logger.exception("Failed to search memories")
        _log_vector_store_event(
            "failed",
            details={
                "operation": "search",
                "query_length": _safe_query_length(query),
                "category_filter": category_filter,
                "top_k": top_k,
                "error": str(exc),
            },
        )
        return [], True


def search(
    query: str,
    top_k: int = 0,
    category_filter: Optional[str] = None,
) -> list[dict]:
    """Search memories by semantic similarity.

    Returns list of dicts with: id, text, category, score, created_at.
    Returns [] on any failure.
    """
    results, _degraded = search_with_status(query, top_k, category_filter)
    return results


def search_formatted(
    query: str,
    top_k: int = 0,
    category_filter: Optional[str] = None,
) -> str:
    """Search memories and return a formatted string for agent context.

    Returns "" on any failure.
    """
    try:
        results, _degraded = search_with_status(query, top_k, category_filter)
        if not results:
            return ""
        lines = []
        for r in results:
            lines.append(f"- [{r['category']}] {r['text']}")
        return "\n".join(lines)
    except Exception:
        logger.exception("Failed to format memory search results")
        return ""


def _reset_vector_store_state() -> None:
    """Reset cached DB state for tests and deterministic evals."""
    global _db
    with _db_lock:
        _db = None
