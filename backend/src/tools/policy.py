"""Tool policy helpers for filtering available agent tools."""

from collections.abc import Iterable

from src.observer.manager import context_manager
from src.plugins.registry import get_tool_metadata

TOOL_POLICY_MODES = ("safe", "balanced", "full")
DEFAULT_TOOL_POLICY_MODE = "full"
MCP_POLICY_MODES = ("disabled", "approval", "full")
DEFAULT_MCP_POLICY_MODE = "full"


def normalize_tool_policy_mode(mode: str | None) -> str:
    """Coerce missing or invalid modes to the default."""
    if mode in TOOL_POLICY_MODES:
        return mode
    return DEFAULT_TOOL_POLICY_MODE


def normalize_mcp_policy_mode(mode: str | None) -> str:
    """Coerce missing or invalid MCP modes to the default."""
    if mode in MCP_POLICY_MODES:
        return mode
    return DEFAULT_MCP_POLICY_MODE


def is_tool_allowed(
    tool_name: str,
    mode: str,
    *,
    is_mcp: bool = False,
    mcp_mode: str | None = None,
) -> bool:
    """Return whether a tool is allowed under the selected policy mode."""
    if is_mcp:
        return normalize_mcp_policy_mode(mcp_mode) in {"approval", "full"}

    normalized = normalize_tool_policy_mode(mode)
    if normalized == "full":
        return True

    metadata = get_tool_metadata(tool_name)
    if metadata is None:
        return normalized == "full"

    return normalized in metadata.get("policy_modes", [])


def filter_tools(
    tools: Iterable,
    mode: str,
    *,
    is_mcp: bool = False,
    mcp_mode: str | None = None,
) -> list:
    """Filter a tool collection to only those allowed by policy."""
    return [
        tool for tool in tools
        if is_tool_allowed(getattr(tool, "name", ""), mode, is_mcp=is_mcp, mcp_mode=mcp_mode)
    ]


def get_tool_risk_level(tool_name: str, *, is_mcp: bool = False) -> str:
    """Infer a coarse risk level from the policy metadata."""
    if is_mcp:
        return "high"

    metadata = get_tool_metadata(tool_name)
    if metadata is None:
        return "high"

    modes = metadata.get("policy_modes", [])
    if modes == ["full"]:
        return "high"
    if modes == ["balanced", "full"]:
        return "medium"
    return "low"


def get_tool_execution_boundaries(tool_name: str, *, is_mcp: bool = False) -> list[str]:
    """Return the execution-boundary tags for a tool."""
    if is_mcp:
        return ["external_mcp"]

    metadata = get_tool_metadata(tool_name)
    if metadata is None:
        return ["unknown"]

    boundaries = metadata.get("execution_boundaries")
    if not isinstance(boundaries, list) or not boundaries:
        return ["unknown"]
    return [str(boundary) for boundary in boundaries]


def get_current_tool_policy_mode() -> str:
    """Read the current tool policy mode from the shared observer context."""
    try:
        return normalize_tool_policy_mode(context_manager.get_context().tool_policy_mode)
    except Exception:
        return DEFAULT_TOOL_POLICY_MODE


def get_current_mcp_policy_mode() -> str:
    """Read the current MCP policy mode from the shared observer context."""
    try:
        return normalize_mcp_policy_mode(context_manager.get_context().mcp_policy_mode)
    except Exception:
        return DEFAULT_MCP_POLICY_MODE
