"""Strategist agent — periodic strategic reasoning with restricted tool set."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from smolagents import ToolCallingAgent

from config.settings import settings
from src.guardian.state import GuardianState
from src.llm_runtime import (
    FallbackLiteLLMModel as LiteLLMModel,
    build_model_kwargs,
    completion_with_fallback,
)
from src.model_fabric.caller_context import build_canonical_inference_context
from src.tools.approval import wrap_tools_for_approval
from src.tools.audit import wrap_tools_for_audit
from src.tools.soul_tool import view_soul
from src.tools.goal_tools import get_goals, get_goal_progress
from src.goals.contracts import GoalCandidateDecision, GoalCandidateRequest
from src.guardian.goal_conditioned_loop import build_goal_candidate_decision
from src.db.models import Goal

logger = logging.getLogger(__name__)


def build_goal_conditioned_candidate(
    goal: Goal,
    request: GoalCandidateRequest,
) -> GoalCandidateDecision:
    """Expose the bounded goal candidate seam to strategist callers.

    Strategist reasoning remains proposal-only; admission and execution stay
    behind the goal-loop adapter and current goal revision checks.
    """

    return build_goal_candidate_decision(goal, request)

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
- Keep messages concise, calm, and guardian-like. Prefer operational clarity over theatrics.
"""


@dataclass
class StrategistDecision:
    should_intervene: bool
    content: str
    intervention_type: str  # nudge | advisory | alert
    urgency: int
    reasoning: str


def create_strategist_agent(
    context_block: str = "",
    *,
    guardian_state: GuardianState | None = None,
) -> ToolCallingAgent:
    """Create a restricted agent for strategic reasoning."""
    model = LiteLLMModel(**build_model_kwargs(
        temperature=0.4,
        max_tokens=settings.model_max_tokens,
        runtime_path="strategist_agent",
    ))

    if guardian_state is not None:
        context_block = guardian_state.to_prompt_block()

    instructions = STRATEGIST_INSTRUCTIONS.format(
        proactivity_level=settings.proactivity_level,
        context_block=context_block,
    )

    return ToolCallingAgent(
        # Strategist construction is also an executable agent boundary.  The
        # read-only-looking tools still expose governed Seraph state, so they
        # must carry the same authenticated capability decision as factory and
        # workflow tools before they can dispatch.
        tools=wrap_tools_for_approval(
            wrap_tools_for_audit([view_soul, get_goals, get_goal_progress])
        ),
        model=model,
        max_steps=5,
        instructions=instructions,
    )


async def run_strategist_decision_completion(
    context_block: str = "",
    *,
    guardian_state: GuardianState | None = None,
) -> str:
    """Run the strategist decision as a bounded JSON-only completion.

    The strategist prompt asks for one JSON decision and does not require tool
    calls. Keeping the scheduled path as a direct completion avoids local models
    spending multiple agent steps trying to parse the JSON answer as a tool call.
    """
    if guardian_state is not None:
        context_block = guardian_state.to_prompt_block()

    prompt = STRATEGIST_INSTRUCTIONS.format(
        proactivity_level=settings.proactivity_level,
        context_block=context_block,
    )
    transport_messages = [
            {
                "role": "system",
                "content": "You return only one valid JSON object. Do not call tools. Do not include markdown.",
            },
            {"role": "user", "content": prompt},
        ]
    response = await completion_with_fallback(
        messages=transport_messages,
        temperature=0.2,
        max_tokens=512,
        timeout=settings.agent_strategist_timeout,
        runtime_path="strategist_agent",
        request_context=build_canonical_inference_context(
            "strategist_agent",
            payload=transport_messages,
            output_tokens=512,
            timeout_seconds=settings.agent_strategist_timeout,
        ),
    )
    return str(response.choices[0].message.content or "").strip()


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
        logger.warning("Failed to parse strategist response: %s\nRaw: %.500s", e, raw)
        return StrategistDecision(
            should_intervene=False,
            content="",
            intervention_type="nudge",
            urgency=0,
            reasoning=f"Parse failure: {e}",
        )
