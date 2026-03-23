"""Metadata registry for bundled native tools.

Native tools have static entries here. Workflow metadata remains dynamic and is
resolved through the workflow manager when requested.
"""

TOOL_NAME_ALIASES: dict[str, str] = {
    "shell_execute": "execute_code",
}


def canonical_tool_name(tool_name: str) -> str:
    """Return the canonical runtime tool name for a tool or legacy alias."""
    return TOOL_NAME_ALIASES.get(tool_name, tool_name)


TOOL_METADATA: dict[str, dict] = {
    # Phase 1 tools
    "web_search": {
        "description": "Search the web for information",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["external_read"],
    },
    "read_file": {
        "description": "Read a file from the workspace",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["workspace_read"],
    },
    "write_file": {
        "description": "Write content to a file",
        "policy_modes": ["balanced", "full"],
        "execution_boundaries": ["workspace_write"],
    },
    "fill_template": {
        "description": "Fill a text template with values",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["local_compute"],
    },
    "view_soul": {
        "description": "View the soul file",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["guardian_state_read"],
    },
    "update_soul": {
        "description": "Update a section of the soul file",
        "policy_modes": ["balanced", "full"],
        "execution_boundaries": ["guardian_state_write"],
    },
    "create_goal": {
        "description": "Create a new goal",
        "policy_modes": ["balanced", "full"],
        "execution_boundaries": ["guardian_state_write"],
    },
    "update_goal": {
        "description": "Update an existing goal",
        "policy_modes": ["balanced", "full"],
        "execution_boundaries": ["guardian_state_write"],
    },
    "get_goals": {
        "description": "List goals",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["guardian_state_read"],
    },
    "get_goal_progress": {
        "description": "Get goal progress dashboard",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["guardian_state_read"],
    },
    # Phase 2 tools
    "execute_code": {
        "description": "Execute bounded code in a sandboxed environment",
        "policy_modes": ["full"],
        "execution_boundaries": ["sandbox_execution"],
    },
    "delegate_task": {
        "description": "Delegate a bounded subtask to a specialist runtime",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["delegation"],
    },
    "clarify": {
        "description": "Request a missing input from the user before continuing",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["conversation"],
    },
    "browse_webpage": {
        "description": "Browse and extract content from a webpage",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["external_read"],
    },
    # Vault tools
    "store_secret": {
        "description": "Store an encrypted secret in the vault",
        "policy_modes": ["balanced", "full"],
        "execution_boundaries": ["secret_management"],
    },
    "get_secret": {
        "description": "Retrieve a secret from the vault",
        "policy_modes": ["full"],
        "execution_boundaries": ["secret_read"],
    },
    "get_secret_ref": {
        "description": "Create an opaque session-scoped reference for a secret",
        "policy_modes": ["full"],
        "execution_boundaries": ["secret_injection"],
    },
    "list_secrets": {
        "description": "List secret keys stored in the vault",
        "policy_modes": ["safe", "balanced", "full"],
        "execution_boundaries": ["secret_management"],
    },
    "delete_secret": {
        "description": "Delete a secret from the vault",
        "policy_modes": ["full"],
        "execution_boundaries": ["secret_management"],
    },
}


def get_tool_metadata(tool_name: str) -> dict | None:
    """Get metadata for a bundled native tool or a derived workflow tool."""
    metadata = TOOL_METADATA.get(canonical_tool_name(tool_name))
    if metadata is not None:
        return metadata
    try:
        from src.workflows.manager import workflow_manager

        return workflow_manager.get_tool_metadata(tool_name)
    except Exception:
        return None


def get_all_metadata() -> dict[str, dict]:
    """Return a copy of all bundled native tool metadata."""
    return TOOL_METADATA.copy()
