"""M7 reply/watch contract and canonical metadata-watch vertical tests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from src.api import mail as mail_api
from src.auth.service import AuthenticatedOperator
from src.db.models import (
    Goal,
    GoogleServiceConnection,
    GuardianInboxDisposition,
    MailLabelBinding,
    MailMessageBinding,
    MailReadConsent,
    MailWatchState,
    OperatorSession,
    ScheduledJob,
    ScheduledJobRun,
    WorkBoardTask,
)
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.integrations.gmail_read import GmailMessageIdPage, GmailMessageMetadata, GMAIL_READONLY_SCOPE, message_key
from src.scheduler import scheduled_jobs
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.vault import crypto as vault_crypto
from src.work_board.dispatcher import TypedInputError, validate_capability_input
from src.workflows.mail_reply_draft import ReplyDraftOutput, parse_model_output
from src.guardian import inbox as inbox_service


OWNER = "operator:test-bypass"
SESSION = "test-auth-bypass"
GOAL = "mail-watch-goal"
CONNECTION = "mail-watch-connection"
CONSENT = "mail-watch-consent"
LABEL = "mail-label"


@pytest.fixture(autouse=True)
def reset_vault_cipher(monkeypatch):
    """Keep this file's test encryption from leaking a Fernet key to siblings."""

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


def _request(body: dict[str, object], operator: AuthenticatedOperator) -> Request:
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/capabilities/mail/watches",
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


async def _seed(async_db, monkeypatch):
    monkeypatch.setattr(mail_api, "get_session", async_db)
    now = datetime.now(timezone.utc)
    budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id="grant-mail-watch",
        max_outstanding_jobs=1,
        max_attempts=1,
        max_runtime_seconds=300,
        notifications_per_day=10,
        period_started_at=now - timedelta(minutes=1),
        period_expires_at=now + timedelta(days=2),
        timezone="UTC",
    )
    async with async_db() as db:
        db.add(
            OperatorSession(
                id=SESSION,
                token_hash="mail-watch-session-hash",
                idle_expires_at=now + timedelta(hours=1),
                absolute_expires_at=now + timedelta(hours=2),
            )
        )
        db.add(
            Goal(
                id=GOAL,
                title="Mail watch goal",
                status="active",
                proactive_enabled=True,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                revision=1,
                admission_budget_json=serialize_admission_budget(budget),
            )
        )
        db.add(
            GoogleServiceConnection(
                connection_id=CONNECTION,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                service="gmail_readonly",
                state="active",
                revision=1,
                vault_secret_key="mail-watch-secret",
                declared_scopes_json=json.dumps([GMAIL_READONLY_SCOPE]),
            )
        )
        db.add(
            MailLabelBinding(
                label_id=LABEL,
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                connection_id=CONNECTION,
                connection_revision=1,
                provider_label_id_ciphertext=mail_api.encrypt("INBOX"),
                provider_label_digest="sha256:" + "a" * 64,
                label_name="Inbox",
                state="active",
            )
        )
        db.add(
            MailReadConsent(
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
                source_revision=1,
                source_digest="sha256:" + "b" * 64,
                expires_at=now + timedelta(days=1),
                state="active",
                revision=1,
            )
        )
        await db.flush()


@pytest.mark.asyncio
async def test_mail_watch_input_is_metadata_only_and_strict():
    payload = {
        "schema_version": 1,
        "consent_id": CONSENT,
        "connection_id": CONNECTION,
        "goal_id": GOAL,
        "goal_revision": 1,
        "source_consent_revision": 1,
        "label_ids": [LABEL],
        "window_days": 7,
        "max_messages": 10,
    }
    assert validate_capability_input("gmail.scan_metadata.v1", payload, allow_scheduler=True) == payload
    with pytest.raises(TypedInputError):
        validate_capability_input("gmail.scan_metadata.v1", {**payload, "plainbody": "private"}, allow_scheduler=True)
    with pytest.raises(TypedInputError):
        validate_capability_input("gmail.scan_metadata.v1", {**payload, "max_messages": "10"}, allow_scheduler=True)


