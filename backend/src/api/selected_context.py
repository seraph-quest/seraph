"""Closed signed companion ingress and current owned Task attachment controls."""
from __future__ import annotations

import time
from fastapi import APIRouter, Request, HTTPException
from sqlalchemy import select
from src.auth.service import AuthenticatedOperator, AuthFailure
from src.db.models import WorkflowRunState, WorkBoardTask
from src.extensions.state import load_extension_state_payload
from src.extensions.node_adapters import list_node_adapter_inventory
from src.api.nodes import _node_inventory_for_owner
from src.workflows import selected_context_runtime as runtime
from src.workflows.selected_context_contract import (
    BindTarget, Decision, Discard, Metadata, SignedQuery, TicketQuery, Upload,
    SelectedContextError, MAX_ENVELOPE_BYTES, JOB_KIND, parse_body,
    verify_signature, job_id, deny, COMPANION_ORIGIN,
)

router = APIRouter(prefix="/context/selected-text")


def operator(request):
    value = getattr(request.state, "operator", None)
    if not isinstance(value, AuthenticatedOperator):
        raise HTTPException(401, detail={"code": "authentication_required"})
    return value


async def body(request, schema):
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_ENVELOPE_BYTES:
            deny("selected_context_envelope_bound", 413)
        raw.extend(chunk)
    return parse_body(bytes(raw), schema)


async def owned_task(db, owner, task_id):
    await runtime.assert_root(db, runtime.OwnerProof(owner))
    row = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
    if row is None or (row.owner_principal_id, row.owner_session_id) != (owner.principal.principal_id, owner.session_id):
        deny("selected_context_task_unavailable", 404)
    return row


@router.get("/pairings")
async def pairings(request: Request):
    owner = operator(request)
    async with runtime.session() as db:
        await runtime.assert_root(db, runtime.OwnerProof(owner))
    payload = load_extension_state_payload()
    # Existing owner-filtered public inventory; never export private Vault locator.
    entries = _node_inventory_for_owner(owner.principal.principal_id)
    return {"state_revision": int(payload.get("revision", 0)), "companion_origin": COMPANION_ORIGIN,
        "adapter_optional": True, "pairings": [{"extension_id": item.extension_id,
        "reference": item.reference, "name": item.name, "pairing": item.pairing} for item in entries]}


@router.post("/tasks/{task_id}/target")
async def target(request: Request, task_id: str):
    return await runtime.bind_target(operator(request), task_id, await body(request, BindTarget))


@router.get("/tasks/{task_id}/captures")
async def captures(request: Request, task_id: str, cursor: str = ""):
    if len(cursor) > 128:
        deny("selected_context_cursor_invalid", 422)
    owner = operator(request)
    async with runtime.session() as db:
        await owned_task(db, owner, task_id)
        rows = (await db.scalars(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == JOB_KIND,
            WorkflowRunState.owner_principal_id == owner.principal.principal_id,
            WorkflowRunState.operator_session_id == owner.session_id,
            WorkflowRunState.source_task_id == task_id,
            WorkflowRunState.run_identity > cursor).order_by(WorkflowRunState.run_identity).limit(33))).all()
        result = []
        for row in rows[:32]:
            run, checkpoint, metadata = await runtime.run_for_owner(db, owner, row.run_identity, task_id)
            result.append(runtime.metadata_projection(run, checkpoint, metadata))
    return {"captures": result, "next_cursor": rows[31].run_identity if len(rows) == 33 else None}


async def canonical(request, task_id, ident):
    owner = operator(request)
    async with runtime.session() as db:
        await owned_task(db, owner, task_id)
        run, checkpoint, metadata = await runtime.run_for_owner(db, owner, ident, task_id)
    return owner, metadata


@router.get("/tasks/{task_id}/captures/{ident}")
async def receipt(request: Request, task_id: str, ident: str):
    owner, _metadata = await canonical(request, task_id, ident)
    async with runtime.session() as db:
        await runtime.assert_root(db, runtime.OwnerProof(owner))
        run, checkpoint, metadata = await runtime.run_for_owner(db, owner, ident, task_id)
        from src.db.models import ApprovalRequest
        from src.approval.repository import approval_decision_digest
        result = runtime.metadata_projection(run, checkpoint, metadata)
        approval = await db.get(ApprovalRequest, checkpoint.get("approval", {}).get("id"))
        if approval:
            result.update(approval_status=approval.status, approval_decision_digest=approval_decision_digest(approval))
        return result


@router.get("/tasks/{task_id}/captures/{ident}/private")
async def private(request: Request, task_id: str, ident: str):
    owner, metadata = await canonical(request, task_id, ident)
    async with runtime.stage_pair(metadata.pair, operator=owner) as (proof, _secret):
        return await runtime.inspect(proof, ident, task_id=task_id, include_private=True)


@router.post("/tasks/{task_id}/captures/{ident}/decision")
async def decision(request: Request, task_id: str, ident: str):
    owner, metadata = await canonical(request, task_id, ident)
    value = await body(request, Decision)
    async with runtime.stage_pair(metadata.pair, operator=owner) as (proof, _secret):
        return await runtime.decide(proof, ident, value)


@router.post("/tasks/{task_id}/captures/{ident}/discard")
async def discard(request: Request, task_id: str, ident: str):
    owner, _metadata = await canonical(request, task_id, ident)
    return await runtime.discard(runtime.OwnerProof(owner), ident, await body(request, Discard), task_id=task_id)


async def paired(request, schema, action):
    if request.headers.get("origin") != COMPANION_ORIGIN or "cookie" in request.headers:
        deny("selected_context_transport_denied", 403)
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or not 1 <= len(header[7:]) <= 4096:
        deny("selected_context_pair_authentication_denied", 401)
    value = await body(request, schema)
    locator = value.metadata.pair if action == "upload" else value.pair
    signature = request.headers.get("x-seraph-context-mac", "")
    if len(signature) != 64 or any(c not in "0123456789abcdef" for c in signature):
        deny("selected_context_signature_invalid", 403)
    return value, locator, header[7:], signature


@router.post("/paired/{action}")
async def ingress(request: Request, action: str):
    schemas = {"target": SignedQuery, "prepare": Metadata, "ticket": TicketQuery, "upload": Upload}
    if action not in schemas:
        deny("selected_context_route_unavailable", 404)
    value, locator, credential, signature = await paired(request, schemas[action], action)
    async with runtime.stage_pair(locator, credential=credential) as (proof, secret):
        verify_signature(secret, action, value.model_dump(), signature)
        expiry = value.metadata.expires_at if action == "upload" else value.expires_at
        if not int(time.time()) < expiry <= min(int(time.time()) + 120, proof.target.expires_at):
            deny("selected_context_ticket_expired", 410)
        if action == "target":
            async with runtime.writer() as db:
                await runtime.assert_target(db, proof)
            return {"target": proof.target.model_dump(), "no_learning": True, "analysis_eligible": False}
        if action == "prepare":
            return await runtime.prepare(proof, value)
        if action == "ticket":
            return await runtime.inspect(proof, job_id(proof.target.owner_principal_id, value.capture_uuid), task_id=proof.target.task_id)
        return await runtime.upload(proof, value)
