"""Async CRUD for the encrypted secret vault."""

import logging
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import InvalidToken
from sqlmodel import select

from src.audit.runtime import log_integration_event
from src.db.engine import get_session
from src.db.models import Secret
from src.vault.crypto import encrypt, decrypt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SecretSnapshot:
    value: str = field(repr=False)
    binding_digest: str
    identity: dict


def secret_identity(secret: Secret) -> dict:
    """Pure loaded-row provenance; no decrypt, authentication or DB access."""
    return {"id": secret.id, "owner": secret.owner_principal_id,
        "key": secret.key,
        "updated_at": secret.updated_at.isoformat(),
        "ciphertext_sha256": hashlib.sha256(secret.encrypted_value.encode()).hexdigest()}


def secret_binding_digest(secret: Secret) -> str:
    # Preserve the established immutable consent digest encoding. The
    # protected READ envelope additionally binds the current exact vault name.
    identity = {key: value for key, value in secret_identity(secret).items() if key != "key"}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


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

    async def snapshot(self, key: str, *, owner_principal_id: str) -> SecretSnapshot | None:
        """Read credential and opaque provenance from one owner-scoped row."""
        async with get_session() as db:
            secret = (await db.execute(select(Secret).where(Secret.key == key,
                Secret.owner_principal_id == owner_principal_id,
                Secret.revoked_at.is_(None)))).scalars().first()
            if secret is None:
                return None
            return SecretSnapshot(decrypt(secret.encrypted_value), secret_binding_digest(secret), secret_identity(secret))

    async def bounded_snapshot(self, key: str, *, owner_principal_id: str) -> SecretSnapshot | None:
        """Single owner/name lookup; oversized ciphertext never reaches Python."""
        from sqlalchemy import case, func
        from types import SimpleNamespace
        if not isinstance(key, str) or not 1 <= len(key) <= 128 or any(ord(c) < 32 for c in key):
            raise ValueError("audio_metadata_key_name_invalid")
        bounded = case((func.octet_length(Secret.encrypted_value) <= 16384, Secret.encrypted_value), else_=None)
        async with get_session() as db:
            item = (await db.execute(select(Secret.id, Secret.key, Secret.owner_principal_id,
                Secret.updated_at, bounded).where(Secret.key == key,
                Secret.owner_principal_id == owner_principal_id, Secret.revoked_at.is_(None)).limit(1))).first()
        if item is None:
            return None
        if item[4] is None:
            raise ValueError("audio_metadata_credential_oversized")
        secret = SimpleNamespace(id=item[0], key=item[1], owner_principal_id=item[2],
            updated_at=item[3], encrypted_value=item[4])
        value = decrypt(secret.encrypted_value)
        if not value or len(value.encode()) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise ValueError("audio_metadata_credential_invalid")
        return SecretSnapshot(value, secret_binding_digest(secret), secret_identity(secret))

    async def store(
        self,
        key: str,
        value: str,
        description: Optional[str] = None,
        *, owner_principal_id: str | None = None,
    ) -> Secret:
        """Upsert an encrypted secret."""
        try:
            encrypted = encrypt(value)
            action = "created"
            async with get_session() as db:
                result = await db.execute(select(Secret).where(Secret.key == key))
                existing = result.scalars().first()
                if existing:
                    if existing.revoked_at is not None:
                        raise PermissionError("vault_secret_revoked")
                    if existing.owner_principal_id != owner_principal_id:
                        raise PermissionError("vault_owner_mismatch")
                    existing.encrypted_value = encrypted
                    if description is not None:
                        existing.description = description
                    existing.updated_at = datetime.now(timezone.utc)
                    db.add(existing)
                    action = "updated"
                    secret = existing
                    logger.info("Vault: updated secret '%s'", key)
                else:
                    secret = Secret(
                        key=key,
                        owner_principal_id=owner_principal_id,
                        encrypted_value=encrypted,
                        description=description,
                    )
                    db.add(secret)
                    await db.flush()
                    logger.info("Vault: stored new secret '%s'", key)

            await _log_vault_event(
                "succeeded",
                "store",
                action=action,
                description_present=description is not None,
            )
            return secret
        except Exception as exc:
            await _log_vault_event("failed", "store", error=str(exc))
            raise

    async def get(self, key: str, *, owner_principal_id: str | None = None) -> Optional[str]:
        """Retrieve and decrypt a secret value. Returns None if not found."""
        try:
            async with get_session() as db:
                result = await db.execute(select(Secret).where(Secret.key == key, Secret.owner_principal_id == owner_principal_id, Secret.revoked_at.is_(None)))
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

    async def list_keys(self, *, owner_principal_id: str | None = None) -> list[dict]:
        """List all secret keys with metadata (never values)."""
        try:
            async with get_session() as db:
                result = await db.execute(select(Secret).where(Secret.owner_principal_id == owner_principal_id, Secret.revoked_at.is_(None)))
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
                result = await db.execute(select(Secret).where(Secret.revoked_at.is_(None)))
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

    async def delete(self, key: str, *, owner_principal_id: str | None = None) -> bool:
        """Delete a secret by key. Returns True if deleted."""
        try:
            async with get_session() as db:
                result = await db.execute(select(Secret).where(Secret.key == key, Secret.owner_principal_id == owner_principal_id, Secret.revoked_at.is_(None)))
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

    async def exists(self, key: str, *, owner_principal_id: str | None = None) -> bool:
        """Check if a secret exists."""
        async with get_session() as db:
            result = await db.execute(select(Secret).where(Secret.key == key, Secret.owner_principal_id == owner_principal_id, Secret.revoked_at.is_(None)))
            return result.scalars().first() is not None


vault_repository = VaultRepository()