@pytest.mark.asyncio
async def test_reply_task_admission_replay_is_owner_bound_and_body_free(async_db, monkeypatch):
    await _seed(async_db, monkeypatch)
    message_revision = "sha256:" + "d" * 64
    async with async_db() as db:
        consent = await db.get(MailReadConsent, CONSENT)
        consent.model_egress_allowed = True
        consent.model_revision = 1
        consent.model_digest = "sha256:" + "e" * 64
        consent.allowed_body_fields_json = json.dumps(["subject", "plainbody", "replyintent"])
        connection = await db.get(GoogleServiceConnection, CONNECTION)
        db.add(
            MailMessageBinding(
                message_binding_id="mail-reply-binding",
                owner_principal_id=OWNER,
                owner_session_id=SESSION,
                connection_id=CONNECTION,
                connection_revision=1,
                source_consent_id=CONSENT,
                source_consent_revision=1,
                source_label_scope_digest=mail_api._source_label_scope_digest(connection, consent),
                provider_message_id_ciphertext=mail_api.encrypt("provider-message-1"),
                provider_thread_id_ciphertext=mail_api.encrypt("provider-thread-1"),
                message_key="message-key-1",
                thread_key="thread-key-1",
                message_revision=message_revision,
                received_at=datetime.now(timezone.utc),
                status="present",
                revision=1,
            )
        )
        await db.flush()

    body = {
        "schema_version": 1,
        "connection_id": CONNECTION,
        "expected_connection_revision": 1,
        "message_binding_id": "mail-reply-binding",
        "expected_message_revision": message_revision,
        "mail_consent_id": CONSENT,
        "expected_source_consent_revision": 1,
        "expected_model_consent_revision": 1,
        "goal_id": GOAL,
        "expected_goal_revision": 1,
        "reply_intent": "Keep this concise and ask for a reply next week.",
        "style": "brief",
        "idempotency_key": "reply-task-key",
    }
    created = await mail_api.create_reply_task(_request(body, _operator()))
    assert created.status_code == 201
    created_body = json.loads(created.body)
    task_id = created_body["task_id"]
    assert created_body["source_status"] == "ready"
    assert created_body["memory_status"] == "no_learning"

    replay = await mail_api.create_reply_task(_request(body, _operator()))
    assert replay.status_code == 200
    replay_body = json.loads(replay.body)
    assert replay_body["task_id"] == task_id
    assert replay_body["input_artifact_id"] == created_body["input_artifact_id"]

    async with async_db() as db:
        task_rows = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.owner_principal_id == OWNER,
                    WorkBoardTask.owner_session_id == SESSION,
                    WorkBoardTask.capability_id == "work.mail-reply-draft.v1",
                )
            )
        ).scalars().all()
        assert len(task_rows) == 1
        artifact = await db.get(mail_api.WorkBoardInputArtifact, created_body["input_artifact_id"])
        assert artifact is not None
        payload_bytes = mail_api._safe_file_bytes(
            mail_api._payload_path(artifact),
            expected_digest=artifact.payload_sha256,
            expected_size=artifact.size_bytes,
        )
        decoded = mail_api._decode_and_validate_payload(artifact, payload_bytes)
        assert decoded["reply_intent"] == body["reply_intent"]
        assert "provider-message-1" not in json.dumps(decoded)
        assert "provider_message_id" not in decoded


