"""Authenticated explicit document selection. Never generic event content."""
from contextlib import asynccontextmanager
from typing import Literal
from fastapi import APIRouter, HTTPException, Query, Request
from src.api.work_board import _operator, _owner, _raise_board_error
from src.db.engine import get_session
from src.work_board import document_pairs as sources
from src.work_board.documents import (
    CAPABILITY, DocumentReadInput, DocumentService, DocumentSourceReserve, projection,
)
from src.work_board.repository import BoardError
from src.auth.service import AuthFailure


@asynccontextmanager
async def lifespan(app):
    service = DocumentService()
    await service.start()
    app.state.document_service = service
    try:
        yield
    finally:
        await service.stop()
        app.state.document_service = None


router = APIRouter(prefix="/documents", lifespan=lifespan)

from src.work_board.document_preparation import PreparationCreate
from src.work_board.document_build_storage import BuildCreate, BuildPrepare, BuildRetire, BuildSourceSelection


async def _private_build_body(request, model, *, maximum):
    """Bound private JSON before parsing; return field errors without literals."""
    from pydantic import ValidationError
    private_headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
        raise HTTPException(status_code=415, detail={"code": "document_build_json_required"}, headers=private_headers)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw)+len(chunk) > maximum:
            raise HTTPException(status_code=413, detail={"code": "document_build_request_bound"}, headers=private_headers)
        raw.extend(chunk)
    try:
        return model.model_validate_json(bytes(raw))
    except ValidationError as exc:
        errors = []
        for error in exc.errors()[:32]:
            location = list(error["loc"])
            if error["type"] == "extra_forbidden" and location:
                location[-1] = "extra_field"
            projected = {"field": location, "code": error["type"]}
            if error["type"] == "document_formula_invalid":
                import re
                from src.work_board.document_build_formula import FORMULA_ERROR_CODES
                context = error.get("ctx", {})
                sheet, cell, code = context.get("sheet"), context.get("cell"), context.get("formula_code")
                if (isinstance(sheet, str) and 1 <= len(sheet) <= 31
                    and isinstance(cell, str) and re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", cell)
                    and code in FORMULA_ERROR_CODES):
                    projected.update({"code": code, "sheet": sheet, "cell": cell})
            errors.append(projected)
        raise HTTPException(status_code=422, detail={"code": "document_build_fields_invalid", "errors": errors}, headers=private_headers) from None


