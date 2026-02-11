"""OCR provider interface and result dataclass."""

import abc
from dataclasses import dataclass


@dataclass
class OCRResult:
    text: str
    provider: str
    duration_ms: int
    success: bool
    error: str | None = None


class OCRProvider(abc.ABC):
    """Abstract base for OCR providers."""

    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    @abc.abstractmethod
    async def extract_text(self, png_bytes: bytes) -> OCRResult:
        """Extract text from a PNG screenshot."""
        ...

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Check if this provider can run (dependencies / keys present)."""
        ...
