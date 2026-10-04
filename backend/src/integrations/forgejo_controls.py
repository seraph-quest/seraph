"""Fixed Forgejo connection controls; configuration has zero provider contact."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import uuid

from sqlalchemy import text
from sqlmodel import select

from src.browser.forgejo_issue_title import ForgejoError, PROFILE, PROVIDER_VERSION, canonical, checked_segment
from src.db import engine
from src.db.models import ForgejoConnection, OperatorSession, Secret
from src.vault.crypto import encrypt
from src.vault.repository import secret_binding_digest, vault_repository


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


async def writer(db):
    if db.get_bind().dialect.name == "sqlite": await db.execute(text("BEGIN IMMEDIATE"))


async def original_root(db, owner):
    root = await db.scalar(select(OperatorSession).where(
        OperatorSession.id == owner.session_id, OperatorSession.principal_id == owner.principal_id,
        OperatorSession.revoked_at.is_(None), OperatorSession.replaced_by_id.is_(None),
        OperatorSession.is_bearer_tombstone.is_(False), OperatorSession.idle_expires_at > now(),
        OperatorSession.absolute_expires_at > now()))
    captured = getattr(owner, "authenticated_token_hash", None)
    if root is None or not captured or root.token_hash != captured:
        raise ForgejoError("forgejo_original_authenticated_root_inactive", status_code=403)
    return root


def credential_payload(raw):
    try:
        if type(raw) is not str or len(raw.encode()) > 4096: raise ValueError()
        value = json.loads(raw)
        if type(value) is not dict or set(value) != {"user_name", "password"}: raise ValueError()
        checked_segment(value["user_name"])
        password = value["password"]
        if (type(password) is not str or not 8 <= len(password.encode()) <= 512
            or any(ord(c) < 32 for c in password)): raise ValueError()
        return value
    except (ValueError, TypeError, KeyError):
        raise ForgejoError("forgejo_vault_credential_schema_invalid", status_code=422) from None


def connection_view(row, *, available=False):
    return {"configured": row is not None, "connection_id": row.id if row else None,
        "revision": row.revision if row else 0, "state": row.state if row else "configured",
        "site_profile": PROFILE, "provider_version": PROVIDER_VERSION,
        "provider_user_id": row.provider_user_id if row else None,
        "provider_login": row.provider_login if row else "",
        "read_consent_revision": row.read_consent_revision if row else 0,
        "read_consent_expires_at": utc(row.read_consent_expires_at).isoformat() if row and row.read_consent_expires_at else None,
        "provisioning_job_id": row.provisioning_job_id if row else None,
        "available": available, "production_acceptance": "blocked_unverified",
        "credential_is_consent": False, "no_learning": True}


class ForgejoService:
    def __init__(self, *, browser=None):
        from src.browser.forgejo_profile import ForgejoTitleBrowser
        self.browser = browser or ForgejoTitleBrowser()
        # Imported lazily: native callbacks reuse the control module's pure
        # canonical checks without introducing a parallel authority store.
        self._native = None

    @property
    def native(self):
        if self._native is None:
            from src.browser.forgejo_native import ForgejoNative
            self._native = ForgejoNative(self)
        return self._native

    async def connection(self, owner):
        async with engine.get_session() as db:
            await original_root(db, owner)
            row = await db.scalar(select(ForgejoConnection).where(
                ForgejoConnection.owner_principal_id == owner.principal_id,
                ForgejoConnection.owner_session_id == owner.session_id))
            return connection_view(row, available=not self.browser.production_blocked)

    async def configure_from_vault(self, owner, *, vault_key, expected_revision):
        """Stage source/decrypt/encrypt outside the short canonical writer."""
        if type(expected_revision) is not int or expected_revision < 0:
            raise ForgejoError("forgejo_configuration_revision_invalid", status_code=422)
        snapshot = await vault_repository.snapshot(vault_key, owner_principal_id=owner.principal_id)
        if snapshot is None: raise ForgejoError("forgejo_owner_vault_credential_required")
        value = credential_payload(snapshot.value)
        encrypted = encrypt(canonical(value).decode())
        identity = str(uuid.uuid4())
        async with engine.get_session() as db:
            await writer(db)
            await original_root(db, owner)
            source = await db.scalar(select(Secret).where(Secret.key == vault_key,
                Secret.owner_principal_id == owner.principal_id, Secret.revoked_at.is_(None)))
            if source is None or secret_binding_digest(source) != snapshot.binding_digest:
                raise ForgejoError("forgejo_staged_vault_credential_changed")
            row = await db.scalar(select(ForgejoConnection).where(ForgejoConnection.owner_principal_id == owner.principal_id))
            if (row is None and expected_revision != 0) or (row and row.revision != expected_revision):
                raise ForgejoError("forgejo_configuration_revision_changed")
            if row is None:
                row = ForgejoConnection(id=identity, owner_principal_id=owner.principal_id,
                                        owner_session_id=owner.session_id)
            else:
                for key in (row.credential_vault_key, row.session_vault_key):
                    old = await db.scalar(select(Secret).where(Secret.key == key,
                        Secret.owner_principal_id == owner.principal_id)) if key else None
                    if old: old.revoked_at = now(); db.add(old)
                row.revision += 1
                row.read_consent_revision += 1
                row.owner_session_id = owner.session_id
            key = "forgejo.credentials." + row.id + "." + str(row.revision)
            secret = Secret(key=key, owner_principal_id=owner.principal_id,
                            encrypted_value=encrypted, description="Fixed Forgejo backend credential")
            db.add(secret); await db.flush(); await db.refresh(secret)
            row.credential_vault_key, row.credential_binding = key, secret_binding_digest(secret)
            row.state, row.session_vault_key, row.session_binding = "configured", "", ""
            row.provider_user_id, row.provider_login = None, ""
            row.read_consent_expires_at = None
            row.provisioning_job_id = None
            row.updated_at = now(); db.add(row)
            return connection_view(row, available=not self.browser.production_blocked)

    async def revoke(self, owner, *, expected_revision):
        async with engine.get_session() as db:
            await writer(db)
            await original_root(db, owner)
            row = await db.scalar(select(ForgejoConnection).where(
                ForgejoConnection.owner_principal_id == owner.principal_id,
                ForgejoConnection.owner_session_id == owner.session_id))
            if row is None or row.revision != expected_revision:
                raise ForgejoError("forgejo_configuration_revision_changed")
            old = await db.scalar(select(Secret).where(Secret.key == row.session_vault_key,
                Secret.owner_principal_id == owner.principal_id)) if row.session_vault_key else None
            if old: old.revoked_at = now(); db.add(old)
            row.revision += 1; row.read_consent_revision += 1
            row.state = "revoked"; row.read_consent_expires_at = None
            row.session_vault_key = row.session_binding = ""
            row.updated_at = now(); db.add(row)
            return connection_view(row, available=not self.browser.production_blocked)


forgejo_service = ForgejoService()
