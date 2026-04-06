"""Delegation runtime tool backed by the existing specialist graph."""

from __future__ import annotations

from contextvars import ContextVar
from collections.abc import Iterable
from typing import Any

from smolagents import tool

from config.settings import settings

SPECIALIST_ALIASES: dict[str, str] = {
    "memory": "memory_keeper",
    "memories": "memory_keeper",
    "soul": "memory_keeper",
    "vault": "vault_keeper",
    "secret": "vault_keeper",
    "secrets": "vault_keeper",
    "credential": "vault_keeper",
    "credentials": "vault_keeper",
    "goals": "goal_planner",
    "goal": "goal_planner",
    "priorities": "goal_planner",
    "priority": "goal_planner",
    "research": "web_researcher",
    "web": "web_researcher",
    "search": "web_researcher",
    "files": "file_worker",
    "file": "file_worker",
    "code": "file_worker",
    "workflow": "workflow_runner",
    "workflows": "workflow_runner",
}

AUTO_ROUTE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workflow_runner", ("workflow", "runbook", "starter pack", "starter-pack")),
    ("vault_keeper", ("vault", "secret", "credential", "credentials", "api key", "password")),
    ("memory_keeper", ("remember", "memory", "identity", "preference", "guardian record", "soul")),
    ("goal_planner", ("goal", "goals", "priority", "priorities", "plan", "roadmap")),
    ("web_researcher", ("research", "search", "look up", "lookup", "find", "web", "weather")),
    ("file_worker", ("file", "files", "workspace", "save", "write", "patch", "code", "script")),
)

_DELEGATION_DEPTH: ContextVar[int] = ContextVar("delegate_task_depth", default=0)
_RISK_RANKS = {"low": 0, "medium": 1, "high": 2}
_DEFAULT_SPECIALIST_NAMES: tuple[str, ...] = (
    "memory_keeper",
    "vault_keeper",
    "goal_planner",
    "web_researcher",
    "file_worker",
    "workflow_runner",
)
_SPECIALIST_DEFAULT_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    "memory_keeper": ("view_soul", "update_soul"),
    "vault_keeper": ("store_secret", "get_secret", "get_secret_ref", "list_secrets", "delete_secret"),
    "goal_planner": ("create_goal", "update_goal", "get_goals", "get_goal_progress"),
    "web_researcher": ("web_search", "browse_webpage", "browser_session", "source_capabilities"),
    "file_worker": ("read_file", "write_file", "fill_template", "execute_code"),
}


def _normalize_specialist_name(value: str) -> str:
    normalized = " ".join(value.strip().lower().replace("-", " ").split())
    if not normalized:
        return ""
    return SPECIALIST_ALIASES.get(normalized, normalized.replace(" ", "_"))


def _extract_output(result: object) -> str:
    output = getattr(result, "output", None)
    if isinstance(output, str) and output.strip():
        return output
    if isinstance(result, str):
        return result
    return str(result)


def _match_specialist(task: str, specialist_names: Iterable[str]) -> str | None:
    lowered = task.lower()
    available = set(specialist_names)
    for specialist_name, keywords in AUTO_ROUTE_KEYWORDS:
        if specialist_name not in available:
            continue
        if any(keyword in lowered for keyword in keywords):
            return specialist_name
    for specialist_name in available:
        if specialist_name.startswith("mcp_") and specialist_name.removeprefix("mcp_") in lowered:
            return specialist_name
    return None


def _max_risk_level(current: str, candidate: str) -> str:
    current_rank = _RISK_RANKS.get(current, _RISK_RANKS["high"])
    candidate_rank = _RISK_RANKS.get(candidate, _RISK_RANKS["high"])
    return current if current_rank >= candidate_rank else candidate


def _build_specialists_by_name(specialists: Iterable[object]) -> dict[str, object]:
    specialists_by_name: dict[str, object] = {}
    for specialist in specialists:
        name = getattr(specialist, "name", None)
        if isinstance(name, str) and name:
            specialists_by_name[name] = specialist
    return specialists_by_name


def resolve_specialist_runtime(
    task: str | None,
    specialist: str | None = None,
    *,
    specialists: Iterable[object] | None = None,
) -> tuple[str | None, object | None]:
    """Resolve the effective specialist name/runtime for a delegation call."""
    if specialists is None:
        try:
            from src.agent.specialists import build_all_specialists

            specialists = build_all_specialists()
        except Exception:
            specialists = []
    specialists_by_name = _build_specialists_by_name(specialists or [])
    candidate_names = specialists_by_name.keys() or _DEFAULT_SPECIALIST_NAMES
    normalized_name = _normalize_specialist_name(specialist or "")
    selected_name = normalized_name or _match_specialist(task or "", candidate_names)
    if not selected_name:
        return None, None
    return selected_name, specialists_by_name.get(selected_name)


