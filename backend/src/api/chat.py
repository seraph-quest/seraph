import asyncio
import contextvars
import logging

from fastapi import APIRouter, HTTPException

from src.approval.exceptions import ApprovalRequired
from src.approval.runtime import reset_runtime_context, set_runtime_context
from config.settings import settings
from src.agent.factory import build_agent
from src.agent.onboarding import create_onboarding_agent
from src.agent.session import session_manager
from src.audit.repository import audit_repository
from src.api.profile import get_or_create_profile, mark_onboarding_complete
from src.memory.soul import read_soul
from src.memory.vector_store import search_formatted
from src.models.schemas import ChatRequest, ChatResponse
from src.tools.policy import get_current_tool_policy_mode
from src.vault.redaction import redact_secrets_in_text

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Send a message and receive an AI response."""
    session = await session_manager.get_or_create(request.session_id)
    await session_manager.add_message(session.id, "user", request.message)

    # Check onboarding status
    profile = await get_or_create_profile()
    if not profile.onboarding_completed:
        agent = create_onboarding_agent()
    else:
        history = await session_manager.get_history_text(session.id)
        soul = read_soul()
        memories = await asyncio.to_thread(search_formatted, request.message)

        from src.observer.manager import context_manager as obs_manager
        observer_context = obs_manager.get_context().to_prompt_block()

        agent = build_agent(
            additional_context=history,
            soul_context=soul,
            memory_context=memories,
            observer_context=observer_context,
        )

    try:
        from src.observer.manager import context_manager as obs_manager
        tokens = set_runtime_context(session.id, obs_manager.get_context().approval_mode)
        run_ctx = contextvars.copy_context()
        reset_runtime_context(tokens)
        result = await asyncio.wait_for(
            asyncio.to_thread(run_ctx.run, agent.run, request.message),
            timeout=settings.agent_chat_timeout,
        )
        response_text = str(result.output) if hasattr(result, "output") else str(result)
        response_text = await redact_secrets_in_text(response_text)
    except ApprovalRequired as exc:
        await audit_repository.log_event(
            session_id=exc.session_id,
            actor="agent",
            event_type="approval_requested",
            tool_name=exc.tool_name,
            risk_level=exc.risk_level,
            policy_mode=get_current_tool_policy_mode(),
            summary=exc.summary,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "type": "approval_required",
                "approval_id": exc.approval_id,
                "tool_name": exc.tool_name,
                "risk_level": exc.risk_level,
                "message": (
                    f"{exc.summary}\n\n"
                    "This is a high-risk action. Approve it first, then resend your request."
                ),
            },
        )
    except asyncio.TimeoutError:
        logger.warning("REST chat agent timed out after %ds", settings.agent_chat_timeout)
        raise HTTPException(status_code=504, detail="Agent timed out — try a simpler request")
    except Exception as e:
        logger.exception("Agent execution failed")
        safe_detail = await redact_secrets_in_text(f"Agent error: {e}")
        raise HTTPException(status_code=500, detail=safe_detail)

    await session_manager.add_message(session.id, "assistant", response_text)

    # Check if onboarding should be marked complete
    if not profile.onboarding_completed:
        msg_count = await session_manager.count_messages(session.id)
        if msg_count >= 6:
            await mark_onboarding_complete()
            logger.info("Onboarding completed via REST")

    # Trigger memory consolidation in background
    if response_text:
        try:
            from src.memory.consolidator import consolidate_session
            from src.utils.background import track_task
            track_task(consolidate_session(session.id), name=f"consolidate-{session.id[:8]}")
        except ImportError:
            pass

    return ChatResponse(
        response=response_text,
        session_id=session.id,
    )