@pytest.mark.asyncio
async def test_watch_baseline_restart_deduplicates_metadata_notice(async_db, monkeypatch):
    await _seed(async_db, monkeypatch)
    operator = _operator()
    create_body = {
        "schema_version": 1,
        "connection_id": CONNECTION,
        "expected_connection_revision": 1,
        "mail_consent_id": CONSENT,
        "expected_source_consent_revision": 1,
        "goal_id": GOAL,
        "expected_goal_revision": 1,
        "label_ids": [LABEL],
        "cadence": "hourly",
        "timezone": "UTC",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(),
        "max_messages": 10,
        "idempotency_key": "watch-key",
    }
    created = await mail_api.create_mail_watch(_request(create_body, operator))
    assert created.status_code == 201
    replay = await mail_api.create_mail_watch(_request(create_body, operator))
    assert replay["status"] == "replayed"

    async with async_db() as db:
        binding = (await db.execute(select(mail_api.GovernedScheduleBinding))).scalar_one()
        job = await db.get(ScheduledJob, binding.scheduled_job_id)
        now = datetime.now(timezone.utc)
        run = ScheduledJobRun(
            id="mail-watch-run-1",
            scheduled_job_id=job.id,
            job_name=job.name,
            trigger_type="governed",
            action_type="gmail.scan_metadata.v1",
            status="started",
            started_at=now,
            metadata_json="{}",
        )
        db.add(run)
        await db.flush()
        job_payload = {
            "id": job.id,
            "enabled": True,
            "trigger_type": job.trigger_type,
            "action_type": job.action_type,
            "action_spec": json.loads(job.action_spec_json),
            "trigger_spec": json.loads(job.trigger_spec_json),
        }

    class FakeAdapter:
        def __init__(self, _connection, *, contact_observer=None, **_kwargs):
            self._contact_observer = contact_observer
            self.round = 0

        async def list_message_ids(self, _labels, *, received_after, max_messages):
            if self._contact_observer:
                self._contact_observer()
            if self.round == 0:
                ids = ("m1",)
            elif self.round == 1:
                ids = ("m1", "m2", "m3", "m4")
            else:
                ids = ("m1", "m2", "m3", "m4", "m5", "m6")
            return GmailMessageIdPage(ids, None)

        async def get_message_metadata(self, provider_message_id):
            if self._contact_observer:
                self._contact_observer()
            return GmailMessageMetadata(
                provider_message_id=provider_message_id,
                provider_thread_id="thread-" + provider_message_id,
                subject="private subject",
                preview="private preview",
                received_at=datetime.now(timezone.utc),
                read_status="unread",
                history_id="h-" + provider_message_id,
                label_ids=("INBOX",),
                message_revision="sha256:" + provider_message_id * 64,
            )

        async def get_message_full(self, *_args, **_kwargs):
            raise AssertionError("metadata watch must never fetch message bodies")

    fake = FakeAdapter(None)
    monkeypatch.setattr(scheduled_jobs, "GoogleGmailReadonlyAdapter", FakeAdapter, raising=False)
    # The handler imports the adapter at call time, so patch the source module.
    monkeypatch.setattr("src.integrations.gmail_read.GoogleGmailReadonlyAdapter", lambda *args, **kwargs: fake)
    first = await scheduled_jobs._run_governed_mail_metadata_scan(
        job_payload,
        scheduled_slot_utc=now.replace(minute=0, second=0, microsecond=0),
        scheduled_run_id="mail-watch-run-1",
    )
    assert first["baseline_complete"] is True
    assert first["notice_count"] == 0

    async with async_db() as db:
        run2 = ScheduledJobRun(
            id="mail-watch-run-2",
            scheduled_job_id=job.id,
            job_name=job.name,
            trigger_type="governed",
            action_type="gmail.scan_metadata.v1",
            status="started",
            started_at=now,
            metadata_json="{}",
        )
        db.add(run2)
        await db.flush()
    fake.round = 1
    second = await scheduled_jobs._run_governed_mail_metadata_scan(
        job_payload,
        scheduled_slot_utc=now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1),
        scheduled_run_id="mail-watch-run-2",
    )
    assert second["new_count"] == 3
    assert second["notice_count"] == 3
    async with async_db() as db:
        notices = (
            await db.execute(
                select(GuardianInboxDisposition).where(
                    GuardianInboxDisposition.source_kind == "mail_notice",
                    GuardianInboxDisposition.owner_principal_id == OWNER,
                )
            )
        ).scalars().all()
        assert len(notices) == 3
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.owner_principal_id == OWNER,
                    WorkBoardTask.capability_id.is_(None),
                )
            )
        ).scalars().all()
        assert tasks == []
        state = await db.get(MailWatchState, binding.binding_id)
        assert json.loads(state.seen_message_keys_json)

    page = await inbox_service.list_owned_items(owner_principal_id=OWNER, owner_session_id=SESSION)
    assert len(page["items"]) == 3
    notice = page["items"][0]
    assert notice["source_kind"] == "mail_notice"
    assert notice["allowed_actions"] == ["accept_followup", "snooze", "dismiss"]
    accepted = await inbox_service.apply_action(
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        item_id=notice["id"],
        action="accept_followup",
        expected_revision=notice["revision"],
        idempotency_key="mail-notice-accept",
    )
    assert accepted["state"] == "accepted"
    assert accepted["task_id"]
    replay_accept = await inbox_service.apply_action(
        owner_principal_id=OWNER,
        owner_session_id=SESSION,
        item_id=notice["id"],
        action="accept_followup",
        expected_revision=notice["revision"],
        idempotency_key="mail-notice-accept",
    )
    assert replay_accept == accepted
    async with async_db() as db:
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.owner_principal_id == OWNER,
                    WorkBoardTask.capability_id.is_(None),
                )
            )
        ).scalars().all()
        assert len(tasks) == 1
        assert "private" not in tasks[0].title.casefold()

    async with async_db() as db:
        run3 = ScheduledJobRun(
            id="mail-watch-run-3",
            scheduled_job_id=job.id,
            job_name=job.name,
            trigger_type="governed",
            action_type="gmail.scan_metadata.v1",
            status="started",
            started_at=now,
            metadata_json="{}",
        )
        db.add(run3)
        await db.flush()
    fake.round = 2
    third = await scheduled_jobs._run_governed_mail_metadata_scan(
        job_payload,
        scheduled_slot_utc=now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=2),
        scheduled_run_id="mail-watch-run-3",
    )
    assert third["new_count"] == 2
    assert third["notice_count"] == 0


