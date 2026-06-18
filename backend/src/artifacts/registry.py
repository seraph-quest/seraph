"""Stable artifact records shared by tools, workflows, and operator surfaces."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from config.settings import settings


def _workspace_root() -> Path:
    return Path(settings.workspace_dir).resolve()


def _safe_workspace_path(file_path: str) -> Path | None:
    if not file_path or not file_path.strip():
        return None
    try:
        resolved = (_workspace_root() / file_path).resolve()
        resolved.relative_to(_workspace_root())
        return resolved
    except Exception:
        return None


def _hash_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def artifact_id_for(
    *,
    file_path: str,
    artifact_type: str,
    producer: str,
    run_id: str | None = None,
    content_sha256: str | None = None,
) -> str:
    seed = "|".join(
        [
            str(producer or "unknown"),
            str(artifact_type or "workspace_file"),
            str(run_id or ""),
            str(file_path or ""),
            str(content_sha256 or ""),
        ]
    )
    return f"art_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


def build_artifact_record(
    *,
    file_path: str,
    artifact_type: str = "workspace_file",
    producer: str = "unknown",
    run_id: str | None = None,
    session_id: str | None = None,
    trust_boundary: str | dict[str, Any] | None = None,
    recovery_hint: str | None = None,
    content: str | bytes | None = None,
) -> dict[str, Any]:
    raw_bytes: bytes | None = None
    resolved = _safe_workspace_path(file_path)
    if content is not None:
        raw_bytes = content if isinstance(content, bytes) else content.encode("utf-8")
    elif resolved is not None and resolved.exists() and resolved.is_file():
        try:
            raw_bytes = resolved.read_bytes()
        except OSError:
            raw_bytes = None

    content_sha256 = _hash_bytes(raw_bytes) if raw_bytes is not None else ""
    size_bytes = len(raw_bytes) if raw_bytes is not None else 0
    artifact_id = artifact_id_for(
        file_path=file_path,
        artifact_type=artifact_type,
        producer=producer,
        run_id=run_id,
        content_sha256=content_sha256,
    )
    return {
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "file_path": file_path,
        "producer": producer,
        "run_id": run_id,
        "session_id": session_id,
        "content_sha256": content_sha256,
        "size_bytes": size_bytes,
        "trust_boundary": trust_boundary or "workspace_write",
        "recovery_hint": recovery_hint or "Use the producer rollback receipt or regenerate from the recorded run inputs.",
        "exists": bool(resolved is not None and resolved.exists()),
    }


def artifact_records_from_paths(
    paths: list[str] | tuple[str, ...],
    *,
    artifact_type: str = "workspace_file",
    producer: str = "workflow",
    run_id: str | None = None,
    session_id: str | None = None,
    trust_boundary: str | dict[str, Any] | None = None,
    recovery_hint: str | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        if not isinstance(path, str) or not path.strip() or path in seen:
            continue
        seen.add(path)
        records.append(
            build_artifact_record(
                file_path=path,
                artifact_type=artifact_type,
                producer=producer,
                run_id=run_id,
                session_id=session_id,
                trust_boundary=trust_boundary,
                recovery_hint=recovery_hint,
            )
        )
    return records

