"""Apple Vision framework OCR provider — local, offline text extraction."""

import asyncio
import logging
import time

from ocr.base import OCRProvider, OCRResult

logger = logging.getLogger("seraph_daemon")


class AppleVisionProvider(OCRProvider):
    """OCR using macOS Vision framework (VNRecognizeTextRequest).

    Runs entirely on-device. ~200ms per full-screen capture on Apple Silicon.
    """

    @property
    def name(self) -> str:
        return "apple-vision"

    def is_available(self) -> bool:
        try:
            import Vision  # noqa: F401

            return True
        except ImportError:
            return False

    async def extract_text(self, png_bytes: bytes) -> OCRResult:
        """Extract text from PNG bytes using Vision framework.

        Wraps the synchronous Vision call in asyncio.to_thread to avoid
        blocking the event loop.
        """
        return await asyncio.to_thread(self._extract_sync, png_bytes)

    def _extract_sync(self, png_bytes: bytes) -> OCRResult:
        start = time.monotonic()
        try:
            import Vision
            from Foundation import NSData

            # Pass PNG bytes directly to Vision — avoids the NSImage → CGImage
            # roundtrip which can produce empty pixel buffers on macOS 15+/Tahoe
            ns_data = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
            if ns_data is None:
                return OCRResult(
                    text="", provider=self.name, duration_ms=0,
                    success=False, error="Failed to create NSData from PNG bytes",
                )

            # Create and configure text recognition request
            request = Vision.VNRecognizeTextRequest.alloc().init()
            request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
            request.setUsesLanguageCorrection_(True)

            # Use initWithData:options: to feed raw PNG directly (macOS 13+)
            handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
                ns_data, None,
            )
            success, error = handler.performRequests_error_([request], None)

            if not success:
                duration_ms = int((time.monotonic() - start) * 1000)
                return OCRResult(
                    text="", provider=self.name, duration_ms=duration_ms,
                    success=False, error=str(error) if error else "VNImageRequestHandler failed",
                )

            # Extract text from observations
            results = request.results()
            lines = []
            for observation in results or []:
                candidates = observation.topCandidates_(1)
                if candidates and len(candidates) > 0:
                    lines.append(candidates[0].string())

            text = "\n".join(lines)
            duration_ms = int((time.monotonic() - start) * 1000)

            return OCRResult(
                text=text, provider=self.name, duration_ms=duration_ms, success=True,
            )

        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return OCRResult(
                text="", provider=self.name, duration_ms=duration_ms,
                success=False, error=str(exc),
            )