@pytest.mark.asyncio
async def test_watch_zero_notification_budget_never_projects_notice(async_db, monkeypatch):
    await _seed(async_db, monkeypatch)
    now = datetime.now(timezone.utc)
    zero_budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id="grant-mail-watch-zero",
        max_outstanding_jobs=1,
        max_attempts=1,
        max_runtime_seconds=300,
        notifications_per_day=0,
        period_started_at=now - timedelta(minutes=1),
        period_expires_at=now + timedelta(days=2),
        timezone="UTC",
    )
    async with async_db() as db:
        goal = await db.get(Goal, GOAL)
        goal.admission_budget_json = serialize_admission_budget(zero_budget)
        await db.flush()

    body = {
        "schema_version": 1,
        "connection_id": CONNECTION,
        "expected_connection_revision": 1,
        "mail_consent_id": CONSENT,
        "expected_source_consent_revision": 1,
        "goal_id": GOAL,
        "expected_goal_revision": 1,
        "label_ids": [LABEL],
        "cadence": "hourly",
        "timezone": "UTC",
        "expires_at": (now + timedelta(hours=6)).isoformat(),
        "max_messages": 10,
        "idempotency_key": "watch-zero-key",
    }
    created = await mail_api.create_mail_watch(_request(body, _operator()))
    assert created.status_code == 201
    async with async_db() as db:
        binding = (await db.execute(select(mail_api.GovernedScheduleBinding))).scalar_one()
        job = await db.get(ScheduledJob, binding.scheduled_job_id)
        run = ScheduledJobRun(
            id="mail-watch-zero-run-1",
            scheduled_job_id=job.id,
            job_name=job.name,
            trigger_type="governed",
            action_type="gmail.scan_metadata.v1",
            status="started",
            started_at=now,
            metadata_json="{}",
        )
        db.add(run)
        await db.flush()
        job_payload = {
            "id": job.id,
            "enabled": True,
            "trigger_type": job.trigger_type,
            "action_type": job.action_type,
            "action_spec": json.loads(job.action_spec_json),
            "trigger_spec": json.loads(job.trigger_spec_json),
        }

    class FakeAdapter:
        def __init__(self, _connection, *, contact_observer=None, **_kwargs):
            self._contact_observer = contact_observer
            self.round = 0

        async def list_message_ids(self, _labels, *, received_after, max_messages):
            if self._contact_observer:
                self._contact_observer()
            return GmailMessageIdPage(("zero-message",), None)

        async def get_message_metadata(self, provider_message_id):
            if self._contact_observer:
                self._contact_observer()
            return GmailMessageMetadata(
                provider_message_id=provider_message_id,
                provider_thread_id="zero-thread",
                subject="private",
                preview="private",
                received_at=now,
                read_status="unread",
                history_id="zero-history",
                label_ids=("INBOX",),
                message_revision="sha256:" + "c" * 64,
            )

    fake = FakeAdapter(None)
    monkeypatch.setattr("src.integrations.gmail_read.GoogleGmailReadonlyAdapter", lambda *args, **kwargs: fake)
    first = await scheduled_jobs._run_governed_mail_metadata_scan(
        job_payload,
        scheduled_slot_utc=now.replace(minute=0, second=0, microsecond=0),
        scheduled_run_id="mail-watch-zero-run-1",
    )
    assert first["baseline_complete"] is True
    async with async_db() as db:
        run2 = ScheduledJobRun(
            id="mail-watch-zero-run-2",
            scheduled_job_id=job.id,
            job_name=job.name,
            trigger_type="governed",
            action_type="gmail.scan_metadata.v1",
            status="started",
            started_at=now,
            metadata_json="{}",
        )
        db.add(run2)
        await db.flush()
    second = await scheduled_jobs._run_governed_mail_metadata_scan(
        job_payload,
        scheduled_slot_utc=now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1),
        scheduled_run_id="mail-watch-zero-run-2",
    )
    assert second["new_count"] == 0
    assert second["notice_count"] == 0
    async with async_db() as db:
        notices = (
            await db.execute(
                select(GuardianInboxDisposition).where(
                    GuardianInboxDisposition.source_kind == "mail_notice",
                )
            )
        ).scalars().all()
        assert notices == []


