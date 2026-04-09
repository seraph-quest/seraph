from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.app import create_app
from src.observer.context import CurrentContext
from src.browser.sessions import browser_session_runtime
from src.api.capabilities import _build_capability_overview
from src.extensions.source_operations import (
    build_source_mutation_plan,
    build_source_report_plan,
    build_source_review_plan,
    execute_source_mutation_bundle,
)
from src.tools.source_evidence_tool import collect_source_evidence
from src.tools.source_mutation_tool import (
    execute_source_mutation,
    plan_source_mutation,
    plan_source_report,
)
from src.tools.source_review_tool import plan_source_review


class FakeMCPTool:
    def __init__(self, name: str, payload):
        self.name = name
        self._payload = payload
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if callable(self._payload):
            return self._payload(**kwargs)
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
        FakeMCPTool("create_issue", [{"id": 4, "title": "Create issue route", "html_url": "https://example.com/issues/4"}]),
        FakeMCPTool(
            "add_comment_to_issue",
            [{"id": 5, "html_url": "https://example.com/issues/2#issuecomment-5"}],
        ),
        FakeMCPTool(
            "create_pull_request",
            [{"id": 6, "title": "Create pull request route", "html_url": "https://example.com/pulls/6"}],
        ),
        FakeMCPTool(
            "add_review_to_pr",
            [{"id": 7, "html_url": "https://example.com/pulls/3#pullrequestreview-7"}],
        ),
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
    assert work_items_write_route["input_mode"] == "structured_action"
    assert {item["kind"] for item in work_items_write_route["actions"]} == {"create", "comment"}
    create_action = next(item for item in work_items_write_route["actions"] if item["kind"] == "create")
    comment_action = next(item for item in work_items_write_route["actions"] if item["kind"] == "comment")
    assert create_action["tool_name"] == "create_issue"
    assert create_action["target_reference_mode"] == "repository"
    assert comment_action["tool_name"] == "add_comment_to_issue"
    assert comment_action["target_reference_mode"] == "work_item"
    code_activity_write_route = next(item for item in managed["operations"] if item["contract"] == "code_activity.write")
    assert code_activity_write_route["executable"] is True
    assert {item["kind"] for item in code_activity_write_route["actions"]} == {"create", "review"}
    create_pr_action = next(item for item in code_activity_write_route["actions"] if item["kind"] == "create")
    review_action = next(item for item in code_activity_write_route["actions"] if item["kind"] == "review")
    assert create_pr_action["tool_name"] == "create_pull_request"
    assert create_pr_action["target_reference_mode"] == "repository"
    assert review_action["tool_name"] == "add_review_to_pr"
    assert review_action["target_reference_mode"] == "pull_request"
    assert review_action["fixed_argument_keys"] == ["action", "file_comments"]


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


def test_build_source_mutation_plan_requires_explicit_action_kind_for_multi_action_route():
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
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "actions": [
                            {
                                "kind": "create",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "create_issue",
                                "target_reference_mode": "repository",
                                "required_payload_fields": ["title", "body"],
                                "allowed_payload_fields": ["title", "body"],
                            },
                            {
                                "kind": "comment",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "add_comment_to_issue",
                                "target_reference_mode": "work_item",
                                "required_payload_fields": ["body"],
                                "allowed_payload_fields": ["body"],
                            },
                        ],
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
            action_summary="Publish the update",
            target_reference="seraph-quest/seraph#343",
            fields=["body"],
        )

    assert plan["status"] == "failed"
    assert "requires an explicit action_kind" in plan["warnings"][0]
    assert {item["kind"] for item in plan["available_actions"]} == {"create", "comment"}
    assert next(item for item in plan["available_actions"] if item["kind"] == "comment")["allowed_payload_fields"] == ["body"]


