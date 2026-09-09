"""Governed self-evolution API for declarative capability assets."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from config.settings import settings
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.api.capabilities import _require_authenticated_capability_operator
from src.api.chat import (
    _begin_rest_revocation_watch,
    _end_rest_revocation_watch,
    _ensure_rest_authorized,
)
from src.audit.runtime import log_integration_event
from src.auth.cancellation import RuntimeRevokedError, assert_runtime_not_revoked
from src.auth.service import AuthFailure, bind_operator_principal
from src.evolution.engine import (
    EVOLUTION_FILE_NAME_ERROR,
    create_evolution_proposal,
    evaluate_candidate,
    list_evolution_targets,
)
from src.extensions.registry import default_manifest_roots_for_workspace
from src.observer.manager import context_manager
from src.runbooks.manager import runbook_manager
from src.skills.manager import skill_manager
from src.starter_packs.manager import starter_pack_manager

router = APIRouter()

EvolutionTargetType = Literal["skill", "runbook", "starter_pack", "prompt_pack"]

_FILE_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")
_DEFAULT_EXTENSIONS: dict[EvolutionTargetType, str] = {
    "skill": ".md",
    "runbook": ".yaml",
    "starter_pack": ".json",
    "prompt_pack": ".md",
}


class EvolutionProposalRequest(BaseModel):
    target_type: EvolutionTargetType
    source_path: str
    objective: str = ""
    observations: list[str] = Field(default_factory=list)
    file_name: str | None = None


class EvolutionValidationRequest(EvolutionProposalRequest):
    candidate_content: str


def _safe_file_name(file_name: str | None, *, target_type: EvolutionTargetType, source_path: str) -> str | None:
    if not file_name:
        return None
    candidate = file_name.strip()
    normalized = os.path.normpath(candidate)
    if (
        not candidate
        or os.path.isabs(candidate)
        or "/" in candidate
        or "\\" in candidate
        or normalized.startswith("..")
        or os.path.basename(normalized) != normalized
    ):
        raise ValueError(EVOLUTION_FILE_NAME_ERROR)
    stem, ext = os.path.splitext(normalized)
    safe_stem = _FILE_NAME_RE.sub("-", stem).strip("-_.") or Path(source_path).stem
    return f"{safe_stem}{ext or _DEFAULT_EXTENSIONS[target_type]}"


def _ensure_evolution_managers_loaded() -> None:
    manifest_roots = default_manifest_roots_for_workspace(settings.workspace_dir)
    skills_dir = os.path.join(settings.workspace_dir, "skills")
    if (
        not skill_manager._skills_dir
        or skill_manager._skills_dir != skills_dir
        or any(root not in skill_manager._manifest_roots for root in manifest_roots)
    ):
        skill_manager.init(skills_dir, manifest_roots=manifest_roots)

    runbooks_dir = os.path.join(settings.workspace_dir, "runbooks")
    if (
        not runbook_manager.is_initialized()
        or runbook_manager._runbooks_dir != runbooks_dir
        or any(root not in runbook_manager._manifest_roots for root in manifest_roots)
    ):
        runbook_manager.init(runbooks_dir, manifest_roots=manifest_roots)

    starter_legacy_path = os.path.join(settings.workspace_dir, "starter-packs.json")
    if (
        not starter_pack_manager.is_initialized()
        or starter_pack_manager._legacy_path != starter_legacy_path
        or any(root not in starter_pack_manager._manifest_roots for root in manifest_roots)
    ):
        starter_pack_manager.init(starter_legacy_path, manifest_roots=manifest_roots)


def _bind_evolution_operator(request: Request):
    """Bind evolution work to middleware-authenticated operator authority."""
    operator = _require_authenticated_capability_operator(request)
    session_id = operator.session_id
    tokens = set_runtime_context(
        session_id,
        context_manager.get_context().approval_mode,
        trust_principal=bind_operator_principal(operator, session_id),
    )
    return operator, tokens


async def _ensure_evolution_authorized(request: Request, revocation_scope) -> None:
    """Fence one stage with the shared REST watcher and current-session check."""
    try:
        if revocation_scope is None:
            assert_runtime_not_revoked()
        else:
            await _ensure_rest_authorized(request, revocation_scope)
    except (RuntimeRevokedError, AuthFailure) as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "Operator session was revoked."},
        ) from exc


def _reload_evolution_managers_with_authority() -> None:
    """Reload each manager only while the bound operator remains usable."""
    assert_runtime_not_revoked()
    skill_manager.reload()
    assert_runtime_not_revoked()
    runbook_manager.reload()
    assert_runtime_not_revoked()
    starter_pack_manager.reload()
    assert_runtime_not_revoked()


def _redact_evolution_value_error(exc: ValueError) -> str:
    """Keep established safe input errors while hiding parser/path details."""
    message = str(exc)
    if message == EVOLUTION_FILE_NAME_ERROR or re.fullmatch(
        r"(?:skill|runbook|starter_pack|prompt_pack) source must be a registered evolution target",
        message,
    ):
        return message
    return "Evolution candidate is invalid; inspect the authenticated operator receipt."


def _evolution_failure_detail(
    code: str,
    *,
    audit_receipt: dict[str, object] | None = None,
    message: str | None = None,
) -> str | dict[str, object]:
    if audit_receipt is None:
        return message or "Evolution operation failed; inspect the authenticated operator receipt."
    detail: dict[str, object] = {
        "code": code,
        "message": "Evolution operation failed; inspect the authenticated operator receipt.",
    }
    if audit_receipt is not None:
        detail["audit_receipt"] = audit_receipt
    return detail


async def _run_evolution_thread_cancel_safe(func, *args, **kwargs):
    """Wait for synchronous evolution work to finish before propagating cancel."""
    worker = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        try:
            await worker
        except (Exception, asyncio.CancelledError):
            # The caller is already being cancelled. The engine owns rollback
            # for worker failures; do not leave a detached write in flight.
            pass
        raise


async def _close_evolution_request(request: Request, revocation_scope, tokens) -> None:
    """Stop the watcher, fence cleanup, and always clear bound context."""
    try:
        await _end_rest_revocation_watch(revocation_scope)
        if revocation_scope is not None:
            await _ensure_evolution_authorized(request, revocation_scope)
    finally:
        reset_runtime_context(tokens)


def _evolution_degraded_audit_receipt(operator) -> dict[str, object]:
    session_id = str(operator.session_id)
    return {
        "status": "degraded",
        "reason": "audit_persistence_failed",
        "principal_id": str(operator.principal.principal_id),
        "session_id_digest": hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
    }


async def _audit_evolution_event(operator, req: EvolutionProposalRequest, *, outcome: str, receipt=None) -> bool:
    """Persist metadata-only evolution lineage and report explicit degradation."""
    try:
        result = await log_integration_event(
            integration_type="self_evolution",
            name=req.target_type,
            outcome=outcome,
            session_id=operator.session_id,
            actor=operator.principal.principal_id,
            principal_id=operator.principal.principal_id,
            policy_mode="authenticated_operator",
            details=_evolution_audit_details(req, outcome=outcome, receipt=receipt),
        )
    except Exception:
        return False
    return result is not False


def _evolution_audit_details(
    req: EvolutionProposalRequest,
    *,
    outcome: str,
    receipt: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a metadata-only audit detail payload.

    Evolution inputs can contain arbitrary operator text and candidate content.
    Keep audit receipts useful for state transitions without copying any of that
    content, paths, or parser error text into the audit stream.
    """
    details: dict[str, object] = {
        "target_type": req.target_type,
        "outcome": outcome,
        "source_path_digest": hashlib.sha256(req.source_path.encode("utf-8")).hexdigest(),
    }
    if receipt is None:
        return details
    benchmark_gate = receipt.get("benchmark_gate")
    details["receipt"] = {
        "valid": bool(receipt.get("valid")),
        "blocked": bool(receipt.get("blocked")),
        "score": receipt.get("score"),
        "quality_state": receipt.get("quality_state"),
        "constraint_states": [
            {
                "name": item.get("name"),
                "status": item.get("status"),
                "blocked": bool(item.get("blocked")),
            }
            for item in receipt.get("constraints", [])
            if isinstance(item, dict)
        ],
        "benchmark_gate": {
            key: benchmark_gate.get(key)
            for key in (
                "rollout_state",
                "regression_gate",
                "acceptance_state",
                "diversity_guard_state",
                "canary_required",
                "rollback_ready_required",
                "rollback_ready",
                "safety_receipt_state",
            )
            if isinstance(benchmark_gate, dict)
        },
        "saved": bool(receipt.get("saved_path")),
        "receipt_written": bool(receipt.get("receipt_path")),
    }
    return details


