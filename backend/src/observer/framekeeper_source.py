"""Framekeeper artifact source ingestion for screen observations."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from sqlmodel import col, select

from src.audit.runtime import log_integration_event
from src.db.engine import get_session
from src.db.models import ScreenObservation
from src.observer.screen_repository import screen_observation_repo


class FramekeeperManifestError(ValueError):
    """Raised when a Framekeeper manifest is unsafe or unsupported."""


@dataclass(frozen=True)
class FramekeeperIngestResult:
    scanned: int
    ingested: int
    skipped_duplicates: int
    blocked_events: int
    rejected: list[dict[str, str]]


async def ingest_framekeeper_root(
    root: Path,
    *,
    limit: int = 100,
    analysis_root: Path | None = None,
) -> FramekeeperIngestResult:
    """Scan a Framekeeper artifact root and persist valid manifests as screen observations."""
    artifact_root = root.expanduser().resolve()
    resolved_analysis_root = analysis_root.expanduser().resolve() if analysis_root is not None else None
    manifests = _manifest_paths(artifact_root, limit=max(limit, 1))
    ingested = 0
    skipped = 0
    blocked = 0
    rejected: list[dict[str, str]] = []

    for manifest_path in manifests:
        try:
            manifest = _read_manifest(manifest_path, artifact_root=artifact_root)
            capture_id = _require_text(manifest, "capture_id")
            if await _capture_already_ingested(capture_id):
                skipped += 1
                continue
            observation = _manifest_to_observation(
                manifest_path,
                manifest,
                artifact_root,
                analysis_root=resolved_analysis_root,
            )
            if observation["blocked"]:
                blocked += 1
            await screen_observation_repo.create(**observation)
            ingested += 1
        except Exception as exc:
            rejected.append({"manifest_path": str(manifest_path), "reason": str(exc)})

    await log_integration_event(
        integration_type="framekeeper",
        name="manifest_ingest",
        outcome="succeeded" if not rejected else "degraded",
        details={
            "root": str(artifact_root),
            "scanned": len(manifests),
            "ingested": ingested,
            "skipped_duplicates": skipped,
            "blocked_events": blocked,
            "rejected_count": len(rejected),
        },
    )
    return FramekeeperIngestResult(
        scanned=len(manifests),
        ingested=ingested,
        skipped_duplicates=skipped,
        blocked_events=blocked,
        rejected=rejected,
    )


def _manifest_paths(root: Path, *, limit: int) -> list[Path]:
    captures_root = root / "captures"
    if not captures_root.exists():
        return []
    manifests = sorted(
        (path.resolve() for path in captures_root.rglob("manifest.json") if path.is_file()),
        reverse=True,
    )
    return manifests[:limit]


def _read_manifest(path: Path, *, artifact_root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_relative_to(artifact_root):
        raise FramekeeperManifestError("manifest is outside Framekeeper artifact root")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FramekeeperManifestError("manifest is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise FramekeeperManifestError("manifest must be a JSON object")
    if payload.get("schema_version") != 1:
        raise FramekeeperManifestError("unsupported Framekeeper manifest schema")
    return payload


def _manifest_to_observation(
    manifest_path: Path,
    manifest: dict[str, Any],
    artifact_root: Path,
    *,
    analysis_root: Path | None = None,
) -> dict[str, Any]:
    capture_dir = manifest_path.parent.resolve()
    capture_id = _require_text(manifest, "capture_id")
    captured_at = _parse_capture_time(_require_text(manifest, "captured_at"))
    subject = _require_mapping(manifest, "subject")
    policy = _require_mapping(manifest, "policy")
    artifacts = _require_mapping(manifest, "artifacts")
    blocked = bool(policy.get("blocked"))

    details = [
        f"framekeeper_capture_id:{capture_id}",
        "framekeeper_manifest:" + json.dumps(_safe_manifest_summary(manifest), sort_keys=True),
    ]
    capture_artifacts: dict[str, Any] = {
        "id": capture_id,
        "provider": "framekeeper",
        "source": "framekeeper",
        "created_at": captured_at.isoformat(),
        "manifest_path": str(manifest_path.resolve()),
        "artifact_root": str(artifact_root),
    }

    if not blocked:
        image_path = _safe_artifact_path(capture_dir, artifacts.get("image_path"))
        expected_hash = _require_text(artifacts, "image_sha256")
        actual_hash = _sha256_file(image_path)
        if actual_hash != expected_hash:
            raise FramekeeperManifestError("image sha256 mismatch")
        analysis = _deterministic_analysis(
            manifest_path=manifest_path,
            manifest=manifest,
            image_path=image_path,
            image_sha256=actual_hash,
            captured_at=captured_at,
        )
        capture_artifacts.update(
            {
                "image_path": str(image_path),
                "image_sha256": actual_hash,
                "image_media_type": artifacts.get("image_media_type") or "image/png",
            }
        )
        if analysis_root is not None:
            analysis_paths = _write_analysis_artifacts(
                analysis_root=analysis_root,
                capture_id=capture_id,
                captured_at=captured_at,
                analysis=analysis,
            )
            capture_artifacts.update({key: str(value) for key, value in analysis_paths.items()})

    details.append(
        "capture_artifacts:"
        + json.dumps(capture_artifacts, sort_keys=True, separators=(",", ":"))
    )
    return {
        "app_name": str(subject.get("app_name") or "Unknown App"),
        "window_title": str(subject.get("window_title") or ""),
        "activity_type": "blocked" if blocked else "screen",
        "project": None,
        "summary": _summary_for_manifest(manifest, blocked=blocked),
        "details": details,
        "blocked": blocked,
        "timestamp": captured_at,
    }


def _safe_artifact_path(capture_dir: Path, raw_path: Any) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise FramekeeperManifestError("artifacts.image_path is required")
    candidate = PurePosixPath(raw_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise FramekeeperManifestError("artifact path must be relative and stay inside capture directory")
    resolved = (capture_dir / Path(*candidate.parts)).resolve()
    if not resolved.is_relative_to(capture_dir):
        raise FramekeeperManifestError("artifact path escapes capture directory")
    if not resolved.exists() or not resolved.is_file():
        raise FramekeeperManifestError("artifact image file not found")
    return resolved


async def _capture_already_ingested(capture_id: str) -> bool:
    marker = f"framekeeper_capture_id:{capture_id}"
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


def _deterministic_analysis(
    *,
    manifest_path: Path,
    manifest: dict[str, Any],
    image_path: Path,
    image_sha256: str,
    captured_at: datetime,
) -> dict[str, Any]:
    subject = _require_mapping(manifest, "subject")
    artifacts = _require_mapping(manifest, "artifacts")
    image_size = image_path.stat().st_size
    image_dimensions = _png_dimensions(image_path)
    summary = _summary_for_manifest(manifest, blocked=False)
    return {
        "provider": "framekeeper",
        "analysis_provider": "deterministic-local",
        "analysis_type": "framekeeper_manifest_image_metadata",
        "summary": summary,
        "activity": "screen",
        "project": None,
        "captured_at": captured_at.isoformat(),
        "app_name": str(subject.get("app_name") or "Unknown App"),
        "window_title": str(subject.get("window_title") or ""),
        "image": {
            "sha256": image_sha256,
            "size_bytes": image_size,
            "media_type": artifacts.get("image_media_type") or "image/png",
            "dimensions": image_dimensions,
        },
        "manifest_path": str(manifest_path.resolve()),
        "privacy": {
            "raw_screenshot_owner": "framekeeper",
            "cloud_analysis_performed": False,
        },
    }


def _write_analysis_artifacts(
    *,
    analysis_root: Path,
    capture_id: str,
    captured_at: datetime,
    analysis: dict[str, Any],
) -> dict[str, Path]:
    safe_capture_id = _safe_filename(capture_id)
    date_dir = analysis_root / captured_at.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    analysis_path = date_dir / f"{safe_capture_id}.analysis.json"
    provider_output_path = date_dir / f"{safe_capture_id}.provider.txt"
    _atomic_write_text(
        analysis_path,
        json.dumps(analysis, indent=2, sort_keys=True) + "\n",
    )
    _atomic_write_text(
        provider_output_path,
        (
            f"{analysis['summary']}\n"
            f"Image SHA-256: {analysis['image']['sha256']}\n"
            f"Analysis provider: {analysis['analysis_provider']}\n"
            "Raw screenshot remains in Framekeeper's local artifact root.\n"
        ),
    )
    return {
        "analysis_path": analysis_path,
        "provider_output_path": provider_output_path,
    }


def _atomic_write_text(path: Path, content: str) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.chmod(0o600)
    tmp_path.replace(path)
    path.chmod(0o600)


def _safe_filename(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{safe or 'capture'}-{digest}"


def _png_dimensions(path: Path) -> dict[str, int] | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    return {
        "width": int.from_bytes(header[16:20], "big"),
        "height": int.from_bytes(header[20:24], "big"),
    }


def _parse_capture_time(value: str) -> datetime:
    if value.startswith("unix:"):
        try:
            return datetime.fromtimestamp(float(value.removeprefix("unix:")), tz=timezone.utc)
        except ValueError as exc:
            raise FramekeeperManifestError("captured_at unix timestamp is invalid") from exc
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FramekeeperManifestError("captured_at timestamp is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _summary_for_manifest(manifest: dict[str, Any], *, blocked: bool) -> str:
    subject = _require_mapping(manifest, "subject")
    app_name = str(subject.get("app_name") or "Unknown App")
    window_title = str(subject.get("window_title") or "").strip()
    if blocked:
        return f"Framekeeper blocked capture for {app_name}."
    if window_title:
        return f"Framekeeper captured {app_name}: {window_title}."
    return f"Framekeeper captured {app_name}."


def _safe_manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": manifest.get("schema_version"),
        "capture_id": manifest.get("capture_id"),
        "captured_at": manifest.get("captured_at"),
        "platform": manifest.get("platform"),
        "reason": manifest.get("reason"),
        "mode": manifest.get("mode"),
        "policy": manifest.get("policy"),
    }


def _require_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FramekeeperManifestError(f"{key} is required")
    return value


def _require_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise FramekeeperManifestError(f"{key} is required")
    return value