@pytest.mark.asyncio
async def test_watch_quiet_hours_blocks_before_provider_contact(async_db, monkeypatch):
    await _seed(async_db, monkeypatch)
    now = datetime.now(timezone.utc)
    quiet_budget = GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id="grant-mail-watch-quiet",
        max_outstanding_jobs=1,
        max_attempts=1,
        max_runtime_seconds=300,
        notifications_per_day=3,
        period_started_at=now - timedelta(minutes=1),
        period_expires_at=now + timedelta(days=2),
        timezone="UTC",
        quiet_hours_start=now.hour,
        quiet_hours_end=(now.hour + 1) % 24,
    )
    async with async_db() as db:
        goal = await db.get(Goal, GOAL)
        goal.admission_budget_json = serialize_admission_budget(quiet_budget)
        await db.flush()

    body = {
        "schema_version": 1,
        "connection_id": CONNECTION,
        "expected_connection_revision": 1,
        "mail_consent_id": CONSENT,
        "expected_source_consent_revision": 1,
        "goal_id": GOAL,
        "expected_goal_revision": 1,
        "label_ids": [LABEL],
        "cadence": "hourly",
        "timezone": "UTC",
        "expires_at": (now + timedelta(hours=6)).isoformat(),
        "max_messages": 10,
        "idempotency_key": "watch-quiet-key",
    }
    created = await mail_api.create_mail_watch(_request(body, _operator()))
    assert created.status_code == 201
    async with async_db() as db:
        binding = (await db.execute(select(mail_api.GovernedScheduleBinding))).scalar_one()
        job = await db.get(ScheduledJob, binding.scheduled_job_id)
        run = ScheduledJobRun(
            id="mail-watch-quiet-run-1",
            scheduled_job_id=job.id,
            job_name=job.name,
            trigger_type="governed",
            action_type="gmail.scan_metadata.v1",
            status="started",
            started_at=now,
            metadata_json="{}",
        )
        db.add(run)
        await db.flush()
        job_payload = {
            "id": job.id,
            "enabled": True,
            "trigger_type": job.trigger_type,
            "action_type": job.action_type,
            "action_spec": json.loads(job.action_spec_json),
            "trigger_spec": json.loads(job.trigger_spec_json),
        }

    class NeverContactAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

        async def list_message_ids(self, *_args, **_kwargs):
            raise AssertionError("quiet-hours admission must stop before provider contact")

    monkeypatch.setattr("src.integrations.gmail_read.GoogleGmailReadonlyAdapter", NeverContactAdapter)
    result = await scheduled_jobs._run_governed_mail_metadata_scan(
        job_payload,
        scheduled_slot_utc=now.replace(minute=0, second=0, microsecond=0),
        scheduled_run_id="mail-watch-quiet-run-1",
    )
    assert result["status"] == "blocked"
    assert result["failure_code"] == "goal_quiet_hours"
    assert result["provider_contact"] is False
    async with async_db() as db:
        occurrence = (
            await db.execute(
                select(mail_api.GovernedScheduleOccurrence).where(
                    mail_api.GovernedScheduleOccurrence.binding_id == binding.binding_id,
                )
            )
        ).scalar_one()
        assert occurrence.state == "blocked"


