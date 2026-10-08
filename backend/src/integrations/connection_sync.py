"""Bounded connected context on the current Python authority/job owners.

The connection row mirrors one active durable root and points at a read-back
private page artifact. It stores no token, content, lease, queue or ledger.
Importing this module activates nothing. The app owns start/stop explicitly.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from src.db.engine import get_session
from src.db.models import CalendarEventBinding, GoogleServiceConnection, MailMessageBinding, MailReadConsent, Secret, WorkflowRunState
from src.integrations import gmail_controls
from src.integrations.gmail_controls import MailSourceLease
from src.integrations.gmail_read import GmailReadError, GoogleGmailReadonlyAdapter, digest, message_key, thread_key
from src.integrations.google_calendar import CalendarIntegrationError, GoogleCalendarReadonlyAdapter, persist_calendar_event_binding
from src.integrations.native_physical_owner import NativeCallbackOwners, process_identity
from src.vault import decrypt, encrypt
from src.vault.repository import secret_binding_digest
from src.work_board.contracts import WorkBoardOwner
from src.scheduler.governed_schedules import _begin_serialized
from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec, NativePhysicalCleanupBinding, NativePhysicalCleanupProof, native_physical_cleanup_binding_payload, durable_job_repository

SYNC_SECONDS = 120
MAX_PAGES = 3
PAGE_ITEMS = 20
MAX_ITEMS = 50
MAX_PRIVATE_ITEMS = 10
RETRY_SECONDS = 30
SYNC_KIND = "connection_source_sync"


def now() -> datetime:
    return datetime.now(timezone.utc)


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class SyncError(GmailReadError):
    pass


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RevisionRef(Strict):
    id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)


class SyncWindow(Strict):
    start: datetime
    end: datetime

    @field_validator("start", "end", mode="before")
    @classmethod
    def timestamp(cls, value: Any) -> datetime:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("An explicit timezone is required")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def bounded(self):
        if not 0 < (self.end - self.start).total_seconds() <= 7 * 86400:
            raise ValueError("The sync window must be at most seven days")
        return self


class SourceScope(Strict):
    provider: Literal["gmail", "calendar"]
    consents: list[RevisionRef] = Field(min_length=1, max_length=3)
    label_ids: list[str] = Field(default_factory=list, max_length=3)
    thread_keys: list[str] = Field(default_factory=list, max_length=10)
    selected_private_items: list[str] = Field(default_factory=list, max_length=10)
    acknowledge_private_read: bool = False
    reset_cursor: bool = False

    @model_validator(mode="after")
    def selection(self):
        values = [ref.id for ref in self.consents]
        if len(set(values)) != len(values):
            raise ValueError("Duplicate grants are invalid")
        for selection in (self.label_ids, self.thread_keys, self.selected_private_items):
            if len(set(selection)) != len(selection) or any(not value or len(value) > 256 or any(ord(char) < 32 for char in value) for value in selection):
                raise ValueError("Selections must be unique bounded opaque identifiers")
        if self.provider == "gmail" and (len(self.consents) != 1 or not self.label_ids):
            raise ValueError("Gmail sync requires one grant and selected labels")
        if self.provider == "calendar" and (self.label_ids or self.thread_keys):
            raise ValueError("Calendar selection uses exact calendar grants")
        if self.selected_private_items and not self.acknowledge_private_read:
            raise ValueError("Private reads require separate explicit acknowledgement")
        return self


class ConnectionSyncInput(Strict):
    goal_ref: RevisionRef
    connection_ref: RevisionRef
    source_scope: SourceScope
    window: SyncWindow
    max_items: int = Field(ge=1, le=50)


class ConnectionCursor(Strict):
    connection_id: str
    revision: int
    scope_digest: str
    provider_cursor: dict[str, Any] | None
    last_complete_at: str | None


class SourceItemRef(Strict):
    provider: Literal["gmail", "calendar"]
    opaque_id: str
    revision: str
    content_digest: str
    privacy: Literal["owner_private"] = "owner_private"
    expires_at: str


class SyncRequest(Strict):
    input: ConnectionSyncInput
    request_uuid: str = Field(min_length=1, max_length=256)


class SyncReadback(Strict):
    acknowledge_private_read: Literal[True]


class SyncRecovery(Strict):
    expected_job_revision: int = Field(ge=1)
    expected_cursor_revision: int = Field(ge=0)
    acknowledge_physical_slot_release: Literal[True]


@dataclass(frozen=True)
class Authority:
    connection: GoogleServiceConnection
    consents: tuple[Any, ...]
    credential_fingerprint: str
    snapshot_digest: str
    expires_at: datetime


def scope_digest(value: ConnectionSyncInput) -> str:
    # Private body selections and fresh request bounds do not expand/change
    # the fixed metadata scope. Their separate request digest remains exact.
    return digest({"provider": value.source_scope.provider, "connection": value.connection_ref.model_dump(), "goal": value.goal_ref.model_dump(), "grants": [ref.model_dump() for ref in value.source_scope.consents], "labels": sorted(value.source_scope.label_ids), "threads": sorted(value.source_scope.thread_keys)})


def page_identity(job_id: str, page: int) -> str:
    return f"{job_id}:page:{page}"


def public_page(payload: dict[str, Any]) -> dict[str, Any]:
    return {"items": [item["ref"] for item in payload.get("items", [])], "coverage": payload.get("coverage", {}), "freshness": payload.get("freshness", {}), "memory_status": "no_learning"}


class ConnectionSyncService:
    """One explicit-lifecycle callable, backed only by canonical owners."""

    def __init__(self) -> None:
        self.started = False
        self._tasks: set[asyncio.Task] = set()
        self._physical_owners = NativeCallbackOwners()

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _ready(self) -> None:
        if not self.started:
            raise SyncError("connection_sync_inactive", "Connected source synchronization is inactive", status_code=503, recovery_action="restart_backend")

    async def _authority(self, db, owner: WorkBoardOwner, value: ConnectionSyncInput, *, original: Authority | None = None, lease: MailSourceLease | None = None) -> Authority:
        from src.api import calendar, mail
        await mail._assert_live_session(db, owner)
        if lease is not None:
            await mail._assert_durable_lease_in_transaction(db, lease)
        connection = (await db.execute(select(GoogleServiceConnection).where(GoogleServiceConnection.connection_id == value.connection_ref.id, GoogleServiceConnection.owner_principal_id == owner.principal_id, GoogleServiceConnection.owner_session_id == owner.session_id).execution_options(populate_existing=True))).scalar_one_or_none()
        expected_service = "gmail_readonly" if value.source_scope.provider == "gmail" else "calendar_readonly"
        if connection is None or connection.service != expected_service or connection.state != "active" or connection.revision != value.connection_ref.revision:
            raise SyncError("connection_sync_authority_changed", "The source connection is unavailable or changed", recovery_action="reload_connection")
        secret = (await db.execute(select(Secret).where(Secret.key == connection.vault_secret_key).execution_options(populate_existing=True))).scalar_one_or_none()
        if secret is None or secret.revoked_at is not None or secret.owner_principal_id not in {None, owner.principal_id}:
            raise SyncError("connection_sync_credential_unavailable", "The original vaulted source credential is unavailable", recovery_action="restore_prerequisite")
        credential_digest = digest({"fingerprint": connection.credential_fingerprint, "vault_binding": secret_binding_digest(secret), "declared_scopes": connection.declared_scopes_json})
        if lease is not None and connection.sync_active_job_id != lease.job_id:
            raise SyncError("connection_sync_reservation_changed", "The source synchronization reservation changed", recovery_action="reconcile_existing_sync")
        await mail.repository._validate_goal(db, owner, goal_id=value.goal_ref.id, goal_revision=value.goal_ref.revision)
        consents = []
        snapshots = []
        for ref in value.source_scope.consents:
            if value.source_scope.provider == "gmail":
                consent = await mail._consent_for(db, owner, ref.id)
                await mail._validate_source_scope(db, owner, connection=connection, consent=consent, expected_source_revision=ref.revision, label_ids=value.source_scope.label_ids)
                if not 0 < consent.sync_metadata_limit <= 50 or value.max_items > consent.sync_metadata_limit:
                    raise SyncError("connection_sync_grant_required", "Explicit bounded metadata synchronization consent is required", recovery_action="create_sync_consent")
                # Stable request window; never slide it forward during retries.
                if value.window.start < aware(consent.created_at) - timedelta(days=consent.window_days) or value.window.end > now() + timedelta(seconds=1):
                    raise SyncError("connection_sync_window_invalid", "The Gmail sync window exceeds the original grant", status_code=422)
                if value.source_scope.selected_private_items and (len(value.source_scope.selected_private_items) > consent.max_messages or "plainbody" not in json.loads(consent.allowed_body_fields_json or "[]")):
                    raise SyncError("connection_sync_private_grant_required", "The selected private read exceeds the original body consent", status_code=403)
                snapshot = {"id": consent.consent_id, "revision": consent.source_revision, "digest": consent.source_digest, "limit": consent.sync_metadata_limit, "expires": aware(consent.expires_at).isoformat()}
            else:
                consent, current = await calendar._active_consent(db, owner, ref.id)
                if current.connection_id != connection.connection_id or consent.revision != ref.revision or not 0 < consent.sync_metadata_limit <= 50 or value.max_items > consent.sync_metadata_limit:
                    raise SyncError("connection_sync_grant_required", "Exact Calendar synchronization consent is required", recovery_action="create_sync_consent")
                if value.window.start < aware(consent.created_at) - timedelta(seconds=1) or value.window.end > aware(consent.created_at) + timedelta(minutes=consent.window_minutes):
                    raise SyncError("connection_sync_window_invalid", "The Calendar sync window exceeds the original grant", status_code=422)
                snapshot = {"id": consent.consent_id, "revision": consent.revision, "digest": consent.consent_digest, "calendar_cipher": digest(consent.calendar_id), "fields": consent.allowed_fields_json, "window": consent.window_minutes, "limit": consent.sync_metadata_limit, "expires": aware(consent.expires_at).isoformat()}
            if consent.goal_id != value.goal_ref.id or consent.goal_revision != value.goal_ref.revision or consent.connection_revision != connection.revision:
                raise SyncError("connection_sync_grant_changed", "The source grant does not match the original goal and connection")
            consents.append(consent)
            snapshots.append(snapshot)
        result = Authority(connection, tuple(consents), credential_digest, digest({"grants": snapshots, "credential": credential_digest, "scope": scope_digest(value)}), min(aware(consent.expires_at) for consent in consents))
        if original is not None and (result.snapshot_digest != original.snapshot_digest or result.credential_fingerprint != original.credential_fingerprint):
            raise SyncError("connection_sync_grant_changed", "The original synchronization authority changed", recovery_action="create_new_work")
        return result

    async def _page(self, job_id: str, page: int) -> dict[str, Any]:
        job = await durable_job_repository.get_job(job_id)
        identity = page_identity(job_id, page)
        path = gmail_controls._artifact_path(identity)
        if not job or job.get("job_kind") != SYNC_KIND:
            raise SyncError("connection_sync_artifact_unavailable", "The synchronized source artifact is unavailable")
        matching = [item for item in job.get("artifacts", []) if item.get("file_path") == path]
        sha = matching[-1].get("content_sha256") if matching else None
        effects = job.get("effects", [])
        if not sha or not any(item.get("receipt_kind") == "readback" and item.get("target_path") == path and item.get("content_sha256") == sha and item.get("status") == "succeeded" for item in effects):
            raise SyncError("connection_sync_artifact_unverified", "The synchronized source page has no verified readback")
        payload = gmail_controls._read_artifact(identity, expected_sha256=sha, max_encrypted_bytes=384 * 1024)
        if not payload or payload.get("schema") != "connection-sync-page-v1":
            raise SyncError("connection_sync_artifact_unavailable", "The synchronized source page is unavailable")
        return payload

    async def _current_page(self, connection: GoogleServiceConnection) -> dict[str, Any] | None:
        if connection.sync_cursor_job_id is None:
            return None
        payload = await self._page(connection.sync_cursor_job_id, connection.sync_cursor_page)
        cursor = ConnectionCursor.model_validate(payload["cursor"])
        if cursor.connection_id != connection.connection_id or cursor.revision != connection.sync_cursor_revision or cursor.scope_digest != connection.sync_scope_digest:
            raise SyncError("connection_sync_cursor_invalid", "The source cursor requires reconciliation", recovery_action="reconcile_existing_sync")
        return payload

    async def synchronize(self, owner: WorkBoardOwner, request: SyncRequest) -> dict[str, Any]:
        self._ready()
        async def callback():
            async with asyncio.timeout(SYNC_SECONDS):
                return await self._synchronize(owner, request)
        task = asyncio.create_task(callback())
        self._tasks.add(task)
        try:
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if any(original[0] is task for original in self._physical_owners.callbacks.values()):
                self._physical_owners.awaited.add(task)
            self._tasks.discard(task)

    async def _synchronize(self, owner: WorkBoardOwner, request: SyncRequest) -> dict[str, Any]:
        value = request.input
        async with get_session() as db:
            original = await self._authority(db, owner, value)
        # The original authority, not a later grant refresh, defines this root.
        job_id = "connection-sync:" + uuid.uuid5(uuid.NAMESPACE_URL, f"{owner.principal_id}|{owner.session_id}|{value.connection_ref.id}|{request.request_uuid}").hex
        exact_digest = digest({"request": request.model_dump(mode="json"), "authority": original.snapshot_digest})
        prior = await durable_job_repository.get_job(job_id)
        if prior:
            if prior.get("run_fingerprint") != exact_digest or prior.get("owner", {}).get("principal_id") != owner.principal_id or prior.get("session_id") != owner.session_id:
                raise SyncError("connection_sync_idempotency_conflict", "The sync request key is bound to different input")
            if prior.get("status") == "succeeded":
                page = int(prior.get("final_page") or 0)
                # Final page is recovered from a canonical checkpoint, not
                # mutable request input or a last-known client response.
                checkpoints = prior.get("checkpoints", [])
                page = max([int(item.get("payload", {}).get("page", 0)) for item in checkpoints if str(item.get("checkpoint_id", "")).startswith("connection-sync-page-")] or [page])
                payload = await self._page(job_id, page)
                await self._assert_payload_authority(owner, payload)
                return {**public_page(payload), "job_id": job_id, "status": "succeeded", "replayed": True}
            raise SyncError("connection_sync_reconciliation_required", "The existing sync root requires reconciliation", recovery_action="reconcile_existing_sync")
        authority = {"capability_id": "mail.messages.read" if value.source_scope.provider == "gmail" else "calendar.events.read", "owner_kind": "user", "principal": owner.principal_id, "session_id": owner.session_id, "operator_session_id": owner.session_id, "goal_id": value.goal_ref.id, "goal_revision": value.goal_ref.revision, "connection_id": value.connection_ref.id, "connection_revision": value.connection_ref.revision, "grant_digest": original.snapshot_digest, "scope_digest": scope_digest(value), "finite_authority": True, "budget_microusd": 0}
        spec = DurableJobSpec(identity=DurableJobIdentity(job_id=job_id, owner_kind="user", owner_principal_id=owner.principal_id, job_kind=SYNC_KIND, capability_version="connection-sync-v1", idempotency_scope="connection-sync:" + value.connection_ref.id, idempotency_key=request.request_uuid), inputs={"request_digest": exact_digest, "authority_digest": original.snapshot_digest, "provider": value.source_scope.provider, "connection_id": value.connection_ref.id}, session_id=owner.session_id, operator_session_id=owner.session_id, conversation_id=owner.session_id, goal_id=value.goal_ref.id, goal_revision=value.goal_ref.revision, priority=60, resource_claims=("connection-sync:" + value.connection_ref.id,), declared_authority=authority, deadline_at=min(now() + timedelta(seconds=SYNC_SECONDS), original.expires_at), max_attempts=1, budget_microusd=0, budget_digest=digest({"budget_microusd": 0}), run_fingerprint=exact_digest)
        admission = await durable_job_repository.admit_job(spec)
        queued = await durable_job_repository.queue_job(job_id, expected_revision=admission["revision"])
        lease_owner = "connection-sync:" + owner.principal_id + ":" + job_id
        claimed = await durable_job_repository.claim_job(job_id, owner=lease_owner, lease_seconds=SYNC_SECONDS, expected_revision=queued["revision"])
        fence = claimed["lease"]["fencing_token"]
        lease = MailSourceLease(job_id, lease_owner, fence, claimed["revision"])
        contacted = False
        reserved = False
        page_count = 0
        current_revision = lease.revision
        retry_used = False

        async def check() -> None:
            self._ready()
            async with get_session() as db:
                await self._authority(db, owner, value, original=original, lease=lease)

        try:
            async with get_session() as db:
                await _begin_serialized(db)
                from src.api import mail
                await mail._assert_durable_lease_in_transaction(db, lease)
                fresh = await self._authority(db, owner, value, original=original)
                connection = fresh.connection
                if connection.sync_active_job_id and connection.sync_active_job_id != job_id:
                    raise SyncError("connection_sync_busy", "Another source synchronization requires settlement", recovery_action="reconcile_existing_sync")
                desired_scope = scope_digest(value)
                if connection.sync_scope_digest and connection.sync_scope_digest != desired_scope and not value.source_scope.reset_cursor:
                    raise SyncError("connection_sync_scope_reset_required", "The selected source scope changed; explicitly reset its cursor", recovery_action="reset_cursor")
                if value.source_scope.reset_cursor:
                    connection.sync_cursor_job_id = None
                    connection.sync_cursor_page = 0
                    connection.sync_cursor_revision += 1
                connection.sync_scope_digest = desired_scope
                connection.sync_active_job_id = job_id
                await db.flush()
                reserved = True
                cursor_revision = connection.sync_cursor_revision
                previous = await self._current_page(connection)
            witness = {**process_identity(), "runtime_nonce": self._physical_owners.nonce, "connection_id": value.connection_ref.id, "scope_digest": scope_digest(value), "original_cursor_revision": cursor_revision}
            binding = NativePhysicalCleanupBinding(job_id=job_id, expected_revision=current_revision, original_owner_principal_id=owner.principal_id, original_operator_session_id=owner.session_id, original_session_id=owner.session_id, input_digest=claimed["input_digest"], authority_digest=claimed["authority_digest"], run_fingerprint=claimed["run_fingerprint"], attempt_count=claimed["attempt_count"], lease_owner=lease_owner, fencing_token=fence, resource_claim="connection-sync:" + value.connection_ref.id, witness_digest=digest(witness))
            reservation = {"binding": native_physical_cleanup_binding_payload(binding), "witness": witness}
            recorded = await durable_job_repository.record_checkpoint(job_id, checkpoint_id="native-physical-resource-reservation", state=reservation, checkpoint_payload=reservation, safe=True, owner=lease_owner, fencing_token=fence, expected_revision=current_revision)
            current_revision = recorded["revision"]
            self._physical_owners.bind(job_id, witness)
            token = previous["cursor"]["provider_cursor"] if previous else None
            # Continuation is tied to the exact original window; a later
            # request may start a fresh bounded scan only once coverage ended.
            if token and previous["input"]["window"] != value.model_dump(mode="json")["window"]:
                raise SyncError("connection_sync_window_changed", "Resume the incomplete original window or explicitly reset the cursor")
            calendar_index = int(token.get("calendar_index", 0)) if token else 0
            provider_token = token.get("page_token") if token else None
            remaining = value.max_items
            items: list[dict[str, Any]] = list(previous["items"]) if token and previous else []
            private_count = 0
            private_calendar_keys: set[str] = set()

            def contact() -> None:
                nonlocal contacted
                contacted = True

            adapter_class = GoogleGmailReadonlyAdapter if value.source_scope.provider == "gmail" else GoogleCalendarReadonlyAdapter
            adapter = adapter_class(original.connection, owner_principal_id=owner.principal_id, authority_check=check, contact_observer=contact, contact_timeout_seconds=2, require_scope_evidence=True, deadline_at=aware(datetime.fromisoformat(str(claimed["deadline_at"]).replace("Z", "+00:00"))))
            while page_count < MAX_PAGES and remaining > 0:
                await check()
                page_count += 1
                effect_id = f"connection-sync-page:{job_id}:{page_count}"
                artifact_identity = page_identity(job_id, page_count)
                artifact_path = gmail_controls._artifact_path(artifact_identity)
                intent = await durable_job_repository.record_effect(job_id, effect_type=SYNC_KIND, effect_id=effect_id, target_path=artifact_path, target_digest=exact_digest, status="intent", adapter_idempotency_key=f"{request.request_uuid}:{page_count}", details={"page": page_count, "provider": value.source_scope.provider, "contact_class": "oauth_refresh_post_and_source_get", "read_only": False}, owner=lease_owner, fencing_token=fence, expected_revision=current_revision)
                current_revision = intent["revision"]
                limit = min(PAGE_ITEMS, remaining)

                async def fetch():
                    nonlocal private_count
                    if value.source_scope.provider == "gmail":
                        from src.api import mail
                        async with get_session() as db:
                            labels = await mail._selected_provider_labels(db, owner, original.connection, value.source_scope.label_ids)
                        page = await adapter.list_sync_page(labels, received_after=value.window.start, received_before=value.window.end, max_messages=limit, page_token=provider_token)
                        semaphore = asyncio.Semaphore(2)
                        async def read_metadata(provider_id):
                            nonlocal private_count
                            async with semaphore:
                                return await read_one(provider_id)
                        async def read_one(provider_id):
                            nonlocal private_count
                            try:
                                item = await adapter.get_message_metadata(provider_id)
                            except GmailReadError as exc:
                                if exc.code != "mail_item_deleted":
                                    raise
                                return (provider_id, None, None)
                            selected_thread = not value.source_scope.thread_keys or thread_key(owner.principal_id, value.connection_ref.id, item.provider_thread_id) in value.source_scope.thread_keys
                            if not selected_thread or {"SPAM", "TRASH"}.intersection(item.label_ids) or not set(labels).issubset(item.label_ids) or item.received_at is None or not value.window.start <= item.received_at < value.window.end:
                                return (provider_id, None, "out_of_scope")
                            key = message_key(owner.principal_id, value.connection_ref.id, provider_id)
                            body = None
                            if key in value.source_scope.selected_private_items and private_count < MAX_PRIVATE_ITEMS:
                                private_count += 1
                                full = await adapter.get_message_full(provider_id)
                                if full.metadata.message_revision != item.message_revision:
                                    raise SyncError("connection_sync_item_changed", "The selected message changed during private read")
                                body = full.body
                            return (provider_id, item, body)
                        tasks = [asyncio.create_task(read_metadata(provider_id)) for provider_id in page.provider_ids]
                        try:
                            metadata = await asyncio.gather(*tasks)
                        except BaseException:
                            for pending in tasks:
                                pending.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)
                            raise
                        return metadata, page.next_page_token, len(page.provider_ids)
                    consent = original.consents[calendar_index]
                    calendar_id = decrypt(consent.calendar_id)
                    selected = set(value.source_scope.selected_private_items) - private_calendar_keys
                    snapshots, next_token = await adapter.list_sync_page(calendar_id, time_min=value.window.start, time_max=value.window.end, max_events=min(limit, consent.max_events), page_token=provider_token, private_event_keys=selected, allowed_fields=set(json.loads(consent.allowed_fields_json or "[]")))
                    private_calendar_keys.update(snapshot.event_key for snapshot in snapshots if snapshot.event_key in selected)
                    return snapshots, next_token, len(snapshots)

                try:
                    records, next_token, consumed = await fetch()
                except (GmailReadError, CalendarIntegrationError) as exc:
                    if exc.status_code != 429 or retry_used or now() + timedelta(seconds=RETRY_SECONDS) >= min(aware(datetime.fromisoformat(str(claimed["deadline_at"]).replace("Z", "+00:00"))), original.expires_at):
                        raise
                    retry_used = True
                    retry_at = now() + timedelta(seconds=RETRY_SECONDS)
                    checkpoint = await durable_job_repository.record_checkpoint(job_id, checkpoint_id="connection-sync-cooldown", state={"retry_at": retry_at.isoformat(), "retry_count": 1, "grant_digest": original.snapshot_digest, "original_deadline": claimed["deadline_at"], "page": page_count}, checkpoint_payload={"retry_at": retry_at.isoformat(), "retry_count": 1, "original_deadline": claimed["deadline_at"]}, safe=True, owner=lease_owner, fencing_token=fence, expected_revision=current_revision)
                    current_revision = checkpoint["revision"]
                    await asyncio.sleep(RETRY_SECONDS)
                    await check()
                    records, next_token, consumed = await fetch()
                remaining -= consumed
                async with get_session() as db:
                    await _begin_serialized(db)
                    fresh = await self._authority(db, owner, value, original=original, lease=lease)
                    if fresh.connection.sync_cursor_revision != cursor_revision:
                        raise SyncError("connection_sync_cursor_conflict", "The synchronized source cursor changed")
                    if value.source_scope.provider == "gmail":
                        page_items = await self._adopt_mail(db, owner, fresh, value, records)
                    else:
                        page_items = await self._adopt_calendar(db, owner, fresh, value, calendar_index, records)
                keyed = {item["ref"]["opaque_id"]: item for item in items}
                keyed.update({item["ref"]["opaque_id"]: item for item in page_items})
                items = list(keyed.values())[-MAX_ITEMS:]
                # No omission tombstones: only explicit provider deletion is
                # adopted. A short/partial list cannot prove an item vanished.
                if value.source_scope.provider == "calendar" and next_token is None and calendar_index + 1 < len(original.consents):
                    calendar_index += 1
                    token = {"calendar_index": calendar_index, "page_token": None}
                elif next_token:
                    token = {"calendar_index": calendar_index, "page_token": next_token}
                else:
                    token = None
                cursor_revision += 1
                completed_at = now().isoformat()
                payload = {"schema": "connection-sync-page-v1", "input": value.model_dump(mode="json"), "authority_digest": original.snapshot_digest, "owner_principal_id": owner.principal_id, "owner_session_id": owner.session_id, "cursor": ConnectionCursor(connection_id=value.connection_ref.id, revision=cursor_revision, scope_digest=scope_digest(value), provider_cursor=token, last_complete_at=completed_at).model_dump(), "items": items, "coverage": {"pages_read": page_count, "returned": len(items), "metadata_examined": value.max_items - remaining, "max_items": value.max_items, "page_complete": True, "more_available": token is not None, "partial": token is not None, "scope_digest": scope_digest(value), "window": value.window.model_dump(mode="json"), "scope_reset": value.source_scope.reset_cursor}, "freshness": {"last_complete_at": completed_at, "expires_at": original.expires_at.isoformat()}, "memory_status": "no_learning"}
                await check()
                path, ciphertext, sha = await asyncio.to_thread(gmail_controls._write_artifact, artifact_identity, payload)
                artifact = await durable_job_repository.record_artifact(job_id, file_path=path, artifact_type="connected_source_page", content=ciphertext, owner=lease_owner, fencing_token=fence, expected_revision=current_revision)
                readback = await durable_job_repository.record_readback(job_id, target_path=path, status="succeeded", effect_id=effect_id, effect_type=SYNC_KIND, target_digest=exact_digest, content_sha256=sha, readback_id=effect_id + ":readback", verified_at=completed_at, details={"verified": True, "memory_status": "no_learning", "page": page_count}, owner=lease_owner, fencing_token=fence, expected_revision=artifact["revision"])
                checkpoint = await durable_job_repository.record_checkpoint(job_id, checkpoint_id=f"connection-sync-page-{page_count}", state={"page": page_count, "cursor_revision": cursor_revision, "artifact_sha256": sha}, checkpoint_payload={"page": page_count, "cursor_revision": cursor_revision}, safe=True, owner=lease_owner, fencing_token=fence, expected_revision=readback["revision"])
                current_revision = checkpoint["revision"]
                async with get_session() as db:
                    await _begin_serialized(db)
                    fresh = await self._authority(db, owner, value, original=original, lease=lease)
                    if fresh.connection.sync_cursor_revision != cursor_revision - 1:
                        raise SyncError("connection_sync_cursor_conflict", "The source cursor changed before adoption")
                    fresh.connection.sync_cursor_job_id = job_id
                    fresh.connection.sync_cursor_page = page_count
                    fresh.connection.sync_cursor_revision = cursor_revision
                    await db.flush()
                if token is None:
                    break
                provider_token = token.get("page_token")
            await check()
            await durable_job_repository.transition_job(job_id, "succeeded", owner=lease_owner, fencing_token=fence, expected_revision=current_revision, result_summary="Bounded connected source pages adopted")
            await self._release(owner, value.connection_ref.id, job_id, known=True)
            self._physical_owners.callbacks.pop(job_id, None)
            return {**public_page(payload), "job_id": job_id, "status": "succeeded", "replayed": False}
        except BaseException as exc:
            # An ambiguous contact is retained. Time/process death never
            # clears this canonical reservation or renews an old grant.
            try:
                current = await durable_job_repository.get_job(job_id)
                if current and current.get("status") == "running":
                    await durable_job_repository.transition_job(job_id, "unknown_external_effect" if contacted else "failed", owner=lease_owner, fencing_token=fence, expected_revision=current["revision"], reason=getattr(exc, "code", "connection_sync_interrupted"))
                if reserved and not contacted:
                    await self._release(owner, value.connection_ref.id, job_id, known=True)
            except Exception:
                pass
            raise

    async def _adopt_mail(self, db, owner: WorkBoardOwner, authority: Authority, value: ConnectionSyncInput, records) -> list[dict[str, Any]]:
        consent = authority.consents[0]
        result = []
        for provider_id, metadata, body in records:
            key = message_key(owner.principal_id, value.connection_ref.id, provider_id)
            row = (await db.execute(select(MailMessageBinding).where(MailMessageBinding.owner_principal_id == owner.principal_id, MailMessageBinding.owner_session_id == owner.session_id, MailMessageBinding.connection_id == value.connection_ref.id, MailMessageBinding.message_key == key))).scalar_one_or_none()
            if row is not None and (row.connection_revision != value.connection_ref.revision or row.source_consent_id != consent.consent_id or row.source_consent_revision != consent.source_revision):
                raise SyncError("connection_sync_original_grant_conflict", "The message belongs to a different original source grant", recovery_action="select_original_grant")
            if metadata is None and body == "out_of_scope":
                continue
            if metadata is None:
                if row is None:
                    continue
                if row.status != "deleted":
                    row.status = "deleted"
                    row.revision += 1
                content = {"status": "deleted"}
                revision = "sha256:" + digest({"message": key, "status": "deleted"})
            else:
                if row is None:
                    from src.api.mail import _source_label_scope_digest
                    row = MailMessageBinding(owner_principal_id=owner.principal_id, owner_session_id=owner.session_id, connection_id=value.connection_ref.id, connection_revision=value.connection_ref.revision, source_consent_id=consent.consent_id, source_consent_revision=consent.source_revision, source_label_scope_digest=_source_label_scope_digest(authority.connection, consent, value.source_scope.label_ids), provider_message_id_ciphertext=encrypt(provider_id), provider_thread_id_ciphertext=encrypt(metadata.provider_thread_id), message_key=key, thread_key=thread_key(owner.principal_id, value.connection_ref.id, metadata.provider_thread_id), message_revision=metadata.message_revision, received_at=metadata.received_at)
                    db.add(row)
                elif row.message_revision != metadata.message_revision or row.status != "present":
                    row.revision += 1
                row.message_revision = metadata.message_revision
                row.status = "present"
                row.fetched_at = now()
                revision = metadata.message_revision
                content = {"status": "present", "subject": metadata.subject, "preview": metadata.preview, "read_status": metadata.read_status, "received_at": metadata.received_at.isoformat() if metadata.received_at else None}
                if body is not None:
                    content["body"] = body
            result.append({"ref": SourceItemRef(provider="gmail", opaque_id=key, revision=revision, content_digest=digest(content), expires_at=authority.expires_at.isoformat()).model_dump(), "content": content})
        await db.flush()
        return result

    async def _adopt_calendar(self, db, owner: WorkBoardOwner, authority: Authority, value: ConnectionSyncInput, index: int, records) -> list[dict[str, Any]]:
        consent = authority.consents[index]
        result = []
        for snapshot in records:
            old = (await db.execute(select(CalendarEventBinding).where(CalendarEventBinding.owner_principal_id == owner.principal_id, CalendarEventBinding.owner_session_id == owner.session_id, CalendarEventBinding.connection_id == value.connection_ref.id, CalendarEventBinding.event_key == snapshot.event_key))).scalar_one_or_none()
            if old is not None and (old.connection_revision != value.connection_ref.revision or old.consent_id != consent.consent_id or old.consent_revision != consent.revision):
                raise SyncError("connection_sync_original_grant_conflict", "The event belongs to a different original source grant", recovery_action="select_original_grant")
            row = await persist_calendar_event_binding(db, owner_principal_id=owner.principal_id, owner_session_id=owner.session_id, connection=authority.connection, consent=consent, snapshot=snapshot)
            row.state = "deleted" if snapshot.fields.get("status") == "cancelled" else "selected"
            content = {key: val for key, val in snapshot.fields.items() if key not in {"etag"}}
            result.append({"ref": SourceItemRef(provider="calendar", opaque_id=snapshot.event_key, revision=snapshot.event_revision, content_digest=digest(content), expires_at=authority.expires_at.isoformat()).model_dump(), "content": content})
        return result

    async def _release(self, owner: WorkBoardOwner, connection_id: str, job_id: str, *, known: bool) -> None:
        if not known:
            return
        async with get_session() as db:
            await _begin_serialized(db)
            connection = (await db.execute(select(GoogleServiceConnection).where(GoogleServiceConnection.connection_id == connection_id, GoogleServiceConnection.owner_principal_id == owner.principal_id, GoogleServiceConnection.owner_session_id == owner.session_id))).scalar_one_or_none()
            root = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))).scalar_one_or_none()
            if connection is not None and connection.sync_active_job_id == job_id and root is not None and root.status in {"succeeded", "failed", "cancelled"}:
                connection.sync_active_job_id = None

    async def _assert_payload_authority(self, owner: WorkBoardOwner, payload: dict[str, Any]) -> Authority:
        if payload.get("owner_principal_id") != owner.principal_id or payload.get("owner_session_id") != owner.session_id:
            raise SyncError("connection_sync_owner_mismatch", "The connected source artifact is unavailable", status_code=404)
        value = ConnectionSyncInput.model_validate(payload["input"])
        async with get_session() as db:
            authority = await self._authority(db, owner, value)
            if authority.snapshot_digest != payload["authority_digest"] or authority.connection.sync_scope_digest != scope_digest(value):
                raise SyncError("connection_sync_readback_revoked", "The original source read authority changed", status_code=403)
        return authority

    async def status(self, owner: WorkBoardOwner, connection_id: str) -> dict[str, Any]:
        self._ready()
        from src.api import mail
        async with get_session() as db:
            await mail._assert_live_session(db, owner)
            connection = (await db.execute(select(GoogleServiceConnection).where(GoogleServiceConnection.connection_id == connection_id, GoogleServiceConnection.owner_principal_id == owner.principal_id, GoogleServiceConnection.owner_session_id == owner.session_id))).scalar_one_or_none()
            if connection is None:
                raise SyncError("connection_sync_not_found", "The source connection is unavailable", status_code=404)
            root_id = connection.sync_active_job_id
            candidates = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind == SYNC_KIND, WorkflowRunState.owner_principal_id == owner.principal_id, WorkflowRunState.operator_session_id == owner.session_id, WorkflowRunState.status == "unknown_external_effect"))).scalars().all()
            unresolved = []
            for candidate in candidates:
                authority = json.loads(candidate.declared_authority_json or "{}")
                if authority.get("connection_id") == connection_id:
                    unresolved.append({"job_id": candidate.run_identity, "revision": candidate.revision, "status": candidate.status, "failure_reason": candidate.failure_reason})
            page = await self._current_page(connection)
        root = await durable_job_repository.get_job(root_id) if root_id else None
        state = "not_started" if not page else "ready"
        if connection.state != "active":
            state = "revoked"
        elif root:
            state = str(root.get("status") or "blocked")
        result = {"connection_id": connection_id, "state": state, "reservation_state": "held" if root_id else "available", "unresolved_jobs": unresolved, "active_job_id": root_id, "active_job_revision": root.get("revision") if root else None, "last_error_code": root.get("failure_reason") if root else None, "cursor_revision": connection.sync_cursor_revision, "scope_digest": connection.sync_scope_digest, "selection": None, "coverage": {}, "freshness": {}, "items": [], "recovery_action": "release_physical_slot" if root and root.get("status") == "unknown_external_effect" else None}
        if page:
            try:
                await self._assert_payload_authority(owner, page)
            except Exception:
                result["state"] = "blocked" if connection.state == "active" else "revoked"
                result["recovery_action"] = "restore_original_grant"
            else:
                result.update(public_page(page))
                selection = ConnectionSyncInput.model_validate(page["input"]).model_dump(mode="json")
                # Reload can recover the exact metadata scope. It cannot
                # silently replay a previous private-read/reset acknowledgement.
                selection["source_scope"]["selected_private_items"] = []
                selection["source_scope"]["acknowledge_private_read"] = False
                selection["source_scope"]["reset_cursor"] = False
                result["selection"] = selection
        if root:
            for checkpoint in root.get("checkpoints", []):
                if checkpoint.get("checkpoint_id") == "connection-sync-cooldown":
                    result["cooldown"] = checkpoint.get("payload", {})
        return result

    async def read_item(self, owner: WorkBoardOwner, connection_id: str, opaque_id: str) -> dict[str, Any]:
        self._ready()
        async with get_session() as db:
            connection = (await db.execute(select(GoogleServiceConnection).where(GoogleServiceConnection.connection_id == connection_id, GoogleServiceConnection.owner_principal_id == owner.principal_id, GoogleServiceConnection.owner_session_id == owner.session_id))).scalar_one_or_none()
            if connection is None or connection.sync_cursor_job_id is None:
                raise SyncError("connection_sync_item_unavailable", "The selected connected source item is unavailable", status_code=404)
            payload = await self._current_page(connection)
        await self._assert_payload_authority(owner, payload)
        item = next((item for item in payload["items"] if item["ref"]["opaque_id"] == opaque_id), None)
        if item is None:
            raise SyncError("connection_sync_item_unavailable", "The item is outside the synchronized source selection", status_code=404)
        # Recheck after decryption before private content crosses the owner API.
        await self._assert_payload_authority(owner, payload)
        return {"item": item, "coverage": payload["coverage"], "freshness": payload["freshness"], "memory_status": "no_learning"}

    async def reconcile(self, owner: WorkBoardOwner, connection_id: str, job_id: str, expected_revision: int, *, expected_cursor_revision: int, authenticated_token_hash: str) -> dict[str, Any]:
        """Negative physical cleanup only; never settles a provider effect."""
        self._ready()
        root = await durable_job_repository.get_job(job_id)
        if not root or root.get("job_kind") != SYNC_KIND or root.get("revision") != expected_revision:
            raise SyncError("connection_sync_recovery_mismatch", "The original synchronization root changed")
        reservations = [item for item in root.get("checkpoints", []) if item.get("checkpoint_id") == "native-physical-resource-reservation"]
        if len(reservations) != 1:
            raise SyncError("connection_sync_recovery_blocked", "The original callback reservation is unavailable")
        payload = reservations[0].get("payload", {})
        binding = NativePhysicalCleanupBinding(**payload["binding"], expected_revision=expected_revision)
        witness = payload["witness"]
        if witness.get("connection_id") != connection_id:
            raise SyncError("connection_sync_recovery_mismatch", "The original source reservation changed")

        async def verify(db, run, reservation):
            if run.status != "unknown_external_effect":
                raise SyncError("connection_sync_recovery_blocked", "The original Unknown root requires canonical recovery")
            connection = (await db.execute(select(GoogleServiceConnection).where(GoogleServiceConnection.connection_id == connection_id).execution_options(populate_existing=True))).scalar_one_or_none()
            if connection is None or connection.owner_principal_id != binding.original_owner_principal_id or connection.owner_session_id != binding.original_operator_session_id or connection.sync_active_job_id != job_id or connection.sync_cursor_revision != expected_cursor_revision or connection.sync_scope_digest != witness.get("scope_digest") or json.loads(run.declared_authority_json or "{}").get("scope_digest") != witness.get("scope_digest"):
                raise SyncError("connection_sync_recovery_mismatch", "The exact physical source pointer changed")
            proof_kind = self._physical_owners.proof(job_id, reservation["witness"])
            if proof_kind is None:
                raise SyncError("connection_sync_callback_active", "Positive original callback quiescence is not proven")
            return NativePhysicalCleanupProof(binding.witness_digest, proof_kind)

        async def release_pointer(db, run):
            connection = await db.get(GoogleServiceConnection, connection_id)
            connection.sync_active_job_id = None
            await db.flush()

        released = await durable_job_repository.record_native_physical_cleanup(binding, current_owner=owner, authenticated_token_hash=authenticated_token_hash, verify_cleanup=verify, release_pointer=release_pointer)
        original_callback = self._physical_owners.callbacks.pop(job_id, None)
        if original_callback is not None:
            self._physical_owners.awaited.discard(original_callback[0])
        return {"job_id": job_id, "status": released["status"], "physical_slot_released": True, "provider_contacts": 0, "memory_status": "no_learning"}
