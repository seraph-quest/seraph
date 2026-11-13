"""OCR provider package — factory for creating providers.

ADR-035 (#1147): the daemon makes no cloud model call. The remote OpenRouter
provider was removed; Apple Vision stays as on-device OCR and captures reach
the backend over the authenticated local upload endpoint instead. The
cloud-backed `codex-local` provider was removed by operator decision on
2026-11-14.
"""

from ocr.base import OCRProvider

REMOVED_PROVIDERS = frozenset({"openrouter", "codex-local"})


def create_provider(
    name: str,
    model: str | None = None,
    preserve_captures: bool | None = None,
    archive_dir: str | None = None,
) -> OCRProvider:
    """Create an OCR provider by name.

    Args:
        name: "apple-vision" (on-device) — the only daemon OCR provider.
        model: Ignored; kept for call-site compatibility.

    Raises:
        ValueError: Unknown or removed provider. ``codex-local`` (a
        cloud-backed Codex CLI) was removed by operator decision on
        2026-11-14 (ADR-035) and is refused here, never spawned.
    """
    if name == "apple-vision":
        from ocr.apple_vision import AppleVisionProvider

        return AppleVisionProvider()

    if name in REMOVED_PROVIDERS:
        raise ValueError(
            f"OCR provider {name!r} was removed (ADR-035): the daemon makes no cloud model call; "
            "use 'apple-vision'"
        )

    raise ValueError(f"Unknown OCR provider: {name!r} (the only provider is 'apple-vision')")
