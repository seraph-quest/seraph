"""Focused M7 Gmail source and durable-control tests.

The provider boundary is always intercepted with an HTTPX transport.  These
tests still exercise the real adapter lifecycle and the canonical durable job
repository/artifact path.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

from src.api import mail as mail_api
from src.api.mail import ConnectionCreate, ModelConsent
from src.auth.service import AuthenticatedOperator
from src.db.models import Goal, GoogleServiceConnection, MailMessageBinding, MailReadConsent, Session, WorkflowRunState
from src.db.engine import _ensure_mail_columns
from src.integrations.gmail_controls import (
    GmailControlError,
    MailSourceLease,
    MailSourceRequest,
    inspect_mail_source_artifact_recovery,
    run_mail_source_control,
)
from src.integrations import gmail_controls
from src.integrations.gmail_read import (
    GMAIL_READONLY_SCOPE,
    GmailReadError,
    GoogleGmailReadonlyAdapter,
)
from config.settings import settings as config_settings
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.vault import encrypt


def _connection() -> GoogleServiceConnection:
    return GoogleServiceConnection(
        owner_principal_id="principal-1",
        owner_session_id="session-1",
        service="gmail_readonly",
        state="active",
        declared_scopes_json=json.dumps([GMAIL_READONLY_SCOPE]),
        vault_secret_key="gmail-test-secret",
    )


def _full_message() -> dict[str, object]:
    return {
        "id": "m1",
        "threadId": "t1",
        "historyId": "h1",
        "internalDate": "1720000000000",
        "snippet": "preview",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Hello"},
                {"name": "Date", "value": "Mon, 1 Jul 2024 00:00:00 +0000"},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "text/plain",
                    "body": {"data": base64.urlsafe_b64encode(b"Hello world").decode()},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "private.pdf",
                    "body": {"attachmentId": "attachment-1"},
                },
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(b'<a href="https://example.invalid">link</a>').decode()},
                },
            ],
        },
    }


def _test_operator() -> AuthenticatedOperator:
    now = datetime.now(timezone.utc)
    return AuthenticatedOperator(
        session_id="test-auth-bypass",
        principal=TrustPrincipal(
            principal_id="operator:test-bypass",
            principal_type=PrincipalType.OPERATOR,
            authenticated=True,
            grants=(AuthorityGrant.INGRESS, AuthorityGrant.CAPABILITY_EXECUTE),
            session_id="test-auth-bypass",
            operator_session_id="test-auth-bypass",
        ),
        idle_expires_at=now,
        absolute_expires_at=now,
    )


def _request_with_body(body: bytes, operator: AuthenticatedOperator) -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/capabilities/mail/test",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1234),
        },
        receive,
    )
    request.state.operator = operator
    return request


@pytest_asyncio.fixture
async def durable_job_db(monkeypatch):
    """Small canonical durable-job schema for fast control-root tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[Session.__table__, WorkflowRunState.__table__],
            )
        )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr("src.workflows.job_runtime.get_session", get_session)
    yield
    await engine.dispose()


@pytest.mark.asyncio
async def test_scan_adapter_uses_fixed_readonly_urls_and_settles_transport():
    seen: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access-token", "scope": GMAIL_READONLY_SCOPE})
        if request.url.path.endswith("/labels"):
            return httpx.Response(200, json={"labels": [{"id": "INBOX", "name": "Inbox", "type": "system"}]})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        if request.url.path.endswith("/messages/m1"):
            return httpx.Response(200, json=_full_message())
        return httpx.Response(404, json={})

    async def vault_get(_key: str) -> str:
        return json.dumps({"client_id": "client", "refresh_token": "refresh"})

    adapter = GoogleGmailReadonlyAdapter(
        _connection(),
        owner_principal_id="principal-1",
        transport=httpx.MockTransport(handler),
        resolver=lambda _host, _port: ["8.8.8.8"],
    )
    with patch("src.integrations.gmail_read.vault_repository.get", new=vault_get):
        labels = await adapter.list_labels()
        page = await adapter.list_message_ids(
            ["INBOX"],
            received_after=datetime(2024, 6, 30, tzinfo=timezone.utc),
            max_messages=1,
        )
        metadata = await adapter.get_message_metadata("m1")
        body = await adapter.get_message_full("m1")

    assert labels[0].provider_id == "INBOX"
    assert page.provider_ids == ("m1",)
    assert metadata.subject == "Hello"
    assert body.body == "Hello world"
    assert adapter.observed_provider_scopes == (GMAIL_READONLY_SCOPE,)
    assert "attachment-1" not in body.body
    assert all(method in {"GET", "POST"} for method, _url in seen)
    assert all(url.startswith(("https://gmail.googleapis.com/gmail/v1/users/me/", "https://oauth2.googleapis.com/token")) for _method, url in seen)
    assert not any("attachments" in url or "history" in url for _method, url in seen)
    assert adapter.transport_quiescence() == {
        "status": "verified",
        "active_operations": 0,
        "unsettled_operations": 0,
        "requests_started": 5,
        "requests_settled": 5,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_limit,provider_count", [(1, 2), (10, 11)])
async def test_scan_adapter_rejects_provider_over_returned_message_limit(
    requested_limit: int,
    provider_count: int,
):
    metadata_requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access-token", "scope": GMAIL_READONLY_SCOPE})
        if request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={"messages": [{"id": f"m{index}"} for index in range(provider_count)]},
            )
        if "/messages/" in request.url.path:
            metadata_requests.append(str(request.url))
        return httpx.Response(200, json={"messages": []})

    async def vault_get(_key: str) -> str:
        return json.dumps({"client_id": "client", "refresh_token": "refresh"})

    adapter = GoogleGmailReadonlyAdapter(
        _connection(),
        owner_principal_id="principal-1",
        transport=httpx.MockTransport(handler),
        resolver=lambda _host, _port: ["8.8.8.8"],
    )
    with patch("src.integrations.gmail_read.vault_repository.get", new=vault_get):
        with pytest.raises(GmailReadError) as error:
            await adapter.list_message_ids(
                ["INBOX"],
                received_after=datetime(2024, 6, 30, tzinfo=timezone.utc),
                max_messages=requested_limit,
            )

    assert error.value.code == "mail_provider_schema_invalid"
    assert error.value.status_code == 502
    assert metadata_requests == []


