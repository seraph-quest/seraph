"""Governed self-evolution API for declarative capability assets."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from config.settings import settings
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.api.capabilities import _require_authenticated_capability_operator
from src.audit.runtime import log_integration_event
from src.auth.cancellation import assert_runtime_not_revoked
from src.auth.service import bind_operator_principal
from src.evolution.engine import create_evolution_proposal, evaluate_candidate, list_evolution_targets
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
        or normalized.startswith("..")
        or os.path.basename(normalized) != normalized
    ):
        raise HTTPException(status_code=400, detail="Candidate file name must stay within the managed workspace package")
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
    return tokens


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
            key: receipt.get("benchmark_gate", {}).get(key)
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
            if isinstance(receipt.get("benchmark_gate"), dict)
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
    tokens = _bind_evolution_operator(request)
    try:
        assert_runtime_not_revoked()
        _ensure_evolution_managers_loaded()
        assert_runtime_not_revoked()
        try:
            receipt = evaluate_candidate(
                req.target_type,
                source_path=req.source_path,
                candidate_content=req.candidate_content,
                objective=req.objective,
                observations=req.observations,
                candidate_file_name=_safe_file_name(
                    req.file_name,
                    target_type=req.target_type,
                    source_path=req.source_path,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"receipt": receipt.to_dict()}
    finally:
        reset_runtime_context(tokens)


@router.post("/evolution/proposals")
async def create_governed_evolution_proposal(req: EvolutionProposalRequest, request: Request):
    tokens = _bind_evolution_operator(request)
    try:
        assert_runtime_not_revoked()
        _ensure_evolution_managers_loaded()
        assert_runtime_not_revoked()
        try:
            proposal = create_evolution_proposal(
                req.target_type,
                source_path=req.source_path,
                objective=req.objective,
                observations=req.observations,
                file_name=_safe_file_name(
                    req.file_name,
                    target_type=req.target_type,
                    source_path=req.source_path,
                ),
            )
        except ValueError as exc:
            assert_runtime_not_revoked()
            await log_integration_event(
                integration_type="self_evolution",
                name=req.target_type,
                outcome="failed",
                details=_evolution_audit_details(req, outcome="failed"),
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if proposal["status"] == "saved":
            assert_runtime_not_revoked()
            skill_manager.reload()
            runbook_manager.reload()
            starter_pack_manager.reload()

        assert_runtime_not_revoked()
        outcome = "succeeded" if proposal["status"] == "saved" else "blocked"
        await log_integration_event(
            integration_type="self_evolution",
            name=req.target_type,
            outcome=outcome,
            details=_evolution_audit_details(req, outcome=outcome, receipt=proposal.get("receipt")),
        )
        return proposal
    finally:
        reset_runtime_context(tokens)
