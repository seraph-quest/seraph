import enum
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, CheckConstraint, Column, Index, Integer, Text, UniqueConstraint, text
from sqlmodel import Field, SQLModel, Relationship


# ─── Enums ────────────────────────────────────────────────

class GoalLevel(str, enum.Enum):
    vision = "vision"
    annual = "annual"
    quarterly = "quarterly"
    monthly = "monthly"
    weekly = "weekly"
    daily = "daily"


class GoalDomain(str, enum.Enum):
    productivity = "productivity"
    performance = "performance"
    health = "health"
    influence = "influence"
    growth = "growth"


class GoalStatus(str, enum.Enum):
    active = "active"
    completed = "completed"
    paused = "paused"
    abandoned = "abandoned"


class WorkBoardStatus(str, enum.Enum):
    """Canonical operator task projection states.

    WorkflowRunState remains the authority for execution.  These values are
    the operator-facing coordination states and deliberately do not mirror
    the durable workflow status vocabulary.
    """

    triage = "triage"
    todo = "todo"
    ready = "ready"
    running = "running"
    blocked = "blocked"
    review = "review"
    done = "done"
    archived = "archived"


class MemoryCategory(str, enum.Enum):
    fact = "fact"
    preference = "preference"
    pattern = "pattern"
    goal = "goal"
    reflection = "reflection"


class MemoryKind(str, enum.Enum):
    fact = "fact"
    preference = "preference"
    pattern = "pattern"
    goal = "goal"
    reflection = "reflection"
    project = "project"
    collaborator = "collaborator"
    obligation = "obligation"
    routine = "routine"
    timeline = "timeline"
    commitment = "commitment"
    communication_preference = "communication_preference"
    procedural = "procedural"


class MemoryStatus(str, enum.Enum):
    active = "active"
    archived = "archived"
    superseded = "superseded"


class MemoryProposalStatus(str, enum.Enum):
    pending_inference = "pending_inference"
    proposed = "proposed"
    accepting = "accepting"
    accepted = "accepted"
    rejected = "rejected"
    no_learning = "no_learning"
    blocked = "blocked"
    expired = "expired"
    rolled_back = "rolled_back"


class MemoryProposalProviderContactState(str, enum.Enum):
    not_started = "not_started"
    started = "started"
    unknown = "unknown"
    succeeded = "succeeded"


class MemoryProposalDecisionEffect(str, enum.Enum):
    none = "none"
    require_operator_confirmation = "require_operator_confirmation"


class MemoryProposalPrivacyState(str, enum.Enum):
    visible = "visible"
    redacted = "redacted"


class WorkBoardDecisionReceiptStage(str, enum.Enum):
    source_baseline = "source_baseline"
    later_comparison = "later_comparison"


class WorkBoardDecisionStatus(str, enum.Enum):
    changed = "changed"
    no_change = "no_change"
    no_comparable = "no_comparable"
    blocked = "blocked"


class WorkBoardDecisionAdmissionStatus(str, enum.Enum):
    not_required = "not_required"
    awaiting_owner_confirmation = "awaiting_owner_confirmation"
    confirmed = "confirmed"
    consumed = "consumed"
    blocked = "blocked"
    superseded = "superseded"


class MemoryEpisodeType(str, enum.Enum):
    conversation = "conversation"
    tool = "tool"
    workflow = "workflow"
    decision = "decision"
    observer = "observer"


class MemoryEntityType(str, enum.Enum):
    person = "person"
    project = "project"
    routine = "routine"
    obligation = "obligation"
    organization = "organization"
    thread = "thread"


class MemorySnapshotKind(str, enum.Enum):
    bounded_guardian_context = "bounded_guardian_context"


class MemoryEdgeType(str, enum.Enum):
    related = "related"
    supports = "supports"
    supersedes = "supersedes"
    contradicts = "contradicts"


# ─── Helper ──────────────────────────────────────────────

def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── Session ─────────────────────────────────────────────

class Session(SQLModel, table=True):
    __tablename__ = "sessions"

    id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: Optional[str] = Field(default=None, index=True)
    title: str = Field(default="New Conversation")
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)

    messages: list["Message"] = Relationship(back_populates="session")


# ─── Message ─────────────────────────────────────────────

class Message(SQLModel, table=True):
    __tablename__ = "messages"

    id: str = Field(default_factory=_uuid, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", index=True)
    # Additive canonical conversation lineage.  ``session_id`` remains the
    # foreign-key authority; these fields make cross-surface receipts queryable
    # without introducing a second conversation store.
    conversation_id: Optional[str] = Field(default=None, index=True)
    thread_id: Optional[str] = Field(default=None, index=True)
    owner_principal_id: Optional[str] = Field(default=None, index=True)
    operator_session_id: Optional[str] = Field(default=None, index=True)
    device_id: Optional[str] = Field(default=None, index=True)
    channel: Optional[str] = Field(default=None, index=True)
    transport: Optional[str] = Field(default=None, index=True)
    correlation_id: Optional[str] = Field(default=None, index=True)
    causation_id: Optional[str] = Field(default=None, index=True)
    attachment_refs_json: str = Field(default="[]")
    role: str = Field(index=True)  # user | assistant | step | error
    content: str = Field(default="")
    metadata_json: Optional[str] = Field(default=None)
    step_number: Optional[int] = Field(default=None)
    tool_used: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)

    session: Optional[Session] = Relationship(back_populates="messages")


# ─── Audio ingress ───────────────────────────────────────