@pytest.mark.asyncio
async def test_provider_scope_absence_stays_unverified_and_broader_scope_is_denied():
    async def vault_get(_key: str) -> str:
        return json.dumps({"client_id": "client", "refresh_token": "refresh"})

    async def absent_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access-token"})
        return httpx.Response(200, json={"labels": []})

    absent = GoogleGmailReadonlyAdapter(
        _connection(),
        owner_principal_id="principal-1",
        transport=httpx.MockTransport(absent_handler),
        resolver=lambda _host, _port: ["8.8.8.8"],
    )
    with patch("src.integrations.gmail_read.vault_repository.get", new=vault_get):
        await absent.list_labels()
    assert absent.observed_provider_scopes is None

    async def broader_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-token",
                    "scope": f"{GMAIL_READONLY_SCOPE} https://www.googleapis.com/auth/gmail.send",
                },
            )
        return httpx.Response(200, json={"labels": []})

    broader = GoogleGmailReadonlyAdapter(
        _connection(),
        owner_principal_id="principal-1",
        transport=httpx.MockTransport(broader_handler),
        resolver=lambda _host, _port: ["8.8.8.8"],
    )
    with patch("src.integrations.gmail_read.vault_repository.get", new=vault_get):
        with pytest.raises(GmailReadError) as error:
            await broader.list_labels()
    assert error.value.code == "mail_scope_broader_than_declared"


@pytest.mark.asyncio
async def test_persisted_scope_omission_clears_prior_positive_evidence(async_db, monkeypatch):
    monkeypatch.setattr(config_settings, "deployment_environment", "test")
    monkeypatch.setattr(config_settings, "operator_auth_allow_unauthenticated_tests", True)
    monkeypatch.setattr(mail_api, "get_session", async_db)
    owner = mail_api._owner(_test_operator())
    connection = GoogleServiceConnection(
        connection_id="scope-clear-connection",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        service="gmail_readonly",
        state="active",
        revision=4,
        provider_scopes_json=json.dumps([GMAIL_READONLY_SCOPE]),
        scope_status="scope_verified",
        vault_secret_key="scope-clear-vault",
    )
    async with async_db() as db:
        db.add(connection)

    persisted = await mail_api._persist_observed_provider_scope(
        owner,
        connection_id=connection.connection_id,
        expected_revision=4,
        observed_scopes=None,
    )
    assert persisted.revision == 5
    assert persisted.provider_scopes_json == "[]"
    assert persisted.scope_status == "scope_unverified"
    async with async_db() as db:
        stored = await db.get(GoogleServiceConnection, connection.connection_id)
        assert stored is not None
        assert stored.revision == 5
        assert stored.provider_scopes_json == "[]"
        assert stored.scope_status == "scope_unverified"


@pytest.mark.asyncio
async def test_source_revoke_after_token_contact_blocks_next_provider_read():
    seen: list[str] = []
    revoked = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal revoked
        seen.append(str(request.url))
        if request.url.host == "oauth2.googleapis.com":
            revoked = True
            return httpx.Response(200, json={"access_token": "access-token", "scope": GMAIL_READONLY_SCOPE})
        return httpx.Response(200, json={"labels": [{"id": "INBOX", "name": "Inbox", "type": "system"}]})

    async def vault_get(_key: str) -> str:
        return json.dumps({"client_id": "client", "refresh_token": "refresh"})

    async def authority_check() -> None:
        if revoked:
            raise GmailReadError("mail_consent_revision_stale", "The Mail consent changed", status_code=409)

    adapter = GoogleGmailReadonlyAdapter(
        _connection(),
        owner_principal_id="principal-1",
        transport=httpx.MockTransport(handler),
        resolver=lambda _host, _port: ["8.8.8.8"],
        authority_check=authority_check,
    )
    with patch("src.integrations.gmail_read.vault_repository.get", new=vault_get):
        with pytest.raises(GmailReadError) as error:
            await adapter.list_labels()
    assert error.value.code == "mail_consent_revision_stale"
    assert seen == ["https://oauth2.googleapis.com/token"]


@pytest.mark.asyncio
async def test_mail_source_control_replays_verified_artifact_and_blocks_unknown(durable_job_db, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config_settings, "workspace_dir", str(tmp_path))
    request = MailSourceRequest(
        operation="mail_test_control",
        owner_principal_id="principal-1",
        owner_session_id="session-1",
        connection_id="connection-1",
        connection_revision=1,
        request_uuid="request-1",
        request_digest="sha256:request-digest",
    )
    calls = 0

    async def execute(_lease):
        nonlocal calls
        calls += 1
        return {"status": "ok", "memory_status": "no_learning", "body": "private body", "secret": "refresh-token"}

    first = await run_mail_source_control(request, execute)
    second = await run_mail_source_control(request, execute)
    assert first == second == {"status": "ok", "memory_status": "no_learning", "body": "private body", "secret": "refresh-token"}
    assert calls == 1
    job = await gmail_controls.durable_job_repository.get_job(request.job_id)
    assert job is not None
    assert "private body" not in json.dumps(job)
    assert "refresh-token" not in json.dumps(job)

    conflicting_request = MailSourceRequest(
        operation=request.operation,
        owner_principal_id=request.owner_principal_id,
        owner_session_id=request.owner_session_id,
        connection_id=request.connection_id,
        connection_revision=request.connection_revision,
        request_uuid=request.request_uuid,
        request_digest="sha256:different-request",
    )
    with pytest.raises(GmailControlError) as conflict_error:
        await run_mail_source_control(conflicting_request, execute)
    assert conflict_error.value.code == "mail_idempotency_conflict"

    unknown_request = MailSourceRequest(
        operation="mail_test_unknown",
        owner_principal_id="principal-1",
        owner_session_id="session-1",
        connection_id="connection-1",
        connection_revision=1,
        request_uuid="request-unknown",
        request_digest="sha256:unknown-digest",
    )

    async def fail(_lease):
        raise GmailControlError("mail_provider_unavailable", "provider unavailable", status_code=503, recovery_action="retry")

    with pytest.raises(GmailControlError) as first_error:
        await run_mail_source_control(unknown_request, fail)
    assert first_error.value.code == "mail_read_reconciliation_required"
    with pytest.raises(GmailControlError) as replay_error:
        await run_mail_source_control(unknown_request, execute)
    assert replay_error.value.code == "mail_read_reconciliation_required"
    assert replay_error.value.recovery_action == "reconcile_existing_read"


def test_model_consent_requires_canonical_acknowledged_field_name():
    accepted = ModelConsent.model_validate(
        {
            "expected_revision": 1,
            "acknowledged_payload_fields": ["subject", "plainbody", "replyintent"],
            "allow": True,
        }
    )
    assert accepted.acknowledged_payload_fields == ["subject", "plainbody", "replyintent"]
    with pytest.raises(Exception):
        ModelConsent.model_validate(
            {
                "expected_revision": 1,
                "acknowledge_payload_fields": ["subject", "plainbody", "replyintent"],
                "allow": True,
            }
        )


