"""Provider-neutral source adapter inventory and normalized evidence collection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from src.browser.sessions import browser_session_runtime
from src.extensions.source_capabilities import list_source_capability_inventory
from src.audit.runtime import log_integration_event_sync
from src.tools.browser_tool import browse_webpage
from src.tools.mcp_manager import mcp_manager
from src.tools.web_search_tool import search_web_records


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _excerpt(content: str, *, limit: int = 240) -> str:
    collapsed = " ".join(str(content or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit - 1]}…"


def _normalize_token(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return "".join(ch for ch in text if ch.isalnum())


def _is_error_result(value: object) -> bool:
    return str(value or "").startswith("Error:")


def _parse_hostname(url: str) -> str:
    parsed = urlparse(url)
    return parsed.hostname or ""


@dataclass(frozen=True)
class SourceOperation:
    contract: str
    description: str
    input_mode: str
    executable: bool
    reason: str = ""
    runtime_server: str = ""
    tool_name: str = ""
    result_kind: str = ""
    per_page_param: str = ""
    mutating: bool = False
    requires_approval: bool = False
    approval_scope_type: str = ""
    audit_category: str = ""
    actions: tuple[dict[str, Any], ...] = ()

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "contract": self.contract,
            "description": self.description,
            "input_mode": self.input_mode,
            "executable": self.executable,
            "mutating": self.mutating,
            "requires_approval": self.requires_approval,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.runtime_server:
            payload["runtime_server"] = self.runtime_server
        if self.tool_name:
            payload["tool_name"] = self.tool_name
        if self.result_kind:
            payload["result_kind"] = self.result_kind
        if self.per_page_param:
            payload["per_page_param"] = self.per_page_param
        if self.approval_scope_type:
            payload["approval_scope_type"] = self.approval_scope_type
        if self.audit_category:
            payload["audit_category"] = self.audit_category
        if self.actions:
            payload["actions"] = [dict(action) for action in self.actions]
        return payload


_NATIVE_OPERATION_DEFINITIONS: dict[str, tuple[SourceOperation, ...]] = {
    "web_search": (
        SourceOperation(
            contract="source_discovery.read",
            description="Search the public web and return bounded discovery results.",
            input_mode="query",
            executable=True,
        ),
    ),
    "browse_webpage": (
        SourceOperation(
            contract="webpage.read",
            description="Read an explicit public URL into one normalized evidence item.",
            input_mode="url",
            executable=True,
        ),
    ),
    "browser_session": (
        SourceOperation(
            contract="webpage.read",
            description="Read an existing browser session ref or latest session snapshot.",
            input_mode="browser_ref_or_session",
            executable=True,
        ),
        SourceOperation(
            contract="browser_session.manage",
            description="Reuse an existing structured browser snapshot without claiming authenticated login control.",
            input_mode="browser_ref_or_session",
            executable=True,
        ),
    ),
}

_PREFERRED_SOURCE_ORDER: dict[str, tuple[str, ...]] = {
    "source_discovery.read": ("web_search",),
    "webpage.read": ("browse_webpage", "browser_session"),
    "browser_session.manage": ("browser_session",),
}

_CONNECTOR_MUTATION_TARGET_KIND: dict[str, str] = {
    "work_items.write": "work_item",
    "repository.write": "repository",
}

_SOURCE_REVIEW_TEMPLATES: dict[str, dict[str, Any]] = {
    "daily_review": {
        "title": "Daily Source Review",
        "description": "Review what moved during the day across external work items, code activity, and public context.",
        "recommended_runbooks": ["runbook:source-daily-review"],
        "recommended_starter_packs": ["source-daily-review"],
        "steps": (
            {
                "id": "work_items",
                "contract": "work_items.read",
                "purpose": "Capture tickets, pull requests, or tasks that moved during the review window.",
                "query_label": "recent work items",
            },
            {
                "id": "code_activity",
                "contract": "code_activity.read",
                "purpose": "Capture commits, branches, or code changes tied to the same focus area.",
                "query_label": "recent code activity",
            },
            {
                "id": "context",
                "contract": "source_discovery.read",
                "purpose": "Fill obvious evidence gaps from public status pages or supporting references when typed coverage is partial.",
                "query_label": "supporting public context",
            },
        ),
    },
    "progress_review": {
        "title": "Progress Review",
        "description": "Collect current external work evidence for one project, repo, or focus area.",
        "recommended_runbooks": ["runbook:source-progress-review"],
        "recommended_starter_packs": ["source-progress-review"],
        "steps": (
            {
                "id": "work_items",
                "contract": "work_items.read",
                "purpose": "Gather the work items that best represent the current state of execution.",
                "query_label": "current work items",
            },
            {
                "id": "code_activity",
                "contract": "code_activity.read",
                "purpose": "Gather recent code activity for the same focus area.",
                "query_label": "recent code activity",
            },
            {
                "id": "repositories",
                "contract": "repository.read",
                "purpose": "Add repository or project records when the review needs repo-level context.",
                "query_label": "repositories or projects",
            },
        ),
    },
    "goal_alignment": {
        "title": "Goal Alignment Review",
        "description": "Compare active external work evidence to the user's stated goals without hardcoding one provider pipeline.",
        "recommended_runbooks": ["runbook:source-goal-alignment"],
        "recommended_starter_packs": ["source-goal-alignment"],
        "steps": (
            {
                "id": "work_items",
                "contract": "work_items.read",
                "purpose": "Gather the work items most likely to represent movement against the goal.",
                "query_label": "goal-linked work items",
            },
            {
                "id": "code_activity",
                "contract": "code_activity.read",
                "purpose": "Gather code activity that advanced or distracted from the goal.",
                "query_label": "goal-linked code activity",
            },
            {
                "id": "context",
                "contract": "source_discovery.read",
                "purpose": "Use public context only when typed goal evidence is missing or ambiguous.",
                "query_label": "goal context or roadmap evidence",
            },
        ),
    },
}
_VALID_SOURCE_REVIEW_INTENTS = tuple(sorted(_SOURCE_REVIEW_TEMPLATES))


def _matching_untyped_sources(
    inventory: dict[str, Any],
    *,
    provider: str,
) -> list[dict[str, str]]:
    provider_token = _normalize_token(provider)
    matches: list[dict[str, str]] = []
    for item in inventory.get("untyped_sources", []):
        if not isinstance(item, dict):
            continue
        combined = " ".join(
            str(item.get(key) or "")
            for key in ("name", "provider", "url", "source")
        )
        combined_token = _normalize_token(combined)
        if provider_token and provider_token not in combined_token:
            continue
        matches.append(
            {
                "name": str(item.get("name") or ""),
                "reason": "raw_mcp_only",
                "description": "Only raw MCP access is visible for this provider, so Seraph cannot claim a typed adapter yet.",
            }
        )
    return matches


def _normalize_string_tuple(values: Any) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    if not isinstance(values, list):
        return ()
    for value in values:
        if not isinstance(value, str):
            continue
        item = value.strip()
        if not item or item in seen:
            continue
        normalized.append(item)
        seen.add(item)
    return tuple(normalized)


def _normalize_string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        normalized_key = key.strip()
        normalized_value = str(item or "").strip()
        if not normalized_key or not normalized_value:
            continue
        normalized[normalized_key] = normalized_value
    return normalized


def _is_mutating_contract(contract: str) -> bool:
    return contract.endswith(".write")


def _mutation_target_kind(contract: str) -> str:
    return _CONNECTOR_MUTATION_TARGET_KIND.get(contract, "external_record")


def _build_mutation_action_routes(
    route: dict[str, Any],
    *,
    bound_server: str,
    tools_by_name: dict[str, object],
) -> tuple[dict[str, Any], ...]:
    raw_actions = route.get("actions")
    if not isinstance(raw_actions, dict):
        return ()
    normalized_actions: list[dict[str, Any]] = []
    for action_name, raw_action in raw_actions.items():
        if not isinstance(action_name, str) or not action_name.strip() or not isinstance(raw_action, dict):
            continue
        normalized_name = action_name.strip()
        tool_names = _normalize_string_tuple(raw_action.get("tool_names"))
        required_payload_fields = _normalize_string_tuple(raw_action.get("required_payload_fields"))
        payload_argument_map = _normalize_string_dict(raw_action.get("payload_argument_map"))
        allowed_payload_fields = _normalize_string_tuple(raw_action.get("allowed_payload_fields"))
        if not allowed_payload_fields:
            inferred_allowed_fields = [
                field_name
                for field_name in (*required_payload_fields, *payload_argument_map.keys())
                if isinstance(field_name, str) and field_name.strip()
            ]
            allowed_payload_fields = _normalize_string_tuple(inferred_allowed_fields)
        target_reference_mode = str(raw_action.get("target_reference_mode") or "").strip() or "none"
        target_argument_name = str(raw_action.get("target_argument_name") or "").strip()
        number_argument_name = str(raw_action.get("number_argument_name") or "").strip()
        matched_tool_name = next((tool_name for tool_name in tool_names if tool_name in tools_by_name), "")
        if not tool_names:
            reason = "route_not_defined"
        elif not bound_server:
            reason = "no_connected_runtime"
        elif not matched_tool_name:
            reason = "missing_runtime_tool"
        else:
            reason = ""
        normalized_actions.append(
            {
                "kind": normalized_name,
                "executable": bool(matched_tool_name),
                **({"reason": reason} if reason else {}),
                **({"runtime_server": bound_server} if bound_server else {}),
                **({"tool_name": matched_tool_name} if matched_tool_name else {}),
                "required_payload_fields": list(required_payload_fields),
                "allowed_payload_fields": list(allowed_payload_fields),
                "payload_argument_map": payload_argument_map,
                "fixed_arguments": dict(raw_action.get("fixed_arguments") or {}),
                "fixed_argument_keys": sorted(
                    str(key)
                    for key in (raw_action.get("fixed_arguments") or {}).keys()
                    if isinstance(key, str) and key
                ),
                "target_reference_mode": target_reference_mode,
                **({"target_argument_name": target_argument_name} if target_argument_name else {}),
                **({"number_argument_name": number_argument_name} if number_argument_name else {}),
                "approval_scope_type": "connector_mutation",
                "audit_category": "authenticated_source_mutation",
            }
        )
    return tuple(normalized_actions)


def _runtime_adapter_payload(source: dict[str, Any]) -> dict[str, Any]:
    runtime_adapter = source.get("runtime_adapter")
    return dict(runtime_adapter) if isinstance(runtime_adapter, dict) else {}


def _runtime_route_map(runtime_adapter: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_routes = runtime_adapter.get("routes")
    if not isinstance(raw_routes, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for contract, payload in raw_routes.items():
        if not isinstance(contract, str) or not contract.strip() or not isinstance(payload, dict):
            continue
        result[contract.strip()] = payload
    return result


def _mcp_config_by_name() -> dict[str, dict[str, Any]]:
    config_by_name: dict[str, dict[str, Any]] = {}
    for item in mcp_manager.get_config():
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        config_by_name[name] = item
    return config_by_name


def _server_tools_by_name(server_name: str) -> dict[str, object]:
    tools_by_name: dict[str, object] = {}
    for tool in mcp_manager.get_server_tools(server_name):
        tool_name = str(getattr(tool, "name", "") or "").strip()
        if not tool_name:
            continue
        tools_by_name[tool_name] = tool
    return tools_by_name


def _resolve_mcp_runtime_operations(
    *,
    source: dict[str, Any],
    inventory: dict[str, Any],
    contracts: tuple[str, ...],
) -> tuple[str, str, list[dict[str, str]], tuple[SourceOperation, ...]]:
    runtime_state = str(source.get("runtime_state") or "unknown")
    if runtime_state != "ready":
        reason = runtime_state if runtime_state in {"requires_config", "disabled"} else "connector_not_ready"
        operations = tuple(
            SourceOperation(
                contract=contract,
                description=f"Typed {contract} contract advertised by {source.get('name')}.",
                input_mode="query",
                executable=False,
                reason=reason,
                mutating=_is_mutating_contract(contract),
                requires_approval=_is_mutating_contract(contract),
                approval_scope_type="connector_mutation" if _is_mutating_contract(contract) else "",
                audit_category="authenticated_source_mutation" if _is_mutating_contract(contract) else "",
            )
            for contract in contracts
        )
        return ("degraded" if contracts else "unavailable"), reason, _matching_untyped_sources(inventory, provider=str(source.get("provider") or "")), operations

    runtime_adapter = _runtime_adapter_payload(source)
    if str(runtime_adapter.get("kind") or "") != "mcp_server":
        reason = "no_runtime_adapter"
        operations = tuple(
            SourceOperation(
                contract=contract,
                description=f"Typed {contract} contract advertised by {source.get('name')}.",
                input_mode="query",
                executable=False,
                reason=reason,
                mutating=_is_mutating_contract(contract),
                requires_approval=_is_mutating_contract(contract),
                approval_scope_type="connector_mutation" if _is_mutating_contract(contract) else "",
                audit_category="authenticated_source_mutation" if _is_mutating_contract(contract) else "",
            )
            for contract in contracts
        )
        return "degraded", reason, _matching_untyped_sources(inventory, provider=str(source.get("provider") or "")), operations

    routes = _runtime_route_map(runtime_adapter)
    server_names = _normalize_string_tuple(runtime_adapter.get("server_names"))
    config_by_name = _mcp_config_by_name()
    bound_server = next(
        (
            server_name
            for server_name in server_names
            if config_by_name.get(server_name, {}).get("connected")
        ),
        "",
    )
    next_best = _matching_untyped_sources(inventory, provider=str(source.get("provider") or ""))
    operations: list[SourceOperation] = []
    executable_count = 0
    default_reason = "no_connected_runtime"
    for contract in contracts:
        mutating = _is_mutating_contract(contract)
        route = routes.get(contract)
        tool_names = _normalize_string_tuple(route.get("tool_names")) if route else ()
        result_kind = (
            str(route.get("result_kind") or "").strip()
            if route
            else ""
        )
        query_param = (
            str(route.get("query_param") or "").strip()
            if route
            else ""
        )
        per_page_param = (
            str(route.get("per_page_param") or "").strip()
            if route
            else ""
        )
        description = f"Typed {contract} contract advertised by {source.get('name')}."
        tools_by_name = _server_tools_by_name(bound_server) if bound_server else {}
        action_routes = _build_mutation_action_routes(
            route or {},
            bound_server=bound_server,
            tools_by_name=tools_by_name,
        )
        if route is None or not tool_names:
            if action_routes:
                executable_actions = [item for item in action_routes if bool(item.get("executable"))]
                reason = str(action_routes[0].get("reason") or "route_not_defined")
                operations.append(
                    SourceOperation(
                        contract=contract,
                        description=description,
                        input_mode="structured_action",
                        executable=bool(executable_actions),
                        reason=reason,
                        runtime_server=str(executable_actions[0].get("runtime_server") or "") if executable_actions else "",
                        tool_name=str(executable_actions[0].get("tool_name") or "") if executable_actions else "",
                        result_kind=result_kind,
                        per_page_param=per_page_param,
                        mutating=mutating,
                        requires_approval=mutating,
                        approval_scope_type="connector_mutation" if mutating else "",
                        audit_category="authenticated_source_mutation" if mutating else "",
                        actions=action_routes,
                    )
                )
                if executable_actions:
                    executable_count += 1
                else:
                    default_reason = reason
                continue
            operations.append(
                SourceOperation(
                    contract=contract,
                    description=description,
                    input_mode="query",
                    executable=False,
                    reason="route_not_defined",
                    result_kind=result_kind,
                    per_page_param=per_page_param,
                    mutating=mutating,
                    requires_approval=mutating,
                    approval_scope_type="connector_mutation" if mutating else "",
                    audit_category="authenticated_source_mutation" if mutating else "",
                )
            )
            default_reason = "route_not_defined"
            continue
        if not bound_server:
            operations.append(
                SourceOperation(
                    contract=contract,
                    description=description,
                    input_mode=query_param or "query",
                    executable=False,
                    reason="no_connected_runtime",
                    result_kind=result_kind,
                    per_page_param=per_page_param,
                    mutating=mutating,
                    requires_approval=mutating,
                    approval_scope_type="connector_mutation" if mutating else "",
                    audit_category="authenticated_source_mutation" if mutating else "",
                )
            )
            continue
        if action_routes:
            executable_actions = [item for item in action_routes if bool(item.get("executable"))]
            first_executable = executable_actions[0] if executable_actions else {}
            reason = (
                str(action_routes[0].get("reason") or default_reason or "missing_runtime_tool")
                if not executable_actions
                else ""
            )
            if executable_actions:
                executable_count += 1
            else:
                default_reason = reason
            operations.append(
                SourceOperation(
                    contract=contract,
                    description=description,
                    input_mode="structured_action",
                    executable=bool(executable_actions),
                    reason=reason,
                    runtime_server=str(first_executable.get("runtime_server") or bound_server or ""),
                    tool_name=str(first_executable.get("tool_name") or ""),
                    result_kind=result_kind,
                    per_page_param=per_page_param,
                    mutating=mutating,
                    requires_approval=mutating,
                    approval_scope_type="connector_mutation" if mutating else "",
                    audit_category="authenticated_source_mutation" if mutating else "",
                    actions=action_routes,
                )
            )
            continue
        matched_tool_name = next((tool_name for tool_name in tool_names if tool_name in tools_by_name), "")
        if not matched_tool_name:
            operations.append(
                SourceOperation(
                    contract=contract,
                    description=description,
                    input_mode=query_param or "query",
                    executable=False,
                    reason="missing_runtime_tool",
                    runtime_server=bound_server,
                    result_kind=result_kind,
                    per_page_param=per_page_param,
                    mutating=mutating,
                    requires_approval=mutating,
                    approval_scope_type="connector_mutation" if mutating else "",
                    audit_category="authenticated_source_mutation" if mutating else "",
                )
            )
            default_reason = "missing_runtime_tool"
            continue
        executable_count += 1
        operations.append(
            SourceOperation(
                contract=contract,
                description=description,
                input_mode=query_param or "query",
                executable=True,
                runtime_server=bound_server,
                tool_name=matched_tool_name,
                result_kind=result_kind,
                per_page_param=per_page_param,
                mutating=mutating,
                requires_approval=mutating,
                approval_scope_type="connector_mutation" if mutating else "",
                audit_category="authenticated_source_mutation" if mutating else "",
            )
        )

    if not operations:
        return "unavailable", "no_contracts", next_best, ()
    required_operations = [operation for operation in operations if not operation.contract.endswith(".write")]
    relevant_operations = required_operations or operations
    relevant_executable_count = sum(1 for operation in relevant_operations if operation.executable)
    if relevant_executable_count == len(relevant_operations):
        return "ready", "", [], tuple(operations)
    if relevant_executable_count > 0:
        return "degraded", "partial_runtime_support", next_best, tuple(operations)
    return "degraded", default_reason, next_best, tuple(operations)


def _adapter_state_for_source(source: dict[str, Any], inventory: dict[str, Any]) -> tuple[str, str, list[dict[str, str]], tuple[SourceOperation, ...]]:
    name = str(source.get("name") or "")
    source_kind = str(source.get("source_kind") or "")
    contracts = tuple(
        str(item) for item in source.get("contracts", [])
        if isinstance(item, str) and item.strip()
    )

    if source_kind == "native_tool" and name in _NATIVE_OPERATION_DEFINITIONS:
        return "ready", "", [], _NATIVE_OPERATION_DEFINITIONS[name]

    if source_kind == "managed_connector":
        return _resolve_mcp_runtime_operations(source=source, inventory=inventory, contracts=contracts)

    return "unavailable", "unsupported_source_kind", [], ()


def list_source_adapter_inventory(
    inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_inventory = inventory or list_source_capability_inventory()
    adapters: list[dict[str, Any]] = []
    ready_count = 0
    degraded_count = 0

    for source in normalized_inventory.get("typed_sources", []):
        if not isinstance(source, dict):
            continue
        adapter_state, degraded_reason, next_best_sources, operations = _adapter_state_for_source(
            source,
            normalized_inventory,
        )
        if adapter_state == "ready":
            ready_count += 1
        elif adapter_state == "degraded":
            degraded_count += 1
        adapters.append(
            {
                "name": str(source.get("name") or ""),
                "provider": str(source.get("provider") or ""),
                "source_kind": str(source.get("source_kind") or ""),
                "authenticated": bool(source.get("authenticated")),
                "runtime_state": str(source.get("runtime_state") or "unknown"),
                "adapter_state": adapter_state,
                "degraded_reason": degraded_reason or None,
                "contracts": list(source.get("contracts") or []),
                "operations": [item.as_payload() for item in operations],
                "next_best_sources": next_best_sources,
                "notes": list(source.get("notes") or []),
            }
        )

    adapters.sort(key=lambda item: (item["adapter_state"] != "ready", item["provider"], item["name"]))
    return {
        "summary": {
            "adapter_count": len(adapters),
            "ready_adapter_count": ready_count,
            "degraded_adapter_count": degraded_count,
        },
        "adapters": adapters,
        "selection_rules": [
            "Prefer connector-backed typed adapters when they are executable and truthfully available.",
            "Use native public-web adapters for public discovery and explicit URLs.",
            "If a typed connector advertises a contract but has no runtime adapter yet, mark it degraded and name the next best fallback instead of faking access.",
        ],
    }


def _find_adapter(adapters: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for adapter in adapters:
        if adapter.get("name") == name:
            return adapter
    return None


def _candidate_adapters_for_contract(adapters: list[dict[str, Any]], contract: str) -> list[dict[str, Any]]:
    ranked: list[tuple[int, dict[str, Any]]] = []
    preferred = _PREFERRED_SOURCE_ORDER.get(contract, ())
    preferred_index = {name: index for index, name in enumerate(preferred)}
    for adapter in adapters:
        contracts = adapter.get("contracts") or []
        if contract not in contracts:
            continue
        base_rank = preferred_index.get(str(adapter.get("name") or ""), len(preferred) + 5)
        if adapter.get("adapter_state") == "ready":
            state_rank = 0
        elif adapter.get("adapter_state") == "degraded":
            state_rank = 1
        else:
            state_rank = 2
        ranked.append((state_rank * 100 + base_rank, adapter))
    ranked.sort(key=lambda item: item[0])
    return [item[1] for item in ranked]


def _normalize_review_template(template: dict[str, Any], intent: str) -> dict[str, Any] | None:
    title = str(template.get("title") or "").strip()
    description = str(template.get("description") or "").strip()
    steps = template.get("steps")
    if not title or not description or not isinstance(steps, tuple):
        return None
    normalized_steps: list[dict[str, str]] = []
    for raw_step in steps:
        if not isinstance(raw_step, dict):
            continue
        step_id = str(raw_step.get("id") or "").strip()
        contract = str(raw_step.get("contract") or "").strip()
        purpose = str(raw_step.get("purpose") or "").strip()
        query_label = str(raw_step.get("query_label") or "").strip()
        if not step_id or not contract or not purpose or not query_label:
            continue
        normalized_steps.append(
            {
                "id": step_id,
                "contract": contract,
                "purpose": purpose,
                "query_label": query_label,
            }
        )
    if not normalized_steps:
        return None
    return {
        "intent": intent,
        "title": title,
        "description": description,
        "recommended_runbooks": [
            str(item).strip()
            for item in template.get("recommended_runbooks", [])
            if isinstance(item, str) and item.strip()
        ],
        "recommended_starter_packs": [
            str(item).strip()
            for item in template.get("recommended_starter_packs", [])
            if isinstance(item, str) and item.strip()
        ],
        "steps": normalized_steps,
    }


def list_source_review_templates() -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    for intent in _VALID_SOURCE_REVIEW_INTENTS:
        template = _normalize_review_template(_SOURCE_REVIEW_TEMPLATES[intent], intent)
        if template is not None:
            templates.append(template)
    return templates


def _review_window_phrase(time_window: str) -> str:
    cleaned = " ".join(time_window.split()).strip()
    return cleaned or "the review window"


def _review_focus_phrase(focus: str, goal_context: str) -> str:
    cleaned_focus = " ".join(focus.split()).strip()
    if cleaned_focus:
        return cleaned_focus
    cleaned_goal = " ".join(goal_context.split()).strip()
    if cleaned_goal:
        return cleaned_goal
    return "the current focus"


def _suggested_query(*, query_label: str, focus: str, goal_context: str, time_window: str) -> str:
    focus_phrase = _review_focus_phrase(focus, goal_context)
    window_phrase = _review_window_phrase(time_window)
    return f"{query_label} for {focus_phrase} during {window_phrase}"


def _query_guidance(contract: str) -> str:
    if contract == "source_discovery.read":
        return "Use a plain-language search that names the focus area, project, or goal. Typed search syntax is optional."
    if contract == "repository.read":
        return "Name the repo, project, or owner whose repository context matters to the review."
    return "Name the project, goal, or repo plus the review window. Provider-specific syntax is optional unless you need tighter filtering."


def _select_review_adapter(
    *,
    adapters: list[dict[str, Any]],
    contract: str,
    preferred_source: str,
) -> tuple[dict[str, Any] | None, list[dict[str, str]], list[str]]:
    warnings: list[str] = []
    next_best: list[dict[str, str]] = []
    if preferred_source:
        preferred_adapter = _find_adapter(adapters, preferred_source)
        if preferred_adapter is None:
            warnings.append(f"Requested source '{preferred_source}' is not a known typed adapter.")
        elif contract in (preferred_adapter.get("contracts") or []):
            next_best = list(preferred_adapter.get("next_best_sources") or [])
            return preferred_adapter, next_best, warnings
    candidates = _candidate_adapters_for_contract(adapters, contract)
    if not candidates:
        return None, [], warnings
    selected = candidates[0]
    next_best = list(selected.get("next_best_sources") or [])
    if not next_best:
        next_best = [
            {
                "name": str(candidate.get("name") or ""),
                "reason": "typed_fallback",
                "description": "Another typed adapter can satisfy the same contract.",
            }
            for candidate in candidates[1:4]
        ]
    return selected, next_best, warnings


def _select_mutation_adapter(
    *,
    adapters: list[dict[str, Any]],
    contract: str,
    preferred_source: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    candidates = _candidate_adapters_for_contract(adapters, contract)
    if not candidates:
        return None, warnings
    if preferred_source:
        preferred_adapter = _find_adapter(adapters, preferred_source)
        preferred_operation = (
            _operation_for_contract(preferred_adapter, contract)
            if preferred_adapter is not None
            else None
        )
        if (
            preferred_adapter is not None
            and contract in (preferred_adapter.get("contracts") or [])
            and bool((preferred_operation or {}).get("executable"))
        ):
            return preferred_adapter, warnings
    selected = candidates[0]
    if preferred_source and str(selected.get("name") or "") != preferred_source:
        warnings.append(
            f"Report publication is falling back to '{selected.get('name')}' because "
            f"'{preferred_source}' does not provide a ready executable route for '{contract}'."
        )
    return selected, warnings


def _build_search_item(record: dict[str, Any], source_name: str) -> dict[str, Any]:
    url = str(record.get("href") or "")
    body = str(record.get("body") or "")
    title = str(record.get("title") or url or "Search result")
    return {
        "id": url or title,
        "kind": "search_result",
        "contract": "source_discovery.read",
        "source_name": source_name,
        "provider": "seraph",
        "source_kind": "native_tool",
        "title": title,
        "location": url,
        "hostname": _parse_hostname(url),
        "summary": body,
        "excerpt": _excerpt(body),
        "content": body,
        "observed_at": _utc_now(),
        "metadata": {},
    }


def _build_page_item(url: str, content: str, source_name: str, *, kind: str = "webpage") -> dict[str, Any]:
    return {
        "id": url,
        "kind": kind,
        "contract": "webpage.read",
        "source_name": source_name,
        "provider": "seraph",
        "source_kind": "native_tool",
        "title": url,
        "location": url,
        "hostname": _parse_hostname(url),
        "summary": _excerpt(content),
        "excerpt": _excerpt(content),
        "content": content,
        "observed_at": _utc_now(),
        "metadata": {},
    }


def _build_browser_item(payload: dict[str, Any], source_name: str) -> dict[str, Any]:
    content = str(payload.get("content") or "")
    return {
        "id": str(payload.get("ref") or payload.get("session_id") or payload.get("url") or ""),
        "kind": "browser_snapshot",
        "contract": "webpage.read",
        "source_name": source_name,
        "provider": str(payload.get("provider_name") or "seraph"),
        "source_kind": "native_tool",
        "title": str(payload.get("ref") or payload.get("url") or "Browser snapshot"),
        "location": str(payload.get("url") or ""),
        "hostname": _parse_hostname(str(payload.get("url") or "")),
        "summary": str(payload.get("summary") or _excerpt(content)),
        "excerpt": _excerpt(content),
        "content": content,
        "observed_at": str(payload.get("created_at") or _utc_now()),
        "metadata": {
            "session_id": str(payload.get("session_id") or ""),
            "ref": str(payload.get("ref") or ""),
            "provider_kind": str(payload.get("provider_kind") or ""),
            "execution_mode": str(payload.get("execution_mode") or ""),
        },
    }


def _browser_session_payload(*, owner_session_id: str, ref: str, session_id: str) -> dict[str, Any] | None:
    if ref:
        return browser_session_runtime.read_ref(ref, owner_session_id=owner_session_id)
    if session_id:
        session = browser_session_runtime.get_session(session_id, owner_session_id=owner_session_id)
        if session is None:
            return None
        snapshots = session.get("snapshots") or []
        if not snapshots:
            return None
        latest_ref = str(snapshots[-1].get("ref") or "")
        if not latest_ref:
            return None
        return browser_session_runtime.read_ref(latest_ref, owner_session_id=owner_session_id)
    return None


def _operation_for_contract(adapter: dict[str, Any], contract: str) -> dict[str, Any] | None:
    for operation in adapter.get("operations") or []:
        if isinstance(operation, dict) and str(operation.get("contract") or "") == contract:
            return operation
    return None


def _normalize_change_fields(fields: Any) -> list[str]:
    if not isinstance(fields, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in fields:
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict):
            value = str(item.get("name") or item.get("key") or "").strip()
        else:
            value = ""
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized


def _normalize_payload_map(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            continue
        normalized_key = key.strip()
        if not normalized_key:
            continue
        result[normalized_key] = value
    return result


def _parse_repository_reference(target_reference: str) -> str:
    normalized = " ".join(str(target_reference or "").split()).strip().strip("/")
    if not normalized or "#" in normalized or normalized.count("/") != 1:
        return ""
    owner, repo = normalized.split("/", 1)
    if not owner or not repo:
        return ""
    return f"{owner}/{repo}"


def _parse_work_item_reference(target_reference: str) -> tuple[str, int] | None:
    normalized = " ".join(str(target_reference or "").split()).strip()
    if "#" not in normalized:
        return None
    repository, _, suffix = normalized.rpartition("#")
    repo_full_name = _parse_repository_reference(repository)
    if not repo_full_name:
        return None
    try:
        number = int(suffix)
    except ValueError:
        return None
    if number <= 0:
        return None
    return repo_full_name, number


def _parse_pull_request_reference(target_reference: str) -> tuple[str, int] | None:
    normalized = " ".join(str(target_reference or "").split()).strip().strip("/")
    if not normalized:
        return None
    parts = normalized.split("/")
    if len(parts) != 4 or parts[2] not in {"pull", "pulls"}:
        return None
    repo_full_name = _parse_repository_reference("/".join(parts[:2]))
    if not repo_full_name:
        return None
    try:
        number = int(parts[3])
    except ValueError:
        return None
    if number <= 0:
        return None
    return repo_full_name, number


def build_source_mutation_plan(
    *,
    contract: str,
    source: str = "",
    action_kind: str = "",
    action_summary: str = "",
    target_reference: str = "",
    fields: list[str] | None = None,
) -> dict[str, Any]:
    normalized_contract = contract.strip()
    requested_source = source.strip()
    normalized_action_kind = action_kind.strip().lower()
    normalized_summary = " ".join(action_summary.split()).strip()
    normalized_target_reference = " ".join(target_reference.split()).strip()
    field_names = _normalize_change_fields(list(fields or []))

    response: dict[str, Any] = {
        "status": "unavailable",
        "request": {
            "contract": normalized_contract,
            "source": requested_source,
            "action_kind": normalized_action_kind,
            "action_summary": normalized_summary,
            "target_reference": normalized_target_reference,
            "fields": field_names,
        },
        "adapter": None,
        "operation": None,
        "action": None,
        "available_actions": [],
        "requires_approval": False,
        "approval_scope": None,
        "approval_context": None,
        "audit_payload": None,
        "warnings": [],
        "next_best_sources": [],
    }

    if not _is_mutating_contract(normalized_contract):
        response["status"] = "failed"
        response["warnings"].append(
            f"Contract '{normalized_contract or contract}' is not a typed mutation contract."
        )
        return response

    inventory = list_source_capability_inventory()
    adapter_inventory = list_source_adapter_inventory(inventory)
    adapters = adapter_inventory["adapters"]
    selected_adapter = _find_adapter(adapters, requested_source) if requested_source else None
    candidates = _candidate_adapters_for_contract(adapters, normalized_contract)
    if selected_adapter is None:
        selected_adapter = candidates[0] if candidates else None

    if selected_adapter is None:
        response["warnings"].append(
            f"No typed source adapter currently advertises mutation contract '{normalized_contract}'."
        )
        return response

    selected_operation = _operation_for_contract(selected_adapter, normalized_contract)
    response["adapter"] = {
        "name": selected_adapter["name"],
        "provider": selected_adapter["provider"],
        "source_kind": selected_adapter["source_kind"],
        "authenticated": selected_adapter["authenticated"],
        "adapter_state": selected_adapter["adapter_state"],
        "degraded_reason": selected_adapter.get("degraded_reason"),
    }
    response["next_best_sources"] = list(selected_adapter.get("next_best_sources") or [])

    if selected_operation is None:
        response["warnings"].append(
            f"Source '{selected_adapter['name']}' does not currently define a mutation route for '{normalized_contract}'."
        )
        return response

    available_actions = [
        dict(action)
        for action in selected_operation.get("actions") or []
        if isinstance(action, dict)
    ]
    selected_action = None
    if available_actions:
        response["available_actions"] = [
            {
                "kind": str(action.get("kind") or ""),
                "executable": bool(action.get("executable")),
                "target_reference_mode": str(action.get("target_reference_mode") or "none"),
                "required_payload_fields": list(action.get("required_payload_fields") or []),
                "allowed_payload_fields": list(action.get("allowed_payload_fields") or []),
                "fixed_argument_keys": sorted(
                    str(key)
                    for key in (action.get("fixed_arguments") or {}).keys()
                    if isinstance(key, str) and key
                ),
            }
            for action in available_actions
        ]
        if normalized_action_kind:
            selected_action = next(
                (action for action in available_actions if str(action.get("kind") or "") == normalized_action_kind),
                None,
            )
            if selected_action is None:
                response["status"] = "failed"
                response["operation"] = dict(selected_operation)
                response["warnings"].append(
                    "Unknown action_kind for "
                    f"'{normalized_contract}': {normalized_action_kind}. "
                    f"Available actions: {', '.join(item['kind'] for item in response['available_actions'])}."
                )
                return response
        elif len(available_actions) == 1:
            selected_action = available_actions[0]
        else:
            response["status"] = "failed"
            response["operation"] = dict(selected_operation)
            response["warnings"].append(
                f"Mutation contract '{normalized_contract}' requires an explicit action_kind. "
                f"Available actions: {', '.join(item['kind'] for item in response['available_actions'])}."
            )
            return response

    effective_operation = dict(selected_action or selected_operation)
    effective_action_kind = str(selected_action.get("kind") or normalized_action_kind) if selected_action is not None else ""
    response["operation"] = dict(selected_operation)
    if selected_action is not None:
        response["action"] = dict(selected_action)
    response["requires_approval"] = bool(effective_operation.get("requires_approval") or selected_operation.get("requires_approval"))
    approval_scope_type = str(
        effective_operation.get("approval_scope_type")
        or selected_operation.get("approval_scope_type")
        or "connector_mutation"
    ).strip()
    audit_category = str(
        effective_operation.get("audit_category")
        or selected_operation.get("audit_category")
        or "authenticated_source_mutation"
    ).strip()
    target_kind = _mutation_target_kind(normalized_contract)
    approval_scope = {
        "type": approval_scope_type,
        "target": {
            "source": str(selected_adapter.get("name") or ""),
            "provider": str(selected_adapter.get("provider") or ""),
            "contract": normalized_contract,
            "target_kind": target_kind,
            "reference": normalized_target_reference,
        },
        "change_scope": {
            "action_summary": normalized_summary,
            "field_names": field_names,
            "field_count": len(field_names),
        },
        "runtime_scope": {
            "runtime_server": str(effective_operation.get("runtime_server") or selected_operation.get("runtime_server") or ""),
            "tool_name": str(effective_operation.get("tool_name") or selected_operation.get("tool_name") or ""),
            "adapter_state": str(selected_adapter.get("adapter_state") or "unknown"),
            "route_executable": bool(effective_operation.get("executable")),
        },
    }
    if selected_action is not None:
        approval_scope["action"] = {
            "kind": str(selected_action.get("kind") or normalized_action_kind),
            "target_reference_mode": str(selected_action.get("target_reference_mode") or "none"),
            "required_payload_fields": list(selected_action.get("required_payload_fields") or []),
            "allowed_payload_fields": list(selected_action.get("allowed_payload_fields") or []),
            "fixed_argument_keys": sorted(
                str(key)
                for key in (selected_action.get("fixed_arguments") or {}).keys()
                if isinstance(key, str) and key
            ),
        }
    approval_context = {
        "risk_level": "high",
        "authenticated_source": bool(selected_adapter.get("authenticated")),
        "execution_boundaries": [
            "external_mcp",
            "authenticated_external_source",
            approval_scope_type,
        ],
        "source_systems": [str(selected_adapter.get("provider") or "")],
        "mutation_contract": normalized_contract,
        "source_adapter": str(selected_adapter.get("name") or ""),
    }
    if effective_action_kind:
        approval_context["mutation_action_kind"] = effective_action_kind
    audit_payload = {
        "event_type": audit_category,
        "source": str(selected_adapter.get("name") or ""),
        "provider": str(selected_adapter.get("provider") or ""),
        "contract": normalized_contract,
        "target_kind": target_kind,
        "target_reference": normalized_target_reference,
        "field_names": field_names,
        "runtime_server": str(effective_operation.get("runtime_server") or selected_operation.get("runtime_server") or ""),
        "tool_name": str(effective_operation.get("tool_name") or selected_operation.get("tool_name") or ""),
    }
    if effective_action_kind:
        audit_payload["action_kind"] = effective_action_kind
    response["approval_scope"] = approval_scope
    response["approval_context"] = approval_context
    response["audit_payload"] = audit_payload

    if not bool(effective_operation.get("executable")):
        response["status"] = "degraded"
        reason = str(
            effective_operation.get("reason")
            or selected_operation.get("reason")
            or selected_adapter.get("degraded_reason")
            or "unavailable"
        ).strip()
        response["warnings"].append(
            f"Source '{selected_adapter['name']}' cannot execute '{normalized_contract}' right now ({reason})."
        )
        return response

    response["status"] = "approval_required"
    return response


def execute_source_mutation_bundle(
    *,
    contract: str,
    source: str = "",
    action_kind: str = "",
    target_reference: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_payload = _normalize_payload_map(payload or {})
    plan = build_source_mutation_plan(
        contract=contract,
        source=source,
        action_kind=action_kind,
        action_summary=str(normalized_payload.get("summary") or ""),
        target_reference=target_reference,
        fields=list(normalized_payload.keys()),
    )
    response: dict[str, Any] = {
        "status": str(plan.get("status") or "unavailable"),
        "request": {
            "contract": contract.strip(),
            "source": source.strip(),
            "action_kind": action_kind.strip().lower(),
            "target_reference": " ".join(str(target_reference or "").split()).strip(),
            "payload_fields": list(normalized_payload.keys()),
        },
        "adapter": plan.get("adapter"),
        "operation": plan.get("operation"),
        "action": plan.get("action"),
        "approval_scope": plan.get("approval_scope"),
        "approval_context": plan.get("approval_context"),
        "audit_payload": plan.get("audit_payload"),
        "warnings": list(plan.get("warnings") or []),
        "result": None,
    }
    if plan.get("status") != "approval_required":
        return response

    selected_action = plan.get("action") or {}
    runtime_server = str(selected_action.get("runtime_server") or "")
    tool_name = str(selected_action.get("tool_name") or "")
    tool = _server_tools_by_name(runtime_server).get(tool_name)
    if tool is None:
        response["status"] = "failed"
        response["warnings"].append(
            f"Source '{source or (plan.get('adapter') or {}).get('name', '')}' is missing runtime tool '{tool_name}' on '{runtime_server}'."
        )
        log_integration_event_sync(
            integration_type="authenticated_source_mutation",
            name=str((plan.get("adapter") or {}).get("name") or source or "source_mutation"),
            outcome="failed",
            details={
                **dict(plan.get("audit_payload") or {}),
                "failure_reason": "missing_runtime_tool",
            },
        )
        return response

    required_payload_fields = _normalize_string_tuple(selected_action.get("required_payload_fields"))
    missing_fields = [field for field in required_payload_fields if field not in normalized_payload]
    if missing_fields:
        response["status"] = "failed"
        response["warnings"].append(
            f"Missing required payload fields for {action_kind.strip().lower() or 'mutation'}: {', '.join(missing_fields)}."
        )
        return response
    allowed_payload_fields = _normalize_string_tuple(selected_action.get("allowed_payload_fields"))
    if allowed_payload_fields:
        unexpected_fields = [
            field_name
            for field_name in normalized_payload
            if field_name not in allowed_payload_fields
        ]
        if unexpected_fields:
            response["status"] = "failed"
            response["warnings"].append(
                "Payload included undeclared fields for "
                f"{action_kind.strip().lower() or 'mutation'}: {', '.join(unexpected_fields)}. "
                f"Allowed fields: {', '.join(allowed_payload_fields)}."
            )
            return response

    target_reference_mode = str(selected_action.get("target_reference_mode") or "none")
    fixed_arguments = selected_action.get("fixed_arguments")
    arguments: dict[str, Any] = dict(fixed_arguments) if isinstance(fixed_arguments, dict) else {}
    consumed_payload_fields: set[str] = set()
    if target_reference_mode == "repository":
        repository = _parse_repository_reference(target_reference)
        if not repository:
            response["status"] = "failed"
            response["warnings"].append("target_reference must be an owner/repo reference for this mutation.")
            return response
        target_argument_name = str(selected_action.get("target_argument_name") or "").strip()
        if target_argument_name:
            arguments[target_argument_name] = repository
    elif target_reference_mode == "work_item":
        work_item_reference = _parse_work_item_reference(target_reference)
        if work_item_reference is None:
            response["status"] = "failed"
            response["warnings"].append("target_reference must be an owner/repo#number reference for this mutation.")
            return response
        repository, issue_number = work_item_reference
        target_argument_name = str(selected_action.get("target_argument_name") or "").strip()
        number_argument_name = str(selected_action.get("number_argument_name") or "").strip()
        if target_argument_name:
            arguments[target_argument_name] = repository
        if number_argument_name:
            arguments[number_argument_name] = issue_number
    elif target_reference_mode == "pull_request":
        pull_request_reference = _parse_pull_request_reference(target_reference)
        if pull_request_reference is None:
            response["status"] = "failed"
            response["warnings"].append(
                "target_reference must be an owner/repo/pull/number reference for this mutation."
            )
            return response
        repository, pr_number = pull_request_reference
        target_argument_name = str(selected_action.get("target_argument_name") or "").strip()
        number_argument_name = str(selected_action.get("number_argument_name") or "").strip()
        if target_argument_name:
            arguments[target_argument_name] = repository
        if number_argument_name:
            arguments[number_argument_name] = pr_number

    payload_argument_map = _normalize_string_dict(selected_action.get("payload_argument_map"))
    for payload_field, argument_name in payload_argument_map.items():
        if payload_field not in normalized_payload:
            continue
        arguments[argument_name] = normalized_payload[payload_field]
        consumed_payload_fields.add(payload_field)
    if isinstance(fixed_arguments, dict):
        for argument_name, value in fixed_arguments.items():
            if isinstance(argument_name, str) and argument_name:
                arguments[argument_name] = value

    try:
        raw_result = tool(**arguments)
    except Exception as exc:
        response["status"] = "failed"
        response["warnings"].append(str(exc))
        log_integration_event_sync(
            integration_type="authenticated_source_mutation",
            name=str((plan.get("adapter") or {}).get("name") or source or "source_mutation"),
            outcome="failed",
            details={
                **dict(plan.get("audit_payload") or {}),
                "action_arguments": sorted(arguments.keys()),
                "error": str(exc),
            },
        )
        return response

    records = _extract_records(raw_result)
    normalized_result = (
        _build_connector_item(
            records[0],
            contract=str(plan.get("request", {}).get("contract") or contract),
            source_name=str((plan.get("adapter") or {}).get("name") or source or ""),
            provider=str((plan.get("adapter") or {}).get("provider") or ""),
            result_kind=str((plan.get("operation") or {}).get("result_kind") or "external_record"),
            runtime_server=runtime_server,
        )
        if records
        else {
            "summary": str(raw_result or ""),
            "title": str((plan.get("approval_scope") or {}).get("target", {}).get("reference") or "authenticated mutation"),
            "contract": str(plan.get("request", {}).get("contract") or contract),
            "source_name": str((plan.get("adapter") or {}).get("name") or source or ""),
            "provider": str((plan.get("adapter") or {}).get("provider") or ""),
            "kind": str((plan.get("operation") or {}).get("result_kind") or "external_record"),
            "metadata": {"runtime_server": runtime_server},
        }
    )
    response["status"] = "ok"
    response["result"] = normalized_result
    log_integration_event_sync(
        integration_type="authenticated_source_mutation",
        name=str((plan.get("adapter") or {}).get("name") or source or "source_mutation"),
        outcome="completed",
        details={
            **dict(plan.get("audit_payload") or {}),
            "action_arguments": sorted(arguments.keys()),
            "result_kind": str(normalized_result.get("kind") or ""),
        },
    )
    return response


def build_source_report_plan(
    *,
    intent: str,
    focus: str = "",
    goal_context: str = "",
    time_window: str = "",
    source: str = "",
    target_reference: str = "",
    publish_action_kind: str = "",
    publish_contract: str = "",
) -> dict[str, Any]:
    review_plan = build_source_review_plan(
        intent=intent,
        focus=focus,
        goal_context=goal_context,
        time_window=time_window,
        source=source,
    )
    focus_phrase = _review_focus_phrase(focus, goal_context)
    report_title = f"{review_plan.get('title', 'Source report')} for {focus_phrase}"
    report_outline = [
        f"Summarize the current state of {focus_phrase}.",
        "List the strongest evidence by source contract and adapter.",
        "Call out blockers, contradictory evidence, and obvious next actions.",
        "End with the smallest concrete follow-through step.",
    ]
    inventory = list_source_capability_inventory()
    adapter_inventory = list_source_adapter_inventory(inventory)
    adapters = adapter_inventory["adapters"]
    publish_source_hint = ""
    for step in review_plan.get("steps", []):
        if not isinstance(step, dict):
            continue
        if str(step.get("contract") or "") != "work_items.read":
            continue
        if str(step.get("status") or "") not in {"ready", "degraded"}:
            continue
        publish_source_hint = str(step.get("source") or "")
        if publish_source_hint:
            break
    normalized_publish_contract = publish_contract.strip()
    if not normalized_publish_contract:
        normalized_publish_contract = "work_items.write"
    normalized_publish_action = publish_action_kind.strip().lower()
    repository_target_reference = _parse_repository_reference(target_reference)
    pull_request_target_reference = _parse_pull_request_reference(target_reference)
    if not normalized_publish_action:
        if normalized_publish_contract == "code_activity.write":
            if pull_request_target_reference is not None:
                normalized_publish_action = "review"
            elif repository_target_reference:
                normalized_publish_action = "create"
        else:
            normalized_publish_action = "comment" if "#" in target_reference else "create"
    publish_plan = None
    warnings = list(review_plan.get("warnings") or [])
    publish_adapter, publish_adapter_warnings = _select_mutation_adapter(
        adapters=adapters,
        contract=normalized_publish_contract,
        preferred_source=publish_source_hint,
    )
    warnings.extend(publish_adapter_warnings)
    if (
        normalized_publish_contract == "code_activity.write"
        and normalized_publish_action == "review"
        and pull_request_target_reference is None
    ):
        warning = (
            "code_activity.write review publication requires target_reference like "
            "owner/repo/pull/number."
        )
        warnings.append(warning)
        publish_plan = {
            "status": "unavailable",
            "adapter": {
                "name": str(publish_adapter.get("name") or ""),
                "provider": str(publish_adapter.get("provider") or ""),
            } if publish_adapter is not None else None,
            "action": {"kind": "review"},
            "warnings": [warning],
        }
    elif normalized_publish_contract == "code_activity.write" and not normalized_publish_action:
        warning = (
            "Provide target_reference like owner/repo for PR creation or "
            "owner/repo/pull/number for PR review publication."
        )
        warnings.append(warning)
        publish_plan = {
            "status": "unavailable",
            "adapter": {
                "name": str(publish_adapter.get("name") or ""),
                "provider": str(publish_adapter.get("provider") or ""),
            } if publish_adapter is not None else None,
            "warnings": [warning],
        }
    elif publish_adapter is not None:
        publish_plan = build_source_mutation_plan(
            contract=normalized_publish_contract,
            source=str(publish_adapter.get("name") or ""),
            action_kind=normalized_publish_action,
            action_summary=f"Publish a source report for {focus_phrase}",
            target_reference=target_reference,
            fields=(
                ["title", "body", "head_branch", "base_branch"]
                if normalized_publish_contract == "code_activity.write" and normalized_publish_action == "create"
                else (["review"] if normalized_publish_contract == "code_activity.write" else (["title", "body"] if normalized_publish_action == "create" else ["body"]))
            ),
        )
        if not target_reference.strip():
            publish_plan = {
                **publish_plan,
                "status": "unavailable",
                "warnings": list(
                    dict.fromkeys(
                        [
                            *(publish_plan.get("warnings") or []),
                            "Provide target_reference to publish the report through the authenticated source.",
                        ]
                    )
                ),
            }
            warnings.append("Provide target_reference to publish the report through the authenticated source.")
    else:
        warnings.append(
            f"No authenticated adapter is currently available for report publication via '{normalized_publish_contract}'."
        )

    return {
        "status": review_plan.get("status", "unavailable"),
        "intent": str(review_plan.get("intent") or intent),
        "title": report_title,
        "focus": focus.strip(),
        "goal_context": goal_context.strip(),
        "time_window": time_window.strip(),
        "publish_contract": normalized_publish_contract,
        "report_outline": report_outline,
        "review_plan": review_plan,
        "publish_plan": publish_plan,
        "recommended_runbooks": list(dict.fromkeys([
            *(review_plan.get("recommended_runbooks") or []),
            "runbook:source-progress-report",
        ])),
        "recommended_starter_packs": list(dict.fromkeys([
            *(review_plan.get("recommended_starter_packs") or []),
            "source-progress-report",
        ])),
        "warnings": warnings,
    }


def build_source_review_plan(
    *,
    intent: str,
    focus: str = "",
    goal_context: str = "",
    time_window: str = "",
    source: str = "",
    url: str = "",
) -> dict[str, Any]:
    normalized_intent = intent.strip().lower()
    if normalized_intent not in _SOURCE_REVIEW_TEMPLATES:
        return {
            "status": "failed",
            "warnings": [
                f"intent must be one of {', '.join(_VALID_SOURCE_REVIEW_INTENTS)}",
            ],
            "steps": [],
        }

    template = _normalize_review_template(_SOURCE_REVIEW_TEMPLATES[normalized_intent], normalized_intent)
    if template is None:
        return {
            "status": "failed",
            "warnings": [f"intent '{normalized_intent}' is misconfigured."],
            "steps": [],
        }

    inventory = list_source_capability_inventory()
    adapter_inventory = list_source_adapter_inventory(inventory)
    adapters = adapter_inventory["adapters"]

    steps: list[dict[str, Any]] = []
    warnings: list[str] = []
    ready_step_count = 0
    degraded_step_count = 0
    unavailable_step_count = 0
    for spec in template["steps"]:
        contract = spec["contract"]
        selected_adapter, next_best, step_warnings = _select_review_adapter(
            adapters=adapters,
            contract=contract,
            preferred_source=source.strip(),
        )
        warnings.extend(step_warnings)
        if selected_adapter is None:
            unavailable_step_count += 1
            steps.append(
                {
                    "id": spec["id"],
                    "contract": contract,
                    "purpose": spec["purpose"],
                    "status": "unavailable",
                    "source": "",
                    "provider": "",
                    "authenticated": False,
                    "input_mode": "query",
                    "query_guidance": _query_guidance(contract),
                    "suggested_input": _suggested_query(
                        query_label=spec["query_label"],
                        focus=focus,
                        goal_context=goal_context,
                        time_window=time_window,
                    ),
                    "collection_arguments": {
                        "contract": contract,
                        "query": _suggested_query(
                            query_label=spec["query_label"],
                            focus=focus,
                            goal_context=goal_context,
                            time_window=time_window,
                        ),
                    },
                    "next_best_sources": [],
                    "warnings": [f"No typed adapter currently advertises '{contract}'."],
                }
            )
            continue

        operation = _operation_for_contract(selected_adapter, contract) or {}
        executable = bool(operation.get("executable"))
        adapter_state = str(selected_adapter.get("adapter_state") or "unavailable")
        if executable and adapter_state == "ready":
            status = "ready"
            ready_step_count += 1
        elif adapter_state == "degraded":
            status = "degraded"
            degraded_step_count += 1
        else:
            status = "unavailable"
            unavailable_step_count += 1

        suggested_input = _suggested_query(
            query_label=spec["query_label"],
            focus=focus,
            goal_context=goal_context,
            time_window=time_window,
        )
        step_payload = {
            "id": spec["id"],
            "contract": contract,
            "purpose": spec["purpose"],
            "status": status,
            "source": str(selected_adapter.get("name") or ""),
            "provider": str(selected_adapter.get("provider") or ""),
            "authenticated": bool(selected_adapter.get("authenticated")),
            "input_mode": str(operation.get("input_mode") or "query"),
            "query_guidance": _query_guidance(contract),
            "suggested_input": suggested_input,
            "collection_arguments": {
                "contract": contract,
                "source": str(selected_adapter.get("name") or ""),
                "query": suggested_input,
            },
            "next_best_sources": next_best,
            "warnings": [],
        }
        degraded_reason = str(operation.get("reason") or selected_adapter.get("degraded_reason") or "").strip()
        if degraded_reason:
            step_payload["degraded_reason"] = degraded_reason
            step_payload["warnings"].append(
                f"Adapter '{selected_adapter.get('name')}' cannot fully execute '{contract}' right now ({degraded_reason})."
            )
        steps.append(step_payload)

    if url.strip():
        steps.append(
            {
                "id": "explicit_page",
                "contract": "webpage.read",
                "purpose": "Inspect the explicit public URL before generalizing beyond it.",
                "status": "ready",
                "source": "browse_webpage",
                "provider": "seraph",
                "authenticated": False,
                "input_mode": "url",
                "query_guidance": "Use the explicit URL directly instead of search when the user already supplied one.",
                "suggested_input": url.strip(),
                "collection_arguments": {
                    "contract": "webpage.read",
                    "source": "browse_webpage",
                    "url": url.strip(),
                },
                "next_best_sources": [
                    {
                        "name": "browser_session",
                        "reason": "structured_fallback",
                        "description": "Reuse an existing structured browser snapshot if the page needs multi-step inspection.",
                    }
                ],
                "warnings": [],
            }
        )
        ready_step_count += 1

    if ready_step_count == len(steps):
        status = "ready"
    elif ready_step_count > 0:
        status = "partial"
    elif degraded_step_count > 0:
        status = "degraded"
    else:
        status = "unavailable"

    return {
        "status": status,
        "intent": normalized_intent,
        "title": template["title"],
        "description": template["description"],
        "focus": focus.strip(),
        "goal_context": goal_context.strip(),
        "time_window": time_window.strip(),
        "recommended_runbooks": template["recommended_runbooks"],
        "recommended_starter_packs": template["recommended_starter_packs"],
        "summary": {
            "step_count": len(steps),
            "ready_step_count": ready_step_count,
            "degraded_step_count": degraded_step_count,
            "unavailable_step_count": unavailable_step_count,
            "ready_adapter_count": int(adapter_inventory["summary"]["ready_adapter_count"]),
            "adapter_count": int(adapter_inventory["summary"]["adapter_count"]),
        },
        "steps": steps,
        "warnings": warnings,
    }


def _extract_records(raw_result: object) -> list[dict[str, Any]]:
    if isinstance(raw_result, list):
        return [item for item in raw_result if isinstance(item, dict)]
    if not isinstance(raw_result, dict):
        return []
    for key in ("items", "results", "nodes", "data"):
        value = raw_result.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [raw_result]


def _record_title(record: dict[str, Any]) -> str:
    for key in ("full_name", "title", "name", "path", "url"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    number = record.get("number")
    if isinstance(number, int):
        return f"#{number}"
    return "external record"


def _record_location(record: dict[str, Any]) -> str:
    for key in ("html_url", "url", "web_url"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""


def _record_summary(record: dict[str, Any]) -> str:
    for key in ("body", "description", "summary", "excerpt"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    state = str(record.get("state") or "").strip()
    if state:
        return f"state={state}"
    return ""


def _build_connector_item(
    record: dict[str, Any],
    *,
    contract: str,
    source_name: str,
    provider: str,
    result_kind: str,
    runtime_server: str,
) -> dict[str, Any]:
    location = _record_location(record)
    summary = _record_summary(record)
    title = _record_title(record)
    record_id = str(record.get("id") or location or title).strip()
    return {
        "id": record_id,
        "kind": result_kind or "external_record",
        "contract": contract,
        "source_name": source_name,
        "provider": provider,
        "source_kind": "managed_connector",
        "title": title,
        "location": location,
        "hostname": _parse_hostname(location),
        "summary": summary or title,
        "excerpt": _excerpt(summary or title),
        "content": summary or title,
        "observed_at": _utc_now(),
        "metadata": {
            "state": str(record.get("state") or ""),
            "number": record.get("number"),
            "runtime_server": runtime_server,
            "repository": record.get("repository"),
        },
    }


def _invoke_mcp_query(tool: object, *, query_param: str, per_page_param: str, query: str, max_results: int) -> object:
    arguments: dict[str, object] = {query_param: query}
    if per_page_param:
        arguments[per_page_param] = max_results
    try:
        return tool(**arguments)
    except TypeError as exc:
        if per_page_param and "unexpected keyword" in str(exc).lower():
            fallback_arguments = {query_param: query}
            return tool(**fallback_arguments)
        raise


def collect_source_evidence_bundle(
    *,
    contract: str,
    source: str = "",
    query: str = "",
    url: str = "",
    ref: str = "",
    session_id: str = "",
    owner_session_id: str = "",
    max_results: int = 5,
) -> dict[str, Any]:
    inventory = list_source_capability_inventory()
    adapter_inventory = list_source_adapter_inventory(inventory)
    adapters = adapter_inventory["adapters"]
    requested_source = source.strip()
    selected_adapter = _find_adapter(adapters, requested_source) if requested_source else None
    if selected_adapter is None:
        candidates = _candidate_adapters_for_contract(adapters, contract)
        selected_adapter = candidates[0] if candidates else None
    else:
        candidates = _candidate_adapters_for_contract(adapters, contract)

    response: dict[str, Any] = {
        "status": "unavailable",
        "request": {
            "contract": contract,
            "source": requested_source or (selected_adapter.get("name") if isinstance(selected_adapter, dict) else ""),
            "query": query,
            "url": url,
            "ref": ref,
            "session_id": session_id,
            "owner_session_id": owner_session_id,
            "max_results": max_results,
        },
        "adapter": None,
        "items": [],
        "warnings": [],
        "next_best_sources": [],
        "summary": {
            "item_count": 0,
            "contract": contract,
        },
    }

    if selected_adapter is None:
        response["warnings"].append(f"No typed source adapter currently advertises contract '{contract}'.")
        return response

    response["adapter"] = {
        "name": selected_adapter["name"],
        "provider": selected_adapter["provider"],
        "source_kind": selected_adapter["source_kind"],
        "authenticated": selected_adapter["authenticated"],
        "adapter_state": selected_adapter["adapter_state"],
        "degraded_reason": selected_adapter.get("degraded_reason"),
    }

    selected_operation = _operation_for_contract(selected_adapter, contract)
    if selected_operation is None:
        response["warnings"].append(
            f"Source '{selected_adapter['name']}' does not currently define an executable route for '{contract}'."
        )
        response["next_best_sources"] = list(selected_adapter.get("next_best_sources") or [])
        return response

    if not bool(selected_operation.get("executable")):
        reason = str(selected_operation.get("reason") or selected_adapter.get("degraded_reason") or "unavailable")
        response["warnings"].append(
            f"Source '{selected_adapter['name']}' cannot execute '{contract}' right now ({reason})."
        )
        response["next_best_sources"] = list(selected_adapter.get("next_best_sources") or [])
        return response

    source_name = str(selected_adapter["name"])
    if source_name == "web_search":
        if not query.strip():
            response["status"] = "failed"
            response["warnings"].append("web_search evidence collection requires a non-empty query.")
            return response
        records, blocked = search_web_records(query.strip(), max_results=max_results)
        response["items"] = [_build_search_item(record, source_name) for record in records]
        if blocked:
            response["warnings"].append(
                f"{len(blocked)} blocked search results were filtered by site policy."
            )
        response["status"] = "ok" if response["items"] else "empty"
    elif source_name == "browse_webpage":
        if not url.strip():
            response["status"] = "failed"
            response["warnings"].append("browse_webpage evidence collection requires an explicit URL.")
            return response
        content = browse_webpage(url.strip(), action="extract")
        if _is_error_result(content):
            response["status"] = "failed"
            response["warnings"].append(str(content))
            return response
        response["items"] = [_build_page_item(url.strip(), str(content), source_name)]
        response["status"] = "ok"
    elif source_name == "browser_session":
        if not owner_session_id.strip():
            response["status"] = "failed"
            response["warnings"].append("browser_session evidence collection requires owner_session_id.")
            return response
        payload = _browser_session_payload(
            owner_session_id=owner_session_id.strip(),
            ref=ref.strip(),
            session_id=session_id.strip(),
        )
        if payload is None:
            response["status"] = "failed"
            response["warnings"].append("The requested browser session ref or session_id was not found.")
            return response
        response["items"] = [_build_browser_item(payload, source_name)]
        response["status"] = "ok"
    elif selected_adapter["source_kind"] == "managed_connector":
        if not query.strip():
            response["status"] = "failed"
            response["warnings"].append(
                f"{source_name} evidence collection requires a non-empty query for '{contract}'."
            )
            return response
        runtime_server = str(selected_operation.get("runtime_server") or "")
        tool_name = str(selected_operation.get("tool_name") or "")
        tools_by_name = _server_tools_by_name(runtime_server)
        tool = tools_by_name.get(tool_name)
        if tool is None:
            response["warnings"].append(
                f"Source '{source_name}' is missing runtime tool '{tool_name}' on '{runtime_server}'."
            )
            response["next_best_sources"] = list(selected_adapter.get("next_best_sources") or [])
            return response
        try:
            raw_result = _invoke_mcp_query(
                tool,
                query_param=str(selected_operation.get("input_mode") or "query"),
                per_page_param=str(selected_operation.get("per_page_param") or "perPage"),
                query=query.strip(),
                max_results=max_results,
            )
        except Exception as exc:
            response["status"] = "failed"
            response["warnings"].append(str(exc))
            return response
        records = _extract_records(raw_result)
        response["items"] = [
            _build_connector_item(
                record,
                contract=contract,
                source_name=source_name,
                provider=str(selected_adapter.get("provider") or ""),
                result_kind=str(selected_operation.get("result_kind") or "external_record"),
                runtime_server=runtime_server,
            )
            for record in records
        ]
        response["status"] = "ok" if response["items"] else "empty"
    else:
        response["warnings"].append(
            f"Source '{source_name}' advertises '{contract}', but no executable runtime adapter is implemented yet."
        )
        response["next_best_sources"] = list(selected_adapter.get("next_best_sources") or [])
        return response

    response["summary"]["item_count"] = len(response["items"])
    if not response["next_best_sources"]:
        ready_fallbacks = [
            {
                "name": candidate["name"],
                "reason": "typed_fallback",
                "description": "Another ready typed adapter can satisfy the same contract.",
            }
            for candidate in candidates[1:]
            if candidate.get("adapter_state") == "ready"
        ]
        response["next_best_sources"] = ready_fallbacks[:3]
    return response
