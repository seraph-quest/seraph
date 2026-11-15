"""Authenticated local capture upload to the Seraph backend (ADR-035, #1147).

The daemon makes no cloud call. Allowed captures go only to the configured
loopback Seraph backend at ``POST /api/observer/captures``, authenticated with
the scoped paired-edge credential (the same credential the paired edge
transport presents). The backend runs governed vision analysis; the daemon
never contacts a model provider.

Bounds: blocklist checked before upload, client-side size cap, at most two
attempts per capture, and a doubling cool-down after failures so an offline or
rate-limited backend is never hammered. There is no poll loop here.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

try:
    from blocklist import is_blocked
except ImportError:  # pragma: no cover - package import fallback
    from .blocklist import is_blocked

CAPTURE_UPLOAD_PATH = "/api/observer/captures"
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_LOCAL_TEXT_CHARS = 8 * 1024
_LOCAL_PROVIDERS = frozenset({"apple-vision"})
DEFAULT_EXTENSION_ID = "seraph.openclaw-device-bridge"
DEFAULT_REFERENCE = "connectors/nodes/device.yaml"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_MAX_ATTEMPTS = 2
_RETRY_DELAY_SECONDS = 0.5
_COOLDOWN_BASE_SECONDS = 30.0
_COOLDOWN_MAX_SECONDS = 600.0


def validate_local_backend_origin(url: str) -> str:
    """Return ``scheme://host[:port]`` for a loopback backend, else raise.

    Uploads carry raw screen pixels; they may only go to this Mac's backend.
    """
    parsed = urlsplit(str(url or "").strip())
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if parsed.scheme not in {"http", "https"} or host not in _LOOPBACK_HOSTS:
        raise ValueError("capture_upload_requires_loopback_backend")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("capture_upload_backend_url_invalid")
    netloc = f"[{host}]" if ":" in host else host
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return f"{parsed.scheme}://{netloc}"


def _read_credential(credential: str | None, credential_file: str | None) -> str | None:
    if credential and credential.strip():
        return credential.strip()
    if credential_file:
        path = Path(credential_file).expanduser()
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value or None
    return None


def _bounded_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def bounded_local_analysis(analysis: dict[str, object] | None) -> dict[str, object] | None:
    """Shape on-device analysis to the backend's bounded upload schema."""
    if not isinstance(analysis, dict):
        return None
    provider = analysis.get("provider")
    if provider not in _LOCAL_PROVIDERS:
        return None
    result: dict[str, object] = {"provider": provider}
    for key, limit in (("activity", 64), ("project", 256), ("summary", 512)):
        text = _bounded_text(analysis.get(key), limit)
        if text:
            result[key] = text
    text = _bounded_text(analysis.get("text"), MAX_LOCAL_TEXT_CHARS)
    if text:
        result["text"] = text
    details = analysis.get("details")
    if isinstance(details, list):
        items = [item.strip()[:256] for item in details if isinstance(item, str) and item.strip()][:16]
        if items:
            result["details"] = items
    return result if len(result) > 1 else None


@dataclass(frozen=True)
class CaptureUploadResult:
    status: str  # accepted | duplicate | suppressed | blocked | skipped | failed
    reason: str
    capture_id: str = ""
    observation_id: str | None = None
    http_status: int | None = None

    @property
    def stored(self) -> bool:
        """True when the backend holds this capture (new or already known)."""
        return self.status in {"accepted", "duplicate", "suppressed"}


