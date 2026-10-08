"""Authenticated explicit document selection. Never generic event content."""
from contextlib import asynccontextmanager
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


@router.get("/sources")
async def list_sources(request: Request, limit: int = Query(default=50, ge=1, le=50), offset: int = Query(default=0, ge=0, le=10000)):
    from sqlalchemy import select
    from src.db.models import WorkBoardInputArtifact
    owner = _owner(_operator(request))
    async with get_session() as db:
        rows = list((await db.scalars(select(WorkBoardInputArtifact).where(
            WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
            WorkBoardInputArtifact.owner_session_id == owner.session_id,
            WorkBoardInputArtifact.capability_id == CAPABILITY,
            WorkBoardInputArtifact.document_reserved_bytes > 0)
            .order_by(WorkBoardInputArtifact.created_at, WorkBoardInputArtifact.artifact_id)
            .limit(limit+1).offset(offset))).all())
        return {"sources": [projection(row) for row in rows[:limit]],
            "next_offset": offset+limit if len(rows) > limit else None, "no_learning": True}


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
            await sources.upload(db, owner, identifier, expected_revision, "source", request.stream(), capability=CAPABILITY)
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