def test_build_source_mutation_plan_selects_requested_action_for_bound_write_route():
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
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "actions": [
                            {
                                "kind": "create",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "create_issue",
                                "target_reference_mode": "repository",
                                "required_payload_fields": ["title", "body"],
                                "allowed_payload_fields": ["title", "body"],
                            },
                            {
                                "kind": "comment",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "add_comment_to_issue",
                                "target_reference_mode": "work_item",
                                "required_payload_fields": ["body"],
                                "allowed_payload_fields": ["body"],
                            },
                        ],
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
            action_kind="comment",
            action_summary="Publish the update",
            target_reference="seraph-quest/seraph#343",
            fields=["body"],
        )

    assert plan["status"] == "approval_required"
    assert plan["action"]["kind"] == "comment"
    assert plan["action"]["tool_name"] == "add_comment_to_issue"
    assert plan["approval_scope"]["action"]["target_reference_mode"] == "work_item"
    assert plan["approval_scope"]["action"]["allowed_payload_fields"] == ["body"]
    assert plan["approval_context"]["mutation_action_kind"] == "comment"
    assert plan["audit_payload"]["action_kind"] == "comment"


def test_build_source_mutation_plan_records_implicit_single_action_scope():
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
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "actions": [
                            {
                                "kind": "comment",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "add_comment_to_issue",
                                "target_reference_mode": "work_item",
                                "required_payload_fields": ["body"],
                            }
                        ],
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
            action_summary="Publish the update",
            target_reference="seraph-quest/seraph#343",
            fields=["body"],
        )

    assert plan["status"] == "approval_required"
    assert plan["action"]["kind"] == "comment"
    assert plan["approval_context"]["mutation_action_kind"] == "comment"
    assert plan["audit_payload"]["action_kind"] == "comment"


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


def test_execute_source_mutation_bundle_executes_repository_create_action():
    create_issue = FakeMCPTool(
        "create_issue",
        {
            "id": 343,
            "title": "Adapter-backed status report",
            "html_url": "https://github.com/seraph-quest/seraph/issues/343",
            "body": "Published from Seraph.",
            "number": 343,
        },
    )
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
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "result_kind": "work_item",
                        "actions": [
                            {
                                "kind": "create",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "create_issue",
                                "target_reference_mode": "repository",
                                "target_argument_name": "repo_full_name",
                                "required_payload_fields": ["title", "body"],
                                "allowed_payload_fields": ["title", "body"],
                                "payload_argument_map": {"title": "title", "body": "body"},
                            }
                        ],
                    }
                ],
            }
        ],
    }

    with (
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
        patch("src.extensions.source_operations.mcp_manager.get_server_tools", return_value=[create_issue]),
        patch("src.extensions.source_operations.log_integration_event_sync") as log_event,
    ):
        result = execute_source_mutation_bundle(
            contract="work_items.write",
            source="github-managed",
            action_kind="create",
            target_reference="seraph-quest/seraph",
            payload={"title": "Adapter-backed status report", "body": "Published from Seraph."},
        )

    assert result["status"] == "ok"
    assert create_issue.calls == [
        {
            "repo_full_name": "seraph-quest/seraph",
            "title": "Adapter-backed status report",
            "body": "Published from Seraph.",
        }
    ]
    assert result["result"]["kind"] == "work_item"
    assert result["result"]["location"] == "https://github.com/seraph-quest/seraph/issues/343"
    log_event.assert_called_once()


