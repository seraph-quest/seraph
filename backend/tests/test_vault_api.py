"""Tests for vault HTTP endpoints (src/api/vault.py)."""

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
    with patch("src.vault.repository.encrypt", side_effect=_identity_encrypt), \
         patch("src.vault.repository.decrypt", side_effect=_identity_decrypt):
        yield


class TestListVaultKeys:
    async def test_empty(self, client):
        res = await client.get("/api/vault/keys")
        assert res.status_code == 200
        assert res.json() == []

    async def test_returns_metadata(self, client, async_db, repo):
        await repo.store("key_a", "val_a", description="A")
        await repo.store("key_b", "val_b", description="B")
        res = await client.get("/api/vault/keys")
        assert res.status_code == 200
        data = res.json()
        assert len(data) == 2
        keys = {entry["key"] for entry in data}
        assert keys == {"key_a", "key_b"}
        # Values must never be exposed
        for entry in data:
            assert "encrypted_value" not in entry
            assert "value" not in entry


class TestDeleteVaultKey:
    async def test_success(self, client, async_db, repo):
        await repo.store("to_delete", "val")
        res = await client.delete("/api/vault/keys/to_delete")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

    async def test_not_found(self, client):
        res = await client.delete("/api/vault/keys/nope")
        assert res.status_code == 404