def test_source_consent_rejects_duplicate_ordered_fields_and_digest_preserves_order():
    base = {
        "schema_version": 1,
        "connection_id": "connection",
        "expected_connection_revision": 1,
        "goal_id": "goal",
        "expected_goal_revision": 1,
        "label_ids": ["label"],
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "acknowledge_source_read": True,
        "idempotency_key": "duplicate-source-fields",
    }
    with pytest.raises(Exception):
        mail_api.ConsentCreate.model_validate(
            {**base, "allowed_body_fields": ["subject", "subject"]}
        )
    row = MailReadConsent(
        consent_id="ordered-digest-consent",
        owner_principal_id="principal",
        owner_session_id="session",
        connection_id="connection",
        goal_id="goal",
        label_ids_json='["label"]',
        source_digest="sha256:source",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    first = mail_api._model_consent_digest(row, allow=True, fields=["subject", "plainbody"])
    second = mail_api._model_consent_digest(row, allow=True, fields=["plainbody", "subject"])
    assert first != second
    assert ModelConsent.model_validate(
        {
            "expected_revision": 1,
            "acknowledged_payload_fields": ["subject"],
            "allow": True,
        }
    ).acknowledged_payload_fields == ["subject"]
    with pytest.raises(Exception):
        ModelConsent.model_validate(
            {
                "expected_revision": 1,
                "acknowledged_payload_fields": ["subject", "subject"],
                "allow": True,
            }
        )
    with pytest.raises(Exception):
        ModelConsent.model_validate(
            {
                "expected_revision": 1,
                "acknowledged_payload_fields": ["snippet"],
                "allow": True,
            }
        )


@pytest.mark.asyncio
async def test_source_consent_requires_explicit_ack_before_any_db_grant(async_db, monkeypatch):
    monkeypatch.setattr(config_settings, "deployment_environment", "test")
    monkeypatch.setattr(config_settings, "operator_auth_allow_unauthenticated_tests", True)
    monkeypatch.setattr(mail_api, "get_session", async_db)
    base = {
        "schema_version": 1,
        "connection_id": "missing-connection",
        "expected_connection_revision": 1,
        "goal_id": "missing-goal",
        "expected_goal_revision": 1,
        "label_ids": ["label-1"],
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "allowed_body_fields": ["subject"],
        "idempotency_key": "explicit-ack-required",
    }
    for payload in (
        base,
        {**base, "acknowledge_source_read": False, "idempotency_key": "false-ack-rejected"},
    ):
        with pytest.raises(HTTPException) as error:
            await mail_api.create_consent(
                _request_with_body(json.dumps(payload).encode(), _test_operator())
            )
        assert error.value.status_code == 422
    async with async_db() as db:
        rows = (await db.execute(select(MailReadConsent))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_authenticated_mail_metadata_get_is_offline_and_owner_bound(async_db, monkeypatch):
    monkeypatch.setattr(config_settings, "deployment_environment", "test")
    monkeypatch.setattr(config_settings, "operator_auth_allow_unauthenticated_tests", True)
    monkeypatch.setattr(mail_api, "get_session", async_db)
    operator = _test_operator()
    connection = GoogleServiceConnection(
        connection_id="connection-private",
        owner_principal_id="operator:test-bypass",
        owner_session_id="test-auth-bypass",
        service="gmail_readonly",
        state="active",
        vault_secret_key="mail-private-key",
        declared_scopes_json=json.dumps([GMAIL_READONLY_SCOPE]),
    )
    async with async_db() as db:
        db.add(connection)

    response = await mail_api.list_labels(_request_with_body(b"", operator), "connection-private")
    assert response["provider_contact"] is False
    assert response["labels"] == []
    with pytest.raises(HTTPException) as foreign:
        await mail_api.list_labels(_request_with_body(b"", operator), "missing-for-owner")
    assert foreign.value.status_code == 404


@pytest.mark.asyncio
async def test_mail_request_body_limit_is_generic_and_does_not_echo_secret(monkeypatch):
    secret = "refresh-token-that-must-never-appear-in-errors"
    body = json.dumps({"refresh_token": secret, "padding": "x" * 20_000}).encode()
    with pytest.raises(HTTPException) as error:
        await mail_api._json_body(_request_with_body(body, _test_operator()), ConnectionCreate)
    assert error.value.status_code == 413
    assert secret not in repr(error.value.detail)


@pytest.mark.asyncio
async def test_cancelled_mail_control_stops_before_second_contact_and_never_publishes(monkeypatch, tmp_path: Path):
    class FakeRepository:
        def __init__(self):
            self.assertions = 0
            self.unknown_recorded = False
            self.artifacts = 0
            self.cancelled = False
            self.job = {
                "job_id": "",
                "status": "running",
                "revision": 4,
                "deadline_at": "2099-01-01T00:00:00+00:00",
                "effects": [],
                "artifacts": [],
            }

        async def get_by_idempotency_binding(self, **_kwargs):
            return None

        async def admit_job(self, spec):
            self.job["job_id"] = spec.identity.job_id
            return {"status": "accepted", "revision": 1, "receipt": {}}

        async def queue_job(self, *_args, **_kwargs):
            return {"revision": 2}

        async def claim_job(self, *_args, **_kwargs):
            return {
                "revision": 3,
                "deadline_at": "2099-01-01T00:00:00+00:00",
                "lease": {"fencing_token": 1},
            }

        async def assert_active_lease(self, *_args, **_kwargs):
            self.assertions += 1
            if self.cancelled and self.assertions >= 4:
                raise gmail_controls.DurableJobLeaseError("durable job is not running")
            return self.job

        async def record_effect(self, *_args, **kwargs):
            if kwargs.get("status") == "unknown":
                self.unknown_recorded = True
            self.job["revision"] += 1
            return {"revision": self.job["revision"]}

        async def record_artifact(self, *_args, **_kwargs):
            self.artifacts += 1
            raise AssertionError("cancelled control must not publish an artifact")

        async def record_readback(self, *_args, **_kwargs):
            raise AssertionError("cancelled control must not publish readback")

        async def transition_job(self, *_args, **_kwargs):
            self.job["status"] = "unknown_external_effect"
            self.job["revision"] += 1
            return self.job

        async def get_job(self, *_args, **_kwargs):
            return self.job

    fake = FakeRepository()
    monkeypatch.setattr(gmail_controls, "durable_job_repository", fake)
    monkeypatch.setattr(config_settings, "workspace_dir", str(tmp_path))
    request = MailSourceRequest(
        operation="mail_cancelled_scan",
        owner_principal_id="operator:single",
        owner_session_id="session-1",
        connection_id="connection-1",
        connection_revision=1,
        request_uuid="cancel-request",
        request_digest="sha256:cancel-request",
    )
    provider_contacts = 0

    async def execute(lease: MailSourceLease):
        nonlocal provider_contacts
        await gmail_controls.assert_mail_source_lease(lease)
        provider_contacts += 1
        fake.cancelled = True
        await gmail_controls.assert_mail_source_lease(lease)
        provider_contacts += 1
        return {"should_not": "publish"}

    with pytest.raises(GmailControlError) as error:
        await run_mail_source_control(request, execute)
    assert error.value.code == "mail_read_reconciliation_required"
    assert provider_contacts == 1
    assert fake.unknown_recorded is True
    assert fake.artifacts == 0


@pytest.mark.asyncio
async def test_real_durable_cancel_during_provider_callback_blocks_second_contact(durable_job_db, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config_settings, "workspace_dir", str(tmp_path))
    request = MailSourceRequest(
        operation="mail_real_cancel_scan",
        owner_principal_id="principal-real",
        owner_session_id="session-real",
        connection_id="connection-real",
        connection_revision=1,
        request_uuid="real-cancel-request",
        request_digest="sha256:real-cancel-request",
    )
    lease_holder: dict[str, MailSourceLease] = {}
    seen: list[str] = []

    async def vault_get(_key: str) -> str:
        return json.dumps({"client_id": "client", "refresh_token": "refresh"})

    async def handler(provider_request: httpx.Request) -> httpx.Response:
        seen.append(str(provider_request.url))
        if provider_request.url.host == "oauth2.googleapis.com":
            lease = lease_holder["lease"]
            await gmail_controls.durable_job_repository.cancel_job(
                lease.job_id,
                owner=lease.owner,
                fencing_token=lease.fencing_token,
                expected_revision=lease.revision,
                reason="test_revoke_during_provider_callback",
            )
            return httpx.Response(200, json={"access_token": "access-token", "scope": GMAIL_READONLY_SCOPE})
        return httpx.Response(200, json={"labels": [{"id": "INBOX", "name": "Inbox", "type": "system"}]})

    connection = _connection()
    connection.owner_principal_id = request.owner_principal_id
    connection.owner_session_id = request.owner_session_id

    async def execute(lease: MailSourceLease):
        lease_holder["lease"] = lease

        async def authority_check() -> None:
            await gmail_controls.assert_mail_source_lease(lease)

        adapter = GoogleGmailReadonlyAdapter(
            connection,
            owner_principal_id=request.owner_principal_id,
            transport=httpx.MockTransport(handler),
            resolver=lambda _host, _port: ["8.8.8.8"],
            authority_check=authority_check,
        )
        with patch("src.integrations.gmail_read.vault_repository.get", new=vault_get):
            await adapter.list_labels()
        return {"must_not": "publish"}

    with pytest.raises(GmailControlError) as error:
        await run_mail_source_control(request, execute)
    assert error.value.code == "mail_read_reconciliation_required"
    job = await gmail_controls.durable_job_repository.get_job(request.job_id)
    assert job is not None
    # The repository deliberately maps cancellation of a running job with an
    # unresolved effect intent to the durable unknown state.  That preserves
    # the no-replay fence while still recording the operator cancellation.
    assert job["status"] == "unknown_external_effect"
    assert job["failure_reason"] == "test_revoke_during_provider_callback"
    assert seen == ["https://oauth2.googleapis.com/token"]
    assert not job["artifacts"]
    assert not any(
        isinstance(effect, dict) and effect.get("receipt_kind") == "readback"
        for effect in job["effects"]
    )


@pytest.mark.asyncio
async def test_python_task_cancel_after_intent_settles_unknown_with_original_cancel(durable_job_db, monkeypatch, tmp_path: Path):
    """A real task cancellation must not leave a running lease after intent."""
    monkeypatch.setattr(config_settings, "workspace_dir", str(tmp_path))
    request = MailSourceRequest(
        operation="mail_task_cancel_scan",
        owner_principal_id="principal-task-cancel",
        owner_session_id="session-task-cancel",
        connection_id="connection-task-cancel",
        connection_revision=1,
        request_uuid="task-cancel-request",
        request_digest="sha256:task-cancel-request",
    )
    entered = asyncio.Event()

    async def execute(_lease: MailSourceLease):
        entered.set()
        await asyncio.Future()

    running = asyncio.create_task(run_mail_source_control(request, execute))
    await asyncio.wait_for(entered.wait(), timeout=5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    job = await gmail_controls.durable_job_repository.get_job(request.job_id)
    assert job is not None
    assert job["status"] == "unknown_external_effect"
    assert job["lease"]["owner"] is None
    assert job["lease"]["expires_at"] is None
    assert any(
        isinstance(effect, dict)
        and effect.get("status") == "unknown"
        and effect.get("adapter_idempotency_key") == request.request_uuid
        for effect in job["effects"]
    )


@pytest.mark.asyncio
async def test_source_scope_revision_and_goal_limits_fail_closed_before_provider(async_db, monkeypatch):
    monkeypatch.setattr(config_settings, "deployment_environment", "test")
    monkeypatch.setattr(config_settings, "operator_auth_allow_unauthenticated_tests", True)
    monkeypatch.setattr(mail_api, "get_session", async_db)
    owner = mail_api._owner(_test_operator())
    now = datetime.now(timezone.utc)
    connection = GoogleServiceConnection(
        connection_id="scope-connection",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        service="gmail_readonly",
        state="active",
        revision=2,
        vault_secret_key="scope-secret",
        declared_scopes_json=json.dumps([GMAIL_READONLY_SCOPE]),
    )
    goal = Goal(
        id="scope-goal",
        title="Mail scope",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        revision=3,
        status="active",
    )
    consent = MailReadConsent(
        consent_id="scope-consent",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        connection_id=connection.connection_id,
        connection_revision=2,
        goal_id=goal.id,
        goal_revision=3,
        label_ids_json='["label-1"]',
        max_messages=1,
        source_revision=4,
        source_digest="sha256:source",
        expires_at=now.replace(microsecond=0),
        state="active",
    )
    consent.expires_at = now + timedelta(hours=1)
    async with async_db() as db:
        db.add(connection)
        db.add(goal)
        db.add(consent)
    async with async_db() as db:
        current = await db.get(MailReadConsent, consent.consent_id)
        assert current is not None
        with pytest.raises(GmailReadError) as stale:
            await mail_api._validate_source_scope(
                db,
                owner,
                connection=connection,
                consent=current,
                expected_source_revision=3,
                label_ids=["label-1"],
                max_messages=2,
            )
    assert stale.value.code == "mail_consent_revision_stale"


@pytest.mark.asyncio
async def test_model_consent_update_preserves_independent_source_revision(async_db, monkeypatch):
    monkeypatch.setattr(config_settings, "deployment_environment", "test")
    monkeypatch.setattr(config_settings, "operator_auth_allow_unauthenticated_tests", True)
    monkeypatch.setattr(mail_api, "get_session", async_db)
    owner = mail_api._owner(_test_operator())
    consent = MailReadConsent(
        consent_id="model-consent-row",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        connection_id="model-connection",
        connection_revision=1,
        goal_id="model-goal",
        goal_revision=1,
        label_ids_json='["label-1"]',
        source_revision=7,
        source_digest="sha256:source-seven",
        model_revision=2,
        allowed_body_fields_json='["subject","plainbody"]',
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        state="active",
    )
    async with async_db() as db:
        db.add(consent)
    body = json.dumps(
        {
            "expected_revision": 1,
            "acknowledged_payload_fields": ["subject", "plainbody"],
            "allow": True,
        }
    ).encode()
    result = await mail_api.set_model_consent(
        _request_with_body(body, _test_operator()),
        consent.consent_id,
    )
    assert result["consent"]["source_revision"] == 7
    assert result["consent"]["model_revision"] == 3
    async with async_db() as db:
        stored = await db.get(MailReadConsent, consent.consent_id)
        assert stored is not None
        assert stored.source_revision == 7
        assert stored.model_egress_allowed is True
        assert stored.model_digest == mail_api._model_consent_digest(
            stored,
            allow=True,
            fields=["subject", "plainbody"],
        )
    mismatch_body = json.dumps(
        {
            "expected_revision": result["consent"]["revision"],
            "acknowledged_payload_fields": ["subject"],
            "allow": True,
        }
    ).encode()
    with pytest.raises(HTTPException) as mismatch:
        await mail_api.set_model_consent(
            _request_with_body(mismatch_body, _test_operator()),
            consent.consent_id,
        )
    assert mismatch.value.status_code == 409
    assert mismatch.value.detail["code"] == "mail_model_consent_fields_mismatch"
    async with async_db() as db:
        unchanged = await db.get(MailReadConsent, consent.consent_id)
        assert unchanged is not None
        assert unchanged.source_revision == 7
        assert unchanged.model_revision == 3


@pytest.mark.asyncio
async def test_revoke_mutations_replay_exact_key_and_redact_provider_identity(async_db, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config_settings, "deployment_environment", "test")
    monkeypatch.setattr(config_settings, "operator_auth_allow_unauthenticated_tests", True)
    monkeypatch.setattr(mail_api, "get_session", async_db)
    monkeypatch.setattr(config_settings, "workspace_dir", str(tmp_path))
    owner = mail_api._owner(_test_operator())
    connection = GoogleServiceConnection(
        connection_id="revoke-connection",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        service="gmail_readonly",
        state="active",
        revision=2,
        vault_secret_key="revoke-vault-key",
    )
    consent = MailReadConsent(
        consent_id="revoke-consent",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        connection_id=connection.connection_id,
        connection_revision=2,
        goal_id="revoke-goal",
        goal_revision=1,
        label_ids_json='["label-1"]',
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    binding = MailMessageBinding(
        message_binding_id="revoke-binding",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        connection_id=connection.connection_id,
        provider_message_id_ciphertext=encrypt("provider-message"),
        provider_thread_id_ciphertext=encrypt("provider-thread"),
        message_key="message-key",
        thread_key="thread-key",
        message_revision="sha256:message",
    )
    async with async_db() as db:
        db.add(connection)
        db.add(consent)
        db.add(binding)

    first = await mail_api.revoke_connection(
        _request_with_body(
            json.dumps({"expected_revision": 2, "idempotency_key": "revoke-key"}).encode(),
            _test_operator(),
        ),
        connection.connection_id,
    )
    second = await mail_api.revoke_connection(
        _request_with_body(
            json.dumps({"expected_revision": 2, "idempotency_key": "revoke-key"}).encode(),
            _test_operator(),
        ),
        connection.connection_id,
    )
    assert second == first
    with pytest.raises(HTTPException) as conflict:
        await mail_api.revoke_connection(
            _request_with_body(
                json.dumps({"expected_revision": 3, "idempotency_key": "revoke-key"}).encode(),
                _test_operator(),
            ),
            connection.connection_id,
        )
    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "mail_revoke_idempotency_conflict"
    async with async_db() as db:
        stored_connection = await db.get(GoogleServiceConnection, connection.connection_id)
        stored_binding = await db.get(MailMessageBinding, binding.message_binding_id)
        assert stored_connection is not None and stored_connection.state == "revoked"
        assert stored_connection.revoke_idempotency_key == "revoke-key"
        assert stored_binding is not None
        assert stored_binding.status == "redacted"
        assert stored_binding.provider_message_id_ciphertext == ""
        assert stored_binding.provider_thread_id_ciphertext == ""

    consent_two = MailReadConsent(
        consent_id="revoke-consent-two",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        connection_id="other-connection",
        connection_revision=1,
        goal_id="revoke-goal-two",
        goal_revision=1,
        label_ids_json='["label-1"]',
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    async with async_db() as db:
        db.add(consent_two)

    consent_first = await mail_api.revoke_consent(
        _request_with_body(
            json.dumps({"expected_revision": 1, "idempotency_key": "consent-revoke-key", "reason": "remove"}).encode(),
            _test_operator(),
        ),
        consent_two.consent_id,
    )
    consent_second = await mail_api.revoke_consent(
        _request_with_body(
            json.dumps({"expected_revision": 1, "idempotency_key": "consent-revoke-key", "reason": "remove"}).encode(),
            _test_operator(),
        ),
        consent_two.consent_id,
    )
    assert consent_second == consent_first
    with pytest.raises(HTTPException) as consent_conflict:
        await mail_api.revoke_consent(
            _request_with_body(
                json.dumps({"expected_revision": 1, "idempotency_key": "consent-revoke-key", "reason": "different"}).encode(),
                _test_operator(),
            ),
            consent_two.consent_id,
        )
    assert consent_conflict.value.detail["code"] == "mail_revoke_idempotency_conflict"


@pytest.mark.asyncio
async def test_message_binding_scope_mismatch_blocks_body_contact(async_db, monkeypatch):
    monkeypatch.setattr(config_settings, "deployment_environment", "test")
    monkeypatch.setattr(config_settings, "operator_auth_allow_unauthenticated_tests", True)
    monkeypatch.setattr(mail_api, "get_session", async_db)
    owner = mail_api._owner(_test_operator())
    connection = GoogleServiceConnection(
        connection_id="binding-scope-connection",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        service="gmail_readonly",
        state="active",
        revision=1,
        vault_secret_key="binding-scope-vault",
    )
    goal = Goal(
        id="binding-scope-goal",
        title="binding scope",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        revision=1,
        status="active",
    )
    consent = MailReadConsent(
        consent_id="binding-scope-consent-b",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        connection_id=connection.connection_id,
        connection_revision=1,
        goal_id=goal.id,
        goal_revision=1,
        label_ids_json='["label-b"]',
        source_revision=1,
        source_digest="sha256:scope-b",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    binding = MailMessageBinding(
        message_binding_id="binding-scope-row",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        connection_id=connection.connection_id,
        connection_revision=1,
        source_consent_id="binding-scope-consent-a",
        source_consent_revision=1,
        source_label_scope_digest="sha256:scope-a",
        provider_message_id_ciphertext=encrypt("provider-message"),
        provider_thread_id_ciphertext=encrypt("provider-thread"),
        message_key="message-key",
        thread_key="thread-key",
        message_revision="sha256:message",
        status="present",
    )
    async with async_db() as db:
        db.add(connection)
        db.add(goal)
        db.add(consent)
        db.add(binding)
    body = {
        "connection_id": connection.connection_id,
        "expected_connection_revision": 1,
        "mail_consent_id": consent.consent_id,
        "expected_source_consent_revision": 1,
        "message_binding_id": binding.message_binding_id,
        "expected_message_revision": "sha256:message",
        "acknowledge_selected_body_read": True,
        "request_uuid": "scope-read-request",
    }
    with pytest.raises(HTTPException) as error:
        await mail_api.read_message(
            _request_with_body(json.dumps(body).encode(), _test_operator()),
            binding.message_binding_id,
        )
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "mail_message_scope_stale"
    async with async_db() as db:
        current_binding = await db.get(MailMessageBinding, binding.message_binding_id)
        assert current_binding is not None
        current_binding.source_consent_id = consent.consent_id
        current_binding.source_label_scope_digest = "sha256:scope-a"
    with pytest.raises(HTTPException) as changed_scope:
        await mail_api.read_message(
            _request_with_body(json.dumps(body).encode(), _test_operator()),
            binding.message_binding_id,
        )
    assert changed_scope.value.status_code == 409
    assert changed_scope.value.detail["code"] == "mail_message_scope_stale"
    async with async_db() as db:
        current_consent = await db.get(MailReadConsent, consent.consent_id)
        assert current_consent is not None
        current_consent.state = "revoked"
        current_consent.source_read_allowed = False
    with pytest.raises(HTTPException) as revoked:
        await mail_api.read_message(
            _request_with_body(json.dumps(body).encode(), _test_operator()),
            binding.message_binding_id,
        )
    assert revoked.value.status_code == 409
    assert revoked.value.detail["code"] == "mail_consent_unavailable"


@pytest.mark.asyncio
async def test_connection_delete_redacts_ciphertext_and_exact_terminal_artifact(async_db, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config_settings, "deployment_environment", "test")
    monkeypatch.setattr(config_settings, "operator_auth_allow_unauthenticated_tests", True)
    monkeypatch.setattr(mail_api, "get_session", async_db)
    monkeypatch.setattr(config_settings, "workspace_dir", str(tmp_path))
    owner = mail_api._owner(_test_operator())
    connection = GoogleServiceConnection(
        connection_id="delete-connection",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        service="gmail_readonly",
        state="active",
        revision=1,
        vault_secret_key="delete-vault",
    )
    job_id = "mail-delete-job"
    path, encrypted, sha = gmail_controls._write_artifact(job_id, {"private": "body"})
    receipt = {
        "artifact_id": "delete-artifact",
        "artifact_type": "mail_source_result",
        "file_path": path,
        "content_sha256": sha,
        "size_bytes": len(encrypted),
        "exists": True,
    }
    run = WorkflowRunState(
        run_identity=job_id,
        root_run_identity=job_id,
        workflow_name="mail_messages_scan",
        session_id=None,
        operator_session_id=owner.session_id,
        status="succeeded",
        job_kind="mail_messages_scan",
        owner_kind="user",
        owner_principal_id=owner.principal_id,
        arguments_json=json.dumps({"connection_id": connection.connection_id}),
        artifact_receipts_json=json.dumps([receipt]),
        effect_receipts_json="[]",
    )
    binding = MailMessageBinding(
        message_binding_id="delete-binding",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        connection_id=connection.connection_id,
        provider_message_id_ciphertext=encrypt("provider-message"),
        provider_thread_id_ciphertext=encrypt("provider-thread"),
        message_key="delete-message-key",
        thread_key="delete-thread-key",
        message_revision="sha256:delete-message",
    )
    async with async_db() as db:
        db.add(connection)
        db.add(run)
        db.add(binding)
    response = await mail_api.revoke_connection(
        _request_with_body(json.dumps({"expected_revision": 1, "idempotency_key": "delete-key"}).encode(), _test_operator()),
        connection.connection_id,
    )
    assert response["connection"]["state"] == "revoked"
    assert not (tmp_path / path).exists()
    async with async_db() as db:
        stored_binding = await db.get(MailMessageBinding, binding.message_binding_id)
        stored_run = (
            await db.execute(
                select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id)
            )
        ).scalar_one_or_none()
        assert stored_binding is not None and stored_binding.status == "redacted"
        assert stored_binding.provider_message_id_ciphertext == ""
        assert stored_run is not None
        tombstones = json.loads(stored_run.artifact_receipts_json)
        assert tombstones[0]["state"] == "redacted"
        assert tombstones[0]["exists"] is False


@pytest.mark.asyncio
async def test_connection_delete_retains_unknown_artifact_and_surfaces_reconciliation(async_db, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config_settings, "deployment_environment", "test")
    monkeypatch.setattr(config_settings, "operator_auth_allow_unauthenticated_tests", True)
    monkeypatch.setattr(mail_api, "get_session", async_db)
    monkeypatch.setattr(config_settings, "workspace_dir", str(tmp_path))
    owner = mail_api._owner(_test_operator())
    connection = GoogleServiceConnection(
        connection_id="unknown-delete-connection",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        service="gmail_readonly",
        state="active",
        revision=1,
        vault_secret_key="unknown-delete-vault",
    )
    job_id = "mail-unknown-delete-job"
    path, _encrypted, sha = gmail_controls._write_artifact(job_id, {"private": "uncertain"})
    run = WorkflowRunState(
        run_identity=job_id,
        root_run_identity=job_id,
        workflow_name="mail_messages_scan",
        session_id=None,
        operator_session_id=owner.session_id,
        status="unknown_external_effect",
        job_kind="mail_messages_scan",
        owner_kind="user",
        owner_principal_id=owner.principal_id,
        arguments_json=json.dumps({"connection_id": connection.connection_id}),
        artifact_receipts_json=json.dumps(
            [{"artifact_id": "unknown-artifact", "artifact_type": "mail_source_result", "file_path": path, "content_sha256": sha, "exists": True}]
        ),
        effect_receipts_json="[]",
    )
    async with async_db() as db:
        db.add(connection)
        db.add(run)
    with pytest.raises(HTTPException) as error:
        await mail_api.revoke_connection(
            _request_with_body(json.dumps({"expected_revision": 1, "idempotency_key": "unknown-delete-key"}).encode(), _test_operator()),
            connection.connection_id,
        )
    assert error.value.status_code == 503
    assert error.value.detail["recovery_action"] == "retry_cleanup"
    assert (tmp_path / path).exists()
    async with async_db() as db:
        stored = await db.get(GoogleServiceConnection, connection.connection_id)
        assert stored is not None and stored.state == "blocked_cleanup"


@pytest.mark.asyncio
async def test_connection_delete_cleanup_exception_is_bounded_and_same_key_retries(async_db, monkeypatch):
    monkeypatch.setattr(config_settings, "deployment_environment", "test")
    monkeypatch.setattr(config_settings, "operator_auth_allow_unauthenticated_tests", True)
    monkeypatch.setattr(mail_api, "get_session", async_db)
    owner = mail_api._owner(_test_operator())
    connection = GoogleServiceConnection(
        connection_id="cleanup-exception-connection",
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        service="gmail_readonly",
        state="active",
        revision=1,
        vault_secret_key="cleanup-exception-vault",
    )
    async with async_db() as db:
        db.add(connection)

    observed_states: list[str] = []

    async def delete_vault(_key: str) -> bool:
        async with async_db() as db:
            current = await db.get(GoogleServiceConnection, connection.connection_id)
            assert current is not None
            observed_states.append(current.state)
        return True

    async def fail_cleanup(*_args, **_kwargs):
        raise RuntimeError("injected cleanup failure")

    monkeypatch.setattr(mail_api.vault_repository, "delete", delete_vault)
    monkeypatch.setattr(mail_api, "_cleanup_connection_mail_artifacts", fail_cleanup)
    body = json.dumps({"expected_revision": 1, "idempotency_key": "cleanup-exception-key"}).encode()
    with pytest.raises(HTTPException) as first:
        await mail_api.revoke_connection(_request_with_body(body, _test_operator()), connection.connection_id)
    assert first.value.status_code == 503
    assert first.value.detail["recovery_action"] == "retry_cleanup"
    assert observed_states == ["blocked_cleanup"]
    async with async_db() as db:
        blocked = await db.get(GoogleServiceConnection, connection.connection_id)
        assert blocked is not None
        assert blocked.state == "blocked_cleanup"
        assert blocked.revoke_idempotency_key == "cleanup-exception-key"
        assert blocked.revoke_request_digest

    async def successful_cleanup(*_args, **_kwargs):
        return True

    monkeypatch.setattr(mail_api, "_cleanup_connection_mail_artifacts", successful_cleanup)
    retry = await mail_api.revoke_connection(_request_with_body(body, _test_operator()), connection.connection_id)
    assert retry["connection"]["state"] == "revoked"
    async with async_db() as db:
        revoked = await db.get(GoogleServiceConnection, connection.connection_id)
        assert revoked is not None and revoked.state == "revoked"


@pytest.mark.asyncio
async def test_published_mail_artifact_failure_is_exact_recovery_pending(durable_job_db, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config_settings, "workspace_dir", str(tmp_path))
    request = MailSourceRequest(
        operation="mail_artifact_receipt_failure",
        owner_principal_id="principal-artifact",
        owner_session_id="session-artifact",
        connection_id="connection-artifact",
        connection_revision=1,
        request_uuid="artifact-failure-request",
        request_digest="sha256:artifact-failure-request",
    )
    async def fail_record_artifact(*_args, **_kwargs):
        raise RuntimeError("injected artifact receipt failure")

    monkeypatch.setattr(gmail_controls.durable_job_repository, "record_artifact", fail_record_artifact)
    with pytest.raises(GmailControlError) as error:
        await run_mail_source_control(request, lambda _lease: asyncio.sleep(0, result={"private": "artifact"}))
    assert error.value.code == "mail_read_reconciliation_required"
    job = await gmail_controls.durable_job_repository.get_job(request.job_id)
    assert job is not None and job["status"] == "unknown_external_effect"
    unknown = [effect for effect in job["effects"] if effect.get("status") == "unknown"]
    assert unknown and unknown[-1]["details"]["artifact_recovery"] == "pending_exact_owner_job_hash_reconciliation"
    recovery = await inspect_mail_source_artifact_recovery(
        request.job_id,
        owner_principal_id=request.owner_principal_id,
    )
    assert recovery["status"] == "pending"
    assert recovery["artifact_state"] == "verified"
    assert (tmp_path / recovery["artifact_path"]).exists()


@pytest.mark.asyncio
async def test_process_boundary_after_publish_uses_prepublication_checkpoint(durable_job_db, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config_settings, "workspace_dir", str(tmp_path))
    request = MailSourceRequest(
        operation="mail_artifact_process_boundary",
        owner_principal_id="principal-process-boundary",
        owner_session_id="session-process-boundary",
        connection_id="connection-process-boundary",
        connection_revision=1,
        request_uuid="process-boundary-request",
        request_digest="sha256:process-boundary-request",
    )

    class ProcessDeath(BaseException):
        pass

    async def fail_after_publish(*_args, **_kwargs):
        raise ProcessDeath("simulated process boundary")

    monkeypatch.setattr(gmail_controls.durable_job_repository, "record_artifact", fail_after_publish)
    with pytest.raises(ProcessDeath):
        await run_mail_source_control(request, lambda _lease: asyncio.sleep(0, result={"private": "process-boundary-body"}))

    job = await gmail_controls.durable_job_repository.get_job(request.job_id)
    assert job is not None and job["status"] == "running"
    checkpoints = [item for item in job["checkpoints"] if item.get("checkpoint_id") == "mail-source-artifact-prepared"]
    assert checkpoints
    checkpoint_payload = checkpoints[-1]["payload"]
    assert checkpoint_payload["job_id"] == request.job_id
    assert checkpoint_payload["artifact_path"] == gmail_controls._artifact_path(request.job_id)
    assert "process-boundary-body" not in json.dumps(job)
    recovery = await inspect_mail_source_artifact_recovery(
        request.job_id,
        owner_principal_id=request.owner_principal_id,
    )
    assert recovery["status"] == "pending"
    assert recovery["artifact_state"] == "verified"
    assert recovery["artifact_sha256"] == checkpoint_payload["artifact_sha256"]
    assert (tmp_path / recovery["artifact_path"]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_message_ids",
    [("m1",), ("m1", "m2")],
    ids=["normal-single-message", "provider-over-return"],
)
async def test_authenticated_asgi_mail_journey_uses_one_owner_and_mock_provider(
    client,
    async_db,
    monkeypatch,
    tmp_path: Path,
    provider_message_ids: tuple[str, ...],
):
    """Exercise the mounted middleware and route sequence with a real session."""
    monkeypatch.setattr(config_settings, "operator_auth_secret", "auth-test-secret")
    monkeypatch.setattr(config_settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(config_settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(config_settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(mail_api, "get_session", async_db)

    provider_requests: list[str] = []
    metadata_requests: list[str] = []

    async def provider_handler(provider_request: httpx.Request) -> httpx.Response:
        provider_requests.append(str(provider_request.url))
        if provider_request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access-token", "scope": GMAIL_READONLY_SCOPE})
        if provider_request.url.path.endswith("/labels"):
            return httpx.Response(
                200,
                json={"labels": [{"id": "INBOX", "name": "Inbox", "type": "system"}]},
            )
        if provider_request.url.path.endswith("/messages"):
            return httpx.Response(
                200,
                json={"messages": [{"id": message_id} for message_id in provider_message_ids]},
            )
        if "/messages/" in provider_request.url.path:
            metadata_requests.append(str(provider_request.url))
            return httpx.Response(200, json=_full_message())
        return httpx.Response(404, json={})

    transport = httpx.MockTransport(provider_handler)

    class RoutedAdapter(GoogleGmailReadonlyAdapter):
        def __init__(self, connection, *args, **kwargs):
            kwargs["transport"] = transport
            kwargs["resolver"] = lambda _host, _port: ["8.8.8.8"]
            super().__init__(connection, *args, **kwargs)

    monkeypatch.setattr(mail_api, "GoogleGmailReadonlyAdapter", RoutedAdapter)
    token, operator = await __import__("src.auth.service", fromlist=["create_session"]).create_session()
    client.cookies.set(config_settings.operator_auth_cookie_name, token)
    async with async_db() as db:
        db.add(Session(id=operator.session_id, owner_principal_id=operator.principal.principal_id))
        db.add(
            Goal(
                id="asgi-mail-goal",
                title="Authenticated Mail journey",
                owner_principal_id=operator.principal.principal_id,
                owner_session_id=operator.session_id,
                revision=1,
                status="active",
            )
        )

    headers = {"host": "test", "origin": "http://localhost:3001"}
    connection_response = await client.post(
        "/api/capabilities/mail/connections",
        headers=headers,
        json={
            "schema_version": 1,
            "service": "gmail_readonly",
            "label": "ASGI Gmail",
            "client_id": "client-id",
            "client_secret": "client-secret",
            "refresh_token": "refresh-token",
            "declared_scopes": [GMAIL_READONLY_SCOPE],
            "idempotency_key": "asgi-connection-key",
        },
    )
    assert connection_response.status_code == 201, connection_response.text
    connection = connection_response.json()["connection"]
    assert connection["state"] == "active"

    verify_response = await client.post(
        f"/api/capabilities/mail/connections/{connection['connection_id']}/verify",
        headers=headers,
        json={"expected_revision": connection["revision"], "request_uuid": "asgi-verify"},
    )
    assert verify_response.status_code == 200, verify_response.text
    verified = verify_response.json()
    assert verified["provider_scopes_verified"] is True
    async with async_db() as db:
        verified_connection = await db.get(GoogleServiceConnection, connection["connection_id"])
        assert verified_connection is not None
        assert verified_connection.scope_status == "scope_verified"
        assert json.loads(verified_connection.provider_scopes_json) == [GMAIL_READONLY_SCOPE]

    labels_response = await client.post(
        "/api/capabilities/mail/labels/refresh",
        headers=headers,
        json={
            "connection_id": connection["connection_id"],
            "expected_connection_revision": verified["connection_revision"],
            "acknowledge_account_label_read": True,
            "request_uuid": "asgi-labels",
        },
    )
    assert labels_response.status_code == 200, labels_response.text
    labels = labels_response.json()
    label_id = labels["labels"][0]["label_id"]

    consent_response = await client.post(
        "/api/capabilities/mail/read-consents",
        headers=headers,
        json={
            "schema_version": 1,
            "connection_id": connection["connection_id"],
            "expected_connection_revision": labels["connection_revision"],
            "goal_id": "asgi-mail-goal",
            "expected_goal_revision": 1,
            "label_ids": [label_id],
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "max_messages": 1,
            "allowed_body_fields": ["subject"],
            "acknowledge_source_read": True,
            "idempotency_key": "asgi-consent-key",
        },
    )
    assert consent_response.status_code == 201, consent_response.text
    consent = consent_response.json()["consent"]

    scan_response = await client.post(
        "/api/capabilities/mail/messages/scan",
        headers=headers,
        json={
            "connection_id": connection["connection_id"],
            "expected_connection_revision": labels["connection_revision"],
            "mail_consent_id": consent["consent_id"],
            "expected_source_consent_revision": consent["source_revision"],
            "label_ids": [label_id],
            "received_after": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            "max_messages": 1,
            "request_uuid": "asgi-scan",
        },
    )
    if len(provider_message_ids) > 1:
        assert scan_response.status_code == 409, scan_response.text
        assert scan_response.json()["detail"]["code"] == "mail_read_reconciliation_required"
        assert metadata_requests == []
        assert not any("model" in url.casefold() for url in provider_requests)
        async with async_db() as db:
            bindings = (
                await db.execute(
                    select(MailMessageBinding).where(
                        MailMessageBinding.owner_principal_id == operator.principal.principal_id,
                        MailMessageBinding.owner_session_id == operator.session_id,
                        MailMessageBinding.connection_id == connection["connection_id"],
                    )
                )
            ).scalars().all()
            jobs = (
                await db.execute(
                    select(WorkflowRunState).where(
                        WorkflowRunState.owner_principal_id == operator.principal.principal_id,
                        WorkflowRunState.operator_session_id == operator.session_id,
                        WorkflowRunState.job_kind == "mail_messages_scan",
                    )
                )
            ).scalars().all()
        assert bindings == []
        assert len(jobs) == 1
        assert jobs[0].status == "unknown_external_effect"
        return

    assert scan_response.status_code == 200, scan_response.text
    scan = scan_response.json()
    assert len(scan["messages"]) == 1
    binding = scan["messages"][0]

    read_response = await client.post(
        f"/api/capabilities/mail/messages/{binding['source_binding_id']}/read",
        headers=headers,
        json={
            "connection_id": connection["connection_id"],
            "expected_connection_revision": labels["connection_revision"],
            "mail_consent_id": consent["consent_id"],
            "expected_source_consent_revision": consent["source_revision"],
            "message_binding_id": binding["source_binding_id"],
            "expected_message_revision": binding["message_revision"],
            "acknowledge_selected_body_read": True,
            "request_uuid": "asgi-read",
        },
    )
    assert read_response.status_code == 200, read_response.text
    read = read_response.json()
    assert read["plain_text"] == "Hello world"
    assert read["provenance"]["memory_status"] == "no_learning"
    assert all("refresh-token" not in url for url in provider_requests)
    assert any(url.endswith("/labels") for url in provider_requests)
    assert any("/messages/m1" in url for url in provider_requests)


@pytest.mark.asyncio
async def test_mail_additive_migration_is_separate_and_idempotent():
    engine = create_async_engine("sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "CREATE TABLE google_service_connections (connection_id VARCHAR PRIMARY KEY)"
        )
        await connection.exec_driver_sql(
            "CREATE TABLE mail_read_consents (consent_id VARCHAR PRIMARY KEY)"
        )
        await connection.exec_driver_sql(
            "CREATE TABLE mail_message_bindings (message_binding_id VARCHAR PRIMARY KEY)"
        )
        await _ensure_mail_columns(connection)
        await _ensure_mail_columns(connection)
        for table, expected in (
            (
                "google_service_connections",
                {"declared_scopes_json", "provider_scopes_json", "scope_status", "revoke_idempotency_key", "revoke_request_digest"},
            ),
            ("mail_read_consents", {"revoke_idempotency_key", "revoke_request_digest"}),
            (
                "mail_message_bindings",
                {"source_consent_id", "source_consent_revision", "source_label_scope_digest"},
            ),
        ):
            result = await connection.exec_driver_sql(f"PRAGMA table_info({table})")
            columns = {row[1] for row in result.fetchall()}
            assert expected <= columns
    await engine.dispose()