def test_execute_source_mutation_bundle_executes_issue_comment_action():
    add_comment = FakeMCPTool(
        "add_comment_to_issue",
        {
            "id": 501,
            "html_url": "https://github.com/seraph-quest/seraph/issues/343#issuecomment-501",
            "body": "Status posted.",
        },
    )
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
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "result_kind": "work_item",
                        "actions": [
                            {
                                "kind": "comment",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "add_comment_to_issue",
                                "target_reference_mode": "work_item",
                                "target_argument_name": "repo_full_name",
                                "number_argument_name": "issue_number",
                                "required_payload_fields": ["body"],
                                "allowed_payload_fields": ["body"],
                                "payload_argument_map": {"body": "comment"},
                            }
                        ],
                    }
                ],
            }
        ],
    }

    with (
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
        patch("src.extensions.source_operations.mcp_manager.get_server_tools", return_value=[add_comment]),
        patch("src.extensions.source_operations.log_integration_event_sync") as log_event,
    ):
        result = execute_source_mutation_bundle(
            contract="work_items.write",
            source="github-managed",
            action_kind="comment",
            target_reference="seraph-quest/seraph#343",
            payload={"body": "Status posted."},
        )

    assert result["status"] == "ok"
    assert add_comment.calls == [
        {
            "repo_full_name": "seraph-quest/seraph",
            "issue_number": 343,
            "comment": "Status posted.",
        }
    ]
    assert result["result"]["location"].endswith("#issuecomment-501")
    log_event.assert_called_once()


def test_execute_source_mutation_bundle_executes_pull_request_create_action():
    create_pull_request = FakeMCPTool(
        "create_pull_request",
        {
            "id": 601,
            "title": "Adapter-backed external action depth",
            "html_url": "https://github.com/seraph-quest/seraph/pull/601",
            "body": "Open a draft PR from Seraph.",
        },
    )
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
                "contracts": ["code_activity.write"],
                "next_best_sources": [],
                "operations": [
                    {
                        "contract": "code_activity.write",
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "result_kind": "code_activity",
                        "actions": [
                            {
                                "kind": "create",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "create_pull_request",
                                "target_reference_mode": "repository",
                                "target_argument_name": "repository_full_name",
                                "required_payload_fields": ["title", "body", "head_branch", "base_branch"],
                                "allowed_payload_fields": ["title", "body", "head_branch", "base_branch", "draft"],
                                "payload_argument_map": {
                                    "title": "title",
                                    "body": "body",
                                    "head_branch": "head_branch",
                                    "base_branch": "base_branch",
                                    "draft": "draft",
                                },
                            }
                        ],
                    }
                ],
            }
        ],
    }

    with (
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
        patch("src.extensions.source_operations.mcp_manager.get_server_tools", return_value=[create_pull_request]),
        patch("src.extensions.source_operations.log_integration_event_sync") as log_event,
    ):
        result = execute_source_mutation_bundle(
            contract="code_activity.write",
            source="github-managed",
            action_kind="create",
            target_reference="seraph-quest/seraph",
            payload={
                "title": "Adapter-backed external action depth",
                "body": "Open a draft PR from Seraph.",
                "head_branch": "feat/adapter-depth",
                "base_branch": "develop",
                "draft": True,
            },
        )

    assert result["status"] == "ok"
    assert create_pull_request.calls == [
        {
            "repository_full_name": "seraph-quest/seraph",
            "title": "Adapter-backed external action depth",
            "body": "Open a draft PR from Seraph.",
            "head_branch": "feat/adapter-depth",
            "base_branch": "develop",
            "draft": True,
        }
    ]
    assert result["result"]["kind"] == "code_activity"
    assert result["result"]["location"] == "https://github.com/seraph-quest/seraph/pull/601"
    log_event.assert_called_once()


