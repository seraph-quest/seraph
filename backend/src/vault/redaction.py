"""Helpers for preventing vault secrets from leaking into chat output."""

import re

from src.vault.repository import vault_repository

_MIN_SECRET_LENGTH = 6


async def redact_secrets_in_text(text: str) -> str:
    """Replace known secret values with a generic redaction marker."""
    if not text:
        return text

    secret_pairs = await vault_repository.list_secret_values()
    if not secret_pairs:
        return text

    redacted = text
    # Replace longer secrets first to avoid partial matches masking the full value.
    for _, secret_value in sorted(secret_pairs, key=lambda item: len(item[1]), reverse=True):
        if len(secret_value) < _MIN_SECRET_LENGTH:
            continue
        redacted = re.sub(re.escape(secret_value), "[redacted secret]", redacted)
    return redacted
