import logging
import threading
from typing import Optional

from config.settings import settings
from src.audit.runtime import log_integration_event_sync

logger = logging.getLogger(__name__)

_model: Optional[object] = None
_model_lock = threading.Lock()
_LOAD_EVENT_EMITTED = False


def _embedder_name() -> str:
    return settings.embedding_model


def _log_embedding_event(outcome: str, details: dict | None = None) -> None:
    log_integration_event_sync(
        integration_type="embedding_model",
        name=_embedder_name(),
        outcome=outcome,
        details=details,
    )


def _get_model():
    """Lazy-load the sentence-transformers model (singleton, thread-safe)."""
    global _model, _LOAD_EVENT_EMITTED
    if _model is None:
        with _model_lock:
            if _model is None:
                logger.info("Loading embedding model: %s", settings.embedding_model)
                try:
                    from sentence_transformers import SentenceTransformer

                    _model = SentenceTransformer(settings.embedding_model)
                    logger.info("Embedding model loaded")
                    if not _LOAD_EVENT_EMITTED:
                        _log_embedding_event("loaded")
                        _LOAD_EVENT_EMITTED = True
                except Exception as exc:
                    _log_embedding_event(
                        "failed",
                        details={"stage": "load", "error": str(exc)},
                    )
                    raise
    return _model


def embed(text: str) -> list[float]:
    """Embed a single text string into a vector."""
    model = _get_model()
    try:
        return model.encode(text, normalize_embeddings=True).tolist()
    except Exception as exc:
        _log_embedding_event(
            "failed",
            details={"stage": "encode", "batch_size": 1, "error": str(exc)},
        )
        raise


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts into vectors."""
    model = _get_model()
    try:
        return model.encode(texts, normalize_embeddings=True).tolist()
    except Exception as exc:
        _log_embedding_event(
            "failed",
            details={"stage": "encode", "batch_size": len(texts), "error": str(exc)},
        )
        raise


def _reset_embedder_state() -> None:
    """Reset cached embedder state for tests and deterministic evals."""
    global _model, _LOAD_EVENT_EMITTED
    with _model_lock:
        _model = None
        _LOAD_EVENT_EMITTED = False
