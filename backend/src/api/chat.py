import asyncio
import contextvars
import hashlib
import json
import logging
from contextlib import suppress
from datetime import datetime, timezone
from dataclasses import replace
from threading import Event
from time import perf_counter
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi import APIRouter, HTTPException, Request as HttpRequest

from src.approval.exceptions import ApprovalRequired
from src.approval.repository import approval_repository
from src.approval.runtime import (
    get_current_approval_mode,
    get_current_trust_principal,
    reset_runtime_context,
    set_runtime_context,
)
from src.agent.exceptions import ClarificationRequired
from config.settings import settings
from src.agent.direct_chat import run_direct_local_chat, should_use_direct_local_chat
from src.agent.factory import build_agent
from src.agent.onboarding import create_onboarding_agent
from src.agent.session import (
    MessageIngressConflictError,
    SessionNotFoundError,
    SessionOwnerMismatchError,
    session_manager,
)
from src.audit.runtime import log_agent_run_event
from src.audit.repository import audit_repository
from src.api.profile import get_or_create_profile, mark_onboarding_complete
from src.conversation.identity import (
    ConversationIdentityError,
    build_conversation_identity,
    build_lineage,
    lineage_json,
    validate_attachment_refs,
)
from src.auth.cancellation import (
    RuntimeRevokedError,
    assert_runtime_not_revoked,
    reset_revocation_guard,
    set_revocation_guard,
)
from src.auth.service import AuthFailure, auth_enabled, authenticate_token, bind_operator_principal
from src.guardian.state import build_guardian_state
from src.models.schemas import ChatIngressEnvelope, ChatRequest, ChatResponse
from src.operators.local_codex import ExternalAgentRuntimeRemovedError
from src.tools.policy import get_current_tool_policy_mode
from src.vault.redaction import redact_secrets_in_text
from src.vlm_runtime import direct_local_chat_route_error
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.llm_runtime import (
    _finish_request,
    _mark_request_timed_out,
    _register_request,
    reset_current_llm_request_id,
    set_current_llm_request_id,
)

logger = logging.getLogger(__name__)

router = APIRouter()


async def _watch_rest_operator_session(
    auth_cookie: str | None,
    revocation_guard: Event,
    stop_event: asyncio.Event,
) -> None:
    """Fail closed when an authenticated REST turn loses its session."""
    if not auth_cookie or not auth_enabled():
        return
    poll_seconds = max(float(settings.operator_auth_revocation_poll_seconds), 0.25)
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
            return
        except asyncio.TimeoutError:
            pass
        try:
            await authenticate_token(auth_cookie, touch=False)
        except AuthFailure:
            revocation_guard.set()
            return
        except Exception:
            logger.exception("REST operator-session validation failed; failing closed")
            revocation_guard.set()
            return


def _begin_rest_revocation_watch(http_request: HttpRequest):
    """Install a context-propagated revocation guard for one REST inference."""
    auth_cookie = http_request.cookies.get(settings.operator_auth_cookie_name)
    if not auth_cookie or not auth_enabled():
        return None
    revocation_guard = Event()
    stop_event = asyncio.Event()
    watcher = asyncio.create_task(
        _watch_rest_operator_session(auth_cookie, revocation_guard, stop_event),
        name="rest-operator-revocation-watch",
    )
    token = set_revocation_guard(revocation_guard)
    return revocation_guard, stop_event, watcher, token


async def _end_rest_revocation_watch(scope) -> None:
    if scope is None:
        return
    _revocation_guard, stop_event, watcher, token = scope
    reset_revocation_guard(token)
    stop_event.set()
    if not watcher.done():
        watcher.cancel()
    with suppress(asyncio.CancelledError):
        await watcher


async def _ensure_rest_authorized(http_request: HttpRequest, scope) -> None:
    """Recheck authority before REST transcript or outcome side effects."""
    if scope is None:
        return
    try:
        assert_runtime_not_revoked()
        if scope[0].is_set():
            raise RuntimeRevokedError("authenticated operator session was revoked")
        await authenticate_token(
            http_request.cookies.get(settings.operator_auth_cookie_name),
            touch=False,
        )
        # The watcher may revoke the session while token authentication is
        # awaiting its result. Recheck both the context guard and the watcher
        # event after that await, before the caller performs its side effect.
        assert_runtime_not_revoked()
        if scope[0].is_set():
            raise RuntimeRevokedError("authenticated operator session was revoked")
    except (AuthFailure, RuntimeRevokedError) as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "Operator session was revoked."},
        ) from exc


