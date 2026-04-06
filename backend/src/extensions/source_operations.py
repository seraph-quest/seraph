"""Provider-neutral source adapter inventory and normalized evidence collection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from src.browser.sessions import browser_session_runtime
from src.extensions.source_capabilities import list_source_capability_inventory
from src.tools.browser_tool import browse_webpage
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

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "contract": self.contract,
            "description": self.description,
            "input_mode": self.input_mode,
            "executable": self.executable,
        }
        if self.reason:
            payload["reason"] = self.reason
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


def _adapter_state_for_source(source: dict[str, Any], inventory: dict[str, Any]) -> tuple[str, str, list[dict[str, str]], tuple[SourceOperation, ...]]:
    name = str(source.get("name") or "")
    source_kind = str(source.get("source_kind") or "")
    runtime_state = str(source.get("runtime_state") or "unknown")
    contracts = tuple(
        str(item) for item in source.get("contracts", [])
        if isinstance(item, str) and item.strip()
    )

    if source_kind == "native_tool" and name in _NATIVE_OPERATION_DEFINITIONS:
        return "ready", "", [], _NATIVE_OPERATION_DEFINITIONS[name]

    if source_kind == "managed_connector":
        if runtime_state == "requires_config":
            reason = "requires_config"
        elif runtime_state == "disabled":
            reason = "disabled"
        else:
            reason = "no_runtime_adapter"
        operations = tuple(
            SourceOperation(
                contract=contract,
                description=f"Typed {contract} contract advertised by {name}.",
                input_mode="adapter_defined",
                executable=False,
                reason=reason,
            )
            for contract in contracts
        )
        next_best = _matching_untyped_sources(inventory, provider=str(source.get("provider") or ""))
        state = "degraded" if contracts else "unavailable"
        return state, reason, next_best, operations

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

    if selected_adapter["adapter_state"] != "ready":
        reason = str(selected_adapter.get("degraded_reason") or "unavailable")
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
