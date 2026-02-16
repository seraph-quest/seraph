"""Tests for VaultRepository (src/vault/repository.py)."""

from unittest.mock import patch

import pytest

from src.vault.repository import VaultRepository


@pytest.fixture
def repo():
    return VaultRepository()


def _identity_encrypt(plaintext: str) -> str:
    return f"ENC:{plaintext}"


def _identity_decrypt(ciphertext: str) -> str:
    return ciphertext.removeprefix("ENC:")


@pytest.fixture(autouse=True)
def mock_crypto():
    """Replace real crypto with identity functions for repository tests."""
    with patch("src.vault.repository.encrypt", side_effect=_identity_encrypt), \
         patch("src.vault.repository.decrypt", side_effect=_identity_decrypt):
        yield


class TestStore:
    async def test_new_secret(self, async_db, repo):
        secret = await repo.store("api_key", "sk-123", description="Test key")
        assert secret.key == "api_key"
        assert secret.encrypted_value == "ENC:sk-123"
        assert secret.description == "Test key"

    async def test_upsert_overwrites(self, async_db, repo):
        await repo.store("token", "old-value")
        await repo.store("token", "new-value")
        result = await repo.get("token")
        assert result == "new-value"

    async def test_upsert_updates_description(self, async_db, repo):
        await repo.store("token", "val", description="v1")
        await repo.store("token", "val", description="v2")
        keys = await repo.list_keys()
        assert keys[0]["description"] == "v2"


class TestGet:
    async def test_existing(self, async_db, repo):
        await repo.store("my_key", "my_value")
        result = await repo.get("my_key")
        assert result == "my_value"

    async def test_nonexistent(self, async_db, repo):
        result = await repo.get("nope")
        assert result is None


class TestListKeys:
    async def test_empty(self, async_db, repo):
        keys = await repo.list_keys()
        assert keys == []

    async def test_returns_metadata_not_values(self, async_db, repo):
        await repo.store("secret_a", "value_a", description="A")
        await repo.store("secret_b", "value_b", description="B")
        keys = await repo.list_keys()
        assert len(keys) == 2
        for entry in keys:
            assert "key" in entry
            assert "description" in entry
            assert "created_at" in entry
            assert "updated_at" in entry
            assert "encrypted_value" not in entry
            assert "value" not in entry


class TestDelete:
    async def test_success(self, async_db, repo):
        await repo.store("to_delete", "val")
        assert await repo.delete("to_delete") is True
        assert await repo.get("to_delete") is None

    async def test_nonexistent(self, async_db, repo):
        assert await repo.delete("nope") is False


class TestExists:
    async def test_existing(self, async_db, repo):
        await repo.store("exists_key", "val")
        assert await repo.exists("exists_key") is True

    async def test_nonexistent(self, async_db, repo):
        assert await repo.exists("nope") is False
