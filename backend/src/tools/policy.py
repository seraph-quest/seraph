"""Tool policy helpers for filtering available agent tools."""

from collections.abc import Iterable

from src.observer.manager import context_manager
from src.native_tools.registry import get_tool_metadata

TOOL_POLICY_MODES = ("safe", "balanced", "full")
DEFAULT_TOOL_POLICY_MODE = "full"
MCP_POLICY_MODES = ("disabled", "approval", "full")
DEFAULT_MCP_POLICY_MODE = "full"
_SECRET_REF_FIELD_CANDIDATES = (
    "headers",
    "authorization",
    "auth_header",
    "api_key",
    "token",
    "bearer_token",
    "password",
    "secret_ref",
)


def _get_explicit_instance_attr(obj: object | None, attr_name: str) -> object | None:
    """Read only explicitly assigned instance attributes, avoiding dynamic mock chains."""
    if obj is None:
        return None
    try:
        instance_dict = vars(obj)
    except TypeError:
        instance_dict = None
    if isinstance(instance_dict, dict) and attr_name in instance_dict:
        return instance_dict[attr_name]
    slots = getattr(type(obj), "__slots__", ())
    if isinstance(slots, str):
        slots = (slots,)
    if attr_name in slots:
        try:
            return object.__getattribute__(obj, attr_name)
        except AttributeError:
            return None
    return None


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


def get_tool_execution_boundaries(tool_name: str, *, is_mcp: bool = False, tool: object | None = None) -> list[str]:
    """Return the execution-boundary tags for a tool."""
    if is_mcp:
        boundaries = ["external_mcp"]
        source_context = get_tool_source_context(tool)
        if isinstance(source_context, dict) and bool(source_context.get("authenticated_source")):
            boundaries.append("authenticated_external_source")
        return boundaries

    metadata = get_tool_metadata(tool_name)
    if metadata is None:
        return ["unknown"]

    boundaries = metadata.get("execution_boundaries")
    if not isinstance(boundaries, list) or not boundaries:
        return ["unknown"]
    return [str(boundary) for boundary in boundaries]


def tool_accepts_secret_refs(tool_name: str, *, is_mcp: bool = False, tool: object | None = None) -> bool:
    """Return whether a tool is allowed to receive resolved secret references."""
    if is_mcp:
        return bool(get_tool_secret_ref_fields(tool_name, is_mcp=True, tool=tool))

    metadata = get_tool_metadata(tool_name)
    if metadata is None:
        return False
    return bool(metadata.get("accepts_secret_refs", False))


def get_tool_secret_ref_fields(tool_name: str, *, is_mcp: bool = False, tool: object | None = None) -> list[str]:
    """Return the allowlisted top-level fields that may contain secret refs."""
    if is_mcp:
        explicit_fields = _get_explicit_instance_attr(tool, "seraph_secret_ref_fields")
        normalized = _normalize_secret_ref_fields(explicit_fields)
        if normalized:
            return normalized
        return _infer_secret_ref_fields_from_inputs(_read_tool_inputs(tool))

    metadata = get_tool_metadata(tool_name)
    if metadata is None or not bool(metadata.get("accepts_secret_refs", False)):
        return []
    normalized = _normalize_secret_ref_fields(metadata.get("secret_ref_fields"))
    if normalized:
        return normalized
    return _infer_secret_ref_fields_from_inputs(_read_tool_inputs(tool))


def get_tool_source_context(tool: object | None) -> dict[str, object] | None:
    """Return the first MCP source context visible through wrapper layers."""
    current = tool
    visited_ids: set[int] = set()
    while current is not None:
        current_id = id(current)
        if current_id in visited_ids:
            break
        visited_ids.add(current_id)
        source_context = _get_explicit_instance_attr(current, "seraph_source_context")
        if isinstance(source_context, dict):
            return source_context
        current = _get_explicit_instance_attr(current, "wrapped_tool")
    return None


def _normalize_secret_ref_fields(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        field_name = item.strip()
        if not field_name or field_name in seen:
            continue
        normalized.append(field_name)
        seen.add(field_name)
    return normalized


def _infer_secret_ref_fields_from_inputs(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    allowed: list[str] = []
    seen: set[str] = set()
    for key in value:
        if not isinstance(key, str):
            continue
        field_name = key.strip()
        if not field_name:
            continue
        normalized_key = field_name.lower()
        if normalized_key not in _SECRET_REF_FIELD_CANDIDATES or field_name in seen:
            continue
        allowed.append(field_name)
        seen.add(field_name)
    return allowed


def _read_tool_inputs(tool: object | None) -> object:
    explicit_inputs = _get_explicit_instance_attr(tool, "inputs")
    if explicit_inputs is not None:
        return explicit_inputs
    return getattr(tool, "inputs", None)


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
