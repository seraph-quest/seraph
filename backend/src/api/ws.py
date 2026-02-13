import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from smolagents import ActionStep, ToolCall, FinalAnswerStep

from config.settings import settings
from src.agent.factory import build_agent
from src.agent.onboarding import create_onboarding_agent
from src.agent.session import session_manager
from src.api.profile import get_or_create_profile, mark_onboarding_complete, reset_onboarding
from src.memory.soul import read_soul
from src.memory.vector_store import search_formatted
from src.models.schemas import WSMessage, WSResponse
from src.scheduler.connection_manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()


_DONE = object()  # sentinel for queue completion


def _format_tool_step(step_name: str, arguments: dict, specialist_names: set[str]) -> str:
    """Format a tool call step for WS display."""
    if step_name in specialist_names:
        task = arguments.get("task", "") if isinstance(arguments, dict) else ""
        return f"Delegating to {step_name}: {task}"
    return f"Calling tool: {step_name}({json.dumps(arguments)})"


def _run_agent_to_queue(agent, message: str, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
    """Run agent with streaming, pushing each step into an asyncio queue."""
    try:
        for step in agent.run(message, stream=True):
            loop.call_soon_threadsafe(queue.put_nowait, step)
    except Exception as exc:
        loop.call_soon_threadsafe(queue.put_nowait, exc)
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, _DONE)


async def _build_agent(session_id: str, message: str):
    """Build the appropriate agent (onboarding vs normal) for this request.

    Returns (agent, is_onboarding, specialist_names).
    """
    profile = await get_or_create_profile()

    if not profile.onboarding_completed:
        return create_onboarding_agent(), True, set()

    history = await session_manager.get_history_text(session_id)
    soul = read_soul()
    memories = await asyncio.to_thread(search_formatted, message)

    agent = build_agent(
        additional_context=history,
        soul_context=soul,
        memory_context=memories,
    )
    specialist_names = (
        set(agent.managed_agents.keys())
        if hasattr(agent, "managed_agents") and agent.managed_agents
        else set()
    )
    return agent, False, specialist_names


@router.websocket("/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket endpoint for streaming chat responses."""
    await websocket.accept()
    ws_manager.connect(websocket)
    _seq = 0

    def _next_seq() -> int:
        nonlocal _seq
        _seq += 1
        return _seq

    # Send welcome message if user hasn't completed onboarding
    try:
        profile = await get_or_create_profile()
        if not profile.onboarding_completed:
            await websocket.send_text(
                WSResponse(
                    type="proactive",
                    content=(
                        "Greetings, traveler! I am Seraph, your guide through these lands. "
                        "Before we embark on our full journey together, let me get to know you a bit. "
                        "I'll ask a few questions to tailor our adventure. "
                        "If you'd prefer to skip ahead, just say the word!"
                    ),
                    intervention_type="advisory",
                    seq=_next_seq(),
                ).model_dump_json()
            )
    except Exception as e:
        logger.warning("Failed to send welcome message: %s", e)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
                ws_msg = WSMessage(**data)
            except (json.JSONDecodeError, Exception) as e:
                await websocket.send_text(
                    WSResponse(type="error", content=f"Invalid message: {e}", seq=_next_seq()).model_dump_json()
                )
                continue

            if ws_msg.type == "ping":
                await websocket.send_text(
                    WSResponse(type="pong", content="pong").model_dump_json()
                )
                continue

            if ws_msg.type == "skip_onboarding":
                await mark_onboarding_complete()
                await websocket.send_text(
                    WSResponse(
                        type="final",
                        content=(
                            "No worries! Onboarding skipped. "
                            "You now have access to all my abilities. How can I help you?"
                        ),
                        seq=_next_seq(),
                    ).model_dump_json()
                )
                continue

            session = await session_manager.get_or_create(ws_msg.session_id)
            await session_manager.add_message(session.id, "user", ws_msg.message)
            try:
                from src.observer.manager import context_manager
                context_manager.update_last_interaction()
            except Exception:
                pass

            agent, is_onboarding, specialist_names = await _build_agent(session.id, ws_msg.message)

            step_num = 0
            final_result = ""

            try:
                queue: asyncio.Queue = asyncio.Queue()
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, _run_agent_to_queue, agent, ws_msg.message, queue, loop)

                async def _drain_queue():
                    nonlocal step_num, final_result
                    while True:
                        step = await queue.get()
                        if step is _DONE:
                            break
                        if isinstance(step, Exception):
                            raise step

                        if isinstance(step, ToolCall):
                            if step.name == "final_answer":
                                continue
                            step_num += 1
                            content = _format_tool_step(step.name, step.arguments, specialist_names)
                            await websocket.send_text(
                                WSResponse(
                                    type="step",
                                    content=content,
                                    session_id=session.id,
                                    step=step_num,
                                    seq=_next_seq(),
                                ).model_dump_json()
                            )

                        elif isinstance(step, ActionStep):
                            if step.observations and not step.is_final_answer:
                                step_num += 1
                                await websocket.send_text(
                                    WSResponse(
                                        type="step",
                                        content=step.observations,
                                        session_id=session.id,
                                        step=step_num,
                                        seq=_next_seq(),
                                    ).model_dump_json()
                                )

                        elif isinstance(step, FinalAnswerStep):
                            final_result = str(step.output)

                await asyncio.wait_for(_drain_queue(), timeout=settings.agent_chat_timeout)

            except asyncio.TimeoutError:
                logger.warning("Agent timed out after %ds for session %s", settings.agent_chat_timeout, session.id)
                final_result = "I'm taking too long on this one. Let me try a simpler approach — could you rephrase or narrow your request?"

            except Exception as e:
                logger.exception("Agent streaming failed")
                await websocket.send_text(
                    WSResponse(
                        type="error",
                        content=f"Agent error: {e}",
                        session_id=session.id,
                        seq=_next_seq(),
                    ).model_dump_json()
                )
                continue

            await session_manager.add_message(session.id, "assistant", final_result)
            await websocket.send_text(
                WSResponse(
                    type="final",
                    content=final_result,
                    session_id=session.id,
                    seq=_next_seq(),
                ).model_dump_json()
            )

            # After a few onboarding exchanges, mark complete
            if is_onboarding:
                msg_count = await session_manager.count_messages(session.id)
                if msg_count >= 6:  # ~3 user messages + 3 agent responses
                    await mark_onboarding_complete()
                    logger.info("Onboarding completed")

            # Trigger memory consolidation in background (only for assistant responses)
            if final_result:
                try:
                    from src.memory.consolidator import consolidate_session
                    from src.utils.background import track_task
                    track_task(consolidate_session(session.id), name=f"consolidate-{session.id[:8]}")
                except Exception:
                    logger.debug("Failed to schedule memory consolidation", exc_info=True)

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
        logger.info("WebSocket client disconnected")
