"""Tests for secret reference issuance and runtime resolution."""

import time
from unittest.mock import AsyncMock, patch

import pytest

from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.tools.secret_ref_tools import SecretRefResolvingTool, get_secret_ref


class DummyHeaderTool:
    name = "mcp_http_request"
    description = "Dummy request tool"
    inputs = {
        "headers": {"type": "object", "description": "Request headers"},
        "body": {"type": "string", "description": "Request body"},
    }
    output_type = "string"

    def __call__(self, *args, **kwargs):
        return {"args": args, "kwargs": kwargs}


class DummyWriteTool(DummyHeaderTool):
    name = "write_file"


def _issue_ref_for_session(session_id: str) -> str:
    with patch("src.tools.secret_ref_tools.vault_repository") as mock_repo, \
         patch("src.tools.vault_tools.audit_repository") as mock_audit, \
         patch("src.tools.secret_ref_tools.get_current_session_id", return_value=session_id), \
         patch("src.tools.vault_tools.get_current_session_id", return_value=session_id), \
         patch("src.tools.vault_tools.get_current_tool_policy_mode", return_value="full"):
        mock_repo.get = AsyncMock(return_value="super-secret-value")
        mock_audit.log_event = AsyncMock()
        return get_secret_ref.forward("api_token")


def test_get_secret_ref_returns_opaque_reference_without_leaking_value():
    result = _issue_ref_for_session("s1")
    assert result.startswith("secret://")
    assert "super-secret-value" not in result


def test_secret_ref_wrapper_resolves_nested_values_for_current_session():
    wrapped = SecretRefResolvingTool(DummyHeaderTool())
    secret_ref = _issue_ref_for_session("s1")
    tokens = set_runtime_context("s1", "high_risk")
    try:
        result = wrapped(
            headers={"Authorization": f"Bearer {secret_ref}"},
            body=f"token={secret_ref}",
        )
    finally:
        reset_runtime_context(tokens)

    assert result["kwargs"]["headers"]["Authorization"] == "Bearer super-secret-value"
    assert result["kwargs"]["body"] == "token=super-secret-value"


def test_secret_ref_wrapper_blocks_disallowed_tools():
    wrapped = SecretRefResolvingTool(DummyWriteTool())
    secret_ref = _issue_ref_for_session("s1")
    tokens = set_runtime_context("s1", "high_risk")
    try:
        with pytest.raises(ValueError, match="cannot receive secret references"):
            wrapped(body=f"token={secret_ref}")
    finally:
        reset_runtime_context(tokens)


def test_secret_ref_wrapper_does_not_resolve_other_session_refs():
    wrapped = SecretRefResolvingTool(DummyHeaderTool())
    secret_ref = _issue_ref_for_session("s1")
    tokens = set_runtime_context("s2", "high_risk")
    try:
        result = wrapped(headers={"Authorization": f"Bearer {secret_ref}"})
    finally:
        reset_runtime_context(tokens)

    assert result["kwargs"]["headers"]["Authorization"] == f"Bearer {secret_ref}"


def test_secret_ref_wrapper_does_not_resolve_expired_refs():
    wrapped = SecretRefResolvingTool(DummyHeaderTool())
    with patch("src.vault.refs.time.time", return_value=1_000.0):
        secret_ref = _issue_ref_for_session("s1")

    tokens = set_runtime_context("s1", "high_risk")
    try:
        with patch("src.vault.refs.time.time", return_value=1_000.0 + 3_601):
            result = wrapped(headers={"Authorization": f"Bearer {secret_ref}"})
    finally:
        reset_runtime_context(tokens)

    assert result["kwargs"]["headers"]["Authorization"] == f"Bearer {secret_ref}"
