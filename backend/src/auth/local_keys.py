"""Vault-derived key material for local no-login mode (#1161).

Local single-operator mode has no operator credential, and a stored password
hash is verification data, never a MAC key.  Server MAC roots that would
otherwise fall back to the operator credential derive from the workspace vault
master key instead, so they are stable across restarts and never configured in
plain text.  Outside that mode this returns ``None`` and callers keep their
existing fail-closed behaviour.
"""
from __future__ import annotations


def local_no_login_vault_secret(info: bytes) -> str | None:
    from src.auth.service import local_no_login_active

    if not local_no_login_active():
        return None
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from src.vault.crypto import vault_key_material

    try:
        material = vault_key_material()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(material).hex()