class ChatAuthorityError(Exception):
    """Raised when an interactive chat turn has no usable operator authority."""

    def __init__(self, message: str, *, status_code: int, code: str):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class ChatIngressValidationError(Exception):
    """Raised when a web ingress payload cannot resolve safely."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.message = message
        self.status_code = 422
        self.code = code


def _bind_chat_principal(
    session_id: str,
    *,
    principal: TrustPrincipal | None = None,
    operator=None,
) -> TrustPrincipal:
    """Bind an already authenticated operator to the server-owned chat session.

    The API route has no authority to mint an operator identity.  A trusted
    ingress boundary must bind the principal to the current runtime context;
    this helper only validates that identity and narrows it to the chat
    session selected by Seraph.
    """
    if operator is not None:
        principal = bind_operator_principal(operator, session_id)
    if principal is None:
        principal = get_current_trust_principal()
    if principal is None:
        raise ChatAuthorityError(
            "Chat requires an authenticated operator before OpenRouter inference.",
            status_code=401,
            code="chat_authentication_required",
        )
    try:
        principal_type = PrincipalType(principal.principal_type)
    except (TypeError, ValueError):
        raise ChatAuthorityError(
            "Chat principal identity is invalid; inference is blocked.",
            status_code=401,
            code="chat_principal_invalid",
        ) from None
    if (
        not principal.authenticated
        or principal.revoked
        or not principal.principal_id
        or principal_type is PrincipalType.ANONYMOUS
    ):
        raise ChatAuthorityError(
            "Chat requires an authenticated, active operator principal.",
            status_code=401,
            code="chat_principal_unauthorized",
        )
    if principal_type is not PrincipalType.OPERATOR:
        raise ChatAuthorityError(
            "Interactive chat requires an operator principal.",
            status_code=403,
            code="chat_principal_forbidden",
        )
    grants = set()
    for grant in principal.grants:
        try:
            grants.add(AuthorityGrant(grant))
        except (TypeError, ValueError):
            continue
    if AuthorityGrant.MODEL_INFERENCE not in grants:
        raise ChatAuthorityError(
            "Chat principal lacks model-inference authority.",
            status_code=403,
            code="chat_model_inference_forbidden",
        )
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        raise ChatAuthorityError(
            "Chat requires a server-owned session identity.",
            status_code=403,
            code="chat_session_missing",
        )
    if principal.session_id and principal.session_id != normalized_session_id:
        raise ChatAuthorityError(
            "Chat principal is not bound to this session.",
            status_code=403,
            code="chat_principal_session_mismatch",
        )
    return replace(principal, session_id=normalized_session_id)


def build_chat_ingress_envelope(
    *,
    message: str,
    session_id: str,
    principal: TrustPrincipal,
    operator_session_id: str,
    transport: str,
    client_message_id: str | None = None,
    idempotency_key: str | None = None,
    attachments: object = None,
) -> ChatIngressEnvelope:
    """Create server-owned metadata for one authenticated web ingress.

    A caller may provide either identity field to make a retry deterministic.
    If both aliases are supplied, they must carry the same normalized value so
    one canonical retry identity can be reserved. The canonical persisted
    message ID is derived by Seraph and is scoped to the bound principal and
    session; caller identity never grants authority.
    """
    normalized_client_message_id, normalized_idempotency_key = validate_chat_ingress_identity(
        client_message_id=client_message_id,
        idempotency_key=idempotency_key,
    )
    identity_material = normalized_idempotency_key or normalized_client_message_id
    if identity_material:
        server_message_id = uuid5(
            NAMESPACE_URL,
            f"seraph-chat:{principal.principal_id}:{session_id}:{identity_material}",
        ).hex
    else:
        server_message_id = uuid4().hex
        identity_material = server_message_id
    idempotency_key_digest = hashlib.sha256(identity_material.encode("utf-8")).hexdigest()
    safe_attachments = validate_attachment_refs(
        attachments,
        owner_principal_id=principal.principal_id,
    )
    conversation_identity = build_conversation_identity(
        conversation_id=session_id,
        thread_id=session_id,
        owner_principal_id=principal.principal_id,
        operator_session_id=operator_session_id,
        device_id=f"web-operator-session:{operator_session_id}",
        channel="web",
        transport=transport,
        correlation_id=f"chat:{server_message_id}",
    )
    return ChatIngressEnvelope(
        message_id=server_message_id,
        client_message_id=normalized_client_message_id,
        idempotency_key=f"sha256:{idempotency_key_digest}",
        idempotency_key_digest=idempotency_key_digest,
        principal_id=principal.principal_id,
        operator_session_id=operator_session_id,
        device_id=conversation_identity.device_id,
        transport=transport,
        session_id=session_id,
        conversation_id=conversation_identity.conversation_id,
        thread_id=conversation_identity.thread_id,
        correlation_id=f"chat:{server_message_id}",
        content_digest=hashlib.sha256(message.encode("utf-8")).hexdigest(),
        attachment_refs=safe_attachments,
        received_at=datetime.now(timezone.utc),
    )


def _chat_identity(envelope: ChatIngressEnvelope):
    return build_conversation_identity(
        conversation_id=envelope.conversation_id or envelope.session_id,
        thread_id=envelope.thread_id or envelope.session_id,
        owner_principal_id=envelope.principal_id,
        operator_session_id=envelope.operator_session_id,
        device_id=envelope.device_id,
        channel=envelope.channel,
        transport=envelope.transport,
        correlation_id=envelope.correlation_id,
        causation_id=envelope.message_id,
    )


def assistant_message_id_for_ingress(envelope: ChatIngressEnvelope, *, suffix: str = "assistant") -> str:
    """Derive one durable assistant identity for a successfully accepted turn."""
    return uuid5(
        NAMESPACE_URL,
        f"seraph-chat:{envelope.principal_id}:{envelope.session_id}:{envelope.message_id}:{suffix}",
    ).hex


def chat_assistant_metadata(
    envelope: ChatIngressEnvelope,
    *,
    message_id: str,
    display_role: str | None = None,
    extra: dict | None = None,
    degraded_state: str | None = None,
) -> str:
    metadata: dict = {
        "lineage": build_lineage(
            _chat_identity(envelope),
            attachment_refs=envelope.attachment_refs,
            message_id=message_id,
            degraded_state=degraded_state,
        )
    }
    if display_role:
        metadata["display_role"] = display_role
    if extra:
        metadata.update(extra)
    return json.dumps(metadata, sort_keys=True)


def validate_chat_ingress_identity(
    *,
    client_message_id: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[str | None, str | None]:
    """Normalize caller identity and reject ambiguous dual identities.

    The two request fields are aliases for this web ingress slice.  When both
    are present they must resolve to the same opaque value; otherwise there is
    no single retry identity to reserve.  This check is pure and can run before
    session creation or audit/model dispatch.
    """

    normalized_client_message_id = str(client_message_id).strip() if client_message_id is not None else None
    normalized_idempotency_key = str(idempotency_key).strip() if idempotency_key is not None else None
    if client_message_id is not None and not normalized_client_message_id:
        raise ChatIngressValidationError(
            "client message_id must not be blank.",
            code="chat_message_identity_invalid",
        )
    if idempotency_key is not None and not normalized_idempotency_key:
        raise ChatIngressValidationError(
            "idempotency_key must not be blank.",
            code="chat_message_identity_invalid",
        )
    if (
        normalized_client_message_id is not None
        and normalized_idempotency_key is not None
        and normalized_client_message_id != normalized_idempotency_key
    ):
        raise ChatIngressValidationError(
            "client message_id and idempotency_key must resolve to one identity.",
            code="chat_message_identity_conflict",
        )
    return normalized_client_message_id, normalized_idempotency_key


def validate_chat_message(message: str) -> None:
    """Reject empty or whitespace-only interactive messages before effects."""

    if not isinstance(message, str) or not message.strip():
        raise ChatIngressValidationError(
            "Chat message must not be blank.",
            code="chat_message_invalid",
        )


def chat_ingress_metadata(envelope: ChatIngressEnvelope) -> str:
    """Serialize only typed, non-content ingress metadata for the transcript."""
    return json.dumps(
        {"ingress": envelope.model_dump(mode="json")},
        sort_keys=True,
    )


def chat_ingress_continuity(envelope: ChatIngressEnvelope) -> dict[str, Any]:
    """Return safe canonical identifiers for a rejected duplicate/retry."""

    return {
        "schema_version": envelope.conversation_schema_version,
        "message_id": envelope.message_id,
        "session_id": envelope.session_id,
        "conversation_id": envelope.conversation_id,
        "thread_id": envelope.thread_id,
        "owner_principal_id": envelope.principal_id,
        "operator_session_id": envelope.operator_session_id,
        "device_id": envelope.device_id,
        "channel": envelope.channel,
        "transport": envelope.transport,
        "correlation_id": envelope.correlation_id,
        "causation_id": envelope.message_id,
        "idempotency_key_digest": envelope.idempotency_key_digest,
        "content_digest": envelope.content_digest,
        "attachment_refs": envelope.attachment_refs,
    }


def chat_ingress_rejection_detail(
    envelope: ChatIngressEnvelope,
    *,
    code: str,
    message: str,
) -> dict[str, Any]:
    """Build the canonical continuity payload for REST error responses."""

    continuity = chat_ingress_continuity(envelope)
    return {"code": code, "message": message, **continuity, "continuity": continuity}


async def log_chat_ingress_event(
    *,
    session_id: str,
    envelope: ChatIngressEnvelope,
    status: str,
) -> None:
    """Write an operator-readable receipt without exposing message content."""
    await audit_repository.log_event(
        session_id=session_id,
        actor="operator",
        event_type="chat_message_ingress",
        tool_name="chat_ingress",
        risk_level="low",
        policy_mode=get_current_tool_policy_mode(),
        summary=f"Chat message ingress {status}.",
        details={
            "status": status,
            "schema_version": envelope.schema_version,
            "message_id": envelope.message_id,
            "idempotency_key_digest": envelope.idempotency_key_digest,
            "content_digest": envelope.content_digest,
            "principal_id": envelope.principal_id,
            "operator_session_id": envelope.operator_session_id,
            "device_id": envelope.device_id,
            "channel": envelope.channel,
            "transport": envelope.transport,
            "session_id": envelope.session_id,
            "conversation_id": envelope.conversation_id,
            "thread_id": envelope.thread_id,
            "correlation_id": envelope.correlation_id,
            "attachment_count": len(envelope.attachment_refs),
        },
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, http_request: HttpRequest):
    """Send a message and receive an AI response."""
    try:
        operator = getattr(http_request.state, "operator", None)
        if operator is None:
            raise ChatAuthorityError(
                "Chat requires an authenticated operator ingress session.",
                status_code=401,
                code="chat_authentication_required",
            )
    except ChatAuthorityError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    try:
        validate_chat_message(request.message)
        validate_chat_ingress_identity(
            client_message_id=request.message_id,
            idempotency_key=request.idempotency_key,
        )
    except ChatIngressValidationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    if request.session_id is not None and not request.session_id.strip():
        raise HTTPException(
            status_code=422,
            detail={"code": "chat_session_required"},
        )
    try:
        session = await session_manager.get_for_ingress(
            request.session_id,
            owner_principal_id=operator.principal.principal_id,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "chat_session_not_found",
                "session_id": exc.session_id,
            },
        ) from exc
    except SessionOwnerMismatchError as exc:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "chat_session_owner_forbidden",
                "session_id": exc.session_id,
            },
        ) from exc
    try:
        chat_principal = _bind_chat_principal(
            session.id,
            operator=operator,
        )
    except ChatAuthorityError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    try:
        ingress = build_chat_ingress_envelope(
            message=request.message,
            session_id=session.id,
            principal=chat_principal,
            operator_session_id=operator.session_id,
            transport="rest",
            client_message_id=request.message_id,
            idempotency_key=request.idempotency_key,
            attachments=request.attachments,
        )
    except ConversationIdentityError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    try:
        _ingress_message, duplicate = await session_manager.reserve_ingress_message(
            session.id,
            request.message,
            message_id=ingress.message_id,
            metadata_json=chat_ingress_metadata(ingress),
            attachment_refs=request.attachments,
        )
    except MessageIngressConflictError as exc:
        await log_chat_ingress_event(
            session_id=session.id,
            envelope=ingress,
            status="identity_conflict",
        )
        raise HTTPException(
            status_code=409,
            detail=chat_ingress_rejection_detail(
                ingress,
                code="chat_message_identity_conflict",
                message="This message identity is already bound to another request.",
            ),
        ) from exc
    if duplicate:
        await log_chat_ingress_event(
            session_id=session.id,
            envelope=ingress,
            status="duplicate_rejected",
        )
        raise HTTPException(
            status_code=409,
            detail=chat_ingress_rejection_detail(
                ingress,
                code="chat_message_duplicate",
                message="This message was already accepted for this session.",
            ),
        )
    await log_chat_ingress_event(
        session_id=session.id,
        envelope=ingress,
        status="accepted",
    )

    # Check onboarding status
    profile = await get_or_create_profile()
    is_onboarding = not profile.onboarding_completed
    direct_runtime_path = "onboarding_agent" if is_onboarding else "chat_agent"
    try:
        from src.llm_runtime import reject_removed_external_agent_route

        reject_removed_external_agent_route(runtime_path=direct_runtime_path)
    except ExternalAgentRuntimeRemovedError as exc:
        raise HTTPException(status_code=410, detail=exc.payload()) from exc
    if should_use_direct_local_chat(
        request.message,
        runtime_path=direct_runtime_path,
        is_onboarding=is_onboarding,
    ):
        started_at = perf_counter()
        route_error = await direct_local_chat_route_error(runtime_path=direct_runtime_path)
        if route_error:
            safe_detail = await redact_secrets_in_text(route_error)
            await log_agent_run_event(
                session_id=session.id,
                transport="rest",
                is_onboarding=is_onboarding,
                outcome="failed",
                policy_mode=get_current_tool_policy_mode(),
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "message_length": len(request.message),
                    "error": safe_detail,
                    "runtime": "direct-openrouter-chat",
                    "failure_stage": "route_preflight",
                },
            )
            raise HTTPException(status_code=503, detail=safe_detail)
        llm_request_id = f"direct-rest:{session.id}:{started_at}"
        _register_request(llm_request_id)
        auth_tokens = set_runtime_context(
            session.id,
            get_current_approval_mode(),
            trust_principal=chat_principal,
        )
        revocation_scope = _begin_rest_revocation_watch(http_request)
        try:
            response_text = await asyncio.wait_for(
                run_direct_local_chat(
                    request.message,
                    runtime_path=direct_runtime_path,
                    is_onboarding=is_onboarding,
                    session_id=session.id,
                    request_id=llm_request_id,
                ),
                timeout=min(settings.agent_chat_timeout, 60),
            )
            response_text = await redact_secrets_in_text(response_text)
            assert_runtime_not_revoked()
        except RuntimeRevokedError as exc:
            raise HTTPException(
                status_code=401,
                detail={"code": "session_revoked", "message": "Operator session was revoked during inference."},
            ) from exc
        except asyncio.TimeoutError:
            _mark_request_timed_out(llm_request_id)
            await log_agent_run_event(
                session_id=session.id,
                transport="rest",
                is_onboarding=is_onboarding,
                outcome="timed_out",
                policy_mode=get_current_tool_policy_mode(),
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "message_length": len(request.message),
                    "timeout_seconds": min(settings.agent_chat_timeout, 60),
                    "request_id": llm_request_id,
                    "runtime": "direct-openrouter-chat",
                },
            )
            raise HTTPException(status_code=504, detail="OpenRouter chat timed out — try again")
        except Exception as e:
            logger.exception("Direct OpenRouter chat failed")
            safe_detail = await redact_secrets_in_text(f"Agent error: {e}")
            await log_agent_run_event(
                session_id=session.id,
                transport="rest",
                is_onboarding=is_onboarding,
                outcome="failed",
                policy_mode=get_current_tool_policy_mode(),
                details={
                    "duration_ms": int((perf_counter() - started_at) * 1000),
                    "message_length": len(request.message),
                    "error": safe_detail,
                    "request_id": llm_request_id,
                    "runtime": "direct-openrouter-chat",
                },
            )
            raise HTTPException(status_code=500, detail=safe_detail)
        finally:
            reset_runtime_context(auth_tokens)
            _finish_request(llm_request_id)
            await _end_rest_revocation_watch(revocation_scope)

        await _ensure_rest_authorized(http_request, revocation_scope)
        assistant_message_id = assistant_message_id_for_ingress(ingress)
        await session_manager.add_message(
            session.id,
            "assistant",
            response_text,
            metadata_json=chat_assistant_metadata(
                ingress,
                message_id=assistant_message_id,
            ),
            message_id=assistant_message_id,
        )
        await log_agent_run_event(
            session_id=session.id,
            transport="rest",
            is_onboarding=is_onboarding,
            outcome="succeeded",
            policy_mode=get_current_tool_policy_mode(),
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "message_length": len(request.message),
                "response_length": len(response_text),
                "request_id": llm_request_id,
                "runtime": "direct-openrouter-chat",
            },
        )
        return ChatResponse(
            response=response_text,
            session_id=session.id,
            conversation_id=ingress.conversation_id,
            thread_id=ingress.thread_id,
            message_id=assistant_message_id,
            owner_principal_id=ingress.principal_id,
            operator_session_id=ingress.operator_session_id,
            device_id=ingress.device_id,
            channel=ingress.channel,
            transport=ingress.transport,
            correlation_id=ingress.correlation_id,
            causation_id=ingress.message_id,
            attachment_refs=ingress.attachment_refs,
        )

    if is_onboarding:
        agent = create_onboarding_agent(request.message)
    else:
        try:
            guardian_state = await asyncio.wait_for(
                build_guardian_state(
                    session_id=session.id,
                    user_message=request.message,
                ),
                timeout=max(float(settings.guardian_state_timeout_seconds), 0.5),
            )
            agent = build_agent(guardian_state=guardian_state)
        except Exception:
            logger.warning(
                "Guardian state unavailable for REST chat; continuing with minimal agent context",
                exc_info=True,
            )
            agent = build_agent()

    revocation_scope = None
    try:
        from src.observer.manager import context_manager as obs_manager
        started_at = perf_counter()
        llm_request_id = f"agent-rest:{session.id}:{started_at}"
        _register_request(llm_request_id)
        revocation_scope = _begin_rest_revocation_watch(http_request)
        tokens = set_runtime_context(
            session.id,
            obs_manager.get_context().approval_mode,
            trust_principal=chat_principal,
        )
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
        assert_runtime_not_revoked()
    except RuntimeRevokedError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "session_revoked", "message": "Operator session was revoked during inference."},
        ) from exc
    except ApprovalRequired as exc:
        await _ensure_rest_authorized(http_request, revocation_scope)
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
        await _ensure_rest_authorized(http_request, revocation_scope)
        rendered = await redact_secrets_in_text(exc.render_message())
        clarification_message_id = assistant_message_id_for_ingress(ingress, suffix="clarification")
        await session_manager.add_message(
            session.id,
            "assistant",
            rendered,
            metadata_json=chat_assistant_metadata(
                ingress,
                message_id=clarification_message_id,
                display_role="clarification",
                extra={
                    "question": exc.question,
                    "reason": exc.reason,
                    "options": exc.options,
                },
            ),
            message_id=clarification_message_id,
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
        await _end_rest_revocation_watch(revocation_scope)

    await _ensure_rest_authorized(http_request, revocation_scope)
    assistant_message_id = assistant_message_id_for_ingress(ingress)
    await session_manager.add_message(
        session.id,
        "assistant",
        response_text,
        metadata_json=chat_assistant_metadata(
            ingress,
            message_id=assistant_message_id,
        ),
        message_id=assistant_message_id,
    )
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
            from src.memory.flush import flush_session_memory
            from src.utils.background import track_task
            track_task(
                flush_session_memory(session.id, trigger="post_response"),
                name=f"consolidate-{session.id[:8]}",
            )
        except ImportError:
            pass

    return ChatResponse(
        response=response_text,
        session_id=session.id,
        conversation_id=ingress.conversation_id,
        thread_id=ingress.thread_id,
        message_id=assistant_message_id,
        owner_principal_id=ingress.principal_id,
        operator_session_id=ingress.operator_session_id,
        device_id=ingress.device_id,
        channel=ingress.channel,
        transport=ingress.transport,
        correlation_id=ingress.correlation_id,
        causation_id=ingress.message_id,
        attachment_refs=ingress.attachment_refs,
    )
