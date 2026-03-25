import json
from unittest.mock import patch

import pytest

from src.agent.session import SessionManager
from src.db.models import MemoryEpisodeType
from src.memory.episodes import EpisodeDraft
from src.memory.repository import memory_repository


@pytest.fixture
def sm():
    return SessionManager()


@pytest.mark.asyncio
async def test_create_episode_persists_typed_fields(async_db, sm):
    await sm.get_or_create("sess-1")
    episode = await memory_repository.create_episode(
        episode_type=MemoryEpisodeType.decision,
        summary="Chose to ship the Atlas launch first",
        content="Decision: prioritize Atlas launch before the Hermes admin cleanup.",
        session_id="sess-1",
        source_tool_name="strategist",
        source_role="assistant",
        salience=0.82,
        confidence=0.77,
        metadata={"writer": "test"},
    )

    episodes = await memory_repository.list_episodes(limit=5)

    assert episode.id
    assert len(episodes) == 1
    assert episodes[0].episode_type == MemoryEpisodeType.decision
    assert episodes[0].summary == "Chose to ship the Atlas launch first"
    assert episodes[0].session_id == "sess-1"
    assert episodes[0].source_tool_name == "strategist"
    assert episodes[0].source_role == "assistant"
    assert episodes[0].salience == pytest.approx(0.82)
    assert episodes[0].confidence == pytest.approx(0.77)
    assert episodes[0].metadata_json == '{"writer": "test"}'


@pytest.mark.asyncio
async def test_session_messages_create_conversation_episode(async_db, sm):
    await sm.get_or_create("s1")

    message = await sm.add_message("s1", "user", "Need to remember the Atlas launch budget discussion.")
    episodes = await memory_repository.list_episodes(session_id="s1", limit=5)

    assert len(episodes) == 1
    assert episodes[0].episode_type == MemoryEpisodeType.conversation
    assert episodes[0].source_message_id == message.id
    assert episodes[0].source_role == "user"
    assert "Atlas launch budget discussion" in episodes[0].summary


@pytest.mark.asyncio
async def test_session_step_messages_create_tool_and_workflow_episodes(async_db, sm):
    await sm.get_or_create("s1")

    await sm.add_message("s1", "step", "Searched the repo for session search usage.", tool_used="session_search")
    await sm.add_message(
        "s1",
        "step",
        "Ran the weekly review workflow with fresh parameters.",
        tool_used="workflow_runner",
        metadata_json=json.dumps({"workflow_name": "weekly_review"}),
    )

    episodes = await memory_repository.list_episodes(session_id="s1", limit=5)
    assert [episode.episode_type for episode in episodes] == [
        MemoryEpisodeType.workflow,
        MemoryEpisodeType.tool,
    ]
    assert episodes[0].source_tool_name == "workflow_runner"
    assert episodes[0].metadata_json == '{"source": "session_step", "workflow_name": "weekly_review"}'
    assert episodes[1].source_tool_name == "session_search"


@pytest.mark.asyncio
async def test_list_episodes_filters_by_type(async_db):
    sm = SessionManager()
    await sm.get_or_create("a")
    await sm.get_or_create("b")
    await memory_repository.create_episode(
        episode_type=MemoryEpisodeType.conversation,
        summary="Reviewed Mercury launch risks",
        content="Conversation about Mercury launch risks.",
        session_id="a",
    )
    await memory_repository.create_episode(
        episode_type=MemoryEpisodeType.observer,
        summary="Focus changed to Apollo roadmap",
        content="Observer transition: focus moved to Apollo roadmap.",
        session_id="b",
    )

    filtered = await memory_repository.list_episodes(
        episode_types=(MemoryEpisodeType.observer,),
        limit=5,
    )

    assert [episode.episode_type for episode in filtered] == [MemoryEpisodeType.observer]
    assert filtered[0].session_id == "b"


@pytest.mark.asyncio
async def test_episode_write_failure_does_not_block_message_persistence(async_db, sm):
    await sm.get_or_create("s1")
    bad_draft = EpisodeDraft(
        episode_type=MemoryEpisodeType.conversation,
        summary="bad episode",
        content="bad episode content",
        source_role="user",
    )

    with patch("src.agent.session.build_message_episode", return_value=bad_draft), patch(
        "src.agent.session.MemoryEpisode",
        side_effect=RuntimeError("episode insert exploded"),
    ):
        message = await sm.add_message("s1", "user", "Keep the chat message even if episodic write fails.")

    messages = await sm.get_messages("s1")
    episodes = await memory_repository.list_episodes(session_id="s1", limit=5)

    assert message.id
    assert [item["content"] for item in messages] == [
        "Keep the chat message even if episodic write fails."
    ]
    assert episodes == []


@pytest.mark.asyncio
async def test_session_delete_cleans_up_episodes_linked_only_by_source_message(async_db, sm):
    await sm.get_or_create("s1")
    message = await sm.add_message("s1", "user", "Atlas launch needs a follow-up note.")
    await memory_repository.create_episode(
        episode_type=MemoryEpisodeType.decision,
        summary="Decision tied only to the message",
        content="This episode intentionally omits session_id but references the message.",
        source_message_id=message.id,
    )

    deleted = await sm.delete("s1")

    assert deleted is True
