"""Tests for secret reference issuance and runtime resolution."""

import time
from unittest.mock import AsyncMock, patch

import pytest

from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.tools.secret_ref_tools import SecretRefResolvingTool, get_secret_ref
from src.vault.refs import issue_secret_ref


class DummyHeaderTool:
    name = "mcp_http_request"
    description = "Dummy request tool"
    inputs = {
        "url": {"type": "string", "description": "Request URL"},
        "headers": {"type": "object", "description": "Request headers"},
        "body": {"type": "string", "description": "Request body"},
    }
    output_type = "string"
    def __init__(self):
        self.last_kwargs = {}
        self.seraph_source_context = {
            "authenticated_source": True,
            "hostname": "api.example.com",
            "credential_egress_policy": {
                "mode": "explicit_host_allowlist",
                "transport": "https",
                "allowed_hosts": ["api.example.com"],
            },
        }

    def __call__(self, *args, **kwargs):
        self.last_kwargs = dict(kwargs)
        return {"status": "ok"}


class DummyLeakyHeaderTool(DummyHeaderTool):
    def __call__(self, *args, **kwargs):
        self.last_kwargs = dict(kwargs)
        return {"headers": kwargs.get("headers", {})}


class DummyWriteTool(DummyHeaderTool):
    name = "write_file"


class DummyUnboundMcpTool(DummyHeaderTool):
    def __init__(self):
        self.seraph_source_context = {
            "authenticated_source": True,
            "hostname": "",
            "credential_egress_policy": {
                "mode": "blocked",
                "transport": "https",
                "allowed_hosts": [],
            },
        }


def _issue_ref_for_session(session_id: str) -> str:
    with patch("src.tools.secret_ref_tools.vault_repository") as mock_repo, \
         patch("src.tools.vault_tools.audit_repository") as mock_audit, \
         patch("src.tools.secret_ref_tools.get_current_session_id", return_value=session_id), \
         patch("src.tools.vault_tools.get_current_session_id", return_value=session_id), \
         patch("src.tools.vault_tools.get_current_tool_policy_mode", return_value="full"):
        mock_repo.get = AsyncMock(return_value="super-secret-value")
        mock_audit.log_event = AsyncMock()
        return get_secret_ref.forward(
            "api_token",
            tool_name="mcp_http_request",
            field_name="headers",
            destination_url="https://api.example.com/v1",
        )


def _secret_principal(session_id: str = "s1") -> TrustPrincipal:
    return TrustPrincipal(
        principal_id="operator:secret-test",
        principal_type=PrincipalType.OPERATOR,
        grants=(
            AuthorityGrant.CAPABILITY_EXECUTE,
            AuthorityGrant.CREDENTIAL_EGRESS,
        ),
        session_id=session_id,
    )


def test_get_secret_ref_returns_opaque_reference_without_leaking_value():
    result = _issue_ref_for_session("s1")
    assert result.startswith("secret://")
    assert "super-secret-value" not in result


def test_get_secret_ref_rejects_unscoped_reference_requests():
    with patch("src.tools.secret_ref_tools.vault_repository") as mock_repo, \
         patch("src.tools.vault_tools.audit_repository") as mock_audit, \
         patch("src.tools.secret_ref_tools.get_current_session_id", return_value="s1"):
        mock_repo.get = AsyncMock(return_value="super-secret-value")
        mock_audit.log_event = AsyncMock()
        result = get_secret_ref.forward("api_token")

    assert result == "Secret references require tool_name, field_name, and destination_url scope."
    assert "super-secret-value" not in result


def test_secret_ref_wrapper_denies_session_without_principal_before_resolution_or_call():
    tool = DummyHeaderTool()
    wrapped = SecretRefResolvingTool(tool)
    secret_ref = _issue_ref_for_session("s1")
    tokens = set_runtime_context("s1", "high_risk")
    try:
        with patch("src.tools.secret_ref_tools.resolve_secret_refs_with_values") as resolver:
            with pytest.raises(PermissionError, match="authenticated runtime authority"):
                wrapped(
                    url="https://api.example.com/v1",
                    headers={"Authorization": f"Bearer {secret_ref}"},
                )
    finally:
        reset_runtime_context(tokens)

    resolver.assert_not_called()
    assert tool.last_kwargs == {}


def test_secret_ref_wrapper_accepts_explicit_scoped_principal():
    tool = DummyHeaderTool()
    wrapped = SecretRefResolvingTool(tool)
    secret_ref = _issue_ref_for_session("s1")
    tokens = set_runtime_context(
        "s1",
        "high_risk",
        trust_principal=_secret_principal(),
    )
    try:
        assert wrapped(
            url="https://api.example.com/v1",
            headers={"Authorization": f"Bearer {secret_ref}"},
        )["status"] == "ok"
    finally:
        reset_runtime_context(tokens)