@pytest.mark.asyncio
async def test_watch_seen_cursor_overflow_blocks_without_eviction(async_db, monkeypatch):
    await _seed(async_db, monkeypatch)
    now = datetime.now(timezone.utc)
    body = {
        "schema_version": 1,
        "connection_id": CONNECTION,
        "expected_connection_revision": 1,
        "mail_consent_id": CONSENT,
        "expected_source_consent_revision": 1,
        "goal_id": GOAL,
        "expected_goal_revision": 1,
        "label_ids": [LABEL],
        "cadence": "hourly",
        "timezone": "UTC",
        "expires_at": (now + timedelta(hours=6)).isoformat(),
        "max_messages": 10,
        "idempotency_key": "watch-overflow-key",
    }
    created = await mail_api.create_mail_watch(_request(body, _operator()))
    assert created.status_code == 201
    seen = [f"opaque-seen-{index}" for index in range(512)]
    async with async_db() as db:
        binding = (await db.execute(select(mail_api.GovernedScheduleBinding))).scalar_one()
        state = await db.get(MailWatchState, binding.binding_id)
        state.baseline_complete = True
        state.state = "active"
        state.seen_message_keys_json = json.dumps(seen)
        job = await db.get(ScheduledJob, binding.scheduled_job_id)
        run = ScheduledJobRun(
            id="mail-watch-overflow-run-1",
            scheduled_job_id=job.id,
            job_name=job.name,
            trigger_type="governed",
            action_type="gmail.scan_metadata.v1",
            status="started",
            started_at=now,
            metadata_json="{}",
        )
        db.add(run)
        await db.flush()
        job_payload = {
            "id": job.id,
            "enabled": True,
            "trigger_type": job.trigger_type,
            "action_type": job.action_type,
            "action_spec": json.loads(job.action_spec_json),
            "trigger_spec": json.loads(job.trigger_spec_json),
        }

    class OverflowAdapter:
        def __init__(self, _connection, *, contact_observer=None, **_kwargs):
            self._contact_observer = contact_observer

        async def list_message_ids(self, _labels, *, received_after, max_messages):
            if self._contact_observer:
                self._contact_observer()
            return GmailMessageIdPage(("overflow-message",), None)

        async def get_message_metadata(self, provider_message_id):
            if self._contact_observer:
                self._contact_observer()
            return GmailMessageMetadata(
                provider_message_id=provider_message_id,
                provider_thread_id="overflow-thread",
                subject="private",
                preview="private",
                received_at=now,
                read_status="unread",
                history_id="overflow-history",
                label_ids=("INBOX",),
                message_revision="sha256:" + "f" * 64,
            )

    monkeypatch.setattr("src.integrations.gmail_read.GoogleGmailReadonlyAdapter", OverflowAdapter)
    result = await scheduled_jobs._run_governed_mail_metadata_scan(
        job_payload,
        scheduled_slot_utc=now.replace(minute=0, second=0, microsecond=0),
        scheduled_run_id="mail-watch-overflow-run-1",
    )
    assert result["status"] == "blocked"
    assert result["failure_code"] == "mail_seen_cursor_capacity_exceeded"
    async with async_db() as db:
        state = await db.get(MailWatchState, binding.binding_id)
        assert json.loads(state.seen_message_keys_json) == seen
        assert state.state == "coverage_blocked"
        assert state.skipped_coverage_reason == "mail_seen_cursor_capacity_exceeded"
        assert (
            await db.execute(
                select(MailMessageBinding).where(MailMessageBinding.message_key == message_key(OWNER, CONNECTION, "overflow-message"))
            )
        ).scalar_one_or_none() is None


def test_reply_output_remains_bounded_and_revision_bound():
    output = parse_model_output(
        {"schema_version": 1, "message_revision": "sha256:" + "a" * 64, "subject": "Reply", "plainbody": "Body", "caveats": []},
        expected_message_revision="sha256:" + "a" * 64,
    )
    assert isinstance(output, ReplyDraftOutput)
    with pytest.raises(ValueError):
        parse_model_output(
            {"schema_version": 1, "message_revision": "sha256:" + "a" * 64, "subject": "Reply", "plainbody": "Body", "caveats": [], "send": True},
            expected_message_revision="sha256:" + "a" * 64,
        )