@router.get("/evolution/targets")
async def evolution_targets():
    _ensure_evolution_managers_loaded()
    return {"targets": list_evolution_targets()}


@router.post("/evolution/validate")
async def validate_evolution_candidate(req: EvolutionValidationRequest, request: Request):
    try:
        operator, tokens = _bind_evolution_operator(request)
    except (RuntimeRevokedError, AuthFailure) as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "Operator session was revoked."},
        ) from exc
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        await _ensure_evolution_authorized(request, revocation_scope)
        await _run_evolution_thread_cancel_safe(_ensure_evolution_managers_loaded)
        await _ensure_evolution_authorized(request, revocation_scope)
        candidate_file_name = _safe_file_name(
            req.file_name,
            target_type=req.target_type,
            source_path=req.source_path,
        )
        await _ensure_evolution_authorized(request, revocation_scope)
        try:
            receipt = await _run_evolution_thread_cancel_safe(
                evaluate_candidate,
                req.target_type,
                source_path=req.source_path,
                candidate_content=req.candidate_content,
                objective=req.objective,
                observations=req.observations,
                candidate_file_name=candidate_file_name,
            )
        except ValueError as exc:
            await _ensure_evolution_authorized(request, revocation_scope)
            audit_ok = await _audit_evolution_event(operator, req, outcome="failed")
            audit_receipt = None if audit_ok else _evolution_degraded_audit_receipt(operator)
            status_code = 400 if audit_ok else 503
            raise HTTPException(
                status_code=status_code,
                detail=_evolution_failure_detail(
                    "evolution_candidate_invalid",
                    audit_receipt=audit_receipt,
                    message=_redact_evolution_value_error(exc),
                ),
            ) from exc
        await _ensure_evolution_authorized(request, revocation_scope)
        return {"receipt": receipt.to_dict()}
    except HTTPException:
        raise
    except (RuntimeRevokedError, AuthFailure) as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "Operator session was revoked."},
        ) from exc
    except ValueError as exc:
        await _ensure_evolution_authorized(request, revocation_scope)
        audit_ok = await _audit_evolution_event(operator, req, outcome="failed")
        audit_receipt = None if audit_ok else _evolution_degraded_audit_receipt(operator)
        status_code = 400 if audit_ok else 503
        raise HTTPException(
            status_code=status_code,
            detail=_evolution_failure_detail(
                "evolution_candidate_invalid",
                audit_receipt=audit_receipt,
                message=_redact_evolution_value_error(exc),
            ),
        ) from exc
    except Exception as exc:
        await _ensure_evolution_authorized(request, revocation_scope)
        audit_ok = await _audit_evolution_event(operator, req, outcome="failed")
        audit_receipt = None if audit_ok else _evolution_degraded_audit_receipt(operator)
        status_code = 500 if audit_ok else 503
        raise HTTPException(
            status_code=status_code,
            detail=_evolution_failure_detail(
                "evolution_operation_failed",
                audit_receipt=audit_receipt,
            ),
        ) from exc
    finally:
        await _close_evolution_request(request, revocation_scope, tokens)


