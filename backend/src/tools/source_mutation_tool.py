"""Native tool for planning connector-backed typed source mutations."""

from __future__ import annotations

from smolagents import tool

from src.extensions.source_operations import build_source_mutation_plan


@tool
def plan_source_mutation(
    contract: str,
    source: str = "",
    action_summary: str = "",
    target_reference: str = "",
    fields: str = "",
) -> str:
    """Plan a typed authenticated source mutation without executing it.

    Args:
        contract: Mutation contract such as work_items.write.
        source: Optional typed source adapter name.
        action_summary: Short plain-language summary of the intended change.
        target_reference: Exact issue, pull request, or repo reference when known.
        fields: Comma-separated field names that would change.

    Returns:
        A short structured mutation plan with approval and audit scope.
    """
    plan = build_source_mutation_plan(
        contract=contract,
        source=source,
        action_summary=action_summary,
        target_reference=target_reference,
        fields=[item.strip() for item in fields.split(",") if item.strip()],
    )
    adapter = plan.get("adapter") or {}
    operation = plan.get("operation") or {}
    lines = [
        f"status: {plan.get('status', 'unknown')}",
        f"contract: {contract}",
        f"source: {adapter.get('name') or source or 'unresolved'}",
    ]
    if adapter:
        lines.append(f"adapter_state: {adapter.get('adapter_state', 'unknown')}")
    if operation:
        lines.append(
            "operation: "
            f"mutating={bool(operation.get('mutating'))} "
            f"approval_required={bool(operation.get('requires_approval'))} "
            f"runtime_server={operation.get('runtime_server') or 'unbound'} "
            f"tool={operation.get('tool_name') or 'unbound'}"
        )
    approval_scope = plan.get("approval_scope") or {}
    target = approval_scope.get("target") or {}
    change_scope = approval_scope.get("change_scope") or {}
    if target:
        lines.append(
            "target: "
            f"{target.get('provider') or 'unknown'} "
            f"{target.get('target_kind') or 'target'} "
            f"{target.get('reference') or '(reference unset)'}"
        )
    if change_scope:
        field_names = ", ".join(str(item) for item in change_scope.get("field_names", [])) or "none"
        lines.append(f"fields: {field_names}")
        if change_scope.get("action_summary"):
            lines.append(f"action_summary: {change_scope['action_summary']}")
    warnings = plan.get("warnings") or []
    if warnings:
        lines.append("warnings:")
        for warning in warnings:
            lines.append(f"- {warning}")
    return "\n".join(lines)