def infer_delegation_approval_context(
    task: str | None,
    specialist: str | None = None,
    *,
    specialists: Iterable[object] | None = None,
) -> dict[str, Any]:
    """Infer the approval-context surface for a delegated specialist route."""
    from src.native_tools.registry import canonical_tool_name
    from src.tools.policy import (
        get_tool_execution_boundaries,
        get_tool_risk_level,
        get_tool_source_context,
        tool_accepts_secret_refs,
    )

    selected_name, selected_runtime = resolve_specialist_runtime(
        task,
        specialist,
        specialists=specialists,
    )
    execution_boundaries = ["delegation"]
    accepts_secret_refs = False
    authenticated_source = False
    source_systems: list[dict[str, Any]] = []
    risk_level = "low"

    raw_specialist_tools = getattr(selected_runtime, "tools", []) or []
    if isinstance(raw_specialist_tools, dict):
        specialist_tools = list(raw_specialist_tools.values())
    else:
        specialist_tools = list(raw_specialist_tools)
    if not specialist_tools and isinstance(selected_name, str):
        for tool_name in _SPECIALIST_DEFAULT_TOOL_NAMES.get(selected_name, ()):
            canonical_name = canonical_tool_name(tool_name)
            for boundary in get_tool_execution_boundaries(canonical_name):
                if boundary not in execution_boundaries:
                    execution_boundaries.append(boundary)
            accepts_secret_refs = accepts_secret_refs or tool_accepts_secret_refs(canonical_name)
            risk_level = _max_risk_level(risk_level, get_tool_risk_level(canonical_name))
        if selected_name == "workflow_runner":
            risk_level = "high"
        if selected_name and selected_name.startswith("mcp_"):
            if "external_mcp" not in execution_boundaries:
                execution_boundaries.append("external_mcp")
            accepts_secret_refs = True
            risk_level = "high"

    for tool in specialist_tools:
        tool_name = getattr(tool, "name", None)
        if not isinstance(tool_name, str) or not tool_name:
            continue
        canonical_name = canonical_tool_name(tool_name)
        is_mcp = canonical_name.startswith("mcp_")
        for boundary in get_tool_execution_boundaries(canonical_name, is_mcp=is_mcp, tool=tool):
            if boundary not in execution_boundaries:
                execution_boundaries.append(boundary)
        accepts_secret_refs = accepts_secret_refs or tool_accepts_secret_refs(canonical_name, is_mcp=is_mcp)
        risk_level = _max_risk_level(risk_level, get_tool_risk_level(canonical_name, is_mcp=is_mcp))
        source_context = get_tool_source_context(tool)
        if is_mcp and isinstance(source_context, dict) and bool(source_context.get("authenticated_source")):
            authenticated_source = True
            source_system = {
                "server_name": str(source_context.get("server_name") or ""),
                "hostname": str(source_context.get("hostname") or ""),
                "source": str(source_context.get("source") or "manual"),
                "authenticated_source": True,
            }
            if source_system not in source_systems:
                source_systems.append(source_system)

    unresolved = selected_name is None
    if unresolved:
        risk_level = "high"
        accepts_secret_refs = True

    return {
        "delegated_specialist": selected_name,
        "delegation_target_unresolved": unresolved,
        "risk_level": risk_level,
        "execution_boundaries": execution_boundaries,
        "accepts_secret_refs": accepts_secret_refs,
        "authenticated_source": authenticated_source,
        "source_systems": source_systems,
    }


@tool
def delegate_task(task: str, specialist: str = "") -> str:
    """Delegate a bounded subtask to a specialist runtime.

    Use this when a focused specialist can handle the subtask more cleanly than
    the current general agent. Prefer an explicit `specialist` value when you
    know which domain should handle the work.

    Args:
        task: The bounded subtask to hand off to a specialist.
        specialist: Optional specialist name or alias such as `research`,
            `files`, `goals`, `memory`, `vault`, or `workflow`.

    Returns:
        The delegated specialist's final response, or a clear routing error.
    """
    from src.agent.specialists import build_all_specialists

    if not settings.use_delegation:
        return "Error: Delegation runtime is disabled."
    current_depth = _DELEGATION_DEPTH.get()
    if current_depth > 0:
        return "Error: Nested delegation is not allowed."

    specialists = build_all_specialists()
    if not specialists:
        return "Error: No specialists are currently available for delegation."

    specialists_by_name = _build_specialists_by_name(specialists)
    selected_name, selected = resolve_specialist_runtime(task, specialist, specialists=specialists)
    if not selected_name:
        available = ", ".join(sorted(specialists_by_name))
        return (
            "Error: Unable to infer a specialist for this task. "
            f"Specify one of: {available}"
        )

    if selected is None:
        available = ", ".join(sorted(specialists_by_name))
        return f"Error: Unknown specialist '{specialist}'. Available specialists: {available}"

    token = _DELEGATION_DEPTH.set(current_depth + 1)
    try:
        result = selected.run(task, stream=False, reset=True)
        return _extract_output(result)
    finally:
        _DELEGATION_DEPTH.reset(token)
