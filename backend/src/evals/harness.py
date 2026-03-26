"""Deterministic runtime evaluation harness for core guardian flows."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import shutil
import sys
import time
import tempfile
import threading
import types
from contextlib import ExitStack, asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from smolagents import ActionStep, FinalAnswerStep, Tool, ToolCall
from smolagents.monitoring import Timing
from sqlmodel import SQLModel
from starlette.testclient import TestClient

from config.settings import settings
from src.approval.exceptions import ApprovalRequired
from src.agent.session import SessionManager, session_manager
from src.agent.context_window import _summarize_middle, _summary_cache
from src.agent.factory import create_agent, create_orchestrator, get_model
from src.agent.onboarding import create_onboarding_agent
from src.agent.specialists import build_all_specialists, create_mcp_specialist, create_specialist, mcp_specialist_runtime_path
from src.agent.strategist import create_strategist_agent
from src.guardian.state import build_guardian_state
from src.guardian.feedback import GuardianLearningSignal
from src.api.mcp import test_server as test_mcp_server
from src.api.observer import (
    InterventionFeedbackRequest,
    ScreenContextRequest,
    ScreenObservationData,
    ack_native_notification,
    daemon_status,
    dismiss_all_native_notifications,
    dismiss_native_notification,
    enqueue_test_native_notification,
    get_next_native_notification,
    get_observer_continuity,
    list_native_notifications,
    post_intervention_feedback,
    post_screen_context,
)
from src.api.skills import UpdateSkillRequest, reload_skills as reload_skill_api, update_skill as update_skill_api
from src.audit.repository import audit_repository
from src.app import create_app
from src.llm_runtime import FallbackLiteLLMModel, _reset_target_health, completion_with_fallback_sync
from src.memory.embedder import _reset_embedder_state, embed
from src.memory.repository import memory_repository
from src.memory.snapshots import _reset_bounded_guardian_snapshot_cache
from src.observer.sources.calendar_source import gather_calendar
from src.observer.sources.goal_source import gather_goals
from src.observer.sources.git_source import gather_git
from src.observer.screen_repository import ScreenObservationRepository
from src.observer.sources.time_source import gather_time
from src.memory.consolidator import consolidate_session
from src.memory import soul as soul_mod
from src.memory.vector_store import _reset_vector_store_state, add_memory, search
from src.observer.context import CurrentContext
from src.observer.manager import ContextManager
from src.observer.delivery import deliver_or_queue, deliver_queued_bundle
from src.observer.insight_queue import insight_queue
from src.observer.intervention_policy import decide_intervention
from src.observer.native_notification_queue import native_notification_queue
from src.observer.salience import derive_observer_assessment
from src.observer.user_state import DeliveryDecision
from src.scheduler.jobs.activity_digest import run_activity_digest
from src.scheduler.jobs.daily_briefing import run_daily_briefing
from src.scheduler.jobs.evening_review import run_evening_review
from src.scheduler.jobs.strategist_tick import run_strategist_tick
from src.scheduler.jobs.weekly_activity_review import run_weekly_activity_review
from src.scheduler.connection_manager import BroadcastResult
from src.tools.audit import wrap_tools_for_audit
from src.tools.browser_tool import browse_webpage
from src.tools.filesystem_tool import read_file, write_file
from src.tools.shell_tool import shell_execute
from src.tools.web_search_tool import web_search
from src.utils.background import drain_tracked_tasks
from src.workflows.manager import WorkflowManager
from src.models.schemas import WSResponse
from src.vault.repository import VaultRepository


Runner = Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]]

_TIMING = Timing(start_time=0.0, end_time=1.0)


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


class _DelegatedSearchTool(Tool):
    name = "web_search"
    description = "Dummy delegated web search"
    inputs = {
        "query": {"type": "string", "description": "Search query"},
    }
    output_type = "string"

    def forward(self, query: str) -> str:
        return (
            "1. Runtime reliability queue\n"
            "   URL: https://example.com/runtime\n"
            "   Finish delegated behavioral evals before policy work.\n\n"
            "2. Provider policy draft\n"
            "   URL: https://example.com/providers\n"
            "   Add capability-aware selection and decision logs."
        )


class _DelegatedFailingSearchTool(Tool):
    name = "web_search"
    description = "Dummy delegated failing web search"
    inputs = {
        "query": {"type": "string", "description": "Search query"},
    }
    output_type = "string"

    def forward(self, query: str) -> str:
        raise RuntimeError("search backend unavailable")


class _DelegatedBrowseTool(Tool):
    name = "browse_webpage"
    description = "Dummy delegated browser reader"
    inputs = {
        "url": {"type": "string", "description": "Page URL"},
    }
    output_type = "string"

    def forward(self, url: str) -> str:
        return (
            "Runtime roadmap note: delegated tool-heavy evals land before provider policy. "
            "Routing decision audit follows after capability metadata."
        )


class _DelegatedFailingBrowseTool(Tool):
    name = "browse_webpage"
    description = "Dummy delegated failing browser reader"
    inputs = {
        "url": {"type": "string", "description": "Page URL"},
    }
    output_type = "string"

    def forward(self, url: str) -> str:
        raise RuntimeError("browser timed out")


class _DelegatedWriteFileTool(Tool):
    name = "write_file"
    description = "Dummy delegated file writer"
    inputs = {
        "file_path": {"type": "string", "description": "Relative workspace path"},
        "content": {"type": "string", "description": "File contents"},
    }
    output_type = "string"

    def forward(self, file_path: str, content: str) -> str:
        resolved = os.path.join(settings.workspace_dir, file_path)
        os.makedirs(os.path.dirname(resolved), exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as handle:
            handle.write(content)
        return f"Successfully wrote {len(content)} characters to {file_path}"


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


@asynccontextmanager
async def _patched_async_db(*patch_targets: str):
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    @asynccontextmanager
    async def _get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    try:
        with ExitStack() as stack:
            for target in dict.fromkeys((
                *patch_targets,
                "src.agent.session.get_session",
                "src.memory.repository.get_session",
                "src.profile.service.get_db",
                "src.memory.hybrid_retrieval.get_session",
                "src.memory.decay.get_session",
                "src.memory.flush.get_session",
            )):
                stack.enter_context(patch(target, _get_session))
            yield
    finally:
        teardown_error: Exception | None = None
        try:
            await drain_tracked_tasks(timeout_seconds=5.0)
        except Exception as exc:
            teardown_error = exc
        finally:
            await engine.dispose()
        if teardown_error is not None:
            raise teardown_error


def _make_sync_client_with_db():
    tmpdir = tempfile.mkdtemp(prefix="seraph-eval-")
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _test_init_db():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    async def _test_close_db():
        await engine.dispose()

    targets = [
        "src.db.engine.get_session",
        "src.agent.session.get_session",
        "src.approval.repository.get_session",
        "src.audit.repository.get_session",
        "src.goals.repository.get_session",
        "src.guardian.feedback.get_session",
        "src.observer.insight_queue.get_session",
        "src.vault.repository.get_session",
        "src.api.settings.get_db",
        "src.memory.repository.get_session",
        "src.profile.service.get_db",
        "src.memory.hybrid_retrieval.get_session",
        "src.memory.decay.get_session",
        "src.memory.flush.get_session",
    ]
    patches = [patch(target, _get_session) for target in targets]
    patches.append(patch("src.app.init_db", _test_init_db))
    patches.append(patch("src.app.close_db", _test_close_db))
    patches.append(patch("src.app.init_scheduler", return_value=None))
    patches.append(patch("src.app.shutdown_scheduler"))
    patches.append(patch.object(settings, "workspace_dir", tmpdir))
    patches.append(patch.object(settings, "llm_log_dir", os.path.join(tmpdir, "logs")))
    patches.append(patch.object(soul_mod, "_soul_path", os.path.join(tmpdir, settings.soul_file)))
    patches.append(patch("src.vault.crypto._fernet", None))

    stack = ExitStack()
    stack.callback(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
    try:
        for item in patches:
            item.start()
        client = stack.enter_context(TestClient(create_app()))
        return client, patches, stack
    except Exception:
        stack.close()
        for item in reversed(patches):
            with suppress(Exception):
                item.stop()
        raise


def _receive_ws_json(ws: Any, *, timeout_seconds: float = 2.0) -> dict[str, Any]:
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _reader() -> None:
        try:
            result_queue.put(("text", ws.receive_text()))
        except Exception as exc:  # pragma: no cover - exercised via timeout/failure handling
            result_queue.put(("error", exc))

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    try:
        kind, payload = result_queue.get(timeout=timeout_seconds)
    except queue.Empty as exc:
        raise AssertionError(f"Timed out waiting for WebSocket message after {timeout_seconds}s") from exc

    if kind == "error":
        raise payload
    return json.loads(payload)


def _make_agent_steps(final_output: str = "It's sunny and 72°F today!") -> list[Any]:
    return [
        ToolCall(name="web_search", arguments={"query": "weather today"}, id="tc1"),
        ActionStep(
            step_number=1,
            timing=_TIMING,
            observations="Search returned 3 results about today's weather.",
            is_final_answer=False,
        ),
        FinalAnswerStep(output=final_output),
    ]


def _make_delegated_tool_workflow_steps(*, degrade_browse: bool = False) -> list[Any]:
    plan_path = "plans/runtime-tool-heavy-plan.md"
    research_task = "Research the strongest remaining runtime reliability gaps."
    write_task = f"Save a short action plan to {plan_path}."
    search_tool = wrap_tools_for_audit([_DelegatedSearchTool()])[0]
    browse_tool = wrap_tools_for_audit([
        _DelegatedFailingBrowseTool() if degrade_browse else _DelegatedBrowseTool()
    ])[0]
    write_tool = wrap_tools_for_audit([_DelegatedWriteFileTool()])[0]

    query = "seraph runtime reliability remaining gaps"
    url = "https://example.com/runtime"
    steps: list[Any] = [
        ToolCall(name="web_researcher", arguments={"task": research_task}, id="spec1"),
        ToolCall(name="web_search", arguments={"query": query}, id="tc1"),
    ]

    degraded = False
    try:
        search_result = search_tool(query=query)
        steps.append(ToolCall(name="browse_webpage", arguments={"url": url}, id="tc2"))
        page_summary = browse_tool(url=url)
        observation = f"Web researcher found:\n{search_result}\n\nPage summary:\n{page_summary}"
        plan_content = (
            "# Runtime reliability action plan\n"
            "- Finish delegated tool-heavy behavioral evals.\n"
            "- Add provider capability policy.\n"
            "- Add routing decision audit.\n"
        )
    except Exception as exc:
        degraded = True
        observation = (
            f"Web researcher hit an error: {exc}. "
            "Falling back to search snippets plus the in-repo runtime plan."
        )
        plan_content = (
            "# Runtime reliability fallback plan\n"
            "- Finish delegated tool-heavy behavioral evals.\n"
            "- Add provider capability policy.\n"
            "- Add routing decision audit.\n"
            "- Re-check incident trace blind spots.\n"
        )

    steps.append(
        ActionStep(
            step_number=1,
            timing=_TIMING,
            observations=observation,
            is_final_answer=False,
        )
    )
    steps.extend([
        ToolCall(name="file_worker", arguments={"task": write_task}, id="spec2"),
        ToolCall(name="write_file", arguments={"file_path": plan_path, "content": plan_content}, id="tc2"),
    ])
    write_result = write_tool(file_path=plan_path, content=plan_content)
    steps.append(
        ActionStep(
            step_number=2,
            timing=_TIMING,
            observations=write_result,
            is_final_answer=False,
        )
    )

    final_output = (
        f"Live research failed, but I saved a fallback action plan to {plan_path}."
        if degraded
        else f"I researched the current runtime gaps and saved an action plan to {plan_path}."
    )
    steps.append(FinalAnswerStep(output=final_output))
    return steps


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


def _eval_rest_chat_behavior() -> dict[str, Any]:
    client, patches, stack = _make_sync_client_with_db()
    try:
        mock_agent = MagicMock()
        mock_agent.run.side_effect = [
            "Hello from Seraph.",
            "Follow-up response",
        ]

        with (
            patch("src.api.chat.create_onboarding_agent", return_value=mock_agent),
            patch("src.api.chat.build_agent"),
            patch("src.memory.vector_store.search_formatted", return_value=""),
            patch("src.memory.consolidator.consolidate_session"),
        ):
            first = client.post("/api/chat", json={"message": "Hello"})
            first_payload = first.json()
            second = client.post(
                "/api/chat",
                json={"message": "Follow up", "session_id": first_payload["session_id"]},
            )
            second_payload = second.json()
            events = client.get("/api/audit/events").json()

        success_event = next(
            event for event in events
            if event["event_type"] == "agent_run_succeeded"
            and event["tool_name"] == "onboarding_agent"
            and event["details"]["transport"] == "rest"
        )
        return {
            "first_status": first.status_code,
            "second_status": second.status_code,
            "session_reused": first_payload["session_id"] == second_payload["session_id"],
            "first_response": first_payload["response"],
            "follow_up_response": second_payload["response"],
            "audit_transport": success_event["details"]["transport"],
        }
    finally:
        stack.close()
        for item in patches:
            item.stop()


def _eval_rest_chat_approval_contract() -> dict[str, Any]:
    client, patches, stack = _make_sync_client_with_db()
    try:
        mock_agent = MagicMock()
        mock_agent.run.side_effect = ApprovalRequired(
            approval_id="approval-123",
            session_id="s1",
            tool_name="shell_execute",
            risk_level="high",
            summary='Calling tool: shell_execute({"code": "[redacted]"})',
        )

        with (
            patch("src.api.chat.create_onboarding_agent", return_value=mock_agent),
            patch("src.api.chat.build_agent"),
            patch("src.memory.vector_store.search_formatted", return_value=""),
            patch("src.memory.consolidator.consolidate_session"),
        ):
            response = client.post("/api/chat", json={"message": "Run this"})
            payload = response.json()
            events = client.get("/api/audit/events").json()

        approval_event = next(
            event for event in events
            if event["event_type"] == "approval_requested"
            and event["tool_name"] == "shell_execute"
        )
        detail = payload["detail"]
        return {
            "status_code": response.status_code,
            "detail_type": detail["type"],
            "approval_id": detail["approval_id"],
            "tool_name": detail["tool_name"],
            "audit_summary_contains_shell": "shell_execute" in approval_event["summary"],
        }
    finally:
        stack.close()
        for item in patches:
            item.stop()


def _eval_rest_chat_timeout_contract() -> dict[str, Any]:
    client, patches, stack = _make_sync_client_with_db()
    try:
        mock_agent = MagicMock()
        mock_agent.run.return_value = "Should not be returned"

        async def _timeout_wait_for(coro: Any, timeout: float):
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise asyncio.TimeoutError

        with (
            patch("src.api.chat.create_onboarding_agent", return_value=mock_agent),
            patch("src.api.chat.build_agent"),
            patch("src.memory.vector_store.search_formatted", return_value=""),
            patch("src.api.chat.asyncio.wait_for", new=_timeout_wait_for),
            patch("src.memory.consolidator.consolidate_session"),
        ):
            response = client.post("/api/chat", json={"message": "Long request"})
            payload = response.json()
            events = client.get("/api/audit/events").json()

        timeout_event = next(
            event for event in events
            if event["event_type"] == "agent_run_timed_out"
            and event["tool_name"] == "onboarding_agent"
            and event["details"]["transport"] == "rest"
        )
        return {
            "status_code": response.status_code,
            "detail": payload["detail"],
            "timeout_seconds": timeout_event["details"]["timeout_seconds"],
        }
    finally:
        stack.close()
        for item in patches:
            item.stop()


def _eval_websocket_chat_behavior() -> dict[str, Any]:
    client, patches, stack = _make_sync_client_with_db()
    try:
        mock_agent = MagicMock()
        mock_agent.run.return_value = iter(_make_agent_steps())

        with (
            patch("src.api.ws._build_agent", return_value=(mock_agent, False, set())),
            patch("src.memory.consolidator.consolidate_session"),
        ):
            with client.websocket_connect("/ws/chat") as ws:
                welcome = _receive_ws_json(ws)
                ws.send_text(json.dumps({"type": "skip_onboarding"}))
                skipped = _receive_ws_json(ws)

                ws.send_text(json.dumps({
                    "type": "message",
                    "message": "What's the weather?",
                    "session_id": None,
                }))

                messages: list[dict[str, Any]] = []
                for _ in range(10):
                    msg = _receive_ws_json(ws)
                    messages.append(msg)
                    if msg["type"] == "final":
                        break

            events = client.get("/api/audit/events").json()

        final = next(msg for msg in messages if msg["type"] == "final")
        seqs = [msg["seq"] for msg in messages if msg.get("seq") is not None]
        success_event = next(
            event for event in events
            if event["event_type"] == "agent_run_succeeded"
            and event["tool_name"] == "chat_agent"
            and event["details"]["transport"] == "websocket"
        )
        return {
            "welcome_type": welcome["type"],
            "skip_type": skipped["type"],
            "step_count": sum(1 for msg in messages if msg["type"] == "step"),
            "final_content": final["content"],
            "seq_monotonic": all(seqs[i] > seqs[i - 1] for i in range(1, len(seqs))),
            "audit_tool_call_count": success_event["details"]["tool_call_count"],
        }
    finally:
        stack.close()
        for item in patches:
            item.stop()


def _eval_websocket_chat_approval_contract() -> dict[str, Any]:
    client, patches, stack = _make_sync_client_with_db()
    try:
        mock_agent = MagicMock()
        mock_agent.run.side_effect = ApprovalRequired(
            approval_id="approval-1",
            session_id="s1",
            tool_name="shell_execute",
            risk_level="high",
            summary='Calling tool: shell_execute({"code": "[redacted]"})',
        )

        with (
            patch("src.api.ws._build_agent", return_value=(mock_agent, False, set())),
            patch("src.memory.consolidator.consolidate_session"),
        ):
            with client.websocket_connect("/ws/chat") as ws:
                _ = _receive_ws_json(ws)
                ws.send_text(json.dumps({"type": "skip_onboarding"}))
                _ = _receive_ws_json(ws)
                ws.send_text(json.dumps({
                    "type": "message",
                    "message": "Run this snippet",
                    "session_id": None,
                }))

                approval_msg = None
                for _ in range(10):
                    msg = _receive_ws_json(ws)
                    if msg["type"] == "approval_required":
                        approval_msg = msg
                        break

            events = client.get("/api/audit/events").json()

        if approval_msg is None:
            raise AssertionError("Expected approval_required WebSocket message")
        approval_event = next(
            event for event in events
            if event["event_type"] == "approval_requested"
            and event["tool_name"] == "shell_execute"
        )
        return {
            "message_type": approval_msg["type"],
            "approval_id": approval_msg["approval_id"],
            "tool_name": approval_msg["tool_name"],
            "risk_level": approval_msg["risk_level"],
            "audit_summary_contains_shell": "shell_execute" in approval_event["summary"],
        }
    finally:
        stack.close()
        for item in patches:
            item.stop()


def _eval_websocket_chat_timeout_contract() -> dict[str, Any]:
    client, patches, stack = _make_sync_client_with_db()
    try:
        mock_agent = MagicMock()
        mock_agent.run.return_value = iter(_make_agent_steps())

        async def _timeout_wait_for(coro: Any, timeout: float):
            task = coro if isinstance(coro, asyncio.Future) else asyncio.create_task(coro)
            await asyncio.sleep(0)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise asyncio.TimeoutError

        with (
            patch("src.api.ws._build_agent", return_value=(mock_agent, False, set())),
            patch("src.api.ws.asyncio.wait_for", new=_timeout_wait_for),
            patch("src.memory.consolidator.consolidate_session"),
        ):
            with client.websocket_connect("/ws/chat") as ws:
                _ = _receive_ws_json(ws)
                ws.send_text(json.dumps({"type": "skip_onboarding"}))
                _ = _receive_ws_json(ws)
                ws.send_text(json.dumps({
                    "type": "message",
                    "message": "Long request",
                    "session_id": None,
                }))

                final_msg = None
                for _ in range(10):
                    msg = _receive_ws_json(ws)
                    if msg["type"] == "final":
                        final_msg = msg
                        break

            events = client.get("/api/audit/events").json()

        if final_msg is None:
            raise AssertionError("Expected timeout final WebSocket message")
        timeout_event = next(
            event for event in events
            if event["event_type"] == "agent_run_timed_out"
            and event["tool_name"] == "chat_agent"
            and event["details"]["transport"] == "websocket"
        )
        return {
            "final_type": final_msg["type"],
            "final_contains_timeout_copy": "taking too long" in final_msg["content"],
            "timeout_seconds": timeout_event["details"]["timeout_seconds"],
            "succeeded_event_present": any(
                event["event_type"] == "agent_run_succeeded"
                and event["tool_name"] == "chat_agent"
                and event["details"]["transport"] == "websocket"
                for event in events
            ),
        }
    finally:
        stack.close()
        for item in patches:
            item.stop()


def _eval_delegated_tool_workflow_behavior() -> dict[str, Any]:
    client, patches, stack = _make_sync_client_with_db()
    try:
        mock_agent = MagicMock()
        mock_agent.run.side_effect = lambda *_args, **_kwargs: iter(_make_delegated_tool_workflow_steps())

        with (
            patch("src.api.ws._build_agent", return_value=(mock_agent, False, {"web_researcher", "file_worker"})),
            patch("src.memory.consolidator.consolidate_session"),
        ):
            with client.websocket_connect("/ws/chat") as ws:
                _ = _receive_ws_json(ws)
                ws.send_text(json.dumps({"type": "skip_onboarding"}))
                _ = _receive_ws_json(ws)
                ws.send_text(json.dumps({
                    "type": "message",
                    "message": "Research the runtime gap and save a note.",
                    "session_id": None,
                }))

                messages: list[dict[str, Any]] = []
                for _ in range(12):
                    msg = _receive_ws_json(ws)
                    messages.append(msg)
                    if msg["type"] == "final":
                        break

            events = client.get("/api/audit/events").json()
            saved_path = os.path.join(settings.workspace_dir, "plans/runtime-tool-heavy-plan.md")
            with open(saved_path, encoding="utf-8") as handle:
                saved_content = handle.read()

        steps = [msg for msg in messages if msg["type"] == "step"]
        final = next(msg for msg in messages if msg["type"] == "final")
        success_event = next(
            event for event in events
            if event["event_type"] == "agent_run_succeeded"
            and event["tool_name"] == "chat_agent"
            and event["details"]["transport"] == "websocket"
        )
        return {
            "delegated_to_web_researcher": any("Delegating to web_researcher" in msg["content"] for msg in steps),
            "delegated_to_file_worker": any("Delegating to file_worker" in msg["content"] for msg in steps),
            "tool_steps_present": {
                "browse_webpage": any("Calling tool: browse_webpage" in msg["content"] for msg in steps),
                "web_search": any("Calling tool: web_search" in msg["content"] for msg in steps),
                "write_file": any("Calling tool: write_file" in msg["content"] for msg in steps),
            },
            "final_mentions_saved_plan": "runtime-tool-heavy-plan.md" in final["content"],
            "audit_result_tools": sorted(
                event["tool_name"]
                for event in events
                if event["event_type"] == "tool_result"
            ),
            "saved_plan_mentions_provider_policy": "provider capability policy" in saved_content.lower(),
            "tool_call_count": success_event["details"]["tool_call_count"],
        }
    finally:
        stack.close()
        for item in patches:
            item.stop()


def _eval_delegated_tool_workflow_degraded_behavior() -> dict[str, Any]:
    client, patches, stack = _make_sync_client_with_db()
    try:
        mock_agent = MagicMock()
        mock_agent.run.side_effect = lambda *_args, **_kwargs: iter(
            _make_delegated_tool_workflow_steps(degrade_browse=True)
        )

        with (
            patch("src.api.ws._build_agent", return_value=(mock_agent, False, {"web_researcher", "file_worker"})),
            patch("src.memory.consolidator.consolidate_session"),
        ):
            with client.websocket_connect("/ws/chat") as ws:
                _ = _receive_ws_json(ws)
                ws.send_text(json.dumps({"type": "skip_onboarding"}))
                _ = _receive_ws_json(ws)
                ws.send_text(json.dumps({
                    "type": "message",
                    "message": "Research the runtime gap and save a note.",
                    "session_id": None,
                }))

                messages: list[dict[str, Any]] = []
                for _ in range(12):
                    msg = _receive_ws_json(ws)
                    messages.append(msg)
                    if msg["type"] == "final":
                        break

            events = client.get("/api/audit/events").json()
            saved_path = os.path.join(settings.workspace_dir, "plans/runtime-tool-heavy-plan.md")
            with open(saved_path, encoding="utf-8") as handle:
                saved_content = handle.read()

        steps = [msg for msg in messages if msg["type"] == "step"]
        final = next(msg for msg in messages if msg["type"] == "final")
        success_event = next(
            event for event in events
            if event["event_type"] == "agent_run_succeeded"
            and event["tool_name"] == "chat_agent"
            and event["details"]["transport"] == "websocket"
        )
        return {
            "delegated_to_web_researcher": any("Delegating to web_researcher" in msg["content"] for msg in steps),
            "web_search_failed_audited": any(
                event["event_type"] == "tool_failed"
                and event["tool_name"] == "web_search"
                for event in events
            ),
            "browse_failed_audited": any(
                event["event_type"] == "tool_failed"
                and event["tool_name"] == "browse_webpage"
                for event in events
            ),
            "write_file_still_succeeded": any(
                event["event_type"] == "tool_result"
                and event["tool_name"] == "write_file"
                for event in events
            ),
            "final_mentions_fallback": "fallback action plan" in final["content"].lower(),
            "saved_plan_mentions_incident_trace": "incident trace blind spots" in saved_content.lower(),
            "tool_call_count": success_event["details"]["tool_call_count"],
        }
    finally:
        stack.close()
        for item in patches:
            item.stop()


def _eval_workflow_composition_behavior() -> dict[str, Any]:
    class _FakeTool:
        def __init__(self, name: str, responder: Callable[..., str]):
            self.name = name
            self.description = f"{name} tool"
            self.inputs = {}
            self.output_type = "string"
            self.calls: list[dict[str, Any]] = []
            self._responder = responder

        def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
            self.calls.append(dict(kwargs))
            return self._responder(**kwargs)

    with tempfile.TemporaryDirectory() as tmpdir:
        workflows_dir = os.path.join(tmpdir, "workflows")
        os.makedirs(workflows_dir, exist_ok=True)

        with open(os.path.join(workflows_dir, "web-brief.md"), "w", encoding="utf-8") as handle:
            handle.write(
                "---\n"
                "name: web-brief-to-file\n"
                "description: Search the web and save a note\n"
                "requires:\n"
                "  tools: [web_search, write_file]\n"
                "inputs:\n"
                "  query:\n"
                "    type: string\n"
                "  file_path:\n"
                "    type: string\n"
                "steps:\n"
                "  - id: search\n"
                "    tool: web_search\n"
                "    arguments:\n"
                "      query: \"{{ query }}\"\n"
                "  - id: save\n"
                "    tool: write_file\n"
                "    arguments:\n"
                "      file_path: \"{{ file_path }}\"\n"
                "      content: |\n"
                "        Search results\n"
                "\n"
                "        {{ steps.search.result }}\n"
                "result: Saved search results for {{ query }} to {{ file_path }}.\n"
                "---\n"
            )
        with open(os.path.join(workflows_dir, "goal-snapshot.md"), "w", encoding="utf-8") as handle:
            handle.write(
                "---\n"
                "name: goal-snapshot-to-file\n"
                "description: Export goals into a note\n"
                "requires:\n"
                "  tools: [get_goals, write_file]\n"
                "  skills: [goal-reflection]\n"
                "inputs:\n"
                "  file_path:\n"
                "    type: string\n"
                "steps:\n"
                "  - id: goals\n"
                "    tool: get_goals\n"
                "    arguments: {}\n"
                "  - id: save\n"
                "    tool: write_file\n"
                "    arguments:\n"
                "      file_path: \"{{ file_path }}\"\n"
                "      content: \"{{ steps.goals.result }}\"\n"
                "---\n"
            )

        mgr = WorkflowManager()
        mgr.init(workflows_dir)
        search_tool = _FakeTool("web_search", lambda query: f"SEARCH<{query}>")
        goal_tool = _FakeTool("get_goals", lambda: "- Ship workflows")
        write_calls: list[dict[str, Any]] = []

        def _write(file_path: str, content: str) -> str:
            write_calls.append({"file_path": file_path, "content": content})
            return f"saved {file_path}"

        write_tool_obj = _FakeTool("write_file", _write)

        without_skill = mgr.build_workflow_tools(
            [search_tool, goal_tool, write_tool_obj],
            active_skill_names=[],
        )
        with_skill = mgr.build_workflow_tools(
            [search_tool, goal_tool, write_tool_obj],
            active_skill_names=["goal-reflection"],
        )

        web_workflow = next(
            tool for tool in with_skill
            if tool.name == "workflow_web_brief_to_file"
        )
        result = web_workflow(
            query="workflow composition",
            file_path="notes/workflow.md",
        )

        workflow_tool = MagicMock()
        workflow_tool.name = "workflow_web_brief_to_file"
        native_tool = MagicMock()
        native_tool.name = "read_file"
        with patch("src.agent.specialists.create_specialist") as mock_create_specialist:
            with (
                patch("src.agent.specialists.discover_tools", return_value=[native_tool]),
                patch("src.agent.specialists.filter_tools", side_effect=lambda tools, *_args, **_kwargs: list(tools)),
                patch("src.agent.specialists.wrap_tools_for_secret_refs", side_effect=lambda tools: list(tools)),
                patch("src.agent.specialists.wrap_tools_for_audit", side_effect=lambda tools, **_kwargs: list(tools)),
                patch("src.agent.specialists.wrap_tools_for_approval", side_effect=lambda tools, **_kwargs: list(tools)),
                patch("src.agent.specialists.wrap_tools_with_forced_approval", side_effect=lambda tools, **_kwargs: list(tools)),
                patch("src.agent.specialists.mcp_manager.get_config", return_value=[]),
                patch("src.agent.specialists.skill_manager.get_active_skills", return_value=[types.SimpleNamespace(name="goal-reflection")]),
                patch("src.agent.specialists.workflow_manager.build_workflow_tools", return_value=[workflow_tool]),
            ):
                mock_create_specialist.side_effect = lambda name, description, tools, temperature, max_steps: types.SimpleNamespace(
                    name=name,
                    description=description,
                    tools=tools,
                )
                specialists = build_all_specialists()

        workflow_runner = next(s for s in specialists if s.name == "workflow_runner")
        return {
            "without_skill_tool_names": sorted(tool.name for tool in without_skill),
            "with_skill_tool_names": sorted(tool.name for tool in with_skill),
            "web_search_called_with": search_tool.calls[0]["query"],
            "saved_file_path": write_calls[0]["file_path"],
            "saved_content_contains_search": "SEARCH<workflow composition>" in write_calls[0]["content"],
            "result": result,
            "workflow_runner_present": workflow_runner.name == "workflow_runner",
            "workflow_runner_tool_names": [tool.name for tool in workflow_runner.tools],
        }


def _eval_provider_fallback_chain() -> dict[str, Any]:
    completion_response = _make_litellm_response("Resolved after ordered fallback chain.")

    with patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"), \
         patch.object(settings, "fallback_model", ""), \
         patch.object(
             settings,
             "fallback_models",
             "openai/gpt-4o-mini,openai/gpt-4.1-mini",
         ), \
         patch.object(settings, "llm_api_key", "primary-key"), \
         patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"), \
         patch(
             "litellm.completion",
             side_effect=[
                 RuntimeError("primary down"),
                 RuntimeError("first fallback down"),
                 completion_response,
             ],
         ) as mock_completion:
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
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4")
            )
            stack.enter_context(patch.object(settings, "fallback_model", ""))
            stack.enter_context(patch.object(settings, "fallback_models", "openai/gpt-4o-mini"))
            stack.enter_context(patch.object(settings, "llm_api_key", "primary-key"))
            stack.enter_context(
                patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1")
            )
            stack.enter_context(
                patch.object(settings, "llm_target_cooldown_seconds", 300)
            )
            mock_completion = stack.enter_context(
                patch(
                    "litellm.completion",
                    side_effect=[
                        RuntimeError("primary down"),
                        first_fallback_response,
                        rerouted_response,
                    ],
                )
            )
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
                "default_contains_title": "# Guardian Record" in default_text,
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


async def _eval_vault_runtime_audit() -> dict[str, Any]:
    repo = VaultRepository()

    def _encrypt(value: str) -> str:
        return f"ENC:{value}"

    def _decrypt(value: str) -> str:
        return value.removeprefix("ENC:")

    with (
        patch("src.vault.repository.encrypt", side_effect=_encrypt),
        patch("src.vault.repository.decrypt", side_effect=_decrypt),
        patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event,
    ):
        async with _patched_async_db("src.vault.repository.get_session"):
            await repo.store("service_token", "super-secret-token", description="Service token")
            stored = await repo.get("service_token")
            missing = await repo.get("missing")
            keys = await repo.list_keys()
            values = await repo.list_secret_values()
            delete_success = await repo.delete("service_token")
            delete_missing = await repo.delete("missing")
            await repo.store("broken", "cipher")

            with patch("src.vault.repository.decrypt", side_effect=ValueError("bad decrypt")):
                try:
                    await repo.get("broken")
                except ValueError:
                    pass

        store_success = next(
            call.kwargs for call in mock_log_event.call_args_list
            if call.kwargs.get("event_type") == "integration_succeeded"
            and call.kwargs.get("tool_name") == "vault:secrets"
            and call.kwargs["details"]["operation"] == "store"
        )
        list_values_success = next(
            call.kwargs for call in mock_log_event.call_args_list
            if call.kwargs.get("event_type") == "integration_succeeded"
            and call.kwargs.get("tool_name") == "vault:secrets"
            and call.kwargs["details"]["operation"] == "list_secret_values"
        )
        missing_get = next(
            call.kwargs for call in mock_log_event.call_args_list
            if call.kwargs.get("event_type") == "integration_empty_result"
            and call.kwargs.get("tool_name") == "vault:secrets"
            and call.kwargs["details"]["operation"] == "get"
        )
        missing_delete = next(
            call.kwargs for call in mock_log_event.call_args_list
            if call.kwargs.get("event_type") == "integration_empty_result"
            and call.kwargs.get("tool_name") == "vault:secrets"
            and call.kwargs["details"]["operation"] == "delete"
        )
        failed_get = next(
            call.kwargs for call in mock_log_event.call_args_list
            if call.kwargs.get("event_type") == "integration_failed"
            and call.kwargs.get("tool_name") == "vault:secrets"
            and call.kwargs["details"]["operation"] == "get"
        )

    return {
        "stored_value_matches": stored == "super-secret-token",
        "missing_get_is_none": missing is None,
        "store_action": store_success["details"]["action"],
        "list_key_count": len(keys),
        "redaction_value_count": len(values),
        "decryptable_count": list_values_success["details"]["decryptable_count"],
        "undecryptable_count": list_values_success["details"]["undecryptable_count"],
        "delete_success": delete_success,
        "delete_missing": delete_missing,
        "missing_get_reason": missing_get["details"]["reason"],
        "missing_delete_reason": missing_delete["details"]["reason"],
        "failed_operation": failed_get["details"]["operation"],
        "failed_error": failed_get["details"]["error"],
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


def _eval_provider_policy_capabilities() -> dict[str, Any]:
    completion_response = _make_litellm_response("Policy matched the fast fallback.")

    _reset_target_health()
    try:
        with (
            patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
            patch.object(settings, "llm_api_key", "primary-key"),
            patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
            patch.object(settings, "local_model", "ollama/llama3.2"),
            patch.object(settings, "local_llm_api_key", ""),
            patch.object(settings, "local_llm_api_base", "http://localhost:11434/v1"),
            patch.object(settings, "fallback_model", ""),
            patch.object(
                settings,
                "fallback_models",
                "openai/gpt-4.1-nano,openai/gpt-4o-mini,openai/gpt-4.1-mini",
            ),
            patch.object(
                settings,
                "provider_capability_overrides",
                (
                    "openrouter/anthropic/claude-sonnet-4=reasoning|tool_use;"
                    "openai/gpt-4.1-nano=cheap;"
                    "openai/gpt-4.1-mini=reasoning|tool_use;"
                    "openai/gpt-4o-mini=fast|cheap"
                ),
            ),
            patch.object(
                settings,
                "runtime_policy_intents",
                (
                    "chat_agent=local_first|reasoning|tool_use;"
                    "session_title_generation=fast|cheap"
                ),
            ),
            patch(
                "litellm.completion",
                side_effect=[RuntimeError("primary down"), completion_response],
            ) as mock_completion,
        ):
            response = completion_with_fallback_sync(
                messages=[{"role": "user", "content": "pick the best fast fallback"}],
                temperature=0.2,
                max_tokens=128,
                runtime_path="session_title_generation",
            )
            chat_model = get_model(runtime_path="chat_agent")
    finally:
        _reset_target_health()

    attempted_models = [call.kwargs["model"] for call in mock_completion.call_args_list]
    assert attempted_models == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4o-mini",
    ]
    assert chat_model._runtime_profile == "local"
    assert [fallback.model_id for fallback in chat_model._fallback_models] == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4.1-mini",
        "openai/gpt-4.1-nano",
        "openai/gpt-4o-mini",
    ]
    return {
        "chat_runtime_profile": chat_model._runtime_profile,
        "chat_fallback_models": [fallback.model_id for fallback in chat_model._fallback_models],
        "completion_attempted_models": attempted_models,
        "completion_final_model": attempted_models[-1],
        "response_excerpt": response.choices[0].message.content,
    }


def _eval_provider_policy_scoring() -> dict[str, Any]:
    completion_response = _make_litellm_response("Weighted policy score chose the strongest fallback.")

    _reset_target_health()
    try:
        with (
            patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
            patch.object(settings, "llm_api_key", "primary-key"),
            patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
            patch.object(settings, "fallback_model", ""),
            patch.object(settings, "fallback_models", ""),
            patch.object(
                settings,
                "runtime_fallback_overrides",
                (
                    "session_title_generation=openai/gpt-4o-mini|openai/gpt-4.1-nano;"
                    "chat_agent=openai/gpt-4o-mini|openai/gpt-4.1-mini"
                ),
            ),
            patch.object(
                settings,
                "provider_capability_overrides",
                (
                    "openrouter/anthropic/claude-sonnet-4=reasoning|tool_use;"
                    "openai/gpt-4o-mini=fast;"
                    "openai/gpt-4.1-nano=cheap|tool_use;"
                    "openai/gpt-4.1-mini=reasoning|tool_use"
                ),
            ),
            patch.object(
                settings,
                "runtime_policy_intents",
                (
                    "session_title_generation=fast|cheap|tool_use;"
                    "chat_agent=fast|reasoning|tool_use"
                ),
            ),
            patch.object(
                settings,
                "runtime_policy_scores",
                (
                    "session_title_generation=fast:5|cheap:4|tool_use:4;"
                    "chat_agent=fast:6|reasoning:4|tool_use:4"
                ),
            ),
            patch(
                "litellm.completion",
                side_effect=[RuntimeError("primary down"), completion_response],
            ) as mock_completion,
        ):
            response = completion_with_fallback_sync(
                messages=[{"role": "user", "content": "pick the highest weighted fallback"}],
                temperature=0.2,
                max_tokens=128,
                runtime_path="session_title_generation",
            )
            chat_model = FallbackLiteLLMModel(
                model_id="openrouter/anthropic/claude-sonnet-4",
                api_key="primary-key",
                api_base="https://openrouter.ai/api/v1",
                runtime_profile="default",
                runtime_path="chat_agent",
            )
    finally:
        _reset_target_health()

    attempted_models = [call.kwargs["model"] for call in mock_completion.call_args_list]
    assert attempted_models == [
        "openrouter/anthropic/claude-sonnet-4",
        "openai/gpt-4.1-nano",
    ]
    assert [fallback.model_id for fallback in chat_model._fallback_models] == [
        "openai/gpt-4.1-mini",
        "openai/gpt-4o-mini",
    ]

    return {
        "completion_attempted_models": attempted_models,
        "completion_final_model": attempted_models[-1],
        "completion_weighted_scores": {"fast": 5.0, "cheap": 4.0, "tool_use": 4.0},
        "agent_weighted_scores": {"fast": 6.0, "reasoning": 4.0, "tool_use": 4.0},
        "agent_fallback_models": [fallback.model_id for fallback in chat_model._fallback_models],
        "response_excerpt": response.choices[0].message.content,
    }


async def _eval_provider_policy_safeguards() -> dict[str, Any]:
    completion_response = _make_litellm_response("Guardrails selected the compliant target.")

    _reset_target_health()
    try:
        async with _patched_async_db("src.audit.repository.get_session"):
            with (
                patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
                patch.object(settings, "llm_api_key", "primary-key"),
                patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
                patch.object(settings, "fallback_model", ""),
                patch.object(settings, "fallback_models", "openai/gpt-4o-mini,openai/gpt-4.1-nano"),
                patch.object(
                    settings,
                    "provider_capability_overrides",
                    (
                        "openrouter/anthropic/claude-sonnet-4=reasoning;"
                        "openai/gpt-4o-mini=tool_use|fast;"
                        "openai/gpt-4.1-nano=cheap"
                    ),
                ),
                patch.object(
                    settings,
                    "provider_cost_tiers",
                    "openrouter/anthropic/claude-sonnet-4=high;openai/gpt-4o-mini=low;openai/gpt-4.1-nano=medium",
                ),
                patch.object(
                    settings,
                    "provider_latency_tiers",
                    "openrouter/anthropic/claude-sonnet-4=high;openai/gpt-4o-mini=low;openai/gpt-4.1-nano=medium",
                ),
                patch.object(
                    settings,
                    "provider_task_classes",
                    "openrouter/anthropic/claude-sonnet-4=analysis;openai/gpt-4o-mini=chat;openai/gpt-4.1-nano=analysis",
                ),
                patch.object(
                    settings,
                    "provider_budget_classes",
                    "openrouter/anthropic/claude-sonnet-4=high;openai/gpt-4o-mini=low;openai/gpt-4.1-nano=medium",
                ),
                patch.object(settings, "runtime_policy_intents", "chat_agent=tool_use|fast"),
                patch.object(settings, "runtime_policy_requirements", "chat_agent=tool_use"),
                patch.object(settings, "runtime_max_cost_tier", "chat_agent=medium"),
                patch.object(settings, "runtime_max_latency_tier", "chat_agent=medium"),
                patch.object(settings, "runtime_task_class", "chat_agent=chat"),
                patch.object(settings, "runtime_max_budget_class", "chat_agent=medium"),
                patch("litellm.completion", return_value=completion_response) as mock_completion,
            ):
                response = completion_with_fallback_sync(
                    messages=[{"role": "user", "content": "pick the guardrail-compliant provider"}],
                    temperature=0.2,
                    max_tokens=128,
                    runtime_path="chat_agent",
                )
                await asyncio.sleep(0)
                events = await audit_repository.list_events(limit=10)
    finally:
        _reset_target_health()

    routing_event = next(event for event in events if event["event_type"] == "llm_routing_decision")
    primary_candidate = next(
        candidate for candidate in routing_event["details"]["candidate_targets"] if candidate["source"] == "primary"
    )
    return {
        "attempted_models": [call.kwargs["model"] for call in mock_completion.call_args_list],
        "response_excerpt": response.choices[0].message.content,
        "selected_model": routing_event["details"]["selected_model"],
        "rerouted_from_policy_guardrails": routing_event["details"]["rerouted_from_policy_guardrails"],
        "required_policy_intents": routing_event["details"]["required_policy_intents"],
        "max_cost_tier": routing_event["details"]["max_cost_tier"],
        "max_latency_tier": routing_event["details"]["max_latency_tier"],
        "required_task_class": routing_event["details"]["required_task_class"],
        "max_budget_class": routing_event["details"]["max_budget_class"],
        "primary_missing_required_intents": primary_candidate["missing_required_intents"],
        "primary_cost_guardrail": primary_candidate["within_cost_guardrail"],
        "primary_latency_guardrail": primary_candidate["within_latency_guardrail"],
        "primary_task_class": primary_candidate["task_class"],
        "primary_task_guardrail": primary_candidate["matched_task_class"],
        "primary_budget_class": primary_candidate["budget_class"],
        "primary_budget_guardrail": primary_candidate["within_budget_guardrail"],
    }


async def _eval_provider_routing_decision_audit() -> dict[str, Any]:
    completion_response = _make_litellm_response("Policy matched the fast fallback.")
    first_agent_response = MagicMock()
    rerouted_agent_response = MagicMock()

    _reset_target_health()
    try:
        with (
            patch.object(settings, "default_model", "openrouter/anthropic/claude-sonnet-4"),
            patch.object(settings, "llm_api_key", "primary-key"),
            patch.object(settings, "llm_api_base", "https://openrouter.ai/api/v1"),
            patch.object(settings, "fallback_model", "ollama/llama3.2"),
            patch.object(settings, "fallback_models", "openai/gpt-4.1-nano,openai/gpt-4o-mini"),
            patch.object(settings, "fallback_llm_api_key", ""),
            patch.object(settings, "fallback_llm_api_base", "http://localhost:11434/v1"),
            patch.object(
                settings,
                "runtime_fallback_overrides",
                "session_title_generation=openai/gpt-4o-mini|openai/gpt-4.1-nano|openai/gpt-4.1-mini",
            ),
            patch.object(
                settings,
                "provider_capability_overrides",
                "openai/gpt-4.1-nano=cheap;openai/gpt-4o-mini=fast|cheap",
            ),
            patch.object(settings, "runtime_policy_intents", "session_title_generation=fast|cheap"),
            patch.object(settings, "llm_target_cooldown_seconds", 300),
            patch.object(audit_repository, "log_event", AsyncMock()) as mock_log_event,
            patch(
                "litellm.completion",
                side_effect=[RuntimeError("primary down"), completion_response],
            ),
            patch(
                "src.llm_runtime.BaseLiteLLMModel.generate",
                autospec=True,
                side_effect=[
                    RuntimeError("primary down"),
                    first_agent_response,
                    rerouted_agent_response,
                ],
            ),
        ):
            completion_with_fallback_sync(
                messages=[{"role": "user", "content": "pick the fastest fallback"}],
                temperature=0.2,
                max_tokens=128,
                runtime_path="session_title_generation",
            )

            first_model = FallbackLiteLLMModel(
                model_id="openrouter/anthropic/claude-sonnet-4",
                api_key="primary-key",
                api_base="https://openrouter.ai/api/v1",
                temperature=0.3,
                max_tokens=256,
            )
            first_model.generate([{"role": "user", "content": "hello"}])

            rerouted_model = FallbackLiteLLMModel(
                model_id="openrouter/anthropic/claude-sonnet-4",
                api_key="primary-key",
                api_base="https://openrouter.ai/api/v1",
                temperature=0.3,
                max_tokens=256,
            )
            rerouted_model.generate([{"role": "user", "content": "hello again"}])
            await asyncio.sleep(0)
    finally:
        _reset_target_health()

    routing_calls = [
        call.kwargs
        for call in mock_log_event.call_args_list
        if call.kwargs.get("event_type") == "llm_routing_decision"
    ]
    completion_decision = next(
        call
        for call in routing_calls
        if call["details"]["runtime_path"] == "session_title_generation"
    )
    rerouted_agent_decision = next(
        call
        for call in routing_calls
        if call["details"]["runtime_path"] == "agent_generate"
        and call["details"]["rerouted_from_unhealthy_primary"] is True
    )
    primary_candidate = next(
        candidate
        for candidate in rerouted_agent_decision["details"]["candidate_targets"]
        if candidate["source"] == "primary"
    )
    return {
        "completion_selected_model": completion_decision["details"]["selected_model"],
        "completion_attempt_order": completion_decision["details"]["attempt_order"],
        "completion_rejected_models": [
            candidate["model_id"]
            for candidate in completion_decision["details"]["rejected_targets"]
        ],
        "agent_selected_model": rerouted_agent_decision["details"]["selected_model"],
        "agent_attempt_order": rerouted_agent_decision["details"]["attempt_order"],
        "agent_primary_decision": primary_candidate["decision"],
        "agent_primary_reason_codes": primary_candidate["reason_codes"],
    }


async def _eval_session_bound_llm_trace() -> dict[str, Any]:
    async with _patched_async_db(
        "src.db.engine.get_session",
        "src.agent.session.get_session",
        "src.audit.repository.get_session",
    ):
        await session_manager.get_or_create("trace-session")
        await session_manager.add_message(
            "trace-session",
            "user",
            "Please title this conversation about building a reliable guardian agent.",
        )
        await session_manager.add_message(
            "trace-session",
            "assistant",
            "We should focus on incident traces, routing visibility, and runtime reliability.",
        )

        title_response = _make_litellm_response("Guardian Reliability")
        consolidation_response = _make_litellm_response(json.dumps({
            "facts": ["The user cares about reliability."],
            "patterns": [],
            "goals": [],
            "reflections": [],
            "soul_updates": {},
        }))

        with (
            patch("litellm.completion", side_effect=[title_response, consolidation_response]),
            patch(
                "src.memory.consolidator.sync_soul_file_to_profile",
                AsyncMock(return_value={"Identity": "Hero"}),
            ),
            patch("src.memory.consolidator.add_memory"),
            patch("src.memory.consolidator.update_profile_soul_section", AsyncMock()),
        ):
            await session_manager.generate_title("trace-session")
            await consolidate_session("trace-session")

        events = await audit_repository.list_events(limit=20, session_id="trace-session")
        title_event = next(
            event
            for event in events
            if event["event_type"] == "llm_primary_success"
            and event["details"]["runtime_path"] == "session_title_generation"
        )
        consolidation_event = next(
            event
            for event in events
            if event["event_type"] == "llm_primary_success"
            and event["details"]["runtime_path"] == "session_consolidation"
        )
        return {
            "session_id": "trace-session",
            "title_trace_has_request_id": bool(title_event["details"]["request_id"]),
            "consolidation_trace_has_request_id": bool(consolidation_event["details"]["request_id"]),
            "request_ids_differ": title_event["details"]["request_id"] != consolidation_event["details"]["request_id"],
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
        patch("src.memory.vector_store.search_with_status", return_value=([{"category": "memory", "text": "Prioritize reliability"}], False)),
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
        patch("src.memory.vector_store.search_with_status", return_value=([], True)),
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


async def _eval_daily_briefing_delivery_behavior() -> dict[str, Any]:
    ctx = _make_context(
        upcoming_events=[{"summary": "Design review", "start": "09:30"}],
        active_goals_summary="Close the proactive eval gap",
    )
    mock_context_manager = MagicMock()
    mock_context_manager.refresh = AsyncMock(return_value=ctx)
    mock_deliver = AsyncMock(return_value=DeliveryDecision.deliver)
    mock_log_event = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_context_manager),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch(
            "src.memory.vector_store.search_with_status",
            return_value=([{"category": "fact", "text": "Morning briefings should be concrete."}], False),
        ),
        patch(
            "src.scheduler.jobs.daily_briefing.completion_with_fallback",
            AsyncMock(
                return_value=_make_litellm_response(
                    "Good morning. Design review at 09:30. Focus on proactive eval coverage."
                )
            ),
        ),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        await run_daily_briefing()

    delivered_message = mock_deliver.await_args.args[0]
    delivered_kwargs = mock_deliver.await_args.kwargs
    succeeded = _find_audit_call(
        mock_log_event,
        event_type="scheduler_job_succeeded",
        tool_name="daily_briefing",
    )
    return {
        "message_type": delivered_message.type,
        "intervention_type": delivered_message.intervention_type,
        "scheduled_delivery": delivered_kwargs["is_scheduled"],
        "content_contains_design_review": "design review" in delivered_message.content.lower(),
        "upcoming_event_count": succeeded["details"]["upcoming_event_count"],
        "data_quality": succeeded["details"]["data_quality"],
    }


async def _eval_activity_digest_degraded_summary_audit() -> dict[str, Any]:
    mock_repo = MagicMock()
    mock_repo.get_daily_summary = AsyncMock(return_value={
        "date": date.today().isoformat(),
        "total_observations": 5,
        "by_activity": {"coding": 3600},
    })
    mock_deliver = AsyncMock()
    mock_log_event = AsyncMock()

    with (
        patch("src.observer.screen_repository.screen_observation_repo", mock_repo),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch(
            "src.scheduler.jobs.activity_digest.completion_with_fallback",
            AsyncMock(return_value=_make_litellm_response("Digest text")),
        ),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        await run_activity_digest()

    degraded = _find_audit_call(
        mock_log_event,
        event_type="background_task_degraded",
        tool_name="activity_digest_inputs",
    )
    succeeded = _find_audit_call(
        mock_log_event,
        event_type="scheduler_job_succeeded",
        tool_name="activity_digest",
    )
    return {
        "background_source": degraded["details"]["source"],
        "missing_fields": degraded["details"]["missing_fields"],
        "data_quality": succeeded["details"]["data_quality"],
        "degraded_inputs": succeeded["details"]["degraded_inputs"],
        "delivered": mock_deliver.await_count == 1,
    }


async def _eval_activity_digest_degraded_delivery_behavior() -> dict[str, Any]:
    mock_repo = MagicMock()
    mock_repo.get_daily_summary = AsyncMock(return_value={
        "date": date.today().isoformat(),
        "total_observations": 5,
        "by_activity": {"coding": 3600},
    })
    mock_deliver = AsyncMock(return_value=DeliveryDecision.deliver)
    mock_log_event = AsyncMock()

    with (
        patch("src.observer.screen_repository.screen_observation_repo", mock_repo),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch(
            "src.scheduler.jobs.activity_digest.completion_with_fallback",
            AsyncMock(return_value=_make_litellm_response("You spent most of today coding. Keep tomorrow calmer.")),
        ),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        await run_activity_digest()

    delivered_message = mock_deliver.await_args.args[0]
    delivered_kwargs = mock_deliver.await_args.kwargs
    degraded = _find_audit_call(
        mock_log_event,
        event_type="background_task_degraded",
        tool_name="activity_digest_inputs",
    )
    succeeded = _find_audit_call(
        mock_log_event,
        event_type="scheduler_job_succeeded",
        tool_name="activity_digest",
    )
    return {
        "message_type": delivered_message.type,
        "scheduled_delivery": delivered_kwargs["is_scheduled"],
        "content_mentions_coding": "coding" in delivered_message.content.lower(),
        "background_source": degraded["details"]["source"],
        "degraded_inputs": succeeded["details"]["degraded_inputs"],
        "data_quality": succeeded["details"]["data_quality"],
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


async def _eval_evening_review_degraded_delivery_behavior() -> dict[str, Any]:
    ctx = _make_context(time_of_day="evening", is_working_hours=False, recent_git_activity=[{"msg": "fix eval gap"}])
    mock_context_manager = MagicMock()
    mock_context_manager.refresh = AsyncMock(return_value=ctx)
    mock_deliver = AsyncMock(return_value=DeliveryDecision.deliver)
    mock_log_event = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_context_manager),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch("src.scheduler.jobs.evening_review._count_messages_today", AsyncMock(return_value=(0, True))),
        patch("src.scheduler.jobs.evening_review._get_completed_goals_today", AsyncMock(return_value=([], True))),
        patch(
            "src.scheduler.jobs.evening_review.completion_with_fallback",
            AsyncMock(return_value=_make_litellm_response("Quiet day. Rest well and reset for tomorrow.")),
        ),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        await run_evening_review()

    delivered_message = mock_deliver.await_args.args[0]
    delivered_kwargs = mock_deliver.await_args.kwargs
    succeeded = _find_audit_call(
        mock_log_event,
        event_type="scheduler_job_succeeded",
        tool_name="evening_review",
    )
    return {
        "message_type": delivered_message.type,
        "scheduled_delivery": delivered_kwargs["is_scheduled"],
        "content_mentions_tomorrow": "tomorrow" in delivered_message.content.lower(),
        "data_quality": succeeded["details"]["data_quality"],
        "degraded_inputs": succeeded["details"]["degraded_inputs"],
        "message_count": succeeded["details"]["message_count"],
    }


async def _eval_scheduled_local_runtime_profile() -> dict[str, Any]:
    ctx = _make_context(upcoming_events=[{"summary": "Ship local routing", "start": "09:30"}])
    mock_context_manager = MagicMock()
    mock_context_manager.refresh = AsyncMock(return_value=ctx)
    mock_deliver = AsyncMock(return_value=DeliveryDecision.deliver)
    daily_summary = {
        "total_observations": 4,
        "total_tracked_minutes": 180,
        "switch_count": 7,
        "by_activity": {"coding": 7200},
        "by_project": {"seraph": 7200},
        "longest_streaks": [{"activity": "coding", "duration_minutes": 90, "started_at": "2026-03-17T09:00:00Z"}],
    }
    weekly_summary = {
        "week_start": "2026-03-16",
        "week_end": "2026-03-22",
        "total_observations": 19,
        "total_tracked_minutes": 840,
        "by_activity": {"coding": 32400},
        "by_project": {"seraph": 28800},
        "daily_breakdown": [{"date": "2026-03-17", "tracked_minutes": 180, "observations": 4}],
    }
    local_responses = [
        _make_litellm_response("Morning briefing via local profile."),
        _make_litellm_response("Evening review via local profile."),
        _make_litellm_response("Activity digest via local profile."),
        _make_litellm_response("Weekly review via local profile."),
    ]

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
        patch("src.observer.screen_repository.screen_observation_repo.get_daily_summary", AsyncMock(return_value=daily_summary)),
        patch("src.observer.screen_repository.screen_observation_repo.get_weekly_summary", AsyncMock(return_value=weekly_summary)),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch("src.scheduler.jobs.evening_review._count_messages_today", AsyncMock(return_value=(5, False))),
        patch("src.scheduler.jobs.evening_review._get_completed_goals_today", AsyncMock(return_value=(["Close routing gap"], False))),
        patch("src.memory.vector_store.search_with_status", return_value=([{"category": "memory", "text": "Prefer local summaries"}], False)),
        patch("litellm.completion", side_effect=local_responses) as mock_completion,
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
    ):
        await run_daily_briefing()
        await run_evening_review()
        await run_activity_digest()
        await run_weekly_activity_review()

    assert mock_completion.call_count == 4
    routed_models = {
        "daily_briefing": mock_completion.call_args_list[0].kwargs["model"],
        "evening_review": mock_completion.call_args_list[1].kwargs["model"],
        "activity_digest": mock_completion.call_args_list[2].kwargs["model"],
        "weekly_activity_review": mock_completion.call_args_list[3].kwargs["model"],
    }
    routed_api_bases = {
        "daily_briefing": mock_completion.call_args_list[0].kwargs["api_base"],
        "evening_review": mock_completion.call_args_list[1].kwargs["api_base"],
        "activity_digest": mock_completion.call_args_list[2].kwargs["api_base"],
        "weekly_activity_review": mock_completion.call_args_list[3].kwargs["api_base"],
    }
    assert set(routed_models.values()) == {"ollama/llama3.2"}
    assert set(routed_api_bases.values()) == {"http://localhost:11434/v1"}
    return {
        "runtime_profile": "local",
        "routed_models": routed_models,
        "routed_api_bases": routed_api_bases,
        "delivery_count": mock_deliver.await_count,
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
        patch("src.scheduler.jobs.strategist_tick.build_guardian_state", AsyncMock(return_value=MagicMock())),
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


async def _eval_strategist_tick_behavior() -> dict[str, Any]:
    mock_context_manager = MagicMock()
    mock_context_manager.refresh = AsyncMock(return_value=_make_context(time_of_day="afternoon"))
    mock_agent = MagicMock()
    mock_agent.run.return_value = (
        '{"should_intervene": true, "content": "Time to refocus on the eval roadmap.", '
        '"intervention_type": "advisory", "urgency": 3, "reasoning": "Focus drift"}'
    )
    mock_deliver = AsyncMock(return_value=DeliveryDecision.deliver)
    mock_log_event = AsyncMock()

    with (
        patch("src.scheduler.jobs.strategist_tick.build_guardian_state", AsyncMock(return_value=MagicMock())),
        patch("src.scheduler.jobs.strategist_tick.create_strategist_agent", return_value=mock_agent),
        patch("src.observer.delivery.deliver_or_queue", mock_deliver),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        await run_strategist_tick()

    delivered_message = mock_deliver.await_args.args[0]
    succeeded = _find_audit_call(
        mock_log_event,
        event_type="scheduler_job_succeeded",
        tool_name="strategist_tick",
    )
    return {
        "message_type": delivered_message.type,
        "intervention_type": delivered_message.intervention_type,
        "urgency": delivered_message.urgency,
        "content_mentions_refocus": "refocus" in delivered_message.content.lower(),
        "delivery": succeeded["details"]["delivery"],
        "reasoning": delivered_message.reasoning,
    }


async def _eval_strategist_tick_learning_continuity_behavior() -> dict[str, Any]:
    from src.guardian.feedback import guardian_feedback_repository

    async with _patched_async_db(
        "src.agent.session.get_session",
        "src.guardian.feedback.get_session",
        "src.observer.insight_queue.get_session",
    ):
        await native_notification_queue.clear()
        for feedback_type, content in (
            ("helpful", "That workflow reminder landed at the right moment."),
            ("helpful", "Another workflow nudge was useful."),
            ("acknowledged", "Acknowledged the workflow reminder from desktop."),
            ("acknowledged", "Acted on the workflow reminder from the notification center."),
        ):
            intervention = await guardian_feedback_repository.create_intervention(
                session_id="strategist-learning",
                message_type="proactive",
                intervention_type="advisory",
                urgency=2,
                content=content,
                reasoning="aligned_work_activity",
                is_scheduled=False,
                guardian_confidence="grounded",
                data_quality="good",
                user_state="available",
                interruption_mode="balanced",
                policy_action="act",
                policy_reason="available_capacity",
                delivery_decision="deliver",
                latest_outcome="delivered",
            )
            await guardian_feedback_repository.record_feedback(
                intervention.id,
                feedback_type=feedback_type,
            )

        strategist_ctx = _make_context(
            user_state="available",
            interruption_mode="balanced",
            attention_budget_remaining=2,
            data_quality="good",
            observer_confidence="grounded",
            salience_level="high",
            salience_reason="aligned_work_activity",
            interruption_cost="high",
            last_daemon_post=time.time(),
        )
        mock_context_manager = MagicMock()
        mock_context_manager.get_context.return_value = strategist_ctx
        mock_context_manager.is_daemon_connected.return_value = True
        mock_context_manager.decrement_attention_budget = MagicMock()
        mock_agent = MagicMock()
        mock_agent.run.return_value = (
            '{"should_intervene": true, '
            '"content": "Stay on the workflow review while the current context is still loaded.", '
            '"intervention_type": "advisory", "urgency": 2, "reasoning": "Aligned work"}'
        )
        mock_ws_manager = MagicMock()
        mock_ws_manager.broadcast = AsyncMock(return_value=BroadcastResult(0, 0, 0))
        mock_log_event = AsyncMock()
        guardian_state = types.SimpleNamespace(
            confidence=types.SimpleNamespace(overall="grounded")
        )

        with (
            patch("src.scheduler.jobs.strategist_tick.build_guardian_state", AsyncMock(return_value=guardian_state)),
            patch("src.scheduler.jobs.strategist_tick.create_strategist_agent", return_value=mock_agent),
            patch("src.observer.manager.context_manager", mock_context_manager),
            patch("src.api.observer.context_manager", mock_context_manager),
            patch("src.scheduler.connection_manager.ws_manager", mock_ws_manager),
            patch.object(audit_repository, "log_event", mock_log_event),
        ):
            await run_strategist_tick()
            continuity = await get_observer_continuity()

        scheduler_event = _find_audit_call(
            mock_log_event,
            event_type="scheduler_job_succeeded",
            tool_name="strategist_tick",
        )
        delivered_event = _find_audit_call(
            mock_log_event,
            event_type="observer_delivery_delivered",
            tool_name="observer_delivery_gate",
        )
        notification = continuity["notifications"][0]
        intervention = continuity["recent_interventions"][0]
        queued_ids = [item.id for item in await insight_queue.peek_all()]
        if queued_ids:
            await insight_queue.delete_many(queued_ids)
        remaining_notifications = await native_notification_queue.count()
        await native_notification_queue.clear()

        return {
            "message_type": "proactive",
            "urgency": 2,
            "scheduler_delivery": scheduler_event["details"]["delivery"],
            "scheduler_policy_action": scheduler_event["details"]["policy_action"],
            "policy_reason": delivered_event["details"]["policy_reason"],
            "learning_bias": delivered_event["details"]["learning_bias"],
            "learning_channel_bias": delivered_event["details"]["learning_channel_bias"],
            "transport": delivered_event["details"]["transport"],
            "broadcast_delivered_connections": delivered_event["details"]["delivered_connections"],
            "continuity_notification_count": len(continuity["notifications"]),
            "continuity_queued_insight_count": continuity["queued_insight_count"],
            "continuity_surface": intervention["continuity_surface"],
            "continuity_excerpt_mentions_workflow": "workflow review" in intervention["content_excerpt"].lower(),
            "notification_intervention_matches": notification["intervention_id"] == intervention["id"],
            "remaining_notifications_before_cleanup": remaining_notifications,
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

    async with _patched_async_db():
        with (
            patch.object(session_manager, "get_history_text", AsyncMock(return_value="User: I need reliability.\nAssistant: Let's harden it.")),
            patch(
                "src.memory.consolidator.sync_soul_file_to_profile",
                AsyncMock(return_value={"Identity": "Hero"}),
            ),
            patch("src.memory.consolidator.completion_with_fallback", AsyncMock(return_value=llm_response)),
            patch("src.memory.consolidator.add_memory", return_value="vec-memory-1"),
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


async def _eval_session_consolidation_behavior() -> dict[str, Any]:
    mock_log_event = AsyncMock()
    llm_response = _make_litellm_response(json.dumps({
        "facts": ["User is building a guardian workspace"],
        "patterns": [],
        "goals": ["Ship behavioral guardian evals"],
        "reflections": [],
        "soul_updates": {"Goals": "- Ship the guardian workspace direction"},
    }))

    async with _patched_async_db():
        with (
            patch.object(
                session_manager,
                "get_history_text",
                AsyncMock(return_value=(
                    "User: I want Seraph to become a dense guardian workspace.\n"
                    "Assistant: Then we need behavioral evals, stronger state, and better operator UX."
                )),
            ),
            patch(
                "src.memory.consolidator.sync_soul_file_to_profile",
                AsyncMock(return_value={"Goals": "- Keep the system grounded"}),
            ),
            patch("src.memory.consolidator.completion_with_fallback", AsyncMock(return_value=llm_response)),
            patch("src.memory.consolidator.add_memory", return_value="vec-memory-1") as mock_add_memory,
            patch("src.memory.consolidator.update_profile_soul_section", AsyncMock()) as mock_update_soul,
            patch.object(audit_repository, "log_event", mock_log_event),
        ):
            await consolidate_session("guardian-session")

    success = _find_audit_call(
        mock_log_event,
        event_type="background_task_succeeded",
        tool_name="session_consolidation",
    )
    return {
        "stored_memory_count": success["details"]["stored_memory_count"],
        "soul_update_count": success["details"]["soul_update_count"],
        "memory_categories": [call.kwargs["category"] for call in mock_add_memory.call_args_list],
        "stored_texts": [call.kwargs["text"] for call in mock_add_memory.call_args_list],
        "updated_soul_section": mock_update_soul.call_args.args[0],
        "updated_soul_mentions_workspace": "guardian workspace" in mock_update_soul.call_args.args[1].lower(),
    }


async def _eval_memory_commitment_continuity_behavior() -> dict[str, Any]:
    from src.db.models import MemoryKind

    async with _patched_async_db(
        "src.agent.session.get_session",
        "src.guardian.feedback.get_session",
    ):
        await session_manager.get_or_create("memory-current")
        await session_manager.add_message("memory-current", "user", "What matters for Atlas right now?")
        await session_manager.add_message("memory-current", "assistant", "Let me ground that in linked memory.")

        atlas = await memory_repository.get_or_create_entity(
            canonical_name="Project Atlas",
            entity_type="project",
            aliases=["Atlas"],
        )
        await memory_repository.create_memory(
            content="Atlas launch is the active release project.",
            kind="project",
            summary="Atlas launch",
            importance=0.9,
            project_entity_id=atlas.id,
        )
        await memory_repository.create_memory(
            content="Send the Atlas checklist before Friday.",
            kind="commitment",
            summary="Send the Atlas checklist before Friday",
            importance=0.88,
            project_entity_id=atlas.id,
        )
        await memory_repository.create_memory(
            content="Renew the Nimbus enterprise contract.",
            kind="commitment",
            summary="Renew the Nimbus enterprise contract",
            importance=0.99,
        )
        await memory_repository.create_memory(
            content="Draft the Orion launch stakeholder update.",
            kind="commitment",
            summary="Draft the Orion launch stakeholder update",
            importance=0.97,
        )

        baseline_grouped = await memory_repository.list_memories_by_kinds(
            kinds=(MemoryKind.commitment,),
            limit_per_kind=2,
        )
        baseline_commitments = tuple(
            (memory.summary or memory.content)
            for memory in baseline_grouped.get("commitment", [])
        )
        baseline_context = "\n".join(
            f"[commitment] {text}"
            for text in baseline_commitments
        )
        baseline_buckets = {"commitment": baseline_commitments}

        ctx = _make_context(
            active_goals_summary="Support Atlas launch",
            active_window="VS Code",
            screen_context="Editing Atlas release notes",
            data_quality="good",
            observer_confidence="grounded",
            salience_level="high",
            salience_reason="active_goals",
            interruption_cost="low",
        )

        with (
            patch("src.observer.manager.context_manager.get_context", return_value=ctx),
            patch("src.agent.context_window._get_encoding", return_value=None),
            patch(
                "src.agent.session.session_manager.get_history_text",
                AsyncMock(return_value="User: What is the Atlas launch status?"),
            ),
            patch(
                "src.profile.service.sync_soul_file_to_profile",
                AsyncMock(return_value={"Identity": "Builder"}),
            ),
            patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
            patch("src.audit.repository.audit_repository.list_events", return_value=[]),
            patch(
                "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
                return_value=["Atlas"],
            ),
            patch(
                "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
                return_value="",
            ),
        ):
            state = await build_guardian_state(
                session_id="memory-current",
                user_message="What matters for Atlas right now?",
            )

        return {
            "baseline_bucket_excludes_linked_commitment": "Send the Atlas checklist before Friday"
            not in baseline_buckets.get("commitment", ()),
            "baseline_context_excludes_linked_commitment": "[commitment] Send the Atlas checklist before Friday"
            not in baseline_context,
            "linked_project_present": "Atlas launch" in state.world_model.active_projects,
            "memory_context_has_commitment": "[commitment] Send the Atlas checklist before Friday" in state.memory_context,
        }


async def _eval_memory_collaborator_lookup_behavior() -> dict[str, Any]:
    from src.db.models import MemoryKind

    async with _patched_async_db(
        "src.agent.session.get_session",
        "src.guardian.feedback.get_session",
    ):
        await session_manager.get_or_create("memory-current")
        await session_manager.add_message("memory-current", "user", "Who owns Atlas communications?")
        await session_manager.add_message("memory-current", "assistant", "Let me look up the collaborator context.")

        atlas = await memory_repository.get_or_create_entity(
            canonical_name="Project Atlas",
            entity_type="project",
            aliases=["Atlas"],
        )
        alice = await memory_repository.get_or_create_entity(
            canonical_name="Alice",
            entity_type="person",
        )
        await memory_repository.create_memory(
            content="Atlas launch is the active release project.",
            kind="project",
            summary="Atlas launch",
            importance=0.9,
            project_entity_id=atlas.id,
        )
        await memory_repository.create_memory(
            content="Alice owns Atlas launch communications.",
            kind="collaborator",
            summary="Alice owns Atlas launch communications",
            importance=0.87,
            subject_entity_id=alice.id,
            project_entity_id=atlas.id,
        )
        await memory_repository.create_memory(
            content="Morgan owns the Nimbus customer rollout.",
            kind="collaborator",
            summary="Morgan owns the Nimbus customer rollout",
            importance=0.99,
        )
        await memory_repository.create_memory(
            content="Priya owns the Orion executive brief.",
            kind="collaborator",
            summary="Priya owns the Orion executive brief",
            importance=0.97,
        )

        baseline_grouped = await memory_repository.list_memories_by_kinds(
            kinds=(MemoryKind.collaborator,),
            limit_per_kind=2,
        )
        baseline_collaborators = tuple(
            (memory.summary or memory.content)
            for memory in baseline_grouped.get("collaborator", [])
        )
        baseline_context = "\n".join(
            f"[collaborator] {text}"
            for text in baseline_collaborators
        )
        baseline_buckets = {"collaborator": baseline_collaborators}

        ctx = _make_context(
            active_goals_summary="Support Atlas launch",
            active_window="VS Code",
            screen_context="Editing Atlas release notes",
            data_quality="good",
            observer_confidence="grounded",
            salience_level="high",
            salience_reason="active_goals",
            interruption_cost="low",
        )

        with (
            patch("src.observer.manager.context_manager.get_context", return_value=ctx),
            patch(
                "src.profile.service.sync_soul_file_to_profile",
                AsyncMock(return_value={"Identity": "Builder"}),
            ),
            patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
            patch("src.audit.repository.audit_repository.list_events", return_value=[]),
            patch(
                "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
                return_value=["Atlas"],
            ),
            patch(
                "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
                return_value="",
            ),
        ):
            state = await build_guardian_state(
                session_id="memory-current",
                user_message="Who owns Atlas communications?",
            )

        return {
            "baseline_bucket_excludes_linked_collaborator": "Alice owns Atlas launch communications"
            not in baseline_buckets.get("collaborator", ()),
            "baseline_context_excludes_linked_collaborator": "[collaborator] Alice owns Atlas launch communications"
            not in baseline_context,
            "collaborator_present": "Alice owns Atlas launch communications" in state.world_model.collaborators,
            "memory_context_has_collaborator": "[collaborator] Alice owns Atlas launch communications" in state.memory_context,
            "active_projects_has_atlas": "Atlas launch" in state.world_model.active_projects,
        }


async def _eval_bounded_memory_snapshot_behavior() -> dict[str, Any]:
    from src.memory.snapshots import refresh_bounded_guardian_snapshot

    async with _patched_async_db(
        "src.agent.session.get_session",
        "src.guardian.feedback.get_session",
    ):
        await session_manager.get_or_create("snapshot-current")
        await session_manager.add_message("snapshot-current", "user", "What should I focus on next?")
        await session_manager.add_message("snapshot-current", "assistant", "Let me ground that in bounded recall.")
        await session_manager.replace_todos(
            "snapshot-current",
            [
                {"content": "Send the Atlas checklist", "completed": False},
                {"content": "Review launch notes", "completed": True},
            ],
        )

        await memory_repository.create_memory(
            content="Atlas launch is the active release project.",
            kind="project",
            summary="Atlas launch",
            importance=0.9,
        )
        await memory_repository.create_memory(
            content="User prefers concise morning briefings.",
            kind="communication_preference",
            summary="Prefers concise morning briefings",
            importance=0.85,
        )
        await refresh_bounded_guardian_snapshot(
            soul_context="# Soul\n\n## Identity\nBuilder\n\n## Goals\n- Keep the system grounded",
        )

        ctx = _make_context(
            active_goals_summary="Support Atlas launch",
            active_window="VS Code",
            screen_context="Editing Atlas launch checklist",
            data_quality="good",
            observer_confidence="grounded",
            salience_level="high",
            salience_reason="active_goals",
            interruption_cost="low",
        )

        with (
            patch("src.observer.manager.context_manager.get_context", return_value=ctx),
            patch("src.memory.soul.read_soul", return_value="# Soul\n\n## Identity\nBuilder\n\n## Goals\n- Keep the system grounded"),
            patch("src.memory.vector_store.search_with_status", return_value=([], False)),
            patch("src.audit.repository.audit_repository.list_events", return_value=[]),
            patch(
                "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
                return_value=["Atlas"],
            ),
            patch(
                "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
                return_value="",
            ),
        ):
            first_state = await build_guardian_state(
                session_id="snapshot-current",
                user_message="What should I focus on next?",
            )
            await memory_repository.create_memory(
                content="Hermes budget memo is now active.",
                kind="project",
                summary="Hermes budget memo",
                importance=0.95,
            )
            await memory_repository.create_memory(
                content="Neptune planning review is now active.",
                kind="project",
                summary="Neptune planning review",
                importance=0.93,
            )
            await session_manager.get_or_create("snapshot-new")
            await session_manager.add_message("snapshot-new", "user", "What should I focus on next?")
            await session_manager.add_message("snapshot-new", "assistant", "Ground me in the latest bounded memory.")
            second_state = await build_guardian_state(
                session_id="snapshot-current",
                user_message="What should I focus on next?",
            )
            fresh_state = await build_guardian_state(
                session_id="snapshot-new",
                user_message="What should I focus on next?",
            )
            fresh_session = await session_manager.get("snapshot-new")

        return {
            "bounded_snapshot_is_stable_within_session": (
                first_state.bounded_memory_context == second_state.bounded_memory_context
            ),
            "bounded_snapshot_includes_todo_overlay": "Send the Atlas checklist" in first_state.bounded_memory_context,
            "bounded_snapshot_line_count": max(
                len(second_state.bounded_memory_context.splitlines()),
                len(fresh_state.bounded_memory_context.splitlines()),
            ),
            "same_session_excludes_new_project": "Hermes budget memo" not in second_state.bounded_memory_context,
            "new_session_sees_new_project": "Hermes budget memo" in fresh_state.bounded_memory_context,
            "new_session_uses_real_session_record": fresh_session is not None,
        }


async def _eval_memory_supersession_filter_behavior() -> dict[str, Any]:
    from src.memory.snapshots import render_bounded_guardian_snapshot

    async with _patched_async_db():
        await memory_repository.create_memory(
            content="Atlas launch is delayed.",
            kind="project",
            summary="Atlas launch delayed",
            importance=0.95,
            status="superseded",
        )
        await memory_repository.create_memory(
            content="Atlas launch is on track.",
            kind="project",
            summary="Atlas launch on track",
            importance=0.9,
        )
        snapshot_content, _ = await render_bounded_guardian_snapshot(
            soul_context="# Soul\n\n## Identity\nBuilder",
        )

    return {
        "active_project_present": "Atlas launch on track" in snapshot_content,
        "superseded_project_filtered": "Atlas launch delayed" not in snapshot_content,
    }


async def _eval_memory_decay_contradiction_cleanup_behavior() -> dict[str, Any]:
    from src.db.models import MemoryKind
    from src.memory.decay import apply_memory_decay_policies
    from src.memory.hybrid_retrieval import retrieve_hybrid_memory

    async with _patched_async_db(
        "src.agent.session.get_session",
        "src.guardian.feedback.get_session",
        "src.memory.decay.get_session",
        "src.memory.hybrid_retrieval.get_session",
    ):
        _reset_bounded_guardian_snapshot_cache()
        await session_manager.get_or_create("decay-current")
        await session_manager.add_message("decay-current", "user", "What is the Atlas launch status?")
        await session_manager.add_message("decay-current", "assistant", "Let me ground that in current memory.")

        atlas = await memory_repository.get_or_create_entity(
            canonical_name="Project Atlas",
            entity_type="project",
            aliases=["Atlas"],
        )
        older = await memory_repository.create_memory(
            content="Atlas launch is delayed.",
            kind="project",
            summary="Atlas launch delayed",
            importance=0.8,
            confidence=0.7,
            project_entity_id=atlas.id,
            embedding_id="vec-atlas-delayed",
            last_confirmed_at=datetime.now(timezone.utc) - timedelta(days=7),
        )
        newer = await memory_repository.create_memory(
            content="Atlas launch is on track.",
            kind="project",
            summary="Atlas launch on track",
            importance=0.92,
            confidence=0.92,
            project_entity_id=atlas.id,
            embedding_id="vec-atlas-on-track",
            last_confirmed_at=datetime.now(timezone.utc),
        )

        decay_result = await apply_memory_decay_policies()
        superseded_memories = await memory_repository.list_memories(
            kind=MemoryKind.project,
            limit=10,
            status="superseded",
        )

        ctx = _make_context(
            active_goals_summary="Support Atlas launch",
            active_window="VS Code",
            screen_context="Reviewing Atlas release notes",
            data_quality="good",
            observer_confidence="grounded",
            salience_level="high",
            salience_reason="active_goals",
            interruption_cost="low",
        )

        with (
            patch("src.observer.manager.context_manager.get_context", return_value=ctx),
            patch(
                "src.profile.service.sync_soul_file_to_profile",
                AsyncMock(return_value={"Identity": "Builder"}),
            ),
            patch(
                "src.memory.hybrid_retrieval.search_with_status",
                return_value=(
                    [
                        {
                            "id": "vec-atlas-delayed",
                            "text": "Atlas launch is delayed.",
                            "category": "project",
                            "score": 0.11,
                            "created_at": "2026-03-25T09:00:00+00:00",
                        },
                        {
                            "id": "vec-atlas-on-track",
                            "text": "Atlas launch is on track.",
                            "category": "project",
                            "score": 0.09,
                            "created_at": "2026-03-25T10:00:00+00:00",
                        },
                    ],
                    False,
                ),
            ),
            patch("src.audit.repository.audit_repository.list_events", return_value=[]),
            patch(
                "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
                return_value=["Atlas"],
            ),
            patch(
                "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
                return_value="",
            ),
        ):
            hybrid = await retrieve_hybrid_memory(
                query="Atlas launch status",
                active_projects=("Atlas",),
                limit=4,
            )
            state = await build_guardian_state(
                session_id="decay-current",
                user_message="What is the Atlas launch status?",
            )

        return {
            "contradiction_count": decay_result.contradiction_count,
            "superseded_count": decay_result.superseded_count,
            "superseded_memory_count": len(superseded_memories),
            "hybrid_filters_superseded": "Atlas launch is delayed." not in hybrid.context,
            "hybrid_keeps_current": "Atlas launch is on track." in hybrid.context,
            "guardian_context_filters_superseded": (
                "Atlas launch delayed" not in state.memory_context
                and "Atlas launch is delayed." not in state.memory_context
                and "- [project] Atlas launch is delayed." not in state.memory_context
            ),
            "guardian_context_keeps_current": "[project] Atlas launch on track" in state.memory_context,
        }


async def _eval_procedural_memory_adaptation_behavior() -> dict[str, Any]:
    from src.db.models import MemoryKind
    from src.guardian.feedback import guardian_feedback_repository

    async with _patched_async_db(
        "src.agent.session.get_session",
        "src.guardian.feedback.get_session",
        "src.observer.insight_queue.get_session",
    ):
        _reset_bounded_guardian_snapshot_cache()
        await session_manager.get_or_create("procedural-current")
        await session_manager.add_message("procedural-current", "user", "Should you interrupt me during deep work?")
        await session_manager.add_message("procedural-current", "assistant", "I will ground that in learned policy.")

        ctx = _make_context(
            user_state="deep_work",
            interruption_mode="balanced",
            active_goals_summary="Ship Atlas launch",
            active_window="VS Code",
            screen_context="Focused implementation work",
            data_quality="good",
            observer_confidence="grounded",
            salience_level="medium",
            salience_reason="active_goals",
            interruption_cost="high",
        )

        async def _build_state() -> Any:
            with ExitStack() as stack:
                stack.enter_context(patch("src.observer.manager.context_manager.get_context", return_value=ctx))
                stack.enter_context(patch("src.agent.context_window._get_encoding", return_value=None))
                stack.enter_context(
                    patch(
                        "src.agent.session.session_manager.get_history_text",
                        AsyncMock(return_value="User: Should you interrupt me during deep work?"),
                    )
                )
                stack.enter_context(
                    patch(
                        "src.profile.service.sync_soul_file_to_profile",
                        AsyncMock(return_value={"Identity": "Builder"}),
                    )
                )
                stack.enter_context(
                    patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False))
                )
                stack.enter_context(
                    patch("src.audit.repository.audit_repository.list_events", return_value=[])
                )
                stack.enter_context(
                    patch(
                        "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
                        return_value=[],
                    )
                )
                stack.enter_context(
                    patch(
                        "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
                        return_value="",
                    )
                )
                return await build_guardian_state(
                    session_id="procedural-current",
                    user_message="Should you interrupt me during deep work?",
                )

        baseline_state = await _build_state()

        for content in (
            "This deep-work interruption was not helpful.",
            "Another deep-work interruption was not helpful.",
        ):
            intervention = await guardian_feedback_repository.create_intervention(
                session_id="procedural-current",
                message_type="proactive",
                intervention_type="advisory",
                urgency=3,
                content=content,
                reasoning="aligned_work_activity",
                is_scheduled=False,
                guardian_confidence="grounded",
                data_quality="good",
                user_state="deep_work",
                interruption_mode="balanced",
                policy_action="act",
                policy_reason="available_capacity",
                delivery_decision="deliver",
                latest_outcome="delivered",
                transport="browser",
            )
            await guardian_feedback_repository.record_feedback(
                intervention.id,
                feedback_type="not_helpful",
            )

        active_procedural_memories = await memory_repository.list_memories(
            kind=MemoryKind.procedural,
            limit=20,
        )

        adapted_state = await _build_state()

        adapted_memory_context = adapted_state.memory_context.lower()
        adapted_bounded_context = adapted_state.bounded_memory_context.lower()

        return {
            "baseline_snapshot_has_no_procedural_rule": (
                "avoid direct interruption during deep-work" not in baseline_state.bounded_memory_context.lower()
            ),
            "same_session_snapshot_refreshes": (
                baseline_state.bounded_memory_context != adapted_state.bounded_memory_context
            ),
            "adapted_memory_context_has_timing_rule": (
                "avoid direct interruption during deep-work" in adapted_memory_context
            ),
            "adapted_memory_context_has_delivery_rule": (
                "reduce direct interruptions" in adapted_memory_context
            ),
            "adapted_bounded_context_has_timing_rule": (
                "avoid direct interruption during deep-work" in adapted_bounded_context
            ),
            "active_procedural_memory_count": len(active_procedural_memories),
            "bounded_snapshot_line_count": len(adapted_state.bounded_memory_context.splitlines()),
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


async def _eval_guardian_state_synthesis() -> dict[str, Any]:
    async with _patched_async_db(
        "src.agent.session.get_session",
        "src.guardian.feedback.get_session",
    ):
        await session_manager.get_or_create("current")
        await session_manager.add_message("current", "user", "What should Seraph improve next?")
        await session_manager.add_message("current", "assistant", "Build explicit guardian state.")
        await session_manager.get_or_create("prior")
        await session_manager.update_title("prior", "Prior roadmap")
        await session_manager.add_message("prior", "assistant", "Land guardian-state synthesis next.")

        ctx = _make_context(
            active_goals_summary="Ship guardian state",
            active_window="VS Code",
            screen_context="Editing roadmap",
            data_quality="good",
            observer_confidence="grounded",
            salience_level="high",
            salience_reason="active_goals",
            interruption_cost="low",
        )

        with (
            patch("src.observer.manager.context_manager.get_context", return_value=ctx),
            patch("src.memory.soul.read_soul", return_value="# Soul\n\n## Identity\nBuilder"),
            patch(
                "src.memory.vector_store.search_with_status",
                return_value=([{"category": "goal", "text": "Ship guardian state"}], False),
            ),
            patch(
                "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
                return_value="- advisory delivered, feedback=helpful: Stretch and refocus.",
            ),
            patch("src.agent.factory.get_model", return_value=MagicMock()),
            patch("src.agent.factory.ToolCallingAgent") as mock_agent_cls,
        ):
            state = await build_guardian_state(
                session_id="current",
                user_message="What should Seraph improve next?",
            )
            create_agent(guardian_state=state)

        instructions = mock_agent_cls.call_args.kwargs["instructions"]
        return {
            "overall_confidence": state.confidence.overall,
            "observer_confidence": state.confidence.observer,
            "world_model_confidence": state.confidence.world_model,
            "observer_salience": state.observer_context.salience_level,
            "observer_interruption_cost": state.observer_context.interruption_cost,
            "world_model_focus": state.world_model.current_focus,
            "world_model_alignment": state.world_model.focus_alignment,
            "world_model_memory_signals": len(state.world_model.memory_signals),
            "memory_confidence": state.confidence.memory,
            "current_session_confidence": state.confidence.current_session,
            "recent_sessions_confidence": state.confidence.recent_sessions,
            "goal_summary": state.active_goals_summary,
            "recent_sessions_contains_title": "Prior roadmap" in state.recent_sessions_summary,
            "current_history_mentions_guardian_state": "Build explicit guardian state." in state.current_session_history,
            "instructions_include_guardian_state": "--- GUARDIAN STATE ---" in instructions,
            "instructions_include_recent_sessions": "Recent sessions:" in instructions,
        }


async def _eval_guardian_world_model_behavior() -> dict[str, Any]:
    async with _patched_async_db(
        "src.agent.session.get_session",
        "src.guardian.feedback.get_session",
    ):
        await session_manager.get_or_create("current")
        await session_manager.add_message("current", "user", "What needs attention today?")
        await session_manager.add_message("current", "assistant", "Protect meeting prep and ship the brief.")
        await session_manager.get_or_create("prior")
        await session_manager.update_title("prior", "Investor follow-up")
        await session_manager.add_message("prior", "assistant", "Close the investor loop before tomorrow.")

        ctx = _make_context(
            active_goals_summary="Prepare investor brief",
            active_window="Arc",
            screen_context="Reviewing investor meeting notes",
            upcoming_events=[{"summary": "Investor sync", "start": "2026-03-18T14:00:00Z"}],
            data_quality="good",
            observer_confidence="grounded",
            salience_level="high",
            salience_reason="aligned_work_activity",
            interruption_cost="high",
            attention_budget_remaining=1,
        )

        with (
            patch("src.observer.manager.context_manager.get_context", return_value=ctx),
            patch("src.memory.soul.read_soul", return_value="# Soul\n\n## Identity\nBuilder"),
            patch(
                "src.memory.vector_store.search_with_status",
                return_value=(
                    [
                        {"category": "goal", "text": "Prepare investor brief"},
                        {"category": "pattern", "text": "Protect prep time during meeting blocks"},
                    ],
                    False,
                ),
            ),
            patch(
                "src.audit.repository.audit_repository.list_events",
                return_value=[
                    {
                        "event_type": "tool_result",
                        "tool_name": "workflow_investor_brief",
                        "details": {
                            "workflow_name": "investor-brief",
                            "continued_error_steps": ["write_file"],
                        },
                    }
                ],
            ),
            patch(
                "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
                return_value=["Investor brief", "Fundraising follow-up"],
            ),
            patch(
                "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
                return_value="- advisory delivered, feedback=not_helpful: Too many nudges during prep.",
            ),
            patch("src.agent.factory.get_model", return_value=MagicMock()),
            patch("src.agent.factory.ToolCallingAgent") as mock_agent_cls,
            patch("src.agent.strategist.LiteLLMModel", return_value=MagicMock()),
        ):
            state = await build_guardian_state(
                session_id="current",
                user_message="What needs attention today?",
            )
            create_agent(guardian_state=state)
            strategist_agent = create_strategist_agent(guardian_state=state)

        instructions = mock_agent_cls.call_args.kwargs["instructions"]
        return {
            "world_model_confidence": state.confidence.world_model,
            "current_focus": state.world_model.current_focus,
            "focus_alignment": state.world_model.focus_alignment,
            "intervention_receptivity": state.world_model.intervention_receptivity,
            "active_blockers": list(state.world_model.active_blockers),
            "next_up": list(state.world_model.next_up),
            "dominant_thread": state.world_model.dominant_thread,
            "active_commitments_count": len(state.world_model.active_commitments),
            "active_projects_count": len(state.world_model.active_projects),
            "includes_investor_sync": "Investor sync" in state.world_model.active_commitments,
            "includes_investor_project": "Investor brief" in state.world_model.active_projects,
            "includes_memory_signal": any(
                "Prepare investor brief" in item
                for item in state.world_model.memory_signals
            ),
            "includes_attention_pressure": any(
                "Attention budget is nearly exhausted" in item
                for item in state.world_model.open_loops_or_pressure
            ),
            "includes_feedback_pressure": any(
                "Recent intervention friction" in item
                for item in state.world_model.open_loops_or_pressure
            ),
            "includes_execution_pressure": any(
                "investor-brief degraded" in item
                for item in state.world_model.execution_pressure
            ),
            "agent_instructions_include_world_model": "World model:" in instructions,
            "agent_instructions_include_focus": "Current focus: Prepare investor brief while in Arc" in instructions,
            "agent_instructions_include_projects": "Active projects:" in instructions,
            "agent_instructions_include_active_blockers": "Active blockers:" in instructions,
            "agent_instructions_include_next_up": "Next up:" in instructions,
            "agent_instructions_include_dominant_thread": "Dominant thread:" in instructions,
            "agent_instructions_include_memory_signals": "Memory signals:" in instructions,
            "strategist_instructions_include_receptivity": (
                "Intervention receptivity: low" in strategist_agent.instructions
            ),
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
        patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            AsyncMock(return_value=GuardianLearningSignal.neutral("advisory")),
        ),
        patch("src.observer.delivery._active_channel_adapters", return_value={"websocket"}),
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
    mock_context_manager.is_daemon_connected.return_value = False
    mock_context_manager.decrement_attention_budget = MagicMock()
    # Keep the transport audit deterministic: websocket failures should stay
    # failed here rather than rerouting through the native-notification lane.
    mock_context_manager.is_daemon_connected.return_value = False
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
    mock_insight_queue.peek_all = AsyncMock(
        side_effect=[
            [MagicMock(id="bundle-1", content="Calendar alert: standup")],
            [MagicMock(id="bundle-2", content="Goal reminder: exercise")],
        ]
    )
    mock_insight_queue.delete_many = AsyncMock(return_value=1)
    mock_log_event = AsyncMock()

    with (
        patch("src.observer.manager.context_manager", mock_context_manager),
        patch("src.scheduler.connection_manager.ws_manager", mock_ws_manager),
        patch("src.observer.insight_queue.insight_queue", mock_insight_queue),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            AsyncMock(return_value=GuardianLearningSignal.neutral("advisory")),
        ),
        patch("src.observer.delivery._active_channel_adapters", return_value={"websocket"}),
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
        "bundle_failed_queue_retained": bundle_failed["details"]["queue_retained"],
    }


async def _eval_observer_refresh_behavior() -> dict[str, Any]:
    mgr = ContextManager()
    mgr.update_screen_context("VS Code", "Editing guardian eval contracts")
    mgr._context.user_state = "deep_work"
    mock_log_event = AsyncMock()

    with (
        patch(
            "src.observer.sources.time_source.gather_time",
            return_value={
                "time_of_day": "morning",
                "day_of_week": "Monday",
                "is_working_hours": True,
            },
        ),
        patch(
            "src.observer.sources.calendar_source.gather_calendar",
            new_callable=AsyncMock,
            return_value={
                "upcoming_events": [{
                    "summary": "Design review",
                    "start": (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
                }],
                "current_event": None,
            },
        ),
        patch(
            "src.observer.sources.git_source.gather_git",
            return_value={"recent_git_activity": [{"msg": "ship guardian evals"}]},
        ),
        patch(
            "src.observer.sources.goal_source.gather_goals",
            new_callable=AsyncMock,
            return_value={"active_goals_summary": "Ship guardian behavioral evals"},
        ),
        patch("src.observer.manager.user_state_machine.derive_state", return_value="transitioning"),
        patch.object(mgr, "_deliver_bundle", AsyncMock()) as mock_deliver_bundle,
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        ctx = await mgr.refresh()
        await asyncio.sleep(0)

    succeeded = _find_audit_call(
        mock_log_event,
        event_type="background_task_succeeded",
        tool_name="observer_context_refresh",
    )
    return {
        "new_user_state": ctx.user_state,
        "data_quality": ctx.data_quality,
        "observer_confidence": ctx.observer_confidence,
        "salience_level": ctx.salience_level,
        "salience_reason": ctx.salience_reason,
        "interruption_cost": ctx.interruption_cost,
        "screen_context_preserved": ctx.screen_context == "Editing guardian eval contracts",
        "active_window_preserved": ctx.active_window == "VS Code",
        "goal_summary": ctx.active_goals_summary,
        "upcoming_event_count": len(ctx.upcoming_events),
        "triggered_bundle_delivery": succeeded["details"]["triggered_bundle_delivery"],
        "bundle_task_scheduled": mock_deliver_bundle.call_count == 1,
    }


async def _eval_observer_delivery_decision_behavior() -> dict[str, Any]:
    available_ctx = _make_context(
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
    )
    blocked_ctx = _make_context(
        user_state="deep_work",
        interruption_mode="balanced",
        attention_budget_remaining=3,
    )
    mock_context_manager = MagicMock()
    mock_context_manager.get_context.side_effect = [available_ctx, blocked_ctx]
    mock_context_manager.decrement_attention_budget = MagicMock()
    mock_ws_manager = MagicMock()
    mock_ws_manager.broadcast = AsyncMock(return_value=BroadcastResult(1, 1, 0))
    mock_insight_queue = MagicMock()
    mock_insight_queue.enqueue = AsyncMock()
    mock_log_event = AsyncMock()
    message = WSResponse(
        type="proactive",
        content="Guardian note: wrap the next guardian flow slice.",
        intervention_type="advisory",
        urgency=3,
        reasoning="Guardian flow follow-up",
    )

    with (
        patch("src.observer.manager.context_manager", mock_context_manager),
        patch("src.scheduler.connection_manager.ws_manager", mock_ws_manager),
        patch("src.observer.insight_queue.insight_queue", mock_insight_queue),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            AsyncMock(return_value=GuardianLearningSignal.neutral("advisory")),
        ),
        patch("src.observer.delivery._active_channel_adapters", return_value={"websocket"}),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        delivered = await deliver_or_queue(message)
        queued = await deliver_or_queue(message)

    delivered_event = _find_audit_call(
        mock_log_event,
        event_type="observer_delivery_delivered",
        tool_name="observer_delivery_gate",
    )
    queued_event = _find_audit_call(
        mock_log_event,
        event_type="observer_delivery_queued",
        tool_name="observer_delivery_gate",
    )
    return {
        "delivered_decision": delivered.delivery_decision.value if delivered.delivery_decision else None,
        "delivered_action": delivered.action.value,
        "queued_decision": queued.delivery_decision.value if queued.delivery_decision else None,
        "queued_action": queued.action.value,
        "budget_decremented": mock_context_manager.decrement_attention_budget.call_count == 1,
        "queued_content_matches": mock_insight_queue.enqueue.await_args.kwargs["content"] == message.content,
        "delivered_connections": delivered_event["details"]["delivered_connections"],
        "queued_user_state": queued_event["details"]["user_state"],
    }


async def _eval_native_presence_notification_behavior() -> dict[str, Any]:
    await native_notification_queue.clear()
    available_ctx = _make_context(
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
        last_daemon_post=time.time(),
    )
    mock_context_manager = MagicMock()
    mock_context_manager.get_context.return_value = available_ctx
    mock_context_manager.decrement_attention_budget = MagicMock()
    mock_context_manager.is_daemon_connected.return_value = True
    mock_ws_manager = MagicMock()
    mock_ws_manager.broadcast = AsyncMock(return_value=BroadcastResult(0, 0, 0))
    mock_log_event = AsyncMock()
    message = WSResponse(
        type="proactive",
        content="Guardian fallback through native notifications.",
        intervention_type="alert",
        urgency=5,
        reasoning="Browser is not connected",
    )

    with (
        patch("src.observer.manager.context_manager", mock_context_manager),
        patch("src.api.observer.context_manager", mock_context_manager),
        patch("src.scheduler.connection_manager.ws_manager", mock_ws_manager),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        decision = await deliver_or_queue(message)
        polled = await get_next_native_notification()
        notification = polled["notification"]
        acked = await ack_native_notification(notification["id"])

    delivered_event = _find_audit_call(
        mock_log_event,
        event_type="observer_delivery_delivered",
        tool_name="observer_delivery_gate",
    )
    integration_event = _find_audit_call(
        mock_log_event,
        event_type="integration_succeeded",
        tool_name="observer_daemon:notifications",
    )
    ack_event = _find_audit_call(
        mock_log_event,
        event_type="integration_acked",
        tool_name="observer_daemon:notifications",
    )
    remaining = await native_notification_queue.count()
    await native_notification_queue.clear()

    return {
        "action": decision.action.value,
        "delivery_decision": decision.delivery_decision.value if decision.delivery_decision else None,
        "transport": delivered_event["details"]["transport"],
        "notification_title": notification["title"],
        "notification_body_matches": notification["body"] == message.content,
        "integration_pending_count": integration_event["details"]["pending_count"],
        "acked": acked["acked"],
        "ack_event_matches": ack_event["details"]["notification_id"] == notification["id"],
        "remaining_notifications": remaining,
    }


async def _eval_native_desktop_shell_behavior() -> dict[str, Any]:
    await native_notification_queue.clear()
    mgr = ContextManager()
    mgr.update_screen_context("VS Code — shell.py", "Editing native presence shell state.")
    mgr.update_capture_mode("balanced")
    mock_log_event = AsyncMock()

    with (
        patch("src.api.observer.context_manager", mgr),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        initial_status = await daemon_status()
        queued = await enqueue_test_native_notification()
        queued_status = await daemon_status()
        acked = await ack_native_notification(queued["id"])
        acked_status = await daemon_status()

    queued_event = _find_audit_call(
        mock_log_event,
        event_type="integration_queued",
        tool_name="observer_daemon:notifications",
    )
    ack_event = _find_audit_call(
        mock_log_event,
        event_type="integration_acked",
        tool_name="observer_daemon:notifications",
    )
    await native_notification_queue.clear()

    return {
        "initial_capture_mode": initial_status["capture_mode"],
        "initial_pending_notifications": initial_status["pending_notification_count"],
        "queued_title": queued["title"],
        "queued_pending_notifications": queued_status["pending_notification_count"],
        "queued_outcome": queued_status["last_native_notification_outcome"],
        "acked": acked["acked"],
        "acked_pending_notifications": acked_status["pending_notification_count"],
        "acked_outcome": acked_status["last_native_notification_outcome"],
        "queued_event_source": queued_event["details"]["source"],
        "ack_event_matches": ack_event["details"]["notification_id"] == queued["id"],
    }


async def _eval_cross_surface_notification_controls_behavior() -> dict[str, Any]:
    await native_notification_queue.clear()
    mgr = ContextManager()
    mgr.update_screen_context("Arc — Guardian Cockpit", "Reviewing pending desktop notifications.")
    mock_log_event = AsyncMock()

    with (
        patch("src.api.observer.context_manager", mgr),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        first = await enqueue_test_native_notification()
        second = await enqueue_test_native_notification()
        listed_before = await list_native_notifications()
        dismissed = await dismiss_native_notification(first["id"])
        listed_after_single = await list_native_notifications()
        dismissed_all = await dismiss_all_native_notifications()
        final_status = await daemon_status()

    dismiss_event = _find_audit_call(
        mock_log_event,
        event_type="integration_dismissed",
        tool_name="observer_daemon:notifications",
    )
    dismiss_all_event = _find_audit_call(
        mock_log_event,
        event_type="integration_dismissed_all",
        tool_name="observer_daemon:notifications",
    )
    await native_notification_queue.clear()

    return {
        "listed_before_pending_count": listed_before["pending_count"],
        "listed_before_titles": [item["title"] for item in listed_before["notifications"]],
        "dismissed_single": dismissed["dismissed"],
        "listed_after_single_pending_count": listed_after_single["pending_count"],
        "dismissed_all_count": dismissed_all["dismissed_count"],
        "final_pending_count": final_status["pending_notification_count"],
        "final_outcome": final_status["last_native_notification_outcome"],
        "dismiss_event_source": dismiss_event["details"]["source"],
        "dismiss_all_event_source": dismiss_all_event["details"]["source"],
        "dismiss_all_event_count": dismiss_all_event["details"]["dismissed_count"],
        "second_notification_preserved_until_bulk_clear": (
            len(listed_after_single["notifications"]) == 1
            and listed_after_single["notifications"][0]["id"] == second["id"]
        ),
    }


async def _eval_cross_surface_continuity_behavior() -> dict[str, Any]:
    from src.guardian.feedback import guardian_feedback_repository

    async with _patched_async_db(
        "src.agent.session.get_session",
        "src.guardian.feedback.get_session",
        "src.observer.insight_queue.get_session",
    ):
        await native_notification_queue.clear()
        mgr = ContextManager()
        mgr.update_screen_context("Arc — Guardian Cockpit", "Reviewing continuity across browser and desktop.")
        mgr.update_capture_mode("balanced")
        mgr.record_native_notification(title="Seraph alert", outcome="queued")

        native_intervention = await guardian_feedback_repository.create_intervention(
            session_id="continuity-session",
            message_type="proactive",
            intervention_type="alert",
            urgency=5,
            content="Desktop fallback is active.",
            reasoning="browser_unavailable",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport="native_notification",
        )
        notification = await native_notification_queue.enqueue(
            intervention_id=native_intervention.id,
            title="Seraph alert",
            body="Desktop fallback is active.",
            intervention_type="alert",
            urgency=5,
        )
        await guardian_feedback_repository.update_outcome(
            native_intervention.id,
            latest_outcome="notification_acked",
            transport="native_notification",
            notification_id=notification.id,
        )

        bundle_intervention = await guardian_feedback_repository.create_intervention(
            session_id="continuity-session",
            message_type="proactive",
            intervention_type="advisory",
            urgency=3,
            content="Bundle this until the browser reconnects.",
            reasoning="high_interruption_cost",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="deep_work",
            interruption_mode="focus",
            policy_action="bundle",
            policy_reason="high_interruption_cost",
            delivery_decision="queue",
            latest_outcome="queued",
        )
        await insight_queue.enqueue(
            content="Bundle this until the browser reconnects.",
            intervention_type="advisory",
            urgency=3,
            reasoning="high_interruption_cost",
            intervention_id=bundle_intervention.id,
        )

        with patch("src.api.observer.context_manager", mgr):
            continuity = await get_observer_continuity()

        queued_ids = [item.id for item in await insight_queue.peek_all()]
        if queued_ids:
            await insight_queue.delete_many(queued_ids)
        await native_notification_queue.clear()

    surfaces = {item["continuity_surface"] for item in continuity["recent_interventions"]}
    return {
        "daemon_pending_notifications": continuity["daemon"]["pending_notification_count"],
        "notification_count": len(continuity["notifications"]),
        "notification_intervention_matches": continuity["notifications"][0]["intervention_id"] == native_intervention.id,
        "queued_insight_count": continuity["queued_insight_count"],
        "queued_bundle_matches": continuity["queued_insights"][0]["intervention_id"] == bundle_intervention.id,
        "recent_surfaces": sorted(surfaces),
        "native_surface_present": "native_notification" in surfaces,
        "bundle_surface_present": "bundle_queue" in surfaces,
    }


async def _eval_guardian_feedback_loop() -> dict[str, Any]:
    from src.guardian.feedback import guardian_feedback_repository

    await native_notification_queue.clear()
    async with _patched_async_db(
        "src.agent.session.get_session",
        "src.guardian.feedback.get_session",
        "src.observer.insight_queue.get_session",
    ):
        await session_manager.get_or_create("feedback-current")
        await session_manager.add_message("feedback-current", "user", "How should Seraph intervene better?")
        await session_manager.add_message("feedback-current", "assistant", "Track intervention outcomes explicitly.")
        await session_manager.get_or_create("feedback-prior")
        await session_manager.update_title("feedback-prior", "Guardian feedback retrospective")
        await session_manager.add_message(
            "feedback-prior",
            "assistant",
            "The last proactive reminder landed at a good time.",
        )

        available_ctx = _make_context(
            user_state="available",
            interruption_mode="balanced",
            attention_budget_remaining=3,
            data_quality="good",
            active_goals_summary="Improve guardian intervention quality",
            last_daemon_post=time.time(),
        )
        mock_context_manager = MagicMock()
        mock_context_manager.get_context.return_value = available_ctx
        mock_context_manager.decrement_attention_budget = MagicMock()
        mock_context_manager.is_daemon_connected.return_value = True
        mock_ws_manager = MagicMock()
        mock_ws_manager.broadcast = AsyncMock(return_value=BroadcastResult(0, 0, 0))
        mock_log_event = AsyncMock()
        message = WSResponse(
            type="proactive",
            content="Guardian note: take a short stretch before the next deep-work block.",
            intervention_type="advisory",
            urgency=4,
            reasoning="recent_interruptions",
        )

        with (
            patch("src.observer.manager.context_manager", mock_context_manager),
            patch("src.scheduler.connection_manager.ws_manager", mock_ws_manager),
            patch("src.memory.soul.read_soul", return_value="# Soul\n\n## Identity\nSeraph"),
            patch(
                "src.memory.vector_store.search_with_status",
                return_value=([{"category": "pattern", "text": "Stretch breaks improve focus"}], False),
            ),
            patch("src.agent.factory.get_model", return_value=MagicMock()),
            patch("src.agent.factory.ToolCallingAgent") as mock_agent_cls,
            patch.object(audit_repository, "log_event", mock_log_event),
        ):
            decision = await deliver_or_queue(
                message,
                guardian_confidence="grounded",
                session_id="feedback-current",
            )
            polled = await get_next_native_notification()
            notification = polled["notification"]
            acked = await ack_native_notification(notification["id"])
            feedback = await post_intervention_feedback(
                message.intervention_id or "",
                InterventionFeedbackRequest(
                    feedback_type="helpful",
                    note="Good timing.",
                ),
            )
            state = await build_guardian_state(
                session_id="feedback-current",
                user_message="How should Seraph intervene better?",
            )
            create_agent(guardian_state=state)

        assert notification is not None
        assert message.intervention_id is not None

        delivery_event = _find_audit_call(
            mock_log_event,
            event_type="observer_delivery_delivered",
            tool_name="observer_delivery_gate",
        )
        ack_event = _find_audit_call(
            mock_log_event,
            event_type="integration_acked",
            tool_name="observer_daemon:notifications",
        )
        feedback_event = _find_audit_call(
            mock_log_event,
            event_type="integration_succeeded",
            tool_name="observer_feedback:intervention",
        )
        intervention = await guardian_feedback_repository.get(message.intervention_id)
        assert intervention is not None
        instructions = mock_agent_cls.call_args.kwargs["instructions"]
        remaining = await native_notification_queue.count()

    await native_notification_queue.clear()
    return {
        "action": decision.action.value,
        "delivery_decision": decision.delivery_decision.value if decision.delivery_decision else None,
        "delivery_transport": delivery_event["details"]["transport"],
        "intervention_id_present": bool(message.intervention_id),
        "notification_intervention_matches": notification["intervention_id"] == message.intervention_id,
        "acked": acked["acked"],
        "feedback_recorded": feedback["recorded"],
        "ack_event_matches": ack_event["details"]["intervention_id"] == message.intervention_id,
        "feedback_type": feedback_event["details"]["feedback_type"],
        "latest_outcome": intervention.latest_outcome,
        "stored_feedback_type": intervention.feedback_type,
        "summary_contains_feedback": "feedback=helpful" in state.recent_intervention_feedback,
        "summary_mentions_excerpt": "take a short stretch" in state.recent_intervention_feedback.lower(),
        "prompt_contains_feedback_section": "Recent intervention feedback:" in state.to_prompt_block(),
        "instructions_include_feedback": "Recent intervention feedback:" in instructions,
        "remaining_notifications": remaining,
    }


async def _eval_guardian_outcome_learning() -> dict[str, Any]:
    from src.guardian.feedback import guardian_feedback_repository

    async with _patched_async_db(
        "src.guardian.feedback.get_session",
        "src.observer.insight_queue.get_session",
    ):
        first = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content="Take a stretch break.",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
        )
        await guardian_feedback_repository.record_feedback(first.id, feedback_type="not_helpful")

        second = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content="Another stretch break reminder.",
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
        )
        await guardian_feedback_repository.record_feedback(second.id, feedback_type="not_helpful")

        available_ctx = _make_context(
            user_state="available",
            interruption_mode="balanced",
            attention_budget_remaining=3,
            data_quality="good",
        )
        mock_context_manager = MagicMock()
        mock_context_manager.get_context.return_value = available_ctx
        mock_context_manager.decrement_attention_budget = MagicMock()
        mock_context_manager.is_daemon_connected.return_value = False
        mock_ws_manager = MagicMock()
        mock_ws_manager.broadcast = AsyncMock(return_value=BroadcastResult(1, 1, 0))
        mock_insight_queue = MagicMock()
        mock_insight_queue.enqueue = AsyncMock()
        mock_log_event = AsyncMock()

        with (
            patch("src.observer.manager.context_manager", mock_context_manager),
            patch("src.scheduler.connection_manager.ws_manager", mock_ws_manager),
            patch("src.observer.insight_queue.insight_queue", mock_insight_queue),
            patch.object(audit_repository, "log_event", mock_log_event),
        ):
            decision = await deliver_or_queue(
                WSResponse(
                    type="proactive",
                    content="Try the same kind of nudge again.",
                    intervention_type="advisory",
                    urgency=3,
                    reasoning="available_capacity",
                ),
                guardian_confidence="grounded",
            )

        queued_event = _find_audit_call(
            mock_log_event,
            event_type="observer_delivery_queued",
            tool_name="observer_delivery_gate",
        )

        for content, feedback_type in (
            ("This direct nudge landed well.", "helpful"),
            ("Another high-signal nudge landed well.", "helpful"),
            ("Acked from native delivery.", "acknowledged"),
            ("Acked from native delivery again.", "acknowledged"),
        ):
            intervention = await guardian_feedback_repository.create_intervention(
                session_id=None,
                message_type="proactive",
                intervention_type="nudge",
                urgency=2,
                content=content,
                reasoning="aligned_work_activity",
                is_scheduled=False,
                guardian_confidence="grounded",
                data_quality="good",
                user_state="available",
                interruption_mode="balanced",
                policy_action="act",
                policy_reason="available_capacity",
                delivery_decision="deliver",
                latest_outcome="delivered",
            )
            await guardian_feedback_repository.record_feedback(
                intervention.id,
                feedback_type=feedback_type,
            )

        positive_ctx = _make_context(
            user_state="available",
            interruption_mode="balanced",
            attention_budget_remaining=2,
            data_quality="good",
            observer_confidence="grounded",
            salience_level="high",
            salience_reason="aligned_work_activity",
            interruption_cost="high",
            last_daemon_post=time.time(),
        )
        positive_context_manager = MagicMock()
        positive_context_manager.get_context.return_value = positive_ctx
        positive_context_manager.decrement_attention_budget = MagicMock()
        positive_context_manager.is_daemon_connected.return_value = True
        positive_ws_manager = MagicMock()
        positive_ws_manager.broadcast = AsyncMock(return_value=BroadcastResult(0, 0, 0))
        positive_log_event = AsyncMock()

        with (
            patch("src.observer.manager.context_manager", positive_context_manager),
            patch("src.scheduler.connection_manager.ws_manager", positive_ws_manager),
            patch.object(audit_repository, "log_event", positive_log_event),
        ):
            positive_decision = await deliver_or_queue(
                WSResponse(
                    type="proactive",
                    content="This is aligned, time-sensitive, and has landed well before.",
                    intervention_type="nudge",
                    urgency=2,
                    reasoning="aligned_work_activity",
                ),
                guardian_confidence="grounded",
            )

        delivered_event = _find_audit_call(
            positive_log_event,
            event_type="observer_delivery_delivered",
            tool_name="observer_delivery_gate",
        )
        positive_signal = await guardian_feedback_repository.get_learning_signal(
            intervention_type="nudge",
            limit=12,
        )
        remaining_notifications = await native_notification_queue.count()
        await native_notification_queue.clear()

    return {
        "action": decision.action.value,
        "reason": decision.reason,
        "queued": mock_insight_queue.enqueue.await_count == 1,
        "broadcast_skipped": mock_ws_manager.broadcast.await_count == 0,
        "learning_bias": queued_event["details"]["learning_bias"],
        "learning_not_helpful_count": queued_event["details"]["learning_not_helpful_count"],
        "positive_action": positive_decision.action.value,
        "positive_reason": positive_decision.reason,
        "positive_transport": delivered_event["details"]["transport"],
        "positive_learning_bias": delivered_event["details"]["learning_bias"],
        "positive_learning_channel_bias": delivered_event["details"]["learning_channel_bias"],
        "positive_helpful_count": positive_signal.helpful_count,
        "positive_acknowledged_count": positive_signal.acknowledged_count,
        "remaining_native_notifications": remaining_notifications,
    }


async def _eval_workflow_approval_threading_behavior() -> dict[str, Any]:
    from src.api.workflows import _list_workflow_runs

    with (
        patch(
            "src.api.workflows.audit_repository.list_events",
            return_value=[
                {
                    "id": "evt-call",
                    "session_id": "thread-1",
                    "event_type": "tool_call",
                    "tool_name": "workflow_web_brief_to_file",
                    "summary": "Calling workflow",
                    "created_at": "2026-03-18T12:01:00Z",
                    "details": {"arguments": {"query": "seraph", "file_path": "notes/brief.md"}},
                },
            ],
        ),
        patch(
            "src.api.workflows.approval_repository.list_pending",
            return_value=[
                {
                    "id": "approval-1",
                    "tool_name": "workflow_web_brief_to_file",
                    "session_id": "thread-1",
                    "fingerprint": "missing-match",
                    "summary": "Approval pending for workflow_web_brief_to_file",
                    "risk_level": "medium",
                    "created_at": "2026-03-18T12:01:10Z",
                    "resume_message": "Continue once the web brief is approved",
                }
            ],
        ),
        patch(
            "src.api.workflows._workflow_runtime_statuses",
            return_value={
                "web-brief-to-file": {
                    "name": "web-brief-to-file",
                    "enabled": True,
                    "availability": "ready",
                    "missing_tools": [],
                    "missing_skills": [],
                    "inputs": {
                        "query": {"type": "string", "description": "Research query"},
                        "file_path": {"type": "string", "description": "Destination path"},
                    },
                }
            },
        ),
        patch(
            "src.api.workflows.workflow_manager.get_tool_metadata",
            return_value={
                "risk_level": "medium",
                "execution_boundaries": ["external_read", "workspace_write"],
                "accepts_secret_refs": False,
            },
        ),
        patch(
            "src.api.workflows.session_manager.list_sessions",
            return_value=[{"id": "thread-1", "title": "Research thread"}],
        ),
    ):
        run = (await _list_workflow_runs(limit=4, session_id="thread-1"))[0]

    return {
        "status": run["status"],
        "thread_label": run["thread_label"],
        "thread_source": run["thread_source"],
        "pending_approval_count": run["pending_approval_count"],
        "pending_resume_message": run["pending_approvals"][0]["resume_message"],
        "timeline_has_approval": any(
            entry["kind"] == "approval_pending" for entry in run["timeline"]
        ),
        "replay_allowed": run["replay_allowed"],
        "replay_block_reason": run["replay_block_reason"],
        "replay_draft_is_none": run["replay_draft"] is None,
        "replay_input_keys": sorted(run["replay_inputs"].keys()),
        "parameter_schema_keys": sorted(run["parameter_schema"].keys()),
        "replay_recommended_actions": [
            action["type"] for action in run["replay_recommended_actions"]
        ],
        "resume_from_step": run["resume_from_step"],
        "resume_checkpoint_label": run["resume_checkpoint_label"],
        "branch_kind": run["branch_kind"],
        "root_run_identity_matches_source": run["root_run_identity"] == run["run_identity"],
        "checkpoint_candidate_kinds": [
            checkpoint["kind"] for checkpoint in run["checkpoint_candidates"]
        ],
        "resume_plan_branch_kind": run["resume_plan"]["branch_kind"],
        "resume_plan_requires_manual_execution": run["resume_plan"]["requires_manual_execution"],
        "thread_continue_message": run["thread_continue_message"],
        "approval_recovery_message": run["approval_recovery_message"],
    }


def _eval_capability_repair_behavior() -> dict[str, Any]:
    from src.api.capabilities import _build_capability_overview

    ctx = _make_context(tool_policy_mode="balanced", mcp_policy_mode="approval", approval_mode="high_risk")
    with (
        patch(
            "src.api.capabilities.get_base_tools_and_active_skills",
            return_value=([types.SimpleNamespace(name="web_search")], ["web-briefing"], "approval"),
        ),
        patch("src.api.capabilities.context_manager.get_context", return_value=ctx),
        patch(
            "src.api.capabilities.skill_manager.list_skills",
            return_value=[
                {
                    "name": "web-briefing",
                    "description": "Web briefing",
                    "requires_tools": ["web_search", "write_file"],
                    "user_invocable": True,
                    "enabled": True,
                    "file_path": "/tmp/web-briefing.md",
                },
            ],
        ),
        patch(
            "src.api.capabilities.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "web-brief-to-file",
                    "tool_name": "workflow_web_brief_to_file",
                    "description": "Research and save",
                    "requires_tools": ["web_search", "write_file"],
                    "requires_skills": ["web-briefing"],
                    "user_invocable": True,
                    "enabled": True,
                    "step_count": 2,
                    "file_path": "/tmp/web-brief-to-file.md",
                    "policy_modes": ["balanced", "full"],
                    "execution_boundaries": ["external_read", "workspace_write"],
                    "risk_level": "medium",
                    "accepts_secret_refs": False,
                    "is_available": False,
                    "missing_tools": ["write_file"],
                    "missing_skills": [],
                },
            ],
        ),
        patch("src.api.capabilities.mcp_manager.get_config", return_value=[]),
        patch("src.api.capabilities.load_catalog_items", return_value={"skills": [], "mcp_servers": []}),
        patch(
            "src.api.capabilities._load_starter_packs",
            return_value=[
                {
                    "name": "research-briefing",
                    "label": "Research briefing",
                    "description": "Research and save a short brief.",
                    "skills": ["web-briefing"],
                    "workflows": ["web-brief-to-file"],
                    "sample_prompt": 'Run workflow "web-brief-to-file" with query="seraph", file_path="notes/brief.md".',
                }
            ],
        ),
    ):
        overview = _build_capability_overview()

    starter_pack = next(item for item in overview["starter_packs"] if item["name"] == "research-briefing")
    workflow = next(item for item in overview["workflows"] if item["name"] == "web-brief-to-file")
    return {
        "starter_pack_availability": starter_pack["availability"],
        "starter_pack_repair_actions": [item["type"] for item in starter_pack["recommended_actions"]],
        "workflow_repair_actions": [item["type"] for item in workflow["recommended_actions"]],
        "recommendation_labels": [item["label"] for item in overview["recommendations"]],
        "runbooks_ready": len(overview["runbooks"]),
    }


async def _eval_threaded_operator_timeline_behavior() -> dict[str, Any]:
    from src.api.operator import get_operator_timeline

    workflow_run = {
        "id": "run-1",
        "workflow_name": "web-brief-to-file",
        "summary": "Workflow failed after saving the partial brief.",
        "status": "failed",
        "started_at": "2026-03-18T12:01:00Z",
        "updated_at": "2026-03-18T12:04:00Z",
        "thread_id": "thread-1",
        "thread_label": "Research thread",
        "replay_draft": 'Run workflow "web-brief-to-file" with query="seraph", file_path="notes/brief.md".',
        "replay_allowed": True,
        "replay_block_reason": None,
        "replay_recommended_actions": [],
        "risk_level": "medium",
        "execution_boundaries": ["external_read", "workspace_write"],
        "pending_approval_count": 0,
        "resume_from_step": "write_step",
        "resume_checkpoint_label": "Retry failed step",
        "retry_from_step_draft": (
            'Run workflow "web-brief-to-file" with query="seraph", file_path="notes/brief.md". '
            'Resume from step "write_step".'
        ),
        "run_identity": "thread-1:workflow_web_brief_to_file:web-brief",
        "run_fingerprint": "web-brief",
        "continued_error_steps": ["write_step"],
        "failed_step_tool": "write_file",
        "checkpoint_step_ids": ["search_step", "write_step"],
        "last_completed_step_id": "write_step",
        "checkpoint_candidates": [
            {
                "step_id": "search_step",
                "label": "search_step (web_search)",
                "kind": "branch_from_checkpoint",
                "status": "succeeded",
            },
            {
                "step_id": "write_step",
                "label": "write_step (write_file)",
                "kind": "retry_failed_step",
                "status": "continued_error",
            },
        ],
        "branch_kind": "retry_failed_step",
        "branch_depth": 0,
        "parent_run_identity": None,
        "root_run_identity": "thread-1:workflow_web_brief_to_file:web-brief",
        "resume_plan": {
            "source_run_identity": "thread-1:workflow_web_brief_to_file:web-brief",
            "parent_run_identity": "thread-1:workflow_web_brief_to_file:web-brief",
            "root_run_identity": "thread-1:workflow_web_brief_to_file:web-brief",
            "branch_kind": "retry_failed_step",
            "resume_from_step": "write_step",
            "resume_checkpoint_label": "write_step (write_file)",
            "requires_manual_execution": True,
        },
        "availability": "ready",
        "thread_continue_message": None,
        "approval_recovery_message": None,
        "step_records": [
            {"id": "search_step", "tool": "web_search", "status": "succeeded"},
            {"id": "write_step", "tool": "write_file", "status": "continued_error"},
        ],
    }
    approval = {
        "id": "approval-1",
        "session_id": "thread-1",
        "thread_id": "thread-1",
        "thread_label": "Research thread",
        "tool_name": "shell_execute",
        "risk_level": "high",
        "summary": "Approval pending for shell_execute",
        "created_at": "2026-03-18T12:03:00Z",
        "resume_message": "Continue after shell approval.",
    }
    notification_created_at = datetime(2026, 3, 18, 12, 6, tzinfo=timezone.utc)
    queued_created_at = datetime(2026, 3, 18, 12, 5, tzinfo=timezone.utc)
    intervention_updated_at = datetime(2026, 3, 18, 12, 5, 30, tzinfo=timezone.utc)

    with (
        patch(
            "src.api.operator.session_manager.list_sessions",
            return_value=[{"id": "thread-1", "title": "Research thread"}],
        ),
        patch(
            "src.api.operator._list_workflow_runs",
            return_value=[workflow_run],
        ),
        patch(
            "src.api.operator.approval_repository.list_pending",
            return_value=[approval],
        ),
        patch(
            "src.api.operator.native_notification_queue.list",
            return_value=[
                types.SimpleNamespace(
                    id="notification-1",
                    title="Seraph alert",
                    body="Pick up the saved brief draft.",
                    session_id="thread-1",
                    resume_message="Continue from native notification.",
                    created_at=notification_created_at,
                    intervention_type="advisory",
                    urgency=3,
                )
            ],
        ),
        patch(
            "src.api.operator.insight_queue.peek_all",
            return_value=[
                types.SimpleNamespace(
                    id="queued-1",
                    intervention_id="intervention-1",
                    intervention_type="advisory",
                    content="Bundle the research notes for later.",
                    urgency=2,
                    reasoning="available_capacity",
                    created_at=queued_created_at,
                )
            ],
        ),
        patch(
            "src.api.operator.guardian_feedback_repository.list_recent",
            return_value=[
                types.SimpleNamespace(
                    id="intervention-1",
                    session_id="thread-1",
                    intervention_type="advisory",
                    content_excerpt="Bundle the research notes for later.",
                    latest_outcome="acked",
                    updated_at=intervention_updated_at,
                    transport="native_notification",
                    policy_action="act",
                    policy_reason="learned_direct_delivery",
                    feedback_type="helpful",
                )
            ],
        ),
        patch(
            "src.api.operator.audit_repository.list_events",
            return_value=[
                {
                    "id": "audit-1",
                    "session_id": "thread-1",
                    "event_type": "tool_failed",
                    "tool_name": "write_file",
                    "risk_level": "medium",
                    "summary": "write_file failed for notes/brief.md",
                    "created_at": "2026-03-18T12:02:00Z",
                }
            ],
        ),
    ):
        payload = await get_operator_timeline(limit=10, session_id="thread-1")

    items = payload["items"]
    workflow_item = next(item for item in items if item["kind"] == "workflow_run")
    approval_item = next(item for item in items if item["kind"] == "approval")
    notification_item = next(item for item in items if item["kind"] == "notification")
    queued_item = next(item for item in items if item["kind"] == "queued_insight")
    intervention_item = next(item for item in items if item["kind"] == "intervention")
    audit_item = next(item for item in items if item["kind"] == "audit")

    return {
        "item_kinds": [item["kind"] for item in items],
        "latest_kind": items[0]["kind"],
        "workflow_thread_id": workflow_item["thread_id"],
        "workflow_continue_message_matches_retry_plan": (
            workflow_item["continue_message"] == workflow_run["retry_from_step_draft"]
        ),
        "workflow_replay_allowed": workflow_item["replay_allowed"],
        "workflow_resume_from_step": workflow_item["metadata"]["resume_from_step"],
        "workflow_resume_checkpoint_label": workflow_item["metadata"]["resume_checkpoint_label"],
        "workflow_run_identity": workflow_item["metadata"]["run_identity"],
        "workflow_branch_kind": workflow_item["metadata"]["branch_kind"],
        "workflow_resume_plan_kind": workflow_item["metadata"]["resume_plan"]["branch_kind"],
        "workflow_failed_step_tool": workflow_item["metadata"]["failed_step_tool"],
        "workflow_checkpoint_candidate_count": len(workflow_item["metadata"]["checkpoint_candidates"]),
        "approval_thread_matches": approval_item["thread_id"] == "thread-1",
        "approval_continue_message": approval_item["continue_message"],
        "notification_thread_matches": notification_item["thread_id"] == "thread-1",
        "notification_continue_message": notification_item["continue_message"],
        "queued_thread_matches": queued_item["thread_id"] == "thread-1",
        "queued_continue_message": queued_item["continue_message"],
        "intervention_source": intervention_item["source"],
        "audit_thread_label": audit_item["thread_label"],
    }


def _eval_capability_preflight_behavior() -> dict[str, Any]:
    from src.api.capabilities import _build_capability_overview, _capability_preflight_payload

    ctx = _make_context(tool_policy_mode="balanced", mcp_policy_mode="approval", approval_mode="high_risk")
    with (
        patch(
            "src.api.capabilities.get_base_tools_and_active_skills",
            return_value=([types.SimpleNamespace(name="web_search")], ["web-briefing"], "approval"),
        ),
        patch("src.api.capabilities.context_manager.get_context", return_value=ctx),
        patch(
            "src.api.capabilities.skill_manager.list_skills",
            return_value=[
                {
                    "name": "web-briefing",
                    "description": "Web briefing",
                    "requires_tools": ["web_search", "write_file"],
                    "user_invocable": True,
                    "enabled": True,
                    "file_path": "/tmp/web-briefing.md",
                },
            ],
        ),
        patch(
            "src.api.capabilities.workflow_manager.list_workflows",
            return_value=[
                {
                    "name": "web-brief-to-file",
                    "tool_name": "workflow_web_brief_to_file",
                    "description": "Research and save",
                    "inputs": {
                        "query": {"type": "string", "description": "Research query"},
                        "file_path": {"type": "string", "description": "Destination path"},
                    },
                    "requires_tools": ["web_search", "write_file"],
                    "requires_skills": ["web-briefing"],
                    "user_invocable": True,
                    "enabled": True,
                    "step_count": 2,
                    "file_path": "/tmp/web-brief-to-file.md",
                    "policy_modes": ["balanced", "full"],
                    "execution_boundaries": ["external_read", "workspace_write"],
                    "risk_level": "medium",
                    "accepts_secret_refs": False,
                    "is_available": False,
                    "missing_tools": ["write_file"],
                    "missing_skills": [],
                },
            ],
        ),
        patch("src.api.capabilities.mcp_manager.get_config", return_value=[]),
        patch("src.api.capabilities.load_catalog_items", return_value={"skills": [], "mcp_servers": []}),
        patch(
            "src.api.capabilities._load_starter_packs",
            return_value=[
                {
                    "name": "research-briefing",
                    "label": "Research briefing",
                    "description": "Research and save a short brief.",
                    "skills": ["web-briefing"],
                    "workflows": ["web-brief-to-file"],
                    "sample_prompt": 'Run workflow "web-brief-to-file" with query="seraph", file_path="notes/brief.md".',
                }
            ],
        ),
    ):
        overview = _build_capability_overview()

    workflow_preflight = _capability_preflight_payload(
        overview=overview,
        target_type="workflow",
        name="web-brief-to-file",
    )
    starter_pack_preflight = _capability_preflight_payload(
        overview=overview,
        target_type="starter_pack",
        name="research-briefing",
    )
    runbook_preflight = _capability_preflight_payload(
        overview={
            "workflows": [],
            "starter_packs": [],
            "runbooks": [
                {
                    "id": "workflow:web-brief-to-file",
                    "label": "Run web-brief-to-file",
                    "description": "Research and save",
                    "availability": "blocked",
                    "command": 'Run workflow "web-brief-to-file" with query="<query>", file_path="notes/output.md".',
                    "parameter_schema": {
                        "query": {"type": "string"},
                        "file_path": {"type": "string"},
                    },
                    "risk_level": "medium",
                    "execution_boundaries": ["external_read", "workspace_write"],
                    "recommended_actions": [
                        {"type": "set_tool_policy", "label": "Allow write_file", "mode": "full"}
                    ],
                    "blocking_reasons": ["missing tool: write_file"],
                }
            ],
        },
        target_type="runbook",
        name="workflow:web-brief-to-file",
    )

    return {
        "workflow_ready": workflow_preflight["ready"],
        "workflow_can_autorepair": workflow_preflight["can_autorepair"],
        "workflow_blocking_reasons": workflow_preflight["blocking_reasons"],
        "workflow_parameter_schema_keys": sorted(workflow_preflight["parameter_schema"].keys()),
        "workflow_recommended_action_types": [
            action["type"] for action in workflow_preflight["recommended_actions"]
        ],
        "workflow_autorepair_action_types": [
            action["type"] for action in workflow_preflight["autorepair_actions"]
        ],
        "starter_pack_can_autorepair": starter_pack_preflight["can_autorepair"],
        "starter_pack_blocking_reasons": starter_pack_preflight["blocking_reasons"],
        "starter_pack_command_present": starter_pack_preflight["command"] is not None,
        "starter_pack_autorepair_action_types": [
            action["type"] for action in starter_pack_preflight["autorepair_actions"]
        ],
        "runbook_ready": runbook_preflight["ready"],
        "runbook_can_autorepair": runbook_preflight["can_autorepair"],
        "runbook_parameter_schema_keys": sorted(runbook_preflight["parameter_schema"].keys()),
        "runbook_risk_level": runbook_preflight["risk_level"],
        "runbook_execution_boundaries": runbook_preflight["execution_boundaries"],
        "runbook_blocking_reasons": runbook_preflight["blocking_reasons"],
        "runbook_autorepair_action_types": [
            action["type"] for action in runbook_preflight["autorepair_actions"]
        ],
    }


async def _eval_activity_ledger_attribution_behavior() -> dict[str, Any]:
    from src.api.activity import get_activity_ledger

    now = datetime.now(timezone.utc)

    def _offset(minutes: int = 0, seconds: int = 0, *, zulu: bool = True) -> str:
        value = (now - timedelta(minutes=minutes, seconds=seconds)).isoformat()
        return value.replace("+00:00", "Z") if zulu else value

    with (
        patch("src.api.activity._list_workflow_runs", AsyncMock(return_value=[])),
        patch("src.api.activity.approval_repository.list_pending", AsyncMock(return_value=[])),
        patch("src.api.activity.native_notification_queue.list", AsyncMock(return_value=[])),
        patch("src.api.activity.insight_queue.peek_all", AsyncMock(return_value=[])),
        patch("src.api.activity.guardian_feedback_repository.list_recent", AsyncMock(return_value=[])),
        patch(
            "src.api.activity.audit_repository.list_events",
            AsyncMock(
                return_value=[
                    {
                        "id": "audit-routing-chat-1",
                        "event_type": "llm_routing_decision",
                        "tool_name": "llm_runtime",
                        "summary": "Initial routing candidate",
                        "created_at": _offset(minutes=7),
                        "session_id": "session-1",
                        "details": {
                            "request_id": "agent-ws:session-1:chat",
                            "runtime_path": "session_runtime",
                            "selected_source": "primary",
                            "max_budget_class": "low",
                        },
                    },
                    {
                        "id": "audit-routing-chat-2",
                        "event_type": "llm_routing_decision",
                        "tool_name": "llm_runtime",
                        "summary": "Selected claude-sonnet-4 for websocket chat",
                        "created_at": _offset(minutes=6),
                        "session_id": "session-1",
                        "details": {
                            "request_id": "agent-ws:session-1:chat",
                            "runtime_path": "chat_agent",
                            "selected_source": "primary",
                            "max_budget_class": "medium",
                        },
                    },
                    {
                        "id": "audit-routing-browser-1",
                        "event_type": "llm_routing_decision",
                        "tool_name": "llm_runtime",
                        "summary": "Selected grok-4.1-fast for browser tool",
                        "created_at": _offset(minutes=5),
                        "session_id": "session-1",
                        "details": {
                            "request_id": "agent-ws:session-1:browser",
                            "runtime_path": "browser_agent",
                            "selected_source": "browser_provider",
                            "max_budget_class": "high",
                        },
                    },
                ]
            ),
        ),
        patch(
            "src.api.activity.list_recent_llm_calls",
            return_value=[
                {
                    "timestamp": _offset(minutes=6, seconds=10, zulu=False),
                    "status": "success",
                    "model": "openrouter/anthropic/claude-sonnet-4",
                    "provider": "openrouter",
                    "tokens": {"input": 500, "output": 150, "total": 650},
                    "cost_usd": 0.01,
                    "latency_ms": 610.0,
                    "session_id": "session-1",
                    "request_id": "agent-ws:session-1:chat",
                    "actor": "user_request",
                    "source": "websocket_chat",
                },
                {
                    "timestamp": _offset(minutes=5, seconds=10, zulu=False),
                    "status": "success",
                    "model": "openrouter/x-ai/grok-4.1-fast",
                    "provider": "openrouter",
                    "tokens": {"input": 250, "output": 60, "total": 310},
                    "cost_usd": 0.025,
                    "latency_ms": 880.0,
                    "session_id": "session-1",
                    "request_id": "agent-ws:session-1:browser",
                    "actor": "user_request",
                    "source": "websocket_chat",
                },
                {
                    "timestamp": _offset(minutes=4, seconds=10, zulu=False),
                    "status": "success",
                    "model": "openai/gpt-4.1-mini",
                    "provider": "openai",
                    "tokens": {"input": 120, "output": 30, "total": 150},
                    "cost_usd": 0.0042,
                    "latency_ms": 420.0,
                    "session_id": "session-1",
                    "request_id": "agent-ws:session-1:unknown",
                    "actor": "user_request",
                    "source": "websocket_chat",
                },
            ],
        ),
        patch(
            "src.api.activity.session_manager.list_sessions",
            AsyncMock(return_value=[{"id": "session-1", "title": "Research thread"}]),
        ),
    ):
        payload = await get_activity_ledger(limit=20, session_id="session-1", window_hours=24)

    llm_items = {
        item["metadata"]["request_id"]: item["metadata"]
        for item in payload["items"]
        if item["kind"] == "llm_call"
    }

    return {
        "runtime_path_bucket_keys": [
            bucket["key"] for bucket in payload["summary"]["llm_cost_by_runtime_path"]
        ],
        "capability_family_bucket_keys": [
            bucket["key"] for bucket in payload["summary"]["llm_cost_by_capability_family"]
        ],
        "chat_runtime_path": llm_items["agent-ws:session-1:chat"]["runtime_path"],
        "chat_budget_class": llm_items["agent-ws:session-1:chat"]["max_budget_class"],
        "browser_selected_source": llm_items["agent-ws:session-1:browser"]["selected_source"],
        "unattributed_family": llm_items["agent-ws:session-1:unknown"]["capability_family"],
    }


def _eval_imported_capability_surface_behavior() -> dict[str, Any]:
    from src.api.catalog import load_catalog_items
    from src.extensions.lifecycle import list_extensions

    catalog_items = load_catalog_items()
    extension_packages = (
        catalog_items.get("extension_packages")
        if isinstance(catalog_items, dict)
        else []
    )
    family_counts: dict[str, int] = {}
    for package in extension_packages if isinstance(extension_packages, list) else []:
        if not isinstance(package, dict):
            continue
        contribution_types = package.get("contribution_types")
        if not isinstance(contribution_types, list):
            continue
        for contribution_type in contribution_types:
            if isinstance(contribution_type, str) and contribution_type.strip():
                family_counts[contribution_type] = family_counts.get(contribution_type, 0) + 1

    payload = list_extensions()
    extensions = payload.get("extensions", []) if isinstance(payload, dict) else []
    installed_contribution_types = sorted({
        str(contribution.get("type"))
        for extension in extensions
        if isinstance(extension, dict)
        for contribution in extension.get("contributions", [])
        if isinstance(contribution, dict) and isinstance(contribution.get("type"), str)
    })

    return {
        "catalog_extension_package_count": (
            len(extension_packages) if isinstance(extension_packages, list) else 0
        ),
        "catalog_families": sorted(family_counts),
        "browser_provider_pack_count": family_counts.get("browser_providers", 0),
        "messaging_connector_pack_count": family_counts.get("messaging_connectors", 0),
        "automation_trigger_pack_count": family_counts.get("automation_triggers", 0),
        "node_adapter_pack_count": family_counts.get("node_adapters", 0),
        "canvas_output_pack_count": family_counts.get("canvas_outputs", 0),
        "workflow_runtime_pack_count": family_counts.get("workflow_runtimes", 0),
        "extension_payload_has_governance": all(
            isinstance(extension, dict)
            and "permission_summary" in extension
            and "approval_profile" in extension
            and "connector_summary" in extension
            for extension in extensions
        ),
        "installed_extension_contribution_types": installed_contribution_types,
        "installed_extension_statuses": sorted({
            str(extension.get("status") or "")
            for extension in extensions
            if isinstance(extension, dict)
        }),
    }


async def _eval_guardian_learning_policy_v2_behavior() -> dict[str, Any]:
    from src.guardian.feedback import guardian_feedback_repository

    async with _patched_async_db("src.guardian.feedback.get_session"):
        for feedback_type, user_state, transport in (
            ("not_helpful", "deep_work", "websocket"),
            ("not_helpful", "in_meeting", "websocket"),
            ("helpful", "away", "native_notification"),
            ("acknowledged", "deep_work", "native_notification"),
        ):
            intervention = await guardian_feedback_repository.create_intervention(
                session_id=None,
                message_type="proactive",
                intervention_type="advisory",
                urgency=3,
                content="Respect the current focus block.",
                reasoning="available_capacity",
                is_scheduled=False,
                guardian_confidence="grounded",
                data_quality="good",
                user_state=user_state,
                interruption_mode="focus" if user_state != "away" else "balanced",
                policy_action="act",
                policy_reason="available_capacity",
                delivery_decision="deliver",
                latest_outcome="delivered",
                transport=transport,
            )
            await guardian_feedback_repository.record_feedback(intervention.id, feedback_type=feedback_type)

        signal = await guardian_feedback_repository.get_learning_signal(intervention_type="advisory")
        blocked_decision = decide_intervention(
            message_type="proactive",
            intervention_type="advisory",
            content="This can wait until the user is out of deep work.",
            urgency=3,
            user_state="deep_work",
            interruption_mode="balanced",
            attention_budget_remaining=2,
            guardian_confidence="grounded",
            observer_confidence="grounded",
            salience_level="medium",
            salience_reason="active_goals",
            interruption_cost="medium",
            learning_timing_bias=signal.timing_bias,
            learning_blocked_state_bias=signal.blocked_state_bias,
        )
        available_decision = decide_intervention(
            message_type="proactive",
            intervention_type="advisory",
            content="Now is a good time for this nudge.",
            urgency=2,
            user_state="available",
            interruption_mode="balanced",
            attention_budget_remaining=2,
            guardian_confidence="grounded",
            observer_confidence="grounded",
            salience_level="medium",
            salience_reason="active_goals",
            interruption_cost="medium",
            learning_timing_bias="prefer_available_windows",
        )

    return {
        "timing_bias": signal.timing_bias,
        "blocked_state_bias": signal.blocked_state_bias,
        "blocked_action": blocked_decision.action.value,
        "blocked_reason": blocked_decision.reason,
        "available_action": available_decision.action.value,
        "available_reason": available_decision.reason,
    }


def _eval_intervention_policy_behavior() -> dict[str, Any]:
    act = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Act now",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=2,
    )
    bundle = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Bundle later",
        urgency=3,
        user_state="deep_work",
        interruption_mode="balanced",
        attention_budget_remaining=2,
    )
    defer = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Too uncertain",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=2,
        guardian_confidence="partial",
    )
    request_approval = decide_intervention(
        message_type="proactive",
        intervention_type="alert",
        content="Need confirmation",
        urgency=4,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=2,
        requires_approval=True,
    )
    stay_silent = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="",
        urgency=2,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=2,
    )
    high_interrupt = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Interrupt later",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=1,
        observer_confidence="grounded",
        salience_level="high",
        interruption_cost="high",
    )
    low_salience = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Background nudge",
        urgency=1,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=2,
        observer_confidence="grounded",
        salience_level="low",
        interruption_cost="low",
    )
    learned_bundle = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="The same nudge after negative feedback",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=2,
        recent_feedback_bias="reduce_interruptions",
    )
    return {
        "act_action": act.action.value,
        "act_reason": act.reason,
        "bundle_action": bundle.action.value,
        "bundle_reason": bundle.reason,
        "defer_action": defer.action.value,
        "defer_reason": defer.reason,
        "approval_action": request_approval.action.value,
        "approval_reason": request_approval.reason,
        "silent_action": stay_silent.action.value,
        "silent_reason": stay_silent.reason,
        "high_interrupt_action": high_interrupt.action.value,
        "high_interrupt_reason": high_interrupt.reason,
        "low_salience_action": low_salience.action.value,
        "low_salience_reason": low_salience.reason,
        "learned_bundle_action": learned_bundle.action.value,
        "learned_bundle_reason": learned_bundle.reason,
    }


def _eval_salience_calibration_behavior() -> dict[str, Any]:
    aligned_work = derive_observer_assessment(
        current_event=None,
        upcoming_events=[],
        recent_git_activity=[{"msg": "ship guardian calibration"}],
        active_goals_summary="Ship guardian calibration",
        active_window="VS Code",
        screen_context="Editing guardian policy code",
        data_quality="good",
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
    )
    single_goal = derive_observer_assessment(
        current_event=None,
        upcoming_events=[],
        recent_git_activity=None,
        active_goals_summary="Ship guardian calibration",
        active_window=None,
        screen_context=None,
        data_quality="good",
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
    )
    calibrated_act = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="This work is directly on your active goal.",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=1,
        guardian_confidence="grounded",
        observer_confidence=aligned_work.observer_confidence,
        salience_level=aligned_work.salience_level,
        salience_reason=aligned_work.salience_reason,
        interruption_cost="high",
    )
    focus_mode_bundle = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="This work is directly on your active goal.",
        urgency=3,
        user_state="available",
        interruption_mode="focus",
        attention_budget_remaining=1,
        guardian_confidence="grounded",
        observer_confidence=aligned_work.observer_confidence,
        salience_level=aligned_work.salience_level,
        salience_reason=aligned_work.salience_reason,
        interruption_cost="high",
    )
    low_observer_confidence = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="The observer signal is too weak to interrupt on.",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
        observer_confidence="degraded",
        salience_level="high",
        salience_reason="aligned_work_activity",
    )
    degraded_state = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="The observer state is degraded.",
        urgency=3,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
        data_quality="degraded",
    )
    urgent_override = decide_intervention(
        message_type="proactive",
        intervention_type="alert",
        content="Urgent issue.",
        urgency=5,
        user_state="available",
        interruption_mode="focus",
        attention_budget_remaining=0,
        guardian_confidence="partial",
        observer_confidence="degraded",
        salience_level="low",
        salience_reason="background",
        interruption_cost="high",
        data_quality="degraded",
    )
    scheduled_override = decide_intervention(
        message_type="proactive",
        intervention_type="advisory",
        content="Scheduled review.",
        urgency=2,
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
        is_scheduled=True,
        guardian_confidence="partial",
        observer_confidence="degraded",
        data_quality="degraded",
    )
    return {
        "aligned_work_salience_level": aligned_work.salience_level,
        "aligned_work_salience_reason": aligned_work.salience_reason,
        "single_goal_salience_level": single_goal.salience_level,
        "single_goal_salience_reason": single_goal.salience_reason,
        "calibrated_action": calibrated_act.action.value,
        "calibrated_reason": calibrated_act.reason,
        "focus_mode_action": focus_mode_bundle.action.value,
        "focus_mode_reason": focus_mode_bundle.reason,
        "low_observer_confidence_action": low_observer_confidence.action.value,
        "low_observer_confidence_reason": low_observer_confidence.reason,
        "degraded_state_action": degraded_state.action.value,
        "degraded_state_reason": degraded_state.reason,
        "urgent_override_action": urgent_override.action.value,
        "urgent_override_reason": urgent_override.reason,
        "scheduled_override_action": scheduled_override.action.value,
        "scheduled_override_reason": scheduled_override.reason,
    }


async def _eval_observer_delivery_salience_confidence_behavior() -> dict[str, Any]:
    calibrated_ctx = _make_context(
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=1,
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="aligned_work_activity",
        interruption_cost="high",
    )
    degraded_ctx = _make_context(
        user_state="available",
        interruption_mode="balanced",
        attention_budget_remaining=3,
        data_quality="good",
        observer_confidence="degraded",
        salience_level="high",
        salience_reason="aligned_work_activity",
        interruption_cost="high",
    )
    mock_context_manager = MagicMock()
    mock_context_manager.get_context.side_effect = [calibrated_ctx, degraded_ctx]
    mock_context_manager.decrement_attention_budget = MagicMock()
    mock_context_manager.is_daemon_connected.return_value = False
    mock_ws_manager = MagicMock()
    mock_ws_manager.broadcast = AsyncMock(return_value=BroadcastResult(1, 1, 0))
    mock_insight_queue = MagicMock()
    mock_insight_queue.enqueue = AsyncMock()
    mock_log_event = AsyncMock()

    calibrated_message = WSResponse(
        type="proactive",
        content="This active goal work can justify an interruption right now.",
        intervention_type="advisory",
        urgency=3,
        reasoning="Aligned work and grounded observer state",
    )
    degraded_message = WSResponse(
        type="proactive",
        content="The observer signal is degraded, so defer instead of interrupting.",
        intervention_type="advisory",
        urgency=3,
        reasoning="Observer confidence is degraded",
    )

    with (
        patch("src.observer.manager.context_manager", mock_context_manager),
        patch("src.scheduler.connection_manager.ws_manager", mock_ws_manager),
        patch("src.observer.insight_queue.insight_queue", mock_insight_queue),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            AsyncMock(return_value=GuardianLearningSignal.neutral("advisory")),
        ),
        patch("src.observer.delivery._active_channel_adapters", return_value={"websocket"}),
        patch.object(audit_repository, "log_event", mock_log_event),
    ):
        calibrated = await deliver_or_queue(calibrated_message, guardian_confidence="grounded")
        degraded = await deliver_or_queue(degraded_message, guardian_confidence="grounded")

    delivered_event = _find_audit_call(
        mock_log_event,
        event_type="observer_delivery_delivered",
        tool_name="observer_delivery_gate",
    )
    deferred_event = _find_audit_call(
        mock_log_event,
        event_type="observer_delivery_deferred",
        tool_name="observer_delivery_gate",
    )

    return {
        "calibrated_action": calibrated.action.value,
        "calibrated_reason": calibrated.reason,
        "calibrated_delivery_decision": (
            calibrated.delivery_decision.value if calibrated.delivery_decision else None
        ),
        "calibrated_transport": delivered_event["details"]["transport"],
        "calibrated_delivered_connections": delivered_event["details"]["delivered_connections"],
        "calibrated_budget_decremented": mock_context_manager.decrement_attention_budget.call_count == 1,
        "calibrated_observer_confidence": delivered_event["details"]["observer_confidence"],
        "calibrated_salience_reason": delivered_event["details"]["salience_reason"],
        "calibrated_interruption_cost": delivered_event["details"]["interruption_cost"],
        "degraded_action": degraded.action.value,
        "degraded_reason": degraded.reason,
        "degraded_delivery_decision": degraded.delivery_decision.value if degraded.delivery_decision else None,
        "degraded_observer_confidence": deferred_event["details"]["observer_confidence"],
        "degraded_salience_reason": deferred_event["details"]["salience_reason"],
        "degraded_transport_present": "transport" in deferred_event["details"],
        "degraded_broadcast_skipped": mock_ws_manager.broadcast.await_count == 1,
        "degraded_queue_skipped": mock_insight_queue.enqueue.await_count == 0,
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


def _eval_tool_policy_guardrails_behavior() -> dict[str, Any]:
    client, patches, stack = _make_sync_client_with_db()
    policy_context_manager = ContextManager()
    mcp_tool = MagicMock()
    mcp_tool.name = "mcp_tasks"
    mcp_tool.description = "Task MCP"

    try:
        with (
            patch("src.api.settings.context_manager", policy_context_manager),
            patch("src.tools.policy.context_manager", policy_context_manager),
            patch("src.agent.factory.mcp_manager.get_tools", return_value=[mcp_tool]),
        ):
            safe_response = client.put("/api/settings/tool-policy-mode", json={"mode": "safe"})
            safe_tools = {tool["name"]: tool for tool in client.get("/api/tools").json()}

            balanced_response = client.put("/api/settings/tool-policy-mode", json={"mode": "balanced"})
            balanced_tools = {tool["name"]: tool for tool in client.get("/api/tools").json()}

            full_response = client.put("/api/settings/tool-policy-mode", json={"mode": "full"})
            full_tools = {tool["name"]: tool for tool in client.get("/api/tools").json()}

            disabled_response = client.put("/api/settings/mcp-policy-mode", json={"mode": "disabled"})
            disabled_tools = {tool["name"]: tool for tool in client.get("/api/tools").json()}

            approval_response = client.put("/api/settings/mcp-policy-mode", json={"mode": "approval"})
            approval_tools = {tool["name"]: tool for tool in client.get("/api/tools").json()}

        return {
            "safe_status": safe_response.status_code,
            "balanced_status": balanced_response.status_code,
            "full_status": full_response.status_code,
            "mcp_disabled_status": disabled_response.status_code,
            "mcp_approval_status": approval_response.status_code,
            "safe_hides_write_file": "write_file" not in safe_tools,
            "safe_hides_execute_code": "execute_code" not in safe_tools,
            "safe_hides_run_command": "run_command" not in safe_tools,
            "balanced_shows_write_file": "write_file" in balanced_tools,
            "balanced_hides_execute_code": "execute_code" not in balanced_tools,
            "balanced_hides_run_command": "run_command" not in balanced_tools,
            "full_shows_execute_code": "execute_code" in full_tools,
            "full_shows_run_command": "run_command" in full_tools,
            "full_hides_shell_execute_alias": "shell_execute" not in full_tools,
            "write_file_accepts_secret_refs": full_tools["write_file"]["accepts_secret_refs"],
            "mcp_disabled_hides_tool": "mcp_tasks" not in disabled_tools,
            "mcp_approval_shows_tool": "mcp_tasks" in approval_tools,
            "mcp_approval_requires_approval": approval_tools["mcp_tasks"]["requires_approval"],
            "mcp_approval_accepts_secret_refs": approval_tools["mcp_tasks"]["accepts_secret_refs"],
        }
    finally:
        stack.close()
        for item in patches:
            item.stop()


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
        name="rest_chat_behavior",
        category="behavior",
        description="REST chat preserves session continuity, redacts secrets, and records a successful agent run.",
        runner=_eval_rest_chat_behavior,
    ),
    EvalScenario(
        name="rest_chat_approval_contract",
        category="behavior",
        description="REST chat returns the approval-required contract and audit event for high-risk tool requests.",
        runner=_eval_rest_chat_approval_contract,
    ),
    EvalScenario(
        name="rest_chat_timeout_contract",
        category="behavior",
        description="REST chat returns a timeout contract and timed-out audit event instead of hanging indefinitely.",
        runner=_eval_rest_chat_timeout_contract,
    ),
    EvalScenario(
        name="websocket_chat_behavior",
        category="behavior",
        description="WebSocket chat streams step and final messages with monotonic sequencing and a success audit event.",
        runner=_eval_websocket_chat_behavior,
    ),
    EvalScenario(
        name="websocket_chat_approval_contract",
        category="behavior",
        description="WebSocket chat emits the approval-required message contract and matching audit event for high-risk tool requests.",
        runner=_eval_websocket_chat_approval_contract,
    ),
    EvalScenario(
        name="websocket_chat_timeout_contract",
        category="behavior",
        description="WebSocket chat returns a timeout final message and timed-out audit event without a false success record.",
        runner=_eval_websocket_chat_timeout_contract,
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
        name="delegated_tool_workflow_behavior",
        category="behavior",
        description="A delegated WebSocket workflow routes through specialists, executes audited tools, and saves the resulting plan.",
        runner=_eval_delegated_tool_workflow_behavior,
    ),
    EvalScenario(
        name="delegated_tool_workflow_degraded_behavior",
        category="behavior",
        description="A delegated WebSocket workflow degrades after tool failure but still saves a fallback plan and surfaces the failure audit.",
        runner=_eval_delegated_tool_workflow_degraded_behavior,
    ),
    EvalScenario(
        name="workflow_composition_behavior",
        category="behavior",
        description="Workflow composition builds reusable multi-step tools, skill-gates them, executes sequential steps, and exposes them through the workflow_runner specialist.",
        runner=_eval_workflow_composition_behavior,
    ),
    EvalScenario(
        name="workflow_approval_threading_behavior",
        category="behavior",
        description="Workflow timeline entries keep pending approvals bound to the right thread, resume message, and replay guardrail when a run is still waiting for approval.",
        runner=_eval_workflow_approval_threading_behavior,
    ),
    EvalScenario(
        name="threaded_operator_timeline_behavior",
        category="behavior",
        description="The operator timeline keeps workflows, approvals, notifications, queued bundles, interventions, and failures bound to one thread with the right continue and replay metadata.",
        runner=_eval_threaded_operator_timeline_behavior,
    ),
    EvalScenario(
        name="capability_repair_behavior",
        category="behavior",
        description="Capability overview exposes actionable starter-pack and blocked-workflow repair sequences instead of only passive blocked states.",
        runner=_eval_capability_repair_behavior,
    ),
    EvalScenario(
        name="capability_preflight_behavior",
        category="behavior",
        description="Capability preflight exposes blocking reasons, parameter schemas, autorepair hints, and runbook metadata before the operator launches or repairs a capability.",
        runner=_eval_capability_preflight_behavior,
    ),
    EvalScenario(
        name="activity_ledger_attribution_behavior",
        category="observability",
        description="Activity ledger attributes LLM spend to runtime paths and capability families, while keeping missing routing metadata explicitly unattributed.",
        runner=_eval_activity_ledger_attribution_behavior,
    ),
    EvalScenario(
        name="imported_capability_surface_behavior",
        category="behavior",
        description="Imported capability families stay visible through catalog packages and extension lifecycle surfaces, with governance metadata present on installed extensions.",
        runner=_eval_imported_capability_surface_behavior,
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
        name="vault_runtime_audit",
        category="observability",
        description="Vault repository reads and writes record success, missing-secret, and decrypt-failure audit outcomes without exposing secret values.",
        runner=_eval_vault_runtime_audit,
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
        name="provider_policy_capabilities",
        category="runtime",
        description="Runtime-path policy intents can prefer local-first primary routing and capability-matched fallback targets.",
        runner=_eval_provider_policy_capabilities,
    ),
    EvalScenario(
        name="provider_policy_scoring",
        category="runtime",
        description="Runtime-path policy scoring can rank targets by weighted capability value instead of only intent order.",
        runner=_eval_provider_policy_scoring,
    ),
    EvalScenario(
        name="provider_policy_safeguards",
        category="runtime",
        description="Runtime-path safeguards can require specific capabilities and steer away from targets that violate cost or latency guardrails when compliant targets exist.",
        runner=_eval_provider_policy_safeguards,
    ),
    EvalScenario(
        name="provider_routing_decision_audit",
        category="runtime",
        description="Runtime routing writes structured decision records that explain selected and deferred targets.",
        runner=_eval_provider_routing_decision_audit,
    ),
    EvalScenario(
        name="session_bound_llm_trace",
        category="runtime",
        description="Session-bound helper LLM events carry enough context to explain a session incident from one trace.",
        runner=_eval_session_bound_llm_trace,
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
        name="strategist_tick_behavior",
        category="behavior",
        description="Strategist tick delivers the modeled intervention and records the resulting delivery outcome.",
        runner=_eval_strategist_tick_behavior,
    ),
    EvalScenario(
        name="strategist_tick_learning_continuity_behavior",
        category="guardian",
        description="Strategist tick can use learned delivery bias to reroute a high-salience reminder through native notifications, and that intervention shows up in the continuity snapshot.",
        runner=_eval_strategist_tick_learning_continuity_behavior,
    ),
    EvalScenario(
        name="guardian_state_synthesis",
        category="guardian",
        description="Guardian state synthesis unifies observer context, memories, recent sessions, and confidence into one downstream state object.",
        runner=_eval_guardian_state_synthesis,
    ),
    EvalScenario(
        name="guardian_world_model_behavior",
        category="guardian",
        description="Guardian state now carries a first explicit world model for focus, commitments, pressure, and intervention receptivity.",
        runner=_eval_guardian_world_model_behavior,
    ),
    EvalScenario(
        name="observer_refresh_behavior",
        category="behavior",
        description="Observer refresh preserves screen context, rebuilds guardian context, and schedules queued-bundle delivery on unblock transitions.",
        runner=_eval_observer_refresh_behavior,
    ),
    EvalScenario(
        name="observer_delivery_decision_behavior",
        category="behavior",
        description="Proactive delivery delivers while available, queues while blocked, and decrements budget only for delivered guardian nudges.",
        runner=_eval_observer_delivery_decision_behavior,
    ),
    EvalScenario(
        name="native_presence_notification_behavior",
        category="presence",
        description="When browser delivery is unavailable but the daemon is connected, proactive messages reroute through native notifications.",
        runner=_eval_native_presence_notification_behavior,
    ),
    EvalScenario(
        name="native_desktop_shell_behavior",
        category="presence",
        description="Native presence exposes daemon status, pending-notification state, and a safe test desktop-notification path.",
        runner=_eval_native_desktop_shell_behavior,
    ),
    EvalScenario(
        name="cross_surface_notification_controls_behavior",
        category="presence",
        description="Browser-side native notification controls can inspect, dismiss, and bulk-clear desktop notifications without losing continuity state.",
        runner=_eval_cross_surface_notification_controls_behavior,
    ),
    EvalScenario(
        name="cross_surface_continuity_behavior",
        category="presence",
        description="Browser and native continuity surfaces read one composed snapshot for daemon state, pending notifications, queued bundles, and recent interventions.",
        runner=_eval_cross_surface_continuity_behavior,
    ),
    EvalScenario(
        name="intervention_policy_behavior",
        category="guardian",
        description="Intervention policy explicitly distinguishes act, bundle, defer, request_approval, and stay_silent outcomes.",
        runner=_eval_intervention_policy_behavior,
    ),
    EvalScenario(
        name="salience_calibration_behavior",
        category="guardian",
        description="Aligned active-work signals raise salience, and high-salience grounded nudges can cut through high interruption cost outside focus mode.",
        runner=_eval_salience_calibration_behavior,
    ),
    EvalScenario(
        name="observer_delivery_salience_confidence_behavior",
        category="guardian",
        description="Grounded high-salience observer state can still deliver through high interruption cost, while degraded observer confidence defers before transport.",
        runner=_eval_observer_delivery_salience_confidence_behavior,
    ),
    EvalScenario(
        name="guardian_feedback_loop",
        category="guardian",
        description="Proactive interventions persist outcomes and user feedback, and that summary flows back into guardian-state synthesis.",
        runner=_eval_guardian_feedback_loop,
    ),
    EvalScenario(
        name="guardian_outcome_learning",
        category="guardian",
        description="Recent negative feedback on the same intervention type reduces future interruption eagerness and shows up in delivery audit details.",
        runner=_eval_guardian_outcome_learning,
    ),
    EvalScenario(
        name="guardian_learning_policy_v2_behavior",
        category="guardian",
        description="Blocked-state and timing feedback now shape future delivery policy, not just phrasing and cadence guidance.",
        runner=_eval_guardian_learning_policy_v2_behavior,
    ),
    EvalScenario(
        name="daily_briefing_fallback",
        category="proactive",
        description="Daily briefing survives a primary provider failure and still delivers via fallback.",
        runner=_eval_daily_briefing_fallback,
    ),
    EvalScenario(
        name="daily_briefing_delivery_behavior",
        category="behavior",
        description="Daily briefing delivers a scheduled proactive message and records a good-quality success contract.",
        runner=_eval_daily_briefing_delivery_behavior,
    ),
    EvalScenario(
        name="daily_briefing_degraded_memories_audit",
        category="observability",
        description="Daily briefing records degraded memory-input quality when vector recall falls back to an empty baseline.",
        runner=_eval_daily_briefing_degraded_memories_audit,
    ),
    EvalScenario(
        name="activity_digest_degraded_delivery_behavior",
        category="behavior",
        description="Activity digest still delivers a scheduled message when summary inputs degrade, while surfacing the degraded contract.",
        runner=_eval_activity_digest_degraded_delivery_behavior,
    ),
    EvalScenario(
        name="activity_digest_degraded_summary_audit",
        category="observability",
        description="Activity digest records degraded input quality when the daily screen summary is structurally incomplete.",
        runner=_eval_activity_digest_degraded_summary_audit,
    ),
    EvalScenario(
        name="evening_review_degraded_delivery_behavior",
        category="behavior",
        description="Evening review still delivers a scheduled message when helper inputs degrade, while surfacing the degraded contract.",
        runner=_eval_evening_review_degraded_delivery_behavior,
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
        description="All current scheduled completion-based jobs can route through the first-class local runtime profile.",
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
        name="session_consolidation_behavior",
        category="behavior",
        description="Session consolidation stores long-term memories and soul updates from the modeled guardian conversation summary.",
        runner=_eval_session_consolidation_behavior,
    ),
    EvalScenario(
        name="memory_commitment_continuity_behavior",
        category="behavior",
        description="Project-linked commitments are recoverable in guardian-state memory context for active work even when the global structured kind bucket would miss them.",
        runner=_eval_memory_commitment_continuity_behavior,
    ),
    EvalScenario(
        name="memory_collaborator_lookup_behavior",
        category="behavior",
        description="Project-linked collaborator memory is recoverable through guardian state for active work instead of staying as inert text.",
        runner=_eval_memory_collaborator_lookup_behavior,
    ),
    EvalScenario(
        name="bounded_memory_snapshot_behavior",
        category="behavior",
        description="Bounded recall stays session-stable, keeps todo overlay, and remains compact even as later memory changes land.",
        runner=_eval_bounded_memory_snapshot_behavior,
    ),
    EvalScenario(
        name="memory_supersession_filter_behavior",
        category="behavior",
        description="Superseded structured memories stay out of bounded recall so stale project state does not surface as current truth.",
        runner=_eval_memory_supersession_filter_behavior,
    ),
    EvalScenario(
        name="memory_decay_contradiction_cleanup_behavior",
        category="behavior",
        description="Decay maintenance supersedes contradictory memory and keeps stale vector hits out of hybrid and guardian retrieval.",
        runner=_eval_memory_decay_contradiction_cleanup_behavior,
    ),
    EvalScenario(
        name="procedural_memory_adaptation_behavior",
        category="behavior",
        description="Feedback-derived procedural memory refreshes same-session bounded recall and surfaces the learned rule text in guardian context.",
        runner=_eval_procedural_memory_adaptation_behavior,
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
        name="tool_policy_guardrails_behavior",
        category="behavior",
        description="Tool and MCP policy modes expose a graduated public control surface instead of leaking high-risk capability in safer modes.",
        runner=_eval_tool_policy_guardrails_behavior,
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
    _reset_bounded_guardian_snapshot_cache()
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
    finally:
        _reset_bounded_guardian_snapshot_cache()


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