def test_execute_source_mutation_bundle_executes_pr_review_action_with_fixed_arguments():
    add_review = FakeMCPTool(
        "add_review_to_pr",
        {
            "id": 777,
            "html_url": "https://github.com/seraph-quest/seraph/pull/343#pullrequestreview-777",
            "body": "Posted PR review.",
        },
    )
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
                "contracts": ["code_activity.write"],
                "next_best_sources": [],
                "operations": [
                    {
                        "contract": "code_activity.write",
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "result_kind": "code_activity",
                        "actions": [
                            {
                                "kind": "review",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "add_review_to_pr",
                                "target_reference_mode": "pull_request",
                                "target_argument_name": "repo_full_name",
                                "number_argument_name": "pr_number",
                                "required_payload_fields": ["review"],
                                "allowed_payload_fields": ["review"],
                                "payload_argument_map": {"review": "review"},
                                "fixed_arguments": {"action": "COMMENT", "file_comments": []},
                            }
                        ],
                    }
                ],
            }
        ],
    }

    with (
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
        patch("src.extensions.source_operations.mcp_manager.get_server_tools", return_value=[add_review]),
        patch("src.extensions.source_operations.log_integration_event_sync") as log_event,
    ):
        result = execute_source_mutation_bundle(
            contract="code_activity.write",
            source="github-managed",
            action_kind="review",
            target_reference="seraph-quest/seraph/pull/343",
            payload={"review": "Looks good overall."},
        )

    assert result["status"] == "ok"
    assert add_review.calls == [
        {
            "action": "COMMENT",
            "file_comments": [],
            "repo_full_name": "seraph-quest/seraph",
            "pr_number": 343,
            "review": "Looks good overall.",
        }
    ]
    assert result["result"]["kind"] == "code_activity"
    assert result["result"]["location"].endswith("#pullrequestreview-777")
    log_event.assert_called_once()


def test_execute_source_mutation_bundle_rejects_payload_override_of_fixed_review_action():
    add_review = FakeMCPTool("add_review_to_pr", {"id": 777, "html_url": "https://example.com/review/777"})
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
                "contracts": ["code_activity.write"],
                "next_best_sources": [],
                "operations": [
                    {
                        "contract": "code_activity.write",
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "actions": [
                            {
                                "kind": "review",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "add_review_to_pr",
                                "target_reference_mode": "pull_request",
                                "target_argument_name": "repo_full_name",
                                "number_argument_name": "pr_number",
                                "required_payload_fields": ["review"],
                                "allowed_payload_fields": ["review"],
                                "payload_argument_map": {"review": "review"},
                                "fixed_arguments": {"action": "COMMENT", "file_comments": []},
                            }
                        ],
                    }
                ],
            }
        ],
    }

    with (
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
        patch("src.extensions.source_operations.mcp_manager.get_server_tools", return_value=[add_review]),
    ):
        result = execute_source_mutation_bundle(
            contract="code_activity.write",
            source="github-managed",
            action_kind="review",
            target_reference="seraph-quest/seraph/pull/343",
            payload={"review": "Looks good overall.", "action": "APPROVE"},
        )

    assert result["status"] == "failed"
    assert "undeclared fields" in result["warnings"][0]
    assert add_review.calls == []


def test_execute_source_mutation_bundle_rejects_invalid_target_reference():
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
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "actions": [
                            {
                                "kind": "comment",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "add_comment_to_issue",
                                "target_reference_mode": "work_item",
                                "target_argument_name": "repo_full_name",
                                "number_argument_name": "issue_number",
                                "required_payload_fields": ["body"],
                                "allowed_payload_fields": ["body"],
                                "payload_argument_map": {"body": "comment"},
                            }
                        ],
                    }
                ],
            }
        ],
    }

    with (
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
        patch(
            "src.extensions.source_operations.mcp_manager.get_server_tools",
            return_value=[FakeMCPTool("add_comment_to_issue", {"id": 1})],
        ),
    ):
        result = execute_source_mutation_bundle(
            contract="work_items.write",
            source="github-managed",
            action_kind="comment",
            target_reference="seraph-quest/seraph",
            payload={"body": "Status posted."},
        )

    assert result["status"] == "failed"
    assert "owner/repo#number" in result["warnings"][0]


def test_execute_source_mutation_bundle_rejects_undeclared_payload_fields():
    create_issue = FakeMCPTool("create_issue", {"id": 343, "title": "ok"})
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
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "actions": [
                            {
                                "kind": "create",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "create_issue",
                                "target_reference_mode": "repository",
                                "target_argument_name": "repo_full_name",
                                "required_payload_fields": ["title", "body"],
                                "allowed_payload_fields": ["title", "body"],
                                "payload_argument_map": {"title": "title", "body": "body"},
                            }
                        ],
                    }
                ],
            }
        ],
    }

    with (
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
        patch("src.extensions.source_operations.mcp_manager.get_server_tools", return_value=[create_issue]),
    ):
        result = execute_source_mutation_bundle(
            contract="work_items.write",
            source="github-managed",
            action_kind="create",
            target_reference="seraph-quest/seraph",
            payload={"title": "Adapter-backed status report", "body": "Published from Seraph.", "labels": ["unsafe-extra"]},
        )

    assert result["status"] == "failed"
    assert "undeclared fields" in result["warnings"][0]
    assert create_issue.calls == []