class LocalCaptureUploader:
    """Uploads allowed captures to the loopback backend; never to the cloud."""

    def __init__(
        self,
        *,
        backend_url: str,
        credential: str | None,
        device_id: str,
        pairing_id: str,
        extension_id: str = DEFAULT_EXTENSION_ID,
        reference: str = DEFAULT_REFERENCE,
        sleep=asyncio.sleep,
        clock=time.monotonic,
    ) -> None:
        self.origin = validate_local_backend_origin(backend_url)
        if not credential:
            raise ValueError("capture_upload_credential_required")
        if not device_id or not pairing_id:
            raise ValueError("capture_upload_pairing_identity_required")
        self._credential = credential
        self.device_id = device_id
        self.pairing_id = pairing_id
        self.extension_id = extension_id
        self.reference = reference
        self._sleep = sleep
        self._clock = clock
        self._cooldown_until = 0.0
        self._consecutive_failures = 0
        self.last_result: CaptureUploadResult | None = None

    @classmethod
    def from_environment(cls, *, backend_url: str, args: object | None = None) -> "LocalCaptureUploader | None":
        """Build from CLI/env; ``None`` when upload is not configured.

        The credential is read from ``SERAPH_EDGE_CREDENTIAL`` or a private
        file (``--edge-credential-file`` / ``SERAPH_EDGE_CREDENTIAL_FILE``),
        never from a CLI flag that would show in the process list.
        """
        device_id = getattr(args, "edge_device_id", None) or os.environ.get("SERAPH_EDGE_DEVICE_ID", "")
        pairing_id = getattr(args, "edge_pairing_id", None) or os.environ.get("SERAPH_EDGE_PAIRING_ID", "")
        credential_file = getattr(args, "edge_credential_file", None) or os.environ.get("SERAPH_EDGE_CREDENTIAL_FILE")
        credential = _read_credential(os.environ.get("SERAPH_EDGE_CREDENTIAL"), credential_file)
        if not (credential and device_id and pairing_id):
            return None
        return cls(
            backend_url=backend_url,
            credential=credential,
            device_id=device_id.strip(),
            pairing_id=pairing_id.strip(),
            extension_id=os.environ.get("SERAPH_EDGE_EXTENSION_ID", "").strip() or DEFAULT_EXTENSION_ID,
            reference=os.environ.get("SERAPH_EDGE_REFERENCE", "").strip() or DEFAULT_REFERENCE,
        )

    def _headers(self) -> dict[str, str]:
        # No Origin and no cookie: the backend accepts this native ingress on
        # the bearer credential alone (a browser always sends Origin).
        return {
            "Authorization": f"Bearer {self._credential}",
            "X-Seraph-Node-Device": self.device_id,
            "X-Seraph-Node-Pairing": self.pairing_id,
            "X-Seraph-Node-Extension": self.extension_id,
            "X-Seraph-Node-Reference": self.reference,
        }

    def _record_failure(self, result: CaptureUploadResult, *, retry_after: float | None = None) -> CaptureUploadResult:
        self._consecutive_failures += 1
        backoff = min(_COOLDOWN_MAX_SECONDS, _COOLDOWN_BASE_SECONDS * (2 ** (self._consecutive_failures - 1)))
        if retry_after is not None:
            backoff = max(backoff, min(_COOLDOWN_MAX_SECONDS, retry_after))
        self._cooldown_until = self._clock() + backoff
        self.last_result = result
        return result

    def _record_success(self, result: CaptureUploadResult) -> CaptureUploadResult:
        self._consecutive_failures = 0
        self._cooldown_until = 0.0
        self.last_result = result
        return result

    async def upload(
        self,
        client: httpx.AsyncClient,
        png_bytes: bytes,
        *,
        app_name: str,
        window_title: str,
        blocklist: set[str],
        captured_at: datetime | None = None,
        local_analysis: dict[str, object] | None = None,
    ) -> CaptureUploadResult:
        if is_blocked(app_name, blocklist):
            result = CaptureUploadResult(status="blocked", reason="sensitive_app_blocked_before_upload")
            self.last_result = result
            return result
        if not png_bytes:
            return CaptureUploadResult(status="skipped", reason="empty_capture")
        if len(png_bytes) > MAX_UPLOAD_BYTES:
            result = CaptureUploadResult(status="skipped", reason="capture_too_large")
            self.last_result = result
            return result
        if self._clock() < self._cooldown_until:
            return CaptureUploadResult(status="skipped", reason="upload_cooldown")

        capture_id = f"cap-{uuid4().hex}"
        when = (captured_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        payload: dict[str, object] = {
            "capture_id": capture_id,
            "app_name": app_name,
            "window_title": window_title or "",
            "captured_at": when.isoformat(),
            "content_base64": base64.b64encode(png_bytes).decode("ascii"),
        }
        bounded = bounded_local_analysis(local_analysis)
        if bounded:
            payload["local_analysis"] = bounded

        last: CaptureUploadResult | None = None
        retry_after: float | None = None
        for attempt in range(_MAX_ATTEMPTS):
            if attempt:
                await self._sleep(_RETRY_DELAY_SECONDS)
            try:
                response = await client.post(
                    f"{self.origin}{CAPTURE_UPLOAD_PATH}",
                    json=payload,
                    headers=self._headers(),
                )
            except httpx.HTTPError:
                last = CaptureUploadResult(status="failed", reason="backend_unreachable", capture_id=capture_id)
                continue
            try:
                data = response.json()
            except ValueError:
                data = {}
            data = data if isinstance(data, dict) else {}
            detail = data.get("detail")
            code = str(detail.get("code")) if isinstance(detail, dict) and detail.get("code") else ""
            if 200 <= response.status_code < 300:
                return self._record_success(
                    CaptureUploadResult(
                        status=str(data.get("status") or "accepted"),
                        reason=str(data.get("reason_code") or "capture_accepted"),
                        capture_id=capture_id,
                        observation_id=data.get("observation_id") if isinstance(data.get("observation_id"), str) else None,
                        http_status=response.status_code,
                    )
                )
            last = CaptureUploadResult(
                status="failed",
                reason=code or f"http_{response.status_code}",
                capture_id=capture_id,
                http_status=response.status_code,
            )
            if response.status_code == 429:
                try:
                    retry_after = float(response.headers.get("retry-after", "60"))
                except ValueError:
                    retry_after = 60.0
                break
            if response.status_code < 500:
                # Auth, consent, blocklist, size or type rejection: retrying
                # the same bytes cannot succeed.
                break
        assert last is not None
        return self._record_failure(last, retry_after=retry_after)

    def status(self) -> dict[str, object]:
        return {
            "transport": "authenticated_local_http",
            "endpoint": CAPTURE_UPLOAD_PATH,
            "device_id": self.device_id,
            "pairing_id": self.pairing_id,
            "consecutive_failures": self._consecutive_failures,
            "cooling_down": self._clock() < self._cooldown_until,
            "last_status": self.last_result.status if self.last_result else None,
            "last_reason_code": self.last_result.reason if self.last_result else None,
        }


__all__ = [
    "CAPTURE_UPLOAD_PATH",
    "CaptureUploadResult",
    "LocalCaptureUploader",
    "bounded_local_analysis",
    "validate_local_backend_origin",
]
