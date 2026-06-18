"""Native tool for normalized source evidence collection."""

from __future__ import annotations

from smolagents import tool

from src.extensions.source_operations import collect_source_evidence_bundle


def _render_item(item: dict[str, object]) -> list[str]:
    title = str(item.get("title") or item.get("location") or item.get("id") or "evidence")
    location = str(item.get("location") or "")
    summary = str(item.get("summary") or item.get("excerpt") or "").strip()
    lines = [f"- {title}"]
    if location:
        lines.append(f"  location: {location}")
    if summary:
        lines.append(f"  summary: {summary}")
    return lines


@tool
def collect_source_evidence(
    contract: str,
    source: str = "",
    query: str = "",
    url: str = "",
    ref: str = "",
    session_id: str = "",
    owner_session_id: str = "",
    max_results: int = 5,
) -> str:
    """Collect normalized evidence through a provider-neutral source contract.

    Args:
        contract: Source contract such as source_discovery.read or webpage.read.
        source: Optional typed source or adapter name.
        query: Search query for source_discovery.read.
        url: Explicit URL for webpage.read.
        ref: Browser-session ref for browser_session evidence.
        session_id: Browser-session id for browser_session evidence.
        owner_session_id: Session owner id for browser_session evidence.
        max_results: Maximum structured result count for discovery queries.

    Returns:
        A short structured evidence summary.
    """
    bundle = collect_source_evidence_bundle(
        contract=contract,
        source=source,
        query=query,
        url=url,
        ref=ref,
        session_id=session_id,
        owner_session_id=owner_session_id,
        max_results=max_results,
    )
    adapter = bundle.get("adapter") or {}
    lines = [
        f"status: {bundle.get('status', 'unknown')}",
        f"contract: {contract}",
        f"source: {adapter.get('name') or source or 'unresolved'}",
    ]
    if adapter:
        lines.append(
            f"adapter_state: {adapter.get('adapter_state', 'unknown')}"
        )
    warnings = bundle.get("warnings") or []
    if warnings:
        lines.append("warnings:")
        for warning in warnings:
            lines.append(f"- {warning}")
    items = bundle.get("items") or []
    if items:
        lines.append("evidence:")
        for item in items:
            if isinstance(item, dict):
                lines.extend(_render_item(item))
    next_best = bundle.get("next_best_sources") or []
    if next_best:
        lines.append("next_best_sources:")
        for candidate in next_best:
            if not isinstance(candidate, dict):
                continue
            name = str(candidate.get("name") or "")
            description = str(candidate.get("description") or candidate.get("reason") or "").strip()
            if description:
                lines.append(f"- {name}: {description}")
            else:
                lines.append(f"- {name}")
    return "\n".join(lines)
