"""Private task evidence inspection; generic task/event projections stay safe."""
from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy.exc import SQLAlchemyError

from src.api.work_board import _operator, _owner, _raise_board_error
from src.db.engine import get_session
from src.memory.evidence_working_set import (
    EvidenceRequest, EvidenceExclusionRequest, EvidenceAdoptionRequest, EvidencePacketResponse,
    adopt_evidence, read_evidence, refresh_evidence,
)
from src.work_board.repository import BoardError
from src.memory.evidence_execution import (
    ExecutionPreviewRequest, ExecutionAcceptRequest, preview_execution, accept_execution, inspect_execution,
)

router = APIRouter(prefix="/work-board/tasks")


async def _call(request, task_id, body=None):
    operator = _operator(request)
    owner = _owner(operator)
    try:
        async with get_session() as db:
            if isinstance(body, EvidenceAdoptionRequest):
                return await adopt_evidence(db, owner, task_id, body, operator=operator)
            return await (read_evidence(db, owner, task_id, operator=operator) if body is None else
                          refresh_evidence(db, owner, task_id, body, operator=operator))
    except BoardError as exc:
        _raise_board_error(exc)
    except (SQLAlchemyError, OSError) as exc:
        raise HTTPException(status_code=503, detail={"code": "evidence_storage_unavailable",
            "recovery": "Restore the canonical workspace/database and retry."}) from exc


@router.get("/{task_id}/evidence", response_model=EvidencePacketResponse)
async def get_task_evidence(request: Request, task_id: str):
    return await _call(request, task_id)


@router.post("/{task_id}/evidence", response_model=EvidencePacketResponse)
async def refresh_task_evidence(request: Request, task_id: str, body: EvidenceRequest):
    return await _call(request, task_id, body)


@router.patch("/{task_id}/evidence", response_model=EvidencePacketResponse)
async def exclude_task_evidence(request: Request, task_id: str, body: EvidenceExclusionRequest):
    return await _call(request, task_id, body)


@router.get("/{task_id}/evidence/sources/{source_id}")
async def inspect_task_evidence_source(request: Request, task_id: str, source_id: str):
    packet = await _call(request, task_id)
    claims = [claim for claim in packet["claims"] if claim["source_id"] == source_id]
    if not claims:
        raise HTTPException(status_code=404, detail={"code": "evidence_source_unavailable"})
    return {"source_id": source_id, "revision": packet["revision"], "digest": packet["digest"],
            "claims": claims, "memory_status": "no_learning"}


@router.post("/{task_id}/evidence/adoption", response_model=EvidencePacketResponse)
async def adopt_task_evidence(request: Request, task_id: str, body: EvidenceAdoptionRequest):
    return await _call(request, task_id, body)


@router.post('/{task_id}/evidence/execution-preview')
async def preview_task_execution_evidence(request: Request, task_id: str, body: ExecutionPreviewRequest):
    operator = _operator(request)
    try:
        async with get_session() as db:
            return await preview_execution(db, _owner(operator), task_id, body, operator=operator)
    except BoardError as exc:
        _raise_board_error(exc)


@router.post('/{task_id}/evidence/execution-binding')
async def bind_task_execution_evidence(request: Request, task_id: str, body: ExecutionAcceptRequest):
    operator = _operator(request)
    try:
        async with get_session() as db:
            return await accept_execution(db, _owner(operator), task_id, body, operator=operator)
    except BoardError as exc:
        _raise_board_error(exc)


@router.get('/{task_id}/evidence/execution-binding')
async def inspect_task_execution_evidence(request: Request, task_id: str,
    pending_request: str | None = Query(default=None, max_length=2048)):
    operator = _operator(request)
    try:
        pending = None if pending_request is None else ExecutionAcceptRequest.model_validate_json(pending_request)
        async with get_session() as db:
            return await inspect_execution(db, _owner(operator), task_id, pending=pending, operator=operator)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={'code': 'evidence_request_invalid'}) from exc
    except BoardError as exc:
        _raise_board_error(exc)
