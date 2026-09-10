import asyncio
import contextvars
import json
import logging
from threading import Event
from contextlib import suppress
from time import perf_counter

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from smolagents import ActionStep, ToolCall, FinalAnswerStep

from config.settings import settings
from src.approval.exceptions import ApprovalRequired
from src.approval.repository import approval_repository
from src.approval.runtime import get_current_approval_mode, reset_runtime_context, set_runtime_context
from src.auth.cancellation import reset_revocation_guard, set_revocation_guard
from src.agent.exceptions import ClarificationRequired
from src.agent.direct_chat import run_direct_local_chat, should_use_direct_local_chat, stream_direct_local_chat
from src.agent.factory import build_agent
from src.agent.onboarding import create_onboarding_agent
from src.agent.session import (
    MessageIngressConflictError,
    SessionNotFoundError,
    SessionOwnerMismatchError,
    session_manager,
)
from src.audit.formatting import format_tool_call_summary
from src.audit.runtime import log_agent_run_event
from src.audit.repository import audit_repository
from src.api.profile import get_or_create_profile, mark_onboarding_complete, reset_onboarding
from src.api.chat import (
    ChatAuthorityError,
    ChatIngressValidationError,
    _bind_chat_principal,
    build_chat_ingress_envelope,
    chat_ingress_metadata,
    log_chat_ingress_event,
    validate_chat_ingress_identity,
    validate_chat_message,
)
from src.auth.middleware import authenticate_websocket
from src.auth.service import AuthFailure, auth_enabled, authenticate_token, bind_operator_principal
from src.guardian.state import build_guardian_state
from src.models.schemas import WSMessage, WSResponse
from src.operators.local_codex import ExternalAgentRuntimeRemovedError
from src.scheduler.connection_manager import ws_manager
from src.tools.policy import get_current_tool_policy_mode
from src.vault.redaction import redact_secrets_for_streaming_snapshot, redact_secrets_in_text
from src.vlm_runtime import direct_local_chat_route_error
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
_INTERRUPTED_TURN_MESSAGE = (
    "Response interrupted because the browser connection closed before Seraph could finish. "
    "Please send that turn again."
)


class _DirectStreamOutcomeUncertain(Exception):
    """A remote stream failed without a confirmed complete response."""


class _OperatorSessionRevoked(Exception):
    """The authenticated operator lost authority during a live turn."""


async def _await_authorized(awaitable, revoked_event: asyncio.Event, *, timeout: float | None = None):
    """Await work while allowing session revocation to cancel the work."""
    work_task = asyncio.ensure_future(awaitable)
    revoked_task = asyncio.create_task(revoked_event.wait(), name="operator-revocation-wait")
    try:
        wait_kwargs = {"return_when": asyncio.FIRST_COMPLETED}
        if timeout is None:
            done, _ = await asyncio.wait({work_task, revoked_task}, **wait_kwargs)
        else:
            done, _ = await asyncio.wait({work_task, revoked_task}, timeout=timeout, **wait_kwargs)
            if not done:
                work_task.cancel()
                with suppress(asyncio.CancelledError):
                    await work_task
                raise asyncio.TimeoutError
        if revoked_task in done and revoked_event.is_set():
            work_task.cancel()
            with suppress(asyncio.CancelledError):
                await work_task
            raise _OperatorSessionRevoked
        return await work_task
    finally:
        if not work_task.done():
            work_task.cancel()
            with suppress(asyncio.CancelledError):
                await work_task
        if not revoked_task.done():
            revoked_task.cancel()
            with suppress(asyncio.CancelledError):
                await revoked_task


