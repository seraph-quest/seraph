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
    # Things3 MCP tools (mapped to church — tasks & goals area)
    "get_inbox": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get Things inbox tasks",
    },
    "get_today": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get Things today tasks",
    },
    "get_upcoming": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get Things upcoming tasks",
    },
    "get_anytime": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get Things anytime tasks",
    },
    "get_someday": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get Things someday tasks",
    },
    "get_logbook": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get Things logbook",
    },
    "get_trash": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get Things trash",
    },
    "get_todos": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get Things todos",
    },
    "get_projects": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get Things projects",
    },
    "get_areas": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get Things areas",
    },
    "get_tags": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get Things tags",
    },
    "get_tagged_items": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get items with a specific tag",
    },
    "get_headings": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get Things headings",
    },
    "search_todos": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Search Things todos",
    },
    "search_advanced": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Advanced search in Things",
    },
    "get_recent": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Get recently modified Things items",
    },
    "add_todo": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Add a todo to Things",
    },
    "add_project": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Add a project to Things",
    },
    "update_todo": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Update a Things todo",
    },
    "update_project": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Update a Things project",
    },
    "show_item": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Show a Things item",
    },
    "search_items": {
        "building": "church",
        "pixel_x": 512,
        "pixel_y": 240,
        "animation": "at-bench",
        "description": "Search Things items via URL scheme",
    },
}


def get_tool_metadata(tool_name: str) -> dict | None:
    """Get metadata for a tool by name."""
    return TOOL_METADATA.get(tool_name)


def get_all_metadata() -> dict[str, dict]:
    """Get all tool metadata."""
    return TOOL_METADATA.copy()
