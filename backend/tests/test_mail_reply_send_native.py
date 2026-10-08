"""Actual file-SQLite/auth/Vault/native Mail boundaries; provider HTTP only mocked."""
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from sqlalchemy import select

from config.settings import settings
from src.auth.service import create_session, authenticate_token
from src.db.models import Goal, GoogleServiceConnection, WorkflowRunState
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.integrations.gmail_send import READ_SERVICE, SEND_SERVICE, SCOPES, digest, GmailReadError
from src.integrations import mail_reply_runtime as runtime
from src.vault import vault_repository
from src.vault import crypto


async def native_setup(async_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "explicit-private-test-password")
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "workspace"))
    (tmp_path / "workspace").mkdir(mode=0o700)
    monkeypatch.setattr(crypto, "_fernet", None)
    token, _ = await create_session()
    operator = await authenticate_token(token)
    current = datetime.now(timezone.utc)
    budget = GoalAdmissionBudget(reviewed_grant=True, grant_id="reply-test-grant",
        max_outstanding_jobs=1, max_attempts=1, max_runtime_seconds=300,
        notifications_per_day=0, period_started_at=current-timedelta(seconds=1),
        period_expires_at=current+timedelta(hours=1), timezone="UTC")
    async with async_db() as db:
        db.add(Goal(id="reply-current-goal", title="Finite exact reply test", status="active",
            owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id,
            revision=1, admission_budget_json=serialize_admission_budget(budget)))
    for service in (READ_SERVICE, SEND_SERVICE):
        credentials = {"client_id": "dummy-client", "client_secret": None, "refresh_token": "dummy-"+service}
        key = "reply-private:"+service
        await vault_repository.store(key, json.dumps(credentials), owner_principal_id=operator.principal.principal_id)
        async with async_db() as db:
            db.add(GoogleServiceConnection(connection_id=service, owner_principal_id=operator.principal.principal_id,
                owner_session_id=operator.session_id, service=service, state="active", revision=1,
                vault_secret_key=key, credential_fingerprint=digest(credentials),
                declared_scopes_json=json.dumps(sorted(SCOPES[service])),
                setup_idempotency_key="setup-"+service))
    return operator


def boundary(calls, *, mismatch=False, omitted=False):
    async def provider(request):
        calls.append((request.method, request.url.host, request.url.path))
        if request.url.host == "oauth2.googleapis.com":
            service = READ_SERVICE if READ_SERVICE.encode() in request.content else SEND_SERVICE
            value = {"access_token": "dummy-"+service}
            if not omitted:
                value["scope"] = " ".join(sorted(SCOPES[service]))
            return httpx.Response(200, json=value)
        if request.url.host == "openidconnect.googleapis.com":
            is_send = SEND_SERVICE in request.headers["authorization"]
            return httpx.Response(200, json={"sub": "changed" if mismatch and is_send else "ExactSubject",
                "email": "mailbox@example.test", "email_verified": True})
        assert request.method == "GET" and request.url.path.endswith("/profile")
        return httpx.Response(200, json={"emailAddress": "mailbox@example.test"})
    return {"transport": httpx.MockTransport(provider), "resolver": lambda host, port: ["93.184.216.34"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("mode", ["positive", "subject_mismatch", "omitted_scope"])
async def test_real_native_pair_identity_vault_reopen_no_sender_authority(async_db, monkeypatch, tmp_path, mode):
    operator = await native_setup(async_db, monkeypatch, tmp_path)
    calls = []
    kwargs = dict(request_uuid="pair-once", goal_id="reply-current-goal", goal_revision=1,
        read_connection_id=READ_SERVICE, send_connection_id=SEND_SERVICE,
        **boundary(calls, mismatch=mode == "subject_mismatch", omitted=mode == "omitted_scope"))
    if mode == "positive":
        result = await runtime.verify_pair(operator, **kwargs)
        assert result["status"] == "succeeded" and result["contacts_spent"] == 5
        assert result["outcome"] == "verified_reply_identity" and result["no_learning"] is True
        await runtime.verify_pair(operator, **kwargs)
        assert len(calls) == 5
        current = await runtime.pair_snapshots(operator, READ_SERVICE, SEND_SERVICE)
        account = await runtime.paired_account(operator, current)
        assert account == {"sub": "ExactSubject", "email": "mailbox@example.test", "issuer": "https://accounts.google.com"}
    else:
        with pytest.raises(GmailReadError):
            await runtime.verify_pair(operator, **kwargs)
        assert len(calls) == (1 if mode == "omitted_scope" else 5)
    async with async_db() as db:
        run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind == runtime.IDENTITY_KIND))).scalar_one()
        assert run.status == ("succeeded" if mode == "positive" else "blocked")
        assert run.attempt_count == 1 and run.effect_receipts_json == "[]"
        assert "dummy-" not in run.checkpoint_context_json
        assert run.lease_owner is None and run.lease_expires_at is None
        if mode != "positive":
            rows = (await db.execute(select(GoogleServiceConnection))).scalars().all()
            assert all(row.scope_status == "scope_unverified" for row in rows)