async def watch_operator_session(
    websocket,
    auth_cookie: str | None,
    revoked_event: asyncio.Event,
    revocation_guard: Event,
) -> None:
    """Poll the authenticated session and close a socket after revocation/expiry."""
    if not auth_cookie:
        return
    poll_seconds = max(float(settings.operator_auth_revocation_poll_seconds), 0.25)
    while True:
        await asyncio.sleep(poll_seconds)
        try:
            await authenticate_token(auth_cookie, touch=False)
        except AuthFailure as exc:
            revoked_event.set()
            revocation_guard.set()
            with suppress(Exception):
                await websocket.close(code=4401, reason=exc.code)
            return
        except Exception:
            # Losing the auth-store connection is an authorization failure. Do
            # not leave an already accepted socket usable while revocation
            # state is unavailable.
            logger.exception("WebSocket operator-session validation failed; closing fail-closed")
            revoked_event.set()
            revocation_guard.set()
            with suppress(Exception):
                await websocket.close(code=1011, reason="auth_state_unavailable")
            return


async def _authorized_with_timeout(
    awaitable,
    revoked_event: asyncio.Event,
    *,
    timeout: float,
):
    """Keep a concrete timeout boundary while cleaning up on patched/cancelled waits."""
    authorized_task = asyncio.create_task(
        _await_authorized(awaitable, revoked_event),
        name="operator-authorized-work",
    )
    try:
        return await asyncio.wait_for(authorized_task, timeout=timeout)
    except BaseException:
        if not authorized_task.done():
            authorized_task.cancel()
            with suppress(asyncio.CancelledError):
                await authorized_task
        raise


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
        return create_onboarding_agent(message), True, set()

    try:
        guardian_state = await asyncio.wait_for(
            build_guardian_state(
                session_id=session_id,
                user_message=message,
            ),
            timeout=max(float(settings.guardian_state_timeout_seconds), 0.5),
        )
    except Exception:
        logger.warning(
            "Guardian state unavailable for websocket chat; continuing with minimal agent context",
            exc_info=True,
        )
        return build_agent(), False, set()
    agent = build_agent(guardian_state=guardian_state)
    specialist_names = (
        set(agent.managed_agents.keys())
        if hasattr(agent, "managed_agents") and agent.managed_agents
        else set()
    )
    return agent, False, specialist_names


