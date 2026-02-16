"""Fernet-based encryption for the agent vault."""

import logging
import os
import stat

from cryptography.fernet import Fernet

from config.settings import settings

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

    key = settings.vault_encryption_key

    if not key:
        key_path = os.path.join(settings.workspace_dir, ".vault-key")
        if os.path.exists(key_path):
            with open(key_path, "r") as f:
                key = f.read().strip()
            logger.info("Vault key loaded from %s", key_path)
        else:
            key = Fernet.generate_key().decode()
            os.makedirs(os.path.dirname(key_path), exist_ok=True)
            with open(key_path, "w") as f:
                f.write(key)
            os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)  # 600
            logger.info("Vault key generated and saved to %s", key_path)

    _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string, returning base64-encoded ciphertext."""
    f = _get_fernet()
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a ciphertext string, returning the original plaintext."""
    f = _get_fernet()
    return f.decrypt(ciphertext.encode()).decode()
