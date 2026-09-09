"""Native tool for governed self-evolution proposals."""

from __future__ import annotations

from smolagents import tool

from src.auth.cancellation import assert_runtime_not_revoked
from src.evolution.engine import (
    create_evolution_proposal,
    require_evolution_operator_authority,
)


@tool
def propose_capability_evolution(
    target_type: str,
    source_path: str,
    objective: str = "",
    observations: str = "",
) -> str:
    """Generate a governed review candidate for a declarative capability asset.

    This direct native-tool entry point has the same human-operator policy as
    the REST route. Service and scheduled principals are denied because an
    evolution proposal changes the future capability surface and must remain
    review-gated.

    Args:
        target_type: One of skill, runbook, starter_pack, or prompt_pack.
        source_path: Existing asset path inside the repo or workspace.
        objective: What the variant should improve.
        observations: Newline-separated friction points or trace observations.

    Returns:
        A short metadata-only proposal receipt with constraint status.
    """
    require_evolution_operator_authority()
    assert_runtime_not_revoked()
    proposal = create_evolution_proposal(
        target_type,  # type: ignore[arg-type]
        source_path=source_path,
        objective=objective,
        observations=[line.strip() for line in observations.splitlines() if line.strip()],
        authority_check=assert_runtime_not_revoked,
    )
    receipt = proposal["receipt"]
    lines = [
        f"status: {proposal['status']}",
        f"target_type: {target_type}",
        f"score: {receipt['score']}",
        f"quality_state: {receipt['quality_state']}",
    ]
    lines.append("constraints:")
    for item in receipt.get("constraints", []):
        lines.append(f"- {item['name']}: {item['status']} ({bool(item.get('blocked'))})")
    lines.append("candidate_content: withheld")
    lines.append("operator_input: withheld")
    return "\n".join(lines)
