"""Helpers for preventing vault secrets from leaking into chat output."""

import logging
import os
import re
import stat

from cryptography.fernet import Fernet, InvalidToken
from sqlmodel import select

from config.settings import settings
from src.db.models import Secret
from src.vault.repository import vault_repository
from src.workspace import canonical_workspace_root

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


async def redact_secrets_in_text_readonly(
    db,
    text: str,
    *,
    fail_closed: bool = True,
    minimum_secret_length: int = _MIN_SECRET_LENGTH,
    header_budget=None,
) -> str:
    """Redact through a caller-owned session without opening an audit writer.

    Work-board and evidence-detail writes may already hold a SQLite transaction,
    while passive projections must not create a nested vault audit transaction.
    This opt-in path reads the canonical secret rows using that same session;
    normal callers keep the audited helper above.
    """
    if type(minimum_secret_length) is not int or not 1 <= minimum_secret_length <= _MIN_SECRET_LENGTH:
        raise ValueError("secret redaction minimum must be an integer from 1 to 6")
    if not text:
        return text
    try:
        secrets = list((await db.execute(select(Secret))).scalars())
    except Exception:
        if fail_closed:
            return "[redaction unavailable]"
        return text
    if not secrets:
        return text

    fernet: Fernet | None = None
    configured_key = settings.vault_encryption_key
    if configured_key:
        try:
            fernet = Fernet(
                configured_key.encode() if isinstance(configured_key, str) else configured_key
            )
        except (TypeError, ValueError):
            fernet = None
    else:
        if header_budget is not None:
            from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
            if type(header_budget) is not HeaderReadBudget:
                raise HeaderBoundsError("canonical_bound_not_certified")
            header_budget.debit(4097)
        key_fd: int | None = None
        try:
            nofollow = getattr(os, "O_NOFOLLOW", None)
            if nofollow is None:
                raise OSError("secure vault-key open is unavailable")
            key_path = canonical_workspace_root(settings.workspace_dir) / ".vault-key"
            key_fd = os.open(
                key_path,
                os.O_RDONLY | nofollow | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0),
            )
            metadata = os.fstat(key_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
                raise OSError("vault key is not a bounded regular file")
            raw_bytes = os.read(key_fd, 4097)
            after = os.fstat(key_fd)
            named = os.stat(key_path, follow_symlinks=False)
            fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(metadata, field) != getattr(current, field) for current in (after, named) for field in fields):
                raise OSError("vault key changed while reading")
            raw_key = raw_bytes.strip()
            if len(raw_bytes) > 4096:
                raise OSError("vault key is too large")
            fernet = Fernet(raw_key)
        except (OSError, RuntimeError, TypeError, ValueError):
            fernet = None
        finally:
            if key_fd is not None:
                try:
                    os.close(key_fd)
                except OSError:
                    pass
    if fernet is None:
        return "[redaction unavailable]" if fail_closed else text

    values: list[str] = []
    try:
        for secret in secrets:
            value = fernet.decrypt(str(secret.encrypted_value or "").encode()).decode()
            if len(value) >= minimum_secret_length:
                values.append(value)
    except (InvalidToken, OSError, RuntimeError, TypeError, ValueError):
        return "[redaction unavailable]" if fail_closed else text

    redacted = text
    for value in sorted(values, key=len, reverse=True):
        redacted = re.sub(re.escape(value), "[redacted secret]", redacted)
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
