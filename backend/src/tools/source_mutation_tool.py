"""Native tools for provider-neutral source mutation planning and execution."""

from __future__ import annotations

import json
from typing import Any

from smolagents import tool

from src.extensions.source_operations import (
    build_source_mutation_plan,
    build_source_report_plan,
    execute_source_mutation_bundle,
)


def _parse_payload_json(payload_json: str) -> tuple[dict[str, Any], str | None]:
    raw = str(payload_json or "").strip()
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"Invalid payload_json: {exc.msg}"
    if not isinstance(parsed, dict):
        return {}, "Invalid payload_json: expected a JSON object."
    return parsed, None


@tool
def plan_source_mutation(
    contract: str,
    source: str = "",
    action_kind: str = "",
    action_summary: str = "",
    target_reference: str = "",
    fields: str = "",
) -> str:
    """Plan a typed authenticated source mutation without executing it.

    Args:
        contract: Mutation contract such as work_items.write.
        source: Optional typed source adapter name.
        action_kind: Optional bounded action kind such as create or comment.
        action_summary: Short plain-language summary of the intended change.
        target_reference: Exact issue, pull request, or repo reference when known.
        fields: Comma-separated field names that would change.

    Returns:
        A short structured mutation plan with approval and audit scope.
    """
    plan = build_source_mutation_plan(
        contract=contract,
        source=source,
        action_kind=action_kind,
        action_summary=action_summary,
        target_reference=target_reference,
        fields=[item.strip() for item in fields.split(",") if item.strip()],
    )
    adapter = plan.get("adapter") or {}
    operation = plan.get("operation") or {}
    action = plan.get("action") or {}
    lines = [
        f"status: {plan.get('status', 'unknown')}",
        f"contract: {contract}",
        f"source: {adapter.get('name') or source or 'unresolved'}",
    ]
    if adapter:
        lines.append(f"adapter_state: {adapter.get('adapter_state', 'unknown')}")
    if action:
        lines.append(
            "action: "
            f"{action.get('kind') or action_kind or 'unresolved'} "
            f"executable={bool(action.get('executable'))} "
            f"runtime_server={action.get('runtime_server') or 'unbound'} "
            f"tool={action.get('tool_name') or 'unbound'}"
        )
    elif operation:
        lines.append(
            "operation: "
            f"mutating={bool(operation.get('mutating'))} "
            f"approval_required={bool(operation.get('requires_approval'))} "
            f"runtime_server={operation.get('runtime_server') or 'unbound'} "
            f"tool={operation.get('tool_name') or 'unbound'}"
        )
    available_actions = plan.get("available_actions") or []
    if available_actions:
        lines.append("available_actions:")
        for candidate in available_actions:
            if not isinstance(candidate, dict):
                continue
            required = ", ".join(str(item) for item in candidate.get("required_payload_fields") or []) or "none"
            lines.append(
                f"- {candidate.get('kind')}: executable={bool(candidate.get('executable'))} "
                f"target={candidate.get('target_reference_mode') or 'none'} "
                f"required_fields={required}"
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


@tool
def execute_source_mutation(
    contract: str,
    action_kind: str,
    source: str = "",
    target_reference: str = "",
    payload_json: str = "",
) -> str:
    """Execute a bounded authenticated source mutation through a typed adapter.

    Args:
        contract: Mutation contract such as work_items.write.
        action_kind: Bounded action kind such as create or comment.
        source: Optional typed source adapter name.
        target_reference: Exact owner/repo or owner/repo#number target.
        payload_json: JSON object for the structured mutation payload.

    Returns:
        A short structured execution result.
    """
    payload, payload_error = _parse_payload_json(payload_json)
    if payload_error:
        return f"Error: {payload_error}"
    result = execute_source_mutation_bundle(
        contract=contract,
        source=source,
        action_kind=action_kind,
        target_reference=target_reference,
        payload=payload,
    )
    adapter = result.get("adapter") or {}
    action = result.get("action") or {}
    lines = [
        f"status: {result.get('status', 'unknown')}",
        f"contract: {contract}",
        f"action_kind: {action_kind}",
        f"source: {adapter.get('name') or source or 'unresolved'}",
    ]
    if adapter:
        lines.append(f"adapter_state: {adapter.get('adapter_state', 'unknown')}")
    if action:
        lines.append(
            "runtime: "
            f"{action.get('runtime_server') or 'unbound'}/"
            f"{action.get('tool_name') or 'unbound'}"
        )
    result_item = result.get("result") or {}
    if isinstance(result_item, dict) and result_item:
        lines.append(
            f"result: {result_item.get('title') or result_item.get('summary') or result_item.get('kind') or 'mutation'}"
        )
        if result_item.get("location"):
            lines.append(f"location: {result_item['location']}")
    warnings = result.get("warnings") or []
    if warnings:
        lines.append("warnings:")
        for warning in warnings:
            lines.append(f"- {warning}")
    return "\n".join(lines)


def _render_report_publish_plan(plan: dict[str, Any]) -> list[str]:
    publish_plan = plan.get("publish_plan") or {}
    if not isinstance(publish_plan, dict) or not publish_plan:
        return []
    target = ((publish_plan.get("approval_scope") or {}).get("target") or {})
    action = publish_plan.get("action") or {}
    return [
        "publish_plan:",
        f"- status: {publish_plan.get('status', 'unknown')}",
        f"- source: {(publish_plan.get('adapter') or {}).get('name') or 'unresolved'}",
        f"- action: {action.get('kind') or 'unresolved'}",
        f"- target: {target.get('reference') or '(reference unset)'}",
    ]


@tool
def plan_source_report(
    intent: str,
    focus: str = "",
    goal_context: str = "",
    time_window: str = "",
    source: str = "",
    target_reference: str = "",
    publish_action_kind: str = "",
) -> str:
    """Plan a source-backed report plus an optional authenticated publication step.

    Args:
        intent: Review intent such as daily_review, progress_review, or goal_alignment.
        focus: Optional project, repo, or topic to center the report around.
        goal_context: Optional goal text for goal-alignment reporting.
        time_window: Optional review window such as today or this week.
        source: Optional preferred typed source adapter.
        target_reference: Optional owner/repo or owner/repo#number publication target.
        publish_action_kind: Optional explicit publish action such as create or comment.

    Returns:
        A short structured report-and-publication plan.
    """
    plan = build_source_report_plan(
        intent=intent,
        focus=focus,
        goal_context=goal_context,
        time_window=time_window,
        source=source,
        target_reference=target_reference,
        publish_action_kind=publish_action_kind,
    )
    lines = [
        f"status: {plan.get('status', 'unknown')}",
        f"intent: {plan.get('intent', intent)}",
        f"title: {plan.get('title', intent)}",
    ]
    lines.append("report_outline:")
    for section in plan.get("report_outline") or []:
        lines.append(f"- {section}")
    review_plan = plan.get("review_plan") or {}
    summary = review_plan.get("summary") or {}
    if summary:
        lines.append(
            "review_summary: "
            f"steps={summary.get('step_count', 0)} "
            f"ready={summary.get('ready_step_count', 0)} "
            f"degraded={summary.get('degraded_step_count', 0)}"
        )
    lines.extend(_render_report_publish_plan(plan))
    warnings = plan.get("warnings") or []
    if warnings:
        lines.append("warnings:")
        for warning in warnings:
            lines.append(f"- {warning}")
    return "\n".join(lines)


def _mutation_approval_context(arguments: dict[str, Any]) -> dict[str, Any] | None:
    payload, payload_error = _parse_payload_json(str(arguments.get("payload_json") or ""))
    if payload_error:
        return None
    plan = build_source_mutation_plan(
        contract=str(arguments.get("contract") or ""),
        source=str(arguments.get("source") or ""),
        action_kind=str(arguments.get("action_kind") or ""),
        action_summary=str(payload.get("summary") or ""),
        target_reference=str(arguments.get("target_reference") or ""),
        fields=list(payload.keys()),
    )
    payload = plan.get("approval_context")
    return payload if isinstance(payload, dict) else None


def _mutation_audit_failure_payload(arguments: dict[str, Any], error: Exception) -> tuple[str, dict[str, Any]] | None:
    payload, payload_error = _parse_payload_json(str(arguments.get("payload_json") or ""))
    if payload_error:
        return None
    plan = build_source_mutation_plan(
        contract=str(arguments.get("contract") or ""),
        source=str(arguments.get("source") or ""),
        action_kind=str(arguments.get("action_kind") or ""),
        action_summary=str(payload.get("summary") or ""),
        target_reference=str(arguments.get("target_reference") or ""),
        fields=list(payload.keys()),
    )
    audit_payload = plan.get("audit_payload")
    if not isinstance(audit_payload, dict):
        return None
    return (
        f"source mutation failed for {arguments.get('contract')}",
        {
            "arguments": {
                "contract": str(arguments.get("contract") or ""),
                "action_kind": str(arguments.get("action_kind") or ""),
                "source": str(arguments.get("source") or ""),
                "target_reference": str(arguments.get("target_reference") or ""),
            },
            "approval_context": plan.get("approval_context"),
            "audit_payload": audit_payload,
            "error": str(error),
        },
    )


setattr(execute_source_mutation, "get_approval_context", _mutation_approval_context)
setattr(execute_source_mutation, "get_audit_failure_payload", _mutation_audit_failure_payload)
