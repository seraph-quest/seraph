import logging
import threading
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

_model: Optional[object] = None
_model_lock = threading.Lock()


def _get_model():
    """Lazy-load the sentence-transformers model (singleton, thread-safe)."""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                logger.info("Loading embedding model: %s", settings.embedding_model)
                from sentence_transformers import SentenceTransformer

                _model = SentenceTransformer(settings.embedding_model)
                logger.info("Embedding model loaded")
    return _model


def embed(text: str) -> list[float]:
    """Embed a single text string into a vector."""
    model = _get_model()
    return model.encode(text, normalize_embeddings=True).tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts into vectors."""
    model = _get_model()
    return model.encode(texts, normalize_embeddings=True).tolist()
