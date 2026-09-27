"""Zero-spend checks for the bounded GitHub follow-through adapter."""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.extensions.github_followthrough import (
    ACTION_CREATE_COMMENT,
    ACTION_CREATE_ISSUE,
    CONNECTION_MODE_RECONCILE_ONLY,
    GitHubFollowthroughError,
    GitHubFollowthroughService,
    PrepareRequest,
    PreparedPublication,
    _final_body,
    _followthrough_approval_scope,
    _marker,
    _operation_id,
    _sha,
    _consumed_approval_resume_is_current,
)
from src.approval.repository import fingerprint_tool_call
from src.db.models import GitHubFollowthroughConnection
from src.workflows.job_runtime import (
    DurableJobLeaseError,
    DurableJobRoutinePublicationAdmissionGuard,
)


async def _public_resolver(_hostname: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


@asynccontextmanager
async def _local_table_database(model):
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(model.__table__.create)

    @asynccontextmanager
    async def _get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    try:
        yield _get_session
    finally:
        await engine.dispose()


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
        "job_kind": "github_followthrough_v1",
        "goal_id": prepared.goal_id,
        "goal_revision": prepared.goal_revision,
        "plan_revision": prepared.plan_revision,
        "operator_session_id": prepared.owner_session_id,
        "authority_digest": "authority-1",
        "budget_digest": "budget-1",
        "capability_version": "1",
        "declared_authority": {"approval_id": "approval-1", "session_id": prepared.owner_session_id},
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


def test_followthrough_approval_scope_binds_exact_destination_and_payload_without_prose():
    scope = _followthrough_approval_scope(
        operation_id="operation-1",
        job_id="job-1",
        connection_id="connection-1",
        connection_revision=3,
        repository="seraph-quest/seraph-m6-fixture",
        action=ACTION_CREATE_ISSUE,
        issue_number=None,
        title_sha256=_sha("M6 local fixture"),
        body_sha256=_sha("private operator supplied test prose"),
        source_watch_id="watch-1",
        plan_revision=4,
        goal_id="goal-1",
        goal_revision=5,
        dossier_artifact_id="dossier-1",
        dossier_sha256="a" * 64,
    )

    assert scope["target"] == {
        "provider": "github",
        "repository": "seraph-quest/seraph-m6-fixture",
        "issue_number": None,
    }
    assert scope["action"] == ACTION_CREATE_ISSUE
    assert scope["authority"]["connection_revision"] == 3
    assert scope["authority"]["goal_revision"] == 5
    assert scope["authority"]["dossier_artifact_id"] == "dossier-1"
    assert scope["payload"] == {
        "title_sha256": _sha("M6 local fixture"),
        "body_sha256": _sha("private operator supplied test prose"),
    }
    assert "private operator supplied test prose" not in json.dumps(scope)

    fingerprint = fingerprint_tool_call(
        "github:followthrough",
        {"operation_id": "operation-1"},
        approval_context=scope,
    )
    changed_destination = {**scope, "target": {**scope["target"], "repository": "other/repo"}}
    assert fingerprint != fingerprint_tool_call(
        "github:followthrough",
        {"operation_id": "operation-1"},
        approval_context=changed_destination,
    )


@pytest.mark.asyncio
async def test_terminal_success_repairs_connection_fence_left_after_crash(monkeypatch):
    service = GitHubFollowthroughService()
    current = {
        "job_id": "ghfollow_success",
        "status": "succeeded",
        "owner": {"principal_id": "operator-1"},
        "declared_authority": {"connection_id": "connection-1", "principal": "operator-1"},
    }
    connection = SimpleNamespace(id="connection-1", active_job_id="ghfollow_success", active_fence=12)
    released: list[dict[str, object]] = []

    async def fake_connection(_owner_principal_id):
        return connection

    async def fake_release(**kwargs):
        released.append(kwargs)
        connection.active_job_id = None
        return True

    monkeypatch.setattr(service, "_get_connection_row", fake_connection)
    monkeypatch.setattr(service, "_release_connection", fake_release)

    assert await service._repair_terminal_connection_reservation(current) == "released"
    assert released == [
        {
            "connection_id": "connection-1",
            "owner_principal_id": "operator-1",
            "job_id": "ghfollow_success",
            "fence": 12,
        }
    ]


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
        self.current["failure_reason"] = _kwargs.get("reason")
        self.current["revision"] += 1
        return copy.deepcopy(self.current)

    async def resume_approved_job(self, _job_id: str, **_kwargs):
        self.current["status"] = "queued"
        self.current["revision"] += 1
        self.current["effects"].append(
            {
                "kind": "approval_resume",
                "status": "approved",
                "approval_id": _kwargs.get("approval_id"),
                "expires_at": _kwargs.get("expires_at"),
            }
        )
        return copy.deepcopy(self.current)

    async def transition_job(self, _job_id: str, status: str, **_kwargs):
        self.current["status"] = status
        self.current["failure_reason"] = _kwargs.get("reason")
        self.current["revision"] += 1
        if status in {"blocked", "cancelled", "unknown_external_effect"}:
            self.current["lease"] = {"owner": None, "fencing_token": 1}
        return copy.deepcopy(self.current)


@pytest.mark.parametrize("approval_status", ["approved", "consumed"])
@pytest.mark.asyncio
async def test_execute_posts_once_then_reads_back_exact_issue_without_secret_receipt(monkeypatch, approval_status):
    prepared = _prepared()
    durable = _MemoryDurableJobs(_job(prepared))
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=4)).timestamp()
    approval_details = {"approval_expires_at": expiry}
    if approval_status == "consumed":
        durable.current["effects"][0].update(
            {
                "approval_request_status": "consumed",
                "expires_at": expiry,
                "operator_principal_id": prepared.owner_principal_id,
                "operator_session_id": prepared.owner_session_id,
                "owner_kind": "user",
                "owner_principal_id": prepared.owner_principal_id,
                "authority_digest": "authority-1",
                "goal_id": prepared.goal_id,
                "goal_revision": prepared.goal_revision,
                "plan_revision": prepared.plan_revision,
                "capability_version": "1",
                "budget_digest": "budget-1",
            }
        )
        approval_details.update(
            {
                "durable_approval_id": "approval-1",
                "durable_job_id": prepared.job_id,
                "durable_owner_kind": "user",
                "durable_owner_principal_id": prepared.owner_principal_id,
                "durable_authority_digest": "authority-1",
                "durable_goal_id": prepared.goal_id,
                "durable_goal_revision": prepared.goal_revision,
                "durable_plan_revision": prepared.plan_revision,
                "durable_capability_version": "1",
                "durable_budget_digest": "budget-1",
                "operator_session_id": prepared.owner_session_id,
            }
        )
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
    monkeypatch.setattr(service, "_assert_dispatch_binding", lambda **_kwargs: _async_value(None))
    monkeypatch.setattr(service, "_load_token", lambda _connection: _async_value("secret-token"))
    monkeypatch.setattr(
        "src.extensions.github_followthrough.approval_repository.get",
        lambda _approval_id: _async_value(
            SimpleNamespace(
                id="approval-1",
                status=approval_status,
                owner_principal_id=prepared.owner_principal_id,
                operator_session_id=prepared.owner_session_id,
                session_id=prepared.owner_session_id,
                details_json=json.dumps(approval_details),
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
async def test_execute_revoked_routine_package_cancels_approved_pending_m3_without_transport(monkeypatch):
    """A package revoke wins before an approved M3 row can consume approval."""

    prepared = _prepared()
    durable = _MemoryDurableJobs(_job(prepared, status="awaiting_approval"))
    service = GitHubFollowthroughService()
    approval_get = AsyncMock(return_value=None)
    approval_resolve = AsyncMock(side_effect=AssertionError("missing approval must not be resolved"))
    monkeypatch.setattr("src.extensions.github_followthrough.durable_job_repository", durable)
    monkeypatch.setattr(service, "_read_prepared", lambda _current: _async_value(prepared))
    monkeypatch.setattr(
        service,
        "_current_routine_publication_binding",
        AsyncMock(side_effect=GitHubFollowthroughError("package_review_required")),
    )
    monkeypatch.setattr("src.extensions.github_followthrough.approval_repository.get", approval_get)
    monkeypatch.setattr("src.extensions.github_followthrough.approval_repository.resolve", approval_resolve)

    async def live_session(session_id: str, *, touch: bool = False):
        return SimpleNamespace(
            session_id=session_id,
            principal=SimpleNamespace(
                principal_id=prepared.owner_principal_id,
                grants={"external_mutation"},
            ),
        )

    monkeypatch.setattr("src.extensions.github_followthrough.authenticate_session", live_session)
    request = AsyncMock(side_effect=AssertionError("revoked routine package must not use transport"))
    monkeypatch.setattr(service, "_request", request)

    result = await service.execute(
        owner_principal_id=prepared.owner_principal_id,
        owner_session_id=prepared.owner_session_id,
        external_mutation_granted=True,
        job_id=prepared.job_id,
    )

    assert result["status"] == "cancelled"
    assert result["reason_code"] == "package_review_required"
    assert result["recovery_action"] == "restore_prerequisite"
    assert durable.current["failure_reason"] == "package_review_required"
    approval_get.assert_awaited_once_with("approval-1")
    approval_resolve.assert_not_awaited()
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_cancelled_routine_parent_cannot_resume_approved_m3(monkeypatch):
    """A cancelled M4 parent invalidates an approved M3 before any claim."""

    prepared = _prepared()
    durable = _MemoryDurableJobs(_job(prepared, status="awaiting_approval"))
    service = GitHubFollowthroughService()
    monkeypatch.setattr("src.extensions.github_followthrough.durable_job_repository", durable)
    monkeypatch.setattr(service, "_read_prepared", lambda _current: _async_value(prepared))
    monkeypatch.setattr(
        service,
        "_current_routine_publication_binding",
        AsyncMock(side_effect=GitHubFollowthroughError("routine_parent_child_not_current")),
    )
    approval_get = AsyncMock(return_value=None)
    approval_resolve = AsyncMock(side_effect=AssertionError("missing approval must not be resolved"))
    monkeypatch.setattr("src.extensions.github_followthrough.approval_repository.get", approval_get)
    monkeypatch.setattr("src.extensions.github_followthrough.approval_repository.resolve", approval_resolve)
    request = AsyncMock(side_effect=AssertionError("cancelled parent must not dispatch"))
    monkeypatch.setattr(service, "_request", request)

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

    assert result["status"] == "cancelled"
    assert result["reason_code"] == "routine_parent_child_not_current"
    assert result["recovery_action"] == "restore_prerequisite"
    approval_get.assert_awaited_once_with("approval-1")
    approval_resolve.assert_not_awaited()
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_rechecks_routine_package_before_dispatch_and_records_no_dispatch(monkeypatch):
    """A package revoke after claim closes the intent without a provider call."""

    prepared = _prepared()
    durable = _MemoryDurableJobs(_job(prepared, status="queued"))
    service = GitHubFollowthroughService()
    connection = SimpleNamespace(
        id=prepared.connection_id,
        revision=prepared.connection_revision,
        repository=prepared.repository,
        mode="active",
        vault_key="github-token",
        active_job_id=prepared.job_id,
        active_fence=1,
    )
    binding_check = AsyncMock(
        side_effect=[
            {"routine_id": "routine-1"},
            GitHubFollowthroughError("package_review_required"),
        ]
    )
    monkeypatch.setattr("src.extensions.github_followthrough.durable_job_repository", durable)
    monkeypatch.setattr(service, "_read_prepared", lambda _current: _async_value(prepared))
    monkeypatch.setattr(service, "_current_routine_publication_binding", binding_check)
    monkeypatch.setattr(service, "_get_connection_row", lambda _owner: _async_value(connection))
    monkeypatch.setattr(service, "_reserve_connection", lambda **_kwargs: _async_value(1))
    monkeypatch.setattr(service, "_verify_live_handoff", lambda *_args, **_kwargs: _async_value(None))
    monkeypatch.setattr(service, "_assert_dispatch_binding", lambda **_kwargs: _async_value(None))
    released: list[int] = []

    async def release(**_kwargs):
        released.append(int(_kwargs["fence"]))
        return True

    monkeypatch.setattr(service, "_release_connection", release)
    approval = SimpleNamespace(
        id="approval-1",
        status="approved",
        owner_principal_id=prepared.owner_principal_id,
        operator_session_id=prepared.owner_session_id,
        session_id=prepared.owner_session_id,
        details_json=json.dumps(
            {"approval_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=4)).timestamp()}
        ),
    )
    monkeypatch.setattr(
        "src.extensions.github_followthrough.approval_repository.get",
        lambda _approval_id: _async_value(approval),
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
    request = AsyncMock(side_effect=AssertionError("routine package revoke must block before POST"))
    monkeypatch.setattr(service, "_request", request)

    result = await service.execute(
        owner_principal_id=prepared.owner_principal_id,
        owner_session_id=prepared.owner_session_id,
        external_mutation_granted=True,
        job_id=prepared.job_id,
    )

    assert result["status"] == "blocked"
    assert result["reason_code"] == "package_review_required"
    assert result["recovery_action"] == "restore_prerequisite"
    assert any(
        item.get("receipt_kind") == "readback"
        and item.get("status") == "succeeded"
        and isinstance(item.get("details"), dict)
        and item["details"].get("no_dispatch") is True
        for item in durable.current["effects"]
    )
    assert released == [1]
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_rechecks_routine_package_after_approval_resume(monkeypatch):
    """A revoke after approval CAS cancels before claim, reservation, or POST."""

    prepared = _prepared()
    durable = _MemoryDurableJobs(_job(prepared, status="awaiting_approval"))
    service = GitHubFollowthroughService()
    binding_check = AsyncMock(
        side_effect=[
            None,
            GitHubFollowthroughError("package_review_required"),
        ]
    )
    monkeypatch.setattr("src.extensions.github_followthrough.durable_job_repository", durable)
    monkeypatch.setattr(service, "_read_prepared", lambda _current: _async_value(prepared))
    monkeypatch.setattr(service, "_current_routine_publication_binding", binding_check)
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=4)).timestamp()
    approval = SimpleNamespace(
        id="approval-1",
        status="approved",
        owner_principal_id=prepared.owner_principal_id,
        operator_session_id=prepared.owner_session_id,
        session_id=prepared.owner_session_id,
        details_json=json.dumps({"approval_expires_at": expiry}),
    )
    monkeypatch.setattr(
        "src.extensions.github_followthrough.approval_repository.get",
        lambda _approval_id: _async_value(approval),
    )
    monkeypatch.setattr(
        "src.extensions.github_followthrough.approval_repository.resolve",
        AsyncMock(side_effect=AssertionError("consumed approval must not be invalidated")),
    )
    monkeypatch.setattr(
        "src.extensions.github_followthrough.authenticate_session",
        lambda session_id, *, touch=False: _async_value(
            SimpleNamespace(
                session_id=session_id,
                principal=SimpleNamespace(
                    principal_id=prepared.owner_principal_id,
                    grants={"external_mutation"},
                ),
            )
        ),
    )
    monkeypatch.setattr(
        service,
        "_request",
        AsyncMock(side_effect=AssertionError("routine package revoke must block before POST")),
    )

    result = await service.execute(
        owner_principal_id=prepared.owner_principal_id,
        owner_session_id=prepared.owner_session_id,
        external_mutation_granted=True,
        job_id=prepared.job_id,
    )

    assert result["status"] == "cancelled"
    assert result["reason_code"] == "package_review_required"
    assert result["recovery_action"] == "restore_prerequisite"
    assert binding_check.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checkpoint_id", "expected_allowed"),
    [
        ("routine-child:prepared", True),
        ("routine-child:adoption_pending", False),
    ],
)
async def test_current_routine_binding_requires_recovered_child_approval_checkpoint(
    monkeypatch,
    checkpoint_id,
    expected_allowed,
):
    """A recovered approval-held child has no lease but keeps the parent fence."""

    service = GitHubFollowthroughService()
    parent_id = "routine-invocation:routine-1:invocation-1"
    invocation_uuid = "11111111-1111-4111-8111-111111111111"
    operation_uuid = "22222222-2222-4222-8222-222222222222"
    child_id = f"routine-child:{uuid.uuid5(uuid.UUID(invocation_uuid), 'seraph:guardian-routine:publication').hex}"
    m3_job_id = f"ghfollow_{_operation_id('operator-1', uuid.UUID(operation_uuid)).hex}"
    package_digest = "d" * 64
    binding = {
        "routine_id": "0123456789abcdef0123456789abcdef",
        "routine_revision": 7,
        "routine_version": 2,
        "package_digest": package_digest,
        "parent_invocation_job_id": parent_id,
        "publication_child_job_id": child_id,
        "invocation_uuid": invocation_uuid,
        "owner_principal_id": "operator-1",
        "owner_session_id": "session-1",
        "goal_id": "goal-1",
        "goal_revision": 3,
        "source_watch_id": "watch-1",
        "connection_id": "connection-1",
        "connection_revision": 2,
        "repository": "acme/example",
        "action": ACTION_CREATE_ISSUE,
        "operation_uuid": operation_uuid,
    }
    parent = {
        "job_id": parent_id,
        "run_identity": parent_id,
        "job_kind": "routine_invocation",
        "status": "running",
        "owner": {"kind": "user", "principal_id": "operator-1"},
        "operator_session_id": "session-1",
        "goal_id": "goal-1",
        "goal_revision": 3,
        "lease": {
            "owner": "routine:parent",
            "fencing_token": 9,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        },
        "declared_authority": {
            "routine_id": binding["routine_id"],
            "routine_revision": 7,
            "routine_version": 2,
            "package_digest": package_digest,
            "invocation_uuid": invocation_uuid,
            "source_watch_id": "watch-1",
            "github_connection_id": "connection-1",
            "github_connection_revision": 2,
            "github_repository": "acme/example",
            "github_action": ACTION_CREATE_ISSUE,
        },
    }
    child = {
        "job_id": child_id,
        "run_identity": child_id,
        "parent_job_id": parent_id,
        "parent_fencing_token": 9,
        "job_kind": "routine_github_followthrough_child",
        "status": "blocked",
        "owner": {"kind": "user", "principal_id": "operator-1"},
        "operator_session_id": "session-1",
        "goal_id": "goal-1",
        "goal_revision": 3,
        "lease": {"owner": None, "expires_at": None, "fencing_token": 3},
        "declared_authority": {
            "parent_job_id": parent_id,
            "parent_fencing_token": 9,
            "routine_invocation_job_id": parent_id,
            "routine_id": binding["routine_id"],
            "routine_revision": 7,
            "routine_version": 2,
            "package_digest": package_digest,
            "invocation_uuid": invocation_uuid,
            "source_watch_id": "watch-1",
            "github_connection_id": "connection-1",
            "github_connection_revision": 2,
            "github_repository": "acme/example",
            "github_action": ACTION_CREATE_ISSUE,
            "step_id": "github_followthrough",
            "m3_job_id": m3_job_id,
            "publication_operation_uuid": operation_uuid,
        },
        "checkpoints": [
            {
                "checkpoint_id": checkpoint_id,
                "payload": {"m3_job_id": m3_job_id, "approval_id": "approval-1"},
            }
        ],
    }
    current = {
        "job_id": m3_job_id,
        "status": "awaiting_approval",
        "declared_authority": {
            "session_id": "session-1",
            "approval_id": "approval-1",
            "routine_binding": binding,
        },
    }
    monkeypatch.setattr(
        "src.extensions.github_followthrough.durable_job_repository.get_job",
        AsyncMock(side_effect=[parent, child]),
    )

    class _ScalarResult:
        def __init__(self, value):
            self.value = value

        def scalars(self):
            return self

        def first(self):
            return self.value

    class _Session:
        def __init__(self):
            self.calls = 0

        async def execute(self, _statement):
            self.calls += 1
            return _ScalarResult(
                SimpleNamespace(
                    state="active",
                    revision=7,
                    current_version=2,
                    installed_package_digest=package_digest,
                )
                if self.calls == 1
                else SimpleNamespace(installed_package_digest=package_digest)
            )

    @asynccontextmanager
    async def fake_session():
        yield _Session()

    monkeypatch.setattr("src.extensions.github_followthrough.db_engine.get_session", fake_session)
    monkeypatch.setattr(
        "src.extensions.github_followthrough.CapabilityPackLifecycle.status",
        lambda _self, *_args, **_kwargs: {
            "active": {
                "pack_id": "seraph.routine.0123456789abcdef0123456789abcdef.v2",
                "status": "active",
                "version": "1.0.2",
                "digest": package_digest,
            },
            "revoked_digests": [],
        },
    )

    if expected_allowed:
        result = await service._current_routine_publication_binding(
            current,
            owner_principal_id="operator-1",
            owner_session_id="session-1",
        )
        assert result == binding
    else:
        with pytest.raises(GitHubFollowthroughError, match="routine_parent_child_not_current"):
            await service._current_routine_publication_binding(
                current,
                owner_principal_id="operator-1",
                owner_session_id="session-1",
            )


@pytest.mark.asyncio
async def test_prepare_guard_rejects_after_cancellation_wins_final_absent_read(monkeypatch):
    """A cancellation finishing after preflight cannot be followed by M3 insert."""

    owner = "operator:prepare-race"
    session_id = "session:prepare-race"
    operation_uuid = uuid.UUID("22222222-2222-4222-8222-222222222222")
    job_id = f"ghfollow_{_operation_id(owner, operation_uuid).hex}"
    parent_id = "routine-invocation:prepare-race"
    child_id = "routine-child:prepare-race"
    binding = {
        "routine_id": "0123456789abcdef0123456789abcdef",
        "routine_revision": 1,
        "routine_version": 1,
        "package_digest": "d" * 64,
        "parent_invocation_job_id": parent_id,
        "publication_child_job_id": child_id,
        "invocation_uuid": "11111111-1111-4111-8111-111111111111",
        "owner_principal_id": owner,
        "owner_session_id": session_id,
        "goal_id": "goal-prepare-race",
        "goal_revision": 3,
        "source_watch_id": "watch-prepare-race",
        "connection_id": "connection-prepare-race",
        "connection_revision": 1,
        "repository": "seraph-quest/seraph",
        "action": ACTION_CREATE_ISSUE,
        "operation_uuid": str(operation_uuid),
    }
    guard = DurableJobRoutinePublicationAdmissionGuard(
        routine_parent_job_id=parent_id,
        routine_parent_fencing_token=7,
        publication_child_job_id=child_id,
        publication_child_fencing_token=4,
        publication_child_parent_fencing_token=7,
        owner_principal_id=owner,
        owner_session_id=session_id,
    )
    preflight_started = asyncio.Event()
    release_preflight = asyncio.Event()
    cancellation_final_read = asyncio.Event()
    admitted_specs = []
    get_job_ids: list[str] = []
    artifact_calls: list[object] = []
    approval_calls: list[object] = []

    class _RaceDurable:
        async def get_job(self, requested_job_id):
            get_job_ids.append(requested_job_id)
            return None

        async def admit_job(self, spec):
            admitted_specs.append(spec)
            assert cancellation_final_read.is_set()
            raise DurableJobLeaseError(
                "routine publication admission child is not current"
            )

        async def record_artifact(self, *_args, **_kwargs):
            artifact_calls.append((_args, _kwargs))
            raise AssertionError("artifact must not be written after guarded admission rejection")

    durable = _RaceDurable()
    service = GitHubFollowthroughService()
    connection = SimpleNamespace(
        id="connection-prepare-race",
        revision=1,
        repository="seraph-quest/seraph",
        mode="active",
        vault_key="github-token",
    )
    packet = SimpleNamespace(plan_revision=4)
    watch = SimpleNamespace(id="watch-prepare-race", plan_revision=4)
    goal = SimpleNamespace(id="goal-prepare-race", revision=3)
    monkeypatch.setattr(
        "src.extensions.github_followthrough.durable_job_repository", durable
    )
    monkeypatch.setattr(
        "src.extensions.github_followthrough._require_live_owner_session",
        lambda **_kwargs: _async_value(None),
    )
    monkeypatch.setattr(service, "_get_connection_row", lambda _owner: _async_value(connection))
    monkeypatch.setattr(
        service,
        "_load_dossier",
        lambda **_kwargs: _async_value((packet, watch, goal, "verified dossier")),
    )
    monkeypatch.setattr(
        service,
        "_discover_routine_publication_binding",
        lambda **_kwargs: _async_value(binding),
    )

    async def live_preflight(_current, **_kwargs):
        preflight_started.set()
        await release_preflight.wait()
        # Return the legacy binding shape so prepare exercises its final
        # wrapper parent/child guard read immediately before admission.
        return binding

    monkeypatch.setattr(service, "_current_routine_publication_binding", live_preflight)
    async def final_guard_read(_binding, **_kwargs):
        assert await durable.get_job(parent_id) is None
        assert await durable.get_job(child_id) is None
        return guard

    monkeypatch.setattr(service, "_routine_publication_admission_guard", final_guard_read)
    class _NoApproval:
        async def get_or_create_pending(self, *_args, **_kwargs):
            approval_calls.append((_args, _kwargs))
            raise AssertionError("approval must not be created after guarded admission rejection")

    monkeypatch.setattr("src.extensions.github_followthrough.approval_repository", _NoApproval())
    monkeypatch.setattr(
        "src.extensions.github_followthrough.vault_repository.exists",
        lambda _key: _async_value(True),
    )

    async def cancellation_after_final_absent_read():
        await preflight_started.wait()
        assert await durable.get_job(job_id) is None
        # This event stands for the completed parent/child tree cancellation;
        # guarded admission must still reject when the preflight resumes.
        cancellation_final_read.set()

    cancellation_task = asyncio.create_task(cancellation_after_final_absent_read())
    prepare_task = asyncio.create_task(
        service.prepare(
            owner_principal_id=owner,
            owner_session_id=session_id,
            external_mutation_granted=True,
            request=PrepareRequest(
                conversation_id=session_id,
                goal_id="goal-prepare-race",
                goal_revision=3,
                dossier_artifact_id="dossier-prepare-race",
                dossier_sha256="a" * 64,
                connection_revision=1,
                action=ACTION_CREATE_ISSUE,
                title="Prepare race",
                body="bounded body",
                idempotency_key=str(operation_uuid),
            ),
        )
    )
    await asyncio.wait_for(preflight_started.wait(), timeout=2)
    await asyncio.wait_for(cancellation_task, timeout=2)
    release_preflight.set()
    with pytest.raises(DurableJobLeaseError, match="routine publication admission"):
        await asyncio.wait_for(prepare_task, timeout=2)
    assert admitted_specs
    assert admitted_specs[0].routine_publication_admission_guard == guard
    assert artifact_calls == []
    assert approval_calls == []
    assert await durable.get_job(job_id) is None
    assert get_job_ids == [job_id, job_id, parent_id, child_id, job_id]
    assert "approval" not in get_job_ids


def _prepare_race_request() -> PrepareRequest:
    return PrepareRequest(
        conversation_id="session-prepare-race",
        goal_id="goal-prepare-race",
        goal_revision=3,
        dossier_artifact_id="dossier-prepare-race",
        dossier_sha256="a" * 64,
        connection_revision=2,
        action=ACTION_CREATE_ISSUE,
        title="Prepare race",
        body="bounded body",
        idempotency_key="33333333-3333-4333-8333-333333333333",
    )


def _patch_prepare_race_dependencies(monkeypatch, service, durable):
    connection = SimpleNamespace(
        id="connection-prepare-race",
        revision=2,
        repository="acme/example",
        mode="active",
        vault_key="github-token",
    )
    packet = SimpleNamespace(plan_revision=4)
    watch = SimpleNamespace(id="watch-prepare-race", plan_revision=4)
    goal = SimpleNamespace(id="goal-prepare-race", revision=3)
    monkeypatch.setattr("src.extensions.github_followthrough.durable_job_repository", durable)
    monkeypatch.setattr(
        "src.extensions.github_followthrough._require_live_owner_session",
        lambda **_kwargs: _async_value(None),
    )
    monkeypatch.setattr(service, "_get_connection_row", lambda _owner: _async_value(connection))
    monkeypatch.setattr(
        service,
        "_load_dossier",
        lambda **_kwargs: _async_value((packet, watch, goal, "verified dossier")),
    )
    monkeypatch.setattr(
        service,
        "_discover_routine_publication_binding",
        lambda **_kwargs: _async_value(None),
    )
    monkeypatch.setattr(
        "src.extensions.github_followthrough.vault_repository.exists",
        lambda _key: _async_value(True),
    )
    return connection, packet, watch, goal


class _PrepareRaceDurable:
    def __init__(self, *, cancel_before_artifact: bool = False):
        self.cancel_before_artifact = cancel_before_artifact
        self.admitted = False
        self.current: dict[str, object] = {}
        self.artifacts: list[dict[str, object]] = []

    async def get_job(self, job_id):
        if not self.admitted or job_id != self.current.get("job_id"):
            return None
        return copy.deepcopy(self.current)

    async def admit_job(self, spec):
        self.admitted = True
        self.current = {
            "job_id": spec.identity.job_id,
            "status": "accepted",
            "owner": {"kind": "user", "principal_id": spec.identity.owner_principal_id},
            "operator_session_id": spec.operator_session_id,
            "session_id": spec.session_id,
            "goal_id": spec.goal_id,
            "goal_revision": spec.goal_revision,
            "plan_revision": spec.plan_revision,
            "capability_version": spec.identity.capability_version,
            "authority_digest": "authority-prepare-race",
            "budget_digest": "budget-prepare-race",
            "declared_authority": copy.deepcopy(spec.declared_authority),
            "revision": 1,
            "lease": {"owner": None, "fencing_token": 0},
            "effects": [],
            "artifacts": [],
            "checkpoints": [],
        }
        return copy.deepcopy(self.current)

    async def queue_job(self, _job_id, **_kwargs):
        self.current["status"] = "queued"
        self.current["revision"] = int(self.current["revision"]) + 1
        return copy.deepcopy(self.current)

    async def claim_job(self, _job_id, **_kwargs):
        self.current["status"] = "running"
        self.current["revision"] = int(self.current["revision"]) + 1
        self.current["lease"] = {
            "owner": "github-followthrough:prepare-race",
            "fencing_token": 1,
        }
        return copy.deepcopy(self.current)

    async def record_artifact(self, _job_id, **kwargs):
        if self.cancel_before_artifact:
            self.current["status"] = "cancelled"
            self.current["lease"] = {"owner": None, "fencing_token": 1}
            raise DurableJobLeaseError("artifact CAS lost to cancellation")
        artifact = {"file_path": kwargs["file_path"], "artifact_type": kwargs["artifact_type"]}
        self.artifacts.append(artifact)
        self.current["artifacts"] = copy.deepcopy(self.artifacts)
        self.current["revision"] = int(self.current["revision"]) + 1
        return {"receipt": {"artifact_id": "artifact-prepare-race"}, **artifact}

    async def record_checkpoint(self, _job_id, **kwargs):
        self.current["checkpoints"].append(
            {
                "checkpoint_id": kwargs["checkpoint_id"],
                "payload": kwargs.get("checkpoint_payload", {}),
            }
        )
        self.current["revision"] = int(self.current["revision"]) + 1
        return copy.deepcopy(self.current)

    async def bind_approval_id(self, _job_id, approval_id, **_kwargs):
        if self.current.get("status") != "running":
            raise DurableJobLeaseError("approval bind CAS lost to cancellation")
        authority = self.current["declared_authority"]
        authority["approval_id"] = approval_id
        self.current["revision"] = int(self.current["revision"]) + 1
        return copy.deepcopy(self.current)

    async def transition_job(self, _job_id, status, **_kwargs):
        if self.current.get("status") == "cancelled":
            raise DurableJobLeaseError("transition CAS lost to cancellation")
        self.current["status"] = status
        self.current["revision"] = int(self.current["revision"]) + 1
        return copy.deepcopy(self.current)


@pytest.mark.asyncio
async def test_prepare_cancellation_after_artifact_before_approval_denies_new_pending_row(
    monkeypatch, tmp_path
):
    """A pending approval created after cancellation cannot remain unbound."""

    durable = _PrepareRaceDurable()
    service = GitHubFollowthroughService()
    _patch_prepare_race_dependencies(monkeypatch, service, durable)
    payload_path = tmp_path / "prepare-race.json"
    monkeypatch.setattr(
        "src.extensions.github_followthrough._safe_resolve",
        lambda _path: payload_path,
    )

    def write_payload(_resolved, content, *, max_bytes):
        payload_path.write_text(content, encoding="utf-8")
        return len(content.encode("utf-8"))

    def read_payload(_resolved, *, max_bytes):
        return payload_path.read_text(encoding="utf-8"), False

    monkeypatch.setattr(
        "src.extensions.github_followthrough._write_workspace_text_bounded",
        write_payload,
    )
    monkeypatch.setattr(
        "src.extensions.github_followthrough._read_workspace_text_bounded",
        read_payload,
    )
    approval_calls: list[dict[str, object]] = []

    class ApprovalRepo:
        def __init__(self):
            self.row = None

        async def get_or_create_pending(self, **kwargs):
            approval_calls.append(kwargs)
            details = dict(kwargs["details"])
            approval_id = "approval-prepare-race"
            details.update({"approval_id": approval_id, "durable_approval_id": approval_id})
            self.row = SimpleNamespace(
                id=approval_id,
                status="pending",
                tool_name=kwargs["tool_name"],
                session_id=kwargs["session_id"],
                operator_session_id=kwargs["session_id"],
                owner_principal_id="operator:prepare-race",
                details_json=json.dumps(details),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=4),
            )
            # Cancellation wins after the row is inserted but before M3 bind.
            durable.current["status"] = "cancelled"
            durable.current["lease"] = {"owner": None, "fencing_token": 1}
            return self.row

        async def get(self, approval_id):
            assert approval_id == "approval-prepare-race"
            return self.row

        async def resolve(self, approval_id, decision):
            assert approval_id == "approval-prepare-race"
            self.row.status = decision
            return self.row

    approvals = ApprovalRepo()
    monkeypatch.setattr("src.extensions.github_followthrough.approval_repository", approvals)
    with pytest.raises(GitHubFollowthroughError, match="prepare_approval_bind_job_not_current"):
        await service.prepare(
            owner_principal_id="operator:prepare-race",
            owner_session_id="session-prepare-race",
            external_mutation_granted=True,
            request=_prepare_race_request(),
        )

    assert len(approval_calls) == 1
    assert approvals.row.status == "denied"
    assert approvals.row.status != "pending"
    assert durable.current["status"] == "cancelled"
    assert durable.artifacts


@pytest.mark.asyncio
async def test_prepare_cancellation_after_file_write_before_artifact_cas_removes_private_file(
    monkeypatch, tmp_path
):
    """A lost artifact CAS cannot strand the private payload file."""

    durable = _PrepareRaceDurable(cancel_before_artifact=True)
    service = GitHubFollowthroughService()
    _patch_prepare_race_dependencies(monkeypatch, service, durable)
    payload_path = tmp_path / "prepare-race.json"
    monkeypatch.setattr(
        "src.extensions.github_followthrough._safe_resolve",
        lambda _path: payload_path,
    )
    def write_payload(_resolved, content, *, max_bytes):
        payload_path.write_text(content, encoding="utf-8")
        return len(content.encode("utf-8"))

    def read_payload(_resolved, *, max_bytes):
        return payload_path.read_text(encoding="utf-8"), False

    monkeypatch.setattr(
        "src.extensions.github_followthrough._write_workspace_text_bounded",
        write_payload,
    )
    monkeypatch.setattr(
        "src.extensions.github_followthrough._read_workspace_text_bounded",
        read_payload,
    )
    monkeypatch.setattr(
        "src.extensions.github_followthrough.approval_repository",
        SimpleNamespace(),
    )

    with pytest.raises(DurableJobLeaseError, match="artifact CAS lost"):
        await service.prepare(
            owner_principal_id="operator:prepare-race",
            owner_session_id="session-prepare-race",
            external_mutation_granted=True,
            request=_prepare_race_request(),
        )

    assert not payload_path.exists()
    assert durable.artifacts == []
    assert durable.current["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cleanup_preserves_private_file_when_durable_read_is_unavailable(
    monkeypatch, tmp_path
):
    """Cleanup cannot delete a file while artifact ownership is unknowable."""

    payload_path = tmp_path / "unreadable-durable-state.json"
    payload_path.write_text("private payload", encoding="utf-8")

    class UnreadableDurable:
        async def get_job(self, _job_id):
            raise DurableJobLeaseError("durable read unavailable")

    service = GitHubFollowthroughService()
    monkeypatch.setattr(
        "src.extensions.github_followthrough.durable_job_repository",
        UnreadableDurable(),
    )
    monkeypatch.setattr(
        "src.extensions.github_followthrough._safe_resolve",
        lambda _path: payload_path,
    )

    await service._cleanup_uncommitted_payload_file(
        job_id="ghfollow-unreadable",
        path="github/followthrough/unreadable-durable-state.json",
    )

    assert payload_path.exists()
    assert payload_path.read_text(encoding="utf-8") == "private payload"


@pytest.mark.asyncio
async def test_cancel_denies_exact_pending_m3_approval_and_retry_is_idempotent(monkeypatch):
    prepared = _prepared()
    durable = _MemoryDurableJobs(_job(prepared, status="awaiting_approval"))
    details = {
        "approval_id": "approval-1",
        "durable_approval_id": "approval-1",
        "durable_job_id": prepared.job_id,
        "durable_owner_kind": "user",
        "durable_owner_principal_id": prepared.owner_principal_id,
        "operator_session_id": prepared.owner_session_id,
        "durable_authority_digest": "authority-1",
        "durable_goal_id": prepared.goal_id,
        "durable_goal_revision": prepared.goal_revision,
        "durable_plan_revision": prepared.plan_revision,
        "durable_capability_version": "1",
        "durable_budget_digest": "budget-1",
    }
    approval = SimpleNamespace(
        id="approval-1",
        status="pending",
        tool_name="github:followthrough",
        session_id=prepared.owner_session_id,
        operator_session_id=prepared.owner_session_id,
        owner_principal_id=prepared.owner_principal_id,
        details_json=json.dumps(details),
    )
    resolve_calls: list[tuple[str, str]] = []

    class _ApprovalRepo:
        async def get(self, approval_id):
            assert approval_id == "approval-1"
            return approval

        async def resolve(self, approval_id, decision):
            resolve_calls.append((approval_id, decision))
            approval.status = decision
            return approval

    monkeypatch.setattr("src.extensions.github_followthrough.durable_job_repository", durable)
    monkeypatch.setattr("src.extensions.github_followthrough.approval_repository", _ApprovalRepo())
    service = GitHubFollowthroughService()

    first = await service.cancel(
        owner_principal_id=prepared.owner_principal_id,
        owner_session_id=prepared.owner_session_id,
        job_id=prepared.job_id,
    )
    second = await service.cancel(
        owner_principal_id=prepared.owner_principal_id,
        owner_session_id=prepared.owner_session_id,
        job_id=prepared.job_id,
    )

    assert first["status"] == "cancelled"
    assert second["status"] == "cancelled"
    assert approval.status == "denied"
    assert resolve_calls == [("approval-1", "denied")]


@pytest.mark.parametrize("failure_mode", ["lookup", "resolve", "binding"])
@pytest.mark.asyncio
async def test_cancel_reports_unresolved_approval_cleanup_and_preserves_retryability(
    monkeypatch, failure_mode
):
    prepared = _prepared(job_id=f"ghfollow_cleanup_{failure_mode}")
    durable = _MemoryDurableJobs(_job(prepared, status="awaiting_approval"))
    durable.current["declared_authority"]["approval_id"] = "approval-cleanup"
    details = {
        "approval_id": "approval-cleanup",
        "durable_approval_id": "approval-cleanup",
        "durable_job_id": prepared.job_id,
        "durable_owner_kind": "user",
        "durable_owner_principal_id": prepared.owner_principal_id,
        "operator_session_id": prepared.owner_session_id,
        "durable_authority_digest": "authority-1",
        "durable_goal_id": prepared.goal_id,
        "durable_goal_revision": prepared.goal_revision,
        "durable_plan_revision": prepared.plan_revision,
        "durable_capability_version": "1",
        "durable_budget_digest": "budget-1",
    }
    if failure_mode == "binding":
        details["durable_job_id"] = "different-job"
    approval = SimpleNamespace(
        id="approval-cleanup",
        status="pending",
        tool_name="github:followthrough",
        session_id=prepared.owner_session_id,
        operator_session_id=prepared.owner_session_id,
        owner_principal_id=prepared.owner_principal_id,
        details_json=json.dumps(details),
    )
    get_calls = 0
    resolve_calls = 0

    class ApprovalRepo:
        async def get(self, approval_id):
            nonlocal get_calls
            assert approval_id == "approval-cleanup"
            get_calls += 1
            if failure_mode == "lookup" and get_calls == 1:
                raise RuntimeError("approval store unavailable")
            return approval

        async def resolve(self, approval_id, decision):
            nonlocal resolve_calls
            assert approval_id == "approval-cleanup"
            assert decision == "denied"
            resolve_calls += 1
            if failure_mode == "resolve" and resolve_calls == 1:
                raise RuntimeError("approval resolve unavailable")
            approval.status = decision
            return approval

    monkeypatch.setattr("src.extensions.github_followthrough.durable_job_repository", durable)
    monkeypatch.setattr("src.extensions.github_followthrough.approval_repository", ApprovalRepo())
    service = GitHubFollowthroughService()

    first = await service.cancel(
        owner_principal_id=prepared.owner_principal_id,
        owner_session_id=prepared.owner_session_id,
        job_id=prepared.job_id,
    )
    second = await service.cancel(
        owner_principal_id=prepared.owner_principal_id,
        owner_session_id=prepared.owner_session_id,
        job_id=prepared.job_id,
    )

    assert first["status"] == "blocked"
    assert first["durable_status"] == "cancelled"
    assert first["recovery_action"] == "reconcile_or_cancel"
    if failure_mode in {"lookup", "resolve"}:
        assert second["status"] == "cancelled"
        assert approval.status == "denied"
    else:
        assert second["status"] == "blocked"
        assert approval.status == "pending"


def test_consumed_approval_resume_rejects_stale_or_incomplete_binding():
    prepared = _prepared()
    projection = _job(prepared)
    projection.update({"authority_digest": "authority-1", "budget_digest": "budget-1"})
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=4)).timestamp()
    projection["effects"][0].update(
        {
            "approval_request_status": "consumed",
            "operator_principal_id": prepared.owner_principal_id,
            "operator_session_id": prepared.owner_session_id,
            "owner_kind": "user",
            "owner_principal_id": prepared.owner_principal_id,
            "authority_digest": "authority-1",
            "goal_id": prepared.goal_id,
            "goal_revision": prepared.goal_revision,
            "plan_revision": prepared.plan_revision,
            "capability_version": "1",
            "budget_digest": "budget-1",
            "expires_at": expiry,
        }
    )
    approval = SimpleNamespace(
        id="approval-1",
        status="consumed",
        owner_principal_id=prepared.owner_principal_id,
        operator_session_id=prepared.owner_session_id,
        details_json=json.dumps(
            {
                "approval_expires_at": expiry,
                "durable_approval_id": "approval-1",
                "durable_job_id": prepared.job_id,
                "durable_owner_kind": "user",
                "durable_owner_principal_id": prepared.owner_principal_id,
                "durable_authority_digest": "authority-1",
                "durable_goal_id": prepared.goal_id,
                "durable_goal_revision": prepared.goal_revision,
                "durable_plan_revision": prepared.plan_revision,
                "durable_capability_version": "1",
                "durable_budget_digest": "budget-1",
                "operator_session_id": prepared.owner_session_id,
            }
        ),
    )

    assert _consumed_approval_resume_is_current(projection, approval) is True
    for field, value in (
        ("approval_id", "approval-other"),
        ("operator_session_id", "session-other"),
        ("approval_request_status", "approved"),
        ("budget_digest", "budget-other"),
    ):
        stale = json.loads(json.dumps(projection))
        stale["effects"][0][field] = value
        assert _consumed_approval_resume_is_current(stale, approval) is False