def test_build_source_report_plan_composes_review_and_publish():
    review_plan = {
        "status": "ready",
        "intent": "progress_review",
        "title": "Progress Review",
        "recommended_runbooks": ["runbook:source-progress-review"],
        "recommended_starter_packs": ["source-progress-review"],
        "warnings": [],
        "steps": [
            {
                "id": "work_items",
                "contract": "work_items.read",
                "status": "ready",
                "source": "github-managed",
            }
        ],
    }
    publish_plan = {
        "status": "approval_required",
        "adapter": {"name": "github-managed"},
        "action": {"kind": "comment"},
        "approval_scope": {"target": {"reference": "seraph-quest/seraph#343"}},
    }

    with (
        patch("src.extensions.source_operations.build_source_review_plan", return_value=review_plan),
        patch("src.extensions.source_operations.build_source_mutation_plan", return_value=publish_plan),
    ):
        plan = build_source_report_plan(
            intent="progress_review",
            focus="adapter-backed authenticated operations",
            target_reference="seraph-quest/seraph#343",
        )

    assert plan["status"] == "ready"
    assert plan["publish_plan"]["action"]["kind"] == "comment"
    assert plan["recommended_runbooks"] == [
        "runbook:source-progress-review",
        "runbook:source-progress-report",
    ]
    assert plan["recommended_starter_packs"] == [
        "source-progress-review",
        "source-progress-report",
    ]


def test_build_source_report_plan_falls_back_to_write_capable_adapter():
    review_plan = {
        "status": "ready",
        "intent": "progress_review",
        "title": "Progress Review",
        "recommended_runbooks": [],
        "recommended_starter_packs": [],
        "warnings": [],
        "steps": [
            {
                "id": "work_items",
                "contract": "work_items.read",
                "status": "ready",
                "source": "jira-managed",
            }
        ],
    }
    adapter_inventory = {
        "adapters": [
            {
                "name": "jira-managed",
                "provider": "jira",
                "source_kind": "managed_connector",
                "authenticated": True,
                "adapter_state": "ready",
                "degraded_reason": "",
                "contracts": ["work_items.read"],
                "next_best_sources": [],
                "operations": [
                    {
                        "contract": "work_items.read",
                        "input_mode": "query",
                        "executable": True,
                    }
                ],
            },
            {
                "name": "github-managed",
                "provider": "github",
                "source_kind": "managed_connector",
                "authenticated": True,
                "adapter_state": "ready",
                "degraded_reason": "",
                "contracts": ["work_items.write"],
                "next_best_sources": [],
                "operations": [
                    {
                        "contract": "work_items.write",
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "actions": [
                            {
                                "kind": "comment",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "add_comment_to_issue",
                                "target_reference_mode": "work_item",
                                "target_argument_name": "repo_full_name",
                                "number_argument_name": "issue_number",
                                "required_payload_fields": ["body"],
                                "payload_argument_map": {"body": "comment"},
                            }
                        ],
                    }
                ],
            },
        ]
    }

    with (
        patch("src.extensions.source_operations.build_source_review_plan", return_value=review_plan),
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
    ):
        plan = build_source_report_plan(
            intent="progress_review",
            focus="adapter-backed authenticated operations",
            target_reference="seraph-quest/seraph#343",
        )

    assert plan["publish_plan"]["status"] == "approval_required"
    assert plan["publish_plan"]["adapter"]["name"] == "github-managed"
    assert "does not provide a ready executable route for 'work_items.write'" in plan["warnings"][0]


