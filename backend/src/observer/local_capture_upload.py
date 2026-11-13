"""Authenticated local capture upload from the native daemon (ADR-035).

The daemon makes no cloud call. It uploads one allowed PNG/JPEG capture with
its real app name, window title and capture time to the local backend, which
stores it privately under the governed screenshot folder and creates the
ScreenObservation through the same ingest/dedupe path as folder images. The
observation is queued for the governed vision analysis job exactly like folder
images; no inference happens here.

Private capture files are local data only; workspace backup/restore of these
captures is tracked separately (#1141) and is not provided by this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import stat
import tempfile
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import col, select

from src.db.engine import get_session
from src.db.models import ScreenObservation
from src.observer.screenshot_folder_source import (
    LOCAL_ANALYSIS_PREFIX,
    LOCAL_ANALYSIS_TEXT_MAX_CHARS,
    LOCAL_CAPTURE_ID_PREFIX,
    LOCAL_CAPTURE_SOURCE,
    LOCAL_CAPTURE_UPLOAD_DIRNAME,
    ImageCaptureContext,
    build_image_observation,
    resolve_screenshot_folder,
    screenshot_ingest_lock,
    validate_screenshot_folder_root,
)

MAX_CAPTURE_BYTES = 8 * 1024 * 1024
# Base64 inflates by 4/3; leave headroom for metadata and OCR text.
MAX_REQUEST_BYTES = (MAX_CAPTURE_BYTES * 4) // 3 + 256 * 1024
MAX_IMAGE_PIXELS = 50_000_000
MAX_METADATA_CHARS = 512
RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_MAX_UPLOADS = 30
MAX_CAPTURE_AGE = timedelta(days=1)
MAX_CLOCK_SKEW = timedelta(minutes=5)
_CAPTURE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"

# Built-in sensitive-app defaults mirrored from daemon/blocklist.py. The
# backend cannot see the operator's local blocklist file, so it enforces the
# defaults fail-closed: a local ``allowed_apps`` override never permits upload.
DEFAULT_CAPTURE_BLOCKLIST: frozenset[str] = frozenset(
    {
        "1password",
        "bitwarden",
        "lastpass",
        "keepassxc",
        "keeper",
        "dashlane",
        "enpass",
        "keychain access",
        "chase",
        "bank of america",
        "wells fargo",
        "capital one",
        "schwab",
        "fidelity",
        "td ameritrade",
        "robinhood",
        "signal",
        "ledger live",
        "trezor suite",
        "metamask",
    }
)


class LocalCaptureRejected(ValueError):
    """A capture upload that must not be stored."""

    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class UploadPolicy:
    """The pairing's stored media bounds that apply to this upload."""

    allowed_media_types: tuple[str, ...]
    max_content_bytes: int


@dataclass(frozen=True)
class LocalCaptureResult:
    status: str  # accepted | duplicate | suppressed
    reason: str
    capture_id: str
    observation_id: str | None
    image_sha256: str


def is_blocked_app(app_name: str) -> bool:
    lowered = app_name.strip().lower()
    return any(pattern in lowered for pattern in DEFAULT_CAPTURE_BLOCKLIST)


def sniff_image_type(content: bytes) -> tuple[str, str] | None:
    """Return (media_type, suffix) from magic bytes, never from client claims."""
    if content.startswith(_PNG_MAGIC):
        return "image/png", ".png"
    if content.startswith(_JPEG_MAGIC):
        return "image/jpeg", ".jpg"
    return None


def image_pixel_count(content: bytes) -> int:
    """Read declared dimensions from the header only (no full decode)."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
    except Image.DecompressionBombError as exc:
        raise LocalCaptureRejected("image_dimensions_too_large", status_code=413) from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise LocalCaptureRejected("image_header_invalid", status_code=415) from exc
    return int(width) * int(height)


def _require_private_store(path: Path) -> None:
    """Refuse a symlinked or foreign-owned capture store before writing."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise LocalCaptureRejected("capture_store_unsafe", status_code=503)


def validate_capture_id(capture_id: str) -> str:
    if not isinstance(capture_id, str) or not _CAPTURE_ID_RE.fullmatch(capture_id):
        raise LocalCaptureRejected("capture_id_invalid")
    return capture_id


def _clean_text(value: object, *, limit: int) -> str:
    text = str(value or "")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 0x20)
    return text[:limit]


def normalize_captured_at(value: datetime, *, now: datetime | None = None) -> datetime:
    captured = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    captured = captured.astimezone(timezone.utc)
    current = now or datetime.now(timezone.utc)
    if captured > current + MAX_CLOCK_SKEW:
        raise LocalCaptureRejected("captured_at_in_future")
    if current - captured > MAX_CAPTURE_AGE:
        raise LocalCaptureRejected("captured_at_too_old")
    return captured