class AudioIngressJob(SQLModel, table=True):
    """Durable metadata for one bounded push-to-talk processing attempt.

    Audio bytes live only in a short-lived quarantine directory.  This row is
    deliberately metadata-only after cleanup and is the idempotency anchor for
    retries, cancellation, restart recovery, and transcript confirmation.
    """

    __tablename__ = "audio_ingress_jobs"
    __table_args__ = (
        Index("ux_audio_ingress_jobs_request_id", "request_id", unique=True),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    request_id: str = Field(index=True)
    request_digest: str = Field(index=True)
    owner_principal_id: str = Field(index=True)
    operator_session_id: Optional[str] = Field(default=None, index=True)
    session_id: str = Field(foreign_key="sessions.id", index=True)
    message_id: str = Field(index=True)
    attachment_id: str = Field(index=True)
    attachment_ref_json: str = Field(default="{}")
    status: str = Field(default="queued", index=True)
    requested_capability: str = Field(default="chat", index=True)
    raw_path: Optional[str] = Field(default=None)
    normalized_path: Optional[str] = Field(default=None)
    captured_at: datetime = Field(index=True)
    audio_payload_digest: str = Field(index=True)
    audio_size_bytes: int = Field(default=0)
    duration_seconds: float = Field(default=0.0)
    decoded_duration_seconds: Optional[float] = Field(default=None)
    media_type: str = Field(default="audio/wav")
    container: str = Field(default="wav")
    codec: str = Field(default="pcm_s16le")
    sample_rate_hz: int = Field(default=16_000)
    channels: int = Field(default=1)
    normalized_wav_size_bytes: Optional[int] = Field(default=None)
    capture_consent_reference: str = Field(default="")
    model_consent_reference: str = Field(default="")
    raw_audio_retention_deadline: datetime = Field(index=True)
    admission_operation_id: Optional[str] = Field(default=None, index=True)
    # A server-owned lease fences the final intercepted transport boundary.
    # It is intentionally never exposed to browser callers; revocation and
    # cancellation can invalidate the durable row while a worker is waiting.
    transport_lease_id: Optional[str] = Field(default=None, index=True)
    transcript: Optional[str] = Field(default=None)
    transcript_digest: Optional[str] = Field(default=None, index=True)
    confirmed_transcript_digest: Optional[str] = Field(default=None, index=True)
    result_digest: Optional[str] = Field(default=None, index=True)
    error_code: Optional[str] = Field(default=None, index=True)
    provider_status: str = Field(default="unverified", index=True)
    transport_status: str = Field(default="unknown", index=True)
    cleanup_status: str = Field(default="complete", index=True)
    metadata_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class AudioConsentGrant(SQLModel, table=True):
    """Server-issued consent for one audio boundary.

    The browser may carry the opaque reference, but it cannot choose the
    state, owner, operator session, or validity window that authorizes a
    capture or model transfer.  Audio workers re-read this row before each
    boundary crossing so revocation is effective for queued jobs too.
    """

    __tablename__ = "audio_consent_grants"
    __table_args__ = (
        Index("ux_audio_consent_grants_reference", "reference", unique=True),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    reference: str = Field(index=True)
    owner_principal_id: str = Field(index=True)
    operator_session_id: str = Field(index=True)
    boundary: str = Field(index=True)  # capture | cloud_upload
    state: str = Field(default="active", index=True)  # active | revoked
    granted_at: datetime = Field(default_factory=_now, index=True)
    expires_at: datetime = Field(index=True)
    revoked_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


# ─── Session Todo ───────────────────────────────────────

class SessionTodo(SQLModel, table=True):
    __tablename__ = "session_todos"

    id: str = Field(default_factory=_uuid, primary_key=True)
    session_id: str = Field(foreign_key="sessions.id", index=True)
    content: str = Field(default="")
    completed: bool = Field(default=False, index=True)
    sort_order: int = Field(default=0, index=True)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ─── Scheduled Job ─────────────────────────────────────

class ScheduledJob(SQLModel, table=True):
    __tablename__ = "scheduled_jobs"

    id: str = Field(default_factory=_uuid, primary_key=True)
    name: str = Field(default="")
    enabled: bool = Field(default=True, index=True)
    trigger_type: str = Field(default="cron", index=True)
    trigger_spec_json: str = Field(default="{}")
    action_type: str = Field(default="deliver_message", index=True)
    action_spec_json: str = Field(default="{}")
    session_id: Optional[str] = Field(default=None, foreign_key="sessions.id", index=True)
    created_by_session_id: Optional[str] = Field(default=None, foreign_key="sessions.id", index=True)
    last_run_at: Optional[datetime] = Field(default=None, index=True)
    last_outcome: Optional[str] = Field(default=None, index=True)
    last_error: Optional[str] = Field(default=None)
    last_approval_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ScheduledJobRun(SQLModel, table=True):
    __tablename__ = "scheduled_job_runs"

    id: str = Field(default_factory=_uuid, primary_key=True)
    scheduled_job_id: str = Field(index=True)
    job_name: str = Field(default="")
    trigger_type: str = Field(default="cron", index=True)
    action_type: str = Field(default="deliver_message", index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    created_by_session_id: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="started", index=True)
    outcome: Optional[str] = Field(default=None, index=True)
    error: Optional[str] = Field(default=None)
    approval_id: Optional[str] = Field(default=None, index=True)
    started_at: datetime = Field(default_factory=_now, index=True)
    finished_at: Optional[datetime] = Field(default=None, index=True)
    metadata_json: Optional[str] = Field(default=None)


# ─── Governed Calendar (M5) ─────────────────────────────

class GoogleServiceConnection(SQLModel, table=True):
    """Owner-bound metadata for one encrypted, read-only Calendar credential.

    Secrets are deliberately kept in ``Secret`` through the vault repository;
    this row contains only the opaque vault key and immutable request digests.
    """

    __tablename__ = "google_service_connections"
    __table_args__ = (
        Index("ix_google_service_connections_owner_state", "owner_principal_id", "owner_session_id", "state"),
        UniqueConstraint(
            "owner_principal_id",
            "owner_session_id",
            "setup_idempotency_key",
            name="ux_google_service_connections_setup_key",
        ),
    )

    connection_id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    service: str = Field(default="calendar_readonly", index=True)
    label: str = Field(default="", max_length=200)
    vault_secret_key: str = Field(index=True, unique=True, max_length=256)
    credential_fingerprint: str = Field(default="", index=True, max_length=128)
    # Mail connections retain scope declarations as configuration evidence only;
    # they never establish provider privilege.  Calendar rows keep the empty
    # defaults for backwards compatibility.
    declared_scopes_json: str = Field(default="[]")
    provider_scopes_json: str = Field(default="[]")
    scope_status: str = Field(default="scope_unverified", index=True)
    setup_idempotency_key: str = Field(default="", max_length=256)
    setup_request_digest: str = Field(default="", index=True, max_length=128)
    state: str = Field(default="preparing", index=True)
    revision: int = Field(default=1, index=True)
    # The canonical verification result lives in the durable control job.  The
    # connection keeps only its opaque root identity for owner-bound lookup;
    # no idempotency key or response payload is cached on this row.
    verified_setup_job_id: Optional[str] = Field(default=None, index=True)
    revoke_idempotency_key: Optional[str] = Field(default=None, index=True, max_length=256)
    revoke_request_digest: Optional[str] = Field(default=None, index=True, max_length=128)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class CalendarReadConsent(SQLModel, table=True):
    """Finite owner/goal grant for bounded Calendar reads."""

    __tablename__ = "calendar_read_consents"
    __table_args__ = (
        Index("ix_calendar_read_consents_owner_state", "owner_principal_id", "owner_session_id", "state"),
        Index("ix_calendar_read_consents_connection", "connection_id", "state"),
        # Empty keys are retained by legacy rows and are not idempotency
        # claims.  Only a real nonempty owner/session key is unique.
        Index(
            "ux_calendar_read_consents_creation_idempotency",
            "owner_principal_id",
            "owner_session_id",
            "creation_idempotency_key",
            unique=True,
            sqlite_where=text("creation_idempotency_key <> ''"),
            postgresql_where=text("creation_idempotency_key <> ''"),
        ),
    )

    consent_id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    connection_id: str = Field(index=True)
    creation_idempotency_key: str = Field(default="", max_length=256)
    creation_request_digest: str = Field(default="", index=True, max_length=128)
    connection_revision: int = Field(default=1, index=True)
    # The value is encrypted ciphertext, whose storage length is unrelated to
    # the public provider-identity bound.  Keep the 1024-character limit at
    # the API/adapter boundary and use an unrestricted text column here.
    calendar_id: str = Field(default="", sa_type=Text)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(default=1, index=True)
    allowed_fields_json: str = Field(default="[]")
    window_minutes: int = Field(default=60)
    max_events: int = Field(default=10)
    allow_remote_model: bool = Field(default=False)
    expires_at: datetime = Field(index=True)
    state: str = Field(default="active", index=True)
    revision: int = Field(default=1, index=True)
    consent_digest: str = Field(default="", index=True, max_length=128)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class CalendarRescheduleConsent(SQLModel, table=True):
    """Finite exact write permission; never execution or effect authority.

    Expired active grants deliberately retain their unique slot until an
    explicit local revoke. No readonly grant is automatically promoted.
    """

    __tablename__ = "calendar_reschedule_consents"
    __table_args__ = (
        UniqueConstraint("owner_principal_id", "original_root_session_id", "creation_request_uuid",
            name="ux_calendar_reschedule_consent_request"),
        Index("ux_calendar_reschedule_active_grant", "owner_principal_id", "original_root_session_id", "event_binding_id",
            unique=True, sqlite_where=text("state = 'active'"), postgresql_where=text("state = 'active'")),
        CheckConstraint("state IN ('active', 'revoked')", name="ck_calendar_reschedule_consent_state"),
        CheckConstraint("revision > 0 AND goal_revision > 0 AND event_binding_revision > 0 AND read_connection_revision > 0 AND write_connection_revision > 0", name="ck_calendar_reschedule_consent_revisions"),
    )

    consent_id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    original_root_session_id: str = Field(index=True)
    goal_id: str = Field(index=True)
    goal_revision: int
    read_connection_id: str = Field(index=True)
    read_connection_revision: int
    write_connection_id: str = Field(index=True)
    write_connection_revision: int
    profile_binding_digest: str = Field(max_length=64)
    account_identity_digest: str = Field(max_length=64)
    selected_calendar_id_private: str = Field(sa_type=Text)
    selected_calendar_digest: str = Field(max_length=64)
    event_binding_id: str = Field(index=True)
    event_binding_revision: int
    event_identity_digest: str = Field(max_length=128)
    owned_event_read_allowed: bool = Field(default=False)
    calendar_list_metadata_read_allowed: bool = Field(default=False)
    one_conditional_reschedule_allowed: bool = Field(default=False)
    expires_at: datetime = Field(index=True)
    state: str = Field(default="active", index=True)
    revision: int = Field(default=1)
    creation_request_uuid: str = Field(max_length=36)
    creation_request_digest: str = Field(max_length=64)
    consent_digest: str = Field(max_length=64)
    revocation_request_uuid: Optional[str] = Field(default=None, max_length=36)
    revocation_request_digest: Optional[str] = Field(default=None, max_length=64)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class MailLabelBinding(SQLModel, table=True):
    """Owner-private mapping from one opaque UI label key to a Gmail label.

    Gmail label identifiers are provider identities.  They are encrypted at
    rest and are only resolved by the mail adapter after the connection and
    consent fences have been re-read.  ``label_id`` is the stable, opaque key
    that the cockpit may carry between requests.
    """

    __tablename__ = "mail_label_bindings"
    __table_args__ = (
        Index(
            "ix_mail_label_bindings_owner_connection",
            "owner_principal_id",
            "owner_session_id",
            "connection_id",
            "state",
        ),
        UniqueConstraint(
            "owner_principal_id",
            "owner_session_id",
            "connection_id",
            "provider_label_digest",
            name="ux_mail_label_bindings_provider_identity",
        ),
    )

    label_id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    connection_id: str = Field(index=True)
    connection_revision: int = Field(default=1, index=True)
    provider_label_id_ciphertext: str = Field(default="", sa_type=Text)
    provider_label_digest: str = Field(default="", index=True, max_length=128)
    label_name: str = Field(default="", max_length=200)
    label_type: str = Field(default="user", max_length=32)
    state: str = Field(default="active", index=True)
    revision: int = Field(default=1, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class MailReadConsent(SQLModel, table=True):
    """Finite, independent source-read and cloud-text consent for Gmail."""

    __tablename__ = "mail_read_consents"
    __table_args__ = (
        Index(
            "ix_mail_read_consents_owner_state",
            "owner_principal_id",
            "owner_session_id",
            "state",
        ),
        Index("ix_mail_read_consents_connection", "connection_id", "state"),
        Index(
            "ux_mail_read_consents_creation_idempotency",
            "owner_principal_id",
            "owner_session_id",
            "creation_idempotency_key",
            unique=True,
            sqlite_where=text("creation_idempotency_key <> ''"),
            postgresql_where=text("creation_idempotency_key <> ''"),
        ),
    )

    consent_id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    connection_id: str = Field(index=True)
    connection_revision: int = Field(default=1, index=True)
    creation_idempotency_key: str = Field(default="", max_length=256)
    creation_request_digest: str = Field(default="", index=True, max_length=128)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(default=1, index=True)
    label_ids_json: str = Field(default="[]")
    window_days: int = Field(default=7, index=True)
    max_messages: int = Field(default=10, index=True)
    source_read_allowed: bool = Field(default=True, index=True)
    source_revision: int = Field(default=1, index=True)
    source_digest: str = Field(default="", index=True, max_length=128)
    source_reviewed_at: datetime = Field(default_factory=_now, index=True)
    model_egress_allowed: bool = Field(default=False, index=True)
    model_revision: int = Field(default=1, index=True)
    model_digest: str = Field(default="", index=True, max_length=128)
    model_reviewed_at: Optional[datetime] = Field(default=None, index=True)
    allowed_body_fields_json: str = Field(default="[]")
    revoke_idempotency_key: Optional[str] = Field(default=None, index=True, max_length=256)
    revoke_request_digest: Optional[str] = Field(default=None, index=True, max_length=128)
    expires_at: datetime = Field(index=True)
    state: str = Field(default="active", index=True)
    revision: int = Field(default=1, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class MailMessageBinding(SQLModel, table=True):
    """Owner-private metadata identity for one bounded Gmail message read."""

    __tablename__ = "mail_message_bindings"
    __table_args__ = (
        Index(
            "ix_mail_message_bindings_owner_state",
            "owner_principal_id",
            "owner_session_id",
            "status",
        ),
        UniqueConstraint(
            "owner_principal_id",
            "owner_session_id",
            "connection_id",
            "message_key",
            name="ux_mail_message_bindings_identity",
        ),
    )

    message_binding_id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    connection_id: str = Field(index=True)
    connection_revision: int = Field(default=1, index=True)
    # The binding is created under a specific source consent and selected
    # label scope.  A later consent on the same connection must never be able
    # to reinterpret this provider identity.
    source_consent_id: Optional[str] = Field(default=None, index=True)
    source_consent_revision: Optional[int] = Field(default=None, index=True)
    source_label_scope_digest: Optional[str] = Field(default=None, index=True, max_length=128)
    provider_message_id_ciphertext: str = Field(default="", sa_type=Text)
    provider_thread_id_ciphertext: str = Field(default="", sa_type=Text)
    message_key: str = Field(default="", index=True, max_length=128)
    thread_key: str = Field(default="", index=True, max_length=128)
    message_revision: str = Field(default="", index=True, max_length=128)
    received_at: Optional[datetime] = Field(default=None, index=True)
    fetched_at: datetime = Field(default_factory=_now, index=True)
    status: str = Field(default="present", index=True)
    revision: int = Field(default=1, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class MailWatchState(SQLModel, table=True):
    """Bounded metadata cursor for one finite, owner-scoped Gmail watch.

    The scheduler binding and occurrence remain the authority for admission
    and execution.  This row only records the watch's metadata-only coverage
    tuple and a bounded set of opaque message keys so a restart cannot emit a
    second notice for the same observed message.
    """

    __tablename__ = "mail_watch_states"
    __table_args__ = (
        UniqueConstraint("binding_id", name="ux_mail_watch_states_binding"),
        Index(
            "ix_mail_watch_states_owner_state",
            "owner_principal_id",
            "owner_session_id",
            "state",
        ),
    )

    binding_id: str = Field(primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    connection_id: str = Field(index=True)
    connection_revision: int = Field(default=1, index=True)
    consent_id: str = Field(index=True)
    source_consent_revision: int = Field(default=1, index=True)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(default=1, index=True)
    revision: int = Field(default=1, index=True)
    state: str = Field(default="not_started", index=True)
    baseline_complete: bool = Field(default=False, index=True)
    seen_message_keys_json: str = Field(default="[]")
    seen_message_keys_digest: str = Field(default="", index=True, max_length=128)
    window_start_utc: Optional[datetime] = Field(default=None, index=True)
    window_end_utc: Optional[datetime] = Field(default=None, index=True)
    list_fetched_at: Optional[datetime] = Field(default=None, index=True)
    list_page_complete: bool = Field(default=False, index=True)
    last_observed_at: Optional[datetime] = Field(default=None, index=True)
    last_completed_occurrence_id: Optional[str] = Field(default=None, index=True)
    skipped_coverage_reason: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class CalendarEventBinding(SQLModel, table=True):
    """Owner-private handoff from a bounded provider event read to a task."""

    __tablename__ = "calendar_event_bindings"
    __table_args__ = (
        Index("ix_calendar_event_bindings_owner_event", "owner_principal_id", "owner_session_id", "event_key"),
        UniqueConstraint(
            "owner_principal_id",
            "owner_session_id",
            "connection_id",
            "provider_identity_digest",
            name="ux_calendar_event_bindings_provider_identity",
        ),
    )

    event_binding_id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    connection_id: str = Field(index=True)
    connection_revision: int = Field(default=1, index=True)
    consent_id: str = Field(index=True)
    consent_revision: int = Field(default=1, index=True)
    # Provider identities are encrypted at rest by the integration module and
    # are never projected through a generic API or model prompt.
    calendar_id_private: str = Field(default="")
    provider_event_id_private: str = Field(default="")
    recurrence_identity_private: str = Field(default="")
    provider_identity_digest: str = Field(default="", index=True, max_length=128)
    event_key: str = Field(default="", index=True, max_length=128)
    event_revision: str = Field(default="", index=True, max_length=128)
    calendar_list_revision: str = Field(default="", index=True, max_length=128)
    fetched_at: datetime = Field(default_factory=_now, index=True)
    state: str = Field(default="selected", index=True)
    revision: int = Field(default=1, index=True)
    snapshot_digest: str = Field(default="", index=True, max_length=128)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class CalendarPrepReceipt(SQLModel, table=True):
    """Bounded two-read/model/readback receipt for one prep attempt."""

    __tablename__ = "calendar_prep_receipts"
    __table_args__ = (
        Index("ix_calendar_prep_receipts_task", "task_id", "attempt_id"),
        Index("ix_calendar_prep_receipts_owner", "owner_principal_id", "owner_session_id"),
    )

    receipt_id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    task_id: str = Field(index=True)
    attempt_id: str = Field(index=True)
    durable_job_id: str = Field(index=True)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(default=1, index=True)
    connection_id: str = Field(index=True)
    connection_revision: int = Field(default=1, index=True)
    consent_id: str = Field(index=True)
    consent_revision: int = Field(default=1, index=True)
    event_binding_id: str = Field(index=True)
    event_key: str = Field(default="", index=True, max_length=128)
    event_revision_read_1: str = Field(default="", max_length=128)
    event_revision_read_2: str = Field(default="", max_length=128)
    calendar_list_revision: str = Field(default="", max_length=128)
    read_1_json: str = Field(default="{}")
    read_2_json: str = Field(default="{}")
    effective_route_json: str = Field(default="{}")
    output_json: str = Field(default="{}")
    artifact_id: Optional[str] = Field(default=None, index=True)
    file_path: Optional[str] = Field(default=None)
    content_sha256: Optional[str] = Field(default=None, index=True)
    readback_id: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="pending", index=True)
    failure_code: Optional[str] = Field(default=None, index=True)
    recovery_action: Optional[str] = Field(default=None)
    memory_status: str = Field(default="no_learning", index=True)
    expires_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class GovernedScheduleBinding(SQLModel, table=True):
    """Shared owner-bound binding around an existing ScheduledJob trigger."""

    __tablename__ = "governed_schedule_bindings"
    __table_args__ = (
        Index("ix_governed_schedule_bindings_owner_state", "owner_principal_id", "owner_session_id", "state"),
        UniqueConstraint("scheduled_job_id", name="ux_governed_schedule_binding_job"),
        UniqueConstraint(
            "owner_principal_id",
            "owner_session_id",
            "schedule_idempotency_key",
            name="ux_governed_schedule_binding_idempotency",
        ),
    )

    binding_id: str = Field(default_factory=_uuid, primary_key=True)
    scheduled_job_id: str = Field(index=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(default=1, index=True)
    capability_id: str = Field(default="calendar.observe_due_events.v1", index=True)
    action_type: str = Field(default="calendar.observe_due_events.v1", index=True)
    input_artifact_id: str = Field(index=True)
    input_digest: str = Field(default="", index=True, max_length=128)
    action_digest: str = Field(default="", index=True, max_length=128)
    consent_kind: str = Field(default="calendar_read", index=True)
    # Goal-budget/system schedules may omit a Calendar read grant. Calendar
    # observation bindings still require a nonempty consent at admission.
    read_consent_id: Optional[str] = Field(default=None, index=True)
    consent_revision: int = Field(default=1, index=True)
    consent_digest: str = Field(default="", index=True, max_length=128)
    schedule_idempotency_key: str = Field(default="", max_length=256)
    schedule_request_digest: str = Field(default="", index=True, max_length=128)
    cadence_kind: str = Field(default="5min", index=True)
    timezone: str = Field(default="UTC")
    daily_hour: Optional[int] = Field(default=None)
    daily_minute: Optional[int] = Field(default=None)
    binding_revision: int = Field(default=1, index=True)
    expires_at: datetime = Field(index=True)
    state: str = Field(default="active", index=True)
    last_slot_utc: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class GovernedScheduleOccurrence(SQLModel, table=True):
    """One immutable UTC scheduler occurrence/idempotency fence."""

    __tablename__ = "governed_schedule_occurrences"
    __table_args__ = (
        Index("ix_governed_schedule_occurrences_binding_slot", "binding_id", "slot_utc"),
        UniqueConstraint("binding_id", "slot_utc", name="ux_governed_schedule_occurrence_slot"),
    )

    occurrence_id: str = Field(default_factory=_uuid, primary_key=True)
    binding_id: str = Field(index=True)
    binding_revision: int = Field(default=1, index=True)
    slot_utc: datetime = Field(index=True)
    idempotency_key: str = Field(default="", index=True, max_length=256)
    request_digest: str = Field(default="", index=True, max_length=128)
    claim_token: Optional[str] = Field(default=None)
    fencing_token: int = Field(default=0)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    state: str = Field(default="reserved", index=True)
    work_board_task_id: Optional[str] = Field(default=None, index=True)
    durable_job_id: Optional[str] = Field(default=None, index=True)
    metadata_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class GuardianSourceWatch(SQLModel, table=True):
    """Owner-bound source watch configuration and its scheduler fence."""

    __tablename__ = "guardian_source_watches"
    __table_args__ = (
        Index("ux_guardian_source_watches_scheduled_job", "scheduled_job_id", unique=True),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    goal_id: str = Field(index=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    state: str = Field(default="active", index=True)
    capability_id: str = Field(default="guardian.research-watch.v1", index=True)
    capability_version: str = Field(default="1", index=True)
    goal_revision: int = Field(default=1, index=True)
    plan_revision: int = Field(default=1, index=True)
    sources_json: str = Field(default="[]")
    criteria_json: str = Field(default="{}")
    schedule_spec_json: str = Field(default="{}")
    read_authority_json: str = Field(default="{}")
    write_authority_json: str = Field(default="{}")
    write_mode: str = Field(default="approval_each_run", index=True)
    scheduled_job_id: str = Field(index=True)
    source_set_digest: str = Field(default="", index=True)
    criteria_digest: str = Field(default="", index=True)
    active_job_id: Optional[str] = Field(default=None, index=True)
    active_job_fence: int = Field(default=0, index=True)
    active_job_started_at: Optional[datetime] = Field(default=None)
    last_run_identity: Optional[str] = Field(default=None, index=True)
    last_status: Optional[str] = Field(default=None, index=True)
    last_error_code: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class GuardianSourceBaseline(SQLModel, table=True):
    """Canonical local baseline for one source identity generation."""

    __tablename__ = "guardian_source_baselines"
    __table_args__ = (
        Index(
            "ux_guardian_source_baselines_watch_source",
            "watch_id",
            "source_key",
            unique=True,
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    watch_id: str = Field(index=True)
    source_key: str = Field(index=True)
    kind: str = Field(default="")
    target: str = Field(default="")
    identity_digest: str = Field(default="", index=True)
    generation: int = Field(default=1)
    baseline_text: str = Field(default="")
    baseline_sha256: str = Field(default="", index=True)
    etag: Optional[str] = Field(default=None)
    last_modified: Optional[str] = Field(default=None)
    observed_at: datetime = Field(default_factory=_now, index=True)
    state: str = Field(default="missing", index=True)
    last_error_code: Optional[str] = Field(default=None, index=True)
    updated_at: datetime = Field(default_factory=_now)


class GuardianDecisionPacket(SQLModel, table=True):
    """Immutable observed checkpoint and verified local artifact handoff."""

    __tablename__ = "guardian_decision_packets"
    __table_args__ = (
        Index(
            "ux_guardian_decision_packets_watch_input",
            "watch_id",
            "input_digest",
            unique=True,
        ),
        Index(
            "ix_guardian_decision_packets_inbox_pending_updated",
            "inbox_pending",
            "updated_at",
            "id",
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    source_watch_id: str = Field(index=True)
    watch_id: str = Field(index=True)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(default=1, index=True)
    plan_revision: int = Field(default=1, index=True)
    run_identity: str = Field(index=True)
    input_digest: str = Field(default="", index=True)
    criteria_digest: str = Field(default="", index=True)
    source_observation_json: str = Field(default="{}")
    material_source_keys_json: str = Field(default="[]")
    proposal_text: str = Field(default="")
    task_text: str = Field(default="")
    status: str = Field(default="prepared", index=True)
    approval_id: Optional[str] = Field(default=None, index=True)
    dossier_path: Optional[str] = Field(default=None)
    dossier_artifact_id: Optional[str] = Field(default=None, index=True)
    dossier_sha256: Optional[str] = Field(default=None, index=True)
    task_path: Optional[str] = Field(default=None)
    task_artifact_id: Optional[str] = Field(default=None, index=True)
    task_sha256: Optional[str] = Field(default=None, index=True)
    verification_status: str = Field(default="pending", index=True)
    memory_status: str = Field(default="no_learning", index=True)
    strategy_delta_id: Optional[str] = Field(default=None, index=True)
    observed_checkpoint_json: str = Field(default="{}")
    observed_checkpoint_sha256: str = Field(default="", index=True)
    redaction_manifest_json: str = Field(default="{}")
    outcome_json: str = Field(default="{}")
    failure_code: Optional[str] = Field(default=None, index=True)
    # Set in the packet finalization transaction when a finite-budget,
    # material, verified completion still needs its inbox projection.
    inbox_pending: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now)


class GitHubFollowthroughConnection(SQLModel, table=True):
    """Operator-owned GitHub binding for the bounded follow-through path.

    The vault key is a server-side reference only. It is excluded from API
    projections, durable job inputs, artifacts, and audit details.
    """

    __tablename__ = "github_followthrough_connections"
    __table_args__ = (
        Index(
            "ux_github_followthrough_connections_owner",
            "owner_principal_id",
            unique=True,
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    repository: str = Field(default="")
    vault_key: str = Field(default="")
    revision: int = Field(default=1, index=True)
    mode: str = Field(default="disabled", index=True)
    active_job_id: Optional[str] = Field(default=None, index=True)
    active_fence: Optional[int] = Field(default=None, index=True)
    consent_id: Optional[str] = Field(default=None)
    consent_owner_session_id: Optional[str] = Field(default=None)
    consent_actions_json: Optional[str] = Field(default=None)
    consent_issued_at: Optional[datetime] = Field(default=None)
    consent_expires_at: Optional[datetime] = Field(default=None)
    consent_connection_revision: Optional[int] = Field(default=None)
    consent_payload_digest: Optional[str] = Field(default=None)
    consent_revoked_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class GuardianInboxDisposition(SQLModel, table=True):
    """Owner/session-bound disposition for one verified source packet.

    This row deliberately stores only opaque identities, digests and delivery
    state.  The decision packet and its canonical artifacts remain the source
    of truth for evidence and execution metadata.
    """

    __tablename__ = "guardian_inbox_dispositions"
    __table_args__ = (
        Index(
            "ux_guardian_inbox_dispositions_source",
            "owner_principal_id",
            "source_kind",
            "source_id",
            unique=True,
        ),
        Index(
            "ix_guardian_inbox_dispositions_owner_state",
            "owner_principal_id",
            "owner_session_id",
            "state",
            "created_at",
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    source_kind: str = Field(default="source_packet", index=True)
    source_id: str = Field(index=True)
    source_digest: str = Field(default="", index=True)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(default=1, index=True)
    watch_id: str = Field(index=True)
    plan_revision: int = Field(default=1, index=True)
    state: str = Field(default="pending", index=True)
    revision: int = Field(default=1, index=True)
    snoozed_until: Optional[datetime] = Field(default=None, index=True)
    expires_at: datetime = Field(index=True)
    task_id: Optional[str] = Field(default=None, index=True)
    last_action_receipt_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class GuardianInboxAction(SQLModel, table=True):
    """Append-only, owner/session-scoped inbox action receipt."""

    __tablename__ = "guardian_inbox_actions"
    __table_args__ = (
        Index(
            "ux_guardian_inbox_actions_idempotency",
            "owner_principal_id",
            "owner_session_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_guardian_inbox_actions_item",
            "owner_principal_id",
            "owner_session_id",
            "item_id",
            "created_at",
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    item_id: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    payload_digest: str = Field(default="", index=True)
    action: str = Field(default="", index=True)
    prior_revision: int = Field(default=1)
    result_revision: int = Field(default=1)
    task_id: Optional[str] = Field(default=None, index=True)
    safe_result_json: str = Field(default="{}")
    # A bounded, server-redacted operator reason.  Keep this nullable so
    # rows written before inbox history shipped remain distinguishable from a
    # new action that explicitly supplied no reason.
    safe_reason: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)


class GuardianRoutine(SQLModel, table=True):
    """Owner-bound reusable guardian routine metadata.

    The row is only a selector and provenance index.  Authority remains in
    the current package review, source-watch grants, and invocation approvals.
    """

    __tablename__ = "guardian_routines"

    id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    name: str = Field(default="", index=True)
    state: str = Field(default="prepared", index=True)
    revision: int = Field(default=1, index=True)
    current_version: Optional[int] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class GuardianRoutineVersion(SQLModel, table=True):
    """Immutable routine bytes and verified M1/M3 provenance."""

    __tablename__ = "guardian_routine_versions"
    __table_args__ = (
        Index("ux_guardian_routine_versions_routine_version", "routine_id", "version", unique=True),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    routine_id: str = Field(index=True)
    version: int = Field(default=1, index=True)
    source_provenance_json: str = Field(default="{}")
    workflow_bytes: str = Field(default="")
    workflow_sha256: str = Field(default="", index=True)
    runbook_bytes: str = Field(default="")
    runbook_sha256: str = Field(default="", index=True)
    installed_package_digest: Optional[str] = Field(default=None, index=True)
    source_repository: Optional[str] = Field(default=None)
    source_action: Optional[str] = Field(default=None)
    source_issue_number: Optional[int] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)
    installed_at: Optional[datetime] = Field(default=None, index=True)


class WorkBoardRoutineBinding(SQLModel, table=True):
    """Durable preview/create idempotency binding for board-derived routines.

    This row is the recovery boundary between the operator's non-persistent
    preview and the existing ``GuardianRoutine`` lifecycle.  It contains
    opaque source identities and digests only; source text, approvals, grants,
    and credentials never belong here.
    """

    __tablename__ = "work_board_routine_bindings"
    __table_args__ = (
        Index(
            "ux_work_board_routine_bindings_idempotency",
            "owner_principal_id",
            "owner_session_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ux_work_board_routine_bindings_deterministic_routine",
            "deterministic_routine_id",
            unique=True,
        ),
    )

    binding_id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    preview_digest: str = Field(default="", index=True)
    source_task_id: str = Field(index=True)
    action_task_id: str = Field(index=True)
    routine_name: str = Field(default="", max_length=80)
    deterministic_routine_id: str = Field(index=True)
    routine_id: Optional[str] = Field(default=None, index=True)
    install_job_id: Optional[str] = Field(default=None, index=True)
    # ``state`` is a recovery projection for the binding transaction.  The
    # routine row and durable install job remain the authority for execution.
    state: str = Field(default="pending", index=True)
    recovery_reason: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)
    revision: int = Field(default=1, index=True)


class ProcedureV2Binding(SQLModel, table=True):
    """Metadata-only preparation fence for a reviewed v2 procedure.

    Immutable procedure bytes and provenance remain in
    ``GuardianRoutineVersion``. This row is only the owner/session,
    idempotency, preview-expiry, and restart-reconciliation boundary between
    a source-proof preview and that existing routine lifecycle.
    """

    __tablename__ = "procedure_v2_bindings"
    __table_args__ = (
        Index(
            "ux_procedure_v2_bindings_idempotency",
            "owner_principal_id",
            "owner_session_id",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ux_procedure_v2_bindings_deterministic_routine",
            "deterministic_routine_id",
            unique=True,
        ),
    )

    binding_id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    idempotency_key: str = Field(index=True, max_length=256)
    request_digest: str = Field(default="", index=True, max_length=128)
    source_refs_json: str = Field(default="{}")
    deterministic_routine_id: str = Field(index=True)
    routine_name: str = Field(default="", max_length=80)
    template_id: str = Field(default="", index=True, max_length=80)
    version_id: Optional[str] = Field(default=None, index=True)
    preview_digest: str = Field(default="", index=True, max_length=128)
    preview_expires_at: datetime = Field(index=True)
    state: str = Field(default="preparing", index=True)
    recovery_reason: Optional[str] = Field(default=None, index=True)
    revision: int = Field(default=1, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


# ─── Operator work board ────────────────────────────────


class WorkBoardTask(SQLModel, table=True):
    """One authenticated operator's durable task intent and projection.

    ``creation_sequence`` is the SQLite insertion sequence used for stable
    FIFO ordering.  ``task_id`` is the public opaque identifier used by API
    callers and relationships, so changing the presentation identifier never
    changes the ordering key.
    """

    __tablename__ = "work_board_tasks"
    __table_args__ = (
        Index(
            "ix_work_board_tasks_ready_order",
            "status",
            "priority",
            "creation_sequence",
        ),
        Index(
            "ux_work_board_tasks_idempotency",
            "owner_principal_id",
            "owner_session_id",
            "idempotency_scope",
            "idempotency_key",
            unique=True,
        ),
        {"sqlite_autoincrement": True},
    )

    creation_sequence: Optional[int] = Field(
        default=None,
        sa_column=Column(Integer, primary_key=True, autoincrement=True),
    )
    task_id: str = Field(default_factory=_uuid, index=True, unique=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    origin_session_id: Optional[str] = Field(default=None, index=True)
    origin_thread_id: Optional[str] = Field(default=None, index=True)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(default=1, index=True)
    title: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=4_000)
    capability_id: Optional[str] = Field(default=None, index=True)
    # Server-bound typed input artifact.  The artifact row remains the
    # authority for state/digest; this opaque pointer makes owner-scoped task
    # projections and the task/artifact CAS cheap without exposing input bytes.
    input_artifact_id: Optional[str] = Field(default=None, index=True)
    # Metadata-only reviewed proposal binding; the task and durable native
    # job remain execution authority, never the proposal itself.
    pipeline_operation_id: Optional[str] = Field(default=None, index=True)
    pipeline_slot: Optional[str] = Field(default=None, index=True)
    typed_input_ref: Optional[str] = Field(default=None, index=True)
    typed_input_digest: Optional[str] = Field(default=None, index=True)
    executor_id: Optional[str] = Field(default=None, index=True)
    assignee_id: Optional[str] = Field(default=None, index=True)
    priority: int = Field(default=50, index=True)
    idempotency_scope: str = Field(default="task", index=True)
    idempotency_key: str = Field(index=True)
    idempotency_payload_digest: str = Field(default="", index=True)
    idempotency_binding: Optional[str] = Field(default=None, index=True)
    scheduled_at: Optional[datetime] = Field(default=None, index=True)
    status: WorkBoardStatus = Field(default=WorkBoardStatus.triage, index=True)
    block_kind: Optional[str] = Field(default=None, index=True)
    block_reason: Optional[str] = Field(default=None)
    block_source_status: Optional[str] = Field(default=None, index=True)
    requires_review: bool = Field(default=False, index=True)
    reviewer_id: Optional[str] = Field(default=None, index=True)
    review_expires_at: Optional[datetime] = Field(default=None, index=True)
    # A worker/operator request is an intent.  The dispatcher may project it
    # to Review only after it rechecks the authoritative durable run and
    # independent readback.  These fields bind the intent to one fenced
    # attempt and one board revision so a late worker cannot promote a newer
    # attempt.
    review_request_attempt_id: Optional[str] = Field(default=None, index=True)
    review_request_fence: Optional[int] = Field(default=None, index=True)
    review_request_revision: Optional[int] = Field(default=None, index=True)
    review_request_digest: Optional[str] = Field(default=None, index=True)
    review_request_evidence_json: str = Field(default="[]")
    review_requested_at: Optional[datetime] = Field(default=None, index=True)
    task_revision: int = Field(default=1, index=True)
    result_refs_json: str = Field(default="[]")
    artifact_refs_json: str = Field(default="[]")
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)
    completed_at: Optional[datetime] = Field(default=None, index=True)
    archived_at: Optional[datetime] = Field(default=None, index=True)


class WorkBoardInputArtifact(SQLModel, table=True):
    """Owner-bound canonical typed input for one executable board task.

    The JSON payload lives below the canonical workspace artifact root. This
    row stores only its verified digest/reference and immutable owner, goal,
    capability, idempotency, and binding metadata. API projections never
    return the input mapping.
    """

    __tablename__ = "work_board_input_artifacts"
    __table_args__ = (
        Index(
            "ux_work_board_input_artifacts_idempotency",
            "owner_principal_id",
            "owner_session_id",
            "capability_id",
            "goal_id",
            "goal_revision",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_work_board_input_artifacts_payload",
            "owner_principal_id",
            "owner_session_id",
            "capability_id",
            "goal_id",
            "goal_revision",
            "payload_sha256",
        ),
        Index(
            "ix_work_board_input_artifacts_state_expiry",
            "state",
            "expires_at",
        ),
    )

    artifact_id: str = Field(primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(index=True)
    capability_id: str = Field(index=True)
    capability_version: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    payload_sha256: str = Field(index=True)
    typed_input_ref: str = Field(index=True)
    size_bytes: int = Field(default=0)
    state: str = Field(default="pending", index=True)
    bound_task_id: Optional[str] = Field(default=None, index=True)
    bound_task_revision: Optional[int] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    expires_at: datetime = Field(index=True)
    consumed_at: Optional[datetime] = Field(default=None, index=True)
    revision: int = Field(default=1, index=True)
    metadata_digest: Optional[str] = Field(default=None, index=True)
    # ADR017 private pair reservation/quota/generation metadata. Source bytes
    # remain encrypted files; this is the existing canonical owner row.
    document_metadata_json: Optional[str] = Field(default=None)
    document_reserved_bytes: int = Field(default=0)


class RepoRepairSourcePacket(SQLModel, table=True):
    """Immutable, owner-bound source evidence for one repository repair.

    The selected source text lives in the private workspace artifact named by
    ``artifact_id``.  This row is deliberately a metadata/provenance index;
    generic board projections must never copy its source text.
    """

    __tablename__ = "repo_repair_source_packets"
    __table_args__ = (
        Index(
            "ux_repo_repair_source_packets_job_input",
            "workflow_run_id",
            "input_digest",
            unique=True,
        ),
        Index(
            "ix_repo_repair_source_packets_owner_state",
            "owner_principal_id",
            "owner_session_id",
            "state",
            "created_at",
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    work_board_task_id: str = Field(index=True)
    work_board_attempt_id: str = Field(index=True)
    workflow_run_id: str = Field(index=True)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(default=1, index=True)
    input_digest: str = Field(default="", index=True, max_length=128)
    repository_ref: str = Field(default="", index=True, max_length=512)
    base_snapshot_digest: str = Field(default="", index=True, max_length=128)
    source_manifest_digest: str = Field(default="", index=True, max_length=128)
    artifact_id: str = Field(default="", index=True, unique=True, max_length=256)
    artifact_sha256: str = Field(default="", index=True, max_length=128)
    manifest_json: str = Field(default="{}")
    state: str = Field(default="inspected", index=True)
    revision: int = Field(default=1, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)

    @property
    def source_packet_id(self) -> str:
        """Compatibility alias used by the repair service and API DTOs."""

        return self.id


class RepoRepairProposal(SQLModel, table=True):
    """Immutable model patch proposal awaiting a separate operator approval."""

    __tablename__ = "repo_repair_proposals"
    __table_args__ = (
        Index(
            "ux_repo_repair_proposals_owner_operation",
            "owner_principal_id",
            "owner_session_id",
            "workflow_run_id",
            "operation_key",
            unique=True,
        ),
        Index(
            "ix_repo_repair_proposals_owner_status",
            "owner_principal_id",
            "owner_session_id",
            "status",
            "expires_at",
        ),
    )

    proposal_id: str = Field(default_factory=_uuid, primary_key=True)
    operation_key: str = Field(default="", index=True, max_length=256)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    work_board_task_id: str = Field(index=True)
    work_board_attempt_id: str = Field(index=True)
    workflow_run_id: str = Field(index=True)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(default=1, index=True)
    repository_ref: str = Field(default="", index=True, max_length=512)
    base_snapshot_digest: str = Field(default="", index=True, max_length=128)
    source_packet_id: str = Field(default="", index=True, max_length=256)
    source_digest: str = Field(default="", index=True, max_length=128)
    model_runtime_path: str = Field(default="strategist_agent", index=True)
    model_profile_id: str = Field(default="", index=True, max_length=256)
    model_request_digest: str = Field(default="", index=True, max_length=128)
    model_output_digest: str = Field(default="", index=True, max_length=128)
    model_response_artifact_id: Optional[str] = Field(default=None, index=True, max_length=256)
    model_response_artifact_sha256: Optional[str] = Field(default=None, index=True, max_length=128)
    patch_artifact_id: str = Field(default="", index=True, max_length=256)
    patch_sha256: str = Field(default="", index=True, max_length=128)
    allowed_paths_json: str = Field(default="[]")
    test_args_json: str = Field(default="[]")
    request_digest: str = Field(default="", index=True, max_length=128)
    authority_digest: str = Field(default="", index=True, max_length=128)
    approval_id: Optional[str] = Field(default=None, index=True, max_length=256)
    approval_fingerprint: Optional[str] = Field(default=None, index=True, max_length=128)
    last_receipt_id: Optional[str] = Field(default=None, index=True, max_length=256)
    status: str = Field(default="prepared", index=True)
    safe_metadata_json: str = Field(default="{}")
    expires_at: datetime = Field(index=True)
    revision: int = Field(default=1, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class RepoRepairEgressConsent(SQLModel, table=True):
    """Explicit, finite consent to send one inspected source packet remotely."""

    __tablename__ = "repo_repair_egress_consents"
    __table_args__ = (
        Index(
            "ux_repo_repair_egress_consents_owner_request",
            "owner_principal_id",
            "owner_session_id",
            "request_key",
            unique=True,
        ),
        Index(
            "ix_repo_repair_egress_consents_job_state",
            "workflow_run_id",
            "state",
            "expires_at",
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    work_board_task_id: str = Field(index=True)
    work_board_attempt_id: str = Field(index=True)
    workflow_run_id: str = Field(index=True)
    source_packet_id: str = Field(index=True, max_length=256)
    source_digest: str = Field(default="", index=True, max_length=128)
    source_manifest_digest: str = Field(default="", index=True, max_length=128)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(default=1, index=True)
    input_digest: str = Field(default="", index=True, max_length=128)
    runtime_path: str = Field(default="strategist_agent", index=True)
    effective_profile_id: str = Field(default="", index=True, max_length=256)
    effective_upstream: str = Field(default="", index=True, max_length=256)
    maximum_input_bytes: int = Field(default=64 * 1024)
    maximum_output_tokens: int = Field(default=4096)
    expires_at: datetime = Field(index=True)
    state: str = Field(default="active", index=True)
    revision: int = Field(default=1, index=True)
    consent_digest: str = Field(default="", index=True, max_length=128)
    request_key: str = Field(default="", index=True, max_length=256)
    request_digest: str = Field(default="", index=True, max_length=128)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)

    @property
    def consent_id(self) -> str:
        """Compatibility alias for the API-facing opaque consent identity."""

        return self.id


class WorkBoardAttempt(SQLModel, table=True):
    """Historical execution attempt linked to at most one durable run."""

    __tablename__ = "work_board_attempts"
    __table_args__ = (
        Index(
            "ux_work_board_attempts_active_task",
            "task_id",
            unique=True,
            sqlite_where=text("ended_at IS NULL"),
        ),
        Index(
            "ux_work_board_attempts_workflow_run",
            "workflow_run_id",
            unique=True,
            sqlite_where=text("workflow_run_id IS NOT NULL"),
        ),
    )

    attempt_id: str = Field(default_factory=_uuid, primary_key=True)
    task_id: str = Field(foreign_key="work_board_tasks.task_id", index=True)
    workflow_run_id: Optional[str] = Field(default=None, index=True)
    task_revision_at_claim: int = Field(default=1, index=True)
    lease_owner: Optional[str] = Field(default=None, index=True)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    heartbeat_at: Optional[datetime] = Field(default=None, index=True)
    fencing_token: int = Field(default=0, index=True)
    executor_id: str = Field(default="", index=True)
    started_at: Optional[datetime] = Field(default=None, index=True)
    ended_at: Optional[datetime] = Field(default=None, index=True)
    cancel_requested_at: Optional[datetime] = Field(default=None, index=True)
    outcome: Optional[str] = Field(default=None, index=True)
    # Immutable, source-verified parent context captured at the fenced claim.
    # Persisting it on the attempt keeps admission digests and restart recovery
    # bound to the same handoffs even if a parent is archived later.
    parent_handoff_context_json: str = Field(default="[]")
    parent_handoff_digest: Optional[str] = Field(default=None)
    receipt_refs_json: str = Field(default="[]")
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class WorkBoardReviewIntent(SQLModel, table=True):
    """Immutable worker/operator request waiting for dispatcher verification."""

    __tablename__ = "work_board_review_intents"
    __table_args__ = (
        Index(
            "ux_work_board_review_intents_binding",
            "owner_principal_id",
            "owner_session_id",
            "task_id",
            "attempt_id",
            "fencing_token",
            "task_revision",
            unique=True,
        ),
    )

    intent_id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    task_id: str = Field(foreign_key="work_board_tasks.task_id", index=True)
    attempt_id: str = Field(index=True)
    workflow_run_id: str = Field(default="", index=True)
    fencing_token: int = Field(default=0, index=True)
    task_revision: int = Field(default=1, index=True)
    request_digest: str = Field(default="", index=True)
    evidence_refs_json: str = Field(default="[]")
    status: str = Field(default="pending", index=True)
    created_at: datetime = Field(default_factory=_now, index=True)


class WorkBoardLink(SQLModel, table=True):
    """Parent-to-child task dependency in the same canonical workspace."""

    __tablename__ = "work_board_links"
    __table_args__ = (
        UniqueConstraint(
            "parent_task_id",
            "child_task_id",
            name="ux_work_board_links_parent_child",
        ),
    )

    link_id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    parent_task_id: str = Field(foreign_key="work_board_tasks.task_id", index=True)
    child_task_id: str = Field(foreign_key="work_board_tasks.task_id", index=True)
    current_handoff_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)


class WorkBoardComment(SQLModel, table=True):
    """Bounded operator handoff/comment record."""

    __tablename__ = "work_board_comments"

    comment_id: str = Field(default_factory=_uuid, primary_key=True)
    task_id: str = Field(foreign_key="work_board_tasks.task_id", index=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    author_principal_id: str = Field(index=True)
    author_session_id: str = Field(index=True)
    body: str = Field(default="", max_length=2_000)
    created_at: datetime = Field(default_factory=_now, index=True)


class WorkBoardEvent(SQLModel, table=True):
    """Append-only safe metadata event with a global monotonic cursor."""

    __tablename__ = "work_board_events"
    __table_args__ = (
        Index("ux_work_board_events_mutation_key", "owner_principal_id",
              "owner_session_id", "mutation_idempotency_key", unique=True),
    )

    event_id: Optional[int] = Field(
        default=None,
        sa_column=Column(Integer, primary_key=True, autoincrement=True),
    )
    task_id: str = Field(foreign_key="work_board_tasks.task_id", index=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    actor_principal_id: str = Field(index=True)
    actor_session_id: Optional[str] = Field(default=None, index=True)
    kind: str = Field(index=True)
    metadata_json: str = Field(default="{}")
    # Ordinary historical events keep NULL. Explicit evidence mutations bind
    # a canonical UUID and complete request digest in this same immutable row.
    mutation_idempotency_key: Optional[str] = Field(default=None, max_length=36)
    mutation_request_digest: Optional[str] = Field(default=None, max_length=64)
    created_at: datetime = Field(default_factory=_now, index=True)


class WorkBoardEvidenceDependency(SQLModel, table=True):
    """Active exact source/span execution preconditions, never fact text.

    Replacement/revocation removes rows in the task CAS. Prior bounded token
    metadata remains only in immutable WorkBoardEvent history.
    """

    __tablename__ = "work_board_evidence_dependencies"
    __table_args__ = (
        UniqueConstraint("task_id", "source_kind", "canonical_source_id", "span_digest",
                         name="ux_work_board_evidence_task_source_span"),
        Index("ix_work_board_evidence_owner_source", "owner_principal_id",
              "owner_session_id", "source_kind", "canonical_source_id", "task_id"),
    )

    dependency_id: str = Field(default_factory=_uuid, primary_key=True)
    task_id: str = Field(foreign_key="work_board_tasks.task_id", index=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    goal_id: str = Field(index=True)
    source_kind: str = Field(max_length=64)
    canonical_source_id: str = Field(max_length=256)
    source_id: str = Field(max_length=64)
    source_digest: str = Field(max_length=64)
    span_digest: str = Field(max_length=64)
    resolved_token_json: str = Field(max_length=8192)
    packet_revision: int = Field(ge=1)
    packet_digest: str = Field(max_length=64)
    binding_task_revision: int = Field(ge=1)
    executor_input_digest: str = Field(max_length=64)
    pipeline_operation_id: Optional[str] = Field(default=None, max_length=256)
    pipeline_slot: Optional[str] = Field(default=None, max_length=256)


class WorkBoardProposal(SQLModel, table=True):
    """Operator reviewed triage proposal staged before task creation.

    Proposal rows are deliberately non-executable.  They bind the source task,
    owner/session, revision and idempotency key so a retried inference request
    can return the same pending/proposed record without creating board tasks or
    spending a second remote request.
    """

    __tablename__ = "work_board_proposals"
    __table_args__ = (
        Index(
            "ux_work_board_proposals_idempotency",
            "owner_principal_id",
            "owner_session_id",
            "parent_task_id",
            "parent_revision",
            "kind",
            "idempotency_key",
            unique=True,
        ),
    )

    proposal_id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    parent_task_id: str = Field(index=True)
    parent_revision: int = Field(default=1, index=True)
    goal_revision: int = Field(default=1, index=True)
    kind: str = Field(index=True)
    idempotency_key: str = Field(index=True)
    # Complete immutable admission binding.  A retry may reuse the durable
    # operation only when every field below still matches.
    request_digest: str = Field(default="", index=True)
    capability_id: str = Field(default="strategist_agent", index=True)
    capability_version: str = Field(default="", index=True)
    authority_digest: str = Field(default="", index=True)
    grant_revision: int = Field(default=1, index=True)
    input_digest: str = Field(default="", index=True)
    route_id: str = Field(default="strategist_agent", index=True)
    admission_job_id: str = Field(default_factory=_uuid, index=True, unique=True)
    effect_id_digest: str = Field(default="", index=True)
    provider_contact_started: bool = Field(default=False, index=True)
    provider_contact_state: str = Field(default="not_started", index=True)
    status: str = Field(default="pending_inference", index=True)
    proposal_json: str = Field(default="{}")
    proposal_digest: str = Field(default="", index=True)
    # Server-resolved text-free actually used evidence; NULL is historical.
    evidence_use_snapshot_json: Optional[str] = Field(default=None)
    estimated_cost: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)
    expires_at: datetime = Field(index=True)
    revision: int = Field(default=1, index=True)


class WorkBoardHandoff(SQLModel, table=True):
    """Bounded persisted evidence handed from one completed task to a child."""

    __tablename__ = "work_board_handoffs"
    __table_args__ = (
        Index(
            "ux_work_board_handoffs_version",
            "owner_principal_id",
            "owner_session_id",
            "parent_task_id",
            "child_task_id",
            "link_id",
            "source_attempt_id",
            "source_task_revision",
            unique=True,
        ),
    )

    handoff_id: str = Field(default_factory=_uuid, primary_key=True)
    schema_version: str = Field(default="work_board_handoff.v1", index=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    parent_task_id: str = Field(foreign_key="work_board_tasks.task_id", index=True)
    child_task_id: str = Field(foreign_key="work_board_tasks.task_id", index=True)
    link_id: str = Field(foreign_key="work_board_links.link_id", index=True)
    source_attempt_id: str = Field(index=True)
    workflow_run_id: str = Field(index=True)
    source_task_revision: int = Field(default=1, index=True)
    summary: str = Field(default="", max_length=500)
    artifact_refs_json: str = Field(default="[]")
    result_refs_json: str = Field(default="[]")
    verification_json: str = Field(default="{}")
    risks_json: str = Field(default="[]")
    created_at: datetime = Field(default_factory=_now, index=True)


class WorkflowRunState(SQLModel, table=True):
    __tablename__ = "workflow_run_states"
    __table_args__ = (
        Index(
            "ux_workflow_run_states_idempotency_binding",
            "idempotency_binding",
            unique=True,
            sqlite_where=text("idempotency_binding IS NOT NULL"),
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    run_identity: str = Field(index=True, unique=True)
    root_run_identity: str = Field(index=True)
    parent_run_identity: Optional[str] = Field(default=None, index=True)
    workflow_name: str = Field(index=True)
    tool_name: str = Field(default="", index=True)
    session_id: Optional[str] = Field(default=None, foreign_key="sessions.id", index=True)
    # ``session_id`` is the execution/conversation scope.  The browser
    # authentication session is a separate durable binding so recovery can be
    # resumed from a different operator session without confusing the two.
    conversation_id: Optional[str] = Field(default=None, index=True)
    operator_session_id: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="running", index=True)
    branch_kind: Optional[str] = Field(default=None, index=True)
    branch_depth: int = Field(default=0)
    run_fingerprint: str = Field(default="", index=True)
    arguments_json: str = Field(default="{}")
    approval_context_json: Optional[str] = Field(default=None)
    checkpoint_context_json: Optional[str] = Field(default=None)
    artifact_paths_json: str = Field(default="[]")
    continued_error_steps_json: str = Field(default="[]")
    last_completed_step_id: Optional[str] = Field(default=None, index=True)
    error: Optional[str] = Field(default=None)
    heartbeat_at: datetime = Field(default_factory=_now, index=True)
    started_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)
    finished_at: Optional[datetime] = Field(default=None, index=True)
    metadata_json: Optional[str] = Field(default=None)
    # Durable invocation contract (additive to the legacy workflow projection).
    record_schema_version: int = Field(default=2, index=True)
    parent_job_id: Optional[str] = Field(default=None, index=True)
    parent_fencing_token: Optional[int] = Field(default=None, index=True)
    job_kind: str = Field(default="workflow", index=True)
    owner_kind: str = Field(default="legacy", index=True)
    owner_principal_id: Optional[str] = Field(default=None, index=True)
    service_id: Optional[str] = Field(default=None, index=True)
    goal_id: Optional[str] = Field(default=None, index=True)
    goal_revision: Optional[int] = Field(default=None, index=True)
    plan_revision: Optional[int] = Field(default=None, index=True)
    candidate_id: Optional[str] = Field(default=None, index=True)
    capability_version: str = Field(default="workflow-v1", index=True)
    input_digest: Optional[str] = Field(default=None, index=True)
    authority_digest: Optional[str] = Field(default=None, index=True)
    # A digest binds the execution budget without retaining a raw allowance
    # in the durable projection.  ``None`` is represented by the canonical
    # digest for an absent budget by the typed repository.
    budget_digest: Optional[str] = Field(default=None, index=True)
    idempotency_scope: Optional[str] = Field(default=None, index=True)
    idempotency_key: Optional[str] = Field(default=None, index=True)
    idempotency_binding: Optional[str] = Field(default=None, index=True)
    priority: int = Field(default=50, index=True)
    dependencies_json: str = Field(default="[]")
    resource_claims_json: str = Field(default="[]")
    declared_authority_json: Optional[str] = Field(default=None)
    deadline_at: Optional[datetime] = Field(default=None, index=True)
    lease_owner: Optional[str] = Field(default=None, index=True)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    fencing_token: int = Field(default=0, index=True)
    # Monotonic compare-and-swap revision for typed durable-job writes. This
    # is separate from ``fencing_token``: receipt writes may advance the row
    # revision without transferring ownership.
    revision: int = Field(default=0, index=True)
    attempt_count: int = Field(default=0, index=True)
    max_attempts: int = Field(default=1, index=True)
    failure_reason: Optional[str] = Field(default=None, index=True)
    checkpoint_receipts_json: str = Field(default="[]")
    artifact_receipts_json: str = Field(default="[]")
    effect_receipts_json: str = Field(default="[]")
    # Protected, server-minted GitHub GET authority. Never accepted from a
    # caller mapping or exposed as an execution grant.
    github_read_revision_json: Optional[str] = Field(default=None)
    github_read_observation_history_json: Optional[str] = Field(default=None)
    github_capacity_closure_json: Optional[str] = Field(default=None)
    result_digest: Optional[str] = Field(default=None)
    result_summary: Optional[str] = Field(default=None)


class InferenceAccountingOwner(SQLModel, table=True):
    """Deployment budget metadata owned by DurableJobRepository."""

    __tablename__ = "inference_accounting_owners"
    id: str = Field(default="deployment", primary_key=True)
    deployment_id: str = Field(default_factory=_uuid, unique=True)
    ceiling_microusd: int
    settings_revision: int = Field(default=1)
    settings_history_json: str = Field(default="[]")
    revision: int = Field(default=1)
    ledger_digest: str = Field(default="")
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class InferenceCostReservation(SQLModel, table=True):
    """Accounting evidence for a canonical job, never a second job lifecycle."""

    __tablename__ = "inference_cost_reservations"
    operation_id: str = Field(primary_key=True)
    deployment_id: str = Field(index=True)
    job_id: str = Field(index=True)
    owner_id: str = Field(index=True)
    goal_id: Optional[str] = Field(default=None, index=True)
    goal_revision: Optional[int] = None
    payload_digest: str
    policy_digest: str
    runtime_path: str
    profile_id: str
    period_id: str = Field(index=True)
    settings_revision: int
    ceiling_microusd: int
    bound_microusd: int
    owner_ceiling_microusd: Optional[int] = None
    sequence: int = Field(index=True)
    priority: int
    deadline_at: datetime
    state: str = Field(default="reserved", index=True)
    job_fencing_token: int
    contact_started_at: Optional[datetime] = None
    actual_cost_microusd: Optional[int] = None
    provider_operation_id: Optional[str] = None
    evidence_json: str = Field(default="[]")
    recovery_reason: Optional[str] = None
    revision: int = Field(default=1)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class WorkflowStepState(SQLModel, table=True):
    __tablename__ = "workflow_step_states"

    id: str = Field(default_factory=_uuid, primary_key=True)
    run_identity: str = Field(index=True)
    workflow_name: str = Field(default="", index=True)
    step_id: str = Field(index=True)
    step_index: int = Field(default=0, index=True)
    tool_name: str = Field(default="", index=True)
    status: str = Field(default="running", index=True)
    arguments_json: str = Field(default="{}")
    result_json: Optional[str] = Field(default=None)
    result_summary: Optional[str] = Field(default=None)
    artifact_paths_json: str = Field(default="[]")
    error_kind: Optional[str] = Field(default=None)
    error_summary: Optional[str] = Field(default=None)
    checkpoint_json: Optional[str] = Field(default=None)
    started_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)
    completed_at: Optional[datetime] = Field(default=None, index=True)


class WorkflowArtifactReview(SQLModel, table=True):
    __tablename__ = "workflow_artifact_reviews"

    id: str = Field(default_factory=_uuid, primary_key=True)
    run_identity: str = Field(index=True)
    root_run_identity: str = Field(index=True)
    parent_run_identity: Optional[str] = Field(default=None, index=True)
    workflow_name: str = Field(default="", index=True)
    artifact_path: str = Field(index=True)
    owner: str = Field(default="workflow", index=True)
    review_state: str = Field(default="pending_review", index=True)
    reviewer: Optional[str] = Field(default=None, index=True)
    decision: Optional[str] = Field(default=None)
    approval_id: Optional[str] = Field(default=None, index=True)
    metadata_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)
    decided_at: Optional[datetime] = Field(default=None, index=True)


class ProductionWorkflowAuthorityState(SQLModel, table=True):
    __tablename__ = "production_workflow_authority_states"

    id: str = Field(default_factory=_uuid, primary_key=True)
    run_identity: str = Field(index=True, unique=True)
    workflow_name: str = Field(default="", index=True)
    scheduler_state_owner: str = Field(index=True)
    workflow_lease_id: str = Field(index=True)
    worker_owner: str = Field(index=True)
    lease_revision: int = Field(default=0, index=True)
    workflow_phase: str = Field(default="running", index=True)
    resumable_step_state: str = Field(default="", index=True)
    replay_window: str = Field(default="")
    recovery_authority: str = Field(default="")
    safe_replay_decision: str = Field(default="unsafe", index=True)
    blocked_replay_reason: Optional[str] = Field(default=None, index=True)
    side_effect_status: str = Field(default="not_started", index=True)
    residual_risk: str = Field(default="")
    transition_ledger_json: str = Field(default="[]")
    metadata_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class ProductionWorkflowFaultReceipt(SQLModel, table=True):
    __tablename__ = "production_workflow_fault_receipts"

    id: str = Field(default_factory=_uuid, primary_key=True)
    fault_key: str = Field(index=True, unique=True)
    run_identity: Optional[str] = Field(default=None, index=True)
    injection_method: str = Field(index=True)
    campaign_window: str = Field(default="14d_accelerated_fault_campaign_equivalent", index=True)
    recovery_result: str = Field(index=True)
    replay_decision: str = Field(index=True)
    duplicate_suppressed_count: int = Field(default=0)
    operator_intervention_required: bool = Field(default=False, index=True)
    raw_receipt_handle: str = Field(index=True)
    residual_risk: str = Field(default="")
    metadata_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class ProductionWorkflowSideEffectReceipt(SQLModel, table=True):
    __tablename__ = "production_workflow_side_effect_receipts"

    id: str = Field(default_factory=_uuid, primary_key=True)
    reconciliation_id: str = Field(index=True, unique=True)
    run_identity: Optional[str] = Field(default=None, index=True)
    side_effect_kind: str = Field(index=True)
    idempotency_scope: str = Field(index=True)
    idempotency_key: str = Field(index=True, unique=True)
    external_confirmation_state: str = Field(index=True)
    provider_receipt: str = Field(default="")
    duplicate_suppression_receipt: str = Field(default="")
    reconciliation_outcome: str = Field(index=True)
    manual_repair_state: str = Field(default="", index=True)
    operator_replay_decision: str = Field(default="unsafe_retry_blocked", index=True)
    redacted_receipt_handle: str = Field(default="", index=True)
    metadata_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


# ─── Memory ──────────────────────────────────────────────

class Memory(SQLModel, table=True):
    __tablename__ = "memories"
    __table_args__ = (
        Index(
            "ix_memories_kind_scope_key_unique",
            "kind",
            "scope_key",
            unique=True,
            sqlite_where=text("scope_key IS NOT NULL"),
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    content: str
    category: MemoryCategory = Field(default=MemoryCategory.fact)
    kind: MemoryKind = Field(default=MemoryKind.fact, index=True)
    summary: Optional[str] = Field(default=None)
    confidence: float = Field(default=0.5)
    importance: float = Field(default=0.5)
    reinforcement: float = Field(default=1.0)
    status: MemoryStatus = Field(default=MemoryStatus.active, index=True)
    subject_entity_id: Optional[str] = Field(default=None, foreign_key="memory_entities.id", index=True)
    project_entity_id: Optional[str] = Field(default=None, foreign_key="memory_entities.id", index=True)
    source_session_id: Optional[str] = Field(default=None)
    embedding_id: Optional[str] = Field(default=None)
    scope_key: Optional[str] = Field(default=None, index=True)
    metadata_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    last_confirmed_at: Optional[datetime] = Field(default=None)


class MemoryTombstone(SQLModel, table=True):
    """Durable local deletion authority for a canonical memory row.

    This ledger intentionally stores no memory content.  It survives a stale
    row restore and lets the canonical repository re-apply redaction before a
    deterministic read or reindex path exposes the row again.
    """

    __tablename__ = "memory_tombstones"

    id: str = Field(default_factory=_uuid, primary_key=True)
    memory_id: str = Field(foreign_key="memories.id", index=True, unique=True)
    actor: str = Field(default="operator", index=True)
    reason: str = Field(default="operator_delete_export")
    created_at: datetime = Field(default_factory=_now, index=True)


class MemoryEntity(SQLModel, table=True):
    __tablename__ = "memory_entities"

    id: str = Field(default_factory=_uuid, primary_key=True)
    canonical_key: str = Field(index=True, unique=True)
    canonical_name: str = Field(index=True)
    entity_type: MemoryEntityType = Field(default=MemoryEntityType.person, index=True)
    aliases_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class MemorySource(SQLModel, table=True):
    __tablename__ = "memory_sources"

    id: str = Field(default_factory=_uuid, primary_key=True)
    memory_id: str = Field(foreign_key="memories.id", index=True)
    source_type: str = Field(default="session", index=True)
    source_session_id: Optional[str] = Field(default=None, index=True)
    source_message_id: Optional[str] = Field(default=None, index=True)
    snippet: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class MemoryProposal(SQLModel, table=True):
    """Owner/session fenced review proposal from one verified board attempt."""

    __tablename__ = "memory_proposals"
    __table_args__ = (
        Index(
            "ux_memory_proposals_owner_attempt_preview",
            "owner_principal_id",
            "owner_session_id",
            "source_task_id",
            "source_attempt_id",
            "preview_text_digest",
            unique=True,
            # A blocked/expired proposal is an immutable historical review
            # projection.  Recovery creates a distinct proposal generation
            # with the same verified preview, so terminal recovery rows must
            # not consume the active-generation uniqueness slot.
            sqlite_where=text(
                "preview_text_digest IS NOT NULL "
                "AND status NOT IN ('blocked', 'expired')"
            ),
        ),
        Index(
            "ux_memory_proposals_owner_attempt_no_learning",
            "owner_principal_id",
            "owner_session_id",
            "source_task_id",
            "source_attempt_id",
            unique=True,
            sqlite_where=text("status = 'no_learning'"),
        ),
        Index(
            "ix_memory_proposals_exact_comparison",
            "owner_principal_id",
            "owner_session_id",
            "goal_id",
            "goal_revision",
            "capability_id",
            "capability_version",
            "typed_input_digest",
            "source_context_digest",
            "status",
            "proposal_id",
        ),
    )

    proposal_id: str = Field(default_factory=_uuid, primary_key=True)
    schema_version: str = Field(default="memory_proposal.v1", index=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    source_task_id: str = Field(index=True)
    source_task_revision: int = Field(default=1, index=True)
    source_attempt_id: str = Field(index=True)
    source_attempt_fence: int = Field(default=0, index=True)
    workflow_run_id: str = Field(default="", index=True)
    workflow_run_revision: int = Field(default=0, index=True)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(default=1, index=True)
    capability_id: str = Field(index=True)
    capability_version: str = Field(default="", index=True)
    typed_input_digest: str = Field(default="", index=True)
    source_context_digest: str = Field(default="", index=True)
    candidate_set_digest: str = Field(default="", index=True)
    evidence_digest: Optional[str] = Field(default=None, index=True)
    readback_kind: str = Field(default="")
    readback_ref: Optional[str] = Field(default=None)
    readback_digest: Optional[str] = Field(default=None, index=True)
    artifact_ref: Optional[str] = Field(default=None, index=True)
    artifact_digest: Optional[str] = Field(default=None, index=True)
    proposal_job_id: Optional[str] = Field(default=None, index=True)
    request_idempotency_key: str = Field(default="", index=True)
    request_binding_digest: str = Field(default="", index=True)
    acceptance_binding_digest: Optional[str] = Field(default=None)
    memory_kind: Optional[MemoryKind] = Field(default=None, index=True)
    memory_scope_json: Optional[str] = Field(default=None)
    preview_text: Optional[str] = Field(default=None)
    preview_text_digest: Optional[str] = Field(default=None, index=True)
    decision_effect: MemoryProposalDecisionEffect = Field(
        default=MemoryProposalDecisionEffect.none,
        index=True,
    )
    confidence: Optional[float] = Field(default=None)
    corrects_memory_id: Optional[str] = Field(default=None, index=True)
    recovered_from_proposal_id: Optional[str] = Field(default=None, index=True)
    provenance_json: str = Field(default="{}")
    source_refs_json: str = Field(default="[]")
    reason_code: str = Field(default="pending", index=True)
    recovery_action: str = Field(default="none", index=True)
    provider_contact_started: bool = Field(default=False, index=True)
    provider_contact_state: MemoryProposalProviderContactState = Field(
        default=MemoryProposalProviderContactState.not_started,
        index=True,
    )
    provider_contact_count: int = Field(default=0, index=True)
    privacy_state: MemoryProposalPrivacyState = Field(
        default=MemoryProposalPrivacyState.visible,
        index=True,
    )
    status: MemoryProposalStatus = Field(
        default=MemoryProposalStatus.pending_inference,
        index=True,
    )
    accepted_memory_id: Optional[str] = Field(default=None, index=True)
    accepted_memory_content_digest: Optional[str] = Field(default=None, index=True)
    accepted_by_principal_id: Optional[str] = Field(default=None, index=True)
    accepted_by_session_id: Optional[str] = Field(default=None, index=True)
    accepted_at: Optional[datetime] = Field(default=None, index=True)
    rejected_by_principal_id: Optional[str] = Field(default=None, index=True)
    rejected_by_session_id: Optional[str] = Field(default=None, index=True)
    rejected_at: Optional[datetime] = Field(default=None, index=True)
    rollback_by_principal_id: Optional[str] = Field(default=None, index=True)
    rollback_by_session_id: Optional[str] = Field(default=None, index=True)
    rollback_at: Optional[datetime] = Field(default=None, index=True)
    rollback_reason: str = Field(default="", max_length=500)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)
    expires_at: Optional[datetime] = Field(default=None, index=True)
    revision: int = Field(default=1, index=True)


class WorkBoardDecisionReceipt(SQLModel, table=True):
    """Canonical before/after decision and confirmation receipt for M5."""

    __tablename__ = "work_board_decision_receipts"
    __table_args__ = (
        Index(
            "ux_work_board_decision_receipts_binding",
            "receipt_binding_digest",
            unique=True,
        ),
        Index(
            "ix_work_board_decision_receipts_exact_intent",
            "owner_principal_id",
            "owner_session_id",
            "later_task_id",
            "later_task_revision",
            "task_intent_digest",
            "goal_id",
            "goal_revision",
            "capability_id",
            "capability_version",
            "typed_input_digest",
            "source_context_digest",
            "receipt_id",
        ),
    )

    receipt_id: str = Field(default_factory=_uuid, primary_key=True)
    schema_version: str = Field(default="work_board_decision_receipt.v1", index=True)
    receipt_stage: WorkBoardDecisionReceiptStage = Field(index=True)
    receipt_binding_digest: str = Field(default="", index=True)
    receipt_integrity_mac: Optional[str] = Field(default=None)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    source_proposal_id: Optional[str] = Field(default=None, index=True)
    source_proposal_revision: int = Field(default=0, index=True)
    source_baseline_receipt_id: Optional[str] = Field(default=None, index=True)
    source_task_id: Optional[str] = Field(default=None, index=True)
    source_task_revision: int = Field(default=0, index=True)
    source_attempt_id: Optional[str] = Field(default=None, index=True)
    source_attempt_fence: int = Field(default=0, index=True)
    source_workflow_run_id: Optional[str] = Field(default=None, index=True)
    source_workflow_run_revision: int = Field(default=0, index=True)
    later_task_id: str = Field(index=True)
    later_task_revision: int = Field(default=1, index=True)
    later_attempt_id: Optional[str] = Field(default=None, index=True)
    later_workflow_run_id: Optional[str] = Field(default=None, index=True)
    later_attempt_fence: int = Field(default=0, index=True)
    goal_id: str = Field(index=True)
    goal_revision: int = Field(default=1, index=True)
    capability_id: str = Field(index=True)
    capability_version: str = Field(default="", index=True)
    typed_input_digest: str = Field(default="", index=True)
    task_intent_digest: str = Field(default="", index=True)
    source_context_digest: str = Field(default="", index=True)
    candidate_set_digest: str = Field(default="")
    accepted_memory_id: Optional[str] = Field(default=None, index=True)
    accepted_memory_content_digest: Optional[str] = Field(default=None, index=True)
    before_input_digest: str = Field(default="", index=True)
    after_input_digest: str = Field(default="", index=True)
    before_action_id: str = Field(default="")
    after_action_id: str = Field(default="")
    before_selected_capability_id: Optional[str] = Field(default=None, index=True)
    after_selected_capability_id: Optional[str] = Field(default=None, index=True)
    confirmed_action_id: Optional[str] = Field(default=None)
    comparison_context_digest: str = Field(default="", index=True)
    retrieval_evidence_ids_json: str = Field(default="[]")
    decision_status: WorkBoardDecisionStatus = Field(index=True)
    admission_status: WorkBoardDecisionAdmissionStatus = Field(index=True)
    reason: str = Field(default="", max_length=1000)
    confirmer_principal_id: Optional[str] = Field(default=None, index=True)
    confirmer_session_id: Optional[str] = Field(default=None, index=True)
    confirmed_at: Optional[datetime] = Field(default=None, index=True)
    confirmation_binding_digest: Optional[str] = Field(default=None, index=True)
    consumed_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)
    revision: int = Field(default=1, index=True)


class MemorySnapshot(SQLModel, table=True):
    __tablename__ = "memory_snapshots"

    id: str = Field(default_factory=_uuid, primary_key=True)
    kind: MemorySnapshotKind = Field(default=MemorySnapshotKind.bounded_guardian_context, index=True, unique=True)
    content: str = Field(default="")
    source_hash: Optional[str] = Field(default=None)
    canonical_tombstone_revision: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class MemoryEdge(SQLModel, table=True):
    __tablename__ = "memory_edges"

    id: str = Field(default_factory=_uuid, primary_key=True)
    from_memory_id: str = Field(foreign_key="memories.id", index=True)
    to_memory_id: str = Field(foreign_key="memories.id", index=True)
    edge_type: MemoryEdgeType = Field(default=MemoryEdgeType.related, index=True)
    weight: float = Field(default=1.0)
    metadata_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class MemoryEpisode(SQLModel, table=True):
    __tablename__ = "memory_episodes"

    id: str = Field(default_factory=_uuid, primary_key=True)
    session_id: Optional[str] = Field(default=None, foreign_key="sessions.id", index=True)
    episode_type: MemoryEpisodeType = Field(default=MemoryEpisodeType.conversation, index=True)
    summary: str = Field(default="")
    content: str = Field(default="")
    source_message_id: Optional[str] = Field(default=None, foreign_key="messages.id", index=True)
    source_tool_name: Optional[str] = Field(default=None, index=True)
    source_role: Optional[str] = Field(default=None, index=True)
    subject_entity_id: Optional[str] = Field(default=None, foreign_key="memory_entities.id", index=True)
    project_entity_id: Optional[str] = Field(default=None, foreign_key="memory_entities.id", index=True)
    salience: float = Field(default=0.5)
    confidence: float = Field(default=0.5)
    metadata_json: Optional[str] = Field(default=None)
    observed_at: datetime = Field(default_factory=_now, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)


# ─── Goal ────────────────────────────────────────────────

class Goal(SQLModel, table=True):
    __tablename__ = "goals"

    id: str = Field(default_factory=_uuid, primary_key=True)
    parent_id: Optional[str] = Field(default=None, foreign_key="goals.id", index=True)
    path: str = Field(default="/")  # Materialized path e.g. /v1/a3/q7/
    level: str = Field(default=GoalLevel.daily)
    title: str
    description: Optional[str] = Field(default=None)
    status: str = Field(default=GoalStatus.active, index=True)
    domain: str = Field(default=GoalDomain.productivity, index=True)
    start_date: Optional[datetime] = Field(default=None)
    due_date: Optional[datetime] = Field(default=None)
    sort_order: int = Field(default=0)
    # Additive v1 goal-conditioned planning fields.  Legacy goals remain
    # readable and proposal-only until an operator supplies a criterion.
    revision: int = Field(default=1, index=True)
    success_criterion_json: Optional[str] = Field(default=None)
    # Autonomous goal work is opt-in and remains disabled for legacy goals.
    # The authenticated goals API records the operator grant; the scheduler
    # must never infer permission from an active status or criterion alone.
    proactive_enabled: bool = Field(default=False, index=True)
    # Nullable so existing local databases and manually-created legacy goals
    # remain readable. Public loop routes require both bindings; scheduler
    # service runs use their own explicit service authority.
    owner_principal_id: Optional[str] = Field(default=None, index=True)
    owner_session_id: Optional[str] = Field(default=None, index=True)
    admission_budget_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class StrategyDelta(SQLModel, table=True):
    """Durable, reversible goal-scoped planning correction."""

    __tablename__ = "strategy_deltas"
    __table_args__ = (
        Index(
            "ux_strategy_deltas_source_event_id",
            "source_event_id",
            unique=True,
        ),
    )

    delta_id: str = Field(default_factory=_uuid, primary_key=True)
    goal_id: str = Field(index=True)
    scope: str = Field(default="goal", index=True)
    field_name: str = Field(default="web_brief_target", index=True)
    before_json: str = Field(default="{}")
    after_json: str = Field(default="{}")
    source_event_id: str = Field(index=True)
    author_id: str = Field(default="")
    evaluator_id: Optional[str] = Field(default=None)
    goal_revision_before: int = Field(default=1, index=True)
    goal_revision_after: Optional[int] = Field(default=None, index=True)
    status: str = Field(default="proposed", index=True)
    rollback_target_id: Optional[str] = Field(default=None, index=True)
    reason: str = Field(default="")
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now)


# ─── UserProfile ─────────────────────────────────────────

class UserProfile(SQLModel, table=True):
    __tablename__ = "user_profiles"

    id: str = Field(default="singleton", primary_key=True)
    name: str = Field(default="Unknown")
    soul_text: Optional[str] = Field(default=None)
    preferences_json: Optional[str] = Field(default=None)
    onboarding_completed: bool = Field(default=False)
    interruption_mode: str = Field(default="balanced")
    capture_mode: str = Field(default="on_switch")  # on_switch | balanced | detailed
    tool_policy_mode: str = Field(default="full")  # safe | balanced | full
    mcp_policy_mode: str = Field(default="full")  # disabled | approval | full
    approval_mode: str = Field(default="high_risk")  # off | high_risk
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ─── QueuedInsight ──────────────────────────────────────

class QueuedInsight(SQLModel, table=True):
    __tablename__ = "queued_insights"

    id: str = Field(default_factory=_uuid, primary_key=True)
    intervention_id: Optional[str] = Field(default=None, index=True)
    session_id: Optional[str] = Field(default=None, foreign_key="sessions.id", index=True)
    owner_principal_id: Optional[str] = Field(default=None, index=True)
    operator_session_id: Optional[str] = Field(default=None, index=True)
    goal_id: Optional[str] = Field(default=None, index=True)
    # Revision fence for goal-bound deferred delivery.  A queued insight is
    # only valid for the canonical goal revision that produced it.
    goal_revision: Optional[int] = Field(default=None, index=True)
    budget_period_key: Optional[str] = Field(default=None, index=True)
    budget_limit: Optional[int] = Field(default=None, index=True)
    content: str
    intervention_type: str = Field(default="advisory")
    urgency: int = Field(default=3)
    reasoning: str = Field(default="")
    created_at: datetime = Field(default_factory=_now)


# ─── GuardianIntervention ──────────────────────────────

class GuardianIntervention(SQLModel, table=True):
    __tablename__ = "guardian_interventions"

    id: str = Field(default_factory=_uuid, primary_key=True)
    session_id: Optional[str] = Field(default=None, foreign_key="sessions.id", index=True)
    message_type: str = Field(default="proactive", index=True)
    intervention_type: str = Field(default="advisory", index=True)
    urgency: int = Field(default=3, index=True)
    content_excerpt: str = Field(default="")
    reasoning: Optional[str] = Field(default=None)
    is_scheduled: bool = Field(default=False, index=True)
    guardian_confidence: Optional[str] = Field(default=None, index=True)
    data_quality: Optional[str] = Field(default=None, index=True)
    user_state: Optional[str] = Field(default=None, index=True)
    active_project: Optional[str] = Field(default=None, index=True)
    interruption_mode: Optional[str] = Field(default=None, index=True)
    policy_action: str = Field(default="act", index=True)
    policy_reason: str = Field(default="")
    delivery_decision: Optional[str] = Field(default=None, index=True)
    transport: Optional[str] = Field(default=None, index=True)
    latest_outcome: str = Field(default="created", index=True)
    notification_id: Optional[str] = Field(default=None, index=True)
    feedback_type: Optional[str] = Field(default=None, index=True)
    feedback_note: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)
    feedback_at: Optional[datetime] = Field(default=None, index=True)


# ─── Native notification outbox ────────────────────────

class NativeNotificationOutbox(SQLModel, table=True):
    """Durable, bounded state for the built-in native notification path.

    This row is a delivery intent and receipt, not proof that an external
    desktop notification was displayed. ``unknown`` is retained whenever a
    daemon handoff is ambiguous because it may have displayed the notification
    before Seraph received a receipt.
    """

    __tablename__ = "native_notification_outbox"
    __table_args__ = (
        Index(
            "ix_native_notification_outbox_pending_order",
            "status",
            "created_at",
            "urgency",
        ),
        Index(
            "ix_native_notification_outbox_lease",
            "status",
            "lease_expires_at",
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    idempotency_key: str = Field(unique=True, index=True)
    payload_digest: str = Field(index=True)
    intervention_id: Optional[str] = Field(default=None, index=True)
    owner_principal_id: Optional[str] = Field(default=None, index=True)
    # Optional standing-goal budget reservation binding. Legacy/manual
    # notifications leave these fields null and retain their existing queue
    # semantics.
    goal_id: Optional[str] = Field(default=None, index=True)
    # Revision fence for goal-bound effects.  A notification may only be
    # recovered while its canonical goal still has this revision.
    goal_revision: Optional[int] = Field(default=None, index=True)
    budget_period_key: Optional[str] = Field(default=None, index=True)
    budget_limit: Optional[int] = Field(default=None, index=True)
    operator_session_id: Optional[str] = Field(default=None, index=True)
    device_id: Optional[str] = Field(default=None, index=True)
    channel: str = Field(default="native_notification", index=True)
    transport: str = Field(default="native_notification", index=True)
    title: str
    body: str
    intervention_type: Optional[str] = Field(default=None, index=True)
    urgency: Optional[int] = Field(default=None, index=True)
    surface: str = Field(default="notification", index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    conversation_id: Optional[str] = Field(default=None, index=True)
    thread_id: Optional[str] = Field(default=None, index=True)
    thread_source: str = Field(default="ambient", index=True)
    continuation_mode: str = Field(default="open_thread", index=True)
    resume_message: Optional[str] = Field(default=None)
    correlation_id: Optional[str] = Field(default=None, index=True)
    causation_id: Optional[str] = Field(default=None, index=True)
    attachment_refs_json: str = Field(default="[]")
    degraded_state: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="queued", index=True)
    attempt_count: int = Field(default=0, index=True)
    max_attempts: int = Field(default=3, index=True)
    deadline_at: datetime = Field(index=True)
    lease_owner: Optional[str] = Field(default=None, index=True)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    fencing_token: int = Field(default=0, index=True)
    last_error: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)
    delivered_at: Optional[datetime] = Field(default=None, index=True)
    cancelled_at: Optional[datetime] = Field(default=None, index=True)


class NativeNotificationDeliveryAttempt(SQLModel, table=True):
    """One claimed native notification attempt with a fenced receipt."""

    __tablename__ = "native_notification_delivery_attempts"
    __table_args__ = (
        Index(
            "ux_native_notification_delivery_attempt_order",
            "notification_id",
            "attempt_index",
            unique=True,
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    notification_id: str = Field(
        foreign_key="native_notification_outbox.id",
        index=True,
    )
    attempt_index: int = Field(index=True)
    lease_owner: str = Field(index=True)
    fencing_token: int = Field(index=True)
    status: str = Field(default="claimed", index=True)
    error_code: Optional[str] = Field(default=None, index=True)
    started_at: datetime = Field(default_factory=_now, index=True)
    finished_at: Optional[datetime] = Field(default=None, index=True)


# ─── Telegram transport ─────────────────────────────────

class TelegramTransportState(SQLModel, table=True):
    """Durable, server-owned state for the provider-free Telegram adapter.

    The token itself lives in the encrypted vault.  This projection only
    stores its fingerprint and the pairing/consent/cursor facts needed to
    reject stale or cross-operator updates after a restart.
    """

    __tablename__ = "telegram_transport_states"

    id: str = Field(default="telegram", primary_key=True)
    owner_principal_id: Optional[str] = Field(default=None, index=True)
    operator_session_id: Optional[str] = Field(default=None, index=True)
    operator_id: Optional[int] = Field(default=None, index=True)
    chat_id: Optional[int] = Field(default=None, index=True)
    pairing_id: Optional[str] = Field(default=None, index=True)
    pairing_state: str = Field(default="unpaired", index=True)
    pairing_expires_at: Optional[datetime] = Field(default=None, index=True)
    token_secret_ref: Optional[str] = Field(default=None, index=True)
    token_fingerprint: Optional[str] = Field(default=None, index=True)
    transit_consent_reference: Optional[str] = Field(default=None, index=True)
    transit_consent_expires_at: Optional[datetime] = Field(default=None, index=True)
    model_consent_reference: Optional[str] = Field(default=None, index=True)
    model_consent_expires_at: Optional[datetime] = Field(default=None, index=True)
    cursor: int = Field(default=0, index=True)
    sequence: int = Field(default=0, index=True)
    rate_events_json: str = Field(default="[]")
    revoked_at: Optional[datetime] = Field(default=None, index=True)
    last_update_at: Optional[datetime] = Field(default=None, index=True)
    last_error: Optional[str] = Field(default=None, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)


class TelegramInboundUpdate(SQLModel, table=True):
    """Replay ledger and bounded ingress receipt for one Telegram update."""

    __tablename__ = "telegram_inbound_updates"

    id: str = Field(default_factory=_uuid, primary_key=True)
    idempotency_key: str = Field(unique=True, index=True)
    request_digest: str = Field(index=True)
    owner_principal_id: str = Field(index=True)
    operator_session_id: str = Field(index=True)
    operator_id: int = Field(index=True)
    chat_id: int = Field(index=True)
    update_id: int = Field(index=True)
    message_id: int = Field(index=True)
    sequence: int = Field(index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    canonical_message_id: Optional[str] = Field(default=None, index=True)
    content_digest: Optional[str] = Field(default=None, index=True)
    attachment_id: Optional[str] = Field(default=None, index=True)
    attachment_hash: Optional[str] = Field(default=None, index=True)
    attachment_media_type: Optional[str] = Field(default=None)
    attachment_size_bytes: Optional[int] = Field(default=None)
    attachment_duration_seconds: Optional[float] = Field(default=None)
    attachment_quarantine_receipt_digest: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="accepted", index=True)
    reason_code: str = Field(default="", index=True)
    receipt_json: str = Field(default="{}")
    created_at: datetime = Field(default_factory=_now, index=True)


class TelegramTransportOutbox(SQLModel, table=True):
    """Canonical Telegram delivery intent with bounded retry state."""

    __tablename__ = "telegram_transport_outbox"

    id: str = Field(default_factory=_uuid, primary_key=True)
    idempotency_key: str = Field(unique=True, index=True)
    payload_digest: str = Field(index=True)
    owner_principal_id: str = Field(index=True)
    operator_session_id: str = Field(index=True)
    chat_id: int = Field(index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    conversation_id: Optional[str] = Field(default=None, index=True)
    thread_id: Optional[str] = Field(default=None, index=True)
    message_id: Optional[str] = Field(default=None, index=True)
    correlation_id: Optional[str] = Field(default=None, index=True)
    content: str = Field(default="")
    content_digest: str = Field(index=True)
    kind: str = Field(default="text", index=True)
    attachment_refs_json: str = Field(default="[]")
    # Private control markup: bearer callbacks never appear in status/audit.
    task_control_markup_json: Optional[str] = Field(default=None)
    task_control_markup_digest: Optional[str] = Field(default=None)
    status: str = Field(default="queued", index=True)
    attempt_count: int = Field(default=0, index=True)
    max_attempts: int = Field(default=3, index=True)
    next_attempt_at: datetime = Field(default_factory=_now, index=True)
    # Delivery claims are durable so two adapter processes cannot both treat
    # the same row as theirs after a restart.  A lease expiry makes an
    # interrupted call recoverable, while the monotonically increasing fence
    # prevents a late callback from overwriting a newer attempt.
    lease_owner: Optional[str] = Field(default=None, index=True)
    lease_expires_at: Optional[datetime] = Field(default=None, index=True)
    fencing_token: int = Field(default=0, index=True)
    deadline_at: Optional[datetime] = Field(default=None, index=True)
    last_error: Optional[str] = Field(default=None, index=True)
    response_code: Optional[int] = Field(default=None, index=True)
    external_message_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now, index=True)
    delivered_at: Optional[datetime] = Field(default=None, index=True)
    cancelled_at: Optional[datetime] = Field(default=None, index=True)


class TelegramDeliveryAttempt(SQLModel, table=True):
    """Durable bounded delivery attempt receipt for injected transport calls."""

    __tablename__ = "telegram_delivery_attempts"
    __table_args__ = (
        Index(
            "ux_telegram_delivery_attempt_order",
            "outbox_id",
            "attempt_index",
            unique=True,
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    outbox_id: str = Field(index=True)
    attempt_index: int = Field(index=True)
    lease_owner: Optional[str] = Field(default=None, index=True)
    fencing_token: int = Field(default=0, index=True)
    status: str = Field(default="started", index=True)
    response_code: Optional[int] = Field(default=None, index=True)
    error_code: Optional[str] = Field(default=None, index=True)
    started_at: datetime = Field(default_factory=_now, index=True)
    finished_at: Optional[datetime] = Field(default=None, index=True)


class TelegramTaskCallback(SQLModel, table=True):
    """One finite, exact, paired task control; wire nonces remain in outbox only."""

    __tablename__ = "telegram_task_callbacks"
    id: str = Field(default_factory=_uuid, primary_key=True)
    nonce_digest: str = Field(unique=True, index=True)
    owner_principal_id: str = Field(index=True)
    operator_session_id: str = Field(index=True)
    pairing_id: str = Field(index=True)
    transit_reference: str
    actor_id: int
    chat_id: int
    root_digest: str
    task_id: str = Field(index=True)
    task_revision: int
    goal_id: str
    goal_revision: int
    outbox_id: str = Field(index=True)
    effect: str
    effect_digest: str
    approval_id: Optional[str] = Field(default=None)
    approval_digest: Optional[str] = Field(default=None)
    attempt_id: Optional[str] = Field(default=None)
    workflow_run_id: Optional[str] = Field(default=None)
    workflow_binding_digest: Optional[str] = Field(default=None)
    board_fence: Optional[int] = Field(default=None)
    lease_owner: Optional[str] = Field(default=None)
    cancel_event_id: Optional[int] = Field(default=None)
    status: str = Field(default="pending", index=True)
    query_id: Optional[str] = Field(default=None, index=True)
    update_id: Optional[int] = Field(default=None)
    request_digest: Optional[str] = Field(default=None)
    result_json: str = Field(default="{}")
    expires_at: datetime = Field(index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    consumed_at: Optional[datetime] = Field(default=None, index=True)


# ─── ScreenObservation ─────────────────────────────────

class ScreenObservation(SQLModel, table=True):
    __tablename__ = "screen_observations"

    id: str = Field(default_factory=_uuid, primary_key=True)
    timestamp: datetime = Field(default_factory=_now, index=True)
    app_name: str = Field(index=True)
    window_title: str = Field(default="")
    activity_type: str = Field(default="other", index=True)
    project: Optional[str] = Field(default=None, index=True)
    summary: Optional[str] = Field(default=None)
    details_json: Optional[str] = Field(default=None)
    duration_s: Optional[int] = Field(default=None)
    blocked: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_now)


# ─── Paired edge artifacts ───────────────────────────────

class PairedEdgeArtifact(SQLModel, table=True):
    """Server-owned bytes accepted from a paired observation edge.

    The edge may report a source path for diagnostics, but that path is never
    persisted here and cannot participate in the canonical artifact ID.  A
    request ID is the idempotency key for the authenticated pairing transport.
    """

    __tablename__ = "paired_edge_artifacts"
    __table_args__ = (
        Index(
            "ux_paired_edge_artifacts_pairing_request",
            "extension_id",
            "reference",
            "pairing_id",
            "request_id",
            unique=True,
        ),
        Index("ix_paired_edge_artifacts_owner_created", "owner_principal_id", "created_at"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    artifact_id: str = Field(unique=True, index=True)
    extension_id: str = Field(index=True)
    reference: str = Field(index=True)
    device_id: str = Field(index=True)
    pairing_id: str = Field(index=True)
    request_id: str = Field(index=True)
    owner_principal_id: str = Field(index=True)
    sequence: int = Field(index=True)
    captured_at: datetime = Field(index=True)
    content_hash: str = Field(index=True)
    media_type: str
    content_size: int
    content: bytes
    app_name: str = Field(default="")
    window_title: str = Field(default="")
    observation_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)


# ─── Secret (Vault) ─────────────────────────────────────

class Secret(SQLModel, table=True):
    __tablename__ = "secrets"

    # Null is a legacy/system secret; never attributable to a browser login.
    owner_principal_id: Optional[str] = Field(default=None, index=True)
    revoked_at: Optional[datetime] = Field(default=None, index=True)

    id: str = Field(default_factory=_uuid, primary_key=True)
    key: str = Field(unique=True, index=True)
    encrypted_value: str
    description: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# ─── AuditEvent ─────────────────────────────────────────

class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_events"

    id: str = Field(default_factory=_uuid, primary_key=True)
    session_id: Optional[str] = Field(default=None, foreign_key="sessions.id", index=True)
    actor: str = Field(default="agent", index=True)
    event_type: str = Field(default="tool_call", index=True)
    tool_name: Optional[str] = Field(default=None, index=True)
    risk_level: str = Field(default="low", index=True)
    policy_mode: str = Field(default="full", index=True)
    summary: str = Field(default="")
    details_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)


# ─── Model Fabric Proofs And Receipts ─────────────────────────

class ModelCapabilityProofRecord(SQLModel, table=True):
    """Sanitized empirical proof for one exact model capability binding."""

    __tablename__ = "model_capability_proofs"
    __table_args__ = (
        Index("ix_model_capability_proofs_binding", "profile_id", "model", "adapter", "capability"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    proof_hash: str = Field(unique=True, index=True)
    profile_schema_version: str = Field(index=True)
    profile_contract_hash: str = Field(index=True)
    profile_id: str = Field(index=True)
    model: str = Field(index=True)
    endpoint: str
    endpoint_digest: str = Field(index=True)
    endpoint_class: str = Field(index=True)
    adapter: str = Field(index=True)
    capability: str = Field(index=True)
    canary_version: str
    outcome: str = Field(index=True)
    checked_at: float = Field(index=True)
    expires_at: float = Field(index=True)
    proven_value_json: Optional[str] = Field(default=None)
    receipt_id: str = Field(index=True)
    receipt_hash: str = Field(index=True)
    created_at: datetime = Field(default_factory=_now, index=True)


class ModelRouteReceiptRecord(SQLModel, table=True):
    """Sanitized final inference-route receipt."""

    __tablename__ = "model_route_receipts"
    __table_args__ = (
        Index("ix_model_route_receipts_workload_success", "workload", "outcome", "finished_at"),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    receipt_id: str = Field(unique=True, index=True)
    receipt_hash: str = Field(unique=True, index=True)
    request_id: str = Field(index=True)
    route_decision_id: str = Field(index=True)
    runtime_path: str = Field(index=True)
    workload: str = Field(index=True)
    outcome: str = Field(index=True)
    actual_profile_id: Optional[str] = Field(default=None, index=True)
    actual_model: Optional[str] = Field(default=None)
    actual_adapter: Optional[str] = Field(default=None)
    destination_class: Optional[str] = Field(default=None)
    egress_class: str = Field(index=True)
    trust_decision_id: Optional[str] = Field(default=None)
    fallback_used: bool = Field(default=False)
    fallback_reason_code: Optional[str] = Field(default=None)
    degradation_codes_json: str = Field(default="[]")
    cost_kind: str = Field(default="unknown")
    cost_amount: Optional[float] = Field(default=None)
    cost_currency: Optional[str] = Field(default=None)
    cost_source: Optional[str] = Field(default=None)
    cost_source_updated_at: Optional[datetime] = Field(default=None)
    usage_input_tokens: Optional[int] = Field(default=None)
    usage_output_tokens: Optional[int] = Field(default=None)
    usage_total_tokens: Optional[int] = Field(default=None)
    started_at: datetime = Field(index=True)
    finished_at: datetime = Field(index=True)
    latency_ms: int
    created_at: datetime = Field(default_factory=_now, index=True)


class ModelRouteAttemptReceiptRecord(SQLModel, table=True):
    """Sanitized receipt for one model transport attempt."""

    __tablename__ = "model_route_attempt_receipts"
    __table_args__ = (
        Index("ux_model_route_attempt_receipts_order", "route_receipt_id", "attempt_index", unique=True),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    route_receipt_id: str = Field(foreign_key="model_route_receipts.receipt_id", index=True)
    attempt_id: str = Field(unique=True, index=True)
    attempt_index: int
    profile_id: str = Field(index=True)
    model: str
    endpoint: str
    endpoint_digest: str = Field(index=True)
    adapter: str
    destination_class: str
    egress_class: str
    trust_decision_id: str
    capability_proof_hashes_json: str = Field(default="[]")
    outcome: str = Field(index=True)
    error_code: Optional[str] = Field(default=None)
    degradation_code: Optional[str] = Field(default=None)
    usage_input_tokens: Optional[int] = Field(default=None)
    usage_output_tokens: Optional[int] = Field(default=None)
    usage_total_tokens: Optional[int] = Field(default=None)
    cost_kind: str = Field(default="unknown")
    cost_amount: Optional[float] = Field(default=None)
    cost_currency: Optional[str] = Field(default=None)
    cost_source: Optional[str] = Field(default=None)
    cost_source_updated_at: Optional[datetime] = Field(default=None)
    started_at: datetime
    finished_at: datetime
    latency_ms: int
    created_at: datetime = Field(default_factory=_now, index=True)


# ─── ApprovalRequest ────────────────────────────────────

class ApprovalRequest(SQLModel, table=True):
    __tablename__ = "approval_requests"

    id: str = Field(default_factory=_uuid, primary_key=True)
    session_id: Optional[str] = Field(default=None, foreign_key="sessions.id", index=True)
    conversation_id: Optional[str] = Field(default=None, index=True)
    thread_id: Optional[str] = Field(default=None, index=True)
    owner_principal_id: Optional[str] = Field(default=None, index=True)
    operator_session_id: Optional[str] = Field(default=None, index=True)
    device_id: Optional[str] = Field(default=None, index=True)
    channel: str = Field(default="web", index=True)
    transport: str = Field(default="rest", index=True)
    correlation_id: Optional[str] = Field(default=None, index=True)
    causation_id: Optional[str] = Field(default=None, index=True)
    attachment_refs_json: str = Field(default="[]")
    challenge: Optional[str] = Field(default=None)
    action: Optional[str] = Field(default=None, index=True)
    expires_at: Optional[datetime] = Field(default=None, index=True)
    tool_name: str = Field(index=True)
    risk_level: str = Field(default="high", index=True)
    status: str = Field(default="pending", index=True)  # pending | approved | denied | consumed
    fingerprint: str = Field(index=True)
    summary: str = Field(default="")
    details_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, index=True)
    resolved_at: Optional[datetime] = Field(default=None)


class OperatorSession(SQLModel, table=True):
    """Revocable single-operator browser session; raw bearer tokens never persist."""

    __tablename__ = "operator_sessions"
    __table_args__ = (
        Index(
            "ix_operator_sessions_replacement_state",
            "replaced_by_id",
            "is_bearer_tombstone",
            "revoked_at",
        ),
    )

    id: str = Field(default_factory=_uuid, primary_key=True)
    token_hash: str = Field(unique=True, index=True)
    principal_id: str = Field(default_factory=lambda: "operator:root:" + _uuid(), unique=True, index=True)
    legacy_owner_principal_id: Optional[str] = Field(default=None)
    # Data continuity only. Never an authentication or execution-session alias.
    operator_identity_id: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now, index=True)
    last_seen_at: datetime = Field(default_factory=_now, index=True)
    idle_expires_at: datetime = Field(index=True)
    absolute_expires_at: datetime = Field(index=True)
    revoked_at: Optional[datetime] = Field(default=None, index=True)
    replaced_by_id: Optional[str] = Field(default=None, index=True)
    # New refreshes retain the active owner id and retire the old bearer hash
    # in a separate revoked row.  Legacy replacement rows remain represented by
    # ``is_bearer_tombstone=False`` and are surfaced as recovery-required.
    is_bearer_tombstone: bool = Field(
        default=False,
        sa_column=Column(
            Boolean,
            nullable=False,
            server_default=text("false"),
            index=True,
        ),
    )


class OperatorIdentity(SQLModel, table=True):
    __tablename__ = "operator_identities"
    id: str = Field(default_factory=_uuid, primary_key=True)
    created_at: datetime = Field(default_factory=_now)
    revoked_at: Optional[datetime] = Field(default=None)


class OperatorContinuityCredential(SQLModel, table=True):
    __tablename__ = "operator_continuity_credentials"
    id: str = Field(default_factory=_uuid, primary_key=True)
    identity_id: str = Field(foreign_key="operator_identities.id", index=True)
    token_hash: str = Field(unique=True, index=True)
    kind: str = Field(index=True)
    expires_at: datetime
    revoked_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class OperatorRecoveryJournal(SQLModel, table=True):
    __tablename__ = "operator_recovery_journals"
    __table_args__ = (Index("ux_operator_recovery_identity_key", "identity_id", "idempotency_key", unique=True),)
    id: str = Field(default_factory=_uuid, primary_key=True)
    identity_id: str = Field(foreign_key="operator_identities.id", index=True)
    current_session_id: str = Field(index=True)
    idempotency_key: str
    request_digest: str
    selections_json: str
    state: str = Field(default="confirmed", index=True)
    fresh_work_json: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    rolled_back_at: Optional[datetime] = Field(default=None)


class MoltbookConnection(SQLModel, table=True):
    """One optional owner-scoped account; secret values remain in the Vault."""
    __tablename__ = "moltbook_connections"
    __table_args__ = (Index("ux_moltbook_connection_owner", "owner_principal_id", unique=True),)
    id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    revision: int = Field(default=1)
    mode: str = Field(default="pending_claim")
    account_id: str = Field(default="")
    account_name: str = Field(default="")
    vault_key: str = Field(default="", max_length=256)
    credential_binding: str = Field(default="", max_length=128)
    consent_json: str = Field(default="{}")
    active_job_id: Optional[str] = Field(default=None)
    active_deadline_at: Optional[datetime] = Field(default=None)
    active_payload_digest: str = Field(default="", max_length=128)
    cooldown_until: Optional[datetime] = Field(default=None)
    setup_key: str = Field(default="", max_length=128)
    setup_digest: str = Field(default="", max_length=128)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ForgejoConnection(SQLModel, table=True):
    """Fixed-site optional configuration; authority remains in native stores."""
    __tablename__ = "forgejo_connections"
    __table_args__ = (Index("ux_forgejo_connection_owner", "owner_principal_id", unique=True),)
    id: str = Field(default_factory=_uuid, primary_key=True)
    owner_principal_id: str = Field(index=True)
    owner_session_id: str = Field(index=True)
    revision: int = Field(default=1)
    state: str = Field(default="configured")
    site_profile: str = Field(default="seraph.forgejo.codeberg-title.v1")
    provider_version: str = Field(default="15.0.9")
    credential_vault_key: str = Field(default="", max_length=256)
    credential_binding: str = Field(default="", max_length=128)
    session_vault_key: str = Field(default="", max_length=256)
    session_binding: str = Field(default="", max_length=128)
    provider_user_id: Optional[int] = Field(default=None)
    provider_login: str = Field(default="", max_length=128)
    read_consent_revision: int = Field(default=0)
    read_consent_expires_at: Optional[datetime] = Field(default=None)
    provisioning_job_id: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
