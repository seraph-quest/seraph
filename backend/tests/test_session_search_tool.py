import pytest

from src.agent.session import session_manager
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.tools.session_search_tool import session_search


@pytest.mark.asyncio
async def test_session_search_finds_prior_matching_sessions(async_db):
    await session_manager.get_or_create("active")
    await session_manager.get_or_create("prior")
    await session_manager.add_message("prior", "assistant", "Weather planning for Wroclaw")

    tokens = set_runtime_context("active", "off")
    try:
        output = session_search(query="weather")
    finally:
        reset_runtime_context(tokens)

    assert "prior" in output
    assert "Weather planning for Wroclaw" in output
    assert "active" not in output


@pytest.mark.asyncio
async def test_session_search_requires_query(async_db):
    assert session_search(query="   ") == "Error: session_search requires a non-empty query."
