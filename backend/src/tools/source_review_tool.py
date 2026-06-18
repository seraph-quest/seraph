"""Native tool for planning provider-neutral source review routines."""

from __future__ import annotations

from smolagents import tool

from src.extensions.source_operations import build_source_review_plan


def _render_review_step(step: dict[str, object]) -> list[str]:
    contract = str(step.get("contract") or "")
    source = str(step.get("source") or "unresolved")
    status = str(step.get("status") or "unknown")
    lines = [f"- {step.get('id')}: {contract} via {source} · {status}"]
    purpose = str(step.get("purpose") or "").strip()
    if purpose:
        lines.append(f"  purpose: {purpose}")
    suggested_input = str(step.get("suggested_input") or "").strip()
    if suggested_input:
        lines.append(f"  suggested_input: {suggested_input}")
    query_guidance = str(step.get("query_guidance") or "").strip()
    if query_guidance:
        lines.append(f"  query_guidance: {query_guidance}")
    degraded_reason = str(step.get("degraded_reason") or "").strip()
    if degraded_reason:
        lines.append(f"  degraded_reason: {degraded_reason}")
    next_best = step.get("next_best_sources") or []
    if isinstance(next_best, list) and next_best:
        lines.append("  next_best_sources:")
        for candidate in next_best:
            if not isinstance(candidate, dict):
                continue
            name = str(candidate.get("name") or "").strip()
            description = str(candidate.get("description") or candidate.get("reason") or "").strip()
            if name and description:
                lines.append(f"  - {name}: {description}")
            elif name:
                lines.append(f"  - {name}")
    return lines


@tool
def plan_source_review(
    intent: str,
    focus: str = "",
    goal_context: str = "",
    time_window: str = "",
    source: str = "",
    url: str = "",
) -> str:
    """Plan a provider-neutral source review routine from the currently available adapters.

    Args:
        intent: Review intent such as daily_review, progress_review, or goal_alignment.
        focus: Optional project, repo, or topic to center the review around.
        goal_context: Optional goal text to compare the gathered evidence against.
        time_window: Optional review window such as today, this week, or last 7 days.
        source: Optional preferred typed source adapter to use when it supports the contract.
        url: Optional explicit public URL to inspect as part of the review.

    Returns:
        A short structured plan showing which source contracts are ready, degraded, or unavailable.
    """
    plan = build_source_review_plan(
        intent=intent,
        focus=focus,
        goal_context=goal_context,
        time_window=time_window,
        source=source,
        url=url,
    )
    lines = [
        f"status: {plan.get('status', 'unknown')}",
        f"intent: {plan.get('intent', intent)}",
        f"title: {plan.get('title', intent)}",
    ]
    description = str(plan.get("description") or "").strip()
    if description:
        lines.append(f"description: {description}")
    summary = plan.get("summary") or {}
    if summary:
        lines.append(
            "summary: "
            f"steps={summary.get('step_count', 0)} "
            f"ready={summary.get('ready_step_count', 0)} "
            f"degraded={summary.get('degraded_step_count', 0)} "
            f"unavailable={summary.get('unavailable_step_count', 0)}"
        )
    recommended_runbooks = plan.get("recommended_runbooks") or []
    if recommended_runbooks:
        lines.append("recommended_runbooks:")
        for item in recommended_runbooks:
            lines.append(f"- {item}")
    recommended_starter_packs = plan.get("recommended_starter_packs") or []
    if recommended_starter_packs:
        lines.append("recommended_starter_packs:")
        for item in recommended_starter_packs:
            lines.append(f"- {item}")
    warnings = plan.get("warnings") or []
    if warnings:
        lines.append("warnings:")
        for warning in warnings:
            lines.append(f"- {warning}")
    steps = plan.get("steps") or []
    if steps:
        lines.append("steps:")
        for step in steps:
            if isinstance(step, dict):
                lines.extend(_render_review_step(step))
    return "\n".join(lines)
