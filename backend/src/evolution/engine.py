"""Governed self-evolution for declarative workspace capability assets."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
from threading import Lock
from typing import Any, Callable, Literal
import uuid

import yaml

from config.settings import settings
from src.extensions.capability_contributions import parse_prompt_pack_definition
from src.approval.runtime import get_current_session_id, get_current_trust_principal
from src.auth.cancellation import assert_runtime_not_revoked
from src.extensions.manifest import load_extension_manifest
from src.extensions.layout import MANIFEST_FILENAMES, expected_layout_prefixes
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.workspace_package import workspace_capability_package_root
from src.evals.benchmark_catalog import benchmark_suite_names
from src.native_tools.registry import TOOL_METADATA
from src.runbooks.loader import Runbook, parse_runbook_content
from src.runbooks.manager import runbook_manager
from src.skills.loader import Skill, parse_skill_content
from src.skills.manager import skill_manager
from src.starter_packs.loader import StarterPack, parse_starter_pack_payload
from src.starter_packs.manager import starter_pack_manager
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal

EvolutionTargetType = Literal["skill", "runbook", "starter_pack", "prompt_pack"]
EvolutionAuthorityCheck = Callable[[], None]
EVOLUTION_FILE_NAME_ERROR = "Candidate file name must stay within the managed workspace package"

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

_EVOLUTION_TARGET_LOCKS_GUARD = Lock()
_EVOLUTION_TARGET_LOCKS: dict[tuple[str, str], Lock] = {}


def require_evolution_operator_authority() -> TrustPrincipal:
    """Require a live human operator for declarative evolution.

    Service and scheduled principals are deliberately denied. Evolution can
    change the future capability surface, so it remains a human operator and
    review-gated operation even when the candidate itself is declarative.
    """
    principal = get_current_trust_principal()
    session_id = str(get_current_session_id() or "").strip()
    principal_id = str(getattr(principal, "principal_id", "") or "").strip()
    principal_session_id = str(getattr(principal, "session_id", "") or "").strip()
    principal_type = getattr(getattr(principal, "principal_type", None), "value", None) or str(
        getattr(principal, "principal_type", "") or ""
    ).strip()
    grants = {
        str(getattr(grant, "value", grant))
        for grant in getattr(principal, "grants", ())
    }
    if (
        principal is None
        or principal_type != PrincipalType.OPERATOR.value
        or not bool(getattr(principal, "authenticated", False))
        or bool(getattr(principal, "revoked", False))
        or not principal_id
        or not session_id
        or principal_session_id != session_id
        or AuthorityGrant.CAPABILITY_EXECUTE.value not in grants
    ):
        raise PermissionError("governed evolution requires an authenticated operator capability principal")
    return principal


def _check_evolution_boundary(authority_check: EvolutionAuthorityCheck | None = None) -> None:
    require_evolution_operator_authority()
    # The engine owns the revocation fence even when a caller supplies an
    # additional callback.  A callback may be incomplete or accidentally omit
    # the built-in session check, so it can only add checks here.
    assert_runtime_not_revoked()
    if authority_check is not None:
        authority_check()


def validate_evolution_file_name(file_name: str) -> str:
    """Allow one canonical, case-insensitive managed-package filename."""
    candidate = str(file_name or "").strip()
    windows_path = PureWindowsPath(candidate)
    if (
        not candidate
        or "\x00" in candidate
        or os.path.isabs(candidate)
        or "/" in candidate
        or "\\" in candidate
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or any(part in {".", ".."} for part in windows_path.parts)
        or Path(candidate).name != candidate
    ):
        raise ValueError(EVOLUTION_FILE_NAME_ERROR)
    return candidate.casefold()


def _candidate_file_name_for_target(
    target_type: EvolutionTargetType,
    *,
    source_path: Path,
    requested_file_name: str | None,
) -> str:
    """Return a review-candidate filename that cannot masquerade as a source.

    The request may provide a readable label for compatibility with the
    validation surface, but the engine owns the extension and candidate suffix.
    Candidate files are never allowed to use the active source basename.
    """
    candidate = validate_evolution_file_name(
        requested_file_name or _default_candidate_file_name(source_path)
    )
    expected_extension = _candidate_extension(target_type)
    stem = Path(candidate).stem.casefold()
    if Path(candidate).suffix.casefold() != expected_extension.casefold():
        raise ValueError(EVOLUTION_FILE_NAME_ERROR)
    if not (
        stem.endswith(_CANDIDATE_SUFFIX.casefold())
        or stem.endswith(_CANDIDATE_SUFFIX.lstrip("-").casefold())
    ):
        raise ValueError(EVOLUTION_FILE_NAME_ERROR)
    if candidate.casefold() == source_path.name.casefold():
        raise ValueError(EVOLUTION_FILE_NAME_ERROR)
    return candidate


def _contribution_type_for_target(target_type: EvolutionTargetType) -> str:
    return {
        "skill": "skills",
        "runbook": "runbooks",
        "starter_pack": "starter_packs",
        "prompt_pack": "prompt_packs",
    }[target_type]


def _candidate_path(target_type: EvolutionTargetType, file_name: str) -> Path:
    file_name = validate_evolution_file_name(file_name)
    package_root = workspace_capability_package_root()
    contribution_type = _contribution_type_for_target(target_type)
    path = package_root / expected_layout_prefixes(contribution_type)[0] / file_name
    return _validate_evolution_path_containment(path)


def _receipt_path(target_type: EvolutionTargetType, file_name: str) -> Path:
    file_name = validate_evolution_file_name(file_name)
    package_root = workspace_capability_package_root()
    path = package_root / "evolution" / "receipts" / target_type / f"{Path(file_name).stem}.json"
    return _validate_evolution_path_containment(path)


def _evolution_lock_path(target_type: EvolutionTargetType, file_name: str) -> Path:
    file_name = validate_evolution_file_name(file_name)
    package_root = workspace_capability_package_root()
    path = package_root / "evolution" / "locks" / target_type / f"{file_name}.lock"
    return _validate_evolution_path_containment(path)


def _case_insensitive_path_exists(path: Path) -> bool:
    """Return whether a destination exists under the managed name identity."""
    try:
        if path.exists():
            return True
        if not path.parent.is_dir():
            return False
        normalized_name = path.name.casefold()
        return any(item.name.casefold() == normalized_name for item in path.parent.iterdir())
    except (OSError, RuntimeError) as exc:
        raise ValueError(EVOLUTION_FILE_NAME_ERROR) from exc


def _assert_review_candidate_destination_available(
    target_type: EvolutionTargetType,
    *,
    source_path: Path,
    file_name: str,
) -> None:
    """Fail closed before any generated content or artifact is written."""
    candidate_path = _candidate_path(target_type, file_name)
    receipt_path = _receipt_path(target_type, file_name)
    package_root = workspace_capability_package_root().resolve()
    manifest_declares_candidate = False
    relative_candidate_path = candidate_path.resolve().relative_to(package_root).as_posix().casefold()
    for manifest_name in MANIFEST_FILENAMES:
        manifest_path = package_root / manifest_name
        if not manifest_path.is_file():
            continue
        try:
            manifest = load_extension_manifest(manifest_path)
        except ValueError as exc:
            raise ValueError(EVOLUTION_FILE_NAME_ERROR) from exc
        declared_paths = getattr(manifest.contributes, _contribution_type_for_target(target_type), ())
        manifest_declares_candidate = manifest_declares_candidate or any(
            str(declared_path).casefold() == relative_candidate_path
            for declared_path in declared_paths
        )
    if (
        candidate_path.resolve() == source_path.resolve()
        or _case_insensitive_path_exists(candidate_path)
        or _case_insensitive_path_exists(receipt_path)
        or manifest_declares_candidate
    ):
        raise ValueError(EVOLUTION_FILE_NAME_ERROR)


@contextmanager
def _evolution_target_write_lock(target_type: EvolutionTargetType, candidate_file_name: str):
    """Serialize candidate lifecycle writes for one final destination.

    Candidate generation and validation stay independent across targets.  A
    proposal owns this narrow lock from destination preflight through snapshot,
    writes, and rollback so competing sources cannot race on one candidate.
    """
    key = (target_type, os.path.normcase(validate_evolution_file_name(candidate_file_name)))
    with _EVOLUTION_TARGET_LOCKS_GUARD:
        lock = _EVOLUTION_TARGET_LOCKS.get(key)
        if lock is None:
            lock = Lock()
            _EVOLUTION_TARGET_LOCKS[key] = lock
    with lock:
        lock_path = _evolution_lock_path(target_type, candidate_file_name)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        open_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, open_flags, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _validate_evolution_path_containment(path: Path) -> Path:
    """Reject managed artifact paths whose resolved parent escapes the package."""
    package_root = workspace_capability_package_root().resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(package_root)
    except ValueError as exc:
        raise ValueError(EVOLUTION_FILE_NAME_ERROR) from exc
    return path


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
    proposal_id: str = ""
    source_content_digest: str = ""
    candidate_content_digest: str = ""
    candidate_artifact_digest: str = ""
    source_version: str = ""
    candidate_handle: str = ""
    receipt_handle: str = ""

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


def _safe_receipt_payload(receipt: EvolutionReceipt) -> dict[str, Any]:
    """Return a metadata-only durable receipt.

    The operator response retains the existing detailed validation contract,
    while the durable receipt and benchmark readback must not become a second
    copy of arbitrary candidate input, objectives, observations, or paths.
    """
    payload = receipt.to_dict()
    benchmark_gate = payload.get("benchmark_gate")
    saved_path_reference = _safe_artifact_reference(receipt.saved_path or receipt.candidate_handle)
    receipt_path_reference = _safe_artifact_reference(receipt.receipt_path or receipt.receipt_handle)
    safe_gate = {
        key: benchmark_gate[key]
        for key in (
            "rollout_state",
            "regression_gate",
            "acceptance_state",
            "diversity_guard_state",
            "preference_signal_count",
            "requires_human_review",
            "canary_required",
            "rollback_ready_required",
            "rollback_ready",
            "safety_receipt_state",
            "adoption_policy",
            "rollback_policy",
            "required_benchmark_suites",
            "blocked_constraints",
            "proof_contract",
            "receipt_surfaces",
        )
        if isinstance(benchmark_gate, dict) and key in benchmark_gate
    }
    if saved_path_reference:
        safe_gate["saved_candidate_path"] = saved_path_reference
    if receipt_path_reference:
        safe_gate["receipt_path"] = receipt_path_reference
    lineage = {
        "proposal_id": str(receipt.proposal_id or ""),
        "source_content_digest": str(receipt.source_content_digest or ""),
        "source_version": str(receipt.source_version or receipt.source_content_digest or ""),
        "candidate_content_digest": str(receipt.candidate_content_digest or ""),
        "candidate_artifact_digest": str(receipt.candidate_artifact_digest or ""),
        "candidate_handle": saved_path_reference,
        "receipt_handle": receipt_path_reference,
    }
    safe_gate.update({key: value for key, value in lineage.items() if value})
    return {
        "target_type": receipt.target_type,
        **lineage,
        "lineage": lineage,
        "source_name_digest": _digest_metadata(receipt.source_name),
        # Candidate names are generated from the registered baseline asset;
        # strip control characters before retaining this stable receipt label.
        "candidate_name": re.sub(r"[\x00-\x1f\x7f]", "-", str(receipt.candidate_name))[:160],
        "candidate_name_digest": _digest_metadata(receipt.candidate_name),
        "candidate_file_name_digest": _digest_metadata(receipt.candidate_file_name),
        "source_path_digest": _digest_metadata(receipt.source_path),
        "saved_path": saved_path_reference,
        "receipt_path": receipt_path_reference,
        "valid": bool(receipt.valid),
        "blocked": bool(receipt.blocked),
        "score": receipt.score,
        "quality_state": receipt.quality_state,
        "constraints": [
            {
                "name": item.get("name"),
                "status": item.get("status"),
                "blocked": bool(item.get("blocked")),
            }
            for item in payload.get("constraints", [])
            if isinstance(item, dict)
        ],
        "evals": [
            {
                "name": item.get("name"),
                "passed": bool(item.get("passed")),
                "score": item.get("score"),
            }
            for item in payload.get("evals", [])
            if isinstance(item, dict)
        ],
        "change_summary": ["Candidate content withheld from the durable receipt."],
        "review_risks": ["Human review remains required before promotion."],
        "benchmark_gate": safe_gate,
        "pr_draft": {
            "title": "Governed evolution review candidate",
            "body": "Candidate content and operator-provided rationale are withheld from this receipt.",
        },
    }


def _digest_metadata(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _safe_artifact_reference(value: str | None, *, package_root: Path | None = None) -> str:
    """Return a package-relative artifact handle without host path details.

    Stored receipts may contain either a legacy absolute path or the current
    package-relative handle.  Resolve both against the managed package and
    return a neutral marker for anything outside it.
    """
    if not value:
        return ""
    raw_value = str(value).strip()
    if not raw_value:
        return ""
    package_root = (package_root or workspace_capability_package_root()).resolve()
    try:
        windows_path = PureWindowsPath(raw_value)
        if windows_path.is_absolute() or bool(windows_path.drive):
            return "artifact"
        raw_path = Path(raw_value)
        resolved = (
            raw_path.resolve()
            if raw_path.is_absolute()
            else (package_root / raw_path).resolve()
        )
        return resolved.relative_to(package_root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return "artifact"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_artifact(path: str | Path) -> str:
    """Hash a managed artifact after writing it, with containment enforced."""
    resolved_path = _validate_evolution_path_containment(Path(path)).resolve()
    return _sha256_bytes(resolved_path.read_bytes())


def _new_proposal_id() -> str:
    return uuid.uuid4().hex


def _write_receipt(candidate_file_name: str, receipt: EvolutionReceipt) -> str:
    candidate_file_name = validate_evolution_file_name(candidate_file_name)
    target = _receipt_path(receipt.target_type, candidate_file_name)
    receipts_dir = target.parent
    receipts_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(_safe_receipt_payload(receipt), indent=2, sort_keys=True) + "\n"
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ValueError(EVOLUTION_FILE_NAME_ERROR) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return str(target)


def _evolution_artifact_snapshot(
    target_type: EvolutionTargetType,
    *,
    candidate_file_name: str,
) -> tuple[tuple[Path, bool, bytes | None], ...]:
    candidate_file_name = validate_evolution_file_name(candidate_file_name)
    package_root = workspace_capability_package_root()
    candidate_path = _candidate_path(target_type, candidate_file_name)
    receipt_path = _receipt_path(target_type, candidate_file_name)
    # Keep the legacy receipt location in the rollback set so a partially
    # written older worker or test double cannot leave sensitive data behind.
    legacy_receipt_path = package_root / "evolution" / "receipts" / f"{Path(candidate_file_name).stem}.json"
    # Candidate writes are intentionally inert and never mutate the manifest;
    # restoring a manifest snapshot could clobber an unrelated operator change.
    for path in (candidate_path, receipt_path, legacy_receipt_path):
        _validate_evolution_path_containment(path)
    snapshot: list[tuple[Path, bool, bytes | None]] = []
    for path in (candidate_path, receipt_path, legacy_receipt_path):
        snapshot.append((path, path.exists(), path.read_bytes() if path.exists() else None))
    return tuple(snapshot)


def _restore_evolution_artifacts(snapshot: tuple[tuple[Path, bool, bytes | None], ...]) -> None:
    for path, existed, content in snapshot:
        if existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content or b"")
        else:
            path.unlink(missing_ok=True)


def _save_candidate(target_type: EvolutionTargetType, *, file_name: str, content: str) -> str:
    file_name = validate_evolution_file_name(file_name)
    # Keep the familiar contribution layout for operator receipts, but do not
    # register the file in the active manifest.  A review candidate therefore
    # remains inert until a separately approved promotion copies it into a
    # manifest-backed contribution.
    candidate_path = _candidate_path(target_type, file_name)
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            candidate_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ValueError(EVOLUTION_FILE_NAME_ERROR) from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
    except Exception:
        candidate_path.unlink(missing_ok=True)
        raise
    return str(candidate_path)


def evaluate_candidate(
    target_type: EvolutionTargetType,
    *,
    source_path: str,
    candidate_content: str,
    objective: str = "",
    observations: list[str] | None = None,
    candidate_file_name: str | None = None,
    proposal_id: str | None = None,
) -> EvolutionReceipt:
    _check_evolution_boundary()
    resolved_source = _resolve_registered_target_path(target_type, source_path)
    candidate_file_name = _candidate_file_name_for_target(
        target_type,
        source_path=resolved_source,
        requested_file_name=candidate_file_name,
    )
    source_bytes = resolved_source.read_bytes()
    base_content = source_bytes.decode("utf-8")
    source_content_digest = _sha256_bytes(source_bytes)
    candidate_content_digest = _sha256_text(candidate_content)
    proposal_id = str(proposal_id or "").strip() or _new_proposal_id()
    objective_text = str(objective or "").strip()
    normalized_observations = _normalize_observations(observations)
    source_metadata = _validate_target(target_type, content=base_content, path=str(resolved_source))
    candidate_metadata = _validate_target(
        target_type,
        content=candidate_content,
        path=str(resolved_source.with_name(candidate_file_name)),
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
        candidate_file_name=candidate_file_name,
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
        proposal_id=proposal_id,
        source_content_digest=source_content_digest,
        candidate_content_digest=candidate_content_digest,
        # The candidate artifact is a UTF-8 file written from this exact
        # content.  create_evolution_proposal verifies the on-disk digest
        # again after the O_EXCL write before persisting the receipt.
        candidate_artifact_digest=candidate_content_digest,
        source_version=source_content_digest,
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
    authority_check: EvolutionAuthorityCheck | None = None,
) -> dict[str, Any]:
    _check_evolution_boundary(authority_check)
    resolved_source = _resolve_registered_target_path(target_type, source_path)
    proposal_id = _new_proposal_id()
    candidate_file_name = _candidate_file_name_for_target(
        target_type,
        source_path=resolved_source,
        requested_file_name=file_name,
    )
    with _evolution_target_write_lock(target_type, candidate_file_name):
        _check_evolution_boundary(authority_check)
        _assert_review_candidate_destination_available(
            target_type,
            source_path=resolved_source,
            file_name=candidate_file_name,
        )
        _check_evolution_boundary(authority_check)
        source_digest_before_generation = _sha256_bytes(resolved_source.read_bytes())
        _check_evolution_boundary(authority_check)
        candidate_name, candidate_content = generate_candidate_content(
            target_type,
            source_path=str(resolved_source),
            objective=objective,
            observations=observations,
        )
        _check_evolution_boundary(authority_check)
        receipt = evaluate_candidate(
            target_type,
            source_path=str(resolved_source),
            candidate_content=candidate_content,
            objective=objective,
            observations=observations,
            candidate_file_name=candidate_file_name,
            proposal_id=proposal_id,
        )
        _check_evolution_boundary(authority_check)
        if receipt.source_content_digest and receipt.source_content_digest != source_digest_before_generation:
            raise ValueError("source content changed during evolution proposal")
        saved_path = None
        receipt_path = None
        if not receipt.blocked and receipt.score >= 0.7:
            _check_evolution_boundary(authority_check)
            if receipt.source_content_digest:
                current_source_digest = _sha256_bytes(resolved_source.read_bytes())
                if current_source_digest != receipt.source_content_digest:
                    raise ValueError("source content changed during evolution proposal")
            _check_evolution_boundary(authority_check)
            snapshot = _evolution_artifact_snapshot(target_type, candidate_file_name=candidate_file_name)
            try:
                _check_evolution_boundary(authority_check)
                saved_path = _save_candidate(target_type, file_name=candidate_file_name, content=candidate_content)
                _check_evolution_boundary(authority_check)
                candidate_artifact_digest = _sha256_artifact(saved_path)
                _check_evolution_boundary(authority_check)
                expected_candidate_digest = receipt.candidate_content_digest or _sha256_text(candidate_content)
                if candidate_artifact_digest != expected_candidate_digest:
                    raise ValueError("candidate artifact digest did not match evaluated content")
                receipt_path = str(_receipt_path(target_type, candidate_file_name))
                updated_gate = dict(receipt.benchmark_gate)
                updated_gate["rollback_ready"] = True
                updated_gate["safety_receipt_state"] = "candidate_and_receipt_written"
                updated_gate["saved_candidate_path"] = saved_path
                updated_gate["receipt_path"] = receipt_path
                receipt = replace(
                    receipt,
                    saved_path=saved_path,
                    benchmark_gate=updated_gate,
                    receipt_path=receipt_path,
                    proposal_id=proposal_id,
                    candidate_content_digest=expected_candidate_digest,
                    candidate_artifact_digest=candidate_artifact_digest,
                    candidate_handle=_safe_artifact_reference(saved_path),
                    receipt_handle=_safe_artifact_reference(receipt_path),
                )
                _check_evolution_boundary(authority_check)
                _write_receipt(candidate_file_name, receipt)
                _check_evolution_boundary(authority_check)
            except Exception:
                _restore_evolution_artifacts(snapshot)
                raise
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
