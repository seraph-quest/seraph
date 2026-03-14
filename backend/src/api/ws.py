import asyncio
import contextvars
import json
import logging
from time import perf_counter

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from smolagents import ActionStep, ToolCall, FinalAnswerStep

from config.settings import settings
from src.approval.exceptions import ApprovalRequired
from src.approval.repository import approval_repository
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.agent.factory import build_agent
from src.agent.onboarding import create_onboarding_agent
from src.agent.session import session_manager
from src.audit.formatting import format_tool_call_summary
from src.audit.runtime import log_agent_run_event
from src.audit.repository import audit_repository
from src.api.profile import get_or_create_profile, mark_onboarding_complete, reset_onboarding
from src.memory.soul import read_soul
from src.memory.vector_store import search_formatted
from src.models.schemas import WSMessage, WSResponse
from src.scheduler.connection_manager import ws_manager
from src.tools.policy import get_current_tool_policy_mode
from src.vault.redaction import redact_secrets_in_text
from src.llm_runtime import (
    _finish_request,
    _mark_request_timed_out,
    _register_request,
    reset_current_llm_request_id,
    set_current_llm_request_id,
)

logger = logging.getLogger(__name__)

router = APIRouter()


_DONE = object()  # sentinel for queue completion


def _format_tool_step(step_name: str, arguments: dict, specialist_names: set[str]) -> str:
    """Format a tool call step for WS display."""
    return format_tool_call_summary(step_name, arguments, specialist_names)


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

    from src.observer.manager import context_manager as obs_manager
    observer_context = obs_manager.get_context().to_prompt_block()

    agent = build_agent(
        additional_context=history,
        soul_context=soul,
        memory_context=memories,
        observer_context=observer_context,
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
            if ws_msg.type != "resume_message":
                await session_manager.add_message(session.id, "user", ws_msg.message)
            try:
                from src.observer.manager import context_manager
                context_manager.update_last_interaction()
            except Exception:
                pass

            agent, is_onboarding, specialist_names = await _build_agent(session.id, ws_msg.message)

            step_num = 0
            final_result = ""
            tool_call_count = 0
            started_at = perf_counter()
            run_outcome = "succeeded"

            try:
                queue: asyncio.Queue = asyncio.Queue()
                loop = asyncio.get_running_loop()
                llm_request_id = f"agent-ws:{session.id}:{started_at}"
                _register_request(llm_request_id)
                tokens = set_runtime_context(session.id, context_manager.get_context().approval_mode)
                llm_request_token = set_current_llm_request_id(llm_request_id)
                run_ctx = contextvars.copy_context()
                reset_runtime_context(tokens)
                reset_current_llm_request_id(llm_request_token)
                loop.run_in_executor(None, run_ctx.run, _run_agent_to_queue, agent, ws_msg.message, queue, loop)

                async def _drain_queue():
                    nonlocal step_num, final_result, tool_call_count
                    while True:
                        step = await queue.get()
                        if step is _DONE:
                            break
                        if isinstance(step, Exception):
                            raise step

                        if isinstance(step, ToolCall):
                            if step.name == "final_answer":
                                continue
                            tool_call_count += 1
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
                                safe_observations = await redact_secrets_in_text(step.observations)
                                step_num += 1
                                await websocket.send_text(
                                    WSResponse(
                                        type="step",
                                        content=safe_observations,
                                        session_id=session.id,
                                        step=step_num,
                                        seq=_next_seq(),
                                    ).model_dump_json()
                                )

                        elif isinstance(step, FinalAnswerStep):
                            final_result = await redact_secrets_in_text(str(step.output))

                await asyncio.wait_for(_drain_queue(), timeout=settings.agent_chat_timeout)

            except asyncio.TimeoutError:
                logger.warning("Agent timed out after %ds for session %s", settings.agent_chat_timeout, session.id)
                run_outcome = "timed_out"
                _mark_request_timed_out(llm_request_id)
                await log_agent_run_event(
                    session_id=session.id,
                    transport="websocket",
                    is_onboarding=is_onboarding,
                    outcome="timed_out",
                    policy_mode=get_current_tool_policy_mode(),
                    details={
                        "duration_ms": int((perf_counter() - started_at) * 1000),
                        "message_length": len(ws_msg.message),
                        "step_count": step_num,
                        "tool_call_count": tool_call_count,
                        "timeout_seconds": settings.agent_chat_timeout,
                    },
                )
                final_result = "I'm taking too long on this one. Let me try a simpler approach — could you rephrase or narrow your request?"

            except ApprovalRequired as exc:
                await approval_repository.merge_details(
                    exc.approval_id,
                    {"resume_message": ws_msg.message},
                )
                await audit_repository.log_event(
                    session_id=exc.session_id,
                    actor="agent",
                    event_type="approval_requested",
                    tool_name=exc.tool_name,
                    risk_level=exc.risk_level,
                    policy_mode=get_current_tool_policy_mode(),
                    summary=exc.summary,
                )
                await websocket.send_text(
                    WSResponse(
                        type="approval_required",
                        content=(
                            f"{exc.summary}\n\n"
                            "This is a high-risk action. Approve it in chat to continue automatically."
                        ),
                        session_id=session.id,
                        seq=_next_seq(),
                        approval_id=exc.approval_id,
                        tool_name=exc.tool_name,
                        risk_level=exc.risk_level,
                    ).model_dump_json()
                )
                continue

            except Exception as e:
                logger.exception("Agent streaming failed")
                safe_error = await redact_secrets_in_text(f"Agent error: {e}")
                await log_agent_run_event(
                    session_id=session.id,
                    transport="websocket",
                    is_onboarding=is_onboarding,
                    outcome="failed",
                    policy_mode=get_current_tool_policy_mode(),
                    details={
                        "duration_ms": int((perf_counter() - started_at) * 1000),
                        "message_length": len(ws_msg.message),
                        "step_count": step_num,
                        "tool_call_count": tool_call_count,
                        "error": safe_error,
                    },
                )
                await websocket.send_text(
                    WSResponse(
                        type="error",
                        content=safe_error,
                        session_id=session.id,
                        seq=_next_seq(),
                    ).model_dump_json()
                )
                continue
            finally:
                if "llm_request_id" in locals():
                    _finish_request(llm_request_id)

            await session_manager.add_message(session.id, "assistant", final_result)
            if run_outcome == "succeeded":
                await log_agent_run_event(
                    session_id=session.id,
                    transport="websocket",
                    is_onboarding=is_onboarding,
                    outcome="succeeded",
                    policy_mode=get_current_tool_policy_mode(),
                    details={
                        "duration_ms": int((perf_counter() - started_at) * 1000),
                        "message_length": len(ws_msg.message),
                        "response_length": len(final_result),
                        "step_count": step_num,
                        "tool_call_count": tool_call_count,
                    },
                )
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
