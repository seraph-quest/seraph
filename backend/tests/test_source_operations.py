from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.app import create_app
from src.observer.context import CurrentContext
from src.browser.sessions import browser_session_runtime
from src.api.capabilities import _build_capability_overview
from src.extensions.source_operations import build_source_mutation_plan, build_source_review_plan
from src.tools.source_evidence_tool import collect_source_evidence
from src.tools.source_mutation_tool import plan_source_mutation
from src.tools.source_review_tool import plan_source_review


class FakeMCPTool:
    def __init__(self, name: str, payload):
        self.name = name
        self._payload = payload

    def __call__(self, **kwargs):
        return self._payload


@pytest.fixture(autouse=True)
def reset_browser_sessions():
    browser_session_runtime.reset_for_tests()
    yield
    browser_session_runtime.reset_for_tests()


@pytest.mark.asyncio
async def test_source_adapters_endpoint_exposes_ready_public_adapters_and_degraded_managed_connector(
    tmp_path,
):
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
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
            response = await client.get("/api/capabilities/source-adapters")

    assert response.status_code == 200
    payload = response.json()
    adapters_by_name = {item["name"]: item for item in payload["adapters"]}

    assert adapters_by_name["web_search"]["adapter_state"] == "ready"
    assert adapters_by_name["browse_webpage"]["operations"][0]["input_mode"] == "url"
    assert adapters_by_name["browser_session"]["operations"][0]["contract"] == "webpage.read"

    managed = adapters_by_name["github-managed"]
    assert managed["adapter_state"] == "degraded"
    assert managed["degraded_reason"] == "requires_config"
    assert managed["operations"][0]["executable"] is False
    assert payload["selection_rules"][0].startswith("Prefer connector-backed typed adapters")