@router.websocket("/chat")
async def websocket_chat(websocket: WebSocket):
    """WebSocket endpoint for streaming chat responses."""
    try:
        operator = await authenticate_websocket(websocket)
    except AuthFailure as exc:
        await websocket.close(code=4401, reason=exc.code)
        return
    await websocket.accept()
    ws_manager.connect(websocket)
    auth_revoked = asyncio.Event()
    revocation_guard = Event()
    auth_cookie = websocket.cookies.get(settings.operator_auth_cookie_name) if auth_enabled() else None

    revocation_task = asyncio.create_task(
        watch_operator_session(websocket, auth_cookie, auth_revoked, revocation_guard),
        name=f"ws-auth-watch:{operator.session_id[:8]}",
    )
    _seq = 0
    active_turn_session_id: str | None = None
    active_turn_completed = True
    revocation_guard_token = None

    def _next_seq() -> int:
        nonlocal _seq
        _seq += 1
        return _seq

    async def _record_interrupted_turn() -> None:
        nonlocal active_turn_completed
        if active_turn_completed or not active_turn_session_id:
            return
        active_turn_completed = True
        with suppress(Exception):
            await session_manager.add_message(active_turn_session_id, "assistant", _INTERRUPTED_TURN_MESSAGE)

    async def _ensure_operator_active() -> None:
        if auth_revoked.is_set():
            raise _OperatorSessionRevoked

    # Send welcome message if user hasn't completed onboarding
    try:
        profile = await get_or_create_profile()
        if not profile.onboarding_completed:
            await websocket.send_text(
                WSResponse(
                    type="proactive",
                    content=(
                        "Seraph online. Before I begin acting on your behalf, I need a clearer read on who you are, "
                        "what matters most, and how you want this workspace to operate. "
                        "I'll ask a few short onboarding questions to establish that baseline. "
                        "During onboarding I'm using a limited setup focused on your guardian record and priorities. "
                        "If you want the full workspace immediately, just say the word and I'll skip ahead."
                    ),
                    intervention_type="advisory",
                    seq=_next_seq(),
                ).model_dump_json()
            )
    except Exception as e:
        logger.warning("Failed to send welcome message: %s", e)

    try:
        while True:
            await _ensure_operator_active()
            raw = await websocket.receive_text()
            if auth_cookie:
                try:
                    operator = await authenticate_token(auth_cookie, touch=True)
                except AuthFailure:
                    auth_revoked.set()
                    revocation_guard.set()
                    raise _OperatorSessionRevoked
                except Exception:
                    logger.exception("WebSocket operator-session refresh failed; closing fail-closed")
                    auth_revoked.set()
                    revocation_guard.set()
                    raise _OperatorSessionRevoked
            try:
                data = json.loads(raw)
                ws_msg = WSMessage(**data)
            except Exception as e:
                await websocket.send_text(
                    WSResponse(
                        type="error",
                        content=f"Invalid message: {e}",
                        reason="chat_message_invalid" if isinstance(e, ValueError) else None,
                        seq=_next_seq(),
                    ).model_dump_json()
                )
                continue

            if ws_msg.type == "ping":
                await _ensure_operator_active()
                await websocket.send_text(
                    WSResponse(type="pong", content="pong").model_dump_json()
                )
                continue

            if ws_msg.type == "skip_onboarding":
                await _ensure_operator_active()
                await mark_onboarding_complete()
                await websocket.send_text(
                    WSResponse(
                        type="final",
                        content=(
                            "Onboarding skipped. "
                            "The full workspace is now available. What do you want me to help you do?"
                        ),
                        seq=_next_seq(),
                    ).model_dump_json()
                )
                continue

            if ws_msg.type != "resume_message":
                try:
                    validate_chat_message(ws_msg.message)
                    validate_chat_ingress_identity(
                        client_message_id=ws_msg.message_id,
                        idempotency_key=ws_msg.idempotency_key,
                    )
                except ChatIngressValidationError as exc:
                    active_turn_completed = True
                    await websocket.send_text(
                        WSResponse(
                            type="error",
                            content=exc.message,
                            reason=exc.code,
                            seq=_next_seq(),
                        ).model_dump_json()
                    )
                    continue

            if ws_msg.session_id is not None and not ws_msg.session_id.strip():
                active_turn_completed = True
                await websocket.send_text(
                    WSResponse(
                        type="error",
                        content="Chat session id must not be blank.",
                        seq=_next_seq(),
                    ).model_dump_json()
                )
                continue

            try:
                session = await session_manager.get_for_ingress(
                    ws_msg.session_id,
                    owner_principal_id=operator.principal.principal_id,
                )
            except SessionNotFoundError as exc:
                active_turn_session_id = exc.session_id
                active_turn_completed = True
                await websocket.send_text(
                    WSResponse(
                        type="error",
                        content="This chat session was not found.",
                        session_id=exc.session_id,
                        reason="chat_session_not_found",
                        seq=_next_seq(),
                    ).model_dump_json()
                )
                continue
            except SessionOwnerMismatchError as exc:
                active_turn_session_id = exc.session_id
                active_turn_completed = True
                await websocket.send_text(
                    WSResponse(
                        type="error",
                        content="This conversation belongs to another operator.",
                        session_id=exc.session_id,
                        seq=_next_seq(),
                    ).model_dump_json()
                )
                continue
            try:
                chat_principal = _bind_chat_principal(
                    session.id,
                    principal=bind_operator_principal(operator, session.id),
                )
            except ChatAuthorityError as exc:
                active_turn_session_id = session.id
                active_turn_completed = True
                await websocket.send_text(
                    WSResponse(
                        type="error",
                        content=exc.message,
                        session_id=session.id,
                        seq=_next_seq(),
                    ).model_dump_json()
                )
                continue
            ingress = None
            if ws_msg.type != "resume_message":
                ingress = build_chat_ingress_envelope(
                    message=ws_msg.message,
                    session_id=session.id,
                    principal=chat_principal,
                    operator_session_id=operator.session_id,
                    transport="websocket",
                    client_message_id=ws_msg.message_id,
                    idempotency_key=ws_msg.idempotency_key,
                )
                try:
                    _ingress_message, duplicate = await session_manager.reserve_ingress_message(
                        session.id,
                        ws_msg.message,
                        message_id=ingress.message_id,
                        metadata_json=chat_ingress_metadata(ingress),
                    )
                except MessageIngressConflictError as exc:
                    await log_chat_ingress_event(
                        session_id=session.id,
                        envelope=ingress,
                        status="identity_conflict",
                    )
                    active_turn_session_id = session.id
                    active_turn_completed = True
                    await websocket.send_text(
                        WSResponse(
                            type="error",
                            content="This message identity is already bound to another request.",
                            session_id=session.id,
                            reason="chat_message_identity_conflict",
                            seq=_next_seq(),
                        ).model_dump_json()
                    )
                    continue
                if duplicate:
                    await log_chat_ingress_event(
                        session_id=session.id,
                        envelope=ingress,
                        status="duplicate_rejected",
                    )
                    active_turn_session_id = session.id
                    active_turn_completed = True
                    await websocket.send_text(
                        WSResponse(
                            type="error",
                            content="This message was already accepted for this session.",
                            session_id=session.id,
                            reason="chat_message_duplicate",
                            seq=_next_seq(),
                        ).model_dump_json()
                    )
                    continue
                await log_chat_ingress_event(
                    session_id=session.id,
                    envelope=ingress,
                    status="accepted",
                )
            active_turn_session_id = session.id
            active_turn_completed = False
            await websocket.send_text(
                WSResponse(
                    type="status",
                    content="Seraph received the message.",
                    session_id=session.id,
                    seq=_next_seq(),
                ).model_dump_json()
            )
            try:
                from src.observer.manager import context_manager
                context_manager.update_last_interaction()
            except Exception:
                pass

            profile = await get_or_create_profile()
            direct_is_onboarding = not profile.onboarding_completed
            direct_runtime_path = "onboarding_agent" if direct_is_onboarding else "chat_agent"
            try:
                from src.llm_runtime import reject_removed_external_agent_route

                reject_removed_external_agent_route(runtime_path=direct_runtime_path)
            except ExternalAgentRuntimeRemovedError as exc:
                active_turn_completed = True
                await websocket.send_text(
                    WSResponse(
                        type="error",
                        content=json.dumps(exc.payload()),
                        session_id=session.id,
                        seq=_next_seq(),
                    ).model_dump_json()
                )
                continue

            if should_use_direct_local_chat(
                ws_msg.message,
                runtime_path=direct_runtime_path,
                is_onboarding=direct_is_onboarding,
            ):
                started_at = perf_counter()
                route_error = await direct_local_chat_route_error(runtime_path=direct_runtime_path)
                if route_error:
                    safe_error = await redact_secrets_in_text(route_error)
                    await log_agent_run_event(
                        session_id=session.id,
                        transport="websocket",
                        is_onboarding=direct_is_onboarding,
                        outcome="failed",
                        policy_mode=get_current_tool_policy_mode(),
                        details={
                            "duration_ms": int((perf_counter() - started_at) * 1000),
                            "message_length": len(ws_msg.message),
                            "error": safe_error,
                            "runtime": "direct-openrouter-chat",
                            "failure_stage": "route_preflight",
                        },
                    )
                    active_turn_completed = True
                    await websocket.send_text(
                        WSResponse(
                            type="error",
                            content=safe_error,
                            session_id=session.id,
                            seq=_next_seq(),
                        ).model_dump_json()
                    )
                    continue
                llm_request_id = f"direct-ws:{session.id}:{started_at}"
                _register_request(llm_request_id)
                auth_tokens = set_runtime_context(
                    session.id,
                    get_current_approval_mode(),
                    trust_principal=chat_principal,
                )
                revocation_guard_token = set_revocation_guard(revocation_guard)
                try:
                    await websocket.send_text(
                        WSResponse(
                            type="status",
                            content="Seraph is using the governed OpenRouter chat runtime.",
                            session_id=session.id,
                            seq=_next_seq(),
                        ).model_dump_json()
                    )
                    streamed_parts: list[str] = []
                    emitted_safe_chars = 0

                    async def _stream_direct_reply() -> str:
                        nonlocal emitted_safe_chars
                        async for delta in stream_direct_local_chat(
                            ws_msg.message,
                            runtime_path=direct_runtime_path,
                            is_onboarding=direct_is_onboarding,
                            session_id=session.id,
                        ):
                            streamed_parts.append(delta)
                            safe_delta, emitted_safe_chars = await redact_secrets_for_streaming_snapshot(
                                "".join(streamed_parts),
                                emitted_safe_chars,
                            )
                            if safe_delta:
                                await websocket.send_text(
                                    WSResponse(
                                        type="delta",
                                        content=safe_delta,
                                        session_id=session.id,
                                        seq=_next_seq(),
                                    ).model_dump_json()
                                )
                        return "".join(streamed_parts).strip()

                    try:
                        final_result = await _authorized_with_timeout(
                            _stream_direct_reply(),
                            auth_revoked,
                            timeout=min(settings.agent_chat_timeout, 60),
                        )
                    except asyncio.TimeoutError:
                        raise
                    except _OperatorSessionRevoked:
                        raise
                    except Exception as exc:
                        raise _DirectStreamOutcomeUncertain(str(exc)) from exc

                    final_result = await redact_secrets_in_text(final_result, fail_closed=True)
                except _OperatorSessionRevoked:
                    active_turn_completed = True
                    raise
                except asyncio.TimeoutError:
                    _mark_request_timed_out(llm_request_id)
                    await log_agent_run_event(
                        session_id=session.id,
                        transport="websocket",
                        is_onboarding=direct_is_onboarding,
                        outcome="timed_out",
                        policy_mode=get_current_tool_policy_mode(),
                        details={
                            "duration_ms": int((perf_counter() - started_at) * 1000),
                            "message_length": len(ws_msg.message),
                            "timeout_seconds": min(settings.agent_chat_timeout, 60),
                            "request_id": llm_request_id,
                            "runtime": "direct-openrouter-chat",
                        },
                    )
                    active_turn_completed = True
                    await websocket.send_text(
                        WSResponse(
                            type="error",
                            content="OpenRouter chat timed out — try again",
                            session_id=session.id,
                            seq=_next_seq(),
                        ).model_dump_json()
                    )
                    continue
                except _DirectStreamOutcomeUncertain as exc:
                    logger.warning(
                        "Direct OpenRouter websocket stream ended with an uncertain outcome; automatic retry suppressed",
                        exc_info=True,
                    )
                    safe_error = await redact_secrets_in_text(str(exc) or "provider stream failed")
                    uncertain_message = (
                        "OpenRouter streaming ended before Seraph received a confirmed complete response. "
                        "The remote outcome is uncertain, so Seraph did not retry automatically. "
                        "Retry this message explicitly if you want to try again."
                    )
                    await log_agent_run_event(
                        session_id=session.id,
                        transport="websocket",
                        is_onboarding=direct_is_onboarding,
                        outcome="uncertain",
                        policy_mode=get_current_tool_policy_mode(),
                        details={
                            "duration_ms": int((perf_counter() - started_at) * 1000),
                            "message_length": len(ws_msg.message),
                            "error": safe_error,
                            "request_id": llm_request_id,
                            "runtime": "direct-openrouter-chat",
                            "failure_stage": "streaming",
                            "remote_outcome": "uncertain",
                            "retry_required": True,
                        },
                    )
                    active_turn_completed = True
                    await websocket.send_text(
                        WSResponse(
                            type="error",
                            content=uncertain_message,
                            session_id=session.id,
                            seq=_next_seq(),
                        ).model_dump_json()
                    )
                    continue
                except Exception as e:
                    logger.exception("Direct OpenRouter websocket chat failed")
                    safe_error = await redact_secrets_in_text(f"Agent error: {e}")
                    await log_agent_run_event(
                        session_id=session.id,
                        transport="websocket",
                        is_onboarding=direct_is_onboarding,
                        outcome="failed",
                        policy_mode=get_current_tool_policy_mode(),
                        details={
                            "duration_ms": int((perf_counter() - started_at) * 1000),
                            "message_length": len(ws_msg.message),
                            "error": safe_error,
                            "request_id": llm_request_id,
                            "runtime": "direct-openrouter-chat",
                        },
                    )
                    active_turn_completed = True
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
                    if revocation_guard_token is not None:
                        reset_revocation_guard(revocation_guard_token)
                        revocation_guard_token = None
                    reset_runtime_context(auth_tokens)
                    _finish_request(llm_request_id)

                await session_manager.add_message(session.id, "assistant", final_result)
                active_turn_completed = True
                await log_agent_run_event(
                    session_id=session.id,
                    transport="websocket",
                    is_onboarding=direct_is_onboarding,
                    outcome="succeeded",
                    policy_mode=get_current_tool_policy_mode(),
                    details={
                        "duration_ms": int((perf_counter() - started_at) * 1000),
                        "message_length": len(ws_msg.message),
                        "response_length": len(final_result),
                        "request_id": llm_request_id,
                        "runtime": "direct-openrouter-chat",
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
                continue

            await websocket.send_text(
                WSResponse(
                    type="status",
                    content="Seraph is preparing the agent context.",
                    session_id=session.id,
                    seq=_next_seq(),
                ).model_dump_json()
            )
            agent, is_onboarding, specialist_names = await _build_agent(session.id, ws_msg.message)
            await websocket.send_text(
                WSResponse(
                    type="status",
                    content="Seraph is using the governed OpenRouter chat runtime.",
                    session_id=session.id,
                    seq=_next_seq(),
                ).model_dump_json()
            )

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
                tokens = set_runtime_context(
                    session.id,
                    context_manager.get_context().approval_mode,
                    trust_principal=chat_principal,
                )
                revocation_guard_token = set_revocation_guard(revocation_guard)
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

                drain_task = asyncio.create_task(
                    _drain_queue(),
                    name=f"ws-drain:{session.id[:8]}",
                )
                try:
                    await _authorized_with_timeout(
                        drain_task,
                        auth_revoked,
                        timeout=settings.agent_chat_timeout,
                    )
                except Exception:
                    if not drain_task.done():
                        drain_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await drain_task
                    raise

            except _OperatorSessionRevoked:
                active_turn_completed = True
                raise
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
                        "request_id": llm_request_id,
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
                active_turn_completed = True
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
                active_turn_completed = True
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
                await websocket.send_text(
                    WSResponse(
                        type="clarification_required",
                        content=rendered,
                        session_id=session.id,
                        seq=_next_seq(),
                        question=exc.question,
                        reason=exc.reason or None,
                        options=exc.options or None,
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
                        "request_id": llm_request_id,
                    },
                )
                active_turn_completed = True
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
                if revocation_guard_token is not None:
                    reset_revocation_guard(revocation_guard_token)
                    revocation_guard_token = None
                if "llm_request_id" in locals():
                    _finish_request(llm_request_id)

            await session_manager.add_message(session.id, "assistant", final_result)
            active_turn_completed = True
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
                        "request_id": llm_request_id,
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
                    from src.memory.flush import flush_session_memory
                    from src.utils.background import track_task
                    track_task(
                        flush_session_memory(session.id, trigger="post_response"),
                        name=f"consolidate-{session.id[:8]}",
                    )
                except Exception:
                    logger.debug("Failed to schedule memory consolidation", exc_info=True)

    except _OperatorSessionRevoked:
        ws_manager.disconnect(websocket)
        logger.info("WebSocket closed because the operator session was revoked or expired")
    except WebSocketDisconnect:
        if not auth_revoked.is_set():
            await _record_interrupted_turn()
        ws_manager.disconnect(websocket)
        logger.info("WebSocket client disconnected")
    except RuntimeError as exc:
        ws_manager.disconnect(websocket)
        if "WebSocket is not connected" in str(exc):
            await _record_interrupted_turn()
            logger.info("WebSocket client disconnected before next receive")
            return
        raise
    finally:
        if not revocation_task.done():
            revocation_task.cancel()
            with suppress(asyncio.CancelledError):
                await revocation_task
