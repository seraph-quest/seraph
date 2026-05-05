from fastapi import APIRouter
from fastapi import HTTPException
from pydantic import BaseModel, Field

from src.memory.benchmark import build_guardian_memory_benchmark_report
from src.memory.control import (
    audit_memory,
    correct_memory,
    forget_memory,
    list_memory_audit_receipts,
    memory_operator_policy_payload,
    pin_memory,
)
from src.memory.decay import summarize_memory_reconciliation_state
from src.memory.providers import list_memory_provider_inventory

router = APIRouter()


class MemoryCorrectionRequest(BaseModel):
    content: str = Field(min_length=1)
    kind: str = "fact"
    summary: str | None = None
    corrects_memory_id: str | None = None
    source_session_id: str | None = None
    actor: str = "operator"
    reason: str | None = None
    confidence: float = 0.95
    importance: float = 0.9
    privacy_boundary: str | None = None
    metadata: dict[str, object] | None = None


class MemoryPinRequest(BaseModel):
    actor: str = "operator"
    reason: str | None = None
    privacy_boundary: str | None = None


class MemoryForgetRequest(BaseModel):
    actor: str = "operator"
    reason: str | None = None
    mode: str = "archive"
    privacy_boundary: str | None = None


class MemoryAuditRequest(BaseModel):
    actor: str = "operator"
    reason: str | None = None


@router.get("/memory/providers")
async def list_memory_providers():
    payload = list_memory_provider_inventory()
    reconciliation = await summarize_memory_reconciliation_state()
    payload["canonical_memory_reconciliation"] = reconciliation
    payload["guardian_memory_benchmark"] = await build_guardian_memory_benchmark_report(
        run_suite=False,
        reconciliation=reconciliation,
    )
    return payload


@router.get("/memory/operator-policy")
async def get_memory_operator_policy():
    return memory_operator_policy_payload()


@router.post("/memory/corrections")
async def create_memory_correction(request: MemoryCorrectionRequest):
    try:
        return await correct_memory(
            content=request.content,
            kind=request.kind,
            summary=request.summary,
            corrects_memory_id=request.corrects_memory_id,
            source_session_id=request.source_session_id,
            actor=request.actor,
            reason=request.reason,
            confidence=request.confidence,
            importance=request.importance,
            privacy_boundary=request.privacy_boundary,
            metadata=request.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/memory/{memory_id}/pin")
async def pin_memory_item(memory_id: str, request: MemoryPinRequest):
    try:
        return await pin_memory(
            memory_id=memory_id,
            actor=request.actor,
            reason=request.reason,
            privacy_boundary=request.privacy_boundary,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/memory/{memory_id}/forget")
async def forget_memory_item(memory_id: str, request: MemoryForgetRequest):
    try:
        return await forget_memory(
            memory_id=memory_id,
            actor=request.actor,
            reason=request.reason,
            mode=request.mode,
            privacy_boundary=request.privacy_boundary,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/memory/{memory_id}/audit")
async def audit_memory_item(memory_id: str, request: MemoryAuditRequest):
    try:
        return await audit_memory(
            memory_id=memory_id,
            actor=request.actor,
            reason=request.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/memory/audit")
async def get_memory_audit(memory_id: str | None = None, limit: int = 20):
    return await list_memory_audit_receipts(memory_id=memory_id, limit=limit)
