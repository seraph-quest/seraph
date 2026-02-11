"""OCR provider package — factory for creating providers."""

from ocr.base import OCRProvider


def create_provider(
    name: str,
    api_key: str | None = None,
    model: str | None = None,
) -> OCRProvider:
    """Create an OCR provider by name.

    Args:
        name: "apple-vision" or "openrouter"
        api_key: Required for openrouter provider
        model: Model name for openrouter (has default)

    Raises:
        ValueError: Unknown provider or missing api_key for openrouter
    """
    if name == "apple-vision":
        from ocr.apple_vision import AppleVisionProvider

        return AppleVisionProvider()

    if name == "openrouter":
        if not api_key:
            raise ValueError("OpenRouter provider requires --openrouter-api-key or OPENROUTER_API_KEY env var")
        from ocr.openrouter import OpenRouterProvider

        return OpenRouterProvider(api_key=api_key, model=model)

    raise ValueError(f"Unknown OCR provider: {name!r} (choose 'apple-vision' or 'openrouter')")
