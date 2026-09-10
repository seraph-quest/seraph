"""Fail-closed execution records for locally adopted capabilities.

The native tool registry describes capabilities, but it historically had no
common execution boundary.  This module is deliberately small: it provides a
local, provider-free journal and a single invocation path for the filesystem
and process tools that are adopted by the authority wrappers.

The journal is an operational recovery record, rather than an audit log.  It
contains digests and bounded summaries only.  A record that was marked as
started before a process restart is changed to ``uncertain`` and cannot be
replayed automatically.  The effect handler is intentionally private to the
adapter layer; callers of :meth:`CapabilityExecutionHost.execute` select a
registered handler and cannot supply an arbitrary callback.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import posixpath
import tempfile
import threading
import time
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from src.audit.formatting import _is_sensitive_key, redact_for_audit
from src.security.trust_contract import canonical_digest

_JOURNAL_VERSION = 3
_DEFAULT_MAX_RECORDS = 256
_DEFAULT_MAX_BYTES = 1_048_576
_DEFAULT_OUTPUT_BYTES = 1_048_576
_DEFAULT_CPU_SECONDS = 300.0
_DEFAULT_MEMORY_BYTES = 512 * 1024 * 1024
_DEFAULT_PROCESS_COUNT = 64
_MAX_IDEMPOTENCY_BYTES = 256
_MAX_DESTINATION_BYTES = 512
_MAX_OUTPUT_BYTES = 1 * 1024 * 1024
_MAX_CPU_SECONDS = 300.0
_MAX_MEMORY_BYTES = 512 * 1024 * 1024
_MAX_PROCESS_COUNT = 64
_TERMINAL_STATES = frozenset({"succeeded", "failed"})
_RECOVERABLE_STATES = frozenset({"prepared", "started", "uncertain", "failed"})
_VALID_STATES = _TERMINAL_STATES | _RECOVERABLE_STATES
_DIGEST_LENGTH = 64
_ALLOWED_LOCAL_DESTINATION_SCHEMES = frozenset({"local", "workspace", "seraph"})
_ADOPTED_CAPABILITIES = frozenset(
    {
        "read_file",
        "write_file",
        "preview_workspace_patch",
        "apply_workspace_patch",
        "run_command",
        "start_process",
        "list_processes",
        "read_process_output",
        "stop_process",
    }
)
_JOURNAL_LOCKS: dict[Path, threading.RLock] = {}
_JOURNAL_LOCKS_GUARD = threading.Lock()
_REGISTRY_TOKEN = object()
_RAW_RESULT_TOKEN = object()
_INTERNAL_RAW_RESULT = ContextVar("capability_internal_raw_result", default=None)

try:
    import fcntl
except ImportError:  # pragma: no cover - the runtime is currently POSIX-only.
    fcntl = None  # type: ignore[assignment]


class CapabilityExecutionError(PermissionError):
    """A capability call was denied or could not be made recoverably."""

    def __init__(self, reason_code: str, message: str | None = None, *, recoverable: bool = False):
        self.reason_code = reason_code
        self.recoverable = recoverable
        super().__init__(message or reason_code)


class CapabilityJournalError(RuntimeError):
    """The durable local execution journal cannot be trusted."""


@dataclass(frozen=True)
class CapabilityExecutionLimits:
    """The local hard bounds carried into a capability request."""

    cpu_seconds: float = _DEFAULT_CPU_SECONDS
    memory_bytes: int = _DEFAULT_MEMORY_BYTES
    process_count: int = _DEFAULT_PROCESS_COUNT
    output_bytes: int = _DEFAULT_OUTPUT_BYTES
    deadline_seconds: float = _DEFAULT_CPU_SECONDS

    def __post_init__(self) -> None:
        for value, name in (
            (self.cpu_seconds, "cpu_seconds"),
            (self.deadline_seconds, "deadline_seconds"),
        ):
            maximum = _MAX_CPU_SECONDS
            if (
                not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
                or value > maximum
            ):
                raise ValueError(f"{name} must be a finite positive number")
        for value, name in (
            (self.memory_bytes, "memory_bytes"),
            (self.process_count, "process_count"),
            (self.output_bytes, "output_bytes"),
        ):
            maximum = {
                "memory_bytes": _MAX_MEMORY_BYTES,
                "process_count": _MAX_PROCESS_COUNT,
                "output_bytes": _MAX_OUTPUT_BYTES,
            }[name]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
                or value > maximum
            ):
                raise ValueError(f"{name} must be a positive integer")

    def as_dict(self) -> dict[str, int | float]:
        return {
            "cpu_seconds": float(self.cpu_seconds),
            "memory_bytes": self.memory_bytes,
            "process_count": self.process_count,
            "output_bytes": self.output_bytes,
            "deadline_seconds": float(self.deadline_seconds),
        }

    def digest(self) -> str:
        return canonical_digest(self.as_dict())


# Alias used by adapters that already call their bounds ResourceLimits.
CapabilityLimits = CapabilityExecutionLimits


def normalize_destination(destination: str) -> str:
    """Return a bounded local destination representation without retaining secrets."""
    value = str(destination or "").strip()
    if not value:
        raise ValueError("destination is required")
    if len(value.encode("utf-8")) > _MAX_DESTINATION_BYTES:
        raise ValueError("destination exceeds the bounded local policy")
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in _ALLOWED_LOCAL_DESTINATION_SCHEMES:
            raise ValueError("network destinations are not adopted by the local capability host")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("destination credentials and fragments are not allowed")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("destination port is invalid") from exc
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/") or "/",
                parsed.query,
                "",
            )
        )
    return posixpath.normpath(value.replace("\\", "/"))


def _journal_mac_secret() -> str:
    """Return configured server secret material without exposing it in errors."""
    try:
        from config.settings import settings
    except Exception as exc:  # pragma: no cover - settings is available in-app.
        raise CapabilityJournalError("execution journal MAC key unavailable") from exc

    for name in (
        "capability_journal_secret",
        "capability_journal_secret_hash",
        "operator_auth_secret",
        "operator_auth_secret_hash",
    ):
        value = str(getattr(settings, name, "") or "").strip()
        if value:
            return value
    raise CapabilityJournalError("execution journal MAC key unavailable")


def _server_secret_key() -> bytes:
    """Derive MAC material from a configured server secret, never source text."""
    return hashlib.sha256(
        b"seraph-capability-server-key-v1:" + _journal_mac_secret().encode("utf-8")
    ).digest()


def _effect_mac_key() -> bytes:
    """Return the configured server-derived key for effect identities/records."""
    return hmac.new(
        _server_secret_key(),
        b"effect-identity-and-records-v1",
        hashlib.sha256,
    ).digest()


def _journal_mac_key(path: Path) -> bytes:
    """Derive a per-journal MAC key from server secret material and identity."""
    identity = str(path.resolve()).encode("utf-8")
    return hmac.new(
        _server_secret_key(),
        b"journal-v3:" + identity,
        hashlib.sha256,
    ).digest()


def _journal_error_code(exc: CapabilityJournalError) -> str:
    """Map journal failures to bounded operator-visible reason codes."""
    if "MAC key unavailable" in str(exc):
        return "journal_mac_key_unavailable"
    return "journal_recovery_required"


def _mac(value: Any, *, key: bytes) -> str:
    payload = json.dumps(_canonical_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _canonical_value(value: Any) -> Any:
    """Make request identity deterministic without serializing object reprs."""
    if isinstance(value, threading.Event):
        return {"__cancel_event__": True}
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(inner) for key, inner in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"__bytes_sha256__": hashlib.sha256(bytes(value)).hexdigest(), "length": len(value)}
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _digest(value: Any) -> str:
    return canonical_digest(_canonical_value(value))


def _iter_json_string(value: str):
    """Yield an ASCII JSON string in small chunks.

    ``json.dumps`` emits a whole string as one allocation.  Capability output
    can be supplied by an adapter, so chunking here keeps the bounded result
    path from materializing a large nested string before the limit is known.
    """
    yield '"'
    for offset in range(0, len(value), 4096):
        encoded = json.dumps(
            value[offset : offset + 4096],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        yield encoded[1:-1]
    yield '"'


def _iter_json_chunks(value: Any, *, key_hint: str | None = None, redact: bool = False):
    """Stream the canonical JSON representation used for result receipts."""
    if redact and key_hint and _is_sensitive_key(key_hint):
        yield from _iter_json_string("[redacted]")
        return
    if isinstance(value, Mapping):
        yield "{"
        for index, (key, inner) in enumerate(sorted(value.items(), key=lambda item: str(item[0]))):
            if index:
                yield ","
            key_text = str(key)
            yield from _iter_json_string(key_text)
            yield ":"
            yield from _iter_json_chunks(inner, key_hint=key_text, redact=redact)
        yield "}"
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        yield "["
        for index, inner in enumerate(value):
            if index:
                yield ","
            yield from _iter_json_chunks(inner, key_hint=key_hint, redact=redact)
        yield "]"
        return
    if isinstance(value, (bytes, bytearray)):
        yield "{"
        yield from _iter_json_string("__bytes_sha256__")
        yield ":"
        yield from _iter_json_string(hashlib.sha256(bytes(value)).hexdigest())
        yield ","
        yield from _iter_json_string("length")
        yield ":"
        yield str(len(value))
        yield "}"
        return
    if isinstance(value, threading.Event):
        yield '{"__cancel_event__":true}'
        return
    if isinstance(value, Path):
        yield from _iter_json_string(str(value))
        return
    if isinstance(value, str):
        text = value
        if redact and len(text) > 200:
            text = f"{text[:197]}..."
        yield from _iter_json_string(text)
        return
    if isinstance(value, (int, float, bool)) or value is None:
        yield json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        return
    yield from _iter_json_string(str(value))


def _json_stats(value: Any, *, redact: bool = False, keep_bytes: int = 0) -> tuple[int, str, bytes]:
    """Return byte count/digest and a bounded prefix without full serialization."""
    digest = hashlib.sha256()
    prefix = bytearray()
    total = 0
    for chunk in _iter_json_chunks(value, redact=redact):
        encoded = chunk.encode("utf-8", errors="replace")
        digest.update(encoded)
        total += len(encoded)
        if len(prefix) < keep_bytes:
            prefix.extend(encoded[: keep_bytes - len(prefix)])
    return total, digest.hexdigest(), bytes(prefix)


def _bounded_json(value: Any, *, limit: int) -> tuple[Any, bool]:
    """Bound a result for the caller while retaining its basic shape."""
    if isinstance(value, str):
        raw = value.encode("utf-8", errors="replace")
        if len(raw) <= limit:
            return value, False
        marker = "\n...[truncated]..."
        marker_bytes = marker.encode()
        if limit <= len(marker_bytes):
            return marker_bytes[:limit].decode("utf-8", errors="ignore"), True
        clipped = raw[: limit - len(marker_bytes)].decode("utf-8", errors="ignore")
        return clipped + marker, True
    if isinstance(value, bytes):
        if len(value) <= limit:
            return value, False
        return value[:limit], True
    output_bytes, output_sha256, _ = _json_stats(value)
    if output_bytes <= limit:
        return value, False
    # Structured outputs are retained as a bounded, explicit receipt.  This
    # prevents a large provider/tool object from crossing the final boundary.
    return {
        "output_truncated": True,
        "output_bytes": output_bytes,
        "output_sha256": output_sha256,
    }, True


def _safe_summary(value: Any, *, output_bytes: int) -> dict[str, Any]:
    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="replace")
        safe_value: Any = redact_for_audit(value)
        bounded, truncated = _bounded_json(safe_value, limit=min(output_bytes, 4096))
    elif isinstance(value, bytes):
        encoded = value
        bounded, truncated = _bounded_json(value, limit=min(output_bytes, 4096))
    else:
        encoded_length, encoded_digest, _ = _json_stats(value)
        summary_limit = min(output_bytes, 4096)
        safe_length, safe_digest, safe_prefix = _json_stats(
            value,
            redact=True,
            keep_bytes=summary_limit,
        )
        if safe_length <= summary_limit:
            try:
                bounded = json.loads(safe_prefix.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                bounded = {"output_sha256": safe_digest, "output_bytes": safe_length}
            truncated = False
        else:
            bounded = {
                "output_truncated": True,
                "output_bytes": safe_length,
                "output_sha256": safe_digest,
            }
            truncated = True
        return {
            "type": type(value).__name__,
            "output_bytes": encoded_length,
            "output_sha256": encoded_digest,
            "output_truncated": truncated,
            "summary": bounded,
        }
    return {
        "type": type(value).__name__,
        "output_bytes": len(encoded),
        "output_sha256": hashlib.sha256(encoded).hexdigest(),
        "output_truncated": truncated,
        "summary": bounded if isinstance(bounded, (str, int, float, bool, type(None), dict, list)) else str(bounded),
    }


@dataclass(frozen=True)
class CapabilityExecutionRequest:
    """Identity and authority material for one capability attempt."""

    owner_principal_id: str
    capability_id: str
    capability_version: str
    destination: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    session_id: str = ""
    job_id: str = ""
    request_id: str = ""
    attempt_id: str = ""
    idempotency_key: str = ""
    approval_id: str = ""
    approval_digest: str = ""
    approval_binding: Mapping[str, Any] | None = None
    fencing_token: str = ""
    policy_version: str = "capability-execution-v1"
    expires_at: float | None = None
    limits: CapabilityExecutionLimits = field(default_factory=CapabilityExecutionLimits)
    replayable: bool = True

    def __post_init__(self) -> None:
        for value, name in (
            (self.owner_principal_id, "owner_principal_id"),
            (self.capability_id, "capability_id"),
            (self.capability_version, "capability_version"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if self.capability_id not in _ADOPTED_CAPABILITIES and not self.capability_id.startswith("test."):
            raise ValueError(f"capability '{self.capability_id}' is not adopted by the local host")
        if len(self.idempotency_key.strip().encode("utf-8")) > _MAX_IDEMPOTENCY_BYTES:
            raise ValueError("idempotency_key exceeds the bounded local policy")
        normalize_destination(self.destination)
        # Trust evaluation requires opaque request references.  They are not
        # part of the effect identity, so retries remain idempotent while the
        # host can reject malformed or replay-shaped authority material.
        if not self.request_id:
            object.__setattr__(self, "request_id", f"request:{uuid4().hex}")
        if not self.attempt_id:
            object.__setattr__(self, "attempt_id", f"attempt:{uuid4().hex}")

    @property
    def normalized_destination(self) -> str:
        return normalize_destination(self.destination)

    @property
    def arguments_digest(self) -> str:
        return _digest(self.arguments)

    @property
    def request_digest(self) -> str:
        # Request and attempt IDs intentionally do not participate in the
        # effect identity. Retries of the same requested effect must collide.
        return _digest(
            {
                "owner_principal_id": self.owner_principal_id,
                "capability_id": self.capability_id,
                "capability_version": self.capability_version,
                "destination": self.normalized_destination,
                "arguments": _canonical_value(self.arguments),
                "session_id": self.session_id,
                "job_id": self.job_id,
                "approval_digest": self.approval_digest or self.approval_id,
                "policy_version": self.policy_version,
                "limits": self.limits.as_dict(),
            }
        )

    @property
    def binding_digest(self) -> str:
        return _digest(
            {
                "owner_principal_id": self.owner_principal_id,
                "capability_id": self.capability_id,
                "capability_version": self.capability_version,
                "destination": self.normalized_destination,
                "request_digest": self.request_digest,
            }
        )

    @property
    def effect_digest(self) -> str:
        return _digest(
            {
                "binding_digest": self.binding_digest,
                "fencing_token": self.fencing_token,
                "limits_digest": self.limits.digest(),
            }
        )

    @property
    def duplicate_key(self) -> str:
        # Never persist a caller-controlled key. HMAC binds an optional caller
        # key to the complete owner/capability/destination request identity,
        # so the same key from another owner cannot collide or disclose input.
        material = {
            "owner_principal_id": self.owner_principal_id,
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "destination": self.normalized_destination,
            "request_digest": self.request_digest,
            "idempotency_key": self.idempotency_key.strip(),
        }
        return f"effect:{_mac(material, key=_effect_mac_key())}"

    def journal_binding(self) -> dict[str, str]:
        return {
            "owner_principal_digest": _digest({"owner_principal_id": self.owner_principal_id}),
            "capability_id": self.capability_id,
            "capability_version": self.capability_version,
            "destination_digest": _digest({"destination": self.normalized_destination}),
            "request_digest": self.request_digest,
            "binding_digest": self.binding_digest,
            "effect_digest": self.effect_digest,
        }


@dataclass(frozen=True)
class CapabilityExecutionReceipt:
    effect_id: str
    duplicate_key: str
    request_digest: str
    effect_digest: str
    state: str
    recoverable: bool
    replay_blocked: bool
    output_digest: str = ""
    output_bytes: int = 0
    output_truncated: bool = False
    output_summary: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    result: Any = field(default=None, repr=False, compare=False)
    started_at: float | None = None
    completed_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "effect_id": self.effect_id,
            "duplicate_key": self.duplicate_key,
            "request_digest": self.request_digest,
            "effect_digest": self.effect_digest,
            "state": self.state,
            "recoverable": self.recoverable,
            "replay_blocked": self.replay_blocked,
            "output_digest": self.output_digest,
            "output_bytes": self.output_bytes,
            "output_truncated": self.output_truncated,
            "output_summary": dict(self.output_summary),
            "error_code": self.error_code,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


CapabilityHandler = Callable[[Mapping[str, Any]], Any]


def _native_adapter_registry() -> dict[str, CapabilityHandler]:
    """Resolve the fixed module-owned native adapters lazily to avoid cycles."""
    from src.tools.filesystem_tool import (
        apply_workspace_patch,
        preview_workspace_patch,
        read_file,
        write_file,
    )
    from src.tools.process_tools import (
        list_processes,
        process_runtime_manager,
        read_process_output,
        run_command,
        start_process,
        stop_process,
    )

    def run_command_adapter(arguments: Mapping[str, Any]) -> Any:
        payload = dict(arguments)
        if "__seraph_raw_result" in payload:
            raise CapabilityExecutionError("raw_result_internal_only")
        if _INTERNAL_RAW_RESULT.get() is _RAW_RESULT_TOKEN:
            return process_runtime_manager.run_command(**payload)
        return run_command(**payload)

    return {
        "read_file": lambda arguments: read_file(**dict(arguments)),
        "write_file": lambda arguments: write_file(**dict(arguments)),
        "preview_workspace_patch": lambda arguments: preview_workspace_patch(**dict(arguments)),
        "apply_workspace_patch": lambda arguments: apply_workspace_patch(**dict(arguments)),
        "run_command": run_command_adapter,
        "start_process": lambda arguments: start_process(**dict(arguments)),
        "list_processes": lambda arguments: list_processes(**dict(arguments)),
        "read_process_output": lambda arguments: read_process_output(**dict(arguments)),
        "stop_process": lambda arguments: stop_process(**dict(arguments)),
    }


def _default_journal_path() -> Path:
    try:
        from config.settings import settings

        workspace = str(Path(settings.workspace_dir).expanduser().resolve())
    except Exception:
        workspace = "seraph-default-workspace"
    workspace_tag = hashlib.sha256(workspace.encode("utf-8")).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / "seraph_runtime" / workspace_tag / "capability-executions-v3.json"


class CapabilityExecutionHost:
    """Durable local chokepoint for adopted capability effects."""

    def __init__(
        self,
        *,
        journal_path: str | Path | None = None,
        handlers: Mapping[str, CapabilityHandler] | None = None,
        max_records: int = _DEFAULT_MAX_RECORDS,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ) -> None:
        if max_records < 1 or max_bytes < 4096:
            raise ValueError("journal retention bounds are invalid")
        if handlers:
            raise CapabilityExecutionError("handler_injection_forbidden")
        self.journal_path = Path(journal_path) if journal_path is not None else _default_journal_path()
        self._test_handlers: dict[str, CapabilityHandler] = {}
        self._uncertain_keys: set[str] = set()
        self._max_records = max_records
        self._max_bytes = max_bytes
        self._journal_recovery_error: str | None = None
        with _JOURNAL_LOCKS_GUARD:
            self._lock = _JOURNAL_LOCKS.setdefault(self.journal_path, threading.RLock())
        try:
            # Resolve the key at host creation so an unconfigured server never
            # presents an apparently ready execution boundary. The secret is
            # deliberately never included in the operator-facing error code.
            _journal_mac_key(self.journal_path)
            self._recover_incomplete()
        except CapabilityJournalError as exc:
            # A corrupt, unreadable, or over-bound journal must not take down
            # the whole backend, but this host remains fail-closed until an
            # operator can inspect or repair the durable record.
            self._journal_recovery_error = _journal_error_code(exc)
        except OSError:
            self._journal_recovery_error = "journal_recovery_required"

    @property
    def handlers(self) -> tuple[str, ...]:
        return tuple(sorted(set(_ADOPTED_CAPABILITIES) | set(self._test_handlers)))

    def _register_handler(
        self,
        capability_id: str,
        handler: CapabilityHandler,
        *,
        _token: object | None = None,
    ) -> None:
        """Register a private test adapter; native adapters are fixed below."""
        if _token is not _REGISTRY_TOKEN or not capability_id.startswith("test."):
            raise CapabilityExecutionError("capability_not_adopted")
        if not callable(handler):
            raise TypeError("capability handler must be callable")
        with self._lock:
            self._test_handlers[capability_id] = handler

    def execute(self, request: CapabilityExecutionRequest) -> CapabilityExecutionReceipt:
        """Execute a registered adapter through the durable effect boundary."""
        if self._journal_recovery_error is not None:
            raise CapabilityExecutionError(
                self._journal_recovery_error,
                recoverable=True,
            )
        handler = self._resolve_handler(request.capability_id)
        if handler is None:
            raise CapabilityExecutionError("capability_handler_unregistered")
        result, receipt = self._execute(request, handler)
        return CapabilityExecutionReceipt(**{**receipt.as_dict(), "result": result})

    def recover(self) -> list[CapabilityExecutionReceipt]:
        """Return unresolved effects without replaying them."""
        with self._journal_guard():
            records = self._read_records()
            return [self._receipt_from_record(record) for record in records if record.get("state") == "uncertain"]

    def journal_records(self) -> list[dict[str, Any]]:
        """Expose bounded records for local tests/operator diagnostics."""
        with self._journal_guard():
            return self._read_records()

    def recovery_status(self) -> dict[str, Any]:
        """Return an operator-safe journal/recovery projection.

        The projection deliberately contains state, bounded counts, and error
        codes only.  Request arguments, paths, and effect payloads remain
        digest-only in the journal and are never exposed by this surface.
        """
        try:
            with self._journal_guard():
                records = self._read_records()
        except (CapabilityJournalError, OSError) as exc:
            self._journal_recovery_error = "journal_recovery_required"
            if isinstance(exc, CapabilityJournalError):
                self._journal_recovery_error = _journal_error_code(exc)
            return {
                "status": "blocked",
                "recovery_required": True,
                "error_code": self._journal_recovery_error,
                "journal_version": _JOURNAL_VERSION,
                "uncertain_count": 0,
                "record_count": 0,
                "records": [],
            }
        uncertain = [record for record in records if record.get("state") == "uncertain"]
        return {
            "status": "blocked" if self._journal_recovery_error or uncertain else "ready",
            "recovery_required": bool(self._journal_recovery_error or uncertain),
            "error_code": self._journal_recovery_error,
            "journal_version": _JOURNAL_VERSION,
            "uncertain_count": len(uncertain),
            "record_count": len(records),
            "records": [
                {
                    "effect_id": str(record.get("effect_id") or ""),
                    "state": str(record.get("state") or "unknown"),
                    "recoverable": bool(record.get("recoverable")),
                    "replay_blocked": bool(record.get("replay_blocked")),
                    "error_code": record.get("error_code") if isinstance(record.get("error_code"), str) else None,
                    "started_at": record.get("started_at"),
                    "completed_at": record.get("completed_at"),
                }
                for record in records
            ],
        }

    def _resolve_handler(self, capability_id: str) -> CapabilityHandler | None:
        if capability_id.startswith("test."):
            return self._test_handlers.get(capability_id)
        if capability_id not in _ADOPTED_CAPABILITIES:
            return None
        return _native_adapter_registry().get(capability_id)

    def _execute_adopted(self, request: CapabilityExecutionRequest) -> tuple[Any, CapabilityExecutionReceipt]:
        """Invoke one fixed native adapter after the wrapper authority check."""
        if self._journal_recovery_error is not None:
            raise CapabilityExecutionError(self._journal_recovery_error, recoverable=True)
        if request.capability_id not in _ADOPTED_CAPABILITIES:
            raise CapabilityExecutionError("capability_not_adopted")
        handler = self._resolve_handler(request.capability_id)
        if handler is None:
            raise CapabilityExecutionError("capability_handler_unregistered")
        return self._execute(request, handler)

    def _execute_adopted_internal_result(
        self,
        request: CapabilityExecutionRequest,
        *,
        _token: object,
    ) -> tuple[Any, CapabilityExecutionReceipt]:
        """Return a native adapter result only to a module-owned server path."""
        if _token is not _RAW_RESULT_TOKEN:
            raise CapabilityExecutionError("raw_result_internal_only")
        if self._journal_recovery_error is not None:
            raise CapabilityExecutionError(self._journal_recovery_error, recoverable=True)
        if request.capability_id not in _ADOPTED_CAPABILITIES:
            raise CapabilityExecutionError("capability_not_adopted")
        handler = self._resolve_handler(request.capability_id)
        if handler is None:
            raise CapabilityExecutionError("capability_handler_unregistered")
        token = _INTERNAL_RAW_RESULT.set(_token)
        try:
            return self._execute(request, handler, return_raw_result=True)
        finally:
            _INTERNAL_RAW_RESULT.reset(token)

    @contextmanager
    def _journal_guard(self):
        """Serialize journal read/claim/write operations across processes."""
        with self._lock:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.journal_path.with_name(self.journal_path.name + ".lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _execute(
        self,
        request: CapabilityExecutionRequest,
        handler: CapabilityHandler,
        *,
        return_raw_result: bool = False,
    ) -> tuple[Any, CapabilityExecutionReceipt]:
        now = time.time()
        self._validate_authority(request, now=now)
        if request.duplicate_key in self._uncertain_keys:
            raise CapabilityExecutionError("effect_uncertain", recoverable=True)
        with self._journal_guard():
            records = self._read_records()
            existing = next((item for item in records if item.get("duplicate_key") == request.duplicate_key), None)
            if existing is not None:
                if existing.get("binding") != request.journal_binding():
                    raise CapabilityExecutionError("duplicate_key_conflict")
                state = str(existing.get("state"))
                if state == "succeeded":
                    replayed = self._result_from_record(existing)
                    persisted_receipt = self._receipt_from_record(existing)
                    return replayed, CapabilityExecutionReceipt(
                        **{**persisted_receipt.as_dict(), "result": replayed}
                    )
                if state == "failed":
                    raise CapabilityExecutionError("effect_failed", recoverable=True)
                raise CapabilityExecutionError("effect_uncertain", recoverable=True)

            started_at = time.time()
            record = {
                "journal_version": _JOURNAL_VERSION,
                "duplicate_key": request.duplicate_key,
                "effect_id": f"effect:{request.effect_digest}",
                "binding": request.journal_binding(),
                "owner_session_digest": _digest({"session_id": request.session_id}) if request.session_id else "",
                "job_digest": _digest({"job_id": request.job_id}) if request.job_id else "",
                "fencing_token_digest": _digest({"fencing_token": request.fencing_token}) if request.fencing_token else "",
                "policy_version": request.policy_version,
                "limits_digest": request.limits.digest(),
                # Keep the durable record digest-only. Redacted argument
                # shapes belong in short-lived audit projections; retaining
                # even field names here can disclose credential conventions.
                "arguments_digest": request.arguments_digest,
                "state": "prepared",
                "recoverable": True,
                "replay_blocked": False,
                "started_at": started_at,
                "completed_at": None,
            }
            records.append(record)
            self._write_records_unlocked(records)
            record["state"] = "started"
            self._write_records_unlocked(records)

        try:
            result = handler(request.arguments)
        except Exception as exc:
            # Ordinary effect failures are durable and retryable after the
            # caller has repaired the input/environment. Keep the error code
            # type-only so exception text cannot become a secret sink.
            completed_at = time.time()
            with self._journal_guard():
                records = self._read_records()
                current = next(
                    (item for item in records if item.get("duplicate_key") == request.duplicate_key),
                    None,
                )
                if current is None:
                    raise CapabilityJournalError("execution record disappeared before failure") from exc
                current.update(
                    {
                        "state": "failed",
                        "recoverable": True,
                        "replay_blocked": False,
                        "error_code": type(exc).__name__,
                        "completed_at": completed_at,
                    }
                )
                self._write_records_unlocked(records)
            return None, CapabilityExecutionReceipt(
                effect_id=str(record["effect_id"]),
                duplicate_key=request.duplicate_key,
                request_digest=request.request_digest,
                effect_digest=request.effect_digest,
                state="failed",
                recoverable=True,
                replay_blocked=False,
                error_code=type(exc).__name__,
                started_at=started_at,
                completed_at=completed_at,
            )
        except BaseException:
            # A process crash or cancellation after the started marker is
            # recoverable, but the host must never silently retry the effect.
            with self._journal_guard():
                records = self._read_records()
                current = next(
                    (item for item in records if item.get("duplicate_key") == request.duplicate_key),
                    None,
                )
                if current is not None:
                    current["state"] = "uncertain"
                    current["replay_blocked"] = True
                    current["error_code"] = "handler_interrupted"
                    current["completed_at"] = time.time()
                    self._write_records_unlocked(records)
            raise

        try:
            bounded_result, truncated = _bounded_json(result, limit=request.limits.output_bytes)
            summary = _safe_summary(result, output_bytes=request.limits.output_bytes)
            # Digest the canonical result incrementally; the raw-result seam
            # must not first build an unbounded JSON byte string.
            _, output_digest, _ = _json_stats(result)
        except Exception as exc:
            self._uncertain_keys.add(request.duplicate_key)
            self._mark_uncertain(request.duplicate_key, error_code="receipt_serialization_failed")
            raise CapabilityExecutionError("effect_uncertain", recoverable=True) from exc
        completed_at = time.time()
        try:
            with self._journal_guard():
                records = self._read_records()
                current = next(
                    (item for item in records if item.get("duplicate_key") == request.duplicate_key),
                    None,
                )
                if current is None:
                    raise CapabilityJournalError("execution record disappeared before completion")
                current.update(
                    {
                        "state": "succeeded",
                        "recoverable": False,
                        "replay_blocked": False,
                        "output_digest": output_digest,
                        "output_bytes": summary["output_bytes"],
                        "output_truncated": truncated,
                        "output_summary": {
                            "type": summary["type"],
                            "output_sha256": summary["output_sha256"],
                            "output_bytes": summary["output_bytes"],
                            "output_truncated": summary["output_truncated"],
                        },
                        "completed_at": completed_at,
                    }
                )
                self._write_records_unlocked(records)
        except Exception as exc:
            # The handler may already have produced a real effect. A receipt
            # write failure therefore becomes an uncertain effect and is
            # denied on every retry, including in this process.
            self._uncertain_keys.add(request.duplicate_key)
            self._mark_uncertain(request.duplicate_key, error_code="receipt_persistence_failed")
            raise CapabilityExecutionError("effect_uncertain", recoverable=True) from exc
        receipt = CapabilityExecutionReceipt(
            effect_id=str(record["effect_id"]),
            duplicate_key=request.duplicate_key,
            request_digest=request.request_digest,
            effect_digest=request.effect_digest,
            state="succeeded",
            recoverable=False,
            replay_blocked=False,
            output_digest=output_digest,
            output_bytes=summary["output_bytes"],
            output_truncated=truncated,
            output_summary=summary,
            started_at=started_at,
            completed_at=completed_at,
            result=bounded_result,
        )
        if return_raw_result:
            # The internal native seam may preserve structured fields, but it
            # must still receive the same bounded value as every other caller.
            # Returning the handler's raw object here would bypass the output
            # limit and let an adapter cross the host boundary with an
            # unbounded result.
            return bounded_result, CapabilityExecutionReceipt(
                **{**receipt.as_dict(), "result": bounded_result}
            )
        return bounded_result, receipt

    def _validate_authority(self, request: CapabilityExecutionRequest, *, now: float) -> None:
        """Resolve authority from the authenticated runtime boundary.

        ``CapabilityExecutionRequest`` carries effect identity, not an
        authorization assertion.  In particular, no caller-provided boolean
        can turn an unauthenticated or revoked principal into an authorized
        effect.  The wrapper performs an early check for useful errors; this
        host repeats it immediately before journal claim and dispatch.
        """
        from src.approval.runtime import (
            get_current_fencing_token,
            get_current_session_id,
            get_current_trust_principal,
        )
        from src.auth.cancellation import assert_runtime_not_revoked
        from src.security.trust_contract import evaluate_trust
        from src.tools.approval import _capability_authority_request

        if request.expires_at is not None and now >= request.expires_at:
            raise CapabilityExecutionError("authority_expired")
        principal = get_current_trust_principal()
        runtime_session_id = get_current_session_id()
        if principal is None or not runtime_session_id:
            raise CapabilityExecutionError("runtime_authority_missing")
        if not request.owner_principal_id.strip():
            raise CapabilityExecutionError("principal_missing")
        if request.owner_principal_id != str(principal.principal_id):
            raise CapabilityExecutionError("owner_principal_mismatch")
        if request.session_id != runtime_session_id or principal.session_id != runtime_session_id:
            raise CapabilityExecutionError("session_identity_mismatch")
        if request.job_id != str(principal.job_id or ""):
            raise CapabilityExecutionError("job_identity_mismatch")
        assert_runtime_not_revoked()

        authority_request = _capability_authority_request(
            session_id=runtime_session_id,
            principal=principal,
            tool_name=request.capability_id,
            arguments=dict(request.arguments),
        )
        decision = evaluate_trust(authority_request, now=now)
        if not decision.allowed:
            reason = {
                "principal_unauthorized": "principal_unauthenticated",
                "authority_grant_missing": "capability_grant_missing",
            }.get(decision.reason_code, decision.reason_code)
            raise CapabilityExecutionError(reason)

        runtime_fencing_token = str(get_current_fencing_token() or "")
        if request.job_id:
            if not request.fencing_token or not runtime_fencing_token:
                raise CapabilityExecutionError("fencing_token_missing")
            if request.fencing_token != runtime_fencing_token:
                raise CapabilityExecutionError("fencing_token_mismatch")
        elif request.fencing_token:
            raise CapabilityExecutionError("fencing_token_unbound")

        if request.approval_id or request.approval_digest or request.approval_binding is not None:
            binding = request.approval_binding
            if not isinstance(binding, Mapping):
                raise CapabilityExecutionError("approval_binding_missing")
            if not isinstance(binding.get("binding_mac"), str) or not isinstance(
                binding.get("receipt_token"), str
            ):
                raise CapabilityExecutionError("approval_binding_missing")
            from src.approval.repository import fingerprint_tool_call

            approval_context = binding.get("approval_context")
            expected_approval_digest = fingerprint_tool_call(
                request.capability_id,
                dict(request.arguments),
                approval_context=(
                    dict(approval_context)
                    if isinstance(approval_context, Mapping)
                    else None
                ),
            )
            if (
                str(binding.get("approval_id") or "") != request.approval_id
                or str(binding.get("status") or "") != "consumed"
                or str(binding.get("session_id") or "") != request.session_id
                or str(binding.get("tool_name") or "") != request.capability_id
                or str(binding.get("fingerprint") or "") != expected_approval_digest
                or request.approval_digest != expected_approval_digest
            ):
                raise CapabilityExecutionError("approval_binding_mismatch")
            owner_session = str(binding.get("owner_operator_session_id") or "")
            principal_operator_session = str(getattr(principal, "operator_session_id", "") or "")
            if not principal_operator_session:
                principal_type = getattr(principal, "principal_type", "")
                principal_type = str(getattr(principal_type, "value", principal_type))
                if principal_type == "operator" and principal.session_id == request.session_id:
                    # Compatibility for pre-auth-session rows whose operator
                    # principal was explicitly bound to the same session.
                    principal_operator_session = request.session_id
            if owner_session and owner_session != principal_operator_session:
                raise CapabilityExecutionError("approval_owner_mismatch")
            approval_expires_at = binding.get("approval_expires_at")
            if approval_expires_at is not None:
                try:
                    if now >= float(approval_expires_at):
                        raise CapabilityExecutionError("approval_expired")
                except (TypeError, ValueError, OverflowError) as exc:
                    raise CapabilityExecutionError("approval_expiry_invalid") from exc
            from src.approval.runtime import _consume_capability_approval

            if not _consume_capability_approval(binding):
                raise CapabilityExecutionError("approval_binding_missing")

    def _find_record(self, duplicate_key: str) -> dict[str, Any] | None:
        for record in self._read_records():
            if record.get("duplicate_key") == duplicate_key:
                return record
        return None

    def _recover_incomplete(self) -> None:
        with self._journal_guard():
            records = self._read_records()
            changed = False
            for record in records:
                if record.get("state") in {"prepared", "started"}:
                    record["state"] = "uncertain"
                    record["recoverable"] = True
                    record["replay_blocked"] = True
                    record["error_code"] = "restart_reconciliation_required"
                    record["completed_at"] = time.time()
                    changed = True
                    self._uncertain_keys.add(str(record.get("duplicate_key", "")))
            if changed:
                self._write_records_unlocked(records)

    def _mark_uncertain(self, duplicate_key: str, *, error_code: str) -> None:
        """Best-effort durable uncertainty marker after an effect boundary error."""
        try:
            with self._journal_guard():
                records = self._read_records()
                current = next((item for item in records if item.get("duplicate_key") == duplicate_key), None)
                if current is None:
                    return
                current.update(
                    {
                        "state": "uncertain",
                        "recoverable": True,
                        "replay_blocked": True,
                        "error_code": error_code,
                        "completed_at": time.time(),
                    }
                )
                self._write_records_unlocked(records)
        except Exception:
            # The in-memory deny set remains authoritative for this process;
            # a failed durable write also blocks every subsequent attempt and
            # is visible through the operator recovery surface.
            self._journal_recovery_error = "journal_recovery_required"
            return

    def _read_records(self) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        try:
            if self.journal_path.stat().st_size > self._max_bytes:
                raise CapabilityJournalError("execution journal exceeds configured bounds")
            payload = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CapabilityJournalError("execution journal is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("journal_version") != _JOURNAL_VERSION:
            raise CapabilityJournalError("execution journal version is unsupported")
        records = payload.get("records")
        if not isinstance(records, list) or len(records) > self._max_records or any(
            not isinstance(item, dict) for item in records
        ):
            raise CapabilityJournalError("execution journal records are invalid")
        core = {"journal_version": payload["journal_version"], "records": records}
        journal_mac = payload.get("journal_mac")
        if not isinstance(journal_mac, str) or not hmac.compare_digest(
            journal_mac,
            _mac(core, key=_journal_mac_key(self.journal_path)),
        ):
            raise CapabilityJournalError("execution journal integrity check failed")
        validated: list[dict[str, Any]] = []
        duplicate_keys: set[str] = set()
        for raw_record in records:
            record = dict(raw_record)
            self._validate_record(record)
            duplicate_key = str(record["duplicate_key"])
            if duplicate_key in duplicate_keys:
                raise CapabilityJournalError("execution journal contains duplicate effect keys")
            duplicate_keys.add(duplicate_key)
            validated.append(record)
        return validated

    def _write_records(self, records: list[dict[str, Any]]) -> None:
        with self._journal_guard():
            self._write_records_unlocked(records)

    def _write_records_unlocked(self, records: list[dict[str, Any]]) -> None:
        retained = self._retain_records(records)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.journal_path.parent.chmod(0o700)
        except OSError:
            pass
        sealed_records = [self._seal_record(item) for item in retained]
        core = {"journal_version": _JOURNAL_VERSION, "records": sealed_records}
        payload = {**core, "journal_mac": _mac(core, key=_journal_mac_key(self.journal_path))}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        if len(encoded) > self._max_bytes:
            raise CapabilityJournalError("execution journal exceeds configured bounds")
        fd, temporary = tempfile.mkstemp(prefix=f".{self.journal_path.name}.", dir=str(self.journal_path.parent))
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.journal_path)
            try:
                self.journal_path.chmod(0o600)
            except OSError:
                pass
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _seal_record(record: Mapping[str, Any]) -> dict[str, Any]:
        sealed = dict(record)
        sealed["record_mac"] = _mac(
            {key: value for key, value in sealed.items() if key != "record_mac"},
            key=_effect_mac_key(),
        )
        return sealed

    def _validate_record(self, record: Mapping[str, Any]) -> None:
        required = {
            "journal_version",
            "duplicate_key",
            "effect_id",
            "binding",
            "state",
            "recoverable",
            "replay_blocked",
            "started_at",
            "completed_at",
            "record_mac",
        }
        if set(record) < required or record.get("journal_version") != _JOURNAL_VERSION:
            raise CapabilityJournalError("execution journal record schema is invalid")
        duplicate_key = record.get("duplicate_key")
        if (
            not isinstance(duplicate_key, str)
            or len(duplicate_key) != len("effect:") + _DIGEST_LENGTH
            or not duplicate_key.startswith("effect:")
        ):
            raise CapabilityJournalError("execution journal effect key is invalid")
        if record.get("state") not in _VALID_STATES:
            raise CapabilityJournalError("execution journal state is invalid")
        if not isinstance(record.get("recoverable"), bool) or not isinstance(record.get("replay_blocked"), bool):
            raise CapabilityJournalError("execution journal flags are invalid")
        binding = record.get("binding")
        if not isinstance(binding, Mapping):
            raise CapabilityJournalError("execution journal binding is invalid")
        for key in (
            "owner_principal_digest",
            "destination_digest",
            "request_digest",
            "binding_digest",
            "effect_digest",
        ):
            value = binding.get(key)
            if not isinstance(value, str) or len(value) != _DIGEST_LENGTH:
                raise CapabilityJournalError("execution journal binding digest is invalid")
        record_mac = record.get("record_mac")
        if not isinstance(record_mac, str) or not hmac.compare_digest(
            record_mac,
            _mac({key: value for key, value in record.items() if key != "record_mac"}, key=_effect_mac_key()),
        ):
            raise CapabilityJournalError("execution journal record integrity check failed")

    def _retain_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Never evict unresolved work. Completed records are dropped oldest
        # first until both bounds hold; the journal remains bounded unless the
        # unresolved set itself exceeds the configured record budget.
        unresolved = [item for item in records if item.get("state") not in _TERMINAL_STATES]
        completed = [item for item in records if item.get("state") in _TERMINAL_STATES]
        available_completed = max(0, self._max_records - len(unresolved))
        completed = completed[-available_completed:] if available_completed else []
        while completed:
            candidate = {"journal_version": _JOURNAL_VERSION, "records": unresolved + completed}
            size = len(json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode())
            if size <= self._max_bytes:
                break
            completed.pop(0)
        return unresolved + completed

    @staticmethod
    def _result_from_record(record: Mapping[str, Any]) -> Any:
        # A duplicate invocation cannot safely reconstruct the original raw
        # output from a digest-only journal. Return a bounded receipt marker so
        # callers know the effect already happened and can read its artifact.
        return {
            "effect_id": record.get("effect_id"),
            "state": record.get("state"),
            "replayed": True,
            "output_digest": record.get("output_digest", ""),
            "output_bytes": record.get("output_bytes", 0),
            "output_truncated": record.get("output_truncated", False),
        }

    @staticmethod
    def _receipt_from_record(record: Mapping[str, Any]) -> CapabilityExecutionReceipt:
        binding = record.get("binding") if isinstance(record.get("binding"), Mapping) else {}
        return CapabilityExecutionReceipt(
            effect_id=str(record.get("effect_id", "")),
            duplicate_key=str(record.get("duplicate_key", "")),
            request_digest=str(binding.get("request_digest", "")),
            effect_digest=str(binding.get("effect_digest", "")),
            state=str(record.get("state", "unknown")),
            recoverable=bool(record.get("recoverable", True)),
            replay_blocked=bool(record.get("replay_blocked", False)),
            output_digest=str(record.get("output_digest", "")),
            output_bytes=int(record.get("output_bytes", 0) or 0),
            output_truncated=bool(record.get("output_truncated", False)),
            output_summary=record.get("output_summary", {}) if isinstance(record.get("output_summary"), Mapping) else {},
            error_code=record.get("error_code") if isinstance(record.get("error_code"), str) else None,
            started_at=record.get("started_at") if isinstance(record.get("started_at"), (int, float)) else None,
            completed_at=record.get("completed_at") if isinstance(record.get("completed_at"), (int, float)) else None,
        )


capability_execution_host = CapabilityExecutionHost()
_workspace_hosts: dict[Path, CapabilityExecutionHost] = {}
_workspace_hosts_lock = threading.Lock()


def current_capability_execution_host() -> CapabilityExecutionHost:
    """Return the journal host for the currently configured workspace."""
    path = _default_journal_path()
    with _workspace_hosts_lock:
        host = _workspace_hosts.get(path)
        if host is None:
            host = CapabilityExecutionHost(journal_path=path)
            _workspace_hosts[path] = host
        return host


def build_capability_request(
    *,
    capability_id: str,
    arguments: Mapping[str, Any],
    owner_principal_id: str | None = None,
    session_id: str | None = None,
    job_id: str | None = None,
    approval_id: str = "",
    approval_digest: str = "",
    approval_binding: Mapping[str, Any] | None = None,
    idempotency_key: str = "",
    fencing_token: str | None = None,
    destination: str | None = None,
    limits: CapabilityExecutionLimits | None = None,
) -> CapabilityExecutionRequest:
    """Build a request from the current runtime authority context.

    Identity and lease values may be repeated by an internal adapter for
    lineage, but they cannot override the authenticated context.  Approval
    bindings are accepted only as repository-issued, MACed receipts and are
    checked again by the host immediately before effect dispatch.
    """
    if "__seraph_raw_result" in arguments:
        raise CapabilityExecutionError("raw_result_internal_only")
    from src.approval.runtime import (
        get_current_fencing_token,
        get_current_session_id,
        get_current_trust_principal,
    )

    principal = get_current_trust_principal()
    runtime_session_id = get_current_session_id()
    if principal is not None:
        expected_owner = str(principal.principal_id)
        if owner_principal_id is not None and owner_principal_id != expected_owner:
            raise CapabilityExecutionError("owner_principal_mismatch")
        owner_principal_id = expected_owner
        expected_session = runtime_session_id or str(principal.session_id or "")
        if session_id is not None and session_id != expected_session:
            raise CapabilityExecutionError("session_identity_mismatch")
        session_id = expected_session
        expected_job = str(principal.job_id or "")
        if job_id is not None and job_id != expected_job:
            raise CapabilityExecutionError("job_identity_mismatch")
        job_id = expected_job
    if owner_principal_id is None:
        owner_principal_id = ""
    if session_id is None:
        session_id = runtime_session_id or ""
    if job_id is None:
        job_id = ""
    runtime_fence = str(get_current_fencing_token() or "")
    if fencing_token is not None and str(fencing_token) != runtime_fence:
        raise CapabilityExecutionError("fencing_token_mismatch")
    fencing_token = runtime_fence
    if destination is None:
        if capability_id in {"read_file", "write_file", "preview_workspace_patch", "apply_workspace_patch"}:
            destination = f"workspace:{arguments.get('file_path', arguments.get('path', ''))}"
        elif capability_id in {"run_command", "start_process"}:
            destination = f"workspace-process:{arguments.get('cwd', '') or '.'}"
        else:
            destination = "seraph:process-runtime"
    return CapabilityExecutionRequest(
        owner_principal_id=owner_principal_id,
        capability_id=capability_id,
        capability_version="native-v1",
        destination=destination,
        arguments=dict(arguments),
        session_id=session_id,
        job_id=job_id,
        approval_id=approval_id,
        approval_digest=approval_digest or approval_id,
        approval_binding=approval_binding,
        idempotency_key=idempotency_key,
        fencing_token=fencing_token,
        limits=limits or CapabilityExecutionLimits(),
    )
