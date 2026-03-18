"""Tool metadata registry — maps tool names to descriptions and policy tiers.

Native tools have static entries in TOOL_METADATA. MCP tools get their
description dynamically from their tool object.
"""

TOOL_METADATA: dict[str, dict] = {
    # Phase 1 tools
    "web_search": {
        "description": "Search the web for information",
        "policy_modes": ["safe", "balanced", "full"],
    },
    "read_file": {
        "description": "Read a file from the workspace",
        "policy_modes": ["safe", "balanced", "full"],
    },
    "write_file": {
        "description": "Write content to a file",
        "policy_modes": ["balanced", "full"],
    },
    "fill_template": {
        "description": "Fill a text template with values",
        "policy_modes": ["safe", "balanced", "full"],
    },
    "view_soul": {
        "description": "View the soul file",
        "policy_modes": ["safe", "balanced", "full"],
    },
    "update_soul": {
        "description": "Update a section of the soul file",
        "policy_modes": ["balanced", "full"],
    },
    "create_goal": {
        "description": "Create a new goal",
        "policy_modes": ["balanced", "full"],
    },
    "update_goal": {
        "description": "Update an existing goal",
        "policy_modes": ["balanced", "full"],
    },
    "get_goals": {
        "description": "List goals",
        "policy_modes": ["safe", "balanced", "full"],
    },
    "get_goal_progress": {
        "description": "Get goal progress dashboard",
        "policy_modes": ["safe", "balanced", "full"],
    },
    # Phase 2 tools
    "shell_execute": {
        "description": "Execute code in a sandboxed environment",
        "policy_modes": ["full"],
    },
    "browse_webpage": {
        "description": "Browse and extract content from a webpage",
        "policy_modes": ["safe", "balanced", "full"],
    },
    # Vault tools
    "store_secret": {
        "description": "Store an encrypted secret in the vault",
        "policy_modes": ["balanced", "full"],
    },
    "get_secret": {
        "description": "Retrieve a secret from the vault",
        "policy_modes": ["full"],
    },
    "get_secret_ref": {
        "description": "Create an opaque session-scoped reference for a secret",
        "policy_modes": ["full"],
    },
    "list_secrets": {
        "description": "List secret keys stored in the vault",
        "policy_modes": ["safe", "balanced", "full"],
    },
    "delete_secret": {
        "description": "Delete a secret from the vault",
        "policy_modes": ["full"],
    },
}


def get_tool_metadata(tool_name: str) -> dict | None:
    """Get metadata for a tool by name."""
    metadata = TOOL_METADATA.get(tool_name)
    if metadata is not None:
        return metadata
    try:
        from src.workflows.manager import workflow_manager

        return workflow_manager.get_tool_metadata(tool_name)
    except Exception:
        return None


def get_all_metadata() -> dict[str, dict]:
    """Get all tool metadata."""
    return TOOL_METADATA.copy()
