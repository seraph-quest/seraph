"""Real default login and finite canonical connection consent, no account writes."""
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import update

from config.settings import settings
from src.db.models import GitHubFollowthroughConnection
from src.extensions.github_consent import GitHubConsentRequest, require_consent, require_readback
from src.extensions.github_followthrough import GitHubFollowthroughService, GitHubFollowthroughError
from src.vault.repository import vault_repository

ORIGIN = {"Origin": "http://localhost:3001"}


async def login(client, monkeypatch):
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "consent-test-password")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    response = await client.post("/api/auth/login", json={"password": "consent-test-password"}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    return response.json()


async def active_connection(client, monkeypatch):
    owner = await login(client, monkeypatch)
    monkeypatch.setattr(settings, "vault_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr("src.vault.crypto._fernet", None)
    await vault_repository.store("github-test", "fixture-token", owner_principal_id=owner["principal_id"])
    request = {"repository": "acme/example", "vault_key": "github-test", "mode": "active", "expected_revision": 0,
        "consent": {"acknowledged": True, "duration_seconds": 900, "actions": ["github_issue_write"]}}
    response = await client.put("/api/capabilities/github/connection", json=request, headers=ORIGIN)
    assert response.status_code == 200, response.text
    row = await GitHubFollowthroughService()._get_connection_row(owner["principal_id"])
    binding = await require_consent(row, principal=owner["principal_id"], root=owner["session_id"], repository=row.repository,
        revision=row.revision, required_actions={"github_issue_write"})
    return owner, row, binding, request


@pytest.mark.asyncio
async def test_default_login_issues_finite_consent_with_independent_metadata_and_cas(client, async_db, monkeypatch):
    owner, row, binding, request = await active_connection(client, monkeypatch)
    response = await client.get("/api/capabilities/github/connection")
    value = response.json()
    assert value["consent"]["state"] == "active" and value["consent"]["root_bound"] is True
    assert value["consent"]["credential_is_consent"] is False
    assert datetime.fromisoformat(value["consent"]["expires_at"]) <= datetime.fromisoformat(owner["absolute_expires_at"])
    stale = await client.put("/api/capabilities/github/connection", json=request, headers=ORIGIN)
    assert stale.status_code == 409
    request["expected_revision"] = row.revision
    request.pop("consent")
    assert (await client.put("/api/capabilities/github/connection", json=request, headers=ORIGIN)).status_code == 422
    assert binding["consent_root_id"] == owner["session_id"]


@pytest.mark.parametrize("consent", [
    {"acknowledged": False, "duration_seconds": 900, "actions": ["github_issue_write"]},
    {"acknowledged": 1, "duration_seconds": 900, "actions": ["github_issue_write"]},
    {"acknowledged": True, "duration_seconds": 3601, "actions": ["github_issue_write"]},
    {"acknowledged": True, "duration_seconds": 900, "actions": ["external_mutation"]},
    {"acknowledged": True, "duration_seconds": 900, "actions": ["github_issue_write", "github_issue_write"]},
    {"acknowledged": True, "duration_seconds": 900, "actions": ["github_issue_write"], "approved": True},
])
def test_consent_request_rejects_bypass_unbounded_and_arbitrary_actions(consent):
    with pytest.raises(ValueError):
        GitHubConsentRequest(**consent)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["root", "expiry", "action", "vault", "legacy"])
async def test_changed_canonical_consent_has_zero_transport_contact(client, async_db, monkeypatch, change):
    owner, row, binding, _ = await active_connection(client, monkeypatch)
    if change == "root":
        owner = await login(client, monkeypatch)
    if change in {"expiry", "legacy"}:
        from src.db import engine
        async with engine.get_session() as db:
            await db.execute(update(GitHubFollowthroughConnection).where(GitHubFollowthroughConnection.id == row.id).values(
                **({"consent_expires_at": datetime.now(timezone.utc)-timedelta(seconds=1)} if change == "expiry" else {"consent_id": None})))
    if change == "vault":
        await vault_repository.store("github-test", "rotated-token", owner_principal_id=owner["principal_id"])
    calls = []
    async def resolver(*_args): return ["93.184.216.34"]
    async def handoff(): return None
    service = GitHubFollowthroughService(resolver=resolver, transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(201)))
    path = "/repos/acme/example/pulls" if change == "action" else "/repos/acme/example/issues"
    body = {"title": "Exact issue", "body": "Exact approved content"} if change != "action" else {"title": "PR", "body": "Body", "head": "feat/x", "base": "main", "draft": False}
    with pytest.raises(GitHubFollowthroughError):
        await service._request(path, method="POST", token="fixture-token", json_body=body,
            authority_check=handoff, github_consent_binding={**binding, "consent_root_id": owner["session_id"]})
    assert calls == []


@pytest.mark.asyncio
async def test_stop_during_dns_cancels_contact_but_retains_original_root_get_recovery(client, async_db, monkeypatch):
    owner, row, binding, request = await active_connection(client, monkeypatch)
    service = GitHubFollowthroughService()
    fence = await service._reserve_connection(owner_principal_id=owner["principal_id"], connection_id=row.id, expected_revision=row.revision, job_id="recorded-job")
    request["expected_revision"] = row.revision
    assert (await client.put("/api/capabilities/github/connection", json=request, headers=ORIGIN)).status_code == 409
    calls = []
    async def resolver(*_args):
        stopped = await client.post("/api/capabilities/github/connection/revoke", json={"expected_revision": row.revision}, headers=ORIGIN)
        assert stopped.status_code == 200
        return ["93.184.216.34"]
    async def handoff(): return None
    service = GitHubFollowthroughService(resolver=resolver, transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(201)))
    with pytest.raises(GitHubFollowthroughError):
        await service._request("/repos/acme/example/issues", method="POST", token="fixture-token", json_body={"title": "Issue", "body": "Body"}, authority_check=handoff, github_consent_binding=binding)
    assert calls == []
    current = await service._get_connection_row(owner["principal_id"])
    assert current.mode == "disabled" and current.active_job_id == "recorded-job" and current.active_fence == fence
    snapshot = await require_readback(current, principal=owner["principal_id"], root=owner["session_id"], original_binding=binding,
        expected_revision=current.revision, job_id="recorded-job", connection_fence=fence)
    assert snapshot.value == "fixture-token"
    fresh = await login(client, monkeypatch)
    with pytest.raises(GitHubFollowthroughError):
        await require_readback(current, principal=fresh["principal_id"], root=fresh["session_id"], original_binding=binding,
            expected_revision=current.revision, job_id="recorded-job", connection_fence=fence)
