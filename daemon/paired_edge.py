"""Provider-free paired edge HTTP transport and durable offline spool."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx

try:
    from blocklist import DEFAULT_BLOCKLIST, is_blocked
except ImportError:  # pragma: no cover - package import fallback
    from .blocklist import DEFAULT_BLOCKLIST, is_blocked


_SPOOL_SCHEMA = "seraph.paired_edge.spool.v1"
_DEFAULT_MAX_COUNT = 64
_DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60
_DEFAULT_MAX_RETRIES = 8
_DEFAULT_BACKOFF_SECONDS = 2.0
_EDGE_UPLOAD_PATH = "/api/nodes/edge/upload"
_EDGE_HEARTBEAT_PATH = "/api/nodes/edge/heartbeat"
_EDGE_ENDPOINTS = frozenset({_EDGE_UPLOAD_PATH, _EDGE_HEARTBEAT_PATH})
_EDGE_KINDS = frozenset({"capture", "heartbeat"})
_KNOWN_RESULT_STATUSES = frozenset(
    {"accepted", "duplicate", "expired", "revoked", "oversized", "blocked", "out_of_order", "retryable"}
)
_TERMINAL_RESULT_STATUSES = frozenset(
    {"accepted", "duplicate", "expired", "revoked", "oversized", "blocked", "out_of_order"}
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_edge_origin(value: str, *, allow_insecure_test_transport: bool = False) -> str:
    """Return a safe configured origin suitable for authenticated requests."""

    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("edge_origin_must_be_http_or_https_without_credentials")
    host = parsed.hostname
    if parsed.scheme.lower() == "http":
        normalized_host = host.strip().rstrip(".").lower()
        if not (allow_insecure_test_transport and normalized_host in {"127.0.0.1", "::1", "localhost"}):
            raise ValueError("edge_origin_requires_https")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip("/"), "", ""))


def _response_outcome(http_status: int, response: dict[str, object]) -> tuple[str, str]:
    """Map both HTTP status and the typed receipt into a durable outcome."""

    status_value = response.get("status")
    status = status_value if isinstance(status_value, str) else ""
    reason_value = response.get("reason_code")
    reason = reason_value if isinstance(reason_value, str) and reason_value else ""
    detail = response.get("detail")
    if isinstance(detail, dict):
        detail_code = detail.get("code")
        if not reason and isinstance(detail_code, str) and detail_code:
            reason = detail_code
    if http_status == 401:
        return "blocked", reason or "http_401_unauthorized"
    if http_status == 403:
        if status == "revoked" or reason in {"pairing_revoked", "credential_revoked", "credential_rotated_or_revoked"}:
            return "revoked", reason or "http_403_revoked"
        return "blocked", reason or "http_403_forbidden"
    if status in _KNOWN_RESULT_STATUSES:
        return status, reason or "server_response"
    if http_status >= 500:
        return "retryable", reason or f"http_{http_status}_server_error"
    if http_status >= 400:
        return "blocked", reason or f"http_{http_status}_client_error"
    return "retryable", reason or "server_response"


@dataclass(frozen=True)
class SpoolItem:
    request_id: str
    sequence: int
    payload: dict[str, object]
    content_size: int
    created_at: str
    endpoint: str = _EDGE_UPLOAD_PATH
    kind: str = "capture"
    retries: int = 0
    next_attempt_at: str | None = None
    last_error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "sequence": self.sequence,
            "payload": self.payload,
            "content_size": self.content_size,
            "created_at": self.created_at,
            "endpoint": self.endpoint,
            "kind": self.kind,
            "retries": self.retries,
            "next_attempt_at": self.next_attempt_at,
            "last_error": self.last_error,
        }


class DurableEdgeSpool:
    """Bounded JSON spool whose request IDs make drain idempotent."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_count: int = _DEFAULT_MAX_COUNT,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        backoff_seconds: float = _DEFAULT_BACKOFF_SECONDS,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.max_count = max(1, int(max_count))
        self.max_bytes = max(1, int(max_bytes))
        self.max_age_seconds = max(1, int(max_age_seconds))
        self.max_retries = max(1, int(max_retries))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self._items: list[SpoolItem] = []
        self._discarded_count = 0
        self._load()

    @property
    def items(self) -> tuple[SpoolItem, ...]:
        return tuple(self._items)

    @property
    def count(self) -> int:
        return len(self._items)

    @property
    def bytes(self) -> int:
        return sum(item.content_size for item in self._items)

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict) or raw.get("schema") != _SPOOL_SCHEMA:
            return
        self._discarded_count = int(raw.get("discarded_count") or 0) if isinstance(raw.get("discarded_count"), int) else 0
        items = raw.get("items")
        if not isinstance(items, list):
            return
        seen: set[str] = set()
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            request_id = raw_item.get("request_id")
            sequence = raw_item.get("sequence")
            payload = raw_item.get("payload")
            size = raw_item.get("content_size")
            created_at = raw_item.get("created_at")
            if (
                not isinstance(request_id, str)
                or request_id in seen
                or not isinstance(sequence, int)
                or isinstance(sequence, bool)
                or not isinstance(payload, dict)
                or not isinstance(size, int)
                or size < 0
                or not isinstance(created_at, str)
            ):
                continue
            seen.add(request_id)
            endpoint = raw_item.get("endpoint")
            kind = raw_item.get("kind")
            if not isinstance(endpoint, str) or endpoint not in _EDGE_ENDPOINTS:
                endpoint = _EDGE_HEARTBEAT_PATH if kind == "heartbeat" else _EDGE_UPLOAD_PATH
            if not isinstance(kind, str) or kind not in _EDGE_KINDS:
                kind = "heartbeat" if endpoint == _EDGE_HEARTBEAT_PATH else "capture"
            if kind == "heartbeat":
                endpoint = _EDGE_HEARTBEAT_PATH
            else:
                endpoint = _EDGE_UPLOAD_PATH
            self._items.append(
                SpoolItem(
                    request_id=request_id,
                    sequence=sequence,
                    payload=payload,
                    content_size=size,
                    created_at=created_at,
                    endpoint=endpoint,
                    kind=kind,
                    retries=int(raw_item.get("retries") or 0) if isinstance(raw_item.get("retries"), int) else 0,
                    next_attempt_at=raw_item.get("next_attempt_at") if isinstance(raw_item.get("next_attempt_at"), str) else None,
                    last_error=raw_item.get("last_error") if isinstance(raw_item.get("last_error"), str) else None,
                )
            )
        self._items.sort(key=lambda item: (item.sequence, item.created_at, item.request_id))
        self._prune(now=_utc_now(), persist=False)

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            self.path.parent.chmod(0o700)
        payload = {
            "schema": _SPOOL_SCHEMA,
            "items": [item.as_dict() for item in self._items],
            "discarded_count": self._discarded_count,
            "updated_at": _iso(_utc_now()),
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)

    def _prune(self, *, now: datetime, persist: bool = True) -> int:
        kept: list[SpoolItem] = []
        removed = 0
        for item in self._items:
            created = _parse_timestamp(item.created_at)
            if created is None or (now - created).total_seconds() > self.max_age_seconds:
                removed += 1
                continue
            kept.append(item)
        self._items = kept
        if removed:
            self._discarded_count += removed
            if persist:
                self._persist()
        return removed

    def enqueue(
        self,
        payload: dict[str, object],
        *,
        request_id: str,
        sequence: int,
        content_size: int,
        endpoint: str = _EDGE_UPLOAD_PATH,
        kind: str = "capture",
        now: datetime | None = None,
    ) -> bool:
        """Queue one capture, returning false for duplicate/full/oversized data."""

        current = now or _utc_now()
        self._prune(now=current)
        if any(item.request_id == request_id for item in self._items):
            return False
        if content_size < 0 or content_size > self.max_bytes or self.bytes + content_size > self.max_bytes:
            return False
        if len(self._items) >= self.max_count:
            return False
        if not isinstance(endpoint, str) or endpoint not in _EDGE_ENDPOINTS:
            endpoint = _EDGE_HEARTBEAT_PATH if kind == "heartbeat" else _EDGE_UPLOAD_PATH
        if not isinstance(kind, str) or kind not in _EDGE_KINDS:
            kind = "heartbeat" if endpoint == _EDGE_HEARTBEAT_PATH else "capture"
        if kind == "heartbeat":
            endpoint = _EDGE_HEARTBEAT_PATH
        else:
            endpoint = _EDGE_UPLOAD_PATH
        self._items.append(
            SpoolItem(
                request_id=request_id,
                sequence=sequence,
                payload=dict(payload),
                content_size=content_size,
                created_at=_iso(current),
                endpoint=endpoint,
                kind=kind,
            )
        )
        self._items.sort(key=lambda item: (item.sequence, item.created_at, item.request_id))
        self._persist()
        return True

    def ready(self, *, now: datetime | None = None) -> list[SpoolItem]:
        current = now or _utc_now()
        self._prune(now=current)
        ready: list[SpoolItem] = []
        for item in self._items:
            next_attempt = _parse_timestamp(item.next_attempt_at) if item.next_attempt_at else None
            if next_attempt is None or current >= next_attempt:
                ready.append(item)
        return sorted(ready, key=lambda item: (item.sequence, item.created_at, item.request_id))

    def acknowledge(self, request_id: str) -> bool:
        before = len(self._items)
        self._items = [item for item in self._items if item.request_id != request_id]
        if len(self._items) == before:
            return False
        self._persist()
        return True

    def retry(self, request_id: str, *, error: str, now: datetime | None = None) -> bool:
        current = now or _utc_now()
        for index, item in enumerate(self._items):
            if item.request_id != request_id:
                continue
            retries = item.retries + 1
            if retries > self.max_retries:
                self._items.pop(index)
                self._discarded_count += 1
                self._persist()
                return False
            delay = self.backoff_seconds * (2 ** min(retries - 1, 8))
            self._items[index] = SpoolItem(
                request_id=item.request_id,
                sequence=item.sequence,
                payload=item.payload,
                content_size=item.content_size,
                created_at=item.created_at,
                endpoint=item.endpoint,
                kind=item.kind,
                retries=retries,
                next_attempt_at=_iso(current if delay <= 0 else current + timedelta(seconds=delay)),
                last_error=str(error)[:160],
            )
            self._persist()
            return True
        return False

    def status(self, *, now: datetime | None = None) -> dict[str, object]:
        current = now or _utc_now()
        oldest = min((item.created_at for item in self._items), default=None)
        max_retries = max((item.retries for item in self._items), default=0)
        return {
            "count": self.count,
            "bytes": self.bytes,
            "oldest_at": oldest,
            "max_retries": max_retries,
            "discarded_count": self._discarded_count,
            "max_count": self.max_count,
            "max_bytes": self.max_bytes,
            "max_age_seconds": self.max_age_seconds,
            "max_retries_allowed": self.max_retries,
            "ready_count": len(self.ready(now=current)),
            "state": "queued" if self._items else "empty",
        }


