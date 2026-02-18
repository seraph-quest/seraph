"""OCR provider interface and result dataclass."""

import abc
from dataclasses import dataclass, field


@dataclass
class OCRResult:
    text: str
    provider: str
    duration_ms: int
    success: bool
    error: str | None = None


@dataclass
class AnalysisResult:
    """Structured screen analysis result from a vision model."""
    success: bool
    data: dict  # {activity, project, summary, details}
    duration_ms: int
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

    async def analyze_screen(self, png_bytes: bytes, app_name: str) -> AnalysisResult:
        """Analyze a screenshot and return structured activity data.

        Default implementation falls back to extract_text, wrapping output
        as an unstructured summary. Subclasses can override for structured analysis.
        """
        result = await self.extract_text(png_bytes)
        return AnalysisResult(
            success=result.success,
            data={
                "activity": "other",
                "project": None,
                "summary": result.text[:200] if result.text else "",
                "details": [],
            },
            duration_ms=result.duration_ms,
            error=result.error,
        )