class UploadRateLimiter:
    """In-process sliding window per uploader; bounds daemon retry storms."""

    def __init__(self, *, max_events: int = RATE_LIMIT_MAX_UPLOADS, window_seconds: float = RATE_LIMIT_WINDOW_SECONDS) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        events = self._events.setdefault(key, deque())
        while events and current - events[0] > self.window_seconds:
            events.popleft()
        if len(events) >= self.max_events:
            return False
        events.append(current)
        return True

    def reset(self) -> None:
        self._events.clear()


upload_rate_limiter = UploadRateLimiter()


def local_capture_upload_root(screenshot_root: Path | None = None) -> Path:
    root = (screenshot_root or resolve_screenshot_folder()).expanduser().resolve()
    return root / LOCAL_CAPTURE_UPLOAD_DIRNAME


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        path.chmod(0o700)


def _atomic_private_write(final_path: Path, content: bytes) -> None:
    """Write via a non-image temp suffix, fsync, then rename into place."""
    fd, temporary = tempfile.mkstemp(prefix=".upload-", suffix=".partial", dir=final_path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, final_path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    with contextlib.suppress(OSError):
        final_path.chmod(0o600)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


async def _observation_for_capture_id(capture_id: str) -> ScreenObservation | None:
    marker = f"{LOCAL_CAPTURE_ID_PREFIX}{capture_id}"
    async with get_session() as db:
        result = await db.execute(
            select(ScreenObservation)
            # Quote the full marker and escape LIKE wildcards so ``_`` in an
            # id cannot match a different capture or a longer prefix.
            .where(col(ScreenObservation.details_json).contains(f'"{marker}"', autoescape=True))
            .limit(1)
        )
        return result.scalar_one_or_none()


def _existing_capture_file(upload_root: Path, capture_id: str) -> Path | None:
    if not upload_root.is_dir():
        return None
    for suffix in (".png", ".jpg"):
        for candidate in upload_root.glob(f"*/{capture_id}{suffix}"):
            if candidate.is_file():
                return candidate
    return None


async def ingest_local_capture(
    *,
    content: bytes,
    app_name: str,
    window_title: str,
    captured_at: datetime,
    capture_id: str,
    local_analysis: dict[str, object] | None,
    uploader: dict[str, str],
    policy: UploadPolicy | None = None,
) -> LocalCaptureResult:
    """Store one capture privately and persist it via the shared ingest path."""
    capture_id = validate_capture_id(capture_id)
    clean_app = _clean_text(app_name, limit=MAX_METADATA_CHARS).strip()
    if not clean_app:
        raise LocalCaptureRejected("app_name_required")
    if is_blocked_app(clean_app):
        raise LocalCaptureRejected("sensitive_app_blocked", status_code=403)
    clean_title = _clean_text(window_title, limit=MAX_METADATA_CHARS)
    if not content:
        raise LocalCaptureRejected("content_required")
    if len(content) > MAX_CAPTURE_BYTES:
        raise LocalCaptureRejected("capture_too_large", status_code=413)
    sniffed = sniff_image_type(content)
    if sniffed is None:
        raise LocalCaptureRejected("unsupported_media_type", status_code=415)
    media_type, suffix = sniffed
    if policy is not None:
        if media_type not in policy.allowed_media_types:
            raise LocalCaptureRejected("media_type_not_allowed_by_pairing_policy", status_code=403)
        if len(content) > policy.max_content_bytes:
            raise LocalCaptureRejected("content_size_exceeds_pairing_policy", status_code=403)
    pixels = await asyncio.to_thread(image_pixel_count, content)
    if pixels <= 0:
        raise LocalCaptureRejected("image_header_invalid", status_code=415)
    if pixels > MAX_IMAGE_PIXELS:
        raise LocalCaptureRejected("image_dimensions_too_large", status_code=413)
    captured = normalize_captured_at(captured_at)
    image_sha256 = _sha256(content)

    screenshot_root = resolve_screenshot_folder()
    validate_screenshot_folder_root(screenshot_root)
    upload_root = local_capture_upload_root(screenshot_root)

    async with screenshot_ingest_lock():
        _require_private_store(upload_root)
        existing = await _observation_for_capture_id(capture_id)
        existing_file = await asyncio.to_thread(_existing_capture_file, upload_root, capture_id)
        if existing_file is not None:
            stored_sha = await asyncio.to_thread(lambda: _sha256(existing_file.read_bytes()))
            if stored_sha != image_sha256:
                raise LocalCaptureRejected("capture_id_conflict", status_code=409)
        if existing is not None:
            return LocalCaptureResult(
                status="duplicate",
                reason="capture_already_accepted",
                capture_id=capture_id,
                observation_id=existing.id,
                image_sha256=image_sha256,
            )

        # Create a missing screenshot folder privately, but never re-permission
        # an existing operator-chosen folder; only the upload subtree is ours.
        screenshot_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _ensure_private_dir(upload_root)
        _require_private_store(upload_root)
        day_dir = upload_root / captured.strftime("%Y-%m-%d")
        _require_private_store(day_dir)
        _ensure_private_dir(day_dir)
        final_path = existing_file or (day_dir / f"{capture_id}{suffix}")
        if existing_file is None:
            await asyncio.to_thread(_atomic_private_write, final_path, content)

        clean_analysis = _clean_local_analysis(local_analysis)
        extra_details = [f"{LOCAL_CAPTURE_ID_PREFIX}{capture_id}"]
        if clean_analysis:
            extra_details.append(
                LOCAL_ANALYSIS_PREFIX
                + json.dumps(clean_analysis, sort_keys=True, separators=(",", ":"))
            )
        context = ImageCaptureContext(
            app_name=clean_app,
            window_title=clean_title,
            captured_at=captured,
            source=LOCAL_CAPTURE_SOURCE,
            extra_artifacts={
                "capture_id": capture_id,
                "media_type": media_type,
                "uploader_device_id": uploader.get("device_id", ""),
                "uploader_pairing_id": uploader.get("pairing_id", ""),
            },
            extra_details=tuple(extra_details),
            summary=_local_summary(clean_analysis)
            or f"Screen capture from {clean_app}" + (f": {clean_title}" if clean_title else "") + ".",
        )
        try:
            decision = await build_image_observation(final_path, screenshot_root, capture=context)
        except Exception:
            with contextlib.suppress(OSError):
                final_path.unlink()
            raise
        if decision.observation is None:
            # Suppressed duplicates keep no private copy: the representative
            # observation already holds the image and its visual run.
            if decision.reason != "already_ingested":
                with contextlib.suppress(OSError):
                    final_path.unlink()
            return LocalCaptureResult(
                status="suppressed" if decision.reason != "already_ingested" else "duplicate",
                reason=decision.reason,
                capture_id=capture_id,
                observation_id=decision.existing_observation_id,
                image_sha256=image_sha256,
            )

        from src.observer.screen_repository import screen_observation_repo

        observation_fields = dict(decision.observation)
        # Non-semantic consumers (digest, end-of-day report) read these
        # columns, so the bounded local analysis is their content until the
        # governed vision analysis succeeds.
        if clean_analysis.get("activity"):
            observation_fields["activity_type"] = str(clean_analysis["activity"])[:64]
        if clean_analysis.get("project"):
            observation_fields["project"] = str(clean_analysis["project"])[:256]
        try:
            observation = await screen_observation_repo.create(**observation_fields)
        except Exception:
            with contextlib.suppress(OSError):
                final_path.unlink()
            raise
        return LocalCaptureResult(
            status="accepted",
            reason="capture_accepted",
            capture_id=capture_id,
            observation_id=observation.id,
            image_sha256=image_sha256,
        )


def _clean_local_analysis(value: dict[str, object] | None) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, object] = {}
    for key, limit in (("provider", 32), ("activity", 64), ("project", 256), ("summary", 512)):
        text = _clean_text(value.get(key), limit=limit).strip()
        if text:
            cleaned[key] = text
    text = _clean_text(value.get("text"), limit=LOCAL_ANALYSIS_TEXT_MAX_CHARS).strip()
    if text:
        cleaned["text"] = text
    details = value.get("details")
    if isinstance(details, list):
        items = [_clean_text(item, limit=256).strip() for item in details if isinstance(item, str)]
        items = [item for item in items if item][:16]
        if items:
            cleaned["details"] = items
    return cleaned


