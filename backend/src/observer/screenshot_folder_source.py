"""Local screenshot-folder scanning for screen observations."""

from __future__ import annotations

import hashlib
import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_, update
from sqlalchemy.exc import OperationalError
from sqlmodel import col, select

from config.settings import settings
from src.audit.runtime import log_integration_event
from src.db.engine import get_session
from src.db.models import ScreenObservation
from src.model_fabric import NoCompliantModelRouteError
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionError
from src.observer.image_metadata import image_metadata_label, local_image_metadata
from src.observer.screen_repository import screen_observation_repo
from src.observer.screenshot_semantic_analysis import (
    ScreenshotSemanticAnalysisError,
    analyze_screenshot_image,
    screenshot_analysis_detail,
    screenshot_analysis_error_detail,
    screenshot_analysis_status_detail,
    semantic_analysis_status_from_details,
    screenshot_semantic_analysis_enabled,
)

logger = logging.getLogger(__name__)


class ScreenshotFolderImageError(ValueError):
    """Raised when a screenshot-folder image is unsafe or unsupported."""


class ScreenshotFolderPersistenceError(RuntimeError):
    """Raised when Seraph cannot persist screenshot-folder analysis state."""


@dataclass(frozen=True)
class ScreenshotFolderScanResult:
    scanned: int
    ingested: int
    skipped_duplicates: int
    rejected: list[dict[str, str]]


@dataclass(frozen=True)
class ScreenshotFolderAnalysisResult:
    scanned: int
    analyzed: int
    failed: int
    skipped: int


SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
SCREENSHOT_FOLDER_PROVIDER = "screenshot_folder"
SCREENSHOT_FOLDER_HASH_PREFIX = "screenshot_folder_image_sha256"
SCREENSHOT_VISUAL_RUN_PREFIX = "screenshot_visual_run:"
SCREENSHOT_FOLDER_ENV = "SERAPH_SCREENSHOT_FOLDER"
_SCAN_LOCK = asyncio.Lock()
_MAX_ANALYSIS_ATTEMPTS = 3
_FAILED_ANALYSIS_RETRY_AFTER = timedelta(minutes=2)
_VISUAL_DEDUPE_VERSION = "seraph.screenshot_visual_dedupe.v1"
_VISUAL_FINGERPRINT_SIZE = 8
_VISUAL_DUPLICATE_MAX_DISTANCE = 2
_VISUAL_DEDUPE_REFRESH_AFTER = timedelta(minutes=10)
_DB_LOCK_RETRY_ATTEMPTS = 3
_DB_LOCK_RETRY_BASE_DELAY_SECONDS = 0.05
_PERSISTENCE_STATS = {
    "db_lock_retries": 0,
    "db_lock_failures": 0,
    "selection_db_lock_retries": 0,
    "selection_db_lock_failures": 0,
    "persistence_db_lock_retries": 0,
    "persistence_db_lock_failures": 0,
}


