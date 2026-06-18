"""Hermes-style search across prior session history."""

import asyncio
import concurrent.futures

from smolagents import tool

from src.approval.runtime import get_current_session_id
from src.agent.session import session_manager


def _run(coro):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


@tool
def session_search(query: str, limit: int = 5) -> str:
    """Search prior session threads for relevant titles and message snippets.

    Use this when you need to recall what happened in earlier threads without
    loading a full long-horizon memory retrieval path.

    Args:
        query: The text to search for across prior session titles and messages.
        limit: Maximum number of matching sessions to return.

    Returns:
        A formatted bounded summary of matching prior sessions.
    """
    normalized_query = query.strip()
    if not normalized_query:
        return "Error: session_search requires a non-empty query."

    current_session_id = get_current_session_id()
    results = _run(
        session_manager.search_sessions(
            normalized_query,
            limit=max(1, min(limit, 10)),
            exclude_session_id=current_session_id,
        )
    )
    if not results:
        return "No prior sessions matched that query."

    lines = []
    for index, item in enumerate(results, start=1):
        lines.append(
            f"{index}. {item['title']} (session={item['session_id']}, source={item['source']})"
        )
        lines.append(f"   {item['snippet']}")
    return "\n".join(lines)