@router.get("/sources/{identifier}/citations")
async def read_build_citations(request: Request, identifier: str,
    limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0, le=10000)):
    from fastapi.responses import JSONResponse
    from src.work_board.document_build_storage import citations
    operator = _operator(request)
    try:
        async with get_session() as db:
            result = await citations(db, _owner(operator), operator, identifier, limit=limit, offset=offset)
            return JSONResponse(result, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except BoardError as exc:
        _raise_board_error(exc)
    except (ValueError, OSError):
        raise HTTPException(status_code=409, detail={"code": "document_build_source_changed",
            "recovery": "Inspect the original adopted source and readback."}) from None


@router.post("/sources/{identifier}/selection")
async def select_build_source(request: Request, identifier: str):
    from fastapi.responses import JSONResponse
    from src.work_board.document_build_storage import select_source
    operator = _operator(request)
    body = await _private_build_body(request, BuildSourceSelection, maximum=16384)
    try:
        async with get_session() as db:
            result = await select_source(db, _owner(operator), operator, identifier, body)
            return JSONResponse(result, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except BoardError as exc:
        _raise_board_error(exc)
    except (ValueError, OSError):
        raise HTTPException(status_code=409, detail={"code": "document_build_source_changed",
            "recovery": "Read and select the original current private source leaves."}) from None


@router.post("/builds")
async def create_build(request: Request):
    from src.work_board.document_build_storage import create
    operator = _operator(request)
    body = await _private_build_body(request, BuildCreate, maximum=81920)
    try:
        async with get_session() as db:
            return await create(db, _owner(operator), operator, body)
    except BoardError as exc:
        _raise_board_error(exc)
    except (ValueError, OSError):
        raise HTTPException(status_code=409, detail={"code": "document_build_publication_unverified",
            "recovery": "Inspect the original charged build; do not overwrite its private files."}) from None


def _build_descriptor():
    from src.api.work_board import dispatcher
    if dispatcher.general_tasks is None:
        raise BoardError("document_build_unavailable", "Restore the local task service", status_code=503)
    descriptors, _digest = dispatcher.general_tasks.snapshot()
    descriptor = next((item for item in descriptors if item.tool_id == "document_build"), None)
    if descriptor is None:
        raise BoardError("document_build_unavailable", "Restore the fixed local document tool", status_code=503)
    return descriptor


@router.get("/builds")
async def list_builds(request: Request, limit: int = Query(default=50, ge=1, le=50), offset: int = Query(default=0, ge=0, le=10000)):
    from sqlalchemy import select
    from fastapi.responses import JSONResponse
    from src.db.models import WorkBoardInputArtifact
    from src.work_board.document_build_storage import CAPABILITY as build_capability, metadata, projection
    from src.work_board.documents import current_source_root
    from src.work_board.input_artifacts import _metadata_digest
    from src.work_board.pipelines import root_binding
    operator = _operator(request)
    owner = _owner(operator)
    try:
        async with get_session() as db:
            await current_source_root(db, owner, operator)
            rows = list((await db.scalars(select(WorkBoardInputArtifact).where(
                WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
                WorkBoardInputArtifact.owner_session_id == owner.session_id,
                WorkBoardInputArtifact.capability_id == build_capability,
                WorkBoardInputArtifact.document_reserved_bytes > 0)
                .order_by(WorkBoardInputArtifact.created_at, WorkBoardInputArtifact.artifact_id)
                .limit(limit+1).offset(offset))).all())
            retained = []
            for row in rows[:limit]:
                value = metadata(row)
                if value["root"] != dict(root_binding()) or row.metadata_digest != _metadata_digest(row):
                    raise BoardError("document_build_metadata_changed", "Inspect the original private build workspace", status_code=409)
                retained.append(projection(row, value))
            return JSONResponse({"builds": retained, "next_offset": offset+limit if len(rows) > limit else None,
                "no_learning": True}, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except BoardError as exc:
        _raise_board_error(exc)


@router.get("/builds/{identifier}")
async def inspect_build(request: Request, identifier: str):
    from src.work_board.document_build_storage import owned, projection
    try:
        async with get_session() as db:
            row, value = await owned(db, _owner(_operator(request)), identifier)
            return projection(row, value)
    except BoardError as exc:
        _raise_board_error(exc)


@router.get("/builds/{identifier}/preview")
async def preview_build(request: Request, identifier: str):
    from fastapi.responses import JSONResponse
    from src.work_board.document_build_storage import preview
    operator = _operator(request)
    try:
        descriptor = _build_descriptor()
        async with get_session() as db:
            result = await preview(db, _owner(operator), operator, identifier, descriptor=descriptor)
            return JSONResponse(result, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except BoardError as exc:
        _raise_board_error(exc)
    except (ValueError, OSError):
        raise HTTPException(status_code=409, detail={"code": "document_build_private_readback_changed",
            "recovery": "Inspect the original specification and selected source."}) from None


@router.post("/builds/{identifier}/prepare")
async def prepare_build(request: Request, identifier: str):
    from src.api.work_board import dispatcher, _safe_task_payload
    from src.work_board.document_build_storage import prepare
    operator = _operator(request)
    body = await _private_build_body(request, BuildPrepare, maximum=8192)
    try:
        _build_descriptor()
        async with get_session() as db:
            mutation = await prepare(db, _owner(operator), operator, dispatcher.general_tasks, identifier, body)
            return {"task": await _safe_task_payload(mutation.task, db=db), "idempotent_replay": mutation.idempotent_replay}
    except BoardError as exc:
        _raise_board_error(exc)
    except (ValueError, OSError):
        raise HTTPException(status_code=409, detail={"code": "document_build_preparation_changed",
            "recovery": "Inspect the original Work task and reload its private review."}) from None


@router.get("/builds/{identifier}/outputs")
async def read_build_outputs(request: Request, identifier: str):
    from fastapi.responses import JSONResponse
    from src.work_board.document_build_storage import outputs
    operator = _operator(request)
    try:
        async with get_session() as db:
            result = await outputs(db, _owner(operator), operator, identifier)
            return JSONResponse(result, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
    except BoardError as exc:
        _raise_board_error(exc)
    except (ValueError, OSError):
        raise HTTPException(status_code=409, detail={"code": "document_build_output_unverified",
            "recovery": "Inspect the original native output and positive cleanup receipt."}) from None


@router.get("/builds/{identifier}/outputs/{slot}")
async def download_build_output(request: Request, identifier: str, slot: Literal["editable", "pdf"]):
    from fastapi.responses import Response
    from src.work_board.document_build_storage import output_read
    operator = _operator(request)
    try:
        async with get_session() as db:
            raw, media, basename = await output_read(db, _owner(operator), operator, identifier, slot)
            return Response(raw, media_type=media, headers={"Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{basename}"', "X-Content-Type-Options": "nosniff"})
    except BoardError as exc:
        _raise_board_error(exc)
    except (ValueError, OSError):
        raise HTTPException(status_code=409, detail={"code": "document_build_output_unverified",
            "recovery": "Inspect the original physical output; private files remain retained."}) from None


@router.delete("/builds/{identifier}")
async def retire_build(request: Request, identifier: str):
    from src.work_board.document_build_storage import retire
    operator = _operator(request)
    body = await _private_build_body(request, BuildRetire, maximum=2048)
    try:
        async with get_session() as db:
            from src.api.work_board import dispatcher
            return await retire(db, _owner(operator), operator, identifier, body, jobs=dispatcher.jobs)
    except BoardError as exc:
        _raise_board_error(exc)


@router.post("/preparations")
async def propose_preparation(request: Request, body: PreparationCreate):
    from src.api.work_board import dispatcher, _safe_task_payload
    from src.work_board.document_preparation import propose
    operator = _operator(request)
    try:
        if dispatcher.general_tasks is None:
            raise BoardError("document_preparation_unavailable", "Restore the local task service", status_code=503)
        async with get_session() as db:
            mutation = await propose(db, _owner(operator), operator, dispatcher.general_tasks, body)
            return {"task": await _safe_task_payload(mutation.task, db=db), "idempotent_replay": mutation.idempotent_replay}
    except BoardError as exc:
        _raise_board_error(exc)
    except (ValueError, OSError):
        raise HTTPException(status_code=409, detail={"code": "document_preparation_readback_changed", "recovery": "Read and select the exact current private source."}) from None
    except AuthFailure:
        raise HTTPException(status_code=401, detail={"code": "session_revoked", "recovery": "Sign in again and inspect the original private source."}) from None


@router.get("/preparations/{task_id}")
async def read_preparation(request: Request, task_id: str):
    from src.api.work_board import dispatcher
    from src.work_board.document_preparation import private_view
    operator = _operator(request)
    try:
        if dispatcher.general_tasks is None:
            raise BoardError("document_preparation_unavailable", "Restore the local task service", status_code=503)
        async with get_session() as db:
            return await private_view(db, _owner(operator), operator, dispatcher.general_tasks, dispatcher.jobs, task_id)
    except BoardError as exc:
        _raise_board_error(exc)
    except (ValueError, OSError):
        raise HTTPException(status_code=409, detail={"code": "document_preparation_readback_changed", "recovery": "Inspect the original task and private source."}) from None
    except AuthFailure:
        raise HTTPException(status_code=401, detail={"code": "session_revoked", "recovery": "Sign in again and inspect the original private source."}) from None


@router.get("/sources")
async def list_sources(request: Request, limit: int = Query(default=50, ge=1, le=50), offset: int = Query(default=0, ge=0, le=10000)):
    from sqlalchemy import select
    from src.db.models import WorkBoardInputArtifact
    owner = _owner(_operator(request))
    service = getattr(request.app.state, "document_service", None)
    try:
        sources.validate_upload_profile(service._upload_profile if service is not None else None)
        upload_readiness = "ready"
    except BoardError:
        upload_readiness = "blocked"
    async with get_session() as db:
        rows = list((await db.scalars(select(WorkBoardInputArtifact).where(
            WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
            WorkBoardInputArtifact.owner_session_id == owner.session_id,
            WorkBoardInputArtifact.capability_id == CAPABILITY,
            WorkBoardInputArtifact.document_reserved_bytes > 0)
            .order_by(WorkBoardInputArtifact.created_at, WorkBoardInputArtifact.artifact_id)
            .limit(limit+1).offset(offset))).all())
        return {"sources": [projection(row) for row in rows[:limit]],
            "next_offset": offset+limit if len(rows) > limit else None, "no_learning": True,
            "upload_readiness": upload_readiness}


@router.post("/sources")
async def reserve_source(request: Request, body: DocumentSourceReserve):
    try:
        async with get_session() as db:
            result = await sources.reserve(db, _owner(_operator(request)), body)
            row, _ = await sources.owned(db, _owner(_operator(request)), result["artifact_id"], capability=CAPABILITY)
            return projection(row)
    except BoardError as exc:
        _raise_board_error(exc)


@router.get("/sources/{identifier}")
async def inspect_source(request: Request, identifier: str):
    try:
        async with get_session() as db:
            row, _ = await sources.owned(db, _owner(_operator(request)), identifier, capability=CAPABILITY)
            return projection(row)
    except BoardError as exc:
        _raise_board_error(exc)


@router.put("/sources/{identifier}/content")
async def upload_source(request: Request, identifier: str, expected_revision: int = Query(ge=1)):
    if request.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
        raise HTTPException(status_code=415, detail={"code": "document_raw_stream_required"})
    try:
        async with get_session() as db:
            owner = _owner(_operator(request))
            service = getattr(request.app.state, "document_service", None)
            await sources.upload(db, owner, identifier, expected_revision, "source", request.stream(), capability=CAPABILITY,
                upload_profile=service._upload_profile if service is not None else None)
            row, _ = await sources.owned(db, owner, identifier, capability=CAPABILITY)
            return projection(row)
    except BoardError as exc:
        _raise_board_error(exc)


@router.post("/sources/{identifier}/seal")
async def seal_source(request: Request, identifier: str, expected_revision: int = Query(ge=1)):
    try:
        async with get_session() as db:
            owner = _owner(_operator(request))
            await sources.complete(db, owner, identifier, expected_revision, capability=CAPABILITY)
            row, _ = await sources.owned(db, owner, identifier, capability=CAPABILITY)
            return projection(row)
    except BoardError as exc:
        _raise_board_error(exc)


@router.delete("/sources/{identifier}")
async def delete_source(request: Request, identifier: str, expected_revision: int = Query(ge=1)):
    try:
        async with get_session() as db:
            owner = _owner(_operator(request))
            await sources.reset_unbound(db, owner, identifier, expected_revision, retry=False, capability=CAPABILITY)
            row, _ = await sources.owned(db, owner, identifier, capability=CAPABILITY)
            return projection(row)
    except BoardError as exc:
        _raise_board_error(exc)


@router.post("/read")
async def read_document(request: Request, body: DocumentReadInput):
    operator = _operator(request)
    owner = _owner(operator)
    service = getattr(request.app.state, "document_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail={"code": "document_service_inactive"})
    try:
        async with get_session() as db:
            return await service.read(db, owner, body, operator=operator)
    except BoardError as exc:
        _raise_board_error(exc)
    except (ValueError, OSError):
        raise HTTPException(status_code=409, detail={"code": "document_private_readback_failed",
            "recovery": "Inspect the retained source and cleanup state; do not overwrite the immutable source."}) from None
    except AuthFailure as exc:
        raise HTTPException(status_code=401, detail={"code": exc.code,
            "recovery": "Sign in again and inspect the original private source."}) from None


@router.post("/sources/{identifier}/reconcile")
async def reconcile_source_reader(request: Request, identifier: str, expected_revision: int = Query(ge=1)):
    from src.work_board.documents import reconcile_reader
    try:
        async with get_session() as db:
            return await reconcile_reader(db, _owner(_operator(request)), identifier, expected_revision)
    except BoardError as exc:
        _raise_board_error(exc)


@router.post("/sources/{identifier}/reconcile-upload")
async def reconcile_source_upload(request: Request, identifier: str, expected_revision: int = Query(ge=1)):
    try:
        async with get_session() as db:
            owner = _owner(_operator(request))
            await sources.reconcile_upload(db, owner, identifier, expected_revision, capability=CAPABILITY)
            row, _ = await sources.owned(db, owner, identifier, capability=CAPABILITY)
            return projection(row)
    except BoardError as exc:
        _raise_board_error(exc)