def resolve_screenshot_folder(configured: str | None = None) -> Path:
    """Resolve Seraph's local screenshot folder."""
    if configured and configured.strip():
        return Path(configured).expanduser().resolve()
    env_root = os.environ.get(SCREENSHOT_FOLDER_ENV, "").strip()
    if env_root:
        return Path(env_root).expanduser().resolve()
    screen_analysis_path = Path(settings.workspace_dir).expanduser().resolve() / "screen-analysis-settings.json"
    if screen_analysis_path.exists():
        try:
            payload = json.loads(screen_analysis_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            settings_root = str(payload.get("screenshot_folder") or "").strip()
            if settings_root:
                return Path(settings_root).expanduser().resolve()
    return Path(settings.workspace_dir).expanduser().resolve() / "artifacts" / "screenshot-folder"


def screenshot_folder_persistence_status() -> dict[str, int]:
    """Return process-local DB lock/backoff counters for operator-visible status."""
    return dict(_PERSISTENCE_STATS)


async def scan_screenshot_folder(root: Path, *, limit: int = 100) -> ScreenshotFolderScanResult:
    """Scan a local screenshot directory and persist new images as observations."""
    async with _SCAN_LOCK:
        screenshot_root = root.expanduser().resolve()
        validate_screenshot_folder_root(screenshot_root)
        ingest_limit = max(limit, 1)
        image_paths = await asyncio.to_thread(_image_paths, screenshot_root)
        scanned = 0
        ingested = 0
        skipped = 0
        rejected: list[dict[str, str]] = []

        for image_path in image_paths:
            if ingested >= ingest_limit:
                break
            scanned += 1
            await asyncio.sleep(0)
            try:
                observation = await _image_to_observation(image_path, screenshot_root)
                if observation is None:
                    skipped += 1
                    continue
                await screen_observation_repo.create(**observation)
                ingested += 1
            except Exception as exc:
                rejected.append({"image_path": str(image_path.resolve()), "reason": str(exc)})

        await log_integration_event(
            integration_type="screenshot_folder",
            name="screenshot_scan",
            outcome="succeeded" if not rejected else "degraded",
            details={
                "screenshot_folder": str(screenshot_root),
                "scanned": scanned,
                "ingested": ingested,
                "skipped_duplicates": skipped,
                "rejected_count": len(rejected),
            },
        )
        return ScreenshotFolderScanResult(
            scanned=scanned,
            ingested=ingested,
            skipped_duplicates=skipped,
            rejected=rejected,
        )


async def analyze_pending_screenshot_folder_observations(
    *,
    limit: int = 5,
    concurrency: int = 1,
) -> ScreenshotFolderAnalysisResult:
    """Analyze already-ingested screenshot-folder observations without blocking folder scans."""
    analysis_limit = max(limit, 1)
    worker_limit = max(concurrency, 1)

    if not screenshot_semantic_analysis_enabled():
        return ScreenshotFolderAnalysisResult(scanned=0, analyzed=0, failed=0, skipped=0)

    candidates = await _select_analysis_candidates_with_retry(limit=analysis_limit * 20)
    observations = [
        observation
        for observation in candidates
        if _analysis_candidate_ready(_observation_details(observation))
    ][:analysis_limit]
    logger.info(
        "screenshot_folder_analysis: selected %d pending observations (limit=%d concurrency=%d)",
        len(observations),
        analysis_limit,
        worker_limit,
    )

    semaphore = asyncio.Semaphore(worker_limit)

    async def analyze_one(observation: ScreenObservation) -> tuple[bool, bool, bool]:
        details = _observation_details(observation)
        artifacts = _capture_artifacts_from_details(details)
        if not artifacts:
            return False, False, True

        image_path = Path(str(artifacts.get("image_path") or "")).expanduser().resolve()
        analysis = None
        failed_reason: str | None = None
        status_override: str | None = None
        try:
            if not image_path.is_file():
                status_override = "source_missing"
                raise ScreenshotFolderImageError("image file not found")
            async with semaphore:
                logger.info("screenshot_folder_analysis: analyzing %s", image_path.name)
                analysis = await analyze_screenshot_image(image_path, artifacts)
                logger.info("screenshot_folder_analysis: analyzed %s", image_path.name)
            details = _replace_analysis_details(
                details,
                analysis=analysis,
                error_reason=None if analysis is not None else "provider not configured",
            )
        except (
            OSError,
            ScreenshotFolderImageError,
            ScreenshotSemanticAnalysisError,
            NoCompliantModelRouteError,
            RemoteInferenceAdmissionError,
        ) as exc:
            failed_reason = str(exc)
            logger.warning("screenshot_folder_analysis: failed %s: %s", image_path.name, failed_reason)
            if status_override is None and _is_blocked_analysis_error(exc):
                status_override = "blocked"
            details = _replace_analysis_details(
                details,
                analysis=None,
                error_reason=str(exc),
                status=status_override or "failed",
            )

        try:
            await _persist_analysis_details_with_retry(observation.id, details)
        except ScreenshotFolderPersistenceError as exc:
            logger.warning(
                "screenshot_folder_analysis: failed to persist %s: %s",
                image_path.name,
                exc,
            )
            return False, True, False
        if analysis is not None:
            return True, False, False
        if failed_reason is not None:
            return False, True, False
        return False, False, True

    results = await asyncio.gather(*(analyze_one(observation) for observation in observations))
    analyzed = sum(1 for item in results if item[0])
    failed = sum(1 for item in results if item[1])
    skipped = sum(1 for item in results if item[2])

    return ScreenshotFolderAnalysisResult(scanned=len(observations), analyzed=analyzed, failed=failed, skipped=skipped)


def _is_blocked_analysis_error(error: BaseException) -> bool:
    """Classify policy/admission denials separately from provider failures."""
    if isinstance(error, RemoteInferenceAdmissionError):
        return True
    if isinstance(error, NoCompliantModelRouteError):
        return True
    return isinstance(error, ScreenshotSemanticAnalysisError) and str(error).startswith(
        "remote_inference_blocked:"
    )


async def _select_analysis_candidates_with_retry(*, limit: int) -> list[ScreenObservation]:
    for attempt in range(_DB_LOCK_RETRY_ATTEMPTS):
        try:
            async with get_session() as db:
                result = await db.execute(
                    select(ScreenObservation)
                    .where(col(ScreenObservation.app_name) == "Screenshot Folder")
                    .where(col(ScreenObservation.details_json).contains("capture_artifacts:"))
                    .where(col(ScreenObservation.details_json).contains(SCREENSHOT_FOLDER_PROVIDER))
                    .where(_analysis_candidate_status_filter())
                    .order_by(col(ScreenObservation.timestamp).asc())
                    .limit(limit)
                )
                return list(result.scalars().all())
        except OperationalError as exc:
            if not _is_database_locked(exc):
                raise
            await _record_db_lock_retry(
                attempt=attempt,
                retry_key="selection_db_lock_retries",
                failure_key="selection_db_lock_failures",
            )
    raise ScreenshotFolderPersistenceError("database is locked while selecting pending screenshot analysis")


async def _persist_analysis_details_with_retry(observation_id: str, details: list[str]) -> None:
    details_json = json.dumps(details)
    for attempt in range(_DB_LOCK_RETRY_ATTEMPTS):
        try:
            async with get_session() as db:
                await db.execute(
                    update(ScreenObservation)
                    .where(ScreenObservation.id == observation_id)
                    .values(details_json=details_json)
                )
            return
        except OperationalError as exc:
            if not _is_database_locked(exc):
                raise
            await _record_db_lock_retry(
                attempt=attempt,
                retry_key="persistence_db_lock_retries",
                failure_key="persistence_db_lock_failures",
            )
    raise ScreenshotFolderPersistenceError("database is locked while saving screenshot analysis")


async def _record_db_lock_retry(*, attempt: int, retry_key: str, failure_key: str) -> None:
    _PERSISTENCE_STATS["db_lock_retries"] += 1
    _PERSISTENCE_STATS[retry_key] += 1
    if attempt >= _DB_LOCK_RETRY_ATTEMPTS - 1:
        _PERSISTENCE_STATS["db_lock_failures"] += 1
        _PERSISTENCE_STATS[failure_key] += 1
        return
    await asyncio.sleep(_DB_LOCK_RETRY_BASE_DELAY_SECONDS * (2**attempt))


def _is_database_locked(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def validate_screenshot_folder_root(root: Path) -> None:
    """Reject roots that are too broad to be a dedicated screenshot folder."""
    dangerous_roots = _dangerous_scan_roots()
    if root in dangerous_roots:
        raise ScreenshotFolderImageError(
            "Screenshot folder must be a dedicated image directory, not a broad home, "
            "desktop, downloads, workspace, or filesystem root"
        )


def _dangerous_scan_roots() -> set[Path]:
    roots: set[Path] = {Path("/").resolve()}
    for candidate in (
        Path.home(),
        Path.home() / "Desktop",
        Path.home() / "Downloads",
        Path(settings.workspace_dir).expanduser(),
    ):
        try:
            roots.add(candidate.resolve())
        except OSError:
            continue
    return roots


def _image_paths(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    return sorted(
        (
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
        ),
        key=lambda path: _capture_timestamp(path, root)[0].timestamp(),
        reverse=True,
    )


async def _image_to_observation(image_path: Path, root: Path) -> dict[str, object] | None:
    resolved = image_path.resolve()
    if not resolved.is_relative_to(root):
        raise ScreenshotFolderImageError("image is outside screenshot folder root")
    if resolved.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
        raise ScreenshotFolderImageError("unsupported image type")
    if not resolved.is_file():
        raise ScreenshotFolderImageError("image file not found")

    stat = resolved.stat()
    metadata = await asyncio.to_thread(local_image_metadata, resolved)
    captured_at, captured_at_source = _capture_timestamp(resolved, root)
    relative_path = resolved.relative_to(root).as_posix()

    image_sha256 = await asyncio.to_thread(_sha256_file, resolved)
    existing_duplicate = await _image_already_ingested(image_sha256)
    if existing_duplicate is not None:
        duplicate_details = _observation_details(existing_duplicate)
        duplicate_artifacts = _capture_artifacts_from_details(duplicate_details) or {}
        duplicate_path = Path(str(duplicate_artifacts.get("image_path") or "")).expanduser().resolve()
        if duplicate_path == resolved:
            return None
        await _extend_visual_run(
            existing_duplicate,
            suppressed_path=resolved,
            suppressed_at=captured_at,
            reason="exact_hash_duplicate",
        )
        return None

    visual_fingerprint = await asyncio.to_thread(_visual_fingerprint, resolved)
    visual_duplicate = await _current_visual_duplicate(
        root=root,
        captured_at=captured_at,
        image_path=resolved,
        metadata=metadata,
        visual_fingerprint=visual_fingerprint,
    )
    if visual_duplicate is not None:
        await _extend_visual_run(
            visual_duplicate,
            suppressed_path=resolved,
            suppressed_at=captured_at,
            reason="near_visual_duplicate",
        )
        return None

    capture_id = image_sha256[:16]
    artifacts = {
        "id": capture_id,
        "provider": SCREENSHOT_FOLDER_PROVIDER,
        "source": "local_image_directory",
        "created_at": captured_at.isoformat(),
        "created_at_source": captured_at_source,
        "screenshot_folder": str(root),
        "image_path": str(resolved),
        "relative_path": relative_path,
        "image_sha256": image_sha256,
        "image_bytes": stat.st_size,
        "file_format": metadata.get("file_format"),
        "width": metadata.get("width"),
        "height": metadata.get("height"),
    }
    if visual_fingerprint is not None:
        artifacts["visual_fingerprint"] = visual_fingerprint
        artifacts["visual_dedupe_version"] = _VISUAL_DEDUPE_VERSION
    details = [
        f"{SCREENSHOT_FOLDER_HASH_PREFIX}:{image_sha256}",
        "capture_artifacts:" + json.dumps(artifacts, sort_keys=True, separators=(",", ":")),
        SCREENSHOT_VISUAL_RUN_PREFIX
        + json.dumps(
            {
                "schema_version": _VISUAL_DEDUPE_VERSION,
                "representative_path": str(resolved),
                "first_seen": captured_at.isoformat(),
                "last_seen": captured_at.isoformat(),
                "suppressed_count": 0,
                "suppressed_reasons": {},
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    ]
    if screenshot_semantic_analysis_enabled():
        details.append(screenshot_analysis_status_detail("pending", reason="queued_for_analysis"))
    else:
        blocked_reason = "remote_inference_blocked:configuration_required"
        details.append(screenshot_analysis_error_detail(blocked_reason))
        details.append(screenshot_analysis_status_detail("blocked", reason=blocked_reason))
    metadata_label = image_metadata_label(metadata)
    summary_suffix = f" ({metadata_label})" if metadata_label else ""
    return {
        "app_name": "Screenshot Folder",
        "window_title": resolved.name,
        "activity_type": "screen",
        "project": None,
        "summary": f"Screenshot image added from folder: {resolved.name}{summary_suffix}.",
        "details": details,
        "blocked": False,
        "timestamp": captured_at,
    }


def _capture_timestamp(image_path: Path, root: Path) -> tuple[datetime, str]:
    relative = image_path.relative_to(root)
    for part in (image_path.stem, *reversed(relative.parts[:-1])):
        parsed = _parse_capture_timestamp_token(part)
        if parsed is not None:
            return parsed, "path"
    return datetime.fromtimestamp(image_path.stat().st_mtime, timezone.utc), "file_mtime"


def _parse_capture_timestamp_token(token: str) -> datetime | None:
    seconds_part, separator, fractional_part = token.partition("-")
    if not separator or not seconds_part.isdigit() or not fractional_part.isdigit():
        return None
    try:
        seconds = int(seconds_part)
        fractional = int(fractional_part[:9].ljust(9, "0"))
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return datetime.fromtimestamp(seconds + fractional / 1_000_000_000, timezone.utc)


async def _image_already_ingested(image_sha256: str) -> ScreenObservation | None:
    marker = f"{SCREENSHOT_FOLDER_HASH_PREFIX}:{image_sha256}"
    async with get_session() as db:
        result = await db.execute(
            select(ScreenObservation)
            .where(col(ScreenObservation.details_json).contains(marker))
            .order_by(col(ScreenObservation.timestamp).desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def _current_visual_duplicate(
    *,
    root: Path,
    captured_at: datetime,
    image_path: Path,
    metadata: dict[str, object],
    visual_fingerprint: str | None,
) -> ScreenObservation | None:
    if visual_fingerprint is None:
        return None
    async with get_session() as db:
        base_query = (
            select(ScreenObservation)
            .where(col(ScreenObservation.blocked) == False)  # noqa: E712
            .where(col(ScreenObservation.app_name) == "Screenshot Folder")
            .where(col(ScreenObservation.details_json).contains("capture_artifacts:"))
            .where(col(ScreenObservation.details_json).contains(SCREENSHOT_FOLDER_PROVIDER))
        )
        result = await db.execute(
            base_query
            .where(col(ScreenObservation.timestamp) <= captured_at)
            .order_by(col(ScreenObservation.timestamp).desc())
            .limit(1)
        )
        representative = result.scalar_one_or_none()
        if representative is None:
            result = await db.execute(
                base_query
                .where(col(ScreenObservation.timestamp) >= captured_at)
                .order_by(col(ScreenObservation.timestamp).asc())
                .limit(1)
            )
            representative = result.scalar_one_or_none()
    if representative is None:
        return None
    details = _observation_details(representative)
    artifacts = _capture_artifacts_from_details(details) or {}
    if Path(str(artifacts.get("screenshot_folder") or "")).expanduser().resolve() != root:
        return None
    if Path(str(artifacts.get("image_path") or "")).expanduser().resolve() == image_path:
        return None
    if artifacts.get("file_format") != metadata.get("file_format"):
        return None
    if artifacts.get("width") != metadata.get("width") or artifacts.get("height") != metadata.get("height"):
        return None
    representative_fingerprint = str(artifacts.get("visual_fingerprint") or "").strip()
    if not representative_fingerprint:
        return None
    run = _visual_run_from_details(details)
    last_seen = _parse_status_recorded_at(run.get("last_seen")) if run else None
    if last_seen is not None:
        distance_from_run = abs((captured_at - last_seen).total_seconds())
        if distance_from_run > _VISUAL_DEDUPE_REFRESH_AFTER.total_seconds():
            return None
    if _fingerprint_distance(visual_fingerprint, representative_fingerprint) > _VISUAL_DUPLICATE_MAX_DISTANCE:
        return None
    return representative


async def _extend_visual_run(
    representative: ScreenObservation,
    *,
    suppressed_path: Path,
    suppressed_at: datetime,
    reason: str,
) -> None:
    details = _observation_details(representative)
    run = _visual_run_from_details(details) or {}
    representative_path = str(run.get("representative_path") or "")
    if not representative_path:
        artifacts = _capture_artifacts_from_details(details) or {}
        representative_path = str(artifacts.get("image_path") or representative.window_title or "")
    first_seen = _min_iso_timestamp(run.get("first_seen"), suppressed_at)
    last_seen = _max_iso_timestamp(run.get("last_seen"), suppressed_at)
    try:
        suppressed_count = int(run.get("suppressed_count") or 0) + 1
    except (TypeError, ValueError):
        suppressed_count = 1
    reasons = run.get("suppressed_reasons")
    reason_counts = dict(reasons) if isinstance(reasons, dict) else {}
    reason_counts[reason] = int(reason_counts.get(reason) or 0) + 1
    next_run = {
        "schema_version": _VISUAL_DEDUPE_VERSION,
        "representative_path": representative_path,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "suppressed_count": suppressed_count,
        "latest_suppressed_path": str(suppressed_path),
        "suppressed_reasons": reason_counts,
    }
    next_details = [
        item for item in details if not item.startswith(SCREENSHOT_VISUAL_RUN_PREFIX)
    ]
    next_details.append(
        SCREENSHOT_VISUAL_RUN_PREFIX + json.dumps(next_run, sort_keys=True, separators=(",", ":"))
    )
    duration_s = _duration_seconds(first_seen, last_seen)
    async with get_session() as db:
        await db.execute(
            update(ScreenObservation)
            .where(ScreenObservation.id == representative.id)
            .values(details_json=json.dumps(next_details), duration_s=duration_s)
        )


def _observation_details(observation: ScreenObservation) -> list[str]:
    try:
        payload = json.loads(observation.details_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload if isinstance(item, str)]


def _capture_artifacts_from_details(details: list[str]) -> dict[str, object] | None:
    for item in details:
        if not item.startswith("capture_artifacts:"):
            continue
        try:
            payload = json.loads(item.removeprefix("capture_artifacts:"))
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict) and payload.get("provider") == SCREENSHOT_FOLDER_PROVIDER:
            return payload
    return None


def _visual_run_from_details(details: list[str]) -> dict[str, object] | None:
    for item in details:
        if not item.startswith(SCREENSHOT_VISUAL_RUN_PREFIX):
            continue
        try:
            payload = json.loads(item.removeprefix(SCREENSHOT_VISUAL_RUN_PREFIX))
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            return payload
    return None


def _visual_fingerprint(path: Path) -> str | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            grayscale = image.convert("L").resize(
                (_VISUAL_FINGERPRINT_SIZE, _VISUAL_FINGERPRINT_SIZE),
                Image.Resampling.BILINEAR,
            )
            if hasattr(grayscale, "get_flattened_data"):
                values = list(grayscale.get_flattened_data())
            else:
                values = list(grayscale.getdata())
    except Exception:
        return None
    if not values:
        return None
    average = sum(int(value) for value in values) / len(values)
    bits = "".join("1" if int(value) >= average else "0" for value in values)
    return f"{int(round(average)):02x}:{int(bits, 2):016x}"


def _fingerprint_distance(left: str, right: str) -> int:
    try:
        left_average, left_bits = left.split(":", 1)
        right_average, right_bits = right.split(":", 1)
        if abs(int(left_average, 16) - int(right_average, 16)) > 4:
            return 65
        return (int(left_bits, 16) ^ int(right_bits, 16)).bit_count()
    except (ValueError, AttributeError):
        return 65


def _min_iso_timestamp(current: object, candidate: datetime) -> str:
    parsed = _parse_status_recorded_at(current)
    if parsed is None or candidate < parsed:
        return candidate.isoformat()
    return parsed.isoformat()


def _max_iso_timestamp(current: object, candidate: datetime) -> str:
    parsed = _parse_status_recorded_at(current)
    if parsed is None or candidate > parsed:
        return candidate.isoformat()
    return parsed.isoformat()


def _duration_seconds(first_seen: str, last_seen: str) -> int:
    first = _parse_status_recorded_at(first_seen)
    last = _parse_status_recorded_at(last_seen)
    if first is None or last is None:
        return 0
    return max(int((last - first).total_seconds()), 0)


def _analysis_candidate_ready(details: list[str], *, now: datetime | None = None) -> bool:
    status = semantic_analysis_status_from_details(details) or {}
    state = str(status.get("status") or "").strip().lower()
    if state in {"", "pending", "needs_reanalysis"}:
        return True
    if state != "failed":
        return False
    try:
        attempts = int(status.get("attempts") or 1)
    except (TypeError, ValueError):
        attempts = 1
    if attempts >= _MAX_ANALYSIS_ATTEMPTS:
        return False
    recorded_at = _parse_status_recorded_at(status.get("recorded_at"))
    if recorded_at is None:
        return True
    return (now or datetime.now(timezone.utc)) - recorded_at >= _FAILED_ANALYSIS_RETRY_AFTER


def _analysis_candidate_status_filter():
    return or_(
        col(ScreenObservation.details_json).contains('"status":"pending"'),
        col(ScreenObservation.details_json).contains('"status": "pending"'),
        col(ScreenObservation.details_json).contains('\\"status\\":\\"pending\\"'),
        col(ScreenObservation.details_json).contains('\\"status\\": \\"pending\\"'),
        col(ScreenObservation.details_json).contains('"status":"needs_reanalysis"'),
        col(ScreenObservation.details_json).contains('"status": "needs_reanalysis"'),
        col(ScreenObservation.details_json).contains('\\"status\\":\\"needs_reanalysis\\"'),
        col(ScreenObservation.details_json).contains('\\"status\\": \\"needs_reanalysis\\"'),
        col(ScreenObservation.details_json).contains('"status":"failed"'),
        col(ScreenObservation.details_json).contains('"status": "failed"'),
        col(ScreenObservation.details_json).contains('\\"status\\":\\"failed\\"'),
        col(ScreenObservation.details_json).contains('\\"status\\": \\"failed\\"'),
    )


def _parse_status_recorded_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _replace_analysis_details(
    details: list[str],
    *,
    analysis,
    error_reason: str | None,
    status: str = "failed",
) -> list[str]:
    previous_status = semantic_analysis_status_from_details(details) or {}
    try:
        previous_attempts = int(previous_status.get("attempts") or 0)
    except (TypeError, ValueError):
        previous_attempts = 0
    attempts = previous_attempts + 1
    next_details = [
        item
        for item in details
        if not (
            item.startswith("screenshot_analysis:")
            or item.startswith("screenshot_analysis_error:")
            or item.startswith("screenshot_analysis_status:")
        )
    ]
    if analysis is not None:
        next_details.append(screenshot_analysis_detail(analysis))
        next_details.append(screenshot_analysis_status_detail("succeeded"))
    else:
        reason = error_reason or "unknown"
        next_details.append(screenshot_analysis_error_detail(reason))
        next_details.append(screenshot_analysis_status_detail(status, reason=reason, attempts=attempts))
    return next_details


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
