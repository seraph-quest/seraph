"""Framekeeper screenshot-directory ingestion for screen observations."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import col, select

from src.audit.runtime import log_integration_event
from src.db.engine import get_session
from src.db.models import ScreenObservation
from src.observer.screen_repository import screen_observation_repo


class FramekeeperImageError(ValueError):
    """Raised when a Framekeeper screenshot is unsafe or unsupported."""


@dataclass(frozen=True)
class FramekeeperIngestResult:
    scanned: int
    ingested: int
    skipped_duplicates: int
    rejected: list[dict[str, str]]


SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}


async def ingest_framekeeper_root(root: Path, *, limit: int = 100) -> FramekeeperIngestResult:
    """Scan a Framekeeper screenshot directory and persist new images as observations."""
    screenshot_root = root.expanduser().resolve()
    image_paths = _image_paths(screenshot_root, limit=max(limit, 1))
    ingested = 0
    skipped = 0
    rejected: list[dict[str, str]] = []

    for image_path in image_paths:
        try:
            observation = await _image_to_observation(image_path, screenshot_root)
            if observation is None:
                skipped += 1
                continue
            await screen_observation_repo.create(**observation)
            ingested += 1
        except Exception as exc:
            rejected.append({"image_path": str(image_path), "reason": str(exc)})

    await log_integration_event(
        integration_type="framekeeper",
        name="screenshot_ingest",
        outcome="succeeded" if not rejected else "degraded",
        details={
            "root": str(screenshot_root),
            "scanned": len(image_paths),
            "ingested": ingested,
            "skipped_duplicates": skipped,
            "rejected_count": len(rejected),
        },
    )
    return FramekeeperIngestResult(
        scanned=len(image_paths),
        ingested=ingested,
        skipped_duplicates=skipped,
        rejected=rejected,
    )


def _image_paths(root: Path, *, limit: int) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    images = sorted(
        (
            path.resolve()
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return images[:limit]


async def _image_to_observation(image_path: Path, root: Path) -> dict[str, object] | None:
    resolved = image_path.resolve()
    if not resolved.is_relative_to(root):
        raise FramekeeperImageError("image is outside Framekeeper screenshot root")
    if resolved.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
        raise FramekeeperImageError("unsupported image type")
    if not resolved.is_file():
        raise FramekeeperImageError("image file not found")

    image_sha256 = _sha256_file(resolved)
    if await _image_already_ingested(image_sha256):
        return None

    stat = resolved.stat()
    captured_at = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
    capture_id = image_sha256[:16]
    details = [
        f"framekeeper_image_sha256:{image_sha256}",
        "capture_artifacts:"
        + json.dumps(
            {
                "id": capture_id,
                "provider": "framekeeper",
                "source": "framekeeper",
                "created_at": captured_at.isoformat(),
                "artifact_root": str(root),
                "image_path": str(resolved),
                "image_sha256": image_sha256,
                "image_bytes": stat.st_size,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    ]
    return {
        "app_name": "Framekeeper",
        "window_title": resolved.name,
        "activity_type": "screen",
        "project": None,
        "summary": f"Framekeeper screenshot ingested from {resolved.name}.",
        "details": details,
        "blocked": False,
        "timestamp": captured_at,
    }


async def _image_already_ingested(image_sha256: str) -> bool:
    marker = f"framekeeper_image_sha256:{image_sha256}"
    async with get_session() as db:
        result = await db.execute(
            select(ScreenObservation)
            .where(col(ScreenObservation.details_json).contains(marker))
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
