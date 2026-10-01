"""Whole Mail reply execution journeys through the durable Work Board path."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from starlette.requests import Request

from config.settings import settings
from src.api import mail as mail_api
from src.auth.service import AuthenticatedOperator
from src.db.models import (
    Goal,
    GoogleServiceConnection,
    MailLabelBinding,
    MailMessageBinding,
    MailReadConsent,
    OperatorSession,
    WorkBoardStatus,
    WorkBoardTask,
    WorkflowRunState,
)
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.integrations.gmail_read import GmailMessageBody, GmailMessageMetadata, GMAIL_READONLY_SCOPE
from src.model_fabric.configuration import (
    ModelFabricConfiguration,
    OpenRouterSetup,
    openrouter_profile_for_setup,
    write_model_fabric_configuration,
)
from src.model_fabric.proofs import build_model_route_proof
from src.model_fabric.receipts import RouteReceipt
from src.model_fabric.repository import model_fabric_repository
from src.model_fabric.contracts import EndpointClass
from src.security.trust_contract import EgressClass, AuthorityGrant, PrincipalType, TrustPrincipal
from src.vault import crypto as vault_crypto
from src.vault import encrypt
from src.work_board.contracts import WorkBoardOwner
from src.work_board.dispatcher import WorkBoardDispatcher
from src.work_board.repository import WorkBoardRepository
from src.workflows.job_runtime import DurableJobRepository


OWNER = "operator:single"
SESSION = "mail-reply-vertical-session"
GOAL = "mail-reply-vertical-goal"
CONNECTION = "mail-reply-vertical-connection"
CONSENT = "mail-reply-vertical-consent"
LABEL = "mail-reply-vertical-label"
BINDING = "mail-reply-vertical-binding"
MESSAGE_REVISION = "sha256:" + "d" * 64


@pytest.fixture(autouse=True)
def reset_vault_cipher(monkeypatch):
    """Keep the encrypted binding fixture and dispatcher on one test key."""

    monkeypatch.setattr(vault_crypto, "_fernet", None)


def _operator() -> AuthenticatedOperator:
    now = datetime.now(timezone.utc)
    return AuthenticatedOperator(
        session_id=SESSION,
        principal=TrustPrincipal(
            principal_id=OWNER,
            principal_type=PrincipalType.OPERATOR,
            authenticated=True,
            revoked=False,
            grants=(AuthorityGrant.INGRESS, AuthorityGrant.CAPABILITY_EXECUTE),
            session_id=SESSION,
            operator_session_id=SESSION,
        ),
        idle_expires_at=now + timedelta(hours=1),
        absolute_expires_at=now + timedelta(hours=2),
    )


def _request(body: dict[str, object]) -> Request:
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/capabilities/mail/reply-tasks",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 1234),
        },
        receive,
    )
    request.state.operator = _operator()
    return request


async def _seed(async_db, monkeypatch, *, model_allowed: bool = True):
    monkeypatch.setattr(mail_api, "get_session", async_db)
    now = datetime.now(timezone.utc)
    budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id="grant-mail-reply-vertical",
        max_outstanding_jobs=1,
        max_attempts=1,
        max_runtime_seconds=300,
        notifications_per_day=0,
        period_started_at=now - timedelta(minutes=1),
        period_expires_at=now + timedelta(days=2),
        timezone="UTC",
    )
    async with async_db() as db:
        db.add(
            OperatorSession(
                id=SESSION,
                token_hash="mail-reply-vertical-session-hash",
                idle_expires_at=now + timedelta(hours=1),
                absolute_expires_at=now + timedelta(hours=2),
            )
        )
        db.add(
            Goal(
                id=GOAL,
                title="Mail reply vertical goal",
                status="active",
                proactive_enabled=True,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                revision=1,
                admission_budget_json=serialize_admission_budget(budget),
            )
        )
        connection = GoogleServiceConnection(
            connection_id=CONNECTION,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
            service="gmail_readonly",
            state="active",
            revision=1,
            vault_secret_key="mail-reply-vertical-secret",
            declared_scopes_json=json.dumps([GMAIL_READONLY_SCOPE]),
        )
        db.add(connection)
        db.add(
            MailLabelBinding(
                label_id=LABEL,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                connection_id=CONNECTION,
                connection_revision=1,
                provider_label_id_ciphertext=encrypt("INBOX"),
                provider_label_digest="sha256:" + "a" * 64,
                label_name="Inbox",
                state="active",
            )
        )
        consent = MailReadConsent(
            consent_id=CONSENT,
            owner_principal_id=OWNER,
            owner_session_id=SESSION,
            connection_id=CONNECTION,
            connection_revision=1,
            goal_id=GOAL,
            goal_revision=1,
            label_ids_json=json.dumps([LABEL]),
            window_days=7,
            max_messages=10,
            source_read_allowed=True,
            model_egress_allowed=model_allowed,
            model_revision=1,
            model_digest="sha256:" + "e" * 64,
            allowed_body_fields_json=json.dumps(["subject", "plainbody", "replyintent"]),
            source_revision=1,
            source_digest="sha256:" + "b" * 64,
            expires_at=now + timedelta(days=1),
            state="active",
            revision=1,
        )
        db.add(consent)
        db.add(
            MailMessageBinding(
                message_binding_id=BINDING,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                connection_id=CONNECTION,
                connection_revision=1,
                source_consent_id=CONSENT,
                source_consent_revision=1,
                source_label_scope_digest=mail_api._source_label_scope_digest(connection, consent),
                provider_message_id_ciphertext=encrypt("provider-message-1"),
                provider_thread_id_ciphertext=encrypt("provider-thread-1"),
                message_key="message-key-1",
                thread_key="thread-key-1",
                message_revision=MESSAGE_REVISION,
                received_at=now,
                status="present",
                revision=1,
            )
        )
        await db.flush()


async def _configure_model_route(async_db, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "mail-reply-vertical-auth")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "openrouter_provider_only", True)
    setup = OpenRouterSetup(
        model_ids=("openrouter/z-ai/glm-5.3-flash",),
        capabilities=("text", "structured_output"),
        allowed_upstreams=("z-ai",),
        egress_class=EgressClass.CLOUD_ALLOWED_FULL,
        cloud_egress_acknowledged=True,
        spend_ceiling_microusd=25_000,
        credential_ref="env:OPENROUTER_API_KEY",
    )
    write_model_fabric_configuration(
        ModelFabricConfiguration(
            profiles=(openrouter_profile_for_setup(setup),),
            openrouter_setup=setup,
            status="ready",
        )
    )
    from src.llm_runtime import _provider_profile

    profile = _provider_profile("openrouter")
    assert profile is not None
    checked_at = time.time() - 1.0
    started = datetime.now(timezone.utc) - timedelta(seconds=1)
    receipt = RouteReceipt(
        receipt_id="mail-reply-vertical-probe-route",
        request_id="mail-reply-vertical-probe-request",
        route_decision_id="mail-reply-vertical-probe-decision",
        runtime_path="strategist_agent",
        workload="background",
        outcome="succeeded",
        egress_class=setup.egress_class.value,
        started_at=started,
        finished_at=started + timedelta(milliseconds=100),
        latency_ms=100,
        actual_profile_id=profile.id,
        actual_model=profile.model,
        actual_adapter=profile.transport_adapter,
        destination_class="remote",
        trust_decision_id="mail-reply-vertical-probe-trust",
    )
    assert (await model_fabric_repository.persist_route_receipt(receipt)).persisted is True
    for capability, proven_value in (
        ("text", "present"),
        ("structured_output", "json"),
        ("health", "healthy"),
        ("latency_ms", 100),
    ):
        proof = build_model_route_proof(
            profile=profile,
            endpoint_class=EndpointClass.REMOTE,
            adapter=profile.transport_adapter,
            capability=capability,
            canary_version="mail-reply-vertical-v1",
            outcome="passed",
            checked_at=checked_at,
            expires_at=checked_at + 3600,
            probe_receipt_id=receipt.receipt_id,
            probe_receipt_hash=receipt.receipt_hash,
            proven_value=proven_value,
        )
        assert (await model_fabric_repository.persist_capability_proof(proof)).persisted is True


def _reply_body(key: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "connection_id": CONNECTION,
        "expected_connection_revision": 1,
        "message_binding_id": BINDING,
        "expected_message_revision": MESSAGE_REVISION,
        "mail_consent_id": CONSENT,
        "expected_source_consent_revision": 1,
        "expected_model_consent_revision": 1,
        "goal_id": GOAL,
        "expected_goal_revision": 1,
        "reply_intent": "Keep this concise and ask for a reply next week.",
        "style": "brief",
        "idempotency_key": key,
    }


async def _create_claim(async_db, monkeypatch, key: str):
    created = await mail_api.create_reply_task(_request(_reply_body(key)))
    assert created.status_code == 201
    created_body = json.loads(created.body)
    repository = WorkBoardRepository()
    owner = WorkBoardOwner(principal_id=OWNER, session_id=SESSION)
    async with async_db() as db:
        task = await repository.get_task(db, owner, created_body["task_id"])
        promoted = await repository.promote_task_ready(
            db,
            task.task_id,
            expected_revision=task.task_revision,
            actor_principal_id=OWNER,
            actor_session_id=SESSION,
        )
        assert promoted is not None
        claim = await repository.claim_ready_task(
            db,
            task.task_id,
            expected_revision=promoted.task.task_revision,
            lease_owner="service:work-board",
            lease_seconds=300,
            actor_principal_id="service:work-board",
            actor_session_id="service-session:work-board",
        )
        assert claim is not None
        artifact = await db.get(mail_api.WorkBoardInputArtifact, created_body["input_artifact_id"])
        payload = mail_api._safe_file_bytes(
            mail_api._payload_path(artifact),
            expected_digest=artifact.payload_sha256,
            expected_size=artifact.size_bytes,
        )
        inputs = mail_api._decode_and_validate_payload(artifact, payload)
    return repository, owner, claim, inputs, created_body


def _body(read_index: int, *, changed: bool = False) -> GmailMessageBody:
    revision = MESSAGE_REVISION if not changed else "sha256:" + "f" * 64
    return GmailMessageBody(
        metadata=GmailMessageMetadata(
            provider_message_id="provider-message-1",
            provider_thread_id="provider-thread-1",
            subject="Architecture review",
            preview="Private preview",
            received_at=datetime.now(timezone.utc),
            read_status="unread",
            history_id=f"history-{read_index}",
            label_ids=("INBOX",),
            message_revision=revision,
        ),
        body="Private source body " + ("changed" if changed else "stable"),
        truncated=False,
    )


@pytest.mark.asyncio
async def test_mail_reply_dispatches_two_reads_one_model_private_readback(async_db, monkeypatch, tmp_path):
    await _seed(async_db, monkeypatch)
    await _configure_model_route(async_db, monkeypatch, tmp_path)
    repository, owner, claim, inputs, created = await _create_claim(async_db, monkeypatch, "reply-vertical-key")

    class FakeAdapter:
        reads = 0

        def __init__(self, _connection, *, contact_observer=None, **_kwargs):
            self.contact_observer = contact_observer

        async def get_message_full(self, _provider_message_id):
            type(self).reads += 1
            if self.contact_observer:
                self.contact_observer()
            return _body(type(self).reads)

    model_calls: list[dict[str, object]] = []
    output = {
        "subject": "Re: Architecture review",
        "body": "Thanks — I will follow up next week.",
        "caveats": ["Review before sending."],
    }

    def governed_transport(**kwargs):
        model_calls.append(dict(kwargs["body"]))
        content = json.dumps(output, separators=(",", ":"))
        message = SimpleNamespace(role="assistant", content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)]), {"choices": [{"message": {"role": "assistant", "content": content}}]}

    monkeypatch.setattr("src.integrations.gmail_read.GoogleGmailReadonlyAdapter", FakeAdapter)
    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", governed_transport)
    dispatcher = WorkBoardDispatcher(repository=repository, jobs=DurableJobRepository(), session_provider=async_db)
    result = await dispatcher._admit_execute_direct(claim, inputs, runtime_seconds=120)
    assert result["completed"] is True, result
    assert FakeAdapter.reads == 2
    assert len(model_calls) == 1

    async with async_db() as db:
        task = await repository.get_task(db, owner, created["task_id"])
        assert task.status is WorkBoardStatus.done
        from src.workflows.mail_reply_draft import reply_job_id

        root = (
            await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.run_identity
                    == reply_job_id(OWNER, created["task_id"], claim.attempt.attempt_id)
                )
            )
        ).scalar_one_or_none()
        assert root is not None
        assert root.status == "succeeded"
        assert any(
            effect.get("details", {}).get("memory_status") == "no_learning"
            for effect in json.loads(root.effect_receipts_json or "[]")
            if isinstance(effect, dict)
        )

    # The actual owner-scoped private route is exercised below with the same
    # authenticated request context; no generic WorkBoard projection is used.
    from src.api.mail import get_reply_draft

    request = _request({})
    request.scope["path"] = f"/api/capabilities/mail/reply-tasks/{created['task_id']}/draft"
    private = await get_reply_draft(request, created["task_id"])
    assert private["status"] == "verified"
    assert private["draft"]["subject"] == output["subject"]
    assert private["sent"] is False
    assert private["saved_to_provider"] is False
    assert private["memory_status"] == "no_learning"
    recovered = await mail_api.recover_reply_task(_request({}), "reply-vertical-key")
    assert recovered["status"] == "verified"
    assert recovered["task_id"] == created["task_id"]
    assert recovered["job_id"]
    assert recovered["recovery_action"] == "open_private_draft"
    assert "plainbody" not in json.dumps(recovered)


@pytest.mark.asyncio
async def test_mail_reply_truncated_source_blocks_before_model_contact(async_db, monkeypatch, tmp_path):
    await _seed(async_db, monkeypatch)
    await _configure_model_route(async_db, monkeypatch, tmp_path)
    repository, _owner, claim, inputs, _created = await _create_claim(async_db, monkeypatch, "reply-truncated-key")

    class TruncatedAdapter:
        reads = 0

        def __init__(self, _connection, **_kwargs):
            pass

        async def get_message_full(self, _provider_message_id):
            type(self).reads += 1
            return replace(_body(type(self).reads), truncated=True)

    model_calls = 0

    def governed_transport(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("a truncated reviewed source must be blocked before model contact")

    monkeypatch.setattr("src.integrations.gmail_read.GoogleGmailReadonlyAdapter", TruncatedAdapter)
    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", governed_transport)
    dispatcher = WorkBoardDispatcher(repository=repository, jobs=DurableJobRepository(), session_provider=async_db)
    result = await dispatcher._admit_execute_direct(claim, inputs, runtime_seconds=120)
    assert result == {"admitted": True, "blocked": True, "completed": False}
    assert TruncatedAdapter.reads == 1
    assert model_calls == 0


@pytest.mark.asyncio
async def test_mail_reply_oversized_source_blocks_before_model_contact(async_db, monkeypatch, tmp_path):
    await _seed(async_db, monkeypatch)
    await _configure_model_route(async_db, monkeypatch, tmp_path)
    repository, _owner, claim, inputs, _created = await _create_claim(async_db, monkeypatch, "reply-oversized-key")

    class OversizedAdapter:
        reads = 0

        def __init__(self, _connection, **_kwargs):
            pass

        async def get_message_full(self, _provider_message_id):
            type(self).reads += 1
            return replace(_body(type(self).reads), body="é" * 4097, truncated=False)

    model_calls = 0

    def governed_transport(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("an oversized reviewed source must be blocked before model contact")

    monkeypatch.setattr("src.integrations.gmail_read.GoogleGmailReadonlyAdapter", OversizedAdapter)
    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", governed_transport)
    dispatcher = WorkBoardDispatcher(repository=repository, jobs=DurableJobRepository(), session_provider=async_db)
    result = await dispatcher._admit_execute_direct(claim, inputs, runtime_seconds=120)
    assert result == {"admitted": True, "blocked": True, "completed": False}
    assert OversizedAdapter.reads == 1
    assert model_calls == 0


@pytest.mark.asyncio
async def test_mail_reply_dispatches_through_normal_work_board_pass(async_db, monkeypatch, tmp_path):
    """The managed dispatcher must execute the canonical Mail task path."""

    await _seed(async_db, monkeypatch)
    await _configure_model_route(async_db, monkeypatch, tmp_path)
    created_response = await mail_api.create_reply_task(_request(_reply_body("reply-dispatch-pass-key")))
    assert created_response.status_code == 201
    created = json.loads(created_response.body)

    class FakeAdapter:
        reads = 0

        def __init__(self, _connection, *, contact_observer=None, **_kwargs):
            self.contact_observer = contact_observer

        async def get_message_full(self, _provider_message_id):
            type(self).reads += 1
            if self.contact_observer:
                self.contact_observer()
            return _body(type(self).reads)

    output = {
        "subject": "Re: Architecture review",
        "body": "Thanks — I will follow up next week.",
        "caveats": ["Review before sending."],
    }

    def governed_transport(**kwargs):
        content = json.dumps(output, separators=(",", ":"))
        message = SimpleNamespace(role="assistant", content=content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)]), {"choices": [{"message": {"role": "assistant", "content": content}}]}

    async def authenticated(_session_id, *, touch=False):
        return _operator()

    monkeypatch.setattr("src.integrations.gmail_read.GoogleGmailReadonlyAdapter", FakeAdapter)
    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", governed_transport)
    monkeypatch.setattr("src.work_board.dispatcher.authenticate_session", authenticated)
    dispatcher = WorkBoardDispatcher(
        repository=WorkBoardRepository(),
        jobs=DurableJobRepository(),
        session_provider=async_db,
        runner_id="service:work-board",
    )

    receipt = await dispatcher.run_pass()
    assert receipt["claimed"] == 1, receipt
    assert receipt["admitted"] == 1, receipt
    assert receipt["completed"] == 1, receipt
    assert FakeAdapter.reads == 2

    async with async_db() as db:
        task = (
            await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == created["task_id"]))
        ).scalar_one_or_none()
        assert task is not None and task.status is WorkBoardStatus.done


@pytest.mark.asyncio
async def test_mail_reply_source_drift_after_model_quarantines_without_replay(async_db, monkeypatch, tmp_path):
    await _seed(async_db, monkeypatch)
    await _configure_model_route(async_db, monkeypatch, tmp_path)
    repository, _owner, claim, inputs, created = await _create_claim(async_db, monkeypatch, "reply-drift-key")

    class DriftAdapter:
        reads = 0

        def __init__(self, _connection, *, contact_observer=None, **_kwargs):
            self.contact_observer = contact_observer

        async def get_message_full(self, _provider_message_id):
            type(self).reads += 1
            if self.contact_observer:
                self.contact_observer()
            return _body(type(self).reads, changed=type(self).reads == 2)

    model_calls = 0
    output = {
        "subject": "Re: Architecture review",
        "body": "Draft must be quarantined.",
        "caveats": [],
    }

    def governed_transport(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        content = json.dumps(output, separators=(",", ":"))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(role="assistant", content=content))]), {}

    monkeypatch.setattr("src.integrations.gmail_read.GoogleGmailReadonlyAdapter", DriftAdapter)
    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", governed_transport)
    dispatcher = WorkBoardDispatcher(repository=repository, jobs=DurableJobRepository(), session_provider=async_db)
    result = await dispatcher._admit_execute_direct(claim, inputs, runtime_seconds=120)
    assert result["blocked"] is True, result
    assert DriftAdapter.reads == 2
    assert model_calls == 1
    async with async_db() as db:
        task = await repository.get_task(db, WorkBoardOwner(principal_id=OWNER, session_id=SESSION), created["task_id"])
        assert task.status is WorkBoardStatus.blocked
        assert task.artifact_refs_json in {None, "[]"}
        from src.workflows.mail_reply_draft import reply_job_id

        root = (
            await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.run_identity
                    == reply_job_id(OWNER, created["task_id"], claim.attempt.attempt_id)
                )
            )
        ).scalar_one_or_none()
        assert root is not None
        assert root.status == "blocked"


@pytest.mark.asyncio
async def test_mail_reply_consent_revoke_after_first_read_blocks_before_model(async_db, monkeypatch, tmp_path):
    await _seed(async_db, monkeypatch)
    await _configure_model_route(async_db, monkeypatch, tmp_path)
    repository, _owner, claim, inputs, created = await _create_claim(async_db, monkeypatch, "reply-revoke-key")

    class RevokingAdapter:
        reads = 0

        def __init__(self, _connection, *, contact_observer=None, **_kwargs):
            self.contact_observer = contact_observer

        async def get_message_full(self, _provider_message_id):
            type(self).reads += 1
            if self.contact_observer:
                self.contact_observer()
            async with async_db() as db:
                consent = await db.get(MailReadConsent, CONSENT)
                consent.model_egress_allowed = False
                consent.revision += 1
                await db.flush()
            return _body(type(self).reads)

    model_calls = 0

    def governed_transport(**_kwargs):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("revoked Mail consent must block before model contact")

    monkeypatch.setattr("src.integrations.gmail_read.GoogleGmailReadonlyAdapter", RevokingAdapter)
    monkeypatch.setattr("src.llm_runtime._governed_openai_chat_completion", governed_transport)
    dispatcher = WorkBoardDispatcher(repository=repository, jobs=DurableJobRepository(), session_provider=async_db)
    result = await dispatcher._admit_execute_direct(claim, inputs, runtime_seconds=120)
    assert result["blocked"] is True, result
    assert RevokingAdapter.reads == 1
    assert model_calls == 0
    async with async_db() as db:
        task = await repository.get_task(db, WorkBoardOwner(principal_id=OWNER, session_id=SESSION), created["task_id"])
        assert task.status is WorkBoardStatus.blocked
        assert task.artifact_refs_json in {None, "[]"}
