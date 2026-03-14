"""Deterministic runtime evaluation harness for core guardian flows."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from config.settings import settings
from src.agent.factory import get_model
from src.agent.onboarding import create_onboarding_agent
from src.agent.strategist import create_strategist_agent
from src.llm_runtime import FallbackLiteLLMModel
from src.observer.context import CurrentContext
from src.scheduler.jobs.daily_briefing import run_daily_briefing
from src.tools.shell_tool import shell_execute


Runner = Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class EvalScenario:
    name: str
    category: str
    description: str
    runner: Runner


@dataclass
class EvalResult:
    name: str
    category: str
    description: str
    passed: bool
    duration_ms: int
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EvalSummary:
    results: list[EvalResult]
    duration_ms: int

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for result in self.results if result.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "duration_ms": self.duration_ms,
            "results": [result.to_dict() for result in self.results],
        }


def _make_litellm_response(text: str) -> MagicMock:
    choice = MagicMock()
    choice.message.content = text
    response = MagicMock()
    response.choices = [choice]
    return response


def _make_context(**overrides: Any) -> CurrentContext:
    defaults = dict(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Ship the next reliability batch",
    )
    defaults.update(overrides)
    return CurrentContext(**defaults)


def _tool_names(agent: Any) -> list[str]:
    tools = agent.tools
    if isinstance(tools, dict):
        return sorted(str(name) for name in tools)
    names = []
    for tool in tools:
        names.append(tool if isinstance(tool, str) else tool.name)
    return sorted(names)


def _eval_chat_model_wrapper() -> dict[str, Any]:
    with (
        patch.object(settings, "default_model", "openai/gpt-4o-mini"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", "ollama/llama3.2"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
    ):
        model = get_model()

    assert isinstance(model, FallbackLiteLLMModel)
    assert model.model_id == "openai/gpt-4o-mini"
    assert model._fallback_model is not None
    assert model._fallback_model.model_id == "ollama/llama3.2"
    return {
        "primary_model": model.model_id,
        "fallback_model": model._fallback_model.model_id,
    }


def _eval_onboarding_model_wrapper() -> dict[str, Any]:
    with (
        patch.object(settings, "fallback_model", "ollama/llama3.2"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
    ):
        agent = create_onboarding_agent()

    tool_names = _tool_names(agent)
    assert isinstance(agent.model, FallbackLiteLLMModel)
    assert agent.model._fallback_model is not None
    assert {"create_goal", "get_goals", "update_soul", "view_soul"} <= set(tool_names)
    return {
        "tool_names": tool_names,
        "fallback_model": agent.model._fallback_model.model_id,
    }


def _eval_strategist_model_wrapper() -> dict[str, Any]:
    with (
        patch.object(settings, "fallback_model", "ollama/llama3.2"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
    ):
        agent = create_strategist_agent("Time: morning\nGoals: 2 active")

    tool_names = _tool_names(agent)
    assert isinstance(agent.model, FallbackLiteLLMModel)
    assert agent.model._fallback_model is not None
    assert {"get_goal_progress", "get_goals", "view_soul"} <= set(tool_names)
    return {
        "tool_names": tool_names,
        "fallback_model": agent.model._fallback_model.model_id,
        "max_steps": agent.max_steps,
    }


async def _eval_daily_briefing_fallback() -> dict[str, Any]:
    ctx = _make_context(upcoming_events=[{"summary": "Ship eval harness", "start": "09:30"}])
    mock_context_manager = MagicMock()
    mock_context_manager.refresh = AsyncMock(return_value=ctx)
    mock_deliver = AsyncMock()
    fallback_response = _make_litellm_response("Morning briefing via fallback.")
    primary_error = RuntimeError("primary down")

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "fallback_model", "ollama/llama3.2"),
        patch.object(settings, "fallback_llm_api_key", ""),
        patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
        patch("src.observer.manager.context_manager", mock_context_manager),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch("src.memory.vector_store.search_formatted", return_value="- [memory] Prioritize reliability"),
        patch("src.llm_runtime.logger.warning"),
        patch("litellm.completion", side_effect=[primary_error, fallback_response]) as mock_completion,
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_daily_briefing()

    assert mock_completion.call_count == 2
    assert mock_completion.call_args_list[0].kwargs["model"] == "openrouter/anthropic/claude-sonnet-4"
    assert mock_completion.call_args_list[1].kwargs["model"] == "ollama/llama3.2"
    mock_deliver.assert_called_once()
    delivered_message = mock_deliver.call_args.args[0]
    return {
        "primary_model": mock_completion.call_args_list[0].kwargs["model"],
        "fallback_model": mock_completion.call_args_list[1].kwargs["model"],
        "delivered_excerpt": delivered_message.content,
    }


def _eval_shell_tool_timeout_contract() -> dict[str, Any]:
    mock_client = MagicMock()
    mock_client.post.side_effect = httpx.TimeoutException("timeout")

    with patch("src.tools.shell_tool.httpx.Client") as client_cls:
        client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        client_cls.return_value.__exit__ = MagicMock(return_value=False)
        result = shell_execute("import time; time.sleep(999)")

    assert "timed out" in result.lower()
    return {"result": result}


_SCENARIOS: tuple[EvalScenario, ...] = (
    EvalScenario(
        name="chat_model_wrapper",
        category="runtime",
        description="Main agent model wiring exposes the shared fallback wrapper.",
        runner=_eval_chat_model_wrapper,
    ),
    EvalScenario(
        name="onboarding_model_wrapper",
        category="guardian",
        description="Onboarding agent keeps its core tools while using fallback-capable model wiring.",
        runner=_eval_onboarding_model_wrapper,
    ),
    EvalScenario(
        name="strategist_model_wrapper",
        category="guardian",
        description="Strategist agent uses fallback-capable model wiring with its restricted tool set.",
        runner=_eval_strategist_model_wrapper,
    ),
    EvalScenario(
        name="daily_briefing_fallback",
        category="proactive",
        description="Daily briefing survives a primary provider failure and still delivers via fallback.",
        runner=_eval_daily_briefing_fallback,
    ),
    EvalScenario(
        name="shell_tool_timeout_contract",
        category="tool",
        description="Shell tool returns a clear timeout contract when sandbox execution stalls.",
        runner=_eval_shell_tool_timeout_contract,
    ),
)


def available_scenarios() -> tuple[EvalScenario, ...]:
    return _SCENARIOS


def _select_scenarios(selected_names: Sequence[str] | None) -> list[EvalScenario]:
    scenarios = list(_SCENARIOS)
    if not selected_names:
        return scenarios

    scenario_map = {scenario.name: scenario for scenario in scenarios}
    missing = sorted({name for name in selected_names if name not in scenario_map})
    if missing:
        available = ", ".join(sorted(scenario_map))
        missing_str = ", ".join(missing)
        raise ValueError(f"Unknown eval scenario(s): {missing_str}. Available: {available}")

    return [scenario_map[name] for name in selected_names]


async def _run_scenario(scenario: EvalScenario) -> EvalResult:
    started = time.perf_counter()
    try:
        output = scenario.runner()
        if asyncio.iscoroutine(output):
            details = await output
        else:
            details = output
        return EvalResult(
            name=scenario.name,
            category=scenario.category,
            description=scenario.description,
            passed=True,
            duration_ms=int((time.perf_counter() - started) * 1000),
            details=details,
        )
    except Exception as exc:
        return EvalResult(
            name=scenario.name,
            category=scenario.category,
            description=scenario.description,
            passed=False,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error=str(exc),
        )


async def run_runtime_evals(selected_names: Sequence[str] | None = None) -> EvalSummary:
    scenarios = _select_scenarios(selected_names)
    started = time.perf_counter()
    results = []
    for scenario in scenarios:
        results.append(await _run_scenario(scenario))
    return EvalSummary(results=results, duration_ms=int((time.perf_counter() - started) * 1000))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        help="Specific scenario to run. Repeat to run more than one.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available scenarios and exit.",
    )
    parser.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indentation for output.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.list:
        for scenario in available_scenarios():
            print(f"{scenario.name}: [{scenario.category}] {scenario.description}")
        return 0

    try:
        summary = asyncio.run(run_runtime_evals(args.scenarios))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(json.dumps(summary.to_dict(), indent=args.indent))
    return 0 if summary.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
