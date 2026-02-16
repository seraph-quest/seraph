"""Async CRUD for the encrypted secret vault."""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import select

from src.db.engine import get_session
from src.db.models import Secret
from src.vault.crypto import encrypt, decrypt

logger = logging.getLogger(__name__)


class VaultRepository:
    """CRUD operations for the Secret table."""

    async def store(
        self,
        key: str,
        value: str,
        description: Optional[str] = None,
    ) -> Secret:
        """Upsert an encrypted secret."""
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
                return existing
            secret = Secret(
                key=key,
                encrypted_value=encrypted,
                description=description,
            )
            db.add(secret)
            await db.flush()
            logger.info("Vault: stored new secret '%s'", key)
            return secret

    async def get(self, key: str) -> Optional[str]:
        """Retrieve and decrypt a secret value. Returns None if not found."""
        async with get_session() as db:
            result = await db.execute(select(Secret).where(Secret.key == key))
            secret = result.scalars().first()
            if not secret:
                return None
            return decrypt(secret.encrypted_value)

    async def list_keys(self) -> list[dict]:
        """List all secret keys with metadata (never values)."""
        async with get_session() as db:
            result = await db.execute(select(Secret))
            secrets = result.scalars().all()
            return [
                {
                    "key": s.key,
                    "description": s.description,
                    "created_at": s.created_at.isoformat(),
                    "updated_at": s.updated_at.isoformat(),
                }
                for s in secrets
            ]

    async def delete(self, key: str) -> bool:
        """Delete a secret by key. Returns True if deleted."""
        async with get_session() as db:
            result = await db.execute(select(Secret).where(Secret.key == key))
            secret = result.scalars().first()
            if not secret:
                return False
            await db.delete(secret)
            logger.info("Vault: deleted secret '%s'", key)
            return True

    async def exists(self, key: str) -> bool:
        """Check if a secret exists."""
        async with get_session() as db:
            result = await db.execute(select(Secret).where(Secret.key == key))
            return result.scalars().first() is not None


vault_repository = VaultRepository()
