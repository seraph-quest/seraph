import os
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Optional

import lancedb
import pyarrow as pa

from config.settings import settings
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
    """Embed text and store as a memory. Returns the memory ID."""
    table = _get_or_create_table()
    vector = embed(text)
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
    return memory_id


def search(
    query: str,
    top_k: int = 0,
    category_filter: Optional[str] = None,
) -> list[dict]:
    """Search memories by semantic similarity.

    Returns list of dicts with: id, text, category, score, created_at.
    """
    if top_k <= 0:
        top_k = settings.memory_search_top_k

    table = _get_or_create_table()

    if table.count_rows() == 0:
        return []

    query_vector = embed(query)

    results = table.search(query_vector).limit(top_k)

    if category_filter:
        # Use parameterized filter to prevent injection
        allowed_categories = {"fact", "preference", "pattern", "goal", "reflection"}
        if category_filter not in allowed_categories:
            return []
        results = results.where(f"category = '{category_filter}'")

    rows = results.to_list()

    return [
        {
            "id": r["id"],
            "text": r["text"],
            "category": r["category"],
            "score": r.get("_distance", 0.0),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def search_formatted(
    query: str,
    top_k: int = 0,
    category_filter: Optional[str] = None,
) -> str:
    """Search memories and return a formatted string for agent context."""
    results = search(query, top_k, category_filter)
    if not results:
        return ""
    lines = []
    for r in results:
        lines.append(f"- [{r['category']}] {r['text']}")
    return "\n".join(lines)
