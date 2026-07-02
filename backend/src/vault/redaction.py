"""Helpers for preventing vault secrets from leaking into chat output."""

import logging
import re

from src.vault.repository import vault_repository

_MIN_SECRET_LENGTH = 6
logger = logging.getLogger(__name__)


async def redact_secrets_in_text(text: str, *, fail_closed: bool = False) -> str:
    """Replace known secret values with a generic redaction marker."""
    if not text:
        return text

    try:
        secret_pairs = await vault_repository.list_secret_values()
    except Exception:
        if fail_closed:
            logger.warning("Vault redaction lookup failed; returning safety placeholder", exc_info=True)
            return "[redaction unavailable]"
        logger.warning("Vault redaction lookup failed; returning original text", exc_info=True)
        return text

    if not secret_pairs:
        return text

    redacted = text
    # Replace longer secrets first to avoid partial matches masking the full value.
    for _, secret_value in sorted(secret_pairs, key=lambda item: len(item[1]), reverse=True):
        if len(secret_value) < _MIN_SECRET_LENGTH:
            continue
        redacted = re.sub(re.escape(secret_value), "[redacted secret]", redacted)
    return redacted


async def redact_secrets_for_streaming_snapshot(text: str, emitted_chars: int) -> tuple[str, int]:
    """Return the next safe redacted prefix delta for a streamed text snapshot.

    Streaming cannot redact each token independently because a secret may be split
    across chunks. This helper redacts the complete snapshot, then withholds a
    tail at least as long as the longest known secret so future chunks can still
    complete a secret boundary before anything is shown to the operator.
    """
    if not text:
        return "", emitted_chars

    try:
        secret_pairs = await vault_repository.list_secret_values()
    except Exception:
        logger.warning("Vault redaction lookup failed; withholding streamed text", exc_info=True)
        return "", emitted_chars

    secrets = [secret_value for _, secret_value in secret_pairs if len(secret_value) >= _MIN_SECRET_LENGTH]
    if not secrets:
        redacted = text
        safe_prefix_len = len(redacted)
    else:
        redacted = text
        for secret_value in sorted(secrets, key=len, reverse=True):
            redacted = re.sub(re.escape(secret_value), "[redacted secret]", redacted)
        safe_prefix_len = max(0, len(redacted) - max(len(secret_value) for secret_value in secrets) + 1)

    if safe_prefix_len <= emitted_chars:
        return "", emitted_chars
    return redacted[emitted_chars:safe_prefix_len], safe_prefix_len
