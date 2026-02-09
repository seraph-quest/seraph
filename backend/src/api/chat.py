import asyncio
import logging

from fastapi import APIRouter, HTTPException

import asyncio

from src.agent.factory import create_agent
from src.agent.session import session_manager
from src.memory.soul import read_soul
from src.memory.vector_store import search_formatted
from src.models.schemas import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Send a message and receive an AI response."""
    session = await session_manager.get_or_create(request.session_id)
    await session_manager.add_message(session.id, "user", request.message)

    history = await session_manager.get_history_text(session.id)
    soul = read_soul()
    memories = await asyncio.to_thread(search_formatted, request.message)
    agent = create_agent(
        additional_context=history,
        soul_context=soul,
        memory_context=memories,
    )

    try:
        result = await asyncio.to_thread(agent.run, request.message)
        response_text = str(result.output) if hasattr(result, "output") else str(result)
    except Exception as e:
        logger.exception("Agent execution failed")
        raise HTTPException(status_code=500, detail=f"Agent error: {e}")

    await session_manager.add_message(session.id, "assistant", response_text)

    return ChatResponse(
        response=response_text,
        session_id=session.id,
    )
