"""Tool metadata registry — maps tool names to village positions and animations.

Native tools have static entries in TOOL_METADATA. MCP tools without static
entries get metadata dynamically from their server's building assignment.
"""

# Building name → default pixel coords and animation state
BUILDING_DEFAULTS: dict[str, dict] = {
    "house-1":  {"pixel_x": 192, "pixel_y": 280, "animation": "at-well"},
    "church":   {"pixel_x": 512, "pixel_y": 240, "animation": "at-bench"},
    "house-2":  {"pixel_x": 832, "pixel_y": 280, "animation": "at-signpost"},
    "forge":    {"pixel_x": 384, "pixel_y": 320, "animation": "at-forge"},
    "tower":    {"pixel_x": 640, "pixel_y": 200, "animation": "at-tower"},
    "clock":    {"pixel_x": 576, "pixel_y": 340, "animation": "at-clock"},
    "mailbox":  {"pixel_x": 128, "pixel_y": 340, "animation": "at-mailbox"},
}

TOOL_METADATA: dict[str, dict] = {
    # Phase 1 tools
    "web_search": {
        "building": "house-1",
        "pixel_x": 192,
        "pixel_y": 280,
        "animation": "at-well",
        "description": "Search the web for information",
    },
    "read_file": {
        "building": "house-2",
        "pixel_x": 832,
        "pixel_y": 280,
        "animation": "at-signpost",
        "description": "Read a file from the workspace",
    },
    "write_file": {
        "building": "house-2",
        "pixel_x": 832,
        "pixel_y": 280,
        "animation": "at-signpost",
        "description": "Write content to a file",
    },
    "fill_template": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Fill a text template with values",
    },
    # Soul / Goal tools (no specific building — use bench)
    "view_soul": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "View the soul file",
    },
    "update_soul": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Update a section of the soul file",
    },
    "create_goal": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Create a new goal",
    },
    "update_goal": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Update an existing goal",
    },
    "get_goals": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "List goals",
    },
    "get_goal_progress": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get goal progress dashboard",
    },
    # Phase 2 tools
    "shell_execute": {
        "building": "forge",
        "pixel_x": 384,
        "pixel_y": 320,
        "animation": "at-forge",
        "description": "Execute code in a sandboxed environment",
    },
    "browse_webpage": {
        "building": "tower",
        "pixel_x": 640,
        "pixel_y": 200,
        "animation": "at-tower",
        "description": "Browse and extract content from a webpage",
    },
}


def _building_to_metadata(building: str) -> dict | None:
    """Convert a building name to tool metadata with default coords."""
    defaults = BUILDING_DEFAULTS.get(building)
    if not defaults:
        return None
    return {"building": building, **defaults}


def get_tool_metadata(tool_name: str) -> dict | None:
    """Get metadata for a tool by name.

    Checks static registry first, then falls back to MCP building assignment.
    """
    # 1. Check static registry
    if tool_name in TOOL_METADATA:
        return TOOL_METADATA[tool_name]

    # 2. Check MCP building assignment
    from src.tools.mcp_manager import mcp_manager
    for server_name, tools in mcp_manager._tools.items():
        if any(t.name == tool_name for t in tools):
            building = mcp_manager.get_server_building(server_name)
            if building:
                return _building_to_metadata(building)
            break

    return None


def get_all_metadata() -> dict[str, dict]:
    """Get all tool metadata — static entries plus dynamic MCP entries."""
    result = TOOL_METADATA.copy()

    # Add dynamic entries for MCP tools not in static registry
    from src.tools.mcp_manager import mcp_manager
    for server_name, tools in mcp_manager._tools.items():
        building = mcp_manager.get_server_building(server_name)
        if not building:
            continue
        meta = _building_to_metadata(building)
        if not meta:
            continue
        for tool in tools:
            if tool.name not in result:
                result[tool.name] = meta

    return result