def test_secret_ref_wrapper_denies_unbound_runtime_before_resolution_or_call():
    tool = DummyHeaderTool()
    wrapped = SecretRefResolvingTool(tool)
    secret_ref = _issue_ref_for_session("s1")

    with patch("src.tools.secret_ref_tools.resolve_secret_refs_with_values") as resolver:
        with pytest.raises(PermissionError, match="authenticated runtime authority") as excinfo:
            wrapped(
                url="https://api.example.com/v1",
                headers={"Authorization": f"Bearer {secret_ref}"},
            )

    resolver.assert_not_called()
    assert tool.last_kwargs == {}
    assert "super-secret-value" not in str(excinfo.value)


@pytest.mark.parametrize(
    "principal",
    [
        TrustPrincipal(
            principal_id="operator:invalid",
            principal_type=PrincipalType.OPERATOR,
            authenticated=False,
            grants=(AuthorityGrant.CAPABILITY_EXECUTE, AuthorityGrant.CREDENTIAL_EGRESS),
            session_id="s1",
        ),
        TrustPrincipal(
            principal_id="operator:invalid",
            principal_type=PrincipalType.OPERATOR,
            revoked=True,
            grants=(AuthorityGrant.CAPABILITY_EXECUTE, AuthorityGrant.CREDENTIAL_EGRESS),
            session_id="s1",
        ),
        TrustPrincipal(
            principal_id="paired-edge:invalid",
            principal_type=PrincipalType.PAIRED_EDGE,
            grants=(AuthorityGrant.CAPABILITY_EXECUTE, AuthorityGrant.CREDENTIAL_EGRESS),
            session_id="s1",
        ),
        TrustPrincipal(
            principal_id="operator:under-scoped",
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.CREDENTIAL_EGRESS,),
            session_id="s1",
        ),
        TrustPrincipal(
            principal_id="operator:missing-credential-egress",
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
            session_id="s1",
        ),
        TrustPrincipal(
            principal_id="service:credential-denied",
            principal_type=PrincipalType.SERVICE,
            grants=(
                AuthorityGrant.CAPABILITY_EXECUTE,
                AuthorityGrant.CREDENTIAL_EGRESS,
            ),
            session_id="s1",
        ),
        TrustPrincipal(
            principal_id="operator:wrong-session",
            principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.CAPABILITY_EXECUTE, AuthorityGrant.CREDENTIAL_EGRESS),
            session_id="s2",
        ),
    ],
    ids=[
        "unauthenticated",
        "revoked",
        "paired-edge",
        "missing-capability",
        "missing-credential-egress",
        "service-operation-forbidden",
        "session-mismatch",
    ],
)
def test_secret_ref_wrapper_denies_invalid_session_principal_before_resolution_or_call(
    principal,
):
    tool = DummyHeaderTool()
    wrapped = SecretRefResolvingTool(tool)
    secret_ref = _issue_ref_for_session("s1")
    tokens = set_runtime_context(
        "s1",
        "high_risk",
        trust_principal=principal,
    )
    try:
        with patch("src.tools.secret_ref_tools.resolve_secret_refs_with_values") as resolver:
            with pytest.raises(PermissionError, match="authenticated runtime authority") as excinfo:
                wrapped(
                    url="https://api.example.com/v1",
                    headers={"Authorization": f"Bearer {secret_ref}"},
                )
    finally:
        reset_runtime_context(tokens)

    resolver.assert_not_called()
    assert tool.last_kwargs == {}
    assert "super-secret-value" not in str(excinfo.value)


def test_secret_ref_wrapper_blocks_result_secret_echo():
    wrapped = SecretRefResolvingTool(DummyLeakyHeaderTool())
    secret_ref = _issue_ref_for_session("s1")
    tokens = set_runtime_context(
        "s1", "high_risk", trust_principal=_secret_principal()
    )
    try:
        with pytest.raises(ValueError, match="redaction failure blocked"):
            wrapped(
                url="https://api.example.com/v1",
                headers={"Authorization": f"Bearer {secret_ref}"},
            )
    finally:
        reset_runtime_context(tokens)


def test_secret_ref_wrapper_blocks_refs_outside_allowlisted_fields():
    wrapped = SecretRefResolvingTool(DummyHeaderTool())
    secret_ref = _issue_ref_for_session("s1")
    tokens = set_runtime_context(
        "s1", "high_risk", trust_principal=_secret_principal()
    )
    try:
        with pytest.raises(ValueError, match="allowlisted fields: headers"):
            wrapped(body=f"token={secret_ref}")
    finally:
        reset_runtime_context(tokens)