@pytest.mark.asyncio
async def test_source_evidence_endpoint_collects_search_results_in_normalized_shape():
    records = [
        {
            "title": "Seraph roadmap",
            "href": "https://example.com/roadmap",
            "body": "Roadmap summary for the product.",
        },
        {
            "title": "Seraph status",
            "href": "https://example.com/status",
            "body": "Current shipped truth.",
        },
    ]

    with patch("src.extensions.source_operations.search_web_records", return_value=(records, [])):
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
            response = await client.post(
                "/api/capabilities/source-evidence",
                json={"contract": "source_discovery.read", "query": "seraph roadmap", "max_results": 2},
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["adapter"]["name"] == "web_search"
    assert payload["summary"]["item_count"] == 2
    assert payload["items"][0]["kind"] == "search_result"
    assert payload["items"][0]["location"] == "https://example.com/roadmap"


@pytest.mark.asyncio
async def test_source_evidence_endpoint_collects_public_page_content():
    with patch(
        "src.extensions.source_operations.browse_webpage",
        return_value="Seraph can inspect public webpages and summarize them.",
    ):
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
            response = await client.post(
                "/api/capabilities/source-evidence",
                json={"contract": "webpage.read", "url": "https://example.com/about"},
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["adapter"]["name"] == "browse_webpage"
    assert payload["items"][0]["kind"] == "webpage"
    assert payload["items"][0]["location"] == "https://example.com/about"


@pytest.mark.asyncio
async def test_source_evidence_endpoint_reads_existing_browser_snapshot():
    payload = browser_session_runtime.open_session(
        owner_session_id="session-1",
        url="https://example.com/context",
        provider_name="local-browser",
        provider_kind="local",
        execution_mode="local_runtime",
        capture="extract",
        content="Snapshot content from an existing browser session.",
    )

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        response = await client.post(
            "/api/capabilities/source-evidence",
            json={
                "contract": "webpage.read",
                "source": "browser_session",
                "owner_session_id": "session-1",
                "ref": payload["latest_ref"],
            },
        )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "ok"
    assert result["adapter"]["name"] == "browser_session"
    assert result["items"][0]["kind"] == "browser_snapshot"
    assert result["items"][0]["metadata"]["ref"] == payload["latest_ref"]


@pytest.mark.asyncio
async def test_source_evidence_endpoint_reports_degraded_managed_connector_with_fallback(
    tmp_path,
):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    mcp_entries = [
        {
            "name": "raw-github-mcp",
            "url": "https://example.com/mcp",
            "enabled": True,
            "connected": True,
            "tool_count": 6,
            "status": "connected",
            "auth_hint": "Token configured.",
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
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
            response = await client.post(
                "/api/capabilities/source-evidence",
                json={"contract": "work_items.read", "source": "github-managed"},
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "unavailable"
    assert payload["adapter"]["name"] == "github-managed"
    assert payload["adapter"]["adapter_state"] == "degraded"
    assert payload["next_best_sources"][0]["name"] == "raw-github-mcp"


@pytest.mark.asyncio
async def test_source_adapters_endpoint_promotes_managed_connector_when_runtime_is_bound(
    tmp_path,
):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    mcp_entries = [
        {
            "name": "github",
            "url": "https://example.com/mcp",
            "enabled": True,
            "connected": True,
            "tool_count": 3,
            "status": "connected",
            "auth_hint": "Token configured.",
            "has_headers": True,
            "source": "extension",
        }
    ]
    state_payload = {
        "extensions": {
            "seraph.core-managed-connectors": {
                "config": {
                    "managed_connectors": {
                        "github-managed": {
                            "installation_id": "inst_123",
                        }
                    }
                },
                "connector_state": {
                    "connectors/managed/github.yaml": {
                        "enabled": True,
                    }
                },
            }
        }
    }
    server_tools = [
        FakeMCPTool("search_repositories", [{"id": 1, "full_name": "seraph-quest/seraph"}]),
        FakeMCPTool("search_issues", [{"id": 2, "title": "Fix source adapter", "html_url": "https://example.com/issues/2"}]),
        FakeMCPTool("search_pull_requests", [{"id": 3, "title": "Improve adapters", "html_url": "https://example.com/pulls/3"}]),
    ]

    with (
        patch("src.extensions.source_capabilities.settings.workspace_dir", str(workspace_dir)),
        patch("src.extensions.source_capabilities.load_extension_state_payload", return_value=state_payload),
        patch("src.extensions.source_capabilities.mcp_manager.get_config", return_value=mcp_entries),
        patch("src.extensions.source_operations.mcp_manager.get_config", return_value=mcp_entries),
        patch("src.extensions.source_operations.mcp_manager.get_server_tools", return_value=server_tools),
    ):
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
            response = await client.get("/api/capabilities/source-adapters")

    assert response.status_code == 200
    payload = response.json()
    managed = next(item for item in payload["adapters"] if item["name"] == "github-managed")
    assert managed["adapter_state"] == "ready"
    assert managed["degraded_reason"] is None
    work_items_route = next(item for item in managed["operations"] if item["contract"] == "work_items.read")
    assert work_items_route["executable"] is True
    assert work_items_route["runtime_server"] == "github"
    assert work_items_route["tool_name"] == "search_issues"
    work_items_write_route = next(item for item in managed["operations"] if item["contract"] == "work_items.write")
    assert work_items_write_route["mutating"] is True
    assert work_items_write_route["requires_approval"] is True
    assert work_items_write_route["reason"] == "route_not_defined"


@pytest.mark.asyncio
async def test_source_evidence_endpoint_collects_github_work_items_via_bound_runtime(
    tmp_path,
):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    mcp_entries = [
        {
            "name": "github",
            "url": "https://example.com/mcp",
            "enabled": True,
            "connected": True,
            "tool_count": 3,
            "status": "connected",
            "auth_hint": "Token configured.",
            "has_headers": True,
            "source": "extension",
        }
    ]
    state_payload = {
        "extensions": {
            "seraph.core-managed-connectors": {
                "config": {
                    "managed_connectors": {
                        "github-managed": {
                            "installation_id": "inst_123",
                        }
                    }
                },
                "connector_state": {
                    "connectors/managed/github.yaml": {
                        "enabled": True,
                    }
                },
            }
        }
    }
    server_tools = [
        FakeMCPTool(
            "search_issues",
            [
                {
                    "id": 41,
                    "title": "Adapter-backed GitHub evidence",
                    "html_url": "https://github.com/seraph-quest/seraph/issues/41",
                    "body": "Expose provider-neutral evidence reads.",
                    "state": "open",
                    "number": 41,
                }
            ],
        )
    ]

    with (
        patch("src.extensions.source_capabilities.settings.workspace_dir", str(workspace_dir)),
        patch("src.extensions.source_capabilities.load_extension_state_payload", return_value=state_payload),
        patch("src.extensions.source_capabilities.mcp_manager.get_config", return_value=mcp_entries),
        patch("src.extensions.source_operations.mcp_manager.get_config", return_value=mcp_entries),
        patch("src.extensions.source_operations.mcp_manager.get_server_tools", return_value=server_tools),
    ):
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
            response = await client.post(
                "/api/capabilities/source-evidence",
                json={
                    "contract": "work_items.read",
                    "source": "github-managed",
                    "query": "is:issue source adapter",
                    "max_results": 5,
                },
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["adapter"]["name"] == "github-managed"
    assert payload["items"][0]["kind"] == "work_item"
    assert payload["items"][0]["location"] == "https://github.com/seraph-quest/seraph/issues/41"
    assert payload["items"][0]["metadata"]["runtime_server"] == "github"


def test_build_source_review_plan_prefers_ready_authenticated_adapters(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    mcp_entries = [
        {
            "name": "github",
            "url": "https://example.com/mcp",
            "enabled": True,
            "connected": True,
            "tool_count": 3,
            "status": "connected",
            "auth_hint": "Token configured.",
            "has_headers": True,
            "source": "extension",
        }
    ]
    state_payload = {
        "extensions": {
            "seraph.core-managed-connectors": {
                "config": {
                    "managed_connectors": {
                        "github-managed": {
                            "installation_id": "inst_123",
                        }
                    }
                },
                "connector_state": {
                    "connectors/managed/github.yaml": {
                        "enabled": True,
                    }
                },
            }
        }
    }
    server_tools = [
        FakeMCPTool("search_repositories", [{"id": 1, "full_name": "seraph-quest/seraph"}]),
        FakeMCPTool("search_issues", [{"id": 2, "title": "Fix source adapter", "html_url": "https://example.com/issues/2"}]),
        FakeMCPTool("search_pull_requests", [{"id": 3, "title": "Improve adapters", "html_url": "https://example.com/pulls/3"}]),
    ]

    with (
        patch("src.extensions.source_capabilities.settings.workspace_dir", str(workspace_dir)),
        patch("src.extensions.source_capabilities.load_extension_state_payload", return_value=state_payload),
        patch("src.extensions.source_capabilities.mcp_manager.get_config", return_value=mcp_entries),
        patch("src.extensions.source_operations.mcp_manager.get_config", return_value=mcp_entries),
        patch("src.extensions.source_operations.mcp_manager.get_server_tools", return_value=server_tools),
    ):
        plan = build_source_review_plan(
            intent="daily_review",
            focus="adapter-backed source operations",
            time_window="today",
        )

    assert plan["status"] == "ready"
    assert plan["recommended_runbooks"] == ["runbook:source-daily-review"]
    steps_by_id = {step["id"]: step for step in plan["steps"]}
    assert steps_by_id["work_items"]["source"] == "github-managed"
    assert steps_by_id["code_activity"]["source"] == "github-managed"
    assert steps_by_id["context"]["source"] == "web_search"


def test_build_source_review_plan_reports_degraded_adapters_with_next_best_sources(tmp_path):
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
        plan = build_source_review_plan(
            intent="goal_alignment",
            focus="adapter-backed source operations",
            goal_context="Move authenticated source routines forward",
            time_window="this week",
            source="github-managed",
        )

    steps_by_id = {step["id"]: step for step in plan["steps"]}
    assert plan["status"] == "partial"
    assert steps_by_id["work_items"]["status"] == "degraded"
    assert steps_by_id["work_items"]["degraded_reason"] == "requires_config"
    assert steps_by_id["work_items"]["next_best_sources"][0]["name"] == "raw-github-mcp"
    assert "does not advertise" not in "\n".join(plan["warnings"])


@pytest.mark.asyncio
async def test_source_review_plan_endpoint_returns_structured_steps(tmp_path):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    mcp_entries = [
        {
            "name": "github",
            "url": "https://example.com/mcp",
            "enabled": True,
            "connected": True,
            "tool_count": 3,
            "status": "connected",
            "auth_hint": "Token configured.",
            "has_headers": True,
            "source": "extension",
        }
    ]
    state_payload = {
        "extensions": {
            "seraph.core-managed-connectors": {
                "config": {
                    "managed_connectors": {
                        "github-managed": {
                            "installation_id": "inst_123",
                        }
                    }
                },
                "connector_state": {
                    "connectors/managed/github.yaml": {
                        "enabled": True,
                    }
                },
            }
        }
    }
    server_tools = [
        FakeMCPTool("search_repositories", [{"id": 1, "full_name": "seraph-quest/seraph"}]),
        FakeMCPTool("search_issues", [{"id": 2, "title": "Fix source adapter", "html_url": "https://example.com/issues/2"}]),
        FakeMCPTool("search_pull_requests", [{"id": 3, "title": "Improve adapters", "html_url": "https://example.com/pulls/3"}]),
    ]

    with (
        patch("src.extensions.source_capabilities.settings.workspace_dir", str(workspace_dir)),
        patch("src.extensions.source_capabilities.load_extension_state_payload", return_value=state_payload),
        patch("src.extensions.source_capabilities.mcp_manager.get_config", return_value=mcp_entries),
        patch("src.extensions.source_operations.mcp_manager.get_config", return_value=mcp_entries),
        patch("src.extensions.source_operations.mcp_manager.get_server_tools", return_value=server_tools),
    ):
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
            response = await client.post(
                "/api/capabilities/source-review-plan",
                json={
                    "intent": "daily_review",
                    "focus": "adapter-backed source operations",
                    "time_window": "today",
                    "url": "https://example.com/status",
                },
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["steps"][-1]["contract"] == "webpage.read"
    assert payload["steps"][-1]["source"] == "browse_webpage"


def test_build_source_mutation_plan_requires_explicit_write_contract():
    plan = build_source_mutation_plan(
        contract="work_items.read",
        source="github-managed",
        action_summary="close stale issue",
    )

    assert plan["status"] == "failed"
    assert "not a typed mutation contract" in plan["warnings"][0]


def test_build_source_mutation_plan_returns_scoped_approval_for_bound_write_route():
    adapter_inventory = {
        "summary": {"adapter_count": 1, "ready_adapter_count": 1},
        "adapters": [
            {
                "name": "github-managed",
                "provider": "github",
                "source_kind": "managed_connector",
                "authenticated": True,
                "adapter_state": "ready",
                "degraded_reason": None,
                "contracts": ["work_items.write"],
                "next_best_sources": [],
                "operations": [
                    {
                        "contract": "work_items.write",
                        "input_mode": "query",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "runtime_server": "github",
                        "tool_name": "create_issue",
                        "result_kind": "work_item",
                    }
                ],
            }
        ],
    }

    with (
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
    ):
        plan = build_source_mutation_plan(
            contract="work_items.write",
            source="github-managed",
            action_summary="Open a follow-up issue for the failing shard contract",
            target_reference="seraph-quest/seraph#349",
            fields=["title", "body", "labels"],
        )

    assert plan["status"] == "approval_required"
    assert plan["requires_approval"] is True
    assert plan["approval_scope"]["target"]["reference"] == "seraph-quest/seraph#349"
    assert plan["approval_scope"]["target"]["target_kind"] == "work_item"
    assert plan["approval_scope"]["change_scope"]["field_names"] == ["title", "body", "labels"]
    assert plan["approval_context"]["execution_boundaries"] == [
        "external_mcp",
        "authenticated_external_source",
        "connector_mutation",
    ]
    assert plan["audit_payload"]["tool_name"] == "create_issue"


def test_build_source_mutation_plan_reports_missing_runtime_route():
    adapter_inventory = {
        "summary": {"adapter_count": 1, "ready_adapter_count": 1},
        "adapters": [
            {
                "name": "github-managed",
                "provider": "github",
                "source_kind": "managed_connector",
                "authenticated": True,
                "adapter_state": "ready",
                "degraded_reason": None,
                "contracts": ["work_items.write"],
                "next_best_sources": [{"name": "raw-github-mcp", "reason": "raw_mcp_only"}],
                "operations": [
                    {
                        "contract": "work_items.write",
                        "input_mode": "query",
                        "executable": False,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "reason": "route_not_defined",
                    }
                ],
            }
        ],
    }

    with (
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
    ):
        plan = build_source_mutation_plan(
            contract="work_items.write",
            source="github-managed",
            action_summary="Close the issue once the source write path is implemented",
            target_reference="seraph-quest/seraph#342",
            fields=["state"],
        )

    assert plan["status"] == "degraded"
    assert "cannot execute 'work_items.write' right now" in plan["warnings"][0]
    assert plan["approval_scope"]["runtime_scope"]["route_executable"] is False
    assert plan["next_best_sources"][0]["name"] == "raw-github-mcp"


def test_build_source_mutation_plan_preserves_mutation_scope_for_missing_runtime_adapter():
    adapter_inventory = {
        "summary": {"adapter_count": 1, "ready_adapter_count": 0},
        "adapters": [
            {
                "name": "github-managed",
                "provider": "github",
                "source_kind": "managed_connector",
                "authenticated": True,
                "adapter_state": "degraded",
                "degraded_reason": "no_runtime_adapter",
                "contracts": ["work_items.write"],
                "next_best_sources": [{"name": "raw-github-mcp", "reason": "raw_mcp_only"}],
                "operations": [
                    {
                        "contract": "work_items.write",
                        "input_mode": "query",
                        "executable": False,
                        "reason": "no_runtime_adapter",
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                    }
                ],
            }
        ],
    }

    with (
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
    ):
        plan = build_source_mutation_plan(
            contract="work_items.write",
            source="github-managed",
            action_summary="Update the tracked work item once runtime binding exists",
            target_reference="seraph-quest/seraph#342",
            fields=["state"],
        )

    assert plan["status"] == "degraded"
    assert plan["requires_approval"] is True
    assert plan["approval_scope"]["type"] == "connector_mutation"
    assert plan["approval_context"]["execution_boundaries"] == [
        "external_mcp",
        "authenticated_external_source",
        "connector_mutation",
    ]
    assert plan["audit_payload"]["event_type"] == "authenticated_source_mutation"
    assert "cannot execute 'work_items.write' right now" in plan["warnings"][0]


@pytest.mark.asyncio
async def test_source_mutation_plan_endpoint_returns_structured_scope():
    mutation_plan = {
        "status": "approval_required",
        "adapter": {
            "name": "github-managed",
            "adapter_state": "ready",
        },
        "operation": {
            "mutating": True,
            "requires_approval": True,
            "runtime_server": "github",
            "tool_name": "create_issue",
        },
        "requires_approval": True,
        "approval_scope": {
            "target": {
                "provider": "github",
                "target_kind": "work_item",
                "reference": "seraph-quest/seraph#342",
            },
            "change_scope": {
                "action_summary": "Open the issue",
                "field_names": ["title", "body"],
            },
        },
        "warnings": [],
        "next_best_sources": [],
    }

    with patch("src.api.capabilities.build_source_mutation_plan", return_value=mutation_plan):
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
            response = await client.post(
                "/api/capabilities/source-mutation-plan",
                json={
                    "contract": "work_items.write",
                    "source": "github-managed",
                    "action_summary": "Open the issue",
                    "target_reference": "seraph-quest/seraph#342",
                    "fields": ["title", "body"],
                },
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "approval_required"
    assert payload["approval_scope"]["target"]["reference"] == "seraph-quest/seraph#342"


def test_plan_source_mutation_tool_renders_structured_scope():
    mutation_plan = {
        "status": "approval_required",
        "adapter": {
            "name": "github-managed",
            "adapter_state": "ready",
        },
        "operation": {
            "mutating": True,
            "requires_approval": True,
            "runtime_server": "github",
            "tool_name": "create_issue",
        },
        "approval_scope": {
            "target": {
                "provider": "github",
                "target_kind": "work_item",
                "reference": "seraph-quest/seraph#342",
            },
            "change_scope": {
                "action_summary": "Open the issue",
                "field_names": ["title", "body"],
            },
        },
        "warnings": [],
    }

    with patch("src.tools.source_mutation_tool.build_source_mutation_plan", return_value=mutation_plan):
        result = plan_source_mutation.forward(
            contract="work_items.write",
            source="github-managed",
            action_summary="Open the issue",
            target_reference="seraph-quest/seraph#342",
            fields="title, body",
        )

    assert "status: approval_required" in result
    assert "adapter_state: ready" in result
    assert "github work_item seraph-quest/seraph#342" in result
    assert "fields: title, body" in result


def test_plan_source_review_tool_renders_structured_plan():
    plan = {
        "status": "partial",
        "intent": "daily_review",
        "title": "Daily Source Review",
        "description": "Review what moved.",
        "summary": {"step_count": 3, "ready_step_count": 2, "degraded_step_count": 1, "unavailable_step_count": 0},
        "recommended_runbooks": ["runbook:source-daily-review"],
        "recommended_starter_packs": ["source-daily-review"],
        "warnings": ["github-managed is degraded"],
        "steps": [
            {
                "id": "work_items",
                "contract": "work_items.read",
                "source": "github-managed",
                "status": "degraded",
                "purpose": "Gather work items.",
                "suggested_input": "recent work items for adapter work during today",
                "query_guidance": "Name the project and window.",
                "degraded_reason": "requires_config",
                "next_best_sources": [{"name": "raw-github-mcp", "description": "Only raw MCP is visible."}],
            }
        ],
    }

    with patch("src.tools.source_review_tool.build_source_review_plan", return_value=plan):
        result = plan_source_review.forward(intent="daily_review", focus="adapter work", time_window="today")

    assert "status: partial" in result
    assert "recommended_runbooks:" in result
    assert "runbook:source-daily-review" in result
    assert "raw-github-mcp" in result


def test_collect_source_evidence_tool_renders_structured_summary():
    bundle = {
        "status": "ok",
        "adapter": {
            "name": "browse_webpage",
            "adapter_state": "ready",
        },
        "items": [
            {
                "title": "https://example.com/about",
                "location": "https://example.com/about",
                "summary": "Public product page.",
            }
        ],
        "warnings": [],
        "next_best_sources": [{"name": "browser_session", "description": "Reuse an existing snapshot."}],
    }

    with patch("src.tools.source_evidence_tool.collect_source_evidence_bundle", return_value=bundle):
        result = collect_source_evidence.forward(contract="webpage.read", url="https://example.com/about")

    assert "status: ok" in result
    assert "source: browse_webpage" in result
    assert "Public product page." in result
    assert "browser_session" in result


def test_capability_overview_includes_source_adapter_inventory():
    source_adapter_inventory = {
        "summary": {"adapter_count": 4, "ready_adapter_count": 3},
        "adapters": [{"name": "web_search", "adapter_state": "ready"}],
        "selection_rules": ["Prefer connector-backed typed adapters when ready."],
    }

    with (
        patch("src.api.capabilities.get_base_tools_and_active_skills", return_value=([], [], "disabled")),
        patch("src.api.capabilities.get_current_tool_policy_mode", return_value="safe"),
        patch("src.api.capabilities._tool_status_list", return_value=[]),
        patch("src.api.capabilities._skill_status_map", return_value=([], {})),
        patch("src.api.capabilities._workflow_status_map", return_value=([], {})),
        patch("src.api.capabilities._mcp_status_list", return_value=[]),
        patch("src.api.capabilities._starter_pack_statuses", return_value=[]),
        patch("src.api.capabilities._load_explicit_runbooks", return_value=[]),
        patch("src.api.capabilities._recommended_actions", return_value=([], [], [])),
        patch("src.api.capabilities.list_source_adapter_inventory", return_value=source_adapter_inventory),
        patch(
            "src.api.capabilities.context_manager.get_context",
            return_value=CurrentContext(tool_policy_mode="safe", mcp_policy_mode="disabled", approval_mode="safe"),
        ),
    ):
        overview = _build_capability_overview()

    assert overview["summary"]["source_adapters_ready"] == 3
    assert overview["summary"]["source_adapters_total"] == 4
    assert overview["source_adapters"][0]["name"] == "web_search"
    assert overview["source_adapter_rules"][0].startswith("Prefer connector-backed typed adapters")