def _local_summary(analysis: dict[str, object]) -> str | None:
    summary = str(analysis.get("summary") or "").strip()
    if not summary:
        summary = " ".join(str(analysis.get("text") or "").split())[:200]
    return summary[:512] or None


def cleanup_local_capture_uploads(
    *,
    retention_days: int,
    screenshot_root: Path | None = None,
    now: datetime | None = None,
    max_files: int = 5_000,
) -> int:
    """Delete uploaded capture files older than retention; bounded per run.

    Only files inside the private upload subdirectory are touched: never the
    operator's own screenshot-folder images. Leftover ``.partial`` temp files
    from interrupted writes are removed on the same schedule.
    """
    upload_root = local_capture_upload_root(screenshot_root)
    try:
        _require_private_store(upload_root)
    except LocalCaptureRejected:
        return 0
    if not upload_root.is_dir():
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max(1, retention_days))
    cutoff_ts = cutoff.timestamp()
    deleted = 0
    for dirpath, dirnames, filenames in os.walk(upload_root, followlinks=False):
        # os.walk lists symlinked directories but never descends into them.
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
        for name in sorted(filenames):
            if deleted >= max_files:
                break
            path = Path(dirpath) / name
            if path.suffix.lower() not in {".png", ".jpg", ".partial"}:
                continue
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff_ts:
                    path.unlink()
                    deleted += 1
            except OSError:
                continue
    for directory in sorted((p for p in upload_root.iterdir() if p.is_dir() and not p.is_symlink()), reverse=True):
        with contextlib.suppress(OSError):
            directory.rmdir()  # only succeeds when empty
    return deleted
