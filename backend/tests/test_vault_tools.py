"""Tests for vault agent tools (src/tools/vault_tools.py)."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_vault_repo():
    """Mock the vault repository for tool tests."""
    with patch("src.tools.vault_tools.vault_repository") as mock:
        yield mock


class TestStoreSecret:
    def test_stores_and_confirms(self, mock_vault_repo):
        from src.tools.vault_tools import store_secret

        mock_vault_repo.store = AsyncMock()
        result = store_secret.forward("my_token", "sk-123", "API token")
        assert "my_token" in result
        assert "stored" in result.lower()
        # Value should never appear in confirmation
        assert "sk-123" not in result
        mock_vault_repo.store.assert_called_once()


class TestGetSecret:
    def test_found(self, mock_vault_repo):
        from src.tools.vault_tools import get_secret

        mock_vault_repo.get = AsyncMock(return_value="decrypted-value")
        result = get_secret.forward("my_token")
        assert result == "decrypted-value"

    def test_not_found(self, mock_vault_repo):
        from src.tools.vault_tools import get_secret

        mock_vault_repo.get = AsyncMock(return_value=None)
        result = get_secret.forward("nope")
        assert "not found" in result.lower()


class TestListSecrets:
    def test_empty(self, mock_vault_repo):
        from src.tools.vault_tools import list_secrets

        mock_vault_repo.list_keys = AsyncMock(return_value=[])
        result = list_secrets.forward()
        assert "no secrets" in result.lower()

    def test_returns_keys_not_values(self, mock_vault_repo):
        from src.tools.vault_tools import list_secrets

        mock_vault_repo.list_keys = AsyncMock(return_value=[
            {"key": "token_a", "description": "Token A", "created_at": "2025-01-01", "updated_at": "2025-01-01"},
            {"key": "token_b", "description": None, "created_at": "2025-01-01", "updated_at": "2025-01-01"},
        ])
        result = list_secrets.forward()
        assert "token_a" in result
        assert "token_b" in result
        assert "Token A" in result


class TestDeleteSecret:
    def test_success(self, mock_vault_repo):
        from src.tools.vault_tools import delete_secret

        mock_vault_repo.delete = AsyncMock(return_value=True)
        result = delete_secret.forward("old_token")
        assert "deleted" in result.lower()

    def test_not_found(self, mock_vault_repo):
        from src.tools.vault_tools import delete_secret

        mock_vault_repo.delete = AsyncMock(return_value=False)
        result = delete_secret.forward("nope")
        assert "not found" in result.lower()
