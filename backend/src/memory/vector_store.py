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
from src.memory.embedder import EmbeddingMetadata, EmbeddingUnavailableError, embed, embedding_metadata
from src.workspace import WorkspaceStateClass, canonical_workspace_registry, canonical_workspace_root

logger = logging.getLogger(__name__)

_TABLE_NAME = "memories"
_TABLE_NAME_PREFIX = f"{_TABLE_NAME}__"

# Historical schema retained only for explicitly non-OpenRouter compatibility
# tests and old data reads. Active vectors use a model/dimension namespace.
_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("text", pa.string()),
    pa.field("category", pa.string()),
    pa.field("source_session_id", pa.string()),
    pa.field("vector", pa.list_(pa.float32(), 384)),
    pa.field("created_at", pa.string()),
])


def _schema_for_embedding(metadata: EmbeddingMetadata | None) -> pa.Schema:
    """Build a schema whose vector width is tied to one embedding namespace.

    Historical ``memories`` data is not selected by active paths. New
    OpenRouter vectors are written to a model/dimension-qualified table so a
    model switch cannot mix incompatible vector spaces.
    """
    if metadata is None:
        raise EmbeddingUnavailableError(
            "embedding_metadata_required",
            stage="metadata",
        )
    return pa.schema([
        pa.field("id", pa.string()),
        pa.field("text", pa.string()),
        pa.field("category", pa.string()),
        pa.field("source_session_id", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), metadata.dimension)),
        pa.field("created_at", pa.string()),
        pa.field("embedding_namespace", pa.string()),
        pa.field("embedding_model", pa.string()),
        pa.field("embedding_schema_version", pa.string()),
        pa.field("embedding_dimension", pa.int32()),
    ])


def _table_name(metadata: EmbeddingMetadata | None) -> str:
    if metadata is None:
        raise EmbeddingUnavailableError(
            "embedding_metadata_required",
            stage="metadata",
        )
    return f"{_TABLE_NAME_PREFIX}{metadata.namespace}"


def _active_embedding_metadata() -> EmbeddingMetadata | None:
    metadata = embedding_metadata()
    if metadata is None:
        raise EmbeddingUnavailableError(
            "embedding_metadata_required",
            stage="metadata",
        )
    return metadata

_db: Optional[lancedb.DBConnection] = None
_db_lock = threading.Lock()


def _lance_dir() -> str:
    """Resolve the derived vector store below the canonical workspace root."""
    workspace_root = canonical_workspace_root(settings.workspace_dir)
    registry = canonical_workspace_registry(workspace_root)
    if registry.classify_path("lance") is not WorkspaceStateClass.DERIVED:
        raise RuntimeError("vector store path is not owned by derived workspace state")
    return str(workspace_root / "lance")


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
                lance_dir = _lance_dir()
                os.makedirs(lance_dir, exist_ok=True)
                _db = lancedb.connect(lance_dir)
                logger.info("LanceDB connected at %s", lance_dir)
    return _db


def _get_or_create_table(*, metadata: EmbeddingMetadata | None = None):
    """Get the active embedding namespace table, creating it if necessary."""
    if metadata is None:
        metadata = _active_embedding_metadata()
    db = _get_db()
    table_name = _table_name(metadata)
    if table_name in db.table_names():
        return db.open_table(table_name)
    return db.create_table(table_name, schema=_schema_for_embedding(metadata))


def add_memory(
    text: str,
    category: str = "fact",
    source_session_id: str = "",
) -> str:
    """Embed text and store as a memory. Returns the memory ID or empty string on failure."""
    try:
        vector = embed(text)
        metadata = _active_embedding_metadata()
        table = _get_or_create_table(metadata=metadata)

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

        row = {
            "id": memory_id,
            "text": text,
            "category": category,
            "source_session_id": source_session_id,
            "vector": vector,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        if metadata is not None:
            row.update(
                {
                    "embedding_namespace": metadata.namespace,
                    "embedding_model": metadata.model,
                    "embedding_schema_version": metadata.schema_version,
                    "embedding_dimension": metadata.dimension,
                }
            )
        table.add([row])

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

        if not isinstance(query, str):
            _log_vector_store_event(
                "empty_result",
                details={
                    "operation": "search",
                    "reason": "invalid_query",
                    "query_length": None,
                    "category_filter": category_filter,
                    "top_k": top_k,
                },
            )
            return [], False

        query_vector = embed(query)
        metadata = _active_embedding_metadata()
        table = _get_or_create_table(metadata=metadata)

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