def test_build_source_report_plan_falls_forward_from_degraded_preferred_write_adapter():
    review_plan = {
        "status": "ready",
        "intent": "progress_review",
        "title": "Progress Review",
        "recommended_runbooks": [],
        "recommended_starter_packs": [],
        "warnings": [],
        "steps": [
            {
                "id": "work_items",
                "contract": "work_items.read",
                "status": "ready",
                "source": "jira-managed",
            }
        ],
    }
    adapter_inventory = {
        "adapters": [
            {
                "name": "jira-managed",
                "provider": "jira",
                "source_kind": "managed_connector",
                "authenticated": True,
                "adapter_state": "degraded",
                "degraded_reason": "requires_runtime_adapter",
                "contracts": ["work_items.read", "work_items.write"],
                "next_best_sources": [],
                "operations": [
                    {
                        "contract": "work_items.read",
                        "input_mode": "query",
                        "executable": True,
                    },
                    {
                        "contract": "work_items.write",
                        "input_mode": "structured_action",
                        "executable": False,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "reason": "requires_runtime_adapter",
                        "actions": [
                            {
                                "kind": "comment",
                                "executable": False,
                                "runtime_server": "jira",
                                "tool_name": "comment_on_ticket",
                                "target_reference_mode": "work_item",
                                "required_payload_fields": ["body"],
                            }
                        ],
                    },
                ],
            },
            {
                "name": "github-managed",
                "provider": "github",
                "source_kind": "managed_connector",
                "authenticated": True,
                "adapter_state": "ready",
                "degraded_reason": "",
                "contracts": ["work_items.write"],
                "next_best_sources": [],
                "operations": [
                    {
                        "contract": "work_items.write",
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "actions": [
                            {
                                "kind": "comment",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "add_comment_to_issue",
                                "target_reference_mode": "work_item",
                                "target_argument_name": "repo_full_name",
                                "number_argument_name": "issue_number",
                                "required_payload_fields": ["body"],
                                "payload_argument_map": {"body": "comment"},
                            }
                        ],
                    }
                ],
            },
        ]
    }

    with (
        patch("src.extensions.source_operations.build_source_review_plan", return_value=review_plan),
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
    ):
        plan = build_source_report_plan(
            intent="progress_review",
            focus="adapter-backed authenticated operations",
            target_reference="seraph-quest/seraph#343",
        )

    assert plan["publish_plan"]["status"] == "approval_required"
    assert plan["publish_plan"]["adapter"]["name"] == "github-managed"
    assert "falling back to 'github-managed'" in plan["warnings"][0]


def test_build_source_report_plan_requires_target_reference_for_publication():
    review_plan = {
        "status": "ready",
        "intent": "progress_review",
        "title": "Progress Review",
        "recommended_runbooks": [],
        "recommended_starter_packs": [],
        "warnings": [],
        "steps": [
            {
                "id": "work_items",
                "contract": "work_items.read",
                "status": "ready",
                "source": "github-managed",
            }
        ],
    }
    adapter_inventory = {
        "adapters": [
            {
                "name": "github-managed",
                "provider": "github",
                "source_kind": "managed_connector",
                "authenticated": True,
                "adapter_state": "ready",
                "degraded_reason": "",
                "contracts": ["work_items.read", "work_items.write"],
                "next_best_sources": [],
                "operations": [
                    {
                        "contract": "work_items.write",
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "actions": [
                            {
                                "kind": "create",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "create_issue",
                                "target_reference_mode": "repository",
                                "target_argument_name": "repo_full_name",
                                "required_payload_fields": ["title", "body"],
                                "payload_argument_map": {"title": "title", "body": "body"},
                            }
                        ],
                    }
                ],
            }
        ]
    }

    with (
        patch("src.extensions.source_operations.build_source_review_plan", return_value=review_plan),
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
    ):
        plan = build_source_report_plan(
            intent="progress_review",
            focus="adapter-backed authenticated operations",
        )

    assert plan["publish_plan"]["status"] == "unavailable"
    assert "Provide target_reference" in plan["publish_plan"]["warnings"][0]
    assert "Provide target_reference" in plan["warnings"][-1]


