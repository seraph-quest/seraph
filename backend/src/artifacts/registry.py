"""Stable artifact records shared by tools, workflows, and operator surfaces."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from config.settings import settings
from src.security.trust_contract import (
    EgressClass,
    TrustDecision,
    TrustOperation,
    TrustRequest,
    canonical_digest,
    evaluate_trust,
)
from src.workspace import (
    UnknownWorkspacePathError,
    WorkspaceStateError,
    canonical_workspace_registry,
    canonical_workspace_root,
)


def _workspace_root() -> Path:
    return canonical_workspace_root(settings.workspace_dir)


def _workspace_state(file_path: str) -> dict[str, str | None]:
    """Return registry ownership without exposing the host workspace path."""
    try:
        state_class = canonical_workspace_registry(_workspace_root()).classify_path(file_path)
    except UnknownWorkspacePathError:
        return {"class": None, "status": "unclassified"}
    except WorkspaceStateError:
        return {"class": None, "status": "blocked"}
    return {"class": state_class.value, "status": "classified"}


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


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _legacy_artifact_trust() -> dict[str, Any]:
    return {
        "state": "legacy_unclassified",
        "policy_version": None,
        "egress_class": None,
        "principal": None,
        "decision": None,
        "provenance": [],
    }


def _governed_artifact_trust(
    *,
    content_sha256: str,
    request: TrustRequest | None,
    decision: TrustDecision | None,
) -> dict[str, Any]:
    if request is None or decision is None:
        raise ValueError("Governed artifacts require an authority request and decision")
    if _enum_value(request.operation) != TrustOperation.ARTIFACT_TRANSFER.value:
        raise ValueError("Governed artifact authority must use artifact_transfer")
    if not content_sha256 or request.data_digest != content_sha256:
        raise ValueError("Governed artifact authority must bind the exact content digest")
    try:
        EgressClass(_enum_value(request.egress_class))
    except ValueError as exc:
        raise ValueError("Governed artifacts require a known data egress class") from exc

    evaluated = evaluate_trust(request)
    if decision != evaluated or not evaluated.allowed:
        raise PermissionError(
            "Governed artifact authority was denied "
            f"({evaluated.reason_code})"
        )

    return {
        "state": "governed",
        "policy_version": request.policy_version,
        "egress_class": _enum_value(request.egress_class),
        "principal": {
            "principal_id_digest": canonical_digest(
                {"principal_id": request.principal.principal_id}
            ),
            "principal_type": _enum_value(request.principal.principal_type),
            "authenticated": request.principal.authenticated,
            "revoked": request.principal.revoked,
        },
        "decision": decision.as_dict(),
        "provenance": [
            {
                "origin": _enum_value(item.origin),
                "source_ref_digest": canonical_digest({"source_id": item.source_id}),
                "data_digest_receipt": canonical_digest(
                    {"data_digest": item.data_digest}
                ),
                "egress_class": _enum_value(item.egress_class),
                "instruction_authority": item.instruction_authority,
            }
            for item in request.provenance
        ],
    }


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
    governed: bool = False,
    trust_request: TrustRequest | None = None,
    trust_decision: TrustDecision | None = None,
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
    workspace_state = _workspace_state(file_path)
    artifact_id = artifact_id_for(
        file_path=file_path,
        artifact_type=artifact_type,
        producer=producer,
        run_id=run_id,
        content_sha256=content_sha256,
    )
    if not governed and (trust_request is not None or trust_decision is not None):
        raise ValueError("Trust metadata requires governed=True")
    artifact_trust = (
        _governed_artifact_trust(
            content_sha256=content_sha256,
            request=trust_request,
            decision=trust_decision,
        )
        if governed
        else _legacy_artifact_trust()
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
        "trust": artifact_trust,
        "recovery_hint": recovery_hint or "Use the producer rollback receipt or regenerate from the recorded run inputs.",
        "exists": bool(resolved is not None and resolved.exists()),
        "workspace_state_class": workspace_state["class"],
        "workspace_state_status": workspace_state["status"],
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
