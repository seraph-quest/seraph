"""Native tool for governed self-evolution proposals."""

from __future__ import annotations

from smolagents import tool

from src.evolution.engine import create_evolution_proposal


@tool
def propose_capability_evolution(
    target_type: str,
    source_path: str,
    objective: str = "",
    observations: str = "",
) -> str:
    """Generate a governed review candidate for a declarative capability asset.

    Args:
        target_type: One of skill, runbook, starter_pack, or prompt_pack.
        source_path: Existing asset path inside the repo or workspace.
        objective: What the variant should improve.
        observations: Newline-separated friction points or trace observations.

    Returns:
        A short proposal receipt with constraint and PR-draft status.
    """
    proposal = create_evolution_proposal(
        target_type,  # type: ignore[arg-type]
        source_path=source_path,
        objective=objective,
        observations=[line.strip() for line in observations.splitlines() if line.strip()],
    )
    receipt = proposal["receipt"]
    lines = [
        f"status: {proposal['status']}",
        f"target_type: {target_type}",
        f"candidate_name: {proposal['candidate_name']}",
        f"score: {receipt['score']}",
        f"quality_state: {receipt['quality_state']}",
    ]
    if receipt.get("saved_path"):
        lines.append(f"saved_path: {receipt['saved_path']}")
    if receipt.get("receipt_path"):
        lines.append(f"receipt_path: {receipt['receipt_path']}")
    lines.append("constraints:")
    for item in receipt.get("constraints", []):
        lines.append(f"- {item['name']}: {item['status']} ({item['summary']})")
    lines.append("pr_draft:")
    lines.append(f"- title: {receipt['pr_draft']['title']}")
    return "\n".join(lines)
