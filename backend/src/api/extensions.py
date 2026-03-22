"""Extension lifecycle API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await log_integration_event(
        integration_type="extension",
        name=extension_id,
        outcome="succeeded",
        details={
            "status": "source_saved",
            "reference": req.reference,
        },
    )
    return payload


@router.post("/extensions/validate")
async def validate_extension_package_path(req: ExtensionPathRequest):
    try:
        return validate_extension_path(req.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/extensions/install", status_code=201)
async def install_extension_package(req: ExtensionPathRequest):
    try:
        extension = install_extension_path(req.path)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await log_integration_event(
        integration_type="extension",
        name=extension["id"],
        outcome="succeeded",
        details={
            "status": "installed",
            "path": req.path,
            "location": extension["location"],
        },
    )
    return {"status": "installed", "extension": extension}


@router.post("/extensions/{extension_id}/enable")
async def enable_extension_package(extension_id: str):
    try:
        result = enable_extension(extension_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    await log_integration_event(
        integration_type="extension",
        name=extension_id,
        outcome="succeeded",
        details={
            "status": "enabled",
            "changed": result["changed"],
        },
    )
    return {"status": "enabled", **result}


@router.post("/extensions/{extension_id}/disable")
async def disable_extension_package(extension_id: str):
    try:
        result = disable_extension(extension_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    await log_integration_event(
        integration_type="extension",
        name=extension_id,
        outcome="succeeded",
        details={
            "status": "disabled",
            "changed": result["changed"],
        },
    )
    return {"status": "disabled", **result}


@router.post("/extensions/{extension_id}/configure")
async def configure_extension_package(extension_id: str, req: ExtensionConfigRequest):
    try:
        extension = configure_extension(extension_id, req.config)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await log_integration_event(
        integration_type="extension",
        name=extension_id,
        outcome="succeeded",
        details={
            "status": "configured",
            "config_keys": sorted(req.config.keys()),
        },
    )
    return {"status": "configured", "extension": extension}


@router.delete("/extensions/{extension_id}")
async def remove_extension_package(extension_id: str):
    try:
        remove_extension(extension_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Extension '{extension_id}' not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await log_integration_event(
        integration_type="extension",
        name=extension_id,
        outcome="succeeded",
        details={"status": "removed"},
    )
    return {"status": "removed", "name": extension_id}
