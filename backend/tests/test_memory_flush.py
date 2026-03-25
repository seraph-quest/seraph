import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.session import SessionManager
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.memory.consolidator import ConsolidationResult
from src.db.models import MemoryKind
from src.memory.flush import flush_session_memory
from src.memory.repository import memory_repository
from src.workflows.loader import Workflow, WorkflowStep
from src.workflows.manager import WorkflowTool


async def test_flush_session_memory_deduplicates_unchanged_session(async_db):
    manager = SessionManager()
    await manager.get_or_create("flush-session")
    await manager.add_message("flush-session", "user", "Remember Atlas launch for Friday.")
    await manager.add_message("flush-session", "assistant", "I will keep that commitment in memory.")

    with patch(
        "src.memory.consolidator.consolidate_session",
        AsyncMock(return_value=ConsolidationResult(outcome="succeeded", should_cache_fingerprint=True)),
    ) as mock_consolidate:
        first = await flush_session_memory("flush-session", trigger="post_response", manager=manager)
        second = await flush_session_memory("flush-session", trigger="session_end", manager=manager)
        await manager.add_message("flush-session", "user", "Also keep the investor checklist.")
        third = await flush_session_memory("flush-session", trigger="post_response", manager=manager)

    assert first is True
    assert second is False
    assert third is True
    assert mock_consolidate.await_count == 2
    assert mock_consolidate.await_args_list[0].kwargs["trigger"] == "post_response"
    assert mock_consolidate.await_args_list[1].kwargs["trigger"] == "post_response"


async def test_flush_session_memory_deduplicates_overlapping_triggers(async_db):
    manager = SessionManager()
    await manager.get_or_create("flush-race")
    await manager.add_message("flush-race", "user", "Remember Atlas launch for Friday.")
    await manager.add_message("flush-race", "assistant", "I will keep that commitment in memory.")

    release = asyncio.Event()

    async def _slow_consolidate(*_args, **_kwargs):
        await release.wait()
        return ConsolidationResult(outcome="succeeded", should_cache_fingerprint=True)

    with patch("src.memory.consolidator.consolidate_session", side_effect=_slow_consolidate) as mock_consolidate:
        first_task = asyncio.create_task(
            flush_session_memory("flush-race", trigger="post_response", manager=manager)
        )
        await asyncio.sleep(0)
        second = await flush_session_memory("flush-race", trigger="post_response", manager=manager)
        release.set()
        first = await first_task

    assert first is True
    assert second is False
    assert mock_consolidate.await_count == 1


async def test_flush_session_memory_retries_same_fingerprint_after_partial_failure(async_db):
    manager = SessionManager()
    await manager.get_or_create("flush-retry")
    await manager.add_message("flush-retry", "user", "Remember the Atlas launch deadline.")
    await manager.add_message("flush-retry", "assistant", "I will keep it in memory.")

    with patch(
        "src.memory.consolidator.consolidate_session",
        AsyncMock(
            side_effect=[
                ConsolidationResult(
                    outcome="partially_succeeded",
                    should_cache_fingerprint=False,
                ),
                ConsolidationResult(
                    outcome="succeeded",
                    should_cache_fingerprint=True,
                ),
            ]
        ),
    ) as mock_consolidate:
        first = await flush_session_memory("flush-retry", trigger="post_response", manager=manager)
        second = await flush_session_memory("flush-retry", trigger="session_end", manager=manager)

    assert first is False
    assert second is True
    assert mock_consolidate.await_count == 2
    assert [call.kwargs["trigger"] for call in mock_consolidate.await_args_list] == [
        "post_response",
        "session_end",
    ]


