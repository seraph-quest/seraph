"""Tool metadata registry — maps tool names to descriptions.

Native tools have static entries in TOOL_METADATA. MCP tools get their
description dynamically from their tool object.
"""

TOOL_METADATA: dict[str, dict] = {
    # Phase 1 tools
    "web_search": {
        "description": "Search the web for information",
    },
    "read_file": {
        "description": "Read a file from the workspace",
    },
    "write_file": {
        "description": "Write content to a file",
    },
    "fill_template": {
        "description": "Fill a text template with values",
    },
    "view_soul": {
        "description": "View the soul file",
    },
    "update_soul": {
        "description": "Update a section of the soul file",
    },
    "create_goal": {
        "description": "Create a new goal",
    },
    "update_goal": {
        "description": "Update an existing goal",
    },
    "get_goals": {
        "description": "List goals",
    },
    "get_goal_progress": {
        "description": "Get goal progress dashboard",
    },
    # Phase 2 tools
    "shell_execute": {
        "description": "Execute code in a sandboxed environment",
    },
    "browse_webpage": {
        "description": "Browse and extract content from a webpage",
    },
    # Vault tools
    "store_secret": {
        "description": "Store an encrypted secret in the vault",
    },
    "get_secret": {
        "description": "Retrieve a secret from the vault",
    },
    "list_secrets": {
        "description": "List secret keys stored in the vault",
    },
    "delete_secret": {
        "description": "Delete a secret from the vault",
    },
}


def get_tool_metadata(tool_name: str) -> dict | None:
    """Get metadata for a tool by name."""
    return TOOL_METADATA.get(tool_name)


def get_all_metadata() -> dict[str, dict]:
    """Get all tool metadata."""
    return TOOL_METADATA.copy()
