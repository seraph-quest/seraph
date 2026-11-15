"""Fernet-based encryption for the agent vault."""

import logging
import os
import stat

from cryptography.fernet import Fernet

from config.settings import settings
from src.workspace import (
    WorkspaceStateClass,
    canonical_workspace_registry,
    canonical_workspace_root,
)

logger = logging.getLogger(__name__)

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """Lazy-initialise Fernet cipher.

    Key resolution order:
    1. ``settings.vault_encryption_key`` (explicit env/config)
    2. Key file at ``{workspace_dir}/.vault-key`` (auto-generated on first use)
    """
    global _fernet
    if _fernet is not None:
        return _fernet
    _fernet = Fernet(_vault_key(announce=True))
    return _fernet


def vault_key_material() -> bytes:
    """Return the vault master key bytes for derived server keys.

    Resolution matches the vault cipher (configured key, else the 0600
    workspace key file, generated on first use). Raises when the key is
    unreadable or malformed so callers fail closed; never log the value.
    """
    key = _vault_key(announce=False)
    Fernet(key)
    return key


def _vault_key(*, announce: bool) -> bytes:
    key = settings.vault_encryption_key

    if not key:
        workspace_root = canonical_workspace_root(settings.workspace_dir)
        registry = canonical_workspace_registry(workspace_root)
        if registry.classify_path(".vault-key") is not WorkspaceStateClass.SECRET_RECOVERY:
            raise RuntimeError("vault key path is not owned by the canonical secret/recovery state")
        key_path = workspace_root / ".vault-key"
        if os.path.exists(key_path):
            with open(key_path, "r") as f:
                key = f.read().strip()
            if announce:
                logger.info("Vault key loaded from %s", key_path)
        else:
            key = Fernet.generate_key().decode()
            os.makedirs(os.path.dirname(key_path), exist_ok=True)
            with open(key_path, "w") as f:
                f.write(key)
            os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)  # 600
            logger.info("Vault key generated and saved to %s", key_path)

    return key.encode() if isinstance(key, str) else key


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string, returning base64-encoded ciphertext."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a ciphertext string, returning the original plaintext."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()
