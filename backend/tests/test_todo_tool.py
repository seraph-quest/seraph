import pytest

from src.agent.session import session_manager
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.tools.todo_tool import todo


@pytest.mark.asyncio
async def test_todo_tool_sets_and_lists_session_todos(async_db):
    await session_manager.get_or_create("s1")
    tokens = set_runtime_context("s1", "off")
    try:
        output = todo(action="set", items="[ ] Clarify missing requirement\n[x] Review prior thread")
        assert "1. [ ] Clarify missing requirement" in output
        assert "2. [x] Review prior thread" in output

        listed = todo(action="list")
        assert "Open: 1 · Completed: 1" in listed
    finally:
        reset_runtime_context(tokens)


@pytest.mark.asyncio
async def test_todo_tool_completes_and_removes_by_index(async_db):
    await session_manager.get_or_create("s1")
    tokens = set_runtime_context("s1", "off")
    try:
        todo(action="set", items="First\nSecond")
        completed = todo(action="complete", item_id="2")
        assert "2. [x] Second" in completed

        removed = todo(action="remove", item_id="1")
        assert "1. [x] Second" in removed
        assert "First" not in removed
    finally:
        reset_runtime_context(tokens)


@pytest.mark.asyncio
async def test_todo_tool_errors_without_session_context(async_db):
    output = todo(action="list")
    assert output == "Error: Todo list is only available inside a conversation session."


@pytest.mark.asyncio
async def test_todo_tool_rejects_invalid_indices(async_db):
    await session_manager.get_or_create("s1")
    tokens = set_runtime_context("s1", "off")
    try:
        todo(action="set", items="First\nSecond")
        assert todo(action="complete", item_id="0") == "Error: Todo '0' was not found."
        assert todo(action="remove", item_id="3") == "Error: Todo '3' was not found."
        listed = todo(action="list")
        assert "1. [ ] First" in listed
        assert "2. [ ] Second" in listed
    finally:
        reset_runtime_context(tokens)


@pytest.mark.asyncio
async def test_todo_tool_treats_null_optional_fields_as_absent(async_db):
    await session_manager.get_or_create("s1")
    tokens = set_runtime_context("s1", "off")
    try:
        assert todo({"action": "add", "items": None}) == "Todo list is empty."
        listed = todo(action="list")
        assert listed == "Todo list is empty."
    finally:
        reset_runtime_context(tokens)
