"""Zero-spend checks for the bounded GitHub follow-through adapter."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

import httpx
import pytest

from src.extensions.github_followthrough import (
    ACTION_CREATE_COMMENT,
    ACTION_CREATE_ISSUE,
    CONNECTION_MODE_RECONCILE_ONLY,
    GitHubFollowthroughError,
    GitHubFollowthroughService,
    PreparedPublication,
    _final_body,
    _marker,
    _operation_id,
    _sha,
)


async def _public_resolver(_hostname: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def _prepared(
    *,
    job_id: str = "ghfollow_test",
    issue_number: int | None = None,
    action: str = ACTION_CREATE_ISSUE,
) -> PreparedPublication:
    operation_id = _operation_id("operator-1", uuid.UUID("11111111-1111-4111-8111-111111111111"))
    body = _final_body("A bounded guardian update.", operation_id)
    return PreparedPublication(
        operation_id=operation_id,
        job_id=job_id,
        owner_principal_id="operator-1",
        owner_session_id="session-1",
        conversation_id="session-1",
        goal_id="goal-1",
        goal_revision=3,
        source_watch_id="watch-1",
        plan_revision=4,
        dossier_artifact_id="dossier-1",
        dossier_sha256="a" * 64,
        connection_id="connection-1",
        connection_revision=2,
        repository="acme/example",
        action=action,
        issue_number=issue_number if action == ACTION_CREATE_COMMENT else None,
        title="Guardian update" if action == ACTION_CREATE_ISSUE else None,
        body=body,
        body_sha256=_sha(body),
        operation_marker=_marker(operation_id),
        payload_path="github/followthrough/operator/job.json",
        payload_sha256="b" * 64,
    )


def _job(prepared: PreparedPublication, *, status: str = "queued") -> dict:
    payload = {
        "job_id": prepared.job_id,
        "owner": {"kind": "user", "principal_id": prepared.owner_principal_id},
        "status": status,
        "goal_id": prepared.goal_id,
        "goal_revision": prepared.goal_revision,
        "plan_revision": prepared.plan_revision,
        "declared_authority": {"approval_id": "approval-1"},
        "deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "revision": 1,
        "lease": {"owner": None, "fencing_token": 0},
        "effects": [],
        "artifacts": [],
        "checkpoints": [],
    }
    if status == "queued":
        payload["effects"] = [{
            "kind": "approval_resume",
            "status": "approved",
            "approval_id": "approval-1",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=4)).timestamp(),
        }]
    return payload


class _MemoryDurableJobs:
    def __init__(self, current: dict):
        self.current = copy.deepcopy(current)

    async def get_job(self, _job_id: str):
        return copy.deepcopy(self.current)

    async def claim_job(self, _job_id: str, **_kwargs):
        self.current["status"] = "running"
        self.current["revision"] += 1
        self.current["lease"] = {"owner": "github-followthrough:" + self.current["job_id"], "fencing_token": 1}
        return copy.deepcopy(self.current)

    async def record_effect(self, _job_id: str, **kwargs):
        effect_id = kwargs["effect_id"]
        receipt = {
            "effect_id": effect_id,
            "receipt_kind": kwargs.get("receipt_kind", "effect"),
            "effect_type": kwargs["effect_type"],
            "target_path": kwargs.get("target_path"),
            "target_digest": kwargs.get("target_digest"),
            "status": kwargs.get("status"),
            "content_sha256": kwargs.get("content_sha256"),
            "details": kwargs.get("details", {}),
        }
        self.current["effects"] = [item for item in self.current["effects"] if item.get("effect_id") != effect_id]
        self.current["effects"].append(receipt)
        self.current["revision"] += 1
        return copy.deepcopy(self.current)

    async def record_readback(self, _job_id: str, **kwargs):
        return await self.record_effect(
            _job_id,
            **kwargs,
            receipt_kind="readback",
        )

    async def record_checkpoint(self, _job_id: str, **kwargs):
        checkpoint_id = kwargs["checkpoint_id"]
        self.current["checkpoints"] = [
            item for item in self.current["checkpoints"] if item.get("checkpoint_id") != checkpoint_id
        ]
        self.current["checkpoints"].append({
            "checkpoint_id": checkpoint_id,
            "payload": kwargs.get("checkpoint_payload", {}),
        })
        self.current["revision"] += 1
        return copy.deepcopy(self.current)

    async def cancel_job(self, _job_id: str, **_kwargs):
        self.current["status"] = "cancelled"
        self.current["revision"] += 1
        return copy.deepcopy(self.current)


@pytest.mark.asyncio
async def test_execute_posts_once_then_reads_back_exact_issue_without_secret_receipt(monkeypatch):
    prepared = _prepared()
    durable = _MemoryDurableJobs(_job(prepared))
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        assert request.headers["authorization"] == "Bearer secret-token"
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload == {"title": prepared.title, "body": prepared.body}
            return httpx.Response(
                201,
                json={"number": 42, "title": prepared.title, "body": prepared.body},
                request=request,
            )
        return httpx.Response(
            200,
            json={"number": 42, "title": prepared.title, "body": prepared.body},
            request=request,
        )

    service = GitHubFollowthroughService(
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
        sleep=lambda _delay: _public_resolver("", 0),
    )
    connection = SimpleNamespace(
        id=prepared.connection_id,
        revision=prepared.connection_revision,
        repository=prepared.repository,
        mode="active",
        vault_key="github-token",
        active_job_id=prepared.job_id,
        active_fence=1,
    )
    monkeypatch.setattr("src.extensions.github_followthrough.durable_job_repository", durable)
    monkeypatch.setattr(service, "_read_prepared", lambda _current: _async_value(prepared))
    monkeypatch.setattr(service, "_get_connection_row", lambda _owner: _async_value(connection))
    monkeypatch.setattr(service, "_reserve_connection", lambda **_kwargs: _async_value(1))
    monkeypatch.setattr(service, "_verify_live_handoff", lambda *_args, **_kwargs: _async_value(None))
    monkeypatch.setattr(service, "_load_token", lambda _connection: _async_value("secret-token"))
    monkeypatch.setattr(
        "src.extensions.github_followthrough.approval_repository.get",
        lambda _approval_id: _async_value(
            SimpleNamespace(
                id="approval-1",
                status="approved",
                operator_session_id=prepared.owner_session_id,
                session_id=prepared.owner_session_id,
                details_json=json.dumps({
                    "approval_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=4)).timestamp(),
                }),
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "_finalize_verified",
        lambda *_args, **_kwargs: _mark_success(durable),
    )
    async def live_session(session_id: str, *, touch: bool = False):
        return SimpleNamespace(
            session_id=session_id,
            principal=SimpleNamespace(
                principal_id=prepared.owner_principal_id,
                grants={"external_mutation"},
            ),
        )

    monkeypatch.setattr("src.extensions.github_followthrough.authenticate_session", live_session)

    result = await service.execute(
        owner_principal_id=prepared.owner_principal_id,
        owner_session_id=prepared.owner_session_id,
        external_mutation_granted=True,
        job_id=prepared.job_id,
    )

    assert result["status"] == "succeeded"
    assert [method for method, _url in calls] == ["POST", "GET"]
    assert sum(method == "POST" for method, _url in calls) == 1
    assert "secret-token" not in json.dumps(durable.current)
    assert prepared.body not in json.dumps(durable.current)


@pytest.mark.asyncio
async def test_execute_rechecks_live_external_mutation_grant(monkeypatch):
    prepared = _prepared()
    service = GitHubFollowthroughService()
    durable = _MemoryDurableJobs(_job(prepared))
    monkeypatch.setattr("src.extensions.github_followthrough.durable_job_repository", durable)

    async def live_session(session_id: str, *, touch: bool = False):
        return SimpleNamespace(
            session_id=session_id,
            principal=SimpleNamespace(
                principal_id=prepared.owner_principal_id,
                grants=set(),
            ),
        )

    monkeypatch.setattr("src.extensions.github_followthrough.authenticate_session", live_session)

    with pytest.raises(GitHubFollowthroughError, match="external_mutation_grant_required"):
        await service.execute(
            owner_principal_id=prepared.owner_principal_id,
            owner_session_id=prepared.owner_session_id,
            external_mutation_granted=True,
            job_id=prepared.job_id,
        )


@pytest.mark.asyncio
async def test_execute_requires_explicit_external_mutation_grant(monkeypatch):
    prepared = _prepared()
    service = GitHubFollowthroughService()
    durable = _MemoryDurableJobs(_job(prepared))
    monkeypatch.setattr("src.extensions.github_followthrough.durable_job_repository", durable)

    with pytest.raises(GitHubFollowthroughError, match="external_mutation_grant_required"):
        await service.execute(
            owner_principal_id=prepared.owner_principal_id,
            owner_session_id=prepared.owner_session_id,
            job_id=prepared.job_id,
        )


@pytest.mark.asyncio
async def test_reconcile_only_reads_back_and_never_reposts(monkeypatch):
    prepared = _prepared(issue_number=42)
    current = _job(prepared, status="unknown_external_effect")
    current["effects"] = [
        {
            "effect_id": f"github:{prepared.operation_id}",
            "effect_type": "github_publication",
            "status": "unknown",
            "target_path": "/repos/acme/example/issues",
            "target_digest": prepared.body_sha256,
            "details": {"remote_id": 42},
        }
    ]
    durable = _MemoryDurableJobs(current)
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(
            200,
            json={"number": 42, "title": prepared.title, "body": prepared.body},
            request=request,
        )

    service = GitHubFollowthroughService(
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
        sleep=lambda _delay: _public_resolver("", 0),
    )
    connection = SimpleNamespace(
        id=prepared.connection_id,
        revision=prepared.connection_revision,
        repository=prepared.repository,
        mode=CONNECTION_MODE_RECONCILE_ONLY,
        vault_key="github-token",
        active_job_id=None,
        active_fence=None,
    )
    monkeypatch.setattr("src.extensions.github_followthrough.durable_job_repository", durable)
    monkeypatch.setattr(service, "_read_prepared", lambda _current: _async_value(prepared))
    monkeypatch.setattr(service, "_get_connection_row", lambda _owner: _async_value(connection))
    monkeypatch.setattr(service, "_load_token", lambda _connection: _async_value("secret-token"))
    monkeypatch.setattr(service, "_finalize_reconciled", lambda *_args, **_kwargs: _mark_success(durable))

    result = await service.reconcile(
        owner_principal_id=prepared.owner_principal_id,
        job_id=prepared.job_id,
        request=type("Reconcile", (), {"remote_id": 42})(),
    )

    assert result["status"] == "succeeded"
    assert calls == ["GET"]
    assert "secret-token" not in json.dumps(durable.current)


@pytest.mark.asyncio
async def test_cancelled_approval_releases_without_transport(monkeypatch):
    prepared = _prepared()
    durable = _MemoryDurableJobs(_job(prepared, status="awaiting_approval"))
    released: list[str] = []
    service = GitHubFollowthroughService()
    connection = SimpleNamespace(
        id=prepared.connection_id,
        active_job_id=prepared.job_id,
        active_fence=7,
    )
    monkeypatch.setattr("src.extensions.github_followthrough.durable_job_repository", durable)
    monkeypatch.setattr(service, "_read_prepared", lambda _current: _async_value(prepared))
    monkeypatch.setattr(service, "_get_connection_row", lambda _owner: _async_value(connection))
    async def release(**_kwargs):
        released.append("released")
        return True
    monkeypatch.setattr(service, "_release_connection", release)

    result = await service.cancel(owner_principal_id=prepared.owner_principal_id, job_id=prepared.job_id)

    assert result["status"] == "cancelled"
    assert released == ["released"]


@pytest.mark.asyncio
async def test_readback_retry_after_is_bounded_and_accepts_exact_issue():
    prepared = _prepared(issue_number=42)
    responses = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json={"number": 42, "title": prepared.title, "body": prepared.body}),
    ]
    sleeps: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        response = responses.pop(0)
        response.request = request
        return response

    async def sleep(delay: float):
        sleeps.append(delay)

    service = GitHubFollowthroughService(
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
        sleep=sleep,
    )
    verified, reason, payload = await service._readback(
        prepared,
        token="secret-token",
        remote_id=42,
        deadline=(datetime.now(timezone.utc) + timedelta(seconds=30)).timestamp(),
    )

    assert verified is True
    assert reason == "readback_verified"
    assert payload and payload["number"] == 42
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_comment_readback_requires_exact_parent_issue_url():
    prepared = _prepared(issue_number=42, action=ACTION_CREATE_COMMENT)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 77,
                "body": prepared.body,
                "issue_url": "https://api.github.com/repos/acme/example/issues/42",
            },
            request=request,
        )

    service = GitHubFollowthroughService(
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    )
    verified, reason, payload = await service._readback(
        prepared,
        token="secret-token",
        remote_id=77,
        deadline=(datetime.now(timezone.utc) + timedelta(seconds=30)).timestamp(),
    )

    assert verified is True
    assert reason == "readback_verified"
    assert payload and payload["id"] == 77


def _async_value(value):
    async def _value():
        return value
    return _value()


async def _mark_success(durable: _MemoryDurableJobs):
    durable.current["status"] = "succeeded"
    durable.current["revision"] += 1
    durable.current["lease"] = {"owner": None, "fencing_token": 1}
    return copy.deepcopy(durable.current)
