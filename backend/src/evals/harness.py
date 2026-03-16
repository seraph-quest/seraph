"""Deterministic runtime evaluation harness for core guardian flows."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import types
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from smolagents import Tool

from config.settings import settings
from src.agent.session import SessionManager, session_manager
from src.agent.context_window import _summarize_middle, _summary_cache
from src.agent.factory import create_orchestrator, get_model
from src.agent.onboarding import create_onboarding_agent
from src.agent.specialists import create_mcp_specialist, create_specialist, mcp_specialist_runtime_path
from src.agent.strategist import create_strategist_agent
from src.api.observer import ScreenContextRequest, ScreenObservationData, post_screen_context
from src.audit.repository import audit_repository
from src.llm_runtime import FallbackLiteLLMModel, _reset_target_health, completion_with_fallback_sync
from src.memory.embedder import _reset_embedder_state, embed
from src.observer.sources.calendar_source import gather_calendar
from src.observer.sources.goal_source import gather_goals
from src.observer.sources.git_source import gather_git
from src.observer.sources.time_source import gather_time
from src.memory.consolidator import consolidate_session
from src.observer.context import CurrentContext
from src.observer.delivery import deliver_or_queue
from src.scheduler.jobs.daily_briefing import run_daily_briefing
from src.scheduler.jobs.strategist_tick import run_strategist_tick
from src.tools.audit import wrap_tools_for_audit
from src.tools.browser_tool import browse_webpage
from src.tools.shell_tool import shell_execute
from src.tools.web_search_tool import web_search
from src.models.schemas import WSResponse


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


def _find_audit_call(
    mock_log_event: AsyncMock,
    *,
    event_type: str,
    tool_name: str | None = None,
) -> dict[str, Any]:
    for call in mock_log_event.call_args_list:
        kwargs = call.kwargs
        if kwargs.get("event_type") != event_type:
            continue
        if tool_name is not None and kwargs.get("tool_name") != tool_name:
            continue
        return kwargs
    raise AssertionError(f"Missing audit event {event_type} for {tool_name or 'any tool'}")


class _DummyStrategistTool(Tool):
    name = "get_goals"
    description = "Dummy strategist tool"
    inputs = {}
    output_type = "string"

    def forward(self) -> str:
        return "2 active goals"


class _FakeScalarResult:
    def __init__(self, messages: list[Any]):
        self._messages = messages

    def scalars(self) -> "_FakeScalarResult":
        return self

    def all(self) -> list[Any]:
        return self._messages


class _FakeDbSession:
    def __init__(self, messages: list[Any]):
        self._messages = messages

    async def execute(self, _query: Any) -> _FakeScalarResult:
        return _FakeScalarResult(self._messages)


class _FakeDbSessionContext:
    def __init__(self, messages: list[Any]):
        self._messages = messages

    async def __aenter__(self) -> _FakeDbSession:
        return _FakeDbSession(self._messages)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


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


def _eval_provider_fallback_chain() -> dict[str, Any]:
    completion_response = _make_litellm_response("Resolved after ordered fallback chain.")

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "fallback_model", ""),
        patch.object(
            settings,
            "fallback_models",
            "openai/gpt-4o-mini,openai/gpt-4.1-mini",
        ),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch(
            "litellm.completion",
            side_effect=[
                RuntimeError("primary down"),
                RuntimeError("first fallback down"),
                completion_response,
            ],
        ) as mock_completion,
    ):
        response = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "route around provider issues"}],
            temperature=0.2,
            max_tokens=128,
        )

    attempted_models = [call.kwargs["model"] for call in mock_completion.call_args_list]
    assert attempted_models == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
    ]
    return {
        "attempted_models": attempted_models,
        "final_model": attempted_models[-1],
        "response_excerpt": response.choices[0].message.content,
    }


def _eval_provider_health_reroute() -> dict[str, Any]:
    first_fallback_response = _make_litellm_response("Recovered through the fallback chain.")
    rerouted_response = _make_litellm_response("Rerouted straight to the healthy fallback.")

    _reset_target_health()
    try:
        with (
            patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
            patch.object(settings, "fallback_model", ""),
            patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
            patch.object(settings, "llm_api_key", "primary-key"),
            patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
            patch.object(settings, "llm_target_cooldown_seconds", 300),
            patch(
                "litellm.completion",
                side_effect=[
                    RuntimeError("primary down"),
                    first_fallback_response,
                    rerouted_response,
                ],
            ) as mock_completion,
        ):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "recover once"}],
                temperature=0.2,
                max_tokens=128,
            )
            response = completion_with_fallback_sync(
                messages=[{"role": "user", "content": "recover again"}],
                temperature=0.2,
                max_tokens=128,
            )
    finally:
        _reset_target_health()

    attempted_models = [call.kwargs["model"] for call in mock_completion.call_args_list]
    assert attempted_models == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
        "openai/gpt-4o-mini",
    ]
    return {
        "cooldown_seconds": 300,
        "rerouted_model": attempted_models[-1],
        "attempted_models": attempted_models,
        "response_excerpt": response.choices[0].message.content,
    }


def _eval_local_runtime_profile() -> dict[str, Any]:
    completion_response = _make_litellm_response("Handled by a local helper profile.")

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(
            settings,
            "local_runtime_paths",
            "context_window_summary,session_title_generation,session_consolidation",
        ),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("litellm.completion", return_value=completion_response) as mock_completion,
    ):
        response = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "keep helper work local"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_consolidation",
        )

    assert mock_completion.call_count == 1
    assert mock_completion.call_args.kwargs["model"] == "ollama/llama3.2"
    assert mock_completion.call_args.kwargs["api_base"] == "http://localhost:11434/v1"
    return {
        "runtime_path": "session_consolidation",
        "runtime_profile": "local",
        "routed_model": mock_completion.call_args.kwargs["model"],
        "response_excerpt": response.choices[0].message.content,
    }


def _eval_helper_local_runtime_paths() -> dict[str, Any]:
    completion_response = _make_litellm_response("Handled by a local helper profile.")

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(
            settings,
            "local_runtime_paths",
            "context_window_summary,session_title_generation,session_consolidation",
        ),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("litellm.completion", return_value=completion_response) as mock_completion,
    ):
        routed_models: dict[str, str] = {}
        for runtime_path in (
            "context_window_summary",
            "session_title_generation",
            "session_consolidation",
        ):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": f"keep {runtime_path} local"}],
                temperature=0.2,
                max_tokens=128,
                runtime_path=runtime_path,
            )
            routed_models[runtime_path] = mock_completion.call_args.kwargs["model"]

    assert set(routed_models.values()) == {"ollama/llama3.2"}
    return {
        "runtime_profile": "local",
        "routed_models": routed_models,
    }


async def _eval_context_window_summary_audit() -> dict[str, Any]:
    _summary_cache.clear()
    success_response = _make_litellm_response("Condensed summary from local helper path.")
    messages = [{"role": "user", "content": "important context " * 10}]

    try:
        with (
            patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
            patch.object(settings, "llm_api_key", "primary-key"),
            patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
            patch.object(settings, "local_model", "ollama/llama3.2"),
            patch.object(settings, "local_llm_api_key", ""),
            patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
            patch.object(settings, "local_runtime_paths", "context_window_summary"),
            patch.object(settings, "fallback_model", ""),
            patch.object(settings, "fallback_models", ""),
            patch("litellm.completion", return_value=success_response) as mock_completion,
            patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event,
        ):
            success_summary = _summarize_middle(messages, session_id="ctx-success", range_key="0-1")
            _summary_cache.clear()
            with patch("src.agent.context_window.completion_with_fallback_sync", side_effect=RuntimeError("provider down")):
                degraded_summary = _summarize_middle(messages, session_id="ctx-fail", range_key="1-2")
            await asyncio.sleep(0)

        success = _find_audit_call(
            mock_log_event,
            event_type="background_task_succeeded",
            tool_name="context_window_summary",
        )
        degraded = _find_audit_call(
            mock_log_event,
            event_type="background_task_degraded",
            tool_name="context_window_summary",
        )
        return {
            "success_summary": success_summary,
            "success_model": mock_completion.call_args.kwargs["model"],
            "success_runtime_path": success["details"]["runtime_path"],
            "degraded_fallback": degraded["details"]["fallback"],
            "degraded_runtime_path": degraded["details"]["runtime_path"],
            "degraded_contains_truncation": "truncated" in degraded_summary,
        }
    finally:
        _summary_cache.clear()


def _eval_agent_local_runtime_profile() -> dict[str, Any]:
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(
            settings,
            "local_runtime_paths",
            "chat_agent,onboarding_agent,strategist_agent,memory_keeper",
        ),
    ):
        chat_model = get_model()
        onboarding_agent = create_onboarding_agent()
        strategist_agent = create_strategist_agent("Time: morning\nGoals: 2 active")
        specialist = create_specialist(
            "memory_keeper",
            "Memory specialist",
            [],
            temperature=0.5,
            max_steps=3,
        )

    routed_models = {
        "chat_agent": chat_model.model_id,
        "onboarding_agent": onboarding_agent.model.model_id,
        "strategist_agent": strategist_agent.model.model_id,
        "memory_keeper": specialist.model.model_id,
    }
    assert set(routed_models.values()) == {"ollama/llama3.2"}
    return {
        "runtime_profile": "local",
        "routed_models": routed_models,
    }


def _eval_delegation_local_runtime_profile() -> dict[str, Any]:
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(
            settings,
            "local_runtime_paths",
            "orchestrator_agent,goal_planner,web_researcher,file_worker",
        ),
        patch("src.agent.specialists.build_all_specialists", return_value=[]),
        patch("src.agent.factory.skill_manager.get_active_skills", return_value=[]),
    ):
        orchestrator = create_orchestrator()
        goal_planner = create_specialist(
            "goal_planner",
            "Goal planner specialist",
            [],
            temperature=0.4,
            max_steps=5,
        )
        web_researcher = create_specialist(
            "web_researcher",
            "Web researcher specialist",
            [],
            temperature=0.3,
            max_steps=8,
        )
        file_worker = create_specialist(
            "file_worker",
            "File worker specialist",
            [],
            temperature=0.3,
            max_steps=6,
        )

    routed_models = {
        "orchestrator_agent": orchestrator.model.model_id,
        "goal_planner": goal_planner.model.model_id,
        "web_researcher": web_researcher.model.model_id,
        "file_worker": file_worker.model.model_id,
    }
    assert set(routed_models.values()) == {"ollama/llama3.2"}
    return {
        "runtime_profile": "local",
        "routed_models": routed_models,
    }


def _eval_mcp_specialist_local_runtime_profile() -> dict[str, Any]:
    runtime_path = mcp_specialist_runtime_path("github-actions")
    tool = MagicMock()
    tool.name = "list_workflows"

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "local_runtime_paths", runtime_path),
        patch("src.agent.specialists.ToolCallingAgent") as mock_agent_cls,
    ):
        def _make_agent(**kwargs: Any) -> MagicMock:
            agent = MagicMock()
            agent.name = kwargs["name"]
            agent.model = kwargs["model"]
            agent.description = kwargs["description"]
            return agent

        mock_agent_cls.side_effect = _make_agent
        specialist = create_mcp_specialist("github-actions", [tool], description="GitHub workflows MCP")

    assert specialist.name == runtime_path
    assert specialist.model.model_id == "ollama/llama3.2"
    return {
        "runtime_profile": "local",
        "runtime_path": runtime_path,
        "routed_model": specialist.model.model_id,
    }


async def _eval_embedding_runtime_audit() -> dict[str, Any]:
    class _Vector:
        def __init__(self, payload: Any):
            self._payload = payload

        def tolist(self) -> Any:
            return self._payload

    class _FakeSentenceTransformer:
        def __init__(self, model_name: str):
            self.model_name = model_name

        def encode(self, value: Any, normalize_embeddings: bool = True) -> _Vector:
            if value == "fail":
                raise RuntimeError("encode crashed")
            if isinstance(value, list):
                return _Vector([[0.1, 0.2] for _ in value])
            return _Vector([0.1, 0.2])

    _reset_embedder_state()
    fake_module = types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer)

    try:
        with (
            patch.dict(sys.modules, {"sentence_transformers": fake_module}),
            patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event,
        ):
            vector = embed("hello")
            try:
                embed("fail")
            except RuntimeError:
                pass
            await asyncio.sleep(0)

        loaded = _find_audit_call(
            mock_log_event,
            event_type="integration_loaded",
            tool_name=f"embedding_model:{settings.embedding_model}",
        )
        failed = _find_audit_call(
            mock_log_event,
            event_type="integration_failed",
            tool_name=f"embedding_model:{settings.embedding_model}",
        )
        return {
            "loaded_model": loaded["details"]["name"],
            "loaded_integration_type": loaded["details"]["integration_type"],
            "vector_length": len(vector),
            "failure_stage": failed["details"]["stage"],
            "failure_error": failed["details"]["error"],
        }
    finally:
        _reset_embedder_state()


def _eval_runtime_model_overrides() -> dict[str, Any]:
    completion_response = _make_litellm_response("Handled by a runtime-specific remote override.")

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "local_runtime_paths", "chat_agent,session_consolidation"),
        patch.object(
            settings,
            "runtime_model_overrides",
            (
                "chat_agent=default:openai/gpt-4.1-mini,"
                "session_consolidation=default:openai/gpt-4o-mini"
            ),
        ),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("litellm.completion", return_value=completion_response) as mock_completion,
    ):
        response = completion_with_fallback_sync(
            messages=[{"role": "user", "content": "keep this helper remote"}],
            temperature=0.2,
            max_tokens=128,
            runtime_path="session_consolidation",
        )
        chat_model = get_model(runtime_path="chat_agent")

    assert mock_completion.call_count == 1
    assert mock_completion.call_args.kwargs["model"] == "openai/gpt-4o-mini"
    assert mock_completion.call_args.kwargs["api_base"] == "https://openrouter.ai/api/v1"
    assert chat_model.model_id == "openai/gpt-4.1-mini"
    assert chat_model.api_base == "https://openrouter.ai/api/v1"
    return {
        "completion_runtime_profile": "default",
        "completion_model": mock_completion.call_args.kwargs["model"],
        "agent_runtime_profile": chat_model._runtime_profile,
        "agent_model": chat_model.model_id,
        "response_excerpt": response.choices[0].message.content,
    }


def _eval_runtime_fallback_overrides() -> dict[str, Any]:
    completion_response = _make_litellm_response("Recovered through a runtime-specific fallback chain.")

    _reset_target_health()
    try:
        with (
            patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
            patch.object(settings, "llm_api_key", "primary-key"),
            patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
            patch.object(settings, "fallback_model", ""),
            patch.object(settings, "fallback_models", "openai/gpt-4o-mini"),
            patch.object(
                settings,
                "runtime_fallback_overrides",
                (
                    "chat_agent=openai/gpt-4.1-mini|openai/gpt-4.1-nano;"
                    "session_consolidation=openai/gpt-4.1-mini|openai/gpt-4.1-nano"
                ),
            ),
            patch.object(settings, "fallback_llm_api_key", ""),
            patch.object(settings, "fallback_llm_api_base", ""),
            patch(
                "litellm.completion",
                side_effect=[
                    RuntimeError("primary down"),
                    RuntimeError("first runtime fallback down"),
                    completion_response,
                ],
            ) as mock_completion,
        ):
            response = completion_with_fallback_sync(
                messages=[{"role": "user", "content": "keep the shared fallback chain path-specific"}],
                temperature=0.2,
                max_tokens=128,
                runtime_path="session_consolidation",
            )
            chat_model = get_model(runtime_path="chat_agent")

        attempted_models = [call.kwargs["model"] for call in mock_completion.call_args_list]
        assert attempted_models == [
            "openrouter/anthropic/claude-sonnet-4",
            "openai/gpt-4.1-mini",
            "openai/gpt-4.1-nano",
        ]
        assert [fallback.model_id for fallback in chat_model._fallback_models] == [
            "openai/gpt-4.1-mini",
            "openai/gpt-4.1-nano",
        ]
        return {
            "completion_attempted_models": attempted_models,
            "completion_final_model": attempted_models[-1],
            "agent_fallback_models": [fallback.model_id for fallback in chat_model._fallback_models],
            "response_excerpt": response.choices[0].message.content,
        }
    finally:
        _reset_target_health()


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


async def _eval_scheduled_local_runtime_profile() -> dict[str, Any]:
    ctx = _make_context(upcoming_events=[{"summary": "Ship local routing", "start": "09:30"}])
    mock_context_manager = MagicMock()
    mock_context_manager.refresh = AsyncMock(return_value=ctx)
    mock_deliver = AsyncMock()
    local_response = _make_litellm_response("Morning briefing via local profile.")

    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "local_runtime_paths", "daily_briefing,evening_review,activity_digest,weekly_activity_review"),
        patch.object(settings, "fallback_model", ""),
        patch.object(settings, "fallback_models", ""),
        patch("src.observer.manager.context_manager", mock_context_manager),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch("src.memory.vector_store.search_formatted", return_value="- [memory] Prefer local summaries"),
        patch("litellm.completion", return_value=local_response) as mock_completion,
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_daily_briefing()

    assert mock_completion.call_count == 1
    assert mock_completion.call_args.kwargs["model"] == "ollama/llama3.2"
    assert mock_completion.call_args.kwargs["api_base"] == "http://localhost:11434/v1"
    return {
        "job_name": "daily_briefing",
        "runtime_profile": "local",
        "routed_model": mock_completion.call_args.kwargs["model"],
        "delivered_excerpt": mock_deliver.call_args.args[0].content,
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


async def _eval_shell_tool_runtime_audit() -> dict[str, Any]:
    mock_client = MagicMock()
    mock_client.post.side_effect = httpx.TimeoutException("timeout")

    with (
        patch("src.tools.shell_tool.httpx.Client") as client_cls,
        patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event,
    ):
        client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        client_cls.return_value.__exit__ = MagicMock(return_value=False)
        result = shell_execute("import time; time.sleep(999)")
        await asyncio.sleep(0)

    assert "timed out" in result.lower()
    timed_out = _find_audit_call(
        mock_log_event,
        event_type="integration_timed_out",
        tool_name="sandbox:snekbox",
    )
    return {
        "result": result,
        "timeout_seconds": timed_out["details"]["timeout_seconds"],
    }


async def _eval_web_search_runtime_audit() -> dict[str, Any]:
    class MockDDGS:
        def __init__(self, **kwargs: Any):
            self.timeout = kwargs.get("timeout")

        def __enter__(self) -> "MockDDGS":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def text(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
            raise TimeoutError("Timed out")

    with (
        patch("src.tools.web_search_tool.DDGS", MockDDGS),
        patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event,
    ):
        result = web_search("slow search", max_results=3)
        await asyncio.sleep(0)

    assert "timed out" in result.lower()
    timed_out = _find_audit_call(
        mock_log_event,
        event_type="integration_timed_out",
        tool_name="web_search:duckduckgo",
    )
    return {
        "result": result,
        "timeout_seconds": timed_out["details"]["timeout_seconds"],
        "query_length": timed_out["details"]["query_length"],
    }


async def _eval_web_search_empty_result_audit() -> dict[str, Any]:
    class MockDDGS:
        def __init__(self, **kwargs: Any):
            self.timeout = kwargs.get("timeout")

        def __enter__(self) -> "MockDDGS":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def text(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
            return []

    with (
        patch("src.tools.web_search_tool.DDGS", MockDDGS),
        patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event,
    ):
        result = web_search("empty query", max_results=2)
        await asyncio.sleep(0)

    assert "no results found" in result.lower()
    empty_result = _find_audit_call(
        mock_log_event,
        event_type="integration_empty_result",
        tool_name="web_search:duckduckgo",
    )
    return {
        "result": result,
        "query_length": empty_result["details"]["query_length"],
        "result_count": empty_result["details"]["result_count"],
    }


async def _eval_browser_runtime_audit() -> dict[str, Any]:
    class _ImmediateFuture:
        def result(self):
            raise TimeoutError("Timed out")

    class _ImmediateExecutor:
        def __enter__(self) -> "_ImmediateExecutor":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def submit(self, _fn, *args, **kwargs) -> _ImmediateFuture:
            return _ImmediateFuture()

    with (
        patch("concurrent.futures.ThreadPoolExecutor", return_value=_ImmediateExecutor()),
        patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event,
    ):
        result = browse_webpage("https://example.com/slow", action="extract")
        await asyncio.sleep(0)

    assert "timed out after" in result.lower()
    timed_out = _find_audit_call(
        mock_log_event,
        event_type="integration_timed_out",
        tool_name="browser:playwright",
    )
    return {
        "result": result,
        "timeout_seconds": timed_out["details"]["timeout_seconds"],
        "hostname": timed_out["details"]["hostname"],
        "action": timed_out["details"]["action"],
    }


async def _eval_observer_calendar_source_audit() -> dict[str, Any]:
    mock_path = MagicMock()
    mock_path.exists.return_value = True

    with (
        patch("src.observer.sources.calendar_source._CREDENTIALS_PATH", mock_path),
        patch(
            "src.observer.sources.calendar_source._fetch_events",
            return_value={
                "upcoming_events": [{"summary": "Standup", "start": "09:00", "end": "09:15"}],
                "current_event": None,
            },
        ),
        patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event,
    ):
        result = await gather_calendar()

    success = _find_audit_call(
        mock_log_event,
        event_type="integration_succeeded",
        tool_name="observer_source:calendar",
    )
    return {
        "upcoming_event_count": len(result["upcoming_events"]),
        "audit_event_count": success["details"]["upcoming_event_count"],
    }


async def _eval_observer_git_source_audit() -> dict[str, Any]:
    with patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event:
        with patch("src.observer.sources.git_source.settings") as mock_settings:
            mock_settings.observer_git_repo_path = "/tmp/missing"
            mock_settings.workspace_dir = "/tmp/missing"
            result = gather_git()
        await asyncio.sleep(0)

    unavailable = _find_audit_call(
        mock_log_event,
        event_type="integration_unavailable",
        tool_name="observer_source:git",
    )
    return {
        "result": result,
        "reason": unavailable["details"]["reason"],
    }


async def _eval_observer_goal_source_audit() -> dict[str, Any]:
    goal1 = MagicMock(domain="productivity", title="Write docs")
    goal2 = MagicMock(domain="health", title="Exercise")
    mock_repo = AsyncMock()
    mock_repo.list_goals.return_value = [goal1, goal2]

    with (
        patch("src.goals.repository.goal_repository", mock_repo),
        patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event,
    ):
        result = await gather_goals()

    success = _find_audit_call(
        mock_log_event,
        event_type="integration_succeeded",
        tool_name="observer_source:goals",
    )
    return {
        "summary": result["active_goals_summary"],
        "goal_count": success["details"]["goal_count"],
        "domain_count": success["details"]["domain_count"],
    }


async def _eval_observer_time_source_audit() -> dict[str, Any]:
    fixed = datetime(2025, 6, 2, 10, 0)
    mock_dt = MagicMock(wraps=datetime)
    mock_dt.now.return_value = fixed

    with (
        patch("src.observer.sources.time_source.datetime", mock_dt),
        patch("src.observer.sources.time_source.settings", MagicMock(
            user_timezone="UTC",
            working_hours_start=9,
            working_hours_end=17,
        )),
        patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event,
    ):
        result = gather_time()
        await asyncio.sleep(0)

    success = _find_audit_call(
        mock_log_event,
        event_type="integration_succeeded",
        tool_name="observer_source:time",
    )
    return {
        "time_of_day": result["time_of_day"],
        "timezone": success["details"]["timezone"],
        "is_working_hours": success["details"]["is_working_hours"],
    }


async def _eval_strategist_tick_tool_audit() -> dict[str, Any]:
    mock_context_manager = MagicMock()
    mock_context_manager.refresh = AsyncMock(return_value=_make_context())
    audited_tool = wrap_tools_for_audit([_DummyStrategistTool()])[0]
    mock_log_event = AsyncMock()

    class DummyAgent:
        def run(self, _prompt: str) -> str:
            audited_tool()
            return (
                '{"should_intervene": false, "content": "", "intervention_type": "nudge", '
                '"urgency": 0, "reasoning": "No intervention"}'
            )

    with (
        patch("src.observer.manager.context_manager", mock_context_manager),
        patch("src.scheduler.jobs.strategist_tick.create_strategist_agent", return_value=DummyAgent()),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        await run_strategist_tick()

    tool_call = _find_audit_call(mock_log_event, event_type="tool_call", tool_name="get_goals")
    return {
        "tool_name": tool_call["tool_name"],
        "session_id": tool_call["session_id"],
        "policy_mode": tool_call["policy_mode"],
    }


async def _eval_session_consolidation_background_audit() -> dict[str, Any]:
    mock_log_event = AsyncMock()
    llm_response = _make_litellm_response(json.dumps({
        "facts": ["User is prioritizing runtime reliability"],
        "patterns": [],
        "goals": [],
        "reflections": [],
        "soul_updates": {},
    }))

    with (
        patch.object(session_manager, "get_history_text", AsyncMock(return_value="User: I need reliability.\nAssistant: Let's harden it.")),
        patch("src.memory.consolidator.read_soul", return_value="# Soul\nName: Hero"),
        patch("src.memory.consolidator.completion_with_fallback", AsyncMock(return_value=llm_response)),
        patch("src.memory.consolidator.add_memory"),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        await consolidate_session("eval-session")

    success = _find_audit_call(
        mock_log_event,
        event_type="background_task_succeeded",
        tool_name="session_consolidation",
    )
    return {
        "task_name": success["tool_name"],
        "session_id": success["session_id"],
        "stored_memory_count": success["details"]["stored_memory_count"],
    }


async def _eval_session_title_generation_background_audit() -> dict[str, Any]:
    sm = SessionManager()
    mock_log_event = AsyncMock()
    llm_response = _make_litellm_response("Reliability planning")
    fake_messages = [
        MagicMock(role="user", content="Help me harden the runtime."),
        MagicMock(role="assistant", content="Let's make the runtime observable and resilient."),
    ]

    with (
        patch.object(sm, "get", AsyncMock(return_value=MagicMock(id="eval-session", title="New Conversation"))),
        patch.object(sm, "update_title", AsyncMock(return_value=True)),
        patch("src.agent.session.get_session", return_value=_FakeDbSessionContext(fake_messages)),
        patch("src.llm_runtime.completion_with_fallback", AsyncMock(return_value=llm_response)),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        title = await sm.generate_title("eval-session")

    success = _find_audit_call(
        mock_log_event,
        event_type="background_task_succeeded",
        tool_name="session_title_generation",
    )
    assert title == "Reliability planning"
    return {
        "task_name": success["tool_name"],
        "session_id": success["session_id"],
        "title_length": success["details"]["title_length"],
    }


async def _eval_observer_delivery_gate_audit() -> dict[str, Any]:
    delivered_ctx = _make_context(user_state="available", interruption_mode="balanced", attention_budget_remaining=3)
    queued_ctx = _make_context(user_state="deep_work", interruption_mode="balanced", attention_budget_remaining=3)
    mock_context_manager = MagicMock()
    mock_context_manager.get_context.side_effect = [delivered_ctx, queued_ctx]
    mock_context_manager.decrement_attention_budget = MagicMock()
    mock_ws_manager = MagicMock()
    mock_ws_manager.broadcast = AsyncMock()
    mock_insight_queue = MagicMock()
    mock_insight_queue.enqueue = AsyncMock()
    mock_log_event = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_context_manager),
        patch("src.scheduler.connection_manager.ws_manager", mock_ws_manager),
        patch("src.observer.insight_queue.insight_queue", mock_insight_queue),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        await deliver_or_queue(
            WSResponse(type="proactive", content="Ship it", intervention_type="advisory", urgency=3)
        )
        await deliver_or_queue(
            WSResponse(type="proactive", content="Queue it", intervention_type="advisory", urgency=3)
        )

    delivered = _find_audit_call(
        mock_log_event,
        event_type="observer_delivery_delivered",
        tool_name="observer_delivery_gate",
    )
    queued = _find_audit_call(
        mock_log_event,
        event_type="observer_delivery_queued",
        tool_name="observer_delivery_gate",
    )
    return {
        "delivered_user_state": delivered["details"]["user_state"],
        "queued_user_state": queued["details"]["user_state"],
        "broadcast_calls": mock_ws_manager.broadcast.await_count,
        "enqueue_calls": mock_insight_queue.enqueue.await_count,
    }


async def _eval_observer_daemon_ingest_audit() -> dict[str, Any]:
    mock_context_manager = MagicMock()
    mock_context_manager.update_screen_context = MagicMock()
    mock_log_event = AsyncMock()
    mock_repo = MagicMock()
    mock_repo.create = AsyncMock(side_effect=[None, RuntimeError("db down")])

    with (
        patch("src.api.observer.context_manager", mock_context_manager),
        patch("src.observer.screen_repository.screen_observation_repo", mock_repo),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        await post_screen_context(
            ScreenContextRequest(
                active_window="VS Code",
                screen_context="Editing eval harness",
                observation=ScreenObservationData(
                    app="VS Code",
                    activity="coding",
                    blocked=False,
                ),
            )
        )
        await post_screen_context(
            ScreenContextRequest(
                active_window="Cursor",
                observation=ScreenObservationData(
                    app="Cursor",
                    activity="coding",
                    blocked=False,
                ),
            )
        )

    received = _find_audit_call(
        mock_log_event,
        event_type="integration_received",
        tool_name="observer_daemon:screen_context",
    )
    persisted = _find_audit_call(
        mock_log_event,
        event_type="integration_persisted",
        tool_name="observer_daemon:screen_context",
    )
    persist_failed = _find_audit_call(
        mock_log_event,
        event_type="integration_persist_failed",
        tool_name="observer_daemon:screen_context",
    )
    return {
        "received_has_observation": received["details"]["has_observation"],
        "persisted_app": persisted["details"]["app"],
        "persist_failed_error": persist_failed["details"]["error"],
        "update_calls": mock_context_manager.update_screen_context.call_count,
    }


_SCENARIOS: tuple[EvalScenario, ...] = (
    EvalScenario(
        name="chat_model_wrapper",
        category="runtime",
        description="Main agent model wiring exposes the shared fallback wrapper.",
        runner=_eval_chat_model_wrapper,
    ),
    EvalScenario(
        name="provider_fallback_chain",
        category="runtime",
        description="Direct completion retries across an ordered fallback chain instead of a single backup target.",
        runner=_eval_provider_fallback_chain,
    ),
    EvalScenario(
        name="provider_health_reroute",
        category="runtime",
        description="A recently failed primary provider target is temporarily rerouted to a healthy fallback target.",
        runner=_eval_provider_health_reroute,
    ),
    EvalScenario(
        name="local_runtime_profile",
        category="runtime",
        description="Bounded helper completions can route through a first-class local runtime profile.",
        runner=_eval_local_runtime_profile,
    ),
    EvalScenario(
        name="helper_local_runtime_paths",
        category="runtime",
        description="The helper runtime-path matrix can route context window, title generation, and consolidation completions through the local profile.",
        runner=_eval_helper_local_runtime_paths,
    ),
    EvalScenario(
        name="context_window_summary_audit",
        category="observability",
        description="Context window summarization records success and degraded truncation outcomes while still routing through the named helper path.",
        runner=_eval_context_window_summary_audit,
    ),
    EvalScenario(
        name="agent_local_runtime_profile",
        category="runtime",
        description="Core agent model factories can route through the first-class local runtime profile.",
        runner=_eval_agent_local_runtime_profile,
    ),
    EvalScenario(
        name="delegation_local_runtime_profile",
        category="runtime",
        description="Delegation paths can route the orchestrator and remaining built-in specialists through the local profile.",
        runner=_eval_delegation_local_runtime_profile,
    ),
    EvalScenario(
        name="mcp_specialist_local_runtime_profile",
        category="runtime",
        description="Dynamic MCP specialists can route through the local profile using their sanitized runtime path.",
        runner=_eval_mcp_specialist_local_runtime_profile,
    ),
    EvalScenario(
        name="embedding_runtime_audit",
        category="observability",
        description="The local embedding model boundary records load success and encode failures without live dependencies.",
        runner=_eval_embedding_runtime_audit,
    ),
    EvalScenario(
        name="runtime_model_overrides",
        category="runtime",
        description="Runtime paths can override their primary model selection without changing the global default or local-routing baseline.",
        runner=_eval_runtime_model_overrides,
    ),
    EvalScenario(
        name="runtime_fallback_overrides",
        category="runtime",
        description="Runtime paths can override their ordered fallback chain without changing the global fallback baseline.",
        runner=_eval_runtime_fallback_overrides,
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
        name="scheduled_local_runtime_profile",
        category="runtime",
        description="Scheduled completion-based jobs can route through the first-class local runtime profile.",
        runner=_eval_scheduled_local_runtime_profile,
    ),
    EvalScenario(
        name="shell_tool_timeout_contract",
        category="tool",
        description="Shell tool returns a clear timeout contract when sandbox execution stalls.",
        runner=_eval_shell_tool_timeout_contract,
    ),
    EvalScenario(
        name="shell_tool_runtime_audit",
        category="observability",
        description="Shell tool timeout records sandbox runtime audit coverage.",
        runner=_eval_shell_tool_runtime_audit,
    ),
    EvalScenario(
        name="web_search_runtime_audit",
        category="observability",
        description="Web search timeout records provider runtime audit coverage.",
        runner=_eval_web_search_runtime_audit,
    ),
    EvalScenario(
        name="web_search_empty_result_audit",
        category="observability",
        description="Web search no-hit responses record a distinct empty-result audit outcome.",
        runner=_eval_web_search_empty_result_audit,
    ),
    EvalScenario(
        name="browser_runtime_audit",
        category="observability",
        description="Browser timeouts record a distinct runtime audit outcome instead of collapsing into generic failures.",
        runner=_eval_browser_runtime_audit,
    ),
    EvalScenario(
        name="observer_calendar_source_audit",
        category="observability",
        description="Observer calendar source records runtime integration coverage for successful fetches.",
        runner=_eval_observer_calendar_source_audit,
    ),
    EvalScenario(
        name="observer_git_source_audit",
        category="observability",
        description="Observer git source records runtime integration coverage when the workspace has no git context.",
        runner=_eval_observer_git_source_audit,
    ),
    EvalScenario(
        name="observer_goal_source_audit",
        category="observability",
        description="Observer goal source records runtime integration coverage for active-goal summaries.",
        runner=_eval_observer_goal_source_audit,
    ),
    EvalScenario(
        name="observer_time_source_audit",
        category="observability",
        description="Observer time source records runtime integration coverage for successful time classification.",
        runner=_eval_observer_time_source_audit,
    ),
    EvalScenario(
        name="strategist_tick_tool_audit",
        category="observability",
        description="Strategist scheduler runs bind runtime context so wrapped strategist tools emit audit events.",
        runner=_eval_strategist_tick_tool_audit,
    ),
    EvalScenario(
        name="session_consolidation_background_audit",
        category="observability",
        description="Per-session memory consolidation records background-task audit success with deterministic mocks.",
        runner=_eval_session_consolidation_background_audit,
    ),
    EvalScenario(
        name="session_title_generation_background_audit",
        category="observability",
        description="Session title generation records background-task audit success without live providers.",
        runner=_eval_session_title_generation_background_audit,
    ),
    EvalScenario(
        name="observer_delivery_gate_audit",
        category="observability",
        description="Proactive delivery gate records delivered and queued audit outcomes with observer context details.",
        runner=_eval_observer_delivery_gate_audit,
    ),
    EvalScenario(
        name="observer_daemon_ingest_audit",
        category="observability",
        description="Observer daemon screen-context ingest records receive, persist success, and persist failure audit events.",
        runner=_eval_observer_daemon_ingest_audit,
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
