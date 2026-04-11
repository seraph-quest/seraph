"""Governed self-evolution for declarative workspace capability assets."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Literal

import yaml

from config.settings import settings
from src.extensions.capability_contributions import parse_prompt_pack_definition
from src.extensions.manifest import load_extension_manifest
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.workspace_package import save_workspace_contribution, workspace_capability_package_root
from src.evals.benchmark_catalog import benchmark_suite_names
from src.native_tools.registry import TOOL_METADATA
from src.runbooks.loader import Runbook, parse_runbook_content
from src.runbooks.manager import runbook_manager
from src.skills.loader import Skill, parse_skill_content
from src.skills.manager import skill_manager
from src.starter_packs.loader import StarterPack, parse_starter_pack_payload
from src.starter_packs.manager import starter_pack_manager

EvolutionTargetType = Literal["skill", "runbook", "starter_pack", "prompt_pack"]

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CANDIDATE_SUFFIX = "-review-candidate"
_DANGEROUS_PROMPT_TOKENS = (
    "secret_ref",
    "vault://",
    "get_secret",
    "store_secret",
    "execute_code",
    "run_command",
    "start_process",
)
_PRIVILEGED_PROMPT_BOUNDARIES = {
    "secret_management",
    "secret_read",
    "secret_injection",
    "sandbox_execution",
    "container_process_execution",
    "container_process_management",
    "container_process_read",
    "authenticated_external_source",
    "connector_mutation",
    "external_mcp",
}
_PREFERENCE_COLLAPSE_TOKENS = (
    "ignore user preferences",
    "ignore user-specific preferences",
    "always use the default workflow",
    "same response for every user",
    "standardize across all users",
    "do not personalize",
    "one-size-fits-all",
    "regardless of user preference",
)


@dataclass(frozen=True)
class EvolutionConstraint:
    name: str
    status: str
    blocked: bool
    summary: str
    details: dict[str, Any]


@dataclass(frozen=True)
class EvolutionReceipt:
    target_type: EvolutionTargetType
    source_path: str
    source_name: str
    candidate_name: str
    candidate_file_name: str
    valid: bool
    blocked: bool
    score: float
    quality_state: str
    objective: str
    observations: tuple[str, ...]
    constraints: tuple[EvolutionConstraint, ...]
    evals: tuple[dict[str, Any], ...]
    change_summary: tuple[str, ...]
    review_risks: tuple[str, ...]
    benchmark_gate: dict[str, Any]
    pr_draft: dict[str, str]
    saved_path: str | None = None
    receipt_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["constraints"] = [asdict(item) for item in self.constraints]
        payload["evals"] = [dict(item) for item in self.evals]
        return payload


def _workspace_manifest_roots() -> list[str]:
    return default_manifest_roots_for_workspace(settings.workspace_dir)


def _ensure_target_catalog_loaded() -> None:
    manifest_roots = _workspace_manifest_roots()
    skills_dir = str(Path(settings.workspace_dir) / "skills")
    if (
        not skill_manager._skills_dir
        or skill_manager._skills_dir != skills_dir
        or any(root not in skill_manager._manifest_roots for root in manifest_roots)
    ):
        skill_manager.init(skills_dir, manifest_roots=manifest_roots)

    runbooks_dir = str(Path(settings.workspace_dir) / "runbooks")
    if (
        not runbook_manager.is_initialized()
        or runbook_manager._runbooks_dir != runbooks_dir
        or any(root not in runbook_manager._manifest_roots for root in manifest_roots)
    ):
        runbook_manager.init(runbooks_dir, manifest_roots=manifest_roots)

    starter_legacy_path = str(Path(settings.workspace_dir) / "starter-packs.json")
    if (
        not starter_pack_manager.is_initialized()
        or starter_pack_manager._legacy_path != starter_legacy_path
        or any(root not in starter_pack_manager._manifest_roots for root in manifest_roots)
    ):
        starter_pack_manager.init(starter_legacy_path, manifest_roots=manifest_roots)


def _normalize_observations(observations: list[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in observations or []:
        item = str(value or "").strip()
        if item and item not in normalized:
            normalized.append(item)
    return tuple(normalized)


def _safe_readable_source_path(source_path: str) -> Path:
    candidate = Path(source_path).expanduser()
    if not candidate.is_absolute():
        candidate = (_REPO_ROOT / candidate).resolve()
    else:
        candidate = candidate.resolve()
    allowed_roots = {
        _REPO_ROOT.resolve(),
        Path(settings.workspace_dir).resolve(),
    }
    if not candidate.exists():
        raise ValueError(f"source path does not exist: {source_path}")
    if not any(root == candidate or root in candidate.parents for root in allowed_roots):
        raise ValueError("source path must stay within the repository or workspace")
    return candidate


def _resolve_registered_target_path(target_type: EvolutionTargetType, source_path: str) -> Path:
    _ensure_target_catalog_loaded()
    resolved = _safe_readable_source_path(source_path)
    for target in list_evolution_targets():
        if target.get("target_type") != target_type:
            continue
        candidate_path = target.get("source_path")
        if not isinstance(candidate_path, str) or not candidate_path.strip():
            continue
        try:
            registered_path = _safe_readable_source_path(candidate_path)
        except ValueError:
            continue
        if registered_path == resolved:
            return resolved
    raise ValueError(f"{target_type} source must be a registered evolution target")


def _slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "candidate"


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    if not content.startswith("---\n"):
        raise ValueError("skill content must include frontmatter")
    marker = "\n---\n"
    end = content.find(marker, 4)
    if end < 0:
        raise ValueError("skill content frontmatter is malformed")
    raw_frontmatter = content[4:end]
    payload = yaml.safe_load(raw_frontmatter)
    if not isinstance(payload, dict):
        raise ValueError("skill frontmatter must be a mapping")
    body = content[end + len(marker) :]
    return payload, body


def _load_skill(content: str, *, path: str) -> Skill:
    errors: list[dict[str, str]] = []
    skill = parse_skill_content(content, path=path, errors=errors)
    if skill is None:
        raise ValueError(errors[0]["message"] if errors else f"invalid skill: {path}")
    return skill


def _load_runbook(content: str, *, path: str) -> Runbook:
    errors: list[dict[str, str]] = []
    runbook = parse_runbook_content(content, path=path, errors=errors)
    if runbook is None:
        raise ValueError(errors[0]["message"] if errors else f"invalid runbook: {path}")
    return runbook


def _load_starter_pack(content: str, *, path: str) -> StarterPack:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"starter pack file {path} has invalid JSON: {exc}") from exc
    errors: list[dict[str, str]] = []
    pack = parse_starter_pack_payload(payload, path=path, errors=errors)
    if pack is None:
        raise ValueError(errors[0]["message"] if errors else f"invalid starter pack: {path}")
    return pack


def _load_prompt_pack(content: str, *, path: str) -> dict[str, Any]:
    return parse_prompt_pack_definition(content, source=path).as_metadata()


def _supporting_sections(*, objective: str, observations: tuple[str, ...]) -> str:
    sections: list[str] = []
    if objective:
        sections.append(f"## Evolution Goal\n{objective}")
    if observations:
        sections.append("## Observed Friction\n" + "\n".join(f"- {item}" for item in observations))
    sections.append(
        "## Guardrails\n"
        "- Preserve the current approval and trust-boundary surface.\n"
        "- Keep the variant declarative and reviewable.\n"
        "- Do not introduce privileged execution or secret-handling expansion."
    )
    return "\n\n".join(sections)


def _generate_skill_candidate(content: str, *, objective: str, observations: tuple[str, ...]) -> tuple[str, str]:
    frontmatter, body = _split_frontmatter(content)
    current_name = str(frontmatter.get("name") or "Skill").strip() or "Skill"
    candidate_name = f"{current_name} Review Candidate"
    frontmatter["name"] = candidate_name
    if objective:
        description = str(frontmatter.get("description") or "").strip()
        if objective not in description:
            frontmatter["description"] = f"{description} Focused on {objective}.".strip()
    candidate_body = body.strip()
    extra = _supporting_sections(objective=objective, observations=observations)
    if extra:
        candidate_body = "\n\n".join(part for part in [candidate_body, extra] if part)
    rendered = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False).strip() + "\n---\n\n" + candidate_body.strip() + "\n"
    return candidate_name, rendered


def _generate_runbook_candidate(content: str, *, objective: str, observations: tuple[str, ...]) -> tuple[str, str]:
    payload = yaml.safe_load(content)
    if not isinstance(payload, dict):
        raise ValueError("runbook root must be a mapping")
    current_title = str(payload.get("title") or "Runbook").strip() or "Runbook"
    candidate_title = f"{current_title} Review Candidate"
    payload["title"] = candidate_title
    current_id = str(payload.get("id") or f"runbook:{_slugify(current_title)}").strip()
    if not current_id.endswith(_CANDIDATE_SUFFIX):
        payload["id"] = f"{current_id}{_CANDIDATE_SUFFIX}"
    summary = str(payload.get("summary") or "").strip()
    summary_fragments = [summary] if summary else []
    if objective:
        summary_fragments.append(f"Focused on {objective}.")
    if observations:
        summary_fragments.append(f"Observed friction: {observations[0]}.")
    payload["summary"] = " ".join(fragment.strip() for fragment in summary_fragments if fragment).strip()
    return candidate_title, yaml.safe_dump(payload, sort_keys=False)


def _generate_starter_pack_candidate(content: str, *, objective: str, observations: tuple[str, ...]) -> tuple[str, str]:
    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise ValueError("starter pack root must be an object")
    current_label = str(payload.get("label") or payload.get("name") or "Starter Pack").strip() or "Starter Pack"
    candidate_label = f"{current_label} Review Candidate"
    payload["label"] = candidate_label
    base_name = str(payload.get("name") or _slugify(current_label)).strip() or _slugify(current_label)
    if not base_name.endswith(_CANDIDATE_SUFFIX):
        payload["name"] = f"{base_name}{_CANDIDATE_SUFFIX}"
    prompt_lines = []
    if objective:
        prompt_lines.append(objective)
    prompt_lines.extend(observations)
    if prompt_lines:
        payload["sample_prompt"] = " ".join(prompt_lines)
    return candidate_label, json.dumps(payload, indent=2) + "\n"


def _generate_prompt_pack_candidate(content: str, *, objective: str, observations: tuple[str, ...]) -> tuple[str, str]:
    stripped = content.strip()
    lines = stripped.splitlines() if stripped else []
    if lines and lines[0].lstrip().startswith("#"):
        current_title = lines[0].lstrip("#").strip() or "Prompt Pack"
        lines[0] = f"# {current_title} Review Candidate"
        candidate_title = f"{current_title} Review Candidate"
    else:
        candidate_title = "Prompt Pack Review Candidate"
        lines.insert(0, f"# {candidate_title}")
    extra = _supporting_sections(objective=objective, observations=observations)
    rendered = "\n".join(lines).strip()
    if extra:
        rendered = rendered + "\n\n" + extra
    return candidate_title, rendered.strip() + "\n"


def _candidate_extension(target_type: EvolutionTargetType) -> str:
    return {
        "skill": ".md",
        "runbook": ".yaml",
        "starter_pack": ".json",
        "prompt_pack": ".md",
    }[target_type]


def _default_candidate_file_name(source_path: Path) -> str:
    return f"{source_path.stem}{_CANDIDATE_SUFFIX}{source_path.suffix}"


def _trace_coverage_score(candidate_content: str, *, objective: str, observations: tuple[str, ...]) -> float:
    expected = [item for item in [objective, *observations] if item]
    if not expected:
        return 1.0
    lowered = candidate_content.lower()
    hits = sum(1 for item in expected if item.lower() in lowered)
    return hits / len(expected)


def _skill_constraints(base_content: str, candidate_content: str, *, source_path: str) -> tuple[EvolutionConstraint, ...]:
    base_skill = _load_skill(base_content, path=source_path)
    candidate_skill = _load_skill(candidate_content, path=f"{source_path}{_CANDIDATE_SUFFIX}")
    added_tools = sorted(set(candidate_skill.requires_tools) - set(base_skill.requires_tools))
    removed_tools = sorted(set(base_skill.requires_tools) - set(candidate_skill.requires_tools))
    return (
        EvolutionConstraint(
            name="tool_scope_expansion",
            status="pass" if not added_tools else "blocked",
            blocked=bool(added_tools),
            summary="Candidate must not add new required tools.",
            details={"added_tools": added_tools, "removed_tools": removed_tools},
        ),
    )


def _runbook_constraints(base_content: str, candidate_content: str, *, source_path: str) -> tuple[EvolutionConstraint, ...]:
    base = _load_runbook(base_content, path=source_path)
    candidate = _load_runbook(candidate_content, path=f"{source_path}{_CANDIDATE_SUFFIX}")
    base_target = ("workflow", base.workflow) if base.workflow else ("starter_pack", base.starter_pack) if base.starter_pack else ("command", base.command)
    candidate_target = ("workflow", candidate.workflow) if candidate.workflow else ("starter_pack", candidate.starter_pack) if candidate.starter_pack else ("command", candidate.command)
    changed = base_target != candidate_target
    return (
        EvolutionConstraint(
            name="target_surface_drift",
            status="blocked" if changed else "pass",
            blocked=changed,
            summary="Runbook target kind and referenced target must stay stable in v1.",
            details={"before": base_target, "after": candidate_target},
        ),
    )


def _starter_pack_constraints(base_content: str, candidate_content: str, *, source_path: str) -> tuple[EvolutionConstraint, ...]:
    base = _load_starter_pack(base_content, path=source_path)
    candidate = _load_starter_pack(candidate_content, path=f"{source_path}{_CANDIDATE_SUFFIX}")
    added_skills = sorted(set(candidate.skills) - set(base.skills))
    added_workflows = sorted(set(candidate.workflows) - set(base.workflows))
    added_install_items = sorted(set(candidate.install_items) - set(base.install_items))
    blocked = bool(added_skills or added_workflows or added_install_items)
    return (
        EvolutionConstraint(
            name="scope_expansion",
            status="blocked" if blocked else "pass",
            blocked=blocked,
            summary="Starter-pack variants must not expand install scope in v1.",
            details={
                "added_skills": added_skills,
                "added_workflows": added_workflows,
                "added_install_items": added_install_items,
            },
        ),
    )


def _prompt_pack_constraints(base_content: str, candidate_content: str) -> tuple[EvolutionConstraint, ...]:
    lowered_base = base_content.lower()
    privileged_tool_tokens = sorted({
        tool_name
        for tool_name, metadata in TOOL_METADATA.items()
        if any(boundary in _PRIVILEGED_PROMPT_BOUNDARIES for boundary in metadata.get("execution_boundaries", ()))
    })
    introduced = [
        token for token in (*_DANGEROUS_PROMPT_TOKENS, *privileged_tool_tokens)
        if token in candidate_content.lower() and token not in lowered_base
    ]
    size_growth = max(0, len(candidate_content) - len(base_content))
    blocked = bool(introduced) or size_growth > max(800, len(base_content))
    return (
        EvolutionConstraint(
            name="instruction_surface_expansion",
            status="blocked" if blocked else "pass",
            blocked=blocked,
            summary="Prompt-pack variants must stay bounded and must not introduce privileged instruction surfaces.",
            details={"introduced_tokens": introduced, "size_growth": size_growth},
        ),
    )


def _preference_diversity_constraint(base_content: str, candidate_content: str) -> EvolutionConstraint:
    lowered_base = base_content.lower()
    lowered_candidate = candidate_content.lower()
    introduced = sorted(
        token for token in _PREFERENCE_COLLAPSE_TOKENS
        if token in lowered_candidate and token not in lowered_base
    )
    return EvolutionConstraint(
        name="preference_diversity_collapse",
        status="blocked" if introduced else "pass",
        blocked=bool(introduced),
        summary="Candidates must not collapse user-specific or minority preferences into one generic behavior.",
        details={"introduced_phrases": introduced},
    )


def _evaluate_constraints(
    target_type: EvolutionTargetType,
    *,
    base_content: str,
    candidate_content: str,
    source_path: str,
) -> tuple[EvolutionConstraint, ...]:
    shared_constraints = (_preference_diversity_constraint(base_content, candidate_content),)
    if target_type == "skill":
        return _skill_constraints(base_content, candidate_content, source_path=source_path) + shared_constraints
    if target_type == "runbook":
        return _runbook_constraints(base_content, candidate_content, source_path=source_path) + shared_constraints
    if target_type == "starter_pack":
        return _starter_pack_constraints(base_content, candidate_content, source_path=source_path) + shared_constraints
    return _prompt_pack_constraints(base_content, candidate_content) + shared_constraints


def _validate_target(target_type: EvolutionTargetType, *, content: str, path: str) -> dict[str, Any]:
    if target_type == "skill":
        skill = _load_skill(content, path=path)
        return {
            "name": skill.name,
            "description": skill.description,
            "requires_tools": list(skill.requires_tools),
        }
    if target_type == "runbook":
        runbook = _load_runbook(content, path=path)
        return {
            "name": runbook.id,
            "title": runbook.title,
            "summary": runbook.summary,
        }
    if target_type == "starter_pack":
        pack = _load_starter_pack(content, path=path)
        return {
            "name": pack.name,
            "label": pack.label,
            "description": pack.description,
        }
    prompt = _load_prompt_pack(content, path=path)
    return {
        "name": prompt["name"],
        "title": prompt["title"],
        "description": prompt["description"],
    }


def _quality_state(*, valid: bool, blocked: bool, score: float) -> str:
    if not valid:
        return "invalid"
    if blocked:
        return "blocked"
    if score >= 0.9:
        return "ready"
    if score >= 0.7:
        return "guarded"
    return "weak"


def _review_lines_from_constraints(
    target_type: EvolutionTargetType,
    *,
    constraints: tuple[EvolutionConstraint, ...],
    score: float,
    benchmark_gate: dict[str, Any],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    change_summary: list[str] = []
    review_risks: list[str] = []

    for constraint in constraints:
        details = constraint.details
        if target_type == "skill" and constraint.name == "tool_scope_expansion":
            added_tools = [str(item) for item in details.get("added_tools", [])]
            removed_tools = [str(item) for item in details.get("removed_tools", [])]
            if added_tools:
                change_summary.append(f"Required tools added: {', '.join(added_tools)}.")
            elif removed_tools:
                change_summary.append(f"Required tools removed: {', '.join(removed_tools)}.")
            else:
                change_summary.append("Required tool scope is unchanged.")
        elif target_type == "runbook" and constraint.name == "target_surface_drift":
            before = details.get("before")
            after = details.get("after")
            if before == after:
                change_summary.append("Runbook target surface is unchanged.")
            else:
                change_summary.append(f"Runbook target changed from {before} to {after}.")
        elif target_type == "starter_pack" and constraint.name == "scope_expansion":
            added_skills = [str(item) for item in details.get("added_skills", [])]
            added_workflows = [str(item) for item in details.get("added_workflows", [])]
            added_install_items = [str(item) for item in details.get("added_install_items", [])]
            if added_skills or added_workflows or added_install_items:
                segments: list[str] = []
                if added_skills:
                    segments.append(f"skills={', '.join(added_skills)}")
                if added_workflows:
                    segments.append(f"workflows={', '.join(added_workflows)}")
                if added_install_items:
                    segments.append(f"install_items={', '.join(added_install_items)}")
                change_summary.append(f"Starter-pack scope changed: {'; '.join(segments)}.")
            else:
                change_summary.append("Starter-pack install scope is unchanged.")
        elif target_type == "prompt_pack" and constraint.name == "instruction_surface_expansion":
            introduced_tokens = [str(item) for item in details.get("introduced_tokens", [])]
            size_growth = int(details.get("size_growth") or 0)
            if introduced_tokens:
                change_summary.append(
                    f"Prompt candidate introduces privileged tokens: {', '.join(introduced_tokens)}."
                )
            else:
                change_summary.append(f"Prompt size growth stays bounded at {size_growth} chars.")
        elif constraint.name == "preference_diversity_collapse":
            introduced_phrases = [str(item) for item in details.get("introduced_phrases", [])]
            if introduced_phrases:
                change_summary.append(
                    f"Preference-diversity collapse phrases introduced: {', '.join(introduced_phrases)}."
                )
            else:
                change_summary.append("Preference-diversity guardrail is unchanged.")

        if constraint.blocked:
            review_risks.append(
                f"{constraint.name} is blocked: {constraint.summary}"
            )

    if score < 0.9:
        review_risks.append(
            "Trace coverage is partial, so human review should verify the variant actually addresses the stated objective."
        )
    if benchmark_gate.get("canary_required"):
        change_summary.append("Canary rollout remains required before any adoption decision.")
    if benchmark_gate.get("rollback_ready_required"):
        change_summary.append("Rollback-ready receipts must remain attached before promotion.")
    if benchmark_gate.get("diversity_guard_state") == "single_signal_watch":
        review_risks.append(
            "Preference-diversity signal is narrow, so review should check for overfitting to a single observation."
        )
    if not review_risks:
        review_risks.append(
            "No blocked guardrails fired, but human review is still required before merge."
        )
    if not change_summary:
        change_summary.append("Variant stays within the existing declared surface.")
    return tuple(change_summary), tuple(review_risks)


def _build_pr_draft(
    target_type: EvolutionTargetType,
    *,
    source_name: str,
    candidate_name: str,
    objective: str,
    review_risks: tuple[str, ...],
) -> dict[str, str]:
    scope = objective.strip() or f"Governed {target_type.replace('_', ' ')} evolution"
    risk_block = ""
    if review_risks:
        risk_block = (
            "\n## Review risks\n"
            + "\n".join(f"- {item}" for item in review_risks[:4])
            + "\n"
        )
    return {
        "title": f"Review {candidate_name}",
        "body": (
            f"## Summary\n"
            f"Review the governed {target_type.replace('_', ' ')} variant for `{source_name}`.\n\n"
            f"## Why\n"
            f"{scope}\n\n"
            f"{risk_block}"
            "## Review checklist\n"
            "- verify the eval score and constraint receipt\n"
            "- verify no trust-boundary or approval-surface expansion slipped in\n"
            "- decide whether to keep, revise, or discard the candidate\n"
        ),
    }


def evolution_benchmark_gate_policy() -> dict[str, Any]:
    return {
        "min_review_ready_score": 0.7,
        "min_strong_score": 0.9,
        "min_preference_signal_count": 2,
        "requires_human_review": True,
        "requires_canary_rollout": True,
        "requires_rollback_ready_receipt": True,
        "blocks_on_constraint_failure": True,
        "adoption_policy": "saved_review_candidates_remain_canary_only_until_reviewed_promotion",
        "rollback_policy": "candidate_receipt_and_source_baseline_required_before_promotion",
        "required_benchmark_suites": list(benchmark_suite_names()),
        "proof_contract": "deterministic_benchmark_suites_plus_review_receipts",
    }


def _benchmark_gate_payload(
    *,
    constraints: tuple[EvolutionConstraint, ...],
    blocked: bool,
    score: float,
    observations: tuple[str, ...],
) -> dict[str, Any]:
    policy = evolution_benchmark_gate_policy()
    blocked_constraints = [item.name for item in constraints if item.blocked]
    preference_collapse = "preference_diversity_collapse" in blocked_constraints
    preference_signal_count = len(observations)
    has_diverse_signal = preference_signal_count >= int(policy["min_preference_signal_count"])
    if blocked:
        rollout_state = "blocked"
        regression_gate = "blocked"
        acceptance_state = "blocked"
    elif score >= float(policy["min_strong_score"]):
        rollout_state = "review_ready"
        regression_gate = "pass"
        acceptance_state = "ready_for_canary" if has_diverse_signal else "held_for_canary"
    elif score >= float(policy["min_review_ready_score"]):
        rollout_state = "guarded_review"
        regression_gate = "warn"
        acceptance_state = "held_for_canary"
    else:
        rollout_state = "weak"
        regression_gate = "warn"
        acceptance_state = "held_back"
    diversity_guard_state = (
        "blocked_preference_collapse"
        if preference_collapse
        else "multi_signal_preserved"
        if has_diverse_signal
        else "single_signal_watch"
    )
    return {
        "rollout_state": rollout_state,
        "regression_gate": regression_gate,
        "acceptance_state": acceptance_state,
        "diversity_guard_state": diversity_guard_state,
        "preference_signal_count": preference_signal_count,
        "requires_human_review": bool(policy["requires_human_review"]),
        "canary_required": not blocked and bool(policy["requires_canary_rollout"]),
        "rollback_ready_required": bool(policy["requires_rollback_ready_receipt"]),
        "rollback_ready": False,
        "safety_receipt_state": "candidate_only",
        "adoption_policy": str(policy["adoption_policy"]),
        "rollback_policy": str(policy["rollback_policy"]),
        "required_benchmark_suites": list(policy["required_benchmark_suites"]),
        "blocked_constraints": blocked_constraints,
        "proof_contract": str(policy["proof_contract"]),
        "receipt_surfaces": [
            "/api/evolution/validate",
            "/api/evolution/proposals",
            "/api/operator/benchmark-proof",
            "/api/operator/governed-improvement-benchmark",
        ],
    }


def _write_receipt(candidate_file_name: str, receipt: EvolutionReceipt) -> str:
    receipts_dir = workspace_capability_package_root() / "evolution" / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)
    target = receipts_dir / f"{Path(candidate_file_name).stem}.json"
    target.write_text(json.dumps(receipt.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return str(target)


def _save_candidate(target_type: EvolutionTargetType, *, file_name: str, content: str) -> str:
    contribution_type = {
        "skill": "skills",
        "runbook": "runbooks",
        "starter_pack": "starter_packs",
        "prompt_pack": "prompt_packs",
    }[target_type]
    return str(save_workspace_contribution(contribution_type, file_name=file_name, content=content))


def evaluate_candidate(
    target_type: EvolutionTargetType,
    *,
    source_path: str,
    candidate_content: str,
    objective: str = "",
    observations: list[str] | None = None,
    candidate_file_name: str | None = None,
) -> EvolutionReceipt:
    resolved_source = _resolve_registered_target_path(target_type, source_path)
    base_content = resolved_source.read_text(encoding="utf-8")
    objective_text = str(objective or "").strip()
    normalized_observations = _normalize_observations(observations)
    source_metadata = _validate_target(target_type, content=base_content, path=str(resolved_source))
    candidate_metadata = _validate_target(
        target_type,
        content=candidate_content,
        path=str(resolved_source.with_name(candidate_file_name or _default_candidate_file_name(resolved_source))),
    )
    constraints = _evaluate_constraints(
        target_type,
        base_content=base_content,
        candidate_content=candidate_content,
        source_path=str(resolved_source),
    )
    coverage = _trace_coverage_score(candidate_content, objective=objective_text, observations=normalized_observations)
    changed = 1.0 if candidate_content.strip() != base_content.strip() else 0.0
    score = round((0.45 * 1.0) + (0.4 * coverage) + (0.15 * changed), 3)
    blocked = any(item.blocked for item in constraints)
    benchmark_gate = _benchmark_gate_payload(
        constraints=constraints,
        blocked=blocked,
        score=score,
        observations=normalized_observations,
    )
    change_summary, review_risks = _review_lines_from_constraints(
        target_type,
        constraints=constraints,
        score=score,
        benchmark_gate=benchmark_gate,
    )
    receipt = EvolutionReceipt(
        target_type=target_type,
        source_path=str(resolved_source),
        source_name=str(source_metadata.get("name") or source_metadata.get("title") or resolved_source.stem),
        candidate_name=str(candidate_metadata.get("name") or candidate_metadata.get("title") or resolved_source.stem),
        candidate_file_name=candidate_file_name or _default_candidate_file_name(resolved_source),
        valid=True,
        blocked=blocked,
        score=score,
        quality_state=_quality_state(valid=True, blocked=blocked, score=score),
        objective=objective_text,
        observations=normalized_observations,
        constraints=constraints,
        evals=(
            {"name": "structural_validity", "passed": True, "score": 1.0},
            {"name": "trace_coverage", "passed": coverage >= 0.6 or not normalized_observations, "score": round(coverage, 3)},
            {"name": "candidate_diff_present", "passed": bool(changed), "score": changed},
        ),
        change_summary=change_summary,
        review_risks=review_risks,
        benchmark_gate=benchmark_gate,
        pr_draft=_build_pr_draft(
            target_type,
            source_name=str(source_metadata.get("name") or source_metadata.get("title") or resolved_source.stem),
            candidate_name=str(candidate_metadata.get("name") or candidate_metadata.get("title") or resolved_source.stem),
            objective=objective_text,
            review_risks=review_risks,
        ),
    )
    return receipt


def generate_candidate_content(
    target_type: EvolutionTargetType,
    *,
    source_path: str,
    objective: str = "",
    observations: list[str] | None = None,
) -> tuple[str, str]:
    resolved_source = _resolve_registered_target_path(target_type, source_path)
    content = resolved_source.read_text(encoding="utf-8")
    normalized_observations = _normalize_observations(observations)
    if target_type == "skill":
        return _generate_skill_candidate(content, objective=objective.strip(), observations=normalized_observations)
    if target_type == "runbook":
        return _generate_runbook_candidate(content, objective=objective.strip(), observations=normalized_observations)
    if target_type == "starter_pack":
        return _generate_starter_pack_candidate(content, objective=objective.strip(), observations=normalized_observations)
    return _generate_prompt_pack_candidate(content, objective=objective.strip(), observations=normalized_observations)


def create_evolution_proposal(
    target_type: EvolutionTargetType,
    *,
    source_path: str,
    objective: str = "",
    observations: list[str] | None = None,
    file_name: str | None = None,
) -> dict[str, Any]:
    resolved_source = _resolve_registered_target_path(target_type, source_path)
    candidate_name, candidate_content = generate_candidate_content(
        target_type,
        source_path=str(resolved_source),
        objective=objective,
        observations=observations,
    )
    candidate_file_name = file_name or _default_candidate_file_name(resolved_source)
    receipt = evaluate_candidate(
        target_type,
        source_path=str(resolved_source),
        candidate_content=candidate_content,
        objective=objective,
        observations=observations,
        candidate_file_name=candidate_file_name,
    )
    saved_path = None
    receipt_path = None
    if not receipt.blocked and receipt.score >= 0.7:
        saved_path = _save_candidate(target_type, file_name=candidate_file_name, content=candidate_content)
        receipt = replace(receipt, saved_path=saved_path)
        receipt_path = _write_receipt(candidate_file_name, receipt)
        updated_gate = dict(receipt.benchmark_gate)
        updated_gate["rollback_ready"] = True
        updated_gate["safety_receipt_state"] = "candidate_and_receipt_written"
        updated_gate["saved_candidate_path"] = saved_path
        updated_gate["receipt_path"] = receipt_path
        receipt = replace(receipt, benchmark_gate=updated_gate, receipt_path=receipt_path)
        _write_receipt(candidate_file_name, receipt)
    return {
        "status": "saved" if saved_path else "blocked",
        "candidate_name": candidate_name,
        "candidate_content": candidate_content,
        "receipt": receipt.to_dict(),
    }


def list_evolution_targets() -> list[dict[str, Any]]:
    _ensure_target_catalog_loaded()
    targets: list[dict[str, Any]] = []
    for skill in skill_manager.list_skills():
        targets.append(
            {
                "target_type": "skill",
                "name": skill.get("name"),
                "label": skill.get("name"),
                "description": skill.get("description"),
                "source_path": skill.get("file_path"),
                "extension_id": skill.get("extension_id"),
                "source": skill.get("source"),
            }
        )
    for runbook in runbook_manager.list_runbooks():
        targets.append(
            {
                "target_type": "runbook",
                "name": runbook.get("id"),
                "label": runbook.get("title"),
                "description": runbook.get("summary"),
                "source_path": runbook.get("file_path"),
                "extension_id": runbook.get("extension_id"),
                "source": runbook.get("source"),
            }
        )
    for pack in starter_pack_manager.list_packs():
        targets.append(
            {
                "target_type": "starter_pack",
                "name": pack.get("name"),
                "label": pack.get("label"),
                "description": pack.get("description"),
                "source_path": pack.get("file_path"),
                "extension_id": pack.get("extension_id"),
                "source": pack.get("source"),
            }
        )
    snapshot = ExtensionRegistry(
        manifest_roots=_workspace_manifest_roots(),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    for contribution in snapshot.list_contributions("prompt_packs"):
        resolved_path = contribution.metadata.get("resolved_path")
        reference = str(resolved_path) if isinstance(resolved_path, str) and resolved_path else contribution.reference
        metadata = dict(contribution.metadata)
        targets.append(
            {
                "target_type": "prompt_pack",
                "name": metadata.get("name"),
                "label": metadata.get("title") or metadata.get("name"),
                "description": metadata.get("description"),
                "source_path": reference,
                "extension_id": contribution.extension_id,
                "source": contribution.source,
            }
        )
    return sorted(targets, key=lambda item: (str(item["target_type"]), str(item["label"] or item["name"] or "")))
