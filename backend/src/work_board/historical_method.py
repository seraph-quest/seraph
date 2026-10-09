"""Metadata-only witnesses for an admitted GeneralTask method.

The method pin shown by Home is historical admission metadata.  This module
does not select a current method, read a method body, or grant execution.  A
Task/Attempt writer may carry the sealed witness forward when its immutable
owner, Goal, and input-artifact tuple still exists; otherwise the caller gets
``None`` and the read projection remains Unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import inspect
import json
import re
from typing import Any, Awaitable, Callable, Mapping, TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:  # pragma: no cover - imports are intentionally lazy at runtime
    from src.db.models import WorkBoardAttempt, WorkBoardTask
    from src.work_board.contracts import GeneralTaskEnvelope, WorkBoardOwner


SCHEMA_VERSION = 1
MAC_DOMAIN = b"seraph-home-admitted-method-v1\0"
_IDENTIFIER_MAX_BYTES = 512
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_KEY_ID = re.compile(r"^[a-f0-9]{24}$")
_MAC = re.compile(r"^[a-f0-9]{64}$")
_MISSING = object()

_WITNESS_FIELDS = frozenset({
    "schema_version",
    "task_id",
    "owner_principal_id",
    "owner_session_id",
    "goal_id",
    "goal_revision",
    "input_artifact_id",
    "input_digest",
    "strategy_status",
    "method_id",
    "version",
    "method_digest",
    "admitted_at",
    "admission_task_revision",
    "attempt_id",
    "key_id",
    "mac",
})
_UNSIGNED_FIELDS = _WITNESS_FIELDS - {"mac"}


def _canonical(value: Any) -> bytes:
    """Encode the small closed witness without importing GeneralTaskService."""

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ValueError("historical method witness exceeds its bound")
    return encoded


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError:
        return None
    if len(encoded) > _IDENTIFIER_MAX_BYTES or any(ord(char) < 0x20 for char in value):
        return None
    return value


def _field(value: Any, name: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        result = value.get(name, _MISSING)
    else:
        result = getattr(value, name, _MISSING)
    if result is _MISSING and default is not _MISSING:
        return default
    return result


def _utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        return None
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    if not isinstance(value, datetime):
        return ""
    parsed = value if value.tzinfo is not None and value.utcoffset() is not None else value.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _key_id(key: bytes) -> str:
    from src.memory.repository import _m5_selection_binding_key_id

    return _m5_selection_binding_key_id(_signing_key=key)


def _mac(unsigned: Mapping[str, Any], key: bytes) -> str:
    return hmac.new(key, MAC_DOMAIN + _canonical(dict(unsigned)), hashlib.sha256).hexdigest()


def _signing_key(handle: Any = None) -> bytes | None:
    """Resolve only an already-staged in-memory key; never initialize one."""

    if handle is not None:
        if isinstance(handle, bytes):
            return handle or None
        if isinstance(handle, bytearray):
            return bytes(handle) or None
        candidate = getattr(handle, "signing_key", _MISSING)
        if callable(candidate):
            candidate = candidate()
        if isinstance(candidate, (bytes, bytearray)):
            return bytes(candidate) or None
        candidate = getattr(handle, "key", _MISSING)
        if isinstance(candidate, (bytes, bytearray)):
            return bytes(candidate) or None
        return None
    return historical_method_service.signing_key


def _admission_error(code: str, message: str, *, status_code: int = 409):
    """Construct a typed source-admission failure without importing at load time."""

    from src.work_board.repository import BoardError

    return BoardError(code, message, status_code=status_code)


class HistoricalMethodService:
    """Lifecycle-owned in-memory signer for the historical metadata witness."""

    def __init__(self) -> None:
        self._started = False
        self._key: bytes | None = None

    @property
    def started(self) -> bool:
        return self._started

    @property
    def signing_key(self) -> bytes | None:
        # A stopped service must not accidentally become a lazy signer.
        return self._key if self._started else None

    @property
    def signing_key_id(self) -> str | None:
        key = self.signing_key
        return _key_id(key) if key is not None else None

    async def start(self) -> None:
        from src.extensions.capability_execution import CapabilityJournalError, _effect_mac_key

        self._started = True
        try:
            # The source helper is a bounded in-process derivation.  Keep the
            # lifecycle handle synchronous so an unavailable key cannot leave
            # an executor thread behind during shutdown.
            self._key = _effect_mac_key()
        except (CapabilityJournalError, OSError, RuntimeError):
            # The feature remains metadata-unavailable; callers do not mint a
            # replacement key or turn an unavailable signer into a baseline.
            self._key = None

    async def stop(self) -> None:
        self._key = None
        self._started = False


historical_method_service = HistoricalMethodService()


@dataclass(frozen=True)
class AcceptedMethodStage:
    """Owner-validated, unsigned admission facts waiting for the source writer."""

    owner_principal_id: str
    owner_session_id: str
    goal_id: str
    goal_revision: int
    input_artifact_id: str
    input_digest: str
    strategy_status: str
    method_id: str | None
    version: str | None
    method_digest: str | None
    admitted_at: datetime
    admission_task_revision: int
    key_id: str
    # The signer is captured before entering the repository writer.  The
    # writer only compares the still-live lifecycle handle for loss/rotation;
    # it never initializes or fetches key material from storage.
    signing_key: bytes = field(default=b"", repr=False, compare=False)
    # Private, ephemeral source-owner callback.  It is never serialized into
    # the historical witness and cannot grant execution or alter the method.
    current_strategy_check: Callable[[Any, Any, "AcceptedMethodStage"], Awaitable[None]] | None = field(
        default=None, repr=False, compare=False
    )


@dataclass(frozen=True)
class HistoricalMethodWitness:
    """Validated closed witness used only for metadata carry/reseal."""

    task_id: str
    owner_principal_id: str
    owner_session_id: str
    goal_id: str
    goal_revision: int
    input_artifact_id: str
    input_digest: str
    strategy_status: str
    method_id: str | None
    version: str | None
    method_digest: str | None
    admitted_at: str
    admission_task_revision: int
    attempt_id: str | None
    key_id: str

    def unsigned(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "owner_principal_id": self.owner_principal_id,
            "owner_session_id": self.owner_session_id,
            "goal_id": self.goal_id,
            "goal_revision": self.goal_revision,
            "input_artifact_id": self.input_artifact_id,
            "input_digest": self.input_digest,
            "strategy_status": self.strategy_status,
            "method_id": self.method_id,
            "version": self.version,
            "method_digest": self.method_digest,
            "admitted_at": self.admitted_at,
            "admission_task_revision": self.admission_task_revision,
            "attempt_id": self.attempt_id,
            "key_id": self.key_id,
        }


def _stage_unsigned(stage: AcceptedMethodStage, *, task_id: str, attempt_id: str | None = None) -> dict[str, Any] | None:
    task_id = _identifier(task_id)
    if task_id is None or _identifier(stage.owner_principal_id) is None or _identifier(stage.owner_session_id) is None:
        return None
    if _identifier(stage.goal_id) is None or _identifier(stage.input_artifact_id) is None:
        return None
    if not isinstance(stage.goal_revision, int) or isinstance(stage.goal_revision, bool) or stage.goal_revision < 1:
        return None
    if not isinstance(stage.admission_task_revision, int) or isinstance(stage.admission_task_revision, bool) or stage.admission_task_revision < 1:
        return None
    if not _DIGEST.fullmatch(str(stage.input_digest)):
        return None
    if stage.strategy_status not in {"none", "active"}:
        return None
    identities = (stage.method_id, stage.version, stage.method_digest)
    if stage.strategy_status == "none":
        if any(item is not None for item in identities):
            return None
    elif (_identifier(stage.method_id) is None or _identifier(stage.version) is None
          or not isinstance(stage.method_digest, str) or not _DIGEST.fullmatch(stage.method_digest)):
        return None
    if not isinstance(stage.admitted_at, datetime) or _utc_datetime(_utc_iso(stage.admitted_at)) is None:
        return None
    if not _KEY_ID.fullmatch(str(stage.key_id)):
        return None
    if attempt_id is not None and _identifier(attempt_id) is None:
        return None
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": task_id,
        "owner_principal_id": stage.owner_principal_id,
        "owner_session_id": stage.owner_session_id,
        "goal_id": stage.goal_id,
        "goal_revision": stage.goal_revision,
        "input_artifact_id": stage.input_artifact_id,
        "input_digest": stage.input_digest,
        "strategy_status": stage.strategy_status,
        "method_id": stage.method_id,
        "version": stage.version,
        "method_digest": stage.method_digest,
        "admitted_at": _utc_iso(stage.admitted_at),
        "admission_task_revision": stage.admission_task_revision,
        "attempt_id": attempt_id,
        "key_id": stage.key_id,
    }


def _seal(unsigned: Mapping[str, Any], key: bytes) -> str:
    body = dict(unsigned)
    body["mac"] = _mac(unsigned, key)
    return _canonical(body).decode("utf-8")


def _task_tuple(task: Any) -> tuple[Any, ...] | None:
    values = tuple(_field(task, name, None) for name in (
        "task_id", "owner_principal_id", "owner_session_id", "goal_id",
        "goal_revision", "input_artifact_id", "typed_input_digest",
    ))
    if any(value is None for value in values):
        return None
    if (not isinstance(values[4], int) or isinstance(values[4], bool)
            or values[4] < 1):
        return None
    return values


def _validate_raw(raw: Any, *, key: bytes | None) -> tuple[HistoricalMethodWitness | None, str]:
    if raw is None:
        return None, "method_projection_missing"
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, "method_projection_invalid"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, "method_projection_invalid"
    if not isinstance(raw, Mapping) or set(raw) != _WITNESS_FIELDS:
        return None, "method_projection_invalid"
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != SCHEMA_VERSION:
        return None, "method_projection_invalid"
    identifiers = (
        "task_id", "owner_principal_id", "owner_session_id", "goal_id", "input_artifact_id",
    )
    if any(_identifier(raw.get(name)) is None for name in identifiers):
        return None, "method_projection_invalid"
    if type(raw.get("goal_revision")) is not int or isinstance(raw["goal_revision"], bool) or raw["goal_revision"] < 1:
        return None, "method_projection_invalid"
    if type(raw.get("admission_task_revision")) is not int or isinstance(raw["admission_task_revision"], bool) or raw["admission_task_revision"] < 1:
        return None, "method_projection_invalid"
    if not isinstance(raw.get("input_digest"), str) or not _DIGEST.fullmatch(raw["input_digest"]):
        return None, "method_projection_invalid"
    status = raw.get("strategy_status")
    if status not in {"none", "active"}:
        return None, "method_projection_invalid"
    method_values = (raw.get("method_id"), raw.get("version"), raw.get("method_digest"))
    if status == "none":
        if any(value is not None for value in method_values):
            return None, "method_projection_invalid"
    elif (_identifier(raw.get("method_id")) is None or _identifier(raw.get("version")) is None
          or not isinstance(raw.get("method_digest"), str) or not _DIGEST.fullmatch(raw["method_digest"] or "")):
        return None, "method_projection_invalid"
    if _utc_datetime(raw.get("admitted_at")) is None:
        return None, "method_projection_invalid"
    attempt_id = raw.get("attempt_id")
    if attempt_id is not None and _identifier(attempt_id) is None:
        return None, "method_projection_invalid"
    if not isinstance(raw.get("key_id"), str) or not _KEY_ID.fullmatch(raw["key_id"]):
        return None, "method_projection_invalid"
    if not isinstance(raw.get("mac"), str) or not _MAC.fullmatch(raw["mac"]):
        return None, "method_projection_invalid"
    if key is None:
        return None, "method_key_unavailable"
    try:
        expected = _mac({name: raw[name] for name in _UNSIGNED_FIELDS}, key)
    except (TypeError, ValueError, OverflowError, UnicodeError):
        return None, "method_projection_invalid"
    if _key_id(key) != raw["key_id"] or not hmac.compare_digest(raw["mac"], expected):
        return None, "method_projection_invalid"
    witness = HistoricalMethodWitness(
        task_id=raw["task_id"], owner_principal_id=raw["owner_principal_id"],
        owner_session_id=raw["owner_session_id"], goal_id=raw["goal_id"],
        goal_revision=raw["goal_revision"], input_artifact_id=raw["input_artifact_id"],
        input_digest=raw["input_digest"], strategy_status=status,
        method_id=raw["method_id"], version=raw["version"], method_digest=raw["method_digest"],
        admitted_at=raw["admitted_at"], admission_task_revision=raw["admission_task_revision"],
        attempt_id=attempt_id, key_id=raw["key_id"],
    )
    return witness, ""


def verify_historical_method(
    raw: Any,
    *,
    task: Any,
    attempt: Any | None = None,
    owner: Any | None = None,
    signing_handle: Any = None,
) -> HistoricalMethodWitness | None:
    """Verify one witness against the exact scalar Task/Attempt tuple.

    This function is deliberately synchronous and metadata-only.  It never
    resolves a method pointer or opens an input artifact file.
    """

    key = _signing_key(signing_handle)
    witness, _reason = _validate_raw(raw, key=key)
    if witness is None:
        return None
    values = _task_tuple(task)
    if values is None:
        return None
    task_id, principal_id, session_id, goal_id, goal_revision, artifact_id, input_digest = values
    if (witness.task_id, witness.owner_principal_id, witness.owner_session_id,
        witness.goal_id, witness.goal_revision, witness.input_artifact_id,
        witness.input_digest) != (task_id, principal_id, session_id, goal_id,
        goal_revision, artifact_id, input_digest):
        return None
    if owner is not None and (witness.owner_principal_id != _field(owner, "principal_id", None)
                              or witness.owner_session_id != _field(owner, "session_id", None)):
        return None
    expected_attempt = _field(attempt, "attempt_id", None) if attempt is not None else None
    if attempt is None:
        if witness.attempt_id is not None:
            return None
    elif witness.attempt_id != expected_attempt or _field(attempt, "task_id", None) != task_id:
        return None
    return witness


async def _metadata_tuple(db: Any, *, task: Any, require_task_binding: bool) -> bool:
    """Check only canonical Goal/input-artifact scalar metadata."""

    from src.db.models import Goal, WorkBoardInputArtifact

    task_id, principal_id, session_id, goal_id, goal_revision, artifact_id, input_digest = _task_tuple(task) or (None,) * 7
    if not all((task_id, principal_id, session_id, goal_id, goal_revision, artifact_id, input_digest)):
        return False
    statement = (
        select(
            Goal.id,
            Goal.owner_principal_id,
            Goal.owner_session_id,
            WorkBoardInputArtifact.artifact_id,
            WorkBoardInputArtifact.payload_sha256,
            WorkBoardInputArtifact.typed_input_ref,
            WorkBoardInputArtifact.bound_task_id,
            WorkBoardInputArtifact.state,
        )
        .join(WorkBoardInputArtifact, WorkBoardInputArtifact.goal_id == Goal.id)
        .where(
            Goal.id == goal_id,
            Goal.owner_principal_id == principal_id,
            Goal.owner_session_id == session_id,
            WorkBoardInputArtifact.artifact_id == artifact_id,
            WorkBoardInputArtifact.owner_principal_id == principal_id,
            WorkBoardInputArtifact.owner_session_id == session_id,
            WorkBoardInputArtifact.goal_id == goal_id,
            WorkBoardInputArtifact.goal_revision == goal_revision,
            WorkBoardInputArtifact.capability_id == "agent.task.v1",
            WorkBoardInputArtifact.payload_sha256 == input_digest,
        )
    )
    row = (await db.execute(statement)).one_or_none()
    if row is None:
        return False
    (_goal_id, _goal_principal, _goal_session, _artifact_id, _payload_sha256,
        artifact_ref, bound_task_id, state) = row
    if state not in {"pending", "bound", "consumed"}:
        return False
    expected_ref = _field(task, "typed_input_ref", None)
    if expected_ref is not None and artifact_ref != expected_ref:
        return False
    if require_task_binding and bound_task_id != task_id:
        return False
    return bound_task_id in {None, task_id}


def _strategy_fields(envelope: Any) -> tuple[str, str | None, str | None, str | None] | None:
    strategy = _field(envelope, "strategy", None)
    status = _field(strategy, "status", None)
    if status == "none":
        return "none", None, None, None
    if status != "active":
        return None
    method_id = _identifier(_field(strategy, "method_id", None))
    version = _identifier(_field(strategy, "version", None))
    method_digest = _field(strategy, "digest", None)
    if method_id is None or version is None or not isinstance(method_digest, str) or not _DIGEST.fullmatch(method_digest):
        return None
    return "active", method_id, version, method_digest


async def stage_accepted_method(
    db: Any,
    *,
    owner: Any,
    envelope: Any,
    artifact: Any | None = None,
    task: Any | None = None,
    admission_task_revision: int | None = None,
    admitted_at: datetime | None = None,
    current_strategy_check: Callable[[Any, Any, AcceptedMethodStage], Awaitable[None]] | None = None,
) -> AcceptedMethodStage | None:
    """Stage facts already validated by the real GeneralTask source owner."""

    key = _signing_key()
    if key is None:
        raise _admission_error(
            "general_task_method_signer_unavailable",
            "The historical admission signer is unavailable",
            status_code=503,
        )
    if not callable(current_strategy_check):
        raise _admission_error(
            "general_task_strategy_owner_unavailable",
            "Restore the current method owner before accepting this task",
            status_code=503,
        )
    fields = _strategy_fields(envelope)
    if fields is None:
        return None
    owner_principal_id = _identifier(_field(owner, "principal_id", None))
    owner_session_id = _identifier(_field(owner, "session_id", None))
    task_input = _field(envelope, "task_input", None)
    goal_id = _identifier(_field(task_input, "goal_ref", None))
    if owner_principal_id is None or owner_session_id is None or goal_id is None:
        return None

    if task is not None:
        values = _task_tuple(task)
        if values is None:
            return None
        task_id, task_principal, task_session, task_goal, task_goal_revision, artifact_id, input_digest = values
        if (task_principal, task_session, task_goal) != (owner_principal_id, owner_session_id, goal_id):
            return None
        goal_revision = task_goal_revision
        if admission_task_revision is None:
            admission_task_revision = _field(task, "task_revision", None)
    else:
        task_id = None
        goal_revision = _field(envelope, "goal_revision", None)
        if goal_revision is None:
            goal_revision = _field(artifact, "goal_revision", None)
        artifact_id = _field(artifact, "artifact_id", None)
        input_digest = _field(artifact, "typed_input_digest", None) or _field(artifact, "payload_sha256", None)
        if admission_task_revision is None:
            admission_task_revision = 1
    artifact_id = _identifier(artifact_id)
    if artifact_id is None or not isinstance(goal_revision, int) or isinstance(goal_revision, bool) or goal_revision < 1:
        return None
    if not isinstance(admission_task_revision, int) or isinstance(admission_task_revision, bool) or admission_task_revision < 1:
        return None
    if not isinstance(input_digest, str) or not _DIGEST.fullmatch(input_digest):
        return None
    if task is not None and not await _metadata_tuple(db, task=task, require_task_binding=True):
        return None
    if task is None:
        # Reuse the same scalar metadata check without manufacturing a Task
        # identity.  The artifact and Goal rows are still source-owned.
        from src.db.models import Goal, WorkBoardInputArtifact
        row = (await db.execute(select(
                Goal.id,
                WorkBoardInputArtifact.artifact_id,
                WorkBoardInputArtifact.typed_input_ref,
                WorkBoardInputArtifact.state,
                WorkBoardInputArtifact.bound_task_id,
            )
            .join(WorkBoardInputArtifact, WorkBoardInputArtifact.goal_id == Goal.id)
            .where(Goal.id == goal_id, Goal.owner_principal_id == owner_principal_id,
                   Goal.owner_session_id == owner_session_id,
                   WorkBoardInputArtifact.artifact_id == artifact_id,
                   WorkBoardInputArtifact.owner_principal_id == owner_principal_id,
                   WorkBoardInputArtifact.owner_session_id == owner_session_id,
                   WorkBoardInputArtifact.goal_id == goal_id,
                   WorkBoardInputArtifact.goal_revision == goal_revision,
                   WorkBoardInputArtifact.payload_sha256 == input_digest,
                   WorkBoardInputArtifact.capability_id == "agent.task.v1"))).one_or_none()
        if row is None:
            return None
        _goal_id, _artifact_id, artifact_ref, artifact_state, bound_task_id = row
        expected_ref = _field(artifact, "typed_input_ref", None)
        if (artifact_state != "pending"
                or bound_task_id is not None
                or (expected_ref is not None and artifact_ref != expected_ref)):
            return None
    admitted = admitted_at or datetime.now(timezone.utc)
    if _utc_datetime(_utc_iso(admitted)) is None:
        return None
    return AcceptedMethodStage(
        owner_principal_id=owner_principal_id,
        owner_session_id=owner_session_id,
        goal_id=goal_id,
        goal_revision=goal_revision,
        input_artifact_id=artifact_id,
        input_digest=input_digest,
        strategy_status=fields[0],
        method_id=fields[1],
        version=fields[2],
        method_digest=fields[3],
        admitted_at=admitted,
        admission_task_revision=admission_task_revision,
        key_id=_key_id(key),
        signing_key=key,
        current_strategy_check=current_strategy_check,
    )


async def publish_accepted_method(db: Any, task: Any, stage: AcceptedMethodStage | None) -> str | None:
    """Seal an accepted Task witness inside its original source transaction."""

    if not isinstance(stage, AcceptedMethodStage):
        return None
    current_key = _signing_key()
    if current_key is None:
        raise _admission_error(
            "general_task_method_signer_unavailable",
            "The historical admission signer is unavailable",
            status_code=503,
        )
    if not isinstance(stage.signing_key, bytes) or not stage.signing_key:
        raise _admission_error(
            "general_task_method_signer_unavailable",
            "The staged historical admission signer is unavailable",
            status_code=503,
        )
    if stage.key_id != _key_id(current_key) or stage.key_id != _key_id(stage.signing_key):
        raise _admission_error(
            "general_task_method_signer_rotated",
            "The historical admission signer changed before publication",
            status_code=503,
        )
    values = _task_tuple(task)
    if values is None:
        raise _admission_error("general_task_admission_binding_changed", "The accepted task binding changed")
    task_id, principal_id, session_id, goal_id, goal_revision, artifact_id, input_digest = values
    if (principal_id, session_id, goal_id, goal_revision, artifact_id, input_digest) != (
        stage.owner_principal_id, stage.owner_session_id, stage.goal_id,
        stage.goal_revision, stage.input_artifact_id, stage.input_digest,
    ):
        raise _admission_error("general_task_admission_binding_changed", "The accepted task binding changed")
    if _field(task, "admitted_method_json", None) not in (None, ""):
        raise _admission_error("general_task_admission_replayed", "The task already has an accepted method witness")
    if _field(task, "task_revision", None) != stage.admission_task_revision:
        raise _admission_error("general_task_admission_revision_changed", "The accepted task revision changed")
    # Publication is intentionally after the original writer binds the
    # canonical input row to this Task.  A pending/unbound reservation cannot
    # become historical admission evidence.
    if not await _metadata_tuple(db, task=task, require_task_binding=True):
        raise _admission_error("general_task_admission_source_changed", "The accepted Goal or input artifact changed")
    checker = stage.current_strategy_check
    if not callable(checker):
        raise _admission_error(
            "general_task_strategy_owner_unavailable",
            "Restore the current method owner before accepting this task",
            status_code=503,
        )
    result = checker(db, task, stage)
    if inspect.isawaitable(result):
        result = await result
    if result is False:
        raise _admission_error("general_task_strategy_changed", "Review the current task method")
    unsigned = _stage_unsigned(stage, task_id=task_id)
    if unsigned is None:
        raise _admission_error("general_task_admission_binding_changed", "The accepted method witness is invalid")
    return _seal(unsigned, stage.signing_key)


async def stage_attempt_method_metadata(
    db: Any,
    *,
    task: Any,
    new_attempt: Any,
    signing_handle: Any = None,
) -> str | None:
    """Carry a valid original Task witness to one actual Attempt insertion."""

    attempt_id = _identifier(_field(new_attempt, "attempt_id", None))
    if (attempt_id is None
            or _field(new_attempt, "task_id", None) != _field(task, "task_id", None)
            or _field(new_attempt, "admitted_method_json", None) not in (None, "")):
        return None
    raw = _field(task, "admitted_method_json", None)
    key = _signing_key(signing_handle)
    witness = verify_historical_method(raw, task=task, attempt=None, signing_handle=key)
    if witness is None:
        return None
    if not await _metadata_tuple(db, task=task, require_task_binding=True):
        return None
    if key is None:
        return None
    # Keep the original UTC spelling and admission timestamp byte-for-byte in
    # the carried witness.  Only the real replacement Attempt identity changes.
    unsigned = witness.unsigned()
    unsigned["attempt_id"] = attempt_id
    return _seal(unsigned, key)


def project_historical_method(
    raw: Any,
    *,
    task: Any,
    attempt: Any | None = None,
    owner: Any | None = None,
    target: Mapping[str, Any] | None = None,
    signing_handle: Any = None,
) -> dict[str, Any]:
    """Return the closed Home method wire without copying invalid identifiers."""

    reason = "method_projection_missing" if raw is None else "method_projection_invalid"
    key = _signing_key(signing_handle)
    if raw is not None:
        _parsed, validation_reason = _validate_raw(raw, key=key)
        reason = validation_reason or reason
    witness = verify_historical_method(
        raw,
        task=task,
        attempt=attempt,
        owner=owner,
        signing_handle=signing_handle,
    )
    if witness is None:
        return {"status": "unknown", "method_id": None, "version": None,
            "digest": None, "admitted_at": None, "lifecycle": "unknown",
            "reason_code": reason, "target": None}
    status = "baseline" if witness.strategy_status == "none" else "admitted"
    safe_target = None
    if witness.strategy_status == "active" and target is not None and isinstance(target, Mapping):
        target_id = _identifier(target.get("proposal_id") or target.get("method_id"))
        target_version = _identifier(target.get("version"))
        target_digest = target.get("digest")
        if (target_id is not None and target_version is not None
                and isinstance(target_digest, str) and _DIGEST.fullmatch(target_digest)):
            safe_target = {"kind": "method", "proposal_id": target_id,
                "version": target_version, "digest": target_digest}
    return {"status": status, "method_id": witness.method_id, "version": witness.version,
        "digest": witness.method_digest, "admitted_at": witness.admitted_at,
        "lifecycle": "active_metadata", "reason_code": None, "target": safe_target}


historical_method_projection = project_historical_method


__all__ = [
    "AcceptedMethodStage",
    "HistoricalMethodService",
    "HistoricalMethodWitness",
    "historical_method_service",
    "historical_method_projection",
    "project_historical_method",
    "publish_accepted_method",
    "stage_accepted_method",
    "stage_attempt_method_metadata",
    "verify_historical_method",
]