@pytest.mark.asyncio
async def test_dispatch_binding_fence_rejects_connection_swap_before_post(monkeypatch):
    """A held reservation blocks reconfiguration and stale final dispatch."""

    service = GitHubFollowthroughService()
    monkeypatch.setattr("src.extensions.github_followthrough.vault_repository.exists", lambda _key: _async_value(True))
    async with _local_table_database(GitHubFollowthroughConnection) as get_session:
        monkeypatch.setattr("src.extensions.github_followthrough.db_engine.get_session", get_session)
        async with get_session() as db:
            db.add(
                GitHubFollowthroughConnection(
                    id="connection-race",
                    owner_principal_id="operator-1",
                    repository="acme/example",
                    vault_key="github-token",
                    revision=2,
                    mode="active",
                )
            )

        fence = await service._reserve_connection(
            owner_principal_id="operator-1",
            connection_id="connection-race",
            expected_revision=2,
            job_id="ghfollow-race",
        )
        with pytest.raises(GitHubFollowthroughError, match="connection_reserved"):
            await service.put_connection(
                owner_principal_id="operator-1",
                repository="acme/example",
                vault_key="rotated-token",
                mode="disabled",
                expected_revision=2,
            )

        await service._assert_dispatch_binding(
            owner_principal_id="operator-1",
            connection_id="connection-race",
            expected_revision=2,
            repository="acme/example",
            vault_key="github-token",
            mode="active",
            job_id="ghfollow-race",
            fence=fence,
        )
        assert await service._release_connection(
            connection_id="connection-race",
            owner_principal_id="operator-1",
            job_id="ghfollow-race",
            fence=fence,
        )
        changed = await service.put_connection(
            owner_principal_id="operator-1",
            repository="acme/example",
            vault_key="rotated-token",
            mode="active",
            expected_revision=2,
        )
        assert changed["revision"] == 3
        with pytest.raises(GitHubFollowthroughError, match="connection_dispatch_binding_stale"):
            await service._assert_dispatch_binding(
                owner_principal_id="operator-1",
                connection_id="connection-race",
                expected_revision=2,
                repository="acme/example",
                vault_key="github-token",
                mode="active",
                job_id="ghfollow-race",
                fence=fence,
            )


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
async def test_reconcile_rejects_remote_id_that_conflicts_with_effect(monkeypatch):
    prepared = _prepared(issue_number=42)
    current = _job(prepared, status="unknown_external_effect")
    current["effects"] = [
        {
            "effect_id": f"github:{prepared.operation_id}",
            "effect_type": "github_publication",
            "status": "unknown",
            "details": {"remote_id": 42},
        }
    ]
    durable = _MemoryDurableJobs(current)
    service = GitHubFollowthroughService()
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

    with pytest.raises(GitHubFollowthroughError, match="remote_id_binding_conflict"):
        await service.reconcile(
            owner_principal_id=prepared.owner_principal_id,
            job_id=prepared.job_id,
            request=type("Reconcile", (), {"remote_id": 99})(),
        )


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
    class _NoApproval:
        async def get(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr("src.extensions.github_followthrough.approval_repository", _NoApproval())
    monkeypatch.setattr(service, "_read_prepared", lambda _current: _async_value(prepared))
    monkeypatch.setattr(service, "_get_connection_row", lambda _owner: _async_value(connection))
    async def release(**_kwargs):
        released.append("released")
        return True
    monkeypatch.setattr(service, "_release_connection", release)

    result = await service.cancel(
        owner_principal_id=prepared.owner_principal_id,
        owner_session_id=prepared.owner_session_id,
        job_id=prepared.job_id,
    )

    assert result["status"] == "cancelled"
    assert released == ["released"]


@pytest.mark.asyncio
async def test_cancel_requires_persisted_owner_session(monkeypatch):
    prepared = _prepared()
    durable = _MemoryDurableJobs(_job(prepared, status="awaiting_approval"))
    service = GitHubFollowthroughService()
    monkeypatch.setattr("src.extensions.github_followthrough.durable_job_repository", durable)

    with pytest.raises(GitHubFollowthroughError, match="job_session_mismatch"):
        await service.cancel(
            owner_principal_id=prepared.owner_principal_id,
            owner_session_id="other-session",
            job_id=prepared.job_id,
        )


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
