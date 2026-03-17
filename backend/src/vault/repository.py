"""Async CRUD for the encrypted secret vault."""

import logging
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import InvalidToken
from sqlmodel import select

from src.audit.runtime import log_integration_event
from src.db.engine import get_session
from src.db.models import Secret
from src.vault.crypto import encrypt, decrypt

logger = logging.getLogger(__name__)


async def _log_vault_event(
    outcome: str,
    operation: str,
    **details: object,
) -> None:
    await log_integration_event(
        integration_type="vault",
        name="secrets",
        outcome=outcome,
        details={
            "operation": operation,
            **details,
        },
    )


class VaultRepository:
    """CRUD operations for the Secret table."""

    async def store(
        self,
        key: str,
        value: str,
        description: Optional[str] = None,
    ) -> Secret:
        """Upsert an encrypted secret."""
        try:
            encrypted = encrypt(value)
            async with get_session() as db:
                result = await db.execute(select(Secret).where(Secret.key == key))
                existing = result.scalars().first()
                if existing:
                    existing.encrypted_value = encrypted
                    if description is not None:
                        existing.description = description
                    existing.updated_at = datetime.now(timezone.utc)
                    db.add(existing)
                    logger.info("Vault: updated secret '%s'", key)
                    await _log_vault_event(
                        "succeeded",
                        "store",
                        action="updated",
                        description_present=description is not None,
                    )
                    return existing

                secret = Secret(
                    key=key,
                    encrypted_value=encrypted,
                    description=description,
                )
                db.add(secret)
                await db.flush()
                logger.info("Vault: stored new secret '%s'", key)
                await _log_vault_event(
                    "succeeded",
                    "store",
                    action="created",
                    description_present=description is not None,
                )
                return secret
        except Exception as exc:
            await _log_vault_event("failed", "store", error=str(exc))
            raise

    async def get(self, key: str) -> Optional[str]:
        """Retrieve and decrypt a secret value. Returns None if not found."""
        try:
            async with get_session() as db:
                result = await db.execute(select(Secret).where(Secret.key == key))
                secret = result.scalars().first()
                if not secret:
                    await _log_vault_event("empty_result", "get", reason="missing_secret")
                    return None

                value = decrypt(secret.encrypted_value)
                await _log_vault_event("succeeded", "get", found=True)
                return value
        except Exception as exc:
            await _log_vault_event("failed", "get", error=str(exc))
            raise

    async def list_keys(self) -> list[dict]:
        """List all secret keys with metadata (never values)."""
        try:
            async with get_session() as db:
                result = await db.execute(select(Secret))
                secrets = result.scalars().all()
                if not secrets:
                    await _log_vault_event("empty_result", "list_keys", reason="empty_vault")
                    return []

                keys = [
                    {
                        "key": s.key,
                        "description": s.description,
                        "created_at": s.created_at.isoformat(),
                        "updated_at": s.updated_at.isoformat(),
                    }
                    for s in secrets
                ]
                await _log_vault_event("succeeded", "list_keys", secret_count=len(keys))
                return keys
        except Exception as exc:
            await _log_vault_event("failed", "list_keys", error=str(exc))
            raise

    async def list_secret_values(self) -> list[tuple[str, str]]:
        """Return decrypted secret values for internal redaction safeguards."""
        try:
            async with get_session() as db:
                result = await db.execute(select(Secret))
                secrets = result.scalars().all()
                if not secrets:
                    await _log_vault_event("empty_result", "list_secret_values", reason="empty_vault")
                    return []

                decrypted: list[tuple[str, str]] = []
                undecryptable_count = 0
                for secret in secrets:
                    try:
                        decrypted.append((secret.key, decrypt(secret.encrypted_value)))
                    except (InvalidToken, ValueError, TypeError):
                        undecryptable_count += 1
                        logger.warning(
                            "Vault: skipping undecryptable secret '%s' during redaction lookup",
                            secret.key,
                        )

                if not decrypted:
                    await _log_vault_event(
                        "empty_result",
                        "list_secret_values",
                        reason="no_decryptable_secrets",
                        secret_count=len(secrets),
                        undecryptable_count=undecryptable_count,
                    )
                    return []

                await _log_vault_event(
                    "succeeded",
                    "list_secret_values",
                    secret_count=len(secrets),
                    decryptable_count=len(decrypted),
                    undecryptable_count=undecryptable_count,
                )
                return decrypted
        except Exception as exc:
            await _log_vault_event("failed", "list_secret_values", error=str(exc))
            raise

    async def delete(self, key: str) -> bool:
        """Delete a secret by key. Returns True if deleted."""
        try:
            async with get_session() as db:
                result = await db.execute(select(Secret).where(Secret.key == key))
                secret = result.scalars().first()
                if not secret:
                    await _log_vault_event("empty_result", "delete", reason="missing_secret")
                    return False

                await db.delete(secret)
                logger.info("Vault: deleted secret '%s'", key)
                await _log_vault_event("succeeded", "delete", deleted=True)
                return True
        except Exception as exc:
            await _log_vault_event("failed", "delete", error=str(exc))
            raise

    async def exists(self, key: str) -> bool:
        """Check if a secret exists."""
        async with get_session() as db:
            result = await db.execute(select(Secret).where(Secret.key == key))
            return result.scalars().first() is not None


vault_repository = VaultRepository()
