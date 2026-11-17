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
import logging
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

logger = logging.getLogger(__name__)

MIB = 1024 * 1024
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
    pruned_files: int = 0
    pruned_bytes: int = 0
    prune_skipped_reason: str | None = None
    capture_store_over_cap: bool = False


@dataclass(frozen=True)
class CaptureStorePruneResult:
    """Outcome of one bounded prune of the private capture upload store."""

    deleted_files: int = 0
    deleted_expired: int = 0
    deleted_for_size: int = 0
    freed_bytes: int = 0
    remaining_bytes: int = 0
    remaining_files: int = 0
    over_cap: bool = False
    skipped_reason: str | None = None


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
        # Still inside the ingest lock: no other write, rename or capture-id
        # check can interleave with this prune. A prune failure never fails
        # the accepted capture.
        prune = await _enforce_store_cap(screenshot_root, keep=final_path)
        return LocalCaptureResult(
            status="accepted",
            reason="capture_accepted",
            capture_id=capture_id,
            observation_id=observation.id,
            image_sha256=image_sha256,
            pruned_files=prune.deleted_files,
            pruned_bytes=prune.freed_bytes,
            prune_skipped_reason=prune.skipped_reason,
            capture_store_over_cap=prune.over_cap,
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


SETTINGS_UNREADABLE = "settings_unreadable"
# The smallest non-zero cap: one capture may be up to 8 MiB, so a smaller cap
# would delete every new file immediately.
MIN_CAPTURE_STORE_CAP_MB = MAX_CAPTURE_BYTES // (1024 * 1024)


def capture_store_limits() -> tuple[int, int] | None:
    """Return ``(max_bytes, retention_days)`` for the private upload store.

    ``archive_max_mb`` caps this store on its own (per store; ``0`` keeps no
    size cap). When the daemon is paired the upload store is the screenshot
    archive, so ``archive_retention_days`` bounds it too: the shorter of it
    and the observation retention applies.

    ``None`` when the screen-analysis settings file exists but cannot be
    read: the limits are then unknown and nothing may be pruned on defaults.
    """
    from config.settings import settings
    from src.observer.screen_analysis_settings import read_screen_analysis_settings

    current = read_screen_analysis_settings()
    if current.get("settings_unreadable"):
        return None
    max_mb = max(0, int(current.get("archive_max_mb") or 0))
    archive_days = max(1, int(current.get("archive_retention_days") or 365))
    observation_days = max(1, int(settings.screen_observation_retention_days))
    return max_mb * MIB, min(observation_days, archive_days)


async def _enforce_store_cap(screenshot_root: Path, *, keep: Path | None = None) -> CaptureStorePruneResult:
    """Size-prune the upload store after a write; returns without a walk when uncapped."""
    try:
        limits = capture_store_limits()
        if limits is None:
            logger.warning("capture store prune skipped: screen-analysis settings are unreadable")
            return CaptureStorePruneResult(skipped_reason=SETTINGS_UNREADABLE)
        max_bytes, _retention_days = limits
        if max_bytes <= 0:
            return CaptureStorePruneResult()
        return await asyncio.to_thread(
            prune_local_capture_uploads,
            retention_days=None,
            max_bytes=max_bytes,
            screenshot_root=screenshot_root,
            keep=keep,
        )
    except Exception:
        logger.warning("capture store prune after upload failed", exc_info=True)
        return CaptureStorePruneResult(skipped_reason="prune_failed")


@dataclass
class _StoreEntry:
    path: Path
    relative: str
    size: int
    mtime_ns: int


_COUNTED_SUFFIXES = frozenset({".png", ".jpg", ".partial"})


def _scan_capture_store(upload_root: Path, *, max_scan: int) -> tuple[list[_StoreEntry], bool]:
    """List candidate files without following symlinks; oldest day directories first."""
    entries: list[_StoreEntry] = []
    scanned = 0
    truncated = False
    pending: list[Path] = [upload_root]
    while pending:
        directory = pending.pop(0)
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError:
            continue
        subdirectories: list[Path] = []
        for child in children:
            scanned += 1
            if scanned > max_scan:
                truncated = True
                break
            try:
                info = child.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                subdirectories.append(Path(child.path))
                continue
            if not stat.S_ISREG(info.st_mode):
                continue  # symlinks and special files are never candidates
            suffix = Path(child.name).suffix.lower()
            if suffix not in _COUNTED_SUFFIXES:
                continue
            path = Path(child.path)
            entries.append(
                _StoreEntry(
                    path=path,
                    relative=path.relative_to(upload_root).as_posix(),
                    size=int(info.st_size),
                    mtime_ns=int(info.st_mtime_ns),
                )
            )
        if truncated:
            break
        # Day directories are named YYYY-MM-DD, so name order is age order.
        pending = subdirectories + pending
    return entries, truncated


def _unlink_regular_file(path: Path) -> bool:
    """Delete one regular file; ``True`` when it is gone afterwards."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def prune_local_capture_uploads(
    *,
    retention_days: int | None,
    max_bytes: int = 0,
    screenshot_root: Path | None = None,
    now: datetime | None = None,
    max_files: int = 5_000,
    max_scan: int = 200_000,
    keep: Path | None = None,
) -> CaptureStorePruneResult:
    """Bounded age and size prune of the private upload store; oldest first.

    Only regular ``.png``/``.jpg``/``.partial`` files inside the private
    upload subdirectory are counted or deleted: never the operator's own
    screenshot-folder images, never a symlink or its target. Every caller
    holds the screenshot ingest lock, so no write is in flight and any
    ``.partial`` is a crash leftover, pruned like a capture. ``keep`` (the
    file the current request just wrote) is never deleted. ``max_bytes`` of
    ``0`` keeps no size cap. Every failure deletes less, never more.
    """
    upload_root = local_capture_upload_root(screenshot_root)
    try:
        _require_private_store(upload_root)
    except LocalCaptureRejected:
        return CaptureStorePruneResult(skipped_reason="capture_store_unsafe")
    if not upload_root.is_dir():
        return CaptureStorePruneResult()
    if retention_days is None and max_bytes <= 0:
        return CaptureStorePruneResult()

    keep_path = keep.resolve() if keep is not None else None
    entries, truncated = _scan_capture_store(upload_root, max_scan=max_scan)
    entries.sort(key=lambda entry: (entry.mtime_ns, entry.relative))
    deleted_expired = 0
    deleted_for_size = 0
    freed = 0
    bound_hit = False
    touched_dirs: set[Path] = set()

    def protected(entry: _StoreEntry) -> bool:
        return keep_path is not None and entry.path.resolve() == keep_path

    def delete(entry: _StoreEntry) -> bool:
        nonlocal freed
        if not _unlink_regular_file(entry.path):
            return False
        freed += entry.size
        touched_dirs.add(entry.path.parent)
        return True

    survivors: list[_StoreEntry] = []
    if retention_days is not None:
        current = now or datetime.now(timezone.utc)
        cutoff_ns = int((current - timedelta(days=max(1, retention_days))).timestamp() * 1_000_000_000)
        for entry in entries:
            if entry.mtime_ns < cutoff_ns and not protected(entry):
                if deleted_expired >= max_files:
                    bound_hit = True  # expired files remain: report it
                elif delete(entry):
                    deleted_expired += 1
                    continue
            survivors.append(entry)
    else:
        survivors = list(entries)

    total = sum(entry.size for entry in survivors)
    remaining: list[_StoreEntry] = []
    if max_bytes > 0:
        for entry in survivors:
            if total > max_bytes and not protected(entry):
                if deleted_expired + deleted_for_size >= max_files:
                    bound_hit = True
                elif delete(entry):
                    deleted_for_size += 1
                    total -= entry.size
                    continue
            remaining.append(entry)
    else:
        remaining = survivors

    # Remove only the day directories this run emptied.
    for directory in sorted(touched_dirs, reverse=True):
        if directory != upload_root and directory.parent == upload_root and not directory.is_symlink():
            with contextlib.suppress(OSError):
                directory.rmdir()  # only succeeds when empty
    return CaptureStorePruneResult(
        deleted_files=deleted_expired + deleted_for_size,
        deleted_expired=deleted_expired,
        deleted_for_size=deleted_for_size,
        freed_bytes=freed,
        remaining_bytes=total,
        remaining_files=len(remaining),
        over_cap=(max_bytes > 0 and total > max_bytes) or truncated or bound_hit,
    )


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
    return prune_local_capture_uploads(
        retention_days=retention_days,
        screenshot_root=screenshot_root,
        now=now,
        max_files=max_files,
    ).deleted_files
