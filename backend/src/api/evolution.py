"""Governed self-evolution API for declarative capability assets."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config.settings import settings
from src.audit.runtime import log_integration_event
from src.evolution.engine import create_evolution_proposal, evaluate_candidate, list_evolution_targets
from src.extensions.registry import default_manifest_roots_for_workspace
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


@router.get("/evolution/targets")
async def evolution_targets():
    _ensure_evolution_managers_loaded()
    return {"targets": list_evolution_targets()}


@router.post("/evolution/validate")
async def validate_evolution_candidate(req: EvolutionValidationRequest):
    _ensure_evolution_managers_loaded()
    try:
        receipt = evaluate_candidate(
            req.target_type,
            source_path=req.source_path,
            candidate_content=req.candidate_content,
            objective=req.objective,
            observations=req.observations,
            candidate_file_name=_safe_file_name(req.file_name, target_type=req.target_type, source_path=req.source_path),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"receipt": receipt.to_dict()}


@router.post("/evolution/proposals")
async def create_governed_evolution_proposal(req: EvolutionProposalRequest):
    _ensure_evolution_managers_loaded()
    try:
        proposal = create_evolution_proposal(
            req.target_type,
            source_path=req.source_path,
            objective=req.objective,
            observations=req.observations,
            file_name=_safe_file_name(req.file_name, target_type=req.target_type, source_path=req.source_path),
        )
    except ValueError as exc:
        await log_integration_event(
            integration_type="self_evolution",
            name=req.target_type,
            outcome="failed",
            details={"source_path": req.source_path, "error": str(exc)},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if proposal["status"] == "saved":
        skill_manager.reload()
        runbook_manager.reload()
        starter_pack_manager.reload()

    await log_integration_event(
        integration_type="self_evolution",
        name=req.target_type,
        outcome="succeeded" if proposal["status"] == "saved" else "blocked",
        details={
            "source_path": req.source_path,
            "objective": req.objective,
            "observations": req.observations,
            "receipt": proposal["receipt"],
        },
    )
    return proposal
