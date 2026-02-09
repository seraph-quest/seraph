"""Tool metadata registry — maps tool names to village positions and animations."""

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
    "get_calendar_events": {
        "building": "clock",
        "pixel_x": 576,
        "pixel_y": 340,
        "animation": "at-clock",
        "description": "Get upcoming calendar events",
    },
    "create_calendar_event": {
        "building": "clock",
        "pixel_x": 576,
        "pixel_y": 340,
        "animation": "at-clock",
        "description": "Create a new calendar event",
    },
    "read_emails": {
        "building": "mailbox",
        "pixel_x": 128,
        "pixel_y": 340,
        "animation": "at-mailbox",
        "description": "Read emails from inbox",
    },
    "send_email": {
        "building": "mailbox",
        "pixel_x": 128,
        "pixel_y": 340,
        "animation": "at-mailbox",
        "description": "Send an email",
    },
}


def get_tool_metadata(tool_name: str) -> dict | None:
    """Get metadata for a tool by name."""
    return TOOL_METADATA.get(tool_name)


def get_all_metadata() -> dict[str, dict]:
    """Get all tool metadata."""
    return TOOL_METADATA.copy()
