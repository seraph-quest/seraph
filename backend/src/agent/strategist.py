"""Strategist agent — periodic strategic reasoning with restricted tool set."""

import json
import logging
from dataclasses import dataclass

from smolagents import LiteLLMModel, ToolCallingAgent

from config.settings import settings
from src.tools.soul_tool import view_soul
from src.tools.goal_tools import get_goals, get_goal_progress

logger = logging.getLogger(__name__)

STRATEGIST_INSTRUCTIONS = """\
You are Seraph's strategic reasoning module. You periodically review the user's context \
and decide whether a proactive intervention is warranted.

Proactivity level: {proactivity_level}/5 (1=minimal, 5=very proactive).

## Current Context
{context_block}

## Your Task
Analyze the context and decide:
1. Is there something the user should know right now?
2. Would a nudge, advisory, or alert help them?
3. Or is everything fine and no intervention is needed?

Use the available tools to check the soul file and goals if you need more context.

## Response Format
Return ONLY a JSON object (no markdown fences):
{{
  "should_intervene": true/false,
  "content": "The message to send to the user (if intervening)",
  "intervention_type": "nudge" | "advisory" | "alert",
  "urgency": 1-5,
  "reasoning": "Why you made this decision"
}}

Guidelines:
- "nudge" = subtle reminder shown as speech bubble (5s). Use for gentle prods.
- "advisory" = opens chat panel. Use for useful information or suggestions.
- "alert" = opens chat panel + high urgency. Use only for time-sensitive items.
- At proactivity_level 1-2, only intervene for urgent/time-sensitive items.
- At proactivity_level 3, intervene for helpful suggestions too.
- At proactivity_level 4-5, be more liberal with nudges and check-ins.
- If the user is in deep_work or a meeting, prefer NOT intervening unless urgent.
- Keep messages concise and RPG-themed (you are a guardian Seraph).
"""


@dataclass
class StrategistDecision:
    should_intervene: bool
    content: str
    intervention_type: str  # nudge | advisory | alert
    urgency: int
    reasoning: str


def create_strategist_agent(context_block: str) -> ToolCallingAgent:
    """Create a restricted agent for strategic reasoning."""
    model = LiteLLMModel(
        model_id=settings.default_model,
        api_key=settings.openrouter_api_key,
        api_base="https://openrouter.ai/api/v1",
        temperature=0.4,
        max_tokens=settings.model_max_tokens,
    )

    instructions = STRATEGIST_INSTRUCTIONS.format(
        proactivity_level=settings.proactivity_level,
        context_block=context_block,
    )

    return ToolCallingAgent(
        tools=[view_soul, get_goals, get_goal_progress],
        model=model,
        max_steps=5,
        instructions=instructions,
    )


def parse_strategist_response(raw: str) -> StrategistDecision:
    """Parse the strategist agent's JSON response into a decision.

    Falls back to should_intervene=False on any parse failure.
    """
    if not raw or not raw.strip():
        return StrategistDecision(
            should_intervene=False,
            content="",
            intervention_type="nudge",
            urgency=0,
            reasoning="Empty response from strategist",
        )

    text = raw.strip()

    # Strip markdown fences if present
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
        return StrategistDecision(
            should_intervene=bool(data.get("should_intervene", False)),
            content=str(data.get("content", "")),
            intervention_type=str(data.get("intervention_type", "nudge")),
            urgency=int(data.get("urgency", 3)),
            reasoning=str(data.get("reasoning", "")),
        )
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Failed to parse strategist response: %s", e)
        return StrategistDecision(
            should_intervene=False,
            content="",
            intervention_type="nudge",
            urgency=0,
            reasoning=f"Parse failure: {e}",
        )
