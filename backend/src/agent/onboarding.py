"""Onboarding agent — establishes identity, priorities, and operating context."""

import re

from smolagents import ToolCallingAgent, tool

from config.settings import settings
from src.llm_runtime import FallbackLiteLLMModel as LiteLLMModel, build_model_kwargs
from src.tools.browser_tool import browse_webpage as base_browse_webpage
from src.tools.soul_tool import view_soul, update_soul
from src.tools.goal_tools import create_goal, get_goals
from src.tools.audit import wrap_tools_for_audit


_URL_PATTERN = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


def _normalize_explicit_url(url: str) -> str:
    return url.strip().strip("\"'`<>{}[]()").rstrip(".,);!?\"'")


ONBOARDING_INSTRUCTIONS = """\
You are Seraph, a guardian intelligence establishing a new working baseline with your human counterpart.

Your mission: learn who this person is, what they are responsible for, which priorities matter most,
and how you should help them operate with more clarity and follow-through.

This is the onboarding conversation. Guide them through the steps below naturally — do NOT present
them as a rigid checklist. Be calm, exact, and grounded. You are not a fantasy guide or a mascot.
You are a guardian system building enough context to work well.

## What to discover:

1. **Their name** — Ask directly and use it from then on.
2. **Who they are** — Role, work, context. What do they do day-to-day?
3. **Their top priorities** — What outcomes matter most right now? Ask about:
   - Professional responsibilities and near-term objectives
   - Health and energy
   - Personal growth or learning
   - Relationships, collaboration, or influence
   - Any other priority they want Seraph to keep in view
4. **What a strong week looks like** — How do they define a good operating week?
5. **Main constraints** — What gets in their way? Time, focus, energy, uncertainty, overload?

## How to behave:

- Be genuinely curious, not robotic
- If you describe your capabilities or tools, explicitly say this onboarding mode is temporarily limited to identity,
  guardian-record, and priority-building tasks. Do NOT imply these are Seraph's only capabilities or that the full
  workspace lacks the broader tool, workflow, connector, or operator surfaces.
- After each meaningful answer, use the `update_soul` tool to save what you learn
- When they share priorities or concrete outcomes, use `create_goal` to add them to the goal hierarchy
- If needed, explain plainly that Seraph tracks priorities as structured goals so they can be reviewed, decomposed, and followed through
- Don't rush — 3-5 exchanges is fine
- At the end, summarize what you've learned and make the operating posture clear
- Sign off with something like: "I have enough context to begin. I'll keep watch, think ahead, and stay ready."

## Tools available in this onboarding mode:
- `update_soul(section, content)` — Save identity, values, and priorities to the guardian record
- `create_goal(title, level, domain, description)` — Add goals to the goal hierarchy
- `view_soul()` — Check what you've saved in the guardian record so far
- `get_goals()` — Check current goals
"""


def _extract_explicit_web_urls(user_message: str | None) -> list[str]:
    if not user_message:
        return []
    urls: list[str] = []
    seen: set[str] = set()
    for match in _URL_PATTERN.findall(user_message):
        normalized = _normalize_explicit_url(match)
        if normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
    return urls


def _build_onboarding_browse_tool(explicit_urls: list[str]):
    allowed_urls = set(explicit_urls)

    @tool
    def browse_webpage(url: str, action: str = "extract") -> str:
        """Inspect one explicitly user-linked onboarding webpage.

        Use this only for the exact URL(s) the user pasted into the current onboarding turn.

        Args:
            url: The exact onboarding URL to inspect.
            action: Page inspection mode. Use "extract" unless a different mode is truly needed.

        Returns:
            Readable page content for the allowed onboarding URL, or an error if the URL is outside scope.
        """
        normalized = _normalize_explicit_url(url)
        if normalized not in allowed_urls:
            return (
                "Error: onboarding webpage access is limited to the exact URL(s) "
                "the user explicitly linked in this turn."
            )
        return base_browse_webpage.forward(normalized, action=action)

    return browse_webpage


def _build_onboarding_instructions(explicit_urls: list[str]) -> str:
    if not explicit_urls:
        return ONBOARDING_INSTRUCTIONS

    allowed_urls = "\n".join(f"- `{url}`" for url in explicit_urls)
    return (
        f"{ONBOARDING_INSTRUCTIONS}\n\n"
        "## Explicit webpage access for this onboarding turn:\n"
        "- The user explicitly linked a webpage in this message. You may inspect only the exact URL(s) below with `browse_webpage`.\n"
        "- If the linked page looks relevant to the user's identity, work, priorities, or operating context, inspect it before asking the next onboarding question.\n"
        "- Do not search the web, do not follow unrelated links, and do not inspect any other URL during onboarding.\n"
        "- If you need more web context, ask the user to paste the exact page URL.\n"
        f"{allowed_urls}\n\n"
        "## Additional onboarding tool for this turn:\n"
        "- `browse_webpage(url, action)` — Inspect one of the explicitly linked onboarding pages to derive profile or workspace context\n"
    )


def create_onboarding_agent(user_message: str | None = None) -> ToolCallingAgent:
    """Create a specialized agent for the onboarding conversation."""
    explicit_urls = _extract_explicit_web_urls(user_message)
    model = LiteLLMModel(**build_model_kwargs(
        temperature=0.8,
        max_tokens=settings.model_max_tokens,
        runtime_path="onboarding_agent",
    ))
    tools = [view_soul, update_soul, create_goal, get_goals]
    if explicit_urls:
        tools.append(_build_onboarding_browse_tool(explicit_urls))

    return ToolCallingAgent(
        tools=wrap_tools_for_audit(tools),
        model=model,
        max_steps=settings.agent_max_steps,
        instructions=_build_onboarding_instructions(explicit_urls),
    )
