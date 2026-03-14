"""Tests for vault agent tools (src/tools/vault_tools.py)."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def mock_vault_deps():
    """Mock vault and audit dependencies for tool tests."""
    with patch("src.tools.vault_tools.vault_repository") as mock_repo, \
         patch("src.tools.vault_tools.audit_repository") as mock_audit, \
         patch("src.tools.vault_tools.get_current_session_id", return_value="s1"), \
         patch("src.tools.vault_tools.get_current_tool_policy_mode", return_value="full"):
        mock_audit.log_event = AsyncMock()
        yield mock_repo, mock_audit


class TestStoreSecret:
    def test_stores_and_confirms(self, mock_vault_deps):
        from src.tools.vault_tools import store_secret

        mock_vault_repo, mock_audit = mock_vault_deps
        mock_vault_repo.store = AsyncMock()
        result = store_secret.forward("my_token", "sk-123", "API token")
        assert "my_token" in result
        assert "stored" in result.lower()
        # Value should never appear in confirmation
        assert "sk-123" not in result
        mock_vault_repo.store.assert_called_once()
        mock_audit.log_event.assert_awaited_once()
        audit_call = mock_audit.log_event.await_args.kwargs
        assert audit_call["event_type"] == "secret_store"
        assert audit_call["details"] == {"key": "my_token", "has_description": True}
        assert "sk-123" not in str(audit_call)


class TestGetSecret:
    def test_found(self, mock_vault_deps):
        from src.tools.vault_tools import get_secret

        mock_vault_repo, mock_audit = mock_vault_deps
        mock_vault_repo.get = AsyncMock(return_value="decrypted-value")
        result = get_secret.forward("my_token")
        assert result == "decrypted-value"
        mock_audit.log_event.assert_awaited_once()
        audit_call = mock_audit.log_event.await_args.kwargs
        assert audit_call["event_type"] == "secret_access"
        assert audit_call["details"] == {"key": "my_token", "found": True}
        assert "decrypted-value" not in str(audit_call)

    def test_not_found(self, mock_vault_deps):
        from src.tools.vault_tools import get_secret

        mock_vault_repo, mock_audit = mock_vault_deps
        mock_vault_repo.get = AsyncMock(return_value=None)
        result = get_secret.forward("nope")
        assert "not found" in result.lower()
        mock_audit.log_event.assert_awaited_once()
        assert mock_audit.log_event.await_args.kwargs["details"] == {"key": "nope", "found": False}


class TestListSecrets:
    def test_empty(self, mock_vault_deps):
        from src.tools.vault_tools import list_secrets

        mock_vault_repo, mock_audit = mock_vault_deps
        mock_vault_repo.list_keys = AsyncMock(return_value=[])
        result = list_secrets.forward()
        assert "no secrets" in result.lower()
        mock_audit.log_event.assert_awaited_once()
        assert mock_audit.log_event.await_args.kwargs["details"] == {"count": 0}

    def test_returns_keys_not_values(self, mock_vault_deps):
        from src.tools.vault_tools import list_secrets

        mock_vault_repo, mock_audit = mock_vault_deps
        mock_vault_repo.list_keys = AsyncMock(return_value=[
            {"key": "token_a", "description": "Token A", "created_at": "2025-01-01", "updated_at": "2025-01-01"},
            {"key": "token_b", "description": None, "created_at": "2025-01-01", "updated_at": "2025-01-01"},
        ])
        result = list_secrets.forward()
        assert "token_a" in result
        assert "token_b" in result
        assert "Token A" in result
        mock_audit.log_event.assert_awaited_once()
        audit_call = mock_audit.log_event.await_args.kwargs
        assert audit_call["event_type"] == "secret_list"
        assert audit_call["details"] == {"count": 2}
        assert "token_a" not in str(audit_call)


class TestDeleteSecret:
    def test_success(self, mock_vault_deps):
        from src.tools.vault_tools import delete_secret

        mock_vault_repo, mock_audit = mock_vault_deps
        mock_vault_repo.delete = AsyncMock(return_value=True)
        result = delete_secret.forward("old_token")
        assert "deleted" in result.lower()
        mock_audit.log_event.assert_awaited_once()
        assert mock_audit.log_event.await_args.kwargs["details"] == {"key": "old_token", "deleted": True}

    def test_not_found(self, mock_vault_deps):
        from src.tools.vault_tools import delete_secret

        mock_vault_repo, mock_audit = mock_vault_deps
        mock_vault_repo.delete = AsyncMock(return_value=False)
        result = delete_secret.forward("nope")
        assert "not found" in result.lower()
        mock_audit.log_event.assert_awaited_once()
        assert mock_audit.log_event.await_args.kwargs["details"] == {"key": "nope", "deleted": False}


class TestVaultAuditResilience:
    def test_store_secret_succeeds_when_audit_logging_fails(self, mock_vault_deps):
        from src.tools.vault_tools import store_secret

        mock_vault_repo, mock_audit = mock_vault_deps
        mock_vault_repo.store = AsyncMock()
        mock_audit.log_event.side_effect = RuntimeError("audit down")

        result = store_secret.forward("my_token", "sk-123", "API token")

        assert "stored" in result.lower()
        mock_vault_repo.store.assert_called_once()
