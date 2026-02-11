"""OpenRouter cloud OCR provider — uses vision models via OpenRouter API."""

import base64
import logging
import time

import httpx

from ocr.base import OCRProvider, OCRResult

logger = logging.getLogger("seraph_daemon")

_DEFAULT_MODEL = "google/gemini-2.0-flash-lite-001"

_SYSTEM_PROMPT = (
    "You are a screen reader. Describe the visible text on screen concisely. "
    "Focus on: file names, code, error messages, document titles, URLs, and UI labels. "
    "Output plain text only, no markdown."
)


class OpenRouterProvider(OCRProvider):
    """OCR using a cloud vision model via OpenRouter."""

    def __init__(self, api_key: str, model: str | None = None) -> None:
        self._api_key = api_key
        self._model = model or _DEFAULT_MODEL
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return "openrouter"

    def is_available(self) -> bool:
        return bool(self._api_key)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def extract_text(self, png_bytes: bytes) -> OCRResult:
        """Send screenshot to OpenRouter vision model for text extraction."""
        start = time.monotonic()
        try:
            b64 = base64.b64encode(png_bytes).decode("ascii")
            client = self._get_client()

            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/png;base64,{b64}",
                                        "detail": "low",
                                    },
                                },
                            ],
                        },
                    ],
                    "max_tokens": 500,
                },
            )
            response.raise_for_status()
            data = response.json()

            text = data["choices"][0]["message"]["content"].strip()
            duration_ms = int((time.monotonic() - start) * 1000)

            return OCRResult(
                text=text, provider=self.name, duration_ms=duration_ms, success=True,
            )

        except httpx.HTTPStatusError as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return OCRResult(
                text="", provider=self.name, duration_ms=duration_ms,
                success=False, error=f"HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            return OCRResult(
                text="", provider=self.name, duration_ms=duration_ms,
                success=False, error=str(exc),
            )

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