@router.post("/evolution/proposals")
async def create_governed_evolution_proposal(req: EvolutionProposalRequest, request: Request):
    try:
        operator, tokens = _bind_evolution_operator(request)
    except (RuntimeRevokedError, AuthFailure) as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "Operator session was revoked."},
        ) from exc
    revocation_scope = None
    try:
        revocation_scope = _begin_rest_revocation_watch(request)
        await _ensure_evolution_authorized(request, revocation_scope)
        await _run_evolution_thread_cancel_safe(_ensure_evolution_managers_loaded)
        await _ensure_evolution_authorized(request, revocation_scope)
        candidate_file_name = _safe_file_name(
            req.file_name,
            target_type=req.target_type,
            source_path=req.source_path,
        )
        await _ensure_evolution_authorized(request, revocation_scope)
        try:
            proposal = await _run_evolution_thread_cancel_safe(
                create_evolution_proposal,
                req.target_type,
                source_path=req.source_path,
                objective=req.objective,
                observations=req.observations,
                file_name=candidate_file_name,
                authority_check=assert_runtime_not_revoked,
            )
        except ValueError as exc:
            await _ensure_evolution_authorized(request, revocation_scope)
            audit_ok = await _audit_evolution_event(operator, req, outcome="failed")
            audit_receipt = None if audit_ok else _evolution_degraded_audit_receipt(operator)
            status_code = 400 if audit_ok else 503
            raise HTTPException(
                status_code=status_code,
                detail=_evolution_failure_detail(
                    "evolution_candidate_invalid",
                    audit_receipt=audit_receipt,
                    message=_redact_evolution_value_error(exc),
                ),
            ) from exc

        if proposal["status"] == "saved":
            await _ensure_evolution_authorized(request, revocation_scope)
            await _run_evolution_thread_cancel_safe(_reload_evolution_managers_with_authority)

        await _ensure_evolution_authorized(request, revocation_scope)
        outcome = "succeeded" if proposal["status"] == "saved" else "blocked"
        audit_ok = await _audit_evolution_event(
            operator,
            req,
            outcome=outcome,
            receipt=proposal.get("receipt"),
        )
        await _ensure_evolution_authorized(request, revocation_scope)
        if not audit_ok:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "code": "audit_persistence_failed",
                    "message": "Evolution completed but its authenticated audit receipt could not be persisted.",
                    "audit_receipt": _evolution_degraded_audit_receipt(operator),
                },
            )
        return proposal
    except HTTPException:
        raise
    except (RuntimeRevokedError, AuthFailure) as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "Operator session was revoked."},
        ) from exc
    except ValueError as exc:
        await _ensure_evolution_authorized(request, revocation_scope)
        audit_ok = await _audit_evolution_event(operator, req, outcome="failed")
        audit_receipt = None if audit_ok else _evolution_degraded_audit_receipt(operator)
        status_code = 400 if audit_ok else 503
        raise HTTPException(
            status_code=status_code,
            detail=_evolution_failure_detail(
                "evolution_candidate_invalid",
                audit_receipt=audit_receipt,
                message=_redact_evolution_value_error(exc),
            ),
        ) from exc
    except Exception as exc:
        await _ensure_evolution_authorized(request, revocation_scope)
        audit_ok = await _audit_evolution_event(operator, req, outcome="failed")
        audit_receipt = None if audit_ok else _evolution_degraded_audit_receipt(operator)
        status_code = 500 if audit_ok else 503
        raise HTTPException(
            status_code=status_code,
            detail=_evolution_failure_detail(
                "evolution_operation_failed",
                audit_receipt=audit_receipt,
            ),
        ) from exc
    finally:
        await _close_evolution_request(request, revocation_scope, tokens)
