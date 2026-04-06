"""Provider-neutral source adapter inventory and normalized evidence collection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from src.browser.sessions import browser_session_runtime
from src.extensions.source_capabilities import list_source_capability_inventory
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

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "contract": self.contract,
            "description": self.description,
            "input_mode": self.input_mode,
            "executable": self.executable,
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
        if route is None or not tool_names:
            operations.append(
                SourceOperation(
                    contract=contract,
                    description=description,
                    input_mode="query",
                    executable=False,
                    reason="route_not_defined",
                    result_kind=result_kind,
                    per_page_param=per_page_param,
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
                )
            )
            continue
        tools_by_name = _server_tools_by_name(bound_server)
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
