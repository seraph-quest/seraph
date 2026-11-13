"""OCR provider package — factory for creating providers.

ADR-035 (#1147): the daemon makes no cloud model call. The remote OpenRouter
provider was removed; Apple Vision stays as on-device OCR and captures reach
the backend over the authenticated local upload endpoint instead.
"""

from ocr.base import OCRProvider


def create_provider(
    name: str,
    model: str | None = None,
    preserve_captures: bool | None = None,
    archive_dir: str | None = None,
) -> OCRProvider:
    """Create an OCR provider by name.

    Args:
        name: "apple-vision" (on-device) or "codex-local" (explicit opt-in)
        model: Model name for codex-local (has a provider default)

    Raises:
        ValueError: Unknown or removed provider
    """
    if name == "apple-vision":
        from ocr.apple_vision import AppleVisionProvider

        return AppleVisionProvider()

    if name == "codex-local":
        # TODO(ADR-035, #1147): codex-local drives a cloud-backed Codex CLI and
        # is slated for removal. Its removal drops the no-API-price Codex path,
        # so it stays behind explicit selection until the operator acknowledges
        # that loss. Do not widen how it can be selected.
        from ocr.codex_local import CodexLocalProvider

        return CodexLocalProvider(
            model=model,
            preserve_captures=preserve_captures,
            archive_dir=archive_dir,
        )

    raise ValueError(f"Unknown OCR provider: {name!r} (choose 'apple-vision' or 'codex-local')")