@dataclass(frozen=True)
class EdgeTransportResult:
    status: str
    reason_code: str
    request_id: str
    sequence: int
    response: dict[str, object] | None = None
    queued: bool = False
    artifact_id: str | None = None
    http_status: int | None = None


class PairedEdgeTransport:
    """Authenticated daemon transport for local core heartbeat and uploads."""

    def __init__(
        self,
        *,
        origin: str,
        credential: str | None,
        device_id: str,
        pairing_id: str,
        spool_path: str | os.PathLike[str],
        extension_id: str = "seraph.openclaw-device-bridge",
        reference: str = "connectors/nodes/device.yaml",
        policy_version: str = "node-pairing-policy.v1",
        capability_scope: str = "media.ingest",
        data_purpose: str = "screen_capture",
        blocklist: set[str] | None = None,
        cloud_upload_enabled: bool = False,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        allow_insecure_test_transport: bool = False,
    ) -> None:
        raw_credential = str(credential or "").strip()
        allow_insecure = bool(allow_insecure_test_transport and not raw_credential)
        self.origin = validate_edge_origin(origin, allow_insecure_test_transport=allow_insecure)
        parsed_origin = urlsplit(self.origin)
        if not raw_credential and not (allow_insecure and parsed_origin.scheme == "http"):
            raise ValueError("paired edge credential is required")
        self.credential = raw_credential or None
        self.device_id = device_id
        self.pairing_id = pairing_id
        self.extension_id = extension_id
        self.reference = reference
        self.policy_version = policy_version
        self.capability_scope = capability_scope
        self.data_purpose = data_purpose
        self.blocklist = set(DEFAULT_BLOCKLIST if blocklist is None else blocklist)
        self.cloud_upload_enabled = bool(cloud_upload_enabled)
        self.timeout_seconds = timeout_seconds
        self.spool = DurableEdgeSpool(spool_path)
        self._sequence_path = self.spool.path.with_name(f"{self.spool.path.stem}.sequence.json")
        self._sequence = self._load_sequence()
        self._client = http_client
        self._owns_client = http_client is None
        self._lock = asyncio.Lock()
        self.last_result: EdgeTransportResult | None = None

    def _load_sequence(self) -> int:
        try:
            payload = json.loads(self._sequence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        value = payload.get("next_sequence") if isinstance(payload, dict) else None
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def _next_sequence(self) -> int:
        self._sequence += 1
        self._sequence_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=f".{self._sequence_path.name}.", dir=self._sequence_path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"schema": "seraph.paired_edge.sequence.v1", "next_sequence": self._sequence}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._sequence_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
        return self._sequence

    async def __aenter__(self) -> "PairedEdgeTransport":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
        self._client = None if self._owns_client else self._client

    def _payload(
        self,
        content: bytes,
        *,
        sequence: int,
        request_id: str,
        captured_at: datetime,
        app: str | None = None,
        window_title: str | None = None,
        observation: dict[str, object] | None = None,
    ) -> dict[str, object]:
        spool_status = self.spool.status()
        return {
            "extension_id": self.extension_id,
            "reference": self.reference,
            "device_id": self.device_id,
            "pairing_id": self.pairing_id,
            "request_id": request_id,
            "sequence": sequence,
            "captured_at": _iso(captured_at),
            "content_hash": f"sha256:{hashlib.sha256(content).hexdigest()}",
            "media_type": "image/png" if content else "application/json",
            "content_size": len(content),
            "policy_version": self.policy_version,
            "capability_scope": self.capability_scope,
            "data_purpose": self.data_purpose,
            "content_base64": base64.b64encode(content).decode("ascii") if content else None,
            "action_authority": False,
            "app": app,
            "window_title": window_title,
            "observation": observation,
            "spool_count": int(spool_status["count"]),
            "spool_bytes": int(spool_status["bytes"]),
            "spool_oldest_at": spool_status["oldest_at"],
            "recovery_state": "queued" if int(spool_status["count"]) else "healthy",
            "degraded_state": None,
        }

    async def _post(self, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        if self._client is None:
            await self.__aenter__()
        assert self._client is not None
        headers = {
            "Origin": self.origin,
            "X-Seraph-Node-Device": self.device_id,
        }
        if self.credential:
            headers["Authorization"] = f"Bearer {self.credential}"
        response = await self._client.post(
            f"{self.origin}{path}",
            json=payload,
            headers=headers,
        )
        try:
            data = response.json()
        except ValueError:
            data = {"status": "retryable", "reason_code": "invalid_server_response"}
        return response.status_code, data if isinstance(data, dict) else {}

    async def _send_or_queue(
        self,
        payload: dict[str, object],
        *,
        path: str,
        content_size: int,
        kind: str,
    ) -> EdgeTransportResult:
        request_id = str(payload["request_id"])
        sequence = int(payload["sequence"])
        try:
            http_status, response = await self._post(path, payload)
            status, reason = _response_outcome(http_status, response)
            artifact = response.get("artifact")
            artifact_id = str(artifact.get("artifact_id")) if isinstance(artifact, dict) and artifact.get("artifact_id") else None
            queued = False
            if status == "retryable":
                queued = self.spool.enqueue(
                    payload,
                    request_id=request_id,
                    sequence=sequence,
                    content_size=content_size,
                    endpoint=path,
                    kind=kind,
                )
            result = EdgeTransportResult(
                status=status,
                reason_code=reason,
                request_id=request_id,
                sequence=sequence,
                response=response,
                queued=queued,
                artifact_id=artifact_id,
                http_status=http_status,
            )
            self.last_result = result
            return result
        except (httpx.HTTPError, OSError, ValueError) as exc:
            queued = self.spool.enqueue(
                payload,
                request_id=request_id,
                sequence=sequence,
                content_size=content_size,
                endpoint=path,
                kind=kind,
            )
            result = EdgeTransportResult(
                status="retryable" if queued else "blocked",
                reason_code="backend_unreachable" if queued else "spool_full",
                request_id=request_id,
                sequence=sequence,
                queued=queued,
                http_status=None,
            )
            self.last_result = result
            return result

    async def heartbeat(self, *, captured_at: datetime | None = None) -> EdgeTransportResult:
        async with self._lock:
            sequence = self._next_sequence()
            payload = self._payload(
                b"",
                sequence=sequence,
                request_id=f"heartbeat-{uuid4().hex}",
                captured_at=captured_at or _utc_now(),
            )
            if self.spool.count:
                queued = self.spool.enqueue(
                    payload,
                    request_id=str(payload["request_id"]),
                    sequence=sequence,
                    content_size=0,
                    endpoint=_EDGE_HEARTBEAT_PATH,
                    kind="heartbeat",
                )
                result = EdgeTransportResult(
                    status="retryable" if queued else "blocked",
                    reason_code="queued_behind_pending_items" if queued else "spool_full",
                    request_id=str(payload["request_id"]),
                    sequence=sequence,
                    queued=queued,
                )
                self.last_result = result
                return result
            return await self._send_or_queue(
                payload,
                path=_EDGE_HEARTBEAT_PATH,
                content_size=0,
                kind="heartbeat",
            )

    async def capture(
        self,
        content: bytes,
        *,
        app: str,
        window_title: str = "",
        observation: dict[str, object] | None = None,
        captured_at: datetime | None = None,
    ) -> EdgeTransportResult:
        if is_blocked(app, self.blocklist):
            result = EdgeTransportResult(
                status="blocked",
                reason_code="sensitive_app_blocked_before_upload",
                request_id="",
                sequence=self._sequence,
            )
            self.last_result = result
            return result
        if self.cloud_upload_enabled:
            # This adapter only posts to the configured Seraph origin. Cloud
            # and model egress require a separate governed setting and are not
            # enabled by changing the edge transport configuration.
            result = EdgeTransportResult(
                status="blocked",
                reason_code="cloud_egress_disabled_for_edge_capture",
                request_id="",
                sequence=self._sequence,
            )
            self.last_result = result
            return result
        async with self._lock:
            sequence = self._next_sequence()
            payload = self._payload(
                content,
                sequence=sequence,
                request_id=f"capture-{uuid4().hex}",
                captured_at=captured_at or _utc_now(),
                app=app,
                window_title=window_title,
                observation=observation,
            )
            return await self._send_or_queue(
                payload,
                path=_EDGE_UPLOAD_PATH,
                content_size=len(content),
                kind="capture",
            )

    async def drain(self) -> list[EdgeTransportResult]:
        """Drain oldest sequence first; server request IDs provide dedupe."""

        results: list[EdgeTransportResult] = []
        async with self._lock:
            for item in list(self.spool.ready()):
                try:
                    http_status, response = await self._post(item.endpoint, item.payload)
                    status, reason = _response_outcome(http_status, response)
                    artifact = response.get("artifact")
                    artifact_id = str(artifact.get("artifact_id")) if isinstance(artifact, dict) and artifact.get("artifact_id") else None
                    result = EdgeTransportResult(
                        status=status,
                        reason_code=reason,
                        request_id=item.request_id,
                        sequence=item.sequence,
                        response=response,
                        artifact_id=artifact_id,
                        http_status=http_status,
                    )
                    if status in _TERMINAL_RESULT_STATUSES:
                        self.spool.acknowledge(item.request_id)
                    else:
                        self.spool.retry(item.request_id, error=reason)
                    results.append(result)
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    self.spool.retry(item.request_id, error="backend_unreachable")
                    results.append(
                        EdgeTransportResult(
                            status="retryable",
                            reason_code="backend_unreachable",
                            request_id=item.request_id,
                            sequence=item.sequence,
                            queued=True,
                        )
                    )
        if results:
            self.last_result = results[-1]
        return results

    def status(self) -> dict[str, object]:
        status = self.spool.status()
        status.update(
            {
                "device_id": self.device_id,
                "pairing_id": self.pairing_id,
                "transport": "authenticated_local_http",
                "cloud_upload_enabled": self.cloud_upload_enabled,
                "action_authority": False,
                "last_status": self.last_result.status if self.last_result else None,
                "last_reason_code": self.last_result.reason_code if self.last_result else None,
            }
        )
        return status


__all__ = [
    "DurableEdgeSpool",
    "EdgeTransportResult",
    "PairedEdgeTransport",
    "SpoolItem",
    "validate_edge_origin",
]
