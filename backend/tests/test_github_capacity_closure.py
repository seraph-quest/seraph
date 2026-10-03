"""Actual protected GET and file SQLite closure; external HTTP intercepted."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import update
from sqlmodel import select

from src.db import engine
from src.db.models import Goal, GitHubFollowthroughConnection, WorkflowRunState
from src.extensions.github_capacity_closure import LegacyCloseRequest, PublicationCloseRequest
from src.extensions.github_followthrough import GitHubFollowthroughService, GitHubFollowthroughError
from src.workflows.job_runtime import durable_job_repository as jobs, DurableJobError
from tests.test_github_connection_consent import ORIGIN
from tests.test_github_recovery_observation import observation_case


def close_request(current, read):
    return {"acknowledged_capacity_close": True, "expected_job_revision": current["revision"],
        "expected_connection_revision": read.connection_revision,
        "expected_connection_fence": read.connection_fence,
        "idempotency_key": str(uuid.uuid4()), "remote_id": 1}


@pytest.mark.parametrize("model", [LegacyCloseRequest, PublicationCloseRequest])
@pytest.mark.parametrize("field,value", [("acknowledged_capacity_close", 1),
    ("acknowledged_capacity_close", "true"), ("acknowledged_capacity_close", False),
    ("expected_job_revision", True), ("expected_connection_revision", True),
    ("expected_connection_fence", True), ("expected_proof", {"verified": True}),
    ("idempotency_key", "AA000000-0000-0000-0000-000000000000")])
def test_close_contract_rejects_coercion_and_caller_proof(model, field, value):
    body = {"acknowledged_capacity_close": True, "expected_job_revision": 1,
        "expected_connection_revision": 1, "expected_connection_fence": 1,
        "idempotency_key": str(uuid.uuid4())}
    body[field] = value
    with pytest.raises(ValueError): model(**body)


async def actual_close_case(client, async_db, monkeypatch, tmp_path, *, status=200):
    current, goal, kwargs, _ = await observation_case(client, monkeypatch, tmp_path, stopped_mode="disabled")
    read = kwargs["read_authority"]
    service = GitHubFollowthroughService()
    prepared = await service._read_prepared(current)
    calls = []
    async def resolver(*_): return ["93.184.216.34"]
    payload = {"number": 1, "title": prepared.title, "body": prepared.body}
    def transport(request):
        calls.append(request.method)
        assert request.method == "GET" and request.url.path == "/repos/acme/example/issues/1"
        return httpx.Response(status, json=payload)
    service = GitHubFollowthroughService(resolver=resolver, transport=httpx.MockTransport(transport))
    monkeypatch.setattr("src.extensions.github_followthrough.github_followthrough_service", service)
    async with engine.get_session() as db:
        await db.execute(update(Goal).where(Goal.id == goal.id).values(revision=2))
    return current, read, service, calls, close_request(current, read)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_actual_legacy_capacity_close_is_atomic_durable_and_permanently_fenced(client, async_db, monkeypatch, tmp_path):
    original, read, service, calls, body = await actual_close_case(client, async_db, monkeypatch, tmp_path)
    response = await client.post(f"/api/capabilities/github/jobs/{read.job_id}/close-capacity", json=body, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert calls == ["GET"] and response.json()["status"] == "unknown_external_effect"
    closed = await jobs.get_job(read.job_id)
    assert closed["revision"] == original["revision"] + 1 and closed["github_capacity_closure"]["observation_only"] is True
    for field in ("status", "goal_revision", "authority_digest", "declared_authority", "deadline_at", "attempt_count", "effects", "result"):
        assert closed[field] == original[field]
    assert closed["lease"]["fencing_token"] == original["lease"]["fencing_token"]
    async with async_db() as db:
        physical_engine = db.bind
        row = await db.get(GitHubFollowthroughConnection, read.original_binding["connection_id"])
        assert row.active_job_id is None and row.active_fence == read.connection_fence
    await physical_engine.dispose()
    repeated = await client.post(f"/api/capabilities/github/jobs/{read.job_id}/close-capacity", json=body, headers=ORIGIN)
    assert repeated.status_code == 200 and repeated.json()["github_capacity_closure"] == response.json()["github_capacity_closure"] and calls == ["GET"]
    different = await client.post(f"/api/capabilities/github/jobs/{read.job_id}/close-capacity", json={**body, "idempotency_key": str(uuid.uuid4())}, headers=ORIGIN)
    assert different.status_code == 409 and calls == ["GET"]
    for action in [lambda: jobs.queue_job(read.job_id, expected_revision=closed["revision"]),
        lambda: jobs.claim_job(read.job_id, owner="forbidden"),
        lambda: jobs.assert_active_lease(read.job_id, owner="forbidden", fencing_token=closed["lease"]["fencing_token"]),
        lambda: jobs.finalize_reconciled_job(read.job_id, owner_kind="user", owner_principal_id=read.principal),
        lambda: jobs.recover_stale_job(read.job_id)]:
        with pytest.raises(DurableJobError): await action()
    with pytest.raises(GitHubFollowthroughError):
        await service._reserve_connection(owner_principal_id=read.principal, connection_id=read.original_binding["connection_id"], expected_revision=read.connection_revision, job_id=read.job_id)
    assert await service._release_connection(connection_id=read.original_binding["connection_id"], owner_principal_id=read.principal, job_id=read.job_id, fence=read.connection_fence) is False
    proof = tmp_path / "actual-legacy-capacity-closure.json"
    proof.write_text(json.dumps({"before": original, "after": closed, "methods": calls, "restart": "actual file SQLite pool reopen"}, sort_keys=True))
    print("ACTUAL_LEGACY_CAPACITY_CLOSURE=" + str(proof))


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("negative", ["404", "missing_id", "atomic_second_cas"])
async def test_legacy_close_negative_keeps_reservation_and_all_canonical_history(client, async_db, monkeypatch, tmp_path, negative):
    original, read, _, calls, body = await actual_close_case(client, async_db, monkeypatch, tmp_path, status=404 if negative == "404" else 200)
    if negative == "missing_id": body.pop("remote_id")
    if negative == "atomic_second_cas":
        original_session = jobs._session
        @asynccontextmanager
        async def failed_second_cas():
            async with original_session() as db:
                actual_execute = db.execute
                async def execute(statement, *args, **kwargs):
                    result = await actual_execute(statement, *args, **kwargs)
                    if getattr(statement, "is_update", False) and getattr(getattr(statement, "table", None), "name", None) == "github_followthrough_connections":
                        return SimpleNamespace(rowcount=0)
                    return result
                monkeypatch.setattr(db, "execute", execute)
                yield db
        monkeypatch.setattr(jobs, "_session", failed_second_cas)
    response = await client.post(f"/api/capabilities/github/jobs/{read.job_id}/close-capacity", json=body, headers=ORIGIN)
    assert response.status_code == 409, response.text
    current = await jobs.get_job(read.job_id)
    assert current["revision"] == original["revision"] and current["effects"] == original["effects"] and current["artifacts"] == original["artifacts"] and current["checkpoints"] == original["checkpoints"]
    assert "github_capacity_closure" not in current
    async with async_db() as db:
        row = await db.get(GitHubFollowthroughConnection, read.original_binding["connection_id"])
        assert row.active_job_id == read.job_id and row.active_fence == read.connection_fence
    assert calls == ([] if negative == "missing_id" else ["GET"])
