"""Tests for vault encryption (src/vault/crypto.py)."""

import os
import tempfile
from unittest.mock import patch

import pytest


class TestEncryptDecrypt:
    def test_roundtrip(self):
        """Encrypt then decrypt returns original plaintext."""
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()

        with patch("src.vault.crypto.settings") as mock_settings:
            mock_settings.vault_encryption_key = key
            mock_settings.workspace_dir = "/tmp"

            # Reset cached fernet
            import src.vault.crypto as mod
            mod._fernet = None

            ciphertext = mod.encrypt("my-secret-token")
            assert ciphertext != "my-secret-token"
            assert mod.decrypt(ciphertext) == "my-secret-token"

            mod._fernet = None

    def test_different_plaintexts_produce_different_ciphertexts(self):
        from cryptography.fernet import Fernet

        key = Fernet.generate_key().decode()

        with patch("src.vault.crypto.settings") as mock_settings:
            mock_settings.vault_encryption_key = key
            mock_settings.workspace_dir = "/tmp"

            import src.vault.crypto as mod
            mod._fernet = None

            c1 = mod.encrypt("secret-a")
            c2 = mod.encrypt("secret-b")
            assert c1 != c2

            mod._fernet = None


class TestAutoKeyGeneration:
    def test_generates_key_file(self):
        """When no key is configured, a key file is auto-generated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            key_path = os.path.join(tmpdir, ".vault-key")

            with patch("src.vault.crypto.settings") as mock_settings:
                mock_settings.vault_encryption_key = ""
                mock_settings.workspace_dir = tmpdir

                import src.vault.crypto as mod
                mod._fernet = None

                plaintext = "auto-key-test"
                ciphertext = mod.encrypt(plaintext)
                assert mod.decrypt(ciphertext) == plaintext
                assert os.path.exists(key_path)

                # Key file should have restricted permissions
                stat = os.stat(key_path)
                assert oct(stat.st_mode & 0o777) == "0o600"

                mod._fernet = None

    def test_reuses_existing_key_file(self):
        """An existing key file is loaded on subsequent inits."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("src.vault.crypto.settings") as mock_settings:
                mock_settings.vault_encryption_key = ""
                mock_settings.workspace_dir = tmpdir

                import src.vault.crypto as mod
                mod._fernet = None

                # First call generates the key
                ciphertext = mod.encrypt("reuse-test")
                mod._fernet = None

                # Second call should reuse the same key and decrypt correctly
                assert mod.decrypt(ciphertext) == "reuse-test"
                mod._fernet = None