def test_build_source_report_plan_supports_code_activity_review_publication():
    review_plan = {
        "status": "ready",
        "intent": "progress_review",
        "title": "Progress Review",
        "recommended_runbooks": [],
        "recommended_starter_packs": [],
        "warnings": [],
        "steps": [
            {
                "id": "work_items",
                "contract": "work_items.read",
                "status": "ready",
                "source": "github-managed",
            }
        ],
    }
    adapter_inventory = {
        "adapters": [
            {
                "name": "github-managed",
                "provider": "github",
                "source_kind": "managed_connector",
                "authenticated": True,
                "adapter_state": "ready",
                "degraded_reason": "",
                "contracts": ["work_items.read", "code_activity.write"],
                "next_best_sources": [],
                "operations": [
                    {
                        "contract": "code_activity.write",
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "actions": [
                            {
                                "kind": "review",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "add_review_to_pr",
                                "target_reference_mode": "pull_request",
                                "target_argument_name": "repo_full_name",
                                "number_argument_name": "pr_number",
                                "required_payload_fields": ["review"],
                                "allowed_payload_fields": ["review"],
                                "fixed_arguments": {"action": "COMMENT", "file_comments": []},
                                "payload_argument_map": {"review": "review"},
                            }
                        ],
                    }
                ],
            }
        ]
    }

    with (
        patch("src.extensions.source_operations.build_source_review_plan", return_value=review_plan),
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
    ):
        plan = build_source_report_plan(
            intent="progress_review",
            focus="adapter-backed authenticated operations",
            target_reference="seraph-quest/seraph/pull/343",
            publish_contract="code_activity.write",
            publish_action_kind="review",
        )

    assert plan["publish_contract"] == "code_activity.write"
    assert plan["publish_plan"]["status"] == "approval_required"
    assert plan["publish_plan"]["action"]["kind"] == "review"
    assert plan["publish_plan"]["approval_scope"]["target"]["contract"] == "code_activity.write"
    assert plan["publish_plan"]["approval_scope"]["action"]["fixed_argument_keys"] == ["action", "file_comments"]


def test_build_source_report_plan_requires_explicit_pull_request_reference_for_review_publication():
    review_plan = {
        "status": "ready",
        "intent": "progress_review",
        "title": "Progress Review",
        "recommended_runbooks": [],
        "recommended_starter_packs": [],
        "warnings": [],
        "steps": [
            {
                "id": "work_items",
                "contract": "work_items.read",
                "status": "ready",
                "source": "github-managed",
            }
        ],
    }
    adapter_inventory = {
        "adapters": [
            {
                "name": "github-managed",
                "provider": "github",
                "source_kind": "managed_connector",
                "authenticated": True,
                "adapter_state": "ready",
                "degraded_reason": "",
                "contracts": ["work_items.read", "code_activity.write"],
                "next_best_sources": [],
                "operations": [
                    {
                        "contract": "code_activity.write",
                        "input_mode": "structured_action",
                        "executable": True,
                        "mutating": True,
                        "requires_approval": True,
                        "approval_scope_type": "connector_mutation",
                        "audit_category": "authenticated_source_mutation",
                        "actions": [
                            {
                                "kind": "review",
                                "executable": True,
                                "runtime_server": "github",
                                "tool_name": "add_review_to_pr",
                                "target_reference_mode": "pull_request",
                                "target_argument_name": "repo_full_name",
                                "number_argument_name": "pr_number",
                                "required_payload_fields": ["review"],
                                "allowed_payload_fields": ["review"],
                                "fixed_arguments": {"action": "COMMENT", "file_comments": []},
                                "payload_argument_map": {"review": "review"},
                            }
                        ],
                    }
                ],
            }
        ]
    }

    with (
        patch("src.extensions.source_operations.build_source_review_plan", return_value=review_plan),
        patch("src.extensions.source_operations.list_source_capability_inventory", return_value={}),
        patch("src.extensions.source_operations.list_source_adapter_inventory", return_value=adapter_inventory),
    ):
        plan = build_source_report_plan(
            intent="progress_review",
            focus="adapter-backed authenticated operations",
            target_reference="seraph-quest/seraph#343",
            publish_contract="code_activity.write",
        )

    assert plan["publish_plan"]["status"] == "unavailable"
    assert "owner/repo/pull/number" in plan["publish_plan"]["warnings"][0]


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


