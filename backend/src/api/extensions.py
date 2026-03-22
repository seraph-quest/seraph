"""Extension lifecycle API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.approval.repository import approval_repository, fingerprint_tool_call
from src.audit.runtime import log_integration_event
from src.extensions.lifecycle import (
    configure_extension,
    disable_extension,
    enable_extension,
    get_extension,
    get_extension_source,
    install_extension_path,
    list_extensions,
    remove_extension,
    save_extension_source,
    validate_extension_path,
)

router = APIRouter()


class ExtensionPathRequest(BaseModel):
    path: str


class ExtensionConfigRequest(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class ExtensionSourceSaveRequest(BaseModel):
    reference: str
    content: str


def _extension_issue_count(preview: dict[str, Any] | None) -> int:
    if not isinstance(preview, dict):
        return 0
    issues = preview.get("issues")
    if isinstance(issues, list):
        return len(issues)
    results = preview.get("results")
    if isinstance(results, list):
        return sum(
            len(result.get("issues", []))
            for result in results
            if isinstance(result, dict) and isinstance(result.get("issues"), list)
        )
    return 0


def _extension_load_error_count(preview: dict[str, Any] | None) -> int:
    if not isinstance(preview, dict):
        return 0
    load_errors = preview.get("load_errors")
    return len(load_errors) if isinstance(load_errors, list) else 0


async def _log_extension_lifecycle_event(
    *,
    action: str,
    outcome: str,
    preview: dict[str, Any] | None = None,
    path: str | None = None,
    error: str | None = None,
    extra_details: dict[str, Any] | None = None,
) -> None:
    preview = preview if isinstance(preview, dict) else {}
    permission_summary = preview.get("permission_summary")
    permission_status = (
        str(permission_summary.get("status"))
        if isinstance(permission_summary, dict) and permission_summary.get("status") is not None
        else None
    )
    extension_id = str(preview.get("id") or preview.get("extension_id") or "")
    display_name = str(preview.get("display_name") or extension_id or Path(path or "extension").name or "extension")
    details = {
        "action": action,
        "status": f"{action}_{outcome}" if outcome == "failed" else (
            "validated" if action == "validate"
            else "source_saved" if action == "save_source"
            else "installed" if action == "install"
            else "enabled" if action == "enable"
            else "disabled" if action == "disable"
            else "configured" if action == "configure"
            else "removed" if action == "remove"
            else action
        ),
        "path": preview.get("path") or path,
        "manifest_path": preview.get("manifest_path"),
        "extension_id": extension_id or None,
        "extension_display_name": display_name,
        "version": preview.get("version"),
        "kind": preview.get("kind"),
        "trust": preview.get("trust"),
        "location": preview.get("location"),
        "package_digest": preview.get("package_digest"),
        "permission_status": permission_status,
        "issue_count": _extension_issue_count(preview),
        "load_error_count": _extension_load_error_count(preview),
        "extension_status": preview.get("status"),
        "ok": preview.get("ok"),
        "error": error,
        **(extra_details or {}),
    }
    await log_integration_event(
        integration_type="extension",
        name=extension_id or display_name,
        outcome=outcome,
        details={key: value for key, value in details.items() if value is not None},
    )


async def _require_extension_lifecycle_approval(action: str, preview: dict[str, Any]) -> None:
    approval_profile = preview.get("approval_profile")
    if not isinstance(approval_profile, dict) or not approval_profile.get("requires_lifecycle_approval"):
        return

    extension_id = str(preview.get("id") or preview.get("extension_id") or "")
    display_name = str(preview.get("display_name") or extension_id or "extension")
    tool_name = f"extension_{action}"
    lifecycle_boundaries = [
        str(boundary)
        for boundary in approval_profile.get("lifecycle_boundaries", [])
        if isinstance(boundary, str) and boundary.strip()
    ]
    arguments = {
        "extension_id": extension_id,
        "version": preview.get("version"),
        "package_path": preview.get("root_path") or preview.get("path"),
        "package_digest": preview.get("package_digest"),
        "boundaries": lifecycle_boundaries,
        "permissions": preview.get("permissions"),
    }
    fingerprint = fingerprint_tool_call(tool_name, arguments)
    if await approval_repository.consume_approved(
        session_id=None,
        tool_name=tool_name,
        fingerprint=fingerprint,
    ):
        return

    summary = (
        f"{action.replace('_', ' ').title()} extension '{display_name}' "
        f"with access to {', '.join(lifecycle_boundaries) or 'high-risk capabilities'}"
    )
    request = await approval_repository.get_or_create_pending(
        session_id=None,
        tool_name=tool_name,
        risk_level=str(approval_profile.get("risk_level") or "high"),
        summary=summary,
        fingerprint=fingerprint,
        details={
            "extension_id": extension_id,
            "extension_display_name": display_name,
            "action": action,
            "package_path": preview.get("root_path") or preview.get("path"),
            "package_digest": preview.get("package_digest"),
            "permissions": preview.get("permissions"),
            "approval_profile": approval_profile,
        },
    )
    raise HTTPException(
        status_code=409,
        detail={
            "type": "approval_required",
            "approval_id": request.id,
            "tool_name": tool_name,
            "risk_level": request.risk_level,
            "message": (
                f"{summary}\n\n"
                "Approve it first, then retry the extension action."
            ),
        },
    )


@router.get("/extensions")
async def list_extension_packages():
    return list_extensions()


@router.get("/extensions/{extension_id}")
async def get_extension_package(extension_id: str):
    try:
        return {"extension": get_extension(extension_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc


@router.get("/extensions/{extension_id}/source")
async def get_extension_package_source(extension_id: str, reference: str):
    try:
        return get_extension_source(extension_id, reference)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/extensions/{extension_id}/source")
async def save_extension_package_source(extension_id: str, req: ExtensionSourceSaveRequest):
    try:
        payload = save_extension_source(extension_id, req.reference, req.content)
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="save_source",
            outcome="failed",
            path=extension_id,
            error=f"Extension '{extension_id}' not found",
            extra_details={"reference": req.reference},
        )
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="save_source",
            outcome="failed",
            path=extension_id,
            error=str(exc),
            extra_details={"reference": req.reference},
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _log_extension_lifecycle_event(
        action="save_source",
        outcome="succeeded",
        preview=payload.get("extension"),
        path=extension_id,
        extra_details={"reference": req.reference},
    )
    return payload


@router.post("/extensions/validate")
async def validate_extension_package_path(req: ExtensionPathRequest):
    try:
        payload = validate_extension_path(req.path)
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="validate",
            outcome="failed",
            path=req.path,
            error=str(exc),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _log_extension_lifecycle_event(
        action="validate",
        outcome="succeeded",
        preview=payload,
        path=req.path,
    )
    return payload


@router.post("/extensions/install", status_code=201)
async def install_extension_package(req: ExtensionPathRequest):
    preview: dict[str, Any] | None = None
    try:
        preview = validate_extension_path(req.path)
        if not preview.get("ok", False):
            raise ValueError("extension package failed validation")
        await _require_extension_lifecycle_approval("install", preview)
        extension = install_extension_path(req.path)
    except FileExistsError as exc:
        await _log_extension_lifecycle_event(
            action="install",
            outcome="failed",
            preview=preview,
            path=req.path,
            error=str(exc),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="install",
            outcome="failed",
            preview=preview,
            path=req.path,
            error=str(exc),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _log_extension_lifecycle_event(
        action="install",
        outcome="succeeded",
        preview=extension,
        path=req.path,
        extra_details={
            "location": extension.get("location"),
        },
    )
    return {"status": "installed", "extension": extension}


@router.post("/extensions/{extension_id}/enable")
async def enable_extension_package(extension_id: str):
    preview: dict[str, Any] | None = None
    try:
        preview = get_extension(extension_id)
        if preview.get("status") != "ready":
            raise ValueError(
                f"extension '{extension_id}' is degraded and cannot be enabled until validation issues are fixed"
            )
        await _require_extension_lifecycle_approval("enable", preview)
        result = enable_extension(extension_id)
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="enable",
            outcome="failed",
            path=extension_id,
            error=f"Extension '{extension_id}' not found",
        )
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="enable",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=str(exc),
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _log_extension_lifecycle_event(
        action="enable",
        outcome="succeeded",
        preview=result.get("extension"),
        path=extension_id,
        extra_details={
            "changed": result["changed"],
            "changed_count": len(result.get("changed", [])),
        },
    )
    return {"status": "enabled", **result}


@router.post("/extensions/{extension_id}/disable")
async def disable_extension_package(extension_id: str):
    try:
        result = disable_extension(extension_id)
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="disable",
            outcome="failed",
            path=extension_id,
            error=f"Extension '{extension_id}' not found",
        )
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    await _log_extension_lifecycle_event(
        action="disable",
        outcome="succeeded",
        preview=result.get("extension"),
        path=extension_id,
        extra_details={
            "changed": result["changed"],
            "changed_count": len(result.get("changed", [])),
        },
    )
    return {"status": "disabled", **result}


@router.post("/extensions/{extension_id}/configure")
async def configure_extension_package(extension_id: str, req: ExtensionConfigRequest):
    try:
        extension = configure_extension(extension_id, req.config)
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="configure",
            outcome="failed",
            path=extension_id,
            error=f"Extension '{extension_id}' not found",
            extra_details={"config_keys": sorted(req.config.keys())},
        )
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="configure",
            outcome="failed",
            path=extension_id,
            error=str(exc),
            extra_details={"config_keys": sorted(req.config.keys())},
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _log_extension_lifecycle_event(
        action="configure",
        outcome="succeeded",
        preview=extension,
        path=extension_id,
        extra_details={"config_keys": sorted(req.config.keys())},
    )
    return {"status": "configured", "extension": extension}


@router.delete("/extensions/{extension_id}")
async def remove_extension_package(extension_id: str):
    preview: dict[str, Any] | None = None
    try:
        preview = get_extension(extension_id)
        remove_extension(extension_id)
    except KeyError as exc:
        await _log_extension_lifecycle_event(
            action="remove",
            outcome="failed",
            path=extension_id,
            error=f"Extension '{extension_id}' not found",
        )
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        await _log_extension_lifecycle_event(
            action="remove",
            outcome="failed",
            preview=preview,
            path=extension_id,
            error=str(exc),
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await _log_extension_lifecycle_event(
        action="remove",
        outcome="succeeded",
        preview=preview,
        path=extension_id,
    )
    return {"status": "removed", "name": extension_id}