async def test_retry_after_snapshot_partial_does_not_double_strengthen(async_db):
    manager = SessionManager()
    await manager.get_or_create("flush-idempotent")
    await manager.add_message(
        "flush-idempotent",
        "user",
        "Please remember that I prefer concise morning briefings for updates.",
    )
    await manager.add_message(
        "flush-idempotent",
        "assistant",
        "I will keep the morning briefing preference in memory.",
    )

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = json.dumps({
        "memories": [
            {
                "text": "User prefers concise morning briefings.",
                "kind": "communication_preference",
                "summary": "Prefers concise morning briefings",
                "confidence": 0.9,
                "importance": 0.8,
            }
        ],
        "facts": [],
        "patterns": [],
        "goals": [],
        "reflections": [],
        "soul_updates": {},
    })

    with patch(
        "src.memory.consolidator.completion_with_fallback",
        AsyncMock(return_value=mock_resp),
    ), patch(
        "src.memory.consolidator.add_memory",
        return_value="vec-1",
    ) as mock_add_memory, patch(
        "src.memory.consolidator.refresh_bounded_guardian_snapshot",
        AsyncMock(side_effect=[RuntimeError("snapshot down"), None]),
    ):
        first = await flush_session_memory(
            "flush-idempotent",
            trigger="post_response",
            manager=manager,
        )
        second = await flush_session_memory(
            "flush-idempotent",
            trigger="session_end",
            manager=manager,
        )

    memories = await memory_repository.list_memories_by_kinds(
        kinds=(MemoryKind.communication_preference,),
        limit_per_kind=1,
    )

    assert first is False
    assert second is True
    assert memories["communication_preference"][0].reinforcement == pytest.approx(1.0)
    assert mock_add_memory.call_count == 1


async def test_get_history_text_triggers_pre_compaction_flush(async_db):
    manager = SessionManager()
    await manager.get_or_create("compact-session")
    for index in range(12):
        await manager.add_message(
            "compact-session",
            "user" if index % 2 == 0 else "assistant",
            f"Atlas update {index} " * 120,
        )

    with patch("src.agent.session.flush_session_memory", new_callable=AsyncMock) as mock_flush, patch(
        "src.agent.context_window.requires_middle_summary",
        return_value=True,
    ), patch(
        "src.agent.context_window.build_context_window",
        return_value="compact-history",
    ):
        history = await manager.get_history_text("compact-session")

    assert history == "compact-history"
    mock_flush.assert_awaited_once()
    assert mock_flush.await_args.kwargs["trigger"] == "pre_compaction"


async def test_delete_triggers_session_end_flush(async_db):
    manager = SessionManager()
    await manager.get_or_create("delete-session")
    await manager.add_message("delete-session", "user", "Close Atlas session cleanly.")
    await manager.add_message("delete-session", "assistant", "I will flush memory before teardown.")

    with patch("src.agent.session.flush_session_memory", new_callable=AsyncMock) as mock_flush:
        deleted = await manager.delete("delete-session")

    assert deleted is True
    mock_flush.assert_awaited_once()
    assert mock_flush.await_args.kwargs["trigger"] == "session_end"


def test_workflow_completion_triggers_memory_flush():
    workflow = Workflow(
        name="atlas-workflow",
        description="Atlas workflow",
        requires_tools=["echo_tool"],
        inputs={},
        steps=[WorkflowStep(id="echo", tool="echo_tool", arguments={})],
        file_path="atlas-workflow.md",
    )

    class EchoTool:
        name = "echo_tool"
        description = "Echo"
        inputs = {}
        output_type = "string"

        def __call__(self, *args, sanitize_inputs_outputs: bool = False, **kwargs):
            return "Atlas workflow finished."

    tool = WorkflowTool(workflow, {"echo_tool": EchoTool()})
    tokens = set_runtime_context("workflow-session", "high_risk")
    try:
        with patch("src.workflows.manager.flush_session_memory_sync", return_value=True) as mock_flush:
            result = tool()
    finally:
        reset_runtime_context(tokens)

    assert "completed" in result.lower()
    mock_flush.assert_called_once()
    assert mock_flush.call_args.kwargs["session_id"] == "workflow-session"
    assert mock_flush.call_args.kwargs["trigger"] == "workflow_completed"
    assert mock_flush.call_args.kwargs["workflow_name"] == "atlas-workflow"