def test_secret_ref_wrapper_blocks_disallowed_tools():
    wrapped = SecretRefResolvingTool(DummyWriteTool())
    secret_ref = _issue_ref_for_session("s1")
    tokens = set_runtime_context(
        "s1", "high_risk", trust_principal=_secret_principal()
    )
    try:
        with pytest.raises(ValueError, match="cannot receive secret references"):
            wrapped(body=f"token={secret_ref}")
    finally:
        reset_runtime_context(tokens)


def test_secret_ref_wrapper_blocks_mcp_refs_without_explicit_egress_allowlist():
    wrapped = SecretRefResolvingTool(DummyUnboundMcpTool())
    secret_ref = _issue_ref_for_session("s1")
    tokens = set_runtime_context(
        "s1", "high_risk", trust_principal=_secret_principal()
    )
    try:
        with pytest.raises(ValueError, match="credential egress allowlist"):
            wrapped(headers={"Authorization": f"Bearer {secret_ref}"})
    finally:
        reset_runtime_context(tokens)


def test_secret_ref_wrapper_blocks_destination_hosts_outside_credential_egress_allowlist():
    wrapped = SecretRefResolvingTool(DummyHeaderTool())
    secret_ref = _issue_ref_for_session("s1")
    tokens = set_runtime_context(
        "s1", "high_risk", trust_principal=_secret_principal()
    )
    try:
        with pytest.raises(ValueError, match="non-allowlisted destination host"):
            wrapped(
                url="https://evil.example/v1",
                headers={"Authorization": f"Bearer {secret_ref}"},
            )
    finally:
        reset_runtime_context(tokens)


def test_secret_ref_wrapper_rejects_other_session_refs():
    wrapped = SecretRefResolvingTool(DummyHeaderTool())
    secret_ref = _issue_ref_for_session("s1")
    tokens = set_runtime_context(
        "s2", "high_risk", trust_principal=_secret_principal("s2")
    )
    try:
        with pytest.raises(ValueError, match="another session"):
            wrapped(
                url="https://api.example.com/v1",
                headers={"Authorization": f"Bearer {secret_ref}"},
            )
    finally:
        reset_runtime_context(tokens)


def test_secret_ref_wrapper_rejects_expired_refs():
    wrapped = SecretRefResolvingTool(DummyHeaderTool())
    with patch("src.vault.refs.time.time", return_value=1_000.0):
        secret_ref = _issue_ref_for_session("s1")

    tokens = set_runtime_context(
        "s1", "high_risk", trust_principal=_secret_principal()
    )
    try:
        with patch("src.vault.refs.time.time", return_value=1_000.0 + 3_601):
            with pytest.raises(ValueError, match="expired"):
                wrapped(
                    url="https://api.example.com/v1",
                    headers={"Authorization": f"Bearer {secret_ref}"},
                )
    finally:
        reset_runtime_context(tokens)


def test_secret_ref_wrapper_enforces_tool_field_and_destination_scope():
    tool = DummyHeaderTool()
    tool.seraph_source_context["credential_egress_policy"]["allowed_hosts"].append("other.example.com")
    wrapped = SecretRefResolvingTool(tool)
    secret_ref = issue_secret_ref(
        "s1",
        "super-secret-value",
        tool_name="mcp_http_request",
        field_name="headers",
        destination_host="api.example.com",
        destination_scheme="https",
        destination_port=443,
        purpose="tool_credential_injection",
    )
    tokens = set_runtime_context(
        "s1", "high_risk", trust_principal=_secret_principal()
    )
    try:
        assert wrapped(
            url="https://api.example.com/v1",
            headers={"Authorization": f"Bearer {secret_ref}"},
        )["status"] == "ok"
        with pytest.raises(ValueError, match="destination host scope mismatch"):
            wrapped(
                url="https://other.example.com/v1",
                headers={"Authorization": f"Bearer {secret_ref}"},
            )
    finally:
        reset_runtime_context(tokens)


def test_secret_ref_wrapper_enforces_one_time_refs():
    wrapped = SecretRefResolvingTool(DummyHeaderTool())
    secret_ref = issue_secret_ref(
        "s1",
        "super-secret-value",
        tool_name="mcp_http_request",
        field_name="headers",
        destination_host="api.example.com",
        destination_scheme="https",
        destination_port=443,
        purpose="tool_credential_injection",
        one_time=True,
    )
    tokens = set_runtime_context(
        "s1", "high_risk", trust_principal=_secret_principal()
    )
    try:
        assert wrapped(
            url="https://api.example.com/v1",
            headers={"Authorization": f"Bearer {secret_ref}"},
        )["status"] == "ok"
        with pytest.raises(ValueError, match="already used"):
            wrapped(
                url="https://api.example.com/v1",
                headers={"Authorization": f"Bearer {secret_ref}"},
            )
    finally:
        reset_runtime_context(tokens)
