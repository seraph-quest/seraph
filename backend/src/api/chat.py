import asyncio
import contextvars
import json
import logging
from time import perf_counter

from fastapi import APIRouter, HTTPException

from src.approval.exceptions import ApprovalRequired
from src.approval.repository import approval_repository
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.agent.exceptions import ClarificationRequired
from config.settings import settings
from src.agent.factory import build_agent
from src.agent.onboarding import create_onboarding_agent
from src.agent.session import session_manager
from src.audit.runtime import log_agent_run_event
from src.audit.repository import audit_repository
from src.api.profile import get_or_create_profile, mark_onboarding_complete
from src.guardian.state import build_guardian_state
from src.models.schemas import ChatRequest, ChatResponse
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
        guardian_state = await build_guardian_state(
            session_id=session.id,
            user_message=request.message,
        )
        agent = build_agent(guardian_state=guardian_state)

    try:
        from src.observer.manager import context_manager as obs_manager
        started_at = perf_counter()
        llm_request_id = f"agent-rest:{session.id}:{started_at}"
        _register_request(llm_request_id)
        tokens = set_runtime_context(session.id, obs_manager.get_context().approval_mode)
        llm_request_token = set_current_llm_request_id(llm_request_id)
        run_ctx = contextvars.copy_context()
        reset_runtime_context(tokens)
        reset_current_llm_request_id(llm_request_token)
        result = await asyncio.wait_for(
            asyncio.to_thread(run_ctx.run, agent.run, request.message),
            timeout=settings.agent_chat_timeout,
        )
        response_text = str(result.output) if hasattr(result, "output") else str(result)
        response_text = await redact_secrets_in_text(response_text)
    except ApprovalRequired as exc:
        await approval_repository.merge_details(
            exc.approval_id,
            {"resume_message": request.message},
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
    except ClarificationRequired as exc:
        rendered = await redact_secrets_in_text(exc.render_message())
        await session_manager.add_message(
            session.id,
            "assistant",
            rendered,
            metadata_json=json.dumps({
                "display_role": "clarification",
                "question": exc.question,
                "reason": exc.reason,
                "options": exc.options,
            }),
        )
        await audit_repository.log_event(
            session_id=session.id,
            actor="agent",
            event_type="clarification_requested",
            tool_name="clarify",
            risk_level="low",
            policy_mode=get_current_tool_policy_mode(),
            summary=exc.question,
            details={
                "reason": exc.reason,
                "options": exc.options,
            },
        )
        raise HTTPException(
            status_code=409,
            detail={
                "type": "clarification_required",
                "session_id": session.id,
                "question": exc.question,
                "reason": exc.reason,
                "options": exc.options,
                "message": rendered,
            },
        )
    except asyncio.TimeoutError:
        _mark_request_timed_out(llm_request_id)
        logger.warning("REST chat agent timed out after %ds", settings.agent_chat_timeout)
        await log_agent_run_event(
            session_id=session.id,
            transport="rest",
            is_onboarding=not profile.onboarding_completed,
            outcome="timed_out",
            policy_mode=get_current_tool_policy_mode(),
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "message_length": len(request.message),
                "timeout_seconds": settings.agent_chat_timeout,
                "request_id": llm_request_id,
            },
        )
        raise HTTPException(status_code=504, detail="Agent timed out — try a simpler request")
    except Exception as e:
        logger.exception("Agent execution failed")
        safe_detail = await redact_secrets_in_text(f"Agent error: {e}")
        await log_agent_run_event(
            session_id=session.id,
            transport="rest",
            is_onboarding=not profile.onboarding_completed,
            outcome="failed",
            policy_mode=get_current_tool_policy_mode(),
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "message_length": len(request.message),
                "error": safe_detail,
                "request_id": llm_request_id,
            },
        )
        raise HTTPException(status_code=500, detail=safe_detail)
    finally:
        if "llm_request_id" in locals():
            _finish_request(llm_request_id)

    await session_manager.add_message(session.id, "assistant", response_text)
    await log_agent_run_event(
        session_id=session.id,
        transport="rest",
        is_onboarding=not profile.onboarding_completed,
        outcome="succeeded",
        policy_mode=get_current_tool_policy_mode(),
        details={
            "duration_ms": int((perf_counter() - started_at) * 1000),
            "message_length": len(request.message),
            "response_length": len(response_text),
            "request_id": llm_request_id,
        },
    )

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