@pytest.mark.asyncio
async def test_source_report_plan_endpoint_returns_review_and_publish_plan():
    report_plan = {
        "status": "ready",
        "title": "Progress Review for adapter-backed authenticated operations",
        "report_outline": ["Summarize movement."],
        "review_plan": {"summary": {"step_count": 2, "ready_step_count": 2, "degraded_step_count": 0}},
        "publish_plan": {
            "status": "approval_required",
            "adapter": {"name": "github-managed"},
            "action": {"kind": "comment"},
            "approval_scope": {"target": {"reference": "seraph-quest/seraph#343"}},
        },
        "warnings": [],
    }

    with patch("src.api.capabilities.build_source_report_plan", return_value=report_plan):
        async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
            response = await client.post(
                "/api/capabilities/source-report-plan",
                json={
                    "intent": "progress_review",
                    "focus": "adapter-backed authenticated operations",
                    "target_reference": "seraph-quest/seraph#343",
                    "publish_contract": "code_activity.write",
                },
            )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["publish_plan"]["action"]["kind"] == "comment"


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


def test_execute_source_mutation_tool_renders_execution_result():
    execution_result = {
        "status": "ok",
        "adapter": {"name": "github-managed", "adapter_state": "ready"},
        "action": {"kind": "comment", "runtime_server": "github", "tool_name": "add_comment_to_issue"},
        "result": {
            "title": "Comment posted",
            "location": "https://github.com/seraph-quest/seraph/issues/343#issuecomment-501",
        },
        "warnings": [],
    }

    with patch("src.tools.source_mutation_tool.execute_source_mutation_bundle", return_value=execution_result):
        result = execute_source_mutation.forward(
            contract="work_items.write",
            action_kind="comment",
            source="github-managed",
            target_reference="seraph-quest/seraph#343",
            payload_json='{"body":"Status posted."}',
        )

    assert "status: ok" in result
    assert "runtime: github/add_comment_to_issue" in result
    assert "location: https://github.com/seraph-quest/seraph/issues/343#issuecomment-501" in result


def test_plan_source_report_tool_renders_publish_plan():
    report_plan = {
        "status": "ready",
        "intent": "progress_review",
        "title": "Progress Review for adapter-backed authenticated operations",
        "publish_contract": "code_activity.write",
        "report_outline": [
            "Summarize the current state.",
            "List the strongest evidence.",
        ],
        "review_plan": {"summary": {"step_count": 3, "ready_step_count": 2, "degraded_step_count": 1}},
        "publish_plan": {
            "status": "approval_required",
            "adapter": {"name": "github-managed"},
            "action": {"kind": "comment"},
            "approval_scope": {"target": {"reference": "seraph-quest/seraph#343"}},
        },
        "warnings": [],
    }

    with patch("src.tools.source_mutation_tool.build_source_report_plan", return_value=report_plan):
        result = plan_source_report.forward(
            intent="progress_review",
            focus="adapter-backed authenticated operations",
            target_reference="seraph-quest/seraph#343",
            publish_contract="code_activity.write",
        )

    assert "status: ready" in result
    assert "publish_plan:" in result
    assert "- contract: code_activity.write" in result
    assert "- action: comment" in result
    assert "- target: seraph-quest/seraph#343" in result


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
