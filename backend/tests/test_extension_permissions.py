from unittest.mock import MagicMock, patch

from src.extensions.permissions import evaluate_tool_permissions


def test_evaluate_tool_permissions_preserves_runtime_mcp_secret_ref_fields():
    mcp_tool = MagicMock()
    mcp_tool.name = "mcp_tasks"
    mcp_tool.inputs = {
        "headers": {"type": "object", "description": "Authentication headers"},
        "body": {"type": "string", "description": "Request body"},
    }

    with patch("src.extensions.permissions.mcp_manager.get_tools", return_value=[mcp_tool]):
        permissions = evaluate_tool_permissions(None, tool_names=["mcp_tasks"])

    assert permissions["accepts_secret_refs"] is True

