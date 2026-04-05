"""Native tool for source-capability discovery."""

from __future__ import annotations

from smolagents import tool

from src.extensions.source_capabilities import list_source_capability_inventory

_VALID_FOCUS = {"all", "contracts", "typed", "untyped"}


def _render_contracts(inventory: dict[str, object]) -> list[str]:
    lines = ["Contracts:"]
    for item in inventory.get("contracts", []):
        if not isinstance(item, dict):
            continue
        lines.append(
            f"- {item.get('name')} · {item.get('preferred_access')} · available_from={item.get('available_from', 0)}"
        )
        description = str(item.get("description") or "").strip()
        if description:
            lines.append(f"  {description}")
    return lines


def _render_typed_sources(inventory: dict[str, object]) -> list[str]:
    lines = ["Typed sources:"]
    for item in inventory.get("typed_sources", []):
        if not isinstance(item, dict):
            continue
        contracts = ", ".join(str(contract) for contract in item.get("contracts", [])) or "none"
        auth_suffix = f" auth={item.get('auth_kind')}" if item.get("authenticated") else " auth=none"
        lines.append(
            f"- {item.get('name')} · {item.get('source_kind')} · {item.get('runtime_state')} · "
            f"{item.get('provider')}{auth_suffix} · contracts={contracts}"
        )
        for note in item.get("notes", []):
            lines.append(f"  {note}")
    return lines


def _render_untyped_sources(inventory: dict[str, object]) -> list[str]:
    lines = ["Untyped sources:"]
    for item in inventory.get("untyped_sources", []):
        if not isinstance(item, dict):
            continue
        lines.append(
            f"- {item.get('name')} · {item.get('status')} · tools={item.get('tool_count', 0)} · source={item.get('source')}"
        )
        for note in item.get("notes", []):
            lines.append(f"  {note}")
    return lines


@tool
def source_capabilities(focus: str = "all") -> str:
    """Inspect provider-neutral source access surfaces available to the current runtime.

    Args:
        focus: all, contracts, typed, or untyped.

    Returns:
        A human-readable summary of source contracts, typed surfaces, and raw untyped sources.
    """
    normalized_focus = focus.strip().lower() or "all"
    if normalized_focus not in _VALID_FOCUS:
        return "Error: focus must be one of all, contracts, typed, or untyped."

    inventory = list_source_capability_inventory()
    sections: list[str] = []
    if normalized_focus in {"all", "contracts"}:
        sections.extend(_render_contracts(inventory))
    if normalized_focus in {"all", "typed"}:
        if sections:
            sections.append("")
        sections.extend(_render_typed_sources(inventory))
    if normalized_focus in {"all", "untyped"}:
        if sections:
            sections.append("")
        sections.extend(_render_untyped_sources(inventory))
    if normalized_focus == "all":
        sections.append("")
        sections.append("Rules:")
        for rule in inventory.get("composition_rules", []):
            sections.append(f"- {rule}")
    return "\n".join(sections)
