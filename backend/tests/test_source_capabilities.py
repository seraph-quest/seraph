from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.app import create_app
from src.tools.source_capabilities_tool import source_capabilities


@pytest.mark.asyncio
async def test_source_surfaces_endpoint_exposes_native_and_managed_sources(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    mcp_entries = [
        {
            "name": "raw-github-mcp",
            "url": "https://example.com/mcp",
            "enabled": True,
            "connected": False,
            "tool_count": 3,
            "status": "auth_required",
            "auth_hint": "Set a bearer token.",
            "has_headers": True,
            "source": "manual",
        }
    ]

    with (
        patch("src.extensions.source_capabilities.settings.workspace_dir", str(workspace_dir)),
        patch("src.extensions.source_capabilities.mcp_manager.get_config", return_value=mcp_entries),
        patch("src.extensions.source_operations.mcp_manager.get_config", return_value=mcp_entries),
        patch("src.extensions.source_operations.mcp_manager.get_server_tools", return_value=[]),
    ):
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as local_client:
            response = await local_client.get("/api/capabilities/source-surfaces")

    assert response.status_code == 200
    payload = response.json()
    typed_by_name = {item["name"]: item for item in payload["typed_sources"]}
    adapters_by_name = {item["name"]: item for item in payload["adapters"]}

    assert typed_by_name["web_search"]["contracts"] == ["source_discovery.read"]
    assert "browser_session.manage" in typed_by_name["browser_session"]["contracts"]
    assert "local-browser" in typed_by_name["browser_session"]["notes"][1]
    assert adapters_by_name["web_search"]["adapter_state"] == "ready"
    assert adapters_by_name["browse_webpage"]["operations"][0]["contract"] == "webpage.read"

    managed = typed_by_name["github-managed"]
    assert managed["source_kind"] == "managed_connector"
    assert managed["provider"] == "github"
    assert managed["authenticated"] is True
    assert managed["runtime_state"] == "requires_config"
    assert "repository.read" in managed["contracts"]
    assert "work_items.read" in managed["contracts"]
    assert "work_items.write" in managed["contracts"]

    assert payload["summary"]["authenticated_typed_source_count"] >= 1
    assert payload["adapter_summary"]["ready_adapter_count"] >= 2
    assert payload["untyped_sources"][0]["name"] == "raw-github-mcp"
    assert payload["untyped_sources"][0]["source_kind"] == "mcp_server"
    assert payload["composition_rules"][0].startswith("Prefer typed authenticated connectors")
    assert payload["selection_rules"][0].startswith("Prefer connector-backed typed adapters")


def test_source_capabilities_tool_reports_connector_first_guidance():
    inventory = {
        "contracts": [
            {
                "name": "source_discovery.read",
                "preferred_access": "web_search",
                "available_from": 1,
                "description": "Discover public sources.",
            }
        ],
        "typed_sources": [
            {
                "name": "github-managed",
                "source_kind": "managed_connector",
                "runtime_state": "ready",
                "provider": "github",
                "authenticated": True,
                "auth_kind": "oauth",
                "contracts": ["repository.read", "work_items.read"],
                "notes": ["Prefer this typed connector over browser login for authenticated source access."],
            }
        ],
        "untyped_sources": [
            {
                "name": "raw-github-mcp",
                "status": "connected",
                "tool_count": 2,
                "source": "manual",
                "notes": ["Raw MCP access is available, but no provider-neutral source contract is attached yet."],
            }
        ],
        "composition_rules": [
            "Prefer typed authenticated connectors over browser login for authenticated systems.",
        ],
    }
    adapter_inventory = {
        "adapters": [
            {
                "name": "github-managed",
                "adapter_state": "degraded",
                "provider": "github",
                "contracts": ["repository.read", "work_items.read"],
                "degraded_reason": "no_runtime_adapter",
                "operations": [
                    {
                        "contract": "work_items.read",
                        "input_mode": "adapter_defined",
                        "executable": False,
                        "reason": "no_runtime_adapter",
                    }
                ],
            }
        ]
    }

    with (
        patch("src.tools.source_capabilities_tool.list_source_capability_inventory", return_value=inventory),
        patch("src.tools.source_capabilities_tool.list_source_adapter_inventory", return_value=adapter_inventory),
    ):
        result = source_capabilities.forward()

    assert "github-managed" in result
    assert "raw-github-mcp" in result
    assert "Prefer typed authenticated connectors over browser login" in result
    assert "no_runtime_adapter" in result


def test_source_capabilities_tool_reports_bound_runtime_routes():
    inventory = {
        "contracts": [],
        "typed_sources": [],
        "untyped_sources": [],
        "composition_rules": [],
    }
    adapter_inventory = {
        "adapters": [
            {
                "name": "github-managed",
                "adapter_state": "ready",
                "provider": "github",
                "contracts": ["work_items.read"],
                "degraded_reason": None,
                "operations": [
                    {
                        "contract": "work_items.read",
                        "input_mode": "query",
                        "executable": True,
                        "runtime_server": "github",
                        "tool_name": "search_issues",
                    }
                ],
            }
        ]
    }

    with (
        patch("src.tools.source_capabilities_tool.list_source_capability_inventory", return_value=inventory),
        patch("src.tools.source_capabilities_tool.list_source_adapter_inventory", return_value=adapter_inventory),
    ):
        result = source_capabilities.forward(focus="adapters")

    assert "github/search_issues" in result
