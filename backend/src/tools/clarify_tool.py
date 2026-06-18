"""Tool for asking a bounded clarification question before continuing."""

from src.agent.exceptions import ClarificationRequired
from smolagents import tool


def _parse_options(options: str) -> list[str]:
    raw = options.replace("\n", ",").split(",")
    return [item.strip() for item in raw if item.strip()]


@tool
def clarify(question: str, reason: str = "", options: str = "") -> str:
    """Request a missing input from the user before continuing.

    Use this when a required parameter or decision is missing and guessing would
    be risky or would degrade the output. Ask one concise blocking question.

    Args:
        question: The follow-up question the user should answer next.
        reason: Optional short explanation of why the missing input matters.
        options: Optional comma-separated or newline-separated suggested answers.

    Returns:
        This tool never returns normally; it raises a clarification interrupt.
    """
    raise ClarificationRequired(
        question=question,
        reason=reason,
        options=_parse_options(options),
    )
