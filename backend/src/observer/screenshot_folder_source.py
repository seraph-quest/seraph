"""Local screenshot-folder scanning for screen observations."""

from __future__ import annotations

import hashlib
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import col, select

from config.settings import settings
from src.audit.runtime import log_integration_event
from src.db.engine import get_session
from src.db.models import ScreenObservation
from src.observer.image_metadata import image_metadata_label, local_image_metadata
from src.observer.screen_repository import screen_observation_repo
from src.observer.screenshot_semantic_analysis import (
    ScreenshotSemanticAnalysisError,
    analyze_screenshot_image,
    screenshot_analysis_detail,
    screenshot_analysis_error_detail,
    screenshot_analysis_status_detail,
)


class ScreenshotFolderImageError(ValueError):
    """Raised when a screenshot-folder image is unsafe or unsupported."""


@dataclass(frozen=True)
class ScreenshotFolderScanResult:
    scanned: int
    ingested: int
    skipped_duplicates: int
    rejected: list[dict[str, str]]


SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
SCREENSHOT_FOLDER_PROVIDER = "screenshot_folder"
SCREENSHOT_FOLDER_HASH_PREFIX = "screenshot_folder_image_sha256"
SCREENSHOT_FOLDER_ENV = "SERAPH_SCREENSHOT_FOLDER"
_SCAN_LOCK = asyncio.Lock()


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

    image_sha256 = await asyncio.to_thread(_sha256_file, resolved)
    if await _image_already_ingested(image_sha256):
        return None

    stat = resolved.stat()
    metadata = await asyncio.to_thread(local_image_metadata, resolved)
    captured_at, captured_at_source = _capture_timestamp(resolved, root)
    relative_path = resolved.relative_to(root).as_posix()
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
    details = [
        f"{SCREENSHOT_FOLDER_HASH_PREFIX}:{image_sha256}",
        "capture_artifacts:" + json.dumps(artifacts, sort_keys=True, separators=(",", ":")),
    ]
    try:
        analysis = await analyze_screenshot_image(resolved, artifacts)
    except ScreenshotSemanticAnalysisError as exc:
        details.append(screenshot_analysis_error_detail(str(exc)))
        details.append(screenshot_analysis_status_detail("failed", reason=str(exc)))
    else:
        if analysis is not None:
            details.append(screenshot_analysis_detail(analysis))
            details.append(screenshot_analysis_status_detail("succeeded"))
        else:
            details.append(screenshot_analysis_status_detail("pending", reason="provider not configured"))
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


async def _image_already_ingested(image_sha256: str) -> bool:
    marker = f"{SCREENSHOT_FOLDER_HASH_PREFIX}:{image_sha256}"
    async with get_session() as db:
        result = await db.execute(
            select(ScreenObservation)
            .where(col(ScreenObservation.details_json).contains(marker))
            .limit(1)
        )
        if result.scalar_one_or_none() is not None:
            return True
    return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
