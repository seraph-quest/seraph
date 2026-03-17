"""Deterministic runtime evaluation harness for core guardian flows."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import tempfile
import types
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException
from smolagents import Tool

from config.settings import settings
from src.agent.session import SessionManager, session_manager
from src.agent.context_window import _summarize_middle, _summary_cache
from src.agent.factory import create_orchestrator, get_model
from src.agent.onboarding import create_onboarding_agent
from src.agent.specialists import create_mcp_specialist, create_specialist, mcp_specialist_runtime_path
from src.agent.strategist import create_strategist_agent
from src.api.mcp import test_server as test_mcp_server
from src.api.observer import ScreenContextRequest, ScreenObservationData, post_screen_context
from src.api.skills import UpdateSkillRequest, reload_skills as reload_skill_api, update_skill as update_skill_api
from src.audit.repository import audit_repository
from src.llm_runtime import FallbackLiteLLMModel, _reset_target_health, completion_with_fallback_sync
from src.memory.embedder import _reset_embedder_state, embed
from src.observer.sources.calendar_source import gather_calendar
from src.observer.sources.goal_source import gather_goals
from src.observer.sources.git_source import gather_git
from src.observer.screen_repository import ScreenObservationRepository
from src.observer.sources.time_source import gather_time
from src.memory.consolidator import consolidate_session
from src.memory import soul as soul_mod
from src.memory.vector_store import _reset_vector_store_state, add_memory, search
from src.observer.context import CurrentContext
from src.observer.delivery import deliver_or_queue, deliver_queued_bundle
from src.scheduler.jobs.daily_briefing import run_daily_briefing
from src.scheduler.jobs.evening_review import run_evening_review
from src.scheduler.jobs.strategist_tick import run_strategist_tick
from src.scheduler.connection_manager import BroadcastResult
from src.tools.audit import wrap_tools_for_audit
from src.tools.browser_tool import browse_webpage
from src.tools.filesystem_tool import read_file, write_file
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


class _FakeExecuteResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def scalars(self) -> "_FakeExecuteResult":
        return self

    def all(self) -> list[Any]:
        return self._rows


class _FakeScreenRepoSession:
    def __init__(self, execute_results: list[list[Any]]):
        self._execute_results = execute_results
        self.deleted: list[Any] = []

    async def execute(self, _query: Any) -> _FakeExecuteResult:
        if not self._execute_results:
            raise AssertionError("Unexpected screen repository execute call")
        return _FakeExecuteResult(self._execute_results.pop(0))

    async def delete(self, obj: Any) -> None:
        self.deleted.append(obj)


class _FakeScreenRepoContext:
    def __init__(self, session: _FakeScreenRepoSession):
        self._session = session

    async def __aenter__(self) -> _FakeScreenRepoSession:
        return self._session

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


async def _eval_vector_store_runtime_audit() -> dict[str, Any]:
    success_table = MagicMock()
    success_table.count_rows.return_value = 0
    success_table.add.return_value = None

    empty_table = MagicMock()
    empty_table.count_rows.return_value = 0

    _reset_vector_store_state()
    try:
        with patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event:
            with (
                patch("src.memory.vector_store._get_or_create_table", return_value=success_table),
                patch("src.memory.vector_store.embed", return_value=[0.1, 0.2]),
            ):
                memory_id = add_memory("remember this", category="fact", source_session_id="sess-1")

            with patch("src.memory.vector_store._get_or_create_table", return_value=empty_table):
                results = search("missing memory", top_k=3)

            with patch("src.memory.vector_store._get_or_create_table", side_effect=RuntimeError("db down")):
                failed_id = add_memory("broken", category="fact", source_session_id="sess-2")

            await asyncio.sleep(0)

        success = _find_audit_call(
            mock_log_event,
            event_type="integration_succeeded",
            tool_name="vector_store:memories",
        )
        empty = _find_audit_call(
            mock_log_event,
            event_type="integration_empty_result",
            tool_name="vector_store:memories",
        )
        failed = _find_audit_call(
            mock_log_event,
            event_type="integration_failed",
            tool_name="vector_store:memories",
        )
        return {
            "memory_created": bool(memory_id),
            "success_operation": success["details"]["operation"],
            "empty_reason": empty["details"]["reason"],
            "empty_query_length": empty["details"]["query_length"],
            "failed_operation": failed["details"]["operation"],
            "failed_error": failed["details"]["error"],
            "failed_memory_id": failed_id,
            "empty_results": len(results),
        }
    finally:
        _reset_vector_store_state()


async def _eval_soul_runtime_audit() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        soul_path = f"{tmpdir}/soul.md"
        original_path = soul_mod._soul_path
        soul_mod._soul_path = soul_path
        try:
            with patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event:
                default_text = soul_mod.read_soul()
                soul_mod.write_soul("# Soul\n\n## Identity\nHero")
                read_back = soul_mod.read_soul()
                soul_mod.ensure_soul_exists()
                with patch("src.memory.soul.open", side_effect=PermissionError("denied")):
                    try:
                        soul_mod.write_soul("broken")
                    except PermissionError:
                        pass
                await asyncio.sleep(0)

            empty = _find_audit_call(
                mock_log_event,
                event_type="integration_empty_result",
                tool_name="soul_file:soul.md",
            )
            success_calls = [
                call.kwargs for call in mock_log_event.call_args_list
                if call.kwargs.get("event_type") == "integration_succeeded"
                and call.kwargs.get("tool_name") == "soul_file:soul.md"
            ]
            skipped = _find_audit_call(
                mock_log_event,
                event_type="integration_skipped",
                tool_name="soul_file:soul.md",
            )
            failed = _find_audit_call(
                mock_log_event,
                event_type="integration_failed",
                tool_name="soul_file:soul.md",
            )
            write_success = next(call for call in success_calls if call["details"]["operation"] == "write")
            read_success = next(call for call in success_calls if call["details"]["operation"] == "read")
            return {
                "default_used": empty["details"]["used_default"],
                "default_contains_title": "# Soul of the Traveler" in default_text,
                "write_length": write_success["details"]["length"],
                "read_length": read_success["details"]["length"],
                "ensure_created": skipped["details"]["created"],
                "failed_operation": failed["details"]["operation"],
                "failed_error": failed["details"]["error"],
                "read_back_contains_hero": "Hero" in read_back,
            }
        finally:
            soul_mod._soul_path = original_path


async def _eval_filesystem_runtime_audit() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        with (
            patch.object(settings, "workspace_dir", tmpdir),
            patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event,
        ):
            missing_result = read_file.forward("missing.txt")
            write_result = write_file.forward("notes/today.txt", "hello filesystem")
            read_result = read_file.forward("notes/today.txt")
            directory_path = f"{tmpdir}/directory_only"
            os.mkdir(directory_path)
            not_a_file_result = read_file.forward("directory_only")
            try:
                read_file.forward("../../etc/passwd")
            except ValueError as blocked_error:
                blocked_message = str(blocked_error)
            else:  # pragma: no cover - defensive guard
                raise AssertionError("Expected path traversal guard to raise")

            with patch("pathlib.Path.write_text", side_effect=PermissionError("denied")):
                write_failure = write_file.forward("blocked.txt", "denied content")

            await asyncio.sleep(0)

        empty = _find_audit_call(
            mock_log_event,
            event_type="integration_empty_result",
            tool_name="filesystem:workspace",
        )
        success_calls = [
            call.kwargs for call in mock_log_event.call_args_list
            if call.kwargs.get("event_type") == "integration_succeeded"
            and call.kwargs.get("tool_name") == "filesystem:workspace"
        ]
        blocked = _find_audit_call(
            mock_log_event,
            event_type="integration_blocked",
            tool_name="filesystem:workspace",
        )
        failed_calls = [
            call.kwargs for call in mock_log_event.call_args_list
            if call.kwargs.get("event_type") == "integration_failed"
            and call.kwargs.get("tool_name") == "filesystem:workspace"
        ]
        read_success = next(call for call in success_calls if call["details"]["operation"] == "read")
        write_success = next(call for call in success_calls if call["details"]["operation"] == "write")
        not_a_file = next(call for call in failed_calls if call["details"].get("reason") == "not_a_file")
        write_failed = next(call for call in failed_calls if call["details"]["operation"] == "write")
        return {
            "missing_reason": empty["details"]["reason"],
            "write_length": write_success["details"]["length"],
            "read_length": read_success["details"]["length"],
            "blocked_path": blocked["details"]["file_path"],
            "blocked_error_contains_traversal": "Path traversal blocked" in blocked_message,
            "not_a_file_reason": not_a_file["details"]["reason"],
            "write_failed_error": write_failed["details"]["error"],
            "write_result_contains_success": "Successfully wrote" in write_result,
            "missing_result_contains_not_found": "File not found" in missing_result,
            "read_result_matches": read_result == "hello filesystem",
            "not_a_file_result_contains_error": "Not a file" in not_a_file_result,
            "write_failure_contains_error": "Failed to write file" in write_failure,
        }


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


def _eval_runtime_profile_preferences() -> dict[str, Any]:
    completion_response = _make_litellm_response("Recovered through the preferred remote profile.")

    _reset_target_health()
    try:
        with (
            patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
            patch.object(settings, "llm_api_key", "primary-key"),
            patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
            patch.object(settings, "local_model", "ollama/llama3.2"),
            patch.object(settings, "local_llm_api_key", ""),
            patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
            patch.object(
                settings,
                "runtime_profile_preferences",
                "chat_agent=local|default;session_consolidation=local|default",
            ),
            patch.object(settings, "fallback_model", "openai/gpt-4.1-mini"),
            patch.object(settings, "fallback_models", ""),
            patch.object(settings, "fallback_llm_api_key", ""),
            patch.object(settings, "fallback_llm_api_base", ""),
            patch(
                "litellm.completion",
                side_effect=[RuntimeError("local down"), completion_response],
            ) as mock_completion,
        ):
            response = completion_with_fallback_sync(
                messages=[{"role": "user", "content": "prefer the local runtime, then remote"}],
                temperature=0.2,
                max_tokens=128,
                runtime_path="session_consolidation",
            )
            chat_model = get_model(runtime_path="chat_agent")

        attempted_models = [call.kwargs["model"] for call in mock_completion.call_args_list]
        assert attempted_models == [
            "ollama/llama3.2",
            "openrouter/anthropic/claude-sonnet-4",
        ]
        assert [fallback.model_id for fallback in chat_model._fallback_models] == [
            "openrouter/anthropic/claude-sonnet-4",
            "openai/gpt-4.1-mini",
        ]
        return {
            "completion_runtime_profile": "local",
            "completion_attempted_models": attempted_models,
            "completion_final_model": attempted_models[-1],
            "agent_runtime_profile": chat_model._runtime_profile,
            "agent_fallback_models": [fallback.model_id for fallback in chat_model._fallback_models],
            "response_excerpt": response.choices[0].message.content,
        }
    finally:
        _reset_target_health()


def _eval_runtime_path_patterns() -> dict[str, Any]:
    with (
        patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
        patch.object(settings, "llm_api_key", "primary-key"),
        patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
        patch.object(settings, "local_model", "ollama/llama3.2"),
        patch.object(settings, "local_llm_api_key", ""),
        patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
        patch.object(settings, "runtime_profile_preferences", "mcp_*=local|default"),
        patch.object(
            settings,
            "runtime_model_overrides",
            "mcp_*=openai/gpt-4.1-mini,mcp_github_actions=local:ollama/coder",
        ),
        patch.object(
            settings,
            "runtime_fallback_overrides",
            (
                "mcp_*=openai/gpt-4.1-mini|openai/gpt-4.1-nano;"
                "mcp_github_actions=openai/gpt-4o-mini|openai/gpt-4.1-mini"
            ),
        ),
    ):
        wildcard_model = get_model(runtime_path="mcp_linear")
        exact_model = get_model(runtime_path="mcp_github_actions")

    assert wildcard_model.model_id == "openai/gpt-4.1-mini"
    assert wildcard_model._runtime_profile == "local"
    assert [fallback.model_id for fallback in wildcard_model._fallback_models] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-4.1-nano",
    ]

    assert exact_model.model_id == "ollama/coder"
    assert exact_model._runtime_profile == "local"
    assert [fallback.model_id for fallback in exact_model._fallback_models] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
    ]

    return {
        "wildcard_runtime_path": "mcp_linear",
        "wildcard_runtime_profile": wildcard_model._runtime_profile,
        "wildcard_model": wildcard_model.model_id,
        "wildcard_fallback_models": [fallback.model_id for fallback in wildcard_model._fallback_models],
        "exact_runtime_path": "mcp_github_actions",
        "exact_runtime_profile": exact_model._runtime_profile,
        "exact_model": exact_model.model_id,
        "exact_fallback_models": [fallback.model_id for fallback in exact_model._fallback_models],
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


async def _eval_daily_briefing_degraded_memories_audit() -> dict[str, Any]:
    ctx = _make_context(upcoming_events=[{"summary": "Ship reliable mornings", "start": "09:30"}])
    mock_context_manager = MagicMock()
    mock_context_manager.refresh = AsyncMock(return_value=ctx)
    mock_deliver = AsyncMock()
    mock_log_event = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_context_manager),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch("src.memory.vector_store.search_formatted", side_effect=Exception("vector store down")),
        patch(
            "src.scheduler.jobs.daily_briefing.completion_with_fallback",
            AsyncMock(return_value=_make_litellm_response("Good morning. Here is the plan.")),
        ),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        await run_daily_briefing()

    degraded = _find_audit_call(
        mock_log_event,
        event_type="background_task_degraded",
        tool_name="daily_briefing_inputs",
    )
    succeeded = _find_audit_call(
        mock_log_event,
        event_type="scheduler_job_succeeded",
        tool_name="daily_briefing",
    )
    return {
        "background_source": degraded["details"]["source"],
        "background_error": degraded["details"]["error"],
        "data_quality": succeeded["details"]["data_quality"],
        "degraded_inputs": succeeded["details"]["degraded_inputs"],
        "delivered": mock_deliver.await_count == 1,
    }


async def _eval_evening_review_degraded_inputs_audit() -> dict[str, Any]:
    ctx = _make_context(time_of_day="evening", is_working_hours=False)
    mock_context_manager = MagicMock()
    mock_context_manager.refresh = AsyncMock(return_value=ctx)
    mock_deliver = AsyncMock()
    mock_log_event = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_context_manager),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch("src.scheduler.jobs.evening_review._count_messages_today", AsyncMock(return_value=(0, True))),
        patch("src.scheduler.jobs.evening_review._get_completed_goals_today", AsyncMock(return_value=([], True))),
        patch(
            "src.scheduler.jobs.evening_review.completion_with_fallback",
            AsyncMock(return_value=_make_litellm_response("Quiet day. Rest well.")),
        ),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        await run_evening_review()

    succeeded = _find_audit_call(
        mock_log_event,
        event_type="scheduler_job_succeeded",
        tool_name="evening_review",
    )
    return {
        "data_quality": succeeded["details"]["data_quality"],
        "degraded_inputs": succeeded["details"]["degraded_inputs"],
        "message_count": succeeded["details"]["message_count"],
        "completed_goal_count": succeeded["details"]["completed_goal_count"],
        "delivered": mock_deliver.await_count == 1,
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
    mock_ws_manager.broadcast = AsyncMock(return_value=BroadcastResult(
        attempted_connections=2,
        delivered_connections=2,
        failed_connections=0,
    ))
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
        "delivered_connections": delivered["details"]["delivered_connections"],
        "broadcast_calls": mock_ws_manager.broadcast.await_count,
        "enqueue_calls": mock_insight_queue.enqueue.await_count,
    }


async def _eval_observer_delivery_transport_audit() -> dict[str, Any]:
    delivered_ctx = _make_context(user_state="available", interruption_mode="balanced", attention_budget_remaining=3)
    failed_ctx = _make_context(user_state="available", interruption_mode="balanced", attention_budget_remaining=3)
    mock_context_manager = MagicMock()
    mock_context_manager.get_context.side_effect = [delivered_ctx, failed_ctx]
    mock_context_manager.decrement_attention_budget = MagicMock()
    mock_ws_manager = MagicMock()
    mock_ws_manager.broadcast = AsyncMock(
        side_effect=[
            BroadcastResult(attempted_connections=2, delivered_connections=2, failed_connections=0),
            BroadcastResult(attempted_connections=1, delivered_connections=0, failed_connections=1),
            BroadcastResult(attempted_connections=2, delivered_connections=2, failed_connections=0),
            BroadcastResult(attempted_connections=1, delivered_connections=0, failed_connections=1),
        ]
    )
    mock_insight_queue = MagicMock()
    mock_insight_queue.enqueue = AsyncMock()
    mock_insight_queue.drain = AsyncMock(
        side_effect=[
            [MagicMock(content="Calendar alert: standup")],
            [MagicMock(content="Goal reminder: exercise")],
        ]
    )
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
            WSResponse(type="proactive", content="Missed transport", intervention_type="advisory", urgency=3)
        )
        delivered_bundle_count = await deliver_queued_bundle()
        failed_bundle_count = await deliver_queued_bundle()

    direct_failure = _find_audit_call(
        mock_log_event,
        event_type="observer_delivery_failed",
        tool_name="observer_delivery_gate",
    )
    bundle_delivered = None
    bundle_failed = None
    for call in mock_log_event.call_args_list:
        kwargs = call.kwargs
        if kwargs.get("tool_name") != "observer_delivery_gate":
            continue
        details = kwargs.get("details", {})
        if kwargs.get("event_type") == "observer_delivery_delivered" and details.get("intervention_type") == "proactive_bundle":
            bundle_delivered = kwargs
        if kwargs.get("event_type") == "observer_delivery_failed" and details.get("intervention_type") == "proactive_bundle":
            bundle_failed = kwargs
    assert bundle_delivered is not None
    assert bundle_failed is not None

    return {
        "direct_failure_error": direct_failure["details"]["error"],
        "direct_failure_delivered_connections": direct_failure["details"]["delivered_connections"],
        "bundle_delivered_count": delivered_bundle_count,
        "bundle_delivered_connections": bundle_delivered["details"]["delivered_connections"],
        "bundle_failed_count": failed_bundle_count,
        "bundle_failed_error": bundle_failed["details"]["error"],
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


async def _eval_mcp_test_api_audit() -> dict[str, Any]:
    mock_tool = MagicMock()
    mock_tool.name = "list_prs"
    mock_client = MagicMock()
    mock_client.get_tools.return_value = [mock_tool]
    mock_log_event = AsyncMock()

    with (
        patch("src.api.mcp.mcp_manager") as mock_mgr,
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        mock_mgr._config = {
            "gh": {
                "url": "http://gh/mcp",
                "headers": {"Authorization": "Bearer ${GITHUB_TOKEN}"},
            }
        }
        mock_mgr._check_unresolved_vars.side_effect = [["GITHUB_TOKEN"], [], []]
        mock_mgr._resolve_env_vars.side_effect = lambda value: value.replace("${GITHUB_TOKEN}", "ghp_test")

        auth_required = await test_mcp_server("gh")

        with patch("smolagents.MCPClient", return_value=mock_client):
            success = await test_mcp_server("gh")

        with patch("smolagents.MCPClient", side_effect=ConnectionError("refused")):
            try:
                await test_mcp_server("gh")
            except HTTPException as exc:
                failure_status_code = exc.status_code
            else:  # pragma: no cover - defensive guard
                raise AssertionError("Expected MCP test API failure to raise HTTPException")

    auth_required_event = _find_audit_call(
        mock_log_event,
        event_type="integration_auth_required",
        tool_name="mcp_test:gh",
    )
    success_event = _find_audit_call(
        mock_log_event,
        event_type="integration_succeeded",
        tool_name="mcp_test:gh",
    )
    failed_event = _find_audit_call(
        mock_log_event,
        event_type="integration_failed",
        tool_name="mcp_test:gh",
    )
    return {
        "auth_required_status": auth_required["status"],
        "missing_env_vars": auth_required_event["details"]["missing_env_vars"],
        "success_status": success["status"],
        "tool_count": success_event["details"]["tool_count"],
        "tool_names": success_event["details"]["tool_names"],
        "used_headers": success_event["details"]["used_headers"],
        "failure_status_code": failure_status_code,
        "failure_status": failed_event["details"]["status"],
        "failure_error": failed_event["details"]["error"],
    }


async def _eval_skills_api_audit() -> dict[str, Any]:
    mock_log_event = AsyncMock()
    mock_skill_manager = MagicMock()
    mock_skill_manager.disable.return_value = True
    mock_skill_manager.enable.return_value = False
    mock_skill_manager.reload.return_value = [
        {
            "name": "test-skill",
            "description": "A test skill",
            "requires_tools": ["web_search"],
            "user_invocable": True,
            "enabled": True,
            "file_path": "/tmp/skills/test-skill.md",
        },
        {
            "name": "simple-skill",
            "description": "No tool requirements",
            "requires_tools": [],
            "user_invocable": False,
            "enabled": True,
            "file_path": "/tmp/skills/simple-skill.md",
        },
    ]

    with (
        patch("src.api.skills.skill_manager", mock_skill_manager),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        updated = await update_skill_api("test-skill", UpdateSkillRequest(enabled=False))
        try:
            await update_skill_api("missing-skill", UpdateSkillRequest(enabled=True))
        except HTTPException as exc:
            missing_status_code = exc.status_code
        else:  # pragma: no cover - defensive guard
            raise AssertionError("Expected missing skill update to raise HTTPException")
        reloaded = await reload_skill_api()

    updated_event = _find_audit_call(
        mock_log_event,
        event_type="integration_succeeded",
        tool_name="skill:test-skill",
    )
    missing_event = _find_audit_call(
        mock_log_event,
        event_type="integration_failed",
        tool_name="skill:missing-skill",
    )
    reloaded_event = _find_audit_call(
        mock_log_event,
        event_type="integration_succeeded",
        tool_name="skills:reload",
    )
    return {
        "updated_status": updated["status"],
        "updated_enabled": updated_event["details"]["enabled"],
        "missing_status_code": missing_status_code,
        "missing_status": missing_event["details"]["status"],
        "reload_count": reloaded["count"],
        "reload_enabled_count": reloaded_event["details"]["enabled_count"],
        "reload_skill_names": reloaded_event["details"]["skill_names"],
    }


async def _eval_screen_repository_runtime_audit() -> dict[str, Any]:
    repo = ScreenObservationRepository()
    target_date = date(2026, 3, 16)
    week_start = date(2026, 3, 16)
    observation = MagicMock()
    observation.activity_type = "coding"
    observation.project = "seraph"
    observation.app_name = "VS Code"
    observation.duration_s = 1800
    observation.timestamp = datetime(2026, 3, 16, 9, 0, tzinfo=timezone.utc)
    old_observation = MagicMock()
    old_observation.timestamp = datetime(2025, 12, 1, 9, 0, tzinfo=timezone.utc)
    mock_log_event = AsyncMock()

    empty_daily_session = _FakeScreenRepoSession([[]])
    success_daily_session = _FakeScreenRepoSession([[observation]])
    cleanup_success_session = _FakeScreenRepoSession([[old_observation]])
    cleanup_skip_session = _FakeScreenRepoSession([[]])

    with patch.object(audit_repository, "log_event", mock_log_event):
        with patch("src.observer.screen_repository.get_session", return_value=_FakeScreenRepoContext(empty_daily_session)):
            empty_daily = await repo.get_daily_summary(target_date)

        with patch("src.observer.screen_repository.get_session", return_value=_FakeScreenRepoContext(success_daily_session)):
            success_daily = await repo.get_daily_summary(target_date)

        with patch.object(
            repo,
            "get_daily_summary",
            AsyncMock(
                side_effect=[
                    {
                        "date": "2026-03-16",
                        "total_observations": 1,
                        "total_tracked_minutes": 30,
                        "by_activity": {"coding": 1800},
                        "by_project": {"seraph": 1800},
                    },
                    {"date": "2026-03-17", "total_observations": 0, "total_tracked_minutes": 0},
                    {"date": "2026-03-18", "total_observations": 0, "total_tracked_minutes": 0},
                    {"date": "2026-03-19", "total_observations": 0, "total_tracked_minutes": 0},
                    {"date": "2026-03-20", "total_observations": 0, "total_tracked_minutes": 0},
                    {"date": "2026-03-21", "total_observations": 0, "total_tracked_minutes": 0},
                    {"date": "2026-03-22", "total_observations": 0, "total_tracked_minutes": 0},
                ]
            ),
        ):
            weekly = await repo.get_weekly_summary(week_start)

        with patch("src.observer.screen_repository.get_session", return_value=_FakeScreenRepoContext(cleanup_success_session)):
            deleted_count = await repo.cleanup_old(retention_days=90)

        with patch("src.observer.screen_repository.get_session", return_value=_FakeScreenRepoContext(cleanup_skip_session)):
            skipped_count = await repo.cleanup_old(retention_days=90)

        with patch.object(repo, "get_daily_summary", AsyncMock(side_effect=RuntimeError("db down"))):
            try:
                await repo.get_weekly_summary(week_start)
            except RuntimeError:
                pass

    empty_daily_event = _find_audit_call(
        mock_log_event,
        event_type="integration_empty_result",
        tool_name="screen_repository:daily_summary",
    )
    weekly_event = _find_audit_call(
        mock_log_event,
        event_type="integration_succeeded",
        tool_name="screen_repository:weekly_summary",
    )
    weekly_failed_event = _find_audit_call(
        mock_log_event,
        event_type="integration_failed",
        tool_name="screen_repository:weekly_summary",
    )
    cleanup_success_event = None
    cleanup_skipped_event = None
    for call in mock_log_event.call_args_list:
        kwargs = call.kwargs
        if kwargs.get("tool_name") != "screen_repository:cleanup":
            continue
        if kwargs.get("event_type") == "integration_succeeded":
            cleanup_success_event = kwargs
        if kwargs.get("event_type") == "integration_skipped":
            cleanup_skipped_event = kwargs
    assert cleanup_success_event is not None
    assert cleanup_skipped_event is not None

    return {
        "empty_daily_reason": empty_daily_event["details"]["reason"],
        "empty_daily_total_observations": empty_daily["total_observations"],
        "success_daily_total_observations": success_daily["total_observations"],
        "weekly_total_observations": weekly["total_observations"],
        "weekly_active_days": weekly_event["details"]["active_days"],
        "weekly_failure_error": weekly_failed_event["details"]["error"],
        "cleanup_deleted_count": deleted_count,
        "cleanup_logged_deleted_count": cleanup_success_event["details"]["deleted_count"],
        "cleanup_skipped_count": skipped_count,
        "cleanup_skipped_logged_deleted_count": cleanup_skipped_event["details"]["deleted_count"],
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
        name="vector_store_runtime_audit",
        category="observability",
        description="The local vector-store boundary records add success, search empty-result, and storage failures without live dependencies.",
        runner=_eval_vector_store_runtime_audit,
    ),
    EvalScenario(
        name="soul_runtime_audit",
        category="observability",
        description="The local soul-file boundary records defaulted reads, writes, ensure skips, and write failures without live dependencies.",
        runner=_eval_soul_runtime_audit,
    ),
    EvalScenario(
        name="filesystem_runtime_audit",
        category="observability",
        description="Filesystem tool reads and writes record workspace runtime audit coverage for success, empty, blocked, and failure outcomes.",
        runner=_eval_filesystem_runtime_audit,
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
        name="runtime_profile_preferences",
        category="runtime",
        description="Runtime paths can prefer an ordered local-vs-default profile chain before explicit fallback models.",
        runner=_eval_runtime_profile_preferences,
    ),
    EvalScenario(
        name="runtime_path_patterns",
        category="runtime",
        description="Runtime-path routing rules can use wildcard patterns, while exact path rules still override the wildcard baseline.",
        runner=_eval_runtime_path_patterns,
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
        name="daily_briefing_degraded_memories_audit",
        category="observability",
        description="Daily briefing records degraded memory-input quality when vector recall falls back to an empty baseline.",
        runner=_eval_daily_briefing_degraded_memories_audit,
    ),
    EvalScenario(
        name="evening_review_degraded_inputs_audit",
        category="observability",
        description="Evening review records degraded input quality when helper fallbacks mask partial data-source failures.",
        runner=_eval_evening_review_degraded_inputs_audit,
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
        name="observer_delivery_transport_audit",
        category="observability",
        description="Direct and bundled proactive delivery record transport-level delivered versus failed audit outcomes.",
        runner=_eval_observer_delivery_transport_audit,
    ),
    EvalScenario(
        name="observer_daemon_ingest_audit",
        category="observability",
        description="Observer daemon screen-context ingest records receive, persist success, and persist failure audit events.",
        runner=_eval_observer_daemon_ingest_audit,
    ),
    EvalScenario(
        name="mcp_test_api_audit",
        category="observability",
        description="Manual MCP server test requests record auth-required and successful tool-discovery audit events.",
        runner=_eval_mcp_test_api_audit,
    ),
    EvalScenario(
        name="skills_api_audit",
        category="observability",
        description="Skill toggle and reload requests record succeeded and failed runtime audit events.",
        runner=_eval_skills_api_audit,
    ),
    EvalScenario(
        name="screen_repository_runtime_audit",
        category="observability",
        description="Screen observation summaries and cleanup record boundary-level runtime audit outcomes for empty, success, and skipped paths.",
        runner=_eval_screen_repository_runtime_audit,
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
