"""Delivery coordinator — single entry point for all proactive messages."""

import hashlib
import logging
from contextlib import contextmanager
from dataclasses import replace

from src.audit.runtime import log_observer_delivery_event
from src.conversation.identity import (
    ConversationIdentityError,
    build_conversation_identity,
    validate_attachment_refs,
)
from src.models.schemas import WSResponse
from src.observer.intervention_policy import InterventionDecision, decide_intervention
from src.observer.native_notification_queue import (
    NativeNotificationBudgetDenied,
    native_notification_queue,
)

logger = logging.getLogger(__name__)

_BUILTIN_CHANNEL_TRANSPORTS = {"websocket", "native_notification"}


def _current_trust_principal():
    """Load runtime authority lazily to keep observer imports acyclic."""
    from src.approval.runtime import get_current_trust_principal

    return get_current_trust_principal()


@contextmanager
def _native_delivery_runtime(principal):
    """Bind delegated scheduler ownership while the native queue validates it."""

    if principal is None:
        yield
        return
    from src.approval.runtime import (
        get_current_approval_mode,
        get_current_session_id,
        get_current_trust_principal,
        reset_runtime_context,
        set_runtime_context,
    )

    current = get_current_trust_principal()
    if current == principal:
        yield
        return
    tokens = set_runtime_context(
        get_current_session_id(),
        get_current_approval_mode(),
        trust_principal=principal,
    )
    try:
        yield
    finally:
        reset_runtime_context(tokens)


def _delegated_native_principal(
    principal,
    *,
    owner_principal_id: str | None,
    session_id: str | None,
    operator_session_id: str | None,
):
    """Let an authenticated service job carry a canonical goal owner fence."""

    if principal is None or not owner_principal_id:
        return principal
    principal_type = str(
        getattr(getattr(principal, "principal_type", None), "value", getattr(principal, "principal_type", ""))
    ).lower()
    if principal_type != "service":
        return principal
    return replace(
        principal,
        principal_id=owner_principal_id,
        session_id=session_id or getattr(principal, "session_id", None),
        operator_session_id=operator_session_id or getattr(principal, "operator_session_id", None),
    )


def _transport_failure_reason(
    *,
    attempted_connections: int,
    failed_connections: int,
    websocket_enabled: bool = True,
) -> str:
    if not websocket_enabled:
        return "websocket_adapter_disabled"
    if attempted_connections <= 0:
        return "no_active_connections"
    if failed_connections >= attempted_connections:
        return "all_connections_failed"
    return "unknown_transport_failure"


def _prefer_delivery_error(current_error: str | None, next_error: str) -> str:
    if not current_error or current_error in {"no_active_route_transport", "unknown_transport_failure"}:
        return next_error
    if next_error in {"daemon_unavailable", "no_active_route_transport"}:
        return current_error
    return next_error


def _route_disabled_error(*, primary_transport: str | None) -> str:
    if primary_transport == "websocket":
        return "websocket_adapter_disabled"
    return "no_active_route_transport"


def _active_channel_adapters() -> set[str]:
    from config.settings import settings
    from src.extensions.channels import select_active_channel_adapters
    from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
    from src.extensions.state import connector_enabled_overrides, load_extension_state_payload

    snapshot = ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    contributions = snapshot.list_contributions("channel_adapters")
    state_payload = load_extension_state_payload()
    state_by_id = state_payload.get("extensions")
    active_adapters = select_active_channel_adapters(
        contributions,
        enabled_overrides=connector_enabled_overrides(state_by_id),
    )
    active_transports = {item.transport for item in active_adapters}
    return active_transports | (_BUILTIN_CHANNEL_TRANSPORTS - active_transports)


def _delivery_route_name(*, message: WSResponse, is_scheduled: bool) -> str:
    if is_scheduled:
        return "scheduled_delivery"
    if message.intervention_type == "alert":
        return "alert_delivery"
    return "live_delivery"


def _route_transport_order(route_name: str, *, active_channel_adapters: set[str]) -> tuple[dict[str, str | None], list[str]]:
    from src.extensions.channel_routing import route_runtime_status
    from src.extensions.state import load_extension_state_payload
    from src.observer.manager import context_manager
    from src.scheduler.connection_manager import ws_manager

    state_payload = load_extension_state_payload()
    _, route_status = route_runtime_status(
        state_payload,
        route=route_name,
        active_transports=active_channel_adapters,
        websocket_connection_count=ws_manager.active_count,
        daemon_connected=context_manager.is_daemon_connected(),
    )
    return route_status, list(route_status.get("delivery_order") or [])


def _bundle_continuation_payload(items: list[object], *, bundle_content: str) -> dict[str, str | None]:
    session_ids = []
    for item in items:
        raw_session_id = getattr(item, "session_id", None)
        session_id = str(raw_session_id).strip() if isinstance(raw_session_id, str) else None
        if not session_id:
            return {
                "session_id": None,
                "thread_id": None,
                "thread_source": "ambient",
                "continuation_mode": "open_thread",
                "resume_message": "Review the queued guardian updates.",
            }
        session_ids.append(session_id)

    common_session_id = session_ids[0] if session_ids else None
    if common_session_id and all(session_id == common_session_id for session_id in session_ids):
        return {
            "session_id": common_session_id,
            "thread_id": common_session_id,
            "thread_source": "session",
            "continuation_mode": "resume_thread",
            "resume_message": f"Continue from these guardian updates: {bundle_content}",
        }

    return {
        "session_id": None,
        "thread_id": None,
        "thread_source": "ambient",
        "continuation_mode": "open_thread",
        "resume_message": "Review the queued guardian updates.",
    }


async def _bundle_owner_binding(items: list[object]) -> tuple[str | None, str | None]:
    """Resolve and validate one durable owner's identity for a bundle group."""
    session_ids = {
        str(getattr(item, "session_id", "")).strip()
        for item in items
        if isinstance(getattr(item, "session_id", None), str)
        and str(getattr(item, "session_id", "")).strip()
    }
    owner_ids = {
        str(getattr(item, "owner_principal_id", "")).strip()
        for item in items
        if isinstance(getattr(item, "owner_principal_id", None), str)
        and str(getattr(item, "owner_principal_id", "")).strip()
    }
    operator_session_ids = {
        str(getattr(item, "operator_session_id", "")).strip()
        for item in items
        if isinstance(getattr(item, "operator_session_id", None), str)
        and str(getattr(item, "operator_session_id", "")).strip()
    }
    if len(session_ids) > 1 or len(owner_ids) > 1 or len(operator_session_ids) > 1:
        raise ConversationIdentityError(
            "conversation_bundle_identity_conflict",
            "A native bundle group contains conflicting owner or session bindings.",
        )
    session_id = next(iter(session_ids), None)
    owner_principal_id = next(iter(owner_ids), None)
    operator_session_id = next(iter(operator_session_ids), None)
    if session_id and owner_principal_id is None:
        from src.agent.session import session_manager

        session = await session_manager.get(session_id)
        owner_principal_id = (
            str(session.owner_principal_id).strip()
            if session is not None and session.owner_principal_id
            else None
        )
    if session_id and owner_principal_id is None:
        raise ConversationIdentityError(
            "conversation_owner_missing",
            "A session-bound queued insight has no canonical owner.",
        )
    if operator_session_id and owner_principal_id is None:
        raise ConversationIdentityError(
            "conversation_owner_missing",
            "An operator-bound queued insight has no canonical owner.",
        )
    return owner_principal_id, operator_session_id


def _bundle_content(items: list[object]) -> str:
    parts = [f"- {item.content}" for item in items]
    return f"While you were away ({len(items)} update{'s' if len(items) != 1 else ''}):\n" + "\n".join(parts)


def _group_native_bundle_items(items: list[object]) -> list[list[object]]:
    session_groups: dict[tuple[str, str, str, int | None, str, str], list[object]] = {}
    ambient_items: list[object] = []
    for item in items:
        session_id = _queued_item_text(item, "session_id")
        budget_goal_id = _queued_item_text(item, "goal_id")
        budget_period_key = _queued_item_text(item, "budget_period_key")
        budget_limit = getattr(item, "budget_limit", None)
        if not isinstance(budget_limit, int) or isinstance(budget_limit, bool):
            budget_limit = None
        owner_principal_id = _queued_item_text(item, "owner_principal_id")
        operator_session_id = _queued_item_text(item, "operator_session_id")
        group_key = (
            session_id,
            budget_goal_id,
            budget_period_key,
            budget_limit,
            owner_principal_id,
            operator_session_id,
        )
        if session_id:
            session_groups.setdefault(group_key, []).append(item)
            continue
        if budget_goal_id:
            session_groups.setdefault(group_key, []).append(item)
        else:
            ambient_items.append(item)

    if len(session_groups) <= 1 and not ambient_items:
        return [items]

    grouped_items = list(session_groups.values())
    if ambient_items:
        grouped_items.append(ambient_items)
    return grouped_items


def _bundle_idempotency_key(items: list[object]) -> str:
    """Derive a stable handoff key from the durable source insight IDs."""
    source_ids = sorted(
        str(item_id)
        for item_id in (getattr(item, "id", None) for item in items)
        if item_id
    )
    if not source_ids:
        # The insight queue normally supplies IDs. An empty set must still be
        # bounded and unique for defensive callers.
        source_ids = ["empty-bundle"]
    digest = hashlib.sha256("|".join(source_ids).encode("utf-8")).hexdigest()
    return f"native_bundle:v1:{digest}"


def _resolve_delivery_identity(
    message: WSResponse,
    *,
    session_id: str | None,
    owner_principal_id: str | None = None,
    operator_session_id: str | None = None,
    trusted_principal=None,
) -> tuple[object | None, str | None, str | None, str | None]:
    """Resolve proactive identity from trusted runtime state.

    Lineage fields on ``WSResponse`` are receipts, not credentials. A bound
    delivery must carry the authenticated owner and conversation scope; a
    caller cannot replace those values by constructing a response object.
    """
    candidates = [
        str(value).strip()
        for value in (session_id, message.conversation_id, message.session_id)
        if isinstance(value, str) and value.strip()
    ]
    if candidates and len(set(candidates)) != 1:
        raise ConversationIdentityError(
            "conversation_session_mismatch",
            "Delivery identity contains conflicting conversation/session ids.",
        )
    requested_conversation_id = candidates[0] if candidates else None

    supplied_owner = str(
        owner_principal_id if owner_principal_id is not None else message.owner_principal_id or ""
    ).strip() or None
    supplied_operator_session = str(
        operator_session_id
        if operator_session_id is not None
        else message.operator_session_id or ""
    ).strip() or None
    trusted_owner = str(getattr(trusted_principal, "principal_id", "") or "").strip() or None
    trusted_operator_session = str(
        getattr(trusted_principal, "operator_session_id", "") or ""
    ).strip() or None
    trusted_conversation = str(getattr(trusted_principal, "session_id", "") or "").strip() or None
    trusted_is_bound_service = (
        trusted_principal is not None
        and str(getattr(getattr(trusted_principal, "principal_type", None), "value", getattr(trusted_principal, "principal_type", ""))).lower()
        == "service"
        and bool(str(getattr(trusted_principal, "job_id", "") or "").strip())
    )

    if trusted_principal is not None and (
        not getattr(trusted_principal, "authenticated", False)
        or getattr(trusted_principal, "revoked", False)
    ):
        raise ConversationIdentityError(
            "conversation_authority_revoked",
            "The runtime principal is not authorized for proactive delivery.",
        )
    if supplied_owner and trusted_principal is not None and supplied_owner != trusted_owner:
        raise ConversationIdentityError(
            "conversation_owner_mismatch",
            "Delivery owner does not match the authenticated runtime principal.",
        )
    if supplied_operator_session and trusted_principal is not None:
        if supplied_operator_session != trusted_operator_session:
            raise ConversationIdentityError(
                "operator_session_mismatch",
                "Delivery operator session does not match the authenticated runtime session.",
            )
    if (
        requested_conversation_id
        and trusted_principal is not None
        and requested_conversation_id != trusted_conversation
    ):
        # APScheduler establishes a service principal before invoking a
        # scheduled job. That envelope is intentionally session-less at the
        # outer wrapper; the job's canonical session argument binds it for the
        # delivery boundary. A user principal can never take this path.
        if trusted_is_bound_service and trusted_conversation is None:
            trusted_conversation = requested_conversation_id
        else:
            raise ConversationIdentityError(
                "conversation_session_mismatch",
                "Delivery conversation does not match the authenticated runtime conversation.",
            )
    if trusted_principal is None and (
        requested_conversation_id
        or supplied_owner
        or supplied_operator_session
        or message.attachment_refs
    ):
        raise ConversationIdentityError(
            "conversation_authority_missing",
            "Bound delivery requires a server-authenticated runtime principal.",
        )

    owner_principal_id = trusted_owner or supplied_owner
    operator_session_id = trusted_operator_session or supplied_operator_session
    if message.attachment_refs:
        # The delivery layer never persists raw attachment objects. This also
        # rejects an attachment that explicitly claims another owner.
        validate_attachment_refs(
            message.attachment_refs,
            owner_principal_id=owner_principal_id,
        )
    if requested_conversation_id and owner_principal_id is None:
        raise ConversationIdentityError(
            "conversation_owner_missing",
            "A session-bound delivery requires an authenticated owner principal.",
        )
    if requested_conversation_id and trusted_principal is not None and trusted_conversation is None:
        raise ConversationIdentityError(
            "conversation_session_missing",
            "A runtime principal without a conversation cannot deliver to a bound session.",
        )

    identity = None
    if requested_conversation_id:
        source_channel = str(message.channel or "web").strip()
        source_transport = str(message.transport or "websocket").strip()
        identity = build_conversation_identity(
            conversation_id=requested_conversation_id,
            thread_id=message.thread_id or requested_conversation_id,
            owner_principal_id=owner_principal_id,
            operator_session_id=operator_session_id,
            device_id=message.device_id,
            channel=source_channel,
            transport=source_transport,
            correlation_id=message.correlation_id,
            causation_id=message.causation_id,
            require_owner=True,
        )
    return identity, requested_conversation_id, owner_principal_id, operator_session_id


def _apply_native_channel_preference(
    *,
    transport_order: list[str],
    message: WSResponse,
    is_scheduled: bool,
    channel_bias: str,
) -> list[str]:
    if (
        channel_bias != "prefer_native_notification"
        or "native_notification" not in transport_order
        or not _should_offer_native_notification(
            message,
            is_scheduled=is_scheduled,
            channel_bias=channel_bias,
        )
    ):
        return transport_order
    if transport_order[0] == "native_notification":
        return transport_order
    return ["native_notification", *[transport for transport in transport_order if transport != "native_notification"]]


def _has_notification_budget(notification_budget: dict[str, object] | None) -> bool:
    """Return whether delivery carries a goal-bound budget assertion.

    Treat a malformed partial binding as bounded work too.  It must fail at the
    native reservation boundary rather than being allowed to escape over the
    unscoped WebSocket broadcast path.
    """

    if not isinstance(notification_budget, dict):
        return False
    return any(
        notification_budget.get(key) is not None
        for key in ("goal_id", "budget_period_key", "budget_limit")
    )


def _queued_item_text(item: object, field: str) -> str:
    value = getattr(item, field, None)
    return value.strip() if isinstance(value, str) else ""


def _is_owner_bound_bundle_item(item: object) -> bool:
    """Return whether a queued item must never enter global WebSocket fanout."""

    if any(
        _queued_item_text(item, field)
        for field in ("session_id", "owner_principal_id", "operator_session_id", "goal_id", "budget_period_key")
    ):
        return True
    budget_limit = getattr(item, "budget_limit", None)
    return isinstance(budget_limit, int) and not isinstance(budget_limit, bool)


def _canonical_delivery_message(
    message: WSResponse,
    *,
    identity,
    requested_conversation_id: str | None,
    owner_principal_id: str | None,
) -> WSResponse:
    """Attach canonical lineage and safe attachment refs to a transport frame."""
    updates: dict[str, object] = {}
    if identity is not None:
        updates.update(
            {
                "session_id": identity.conversation_id or requested_conversation_id or "",
                "conversation_id": identity.conversation_id or "",
                "thread_id": identity.thread_id or "",
                "owner_principal_id": identity.owner_principal_id,
                "operator_session_id": identity.operator_session_id,
                "channel": identity.channel,
                "transport": identity.transport,
                "device_id": identity.device_id,
                "correlation_id": identity.correlation_id,
                "causation_id": identity.causation_id,
            }
        )
    if message.attachment_refs:
        updates["attachment_refs"] = validate_attachment_refs(
            message.attachment_refs,
            owner_principal_id=owner_principal_id,
        )
    return message.model_copy(update=updates) if updates else message


def _should_offer_native_notification(
    message: WSResponse,
    *,
    is_scheduled: bool,
    channel_bias: str = "neutral",
) -> bool:
    if message.type != "proactive":
        return False
    if bool(message.requires_approval):
        return False
    if not message.content.strip():
        return False
    if channel_bias == "prefer_native_notification":
        return True
    if is_scheduled:
        return True
    if message.intervention_type == "alert":
        return True
    return (message.urgency or 0) >= 3


def _native_notification_title(message: WSResponse, *, is_scheduled: bool) -> str:
    if message.intervention_type == "alert":
        return "Seraph alert"
    if is_scheduled:
        return "Seraph update"
    return "Seraph"


async def _create_intervention_record(
    *,
    session_id: str | None,
    message: WSResponse,
    is_scheduled: bool,
    guardian_confidence: str | None,
    user_state: str,
    active_project: str | None,
    interruption_mode: str,
    data_quality: str,
    policy_decision: InterventionDecision,
) -> str | None:
    from src.guardian.feedback import guardian_feedback_repository

    initial_outcome = "pending" if policy_decision.action.value == "act" else policy_decision.audit_decision
    try:
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=session_id,
            message_type=message.type,
            intervention_type=message.intervention_type,
            urgency=message.urgency,
            content=message.content,
            reasoning=message.reasoning,
            is_scheduled=is_scheduled,
            guardian_confidence=guardian_confidence,
            data_quality=data_quality,
            user_state=user_state,
            active_project=active_project,
            interruption_mode=interruption_mode,
            policy_action=policy_decision.action.value,
            policy_reason=policy_decision.reason,
            delivery_decision=(
                policy_decision.delivery_decision.value
                if policy_decision.delivery_decision is not None
                else None
            ),
            latest_outcome=initial_outcome,
        )
        return intervention.id
    except Exception:
        logger.debug("Failed to persist guardian intervention record", exc_info=True)
        return None


async def _update_intervention_outcome(
    intervention_id: str | None,
    *,
    latest_outcome: str,
    transport: str | None = None,
    notification_id: str | None = None,
) -> None:
    if not intervention_id:
        return
    from src.guardian.feedback import guardian_feedback_repository

    try:
        await guardian_feedback_repository.update_outcome(
            intervention_id,
            latest_outcome=latest_outcome,
            transport=transport,
            notification_id=notification_id,
        )
    except Exception:
        logger.debug("Failed to update guardian intervention outcome", exc_info=True)


async def deliver_or_queue(
    message: WSResponse,
    is_scheduled: bool = False,
    *,
    guardian_confidence: str | None = None,
    session_id: str | None = None,
    owner_principal_id: str | None = None,
    operator_session_id: str | None = None,
    notification_budget: dict[str, object] | None = None,
) -> InterventionDecision:
    """Route a proactive message through the delivery gate.

    Reads current context, makes an explicit intervention-policy decision,
    and executes the resulting delivery/bundle/silence path.
    """
    from src.observer.manager import context_manager
    from src.observer.insight_queue import insight_queue
    from src.scheduler.connection_manager import ws_manager
    from src.guardian.feedback import GuardianLearningSignal, guardian_feedback_repository
    from src.guardian.learning_arbitration import arbitrate_learning_signal
    from src.memory.procedural_guidance import load_procedural_memory_guidance

    ctx = context_manager.get_context()
    # A proactive message may be created by an authenticated interactive turn
    # or by an ambient scheduler. Bound identity is resolved before any
    # intervention or delivery receipt is persisted.
    trusted_principal = _current_trust_principal()
    # The scheduler wrapper carries a service job envelope before it knows the
    # conversation's user owner. Resolve that owner from the canonical session
    # row before identity validation; never let the scheduled action's payload
    # supply it. This also gives the native outbox the same owner fence as the
    # browser/WebSocket surface.
    principal_for_delivery = trusted_principal
    trusted_type = str(
        getattr(getattr(trusted_principal, "principal_type", None), "value", getattr(trusted_principal, "principal_type", ""))
    ).lower()
    if (
        trusted_principal is not None
        and trusted_type == "service"
        and str(getattr(trusted_principal, "job_id", "") or "").strip()
        and (
            session_id
            or message.conversation_id
            or message.session_id
            or owner_principal_id
            or operator_session_id
            or notification_budget
        )
    ):
        service_session_id = str(session_id or message.conversation_id or message.session_id or "").strip()
        canonical_owner = str(owner_principal_id or message.owner_principal_id or "").strip()
        canonical_operator_session = str(
            operator_session_id or message.operator_session_id or ""
        ).strip()
        if service_session_id:
            from src.agent.session import session_manager

            canonical_session = await session_manager.get(service_session_id)
            canonical_owner = str(getattr(canonical_session, "owner_principal_id", "") or "").strip()
            if canonical_session is None or not canonical_owner:
                raise ConversationIdentityError(
                    "conversation_owner_missing",
                    "Scheduled delivery requires an owner on the canonical conversation session.",
                )
        if not canonical_owner and (notification_budget or canonical_operator_session):
            raise ConversationIdentityError(
                "conversation_owner_missing",
                "Scheduled delivery requires a canonical goal owner.",
            )
        principal_for_delivery = replace(
            trusted_principal,
            principal_id=canonical_owner or trusted_principal.principal_id,
            session_id=service_session_id,
            operator_session_id=canonical_operator_session,
        )
    identity, requested_conversation_id, owner_principal_id, operator_session_id = (
        _resolve_delivery_identity(
            message,
            session_id=session_id,
            owner_principal_id=owner_principal_id,
            operator_session_id=operator_session_id,
            trusted_principal=principal_for_delivery,
        )
    )
    # Normalize once at the transport boundary. Persisted receipts and WS
    # frames contain only the signed canonical metadata; the ingress bearer
    # token is never forwarded to a daemon or browser.
    delivery_message = _canonical_delivery_message(
        message,
        identity=identity,
        requested_conversation_id=requested_conversation_id,
        owner_principal_id=owner_principal_id,
    )
    # Preserve one normalized session id for all downstream paths.
    session_id = requested_conversation_id
    active_channel_adapters = _active_channel_adapters()
    intervention_type = message.intervention_type or message.type
    urgency = message.urgency or 0
    policy_decision: InterventionDecision | None = None
    learning_signal = GuardianLearningSignal.neutral(intervention_type)
    effective_learning_signal = learning_signal
    learning_signal_source = "heuristic_only"
    live_learning_scope = "global"
    live_learning_scope_axes: dict[str, str] = {}
    procedural_lesson_types: tuple[str, ...] = ()
    learning_arbitration_sources: dict[str, str] = {}
    learning_arbitration_reasons: dict[str, str] = {}
    learning_arbitration_weights: dict[str, float] = {}

    try:
        try:
            live_learning_resolution = await guardian_feedback_repository.resolve_learning_signal(
                intervention_type=intervention_type,
                limit=12,
                session_id=session_id,
                active_project=ctx.active_project,
            )
            learning_signal = live_learning_resolution.effective_signal
            live_learning_scope = live_learning_resolution.dominant_scope
            live_learning_scope_axes = live_learning_resolution.selected_scopes()
        except Exception:
            logger.debug("Failed to compute guardian learning signal", exc_info=True)
        try:
            procedural_guidance = await load_procedural_memory_guidance(
                intervention_type,
                continuity_thread_id=session_id,
                active_project=ctx.active_project,
            )
            arbitration = arbitrate_learning_signal(
                live_signal=learning_signal,
                procedural_guidance=procedural_guidance,
            )
            effective_learning_signal = arbitration.effective_signal
            learning_signal_source = arbitration.source_label
            procedural_lesson_types = procedural_guidance.lesson_types
            learning_arbitration_sources = arbitration.selected_sources()
            learning_arbitration_reasons = arbitration.selected_reasons()
            learning_arbitration_weights = arbitration.selected_weights()
        except Exception:
            logger.debug("Failed to load procedural learning guidance", exc_info=True)
            effective_learning_signal = learning_signal

        policy_decision = decide_intervention(
            message_type=message.type,
            intervention_type=intervention_type,
            content=message.content,
            urgency=urgency,
            user_state=ctx.user_state,
            interruption_mode=ctx.interruption_mode,
            attention_budget_remaining=ctx.attention_budget_remaining,
            is_scheduled=is_scheduled,
            data_quality=ctx.data_quality,
            guardian_confidence=guardian_confidence,
            observer_confidence=ctx.observer_confidence,
            salience_level=ctx.salience_level,
            salience_reason=ctx.salience_reason,
            interruption_cost=ctx.interruption_cost,
            requires_approval=bool(message.requires_approval),
            recent_feedback_bias=effective_learning_signal.bias,
            learning_phrasing_bias=effective_learning_signal.phrasing_bias,
            learning_cadence_bias=effective_learning_signal.cadence_bias,
            learning_channel_bias=effective_learning_signal.channel_bias,
            learning_escalation_bias=effective_learning_signal.escalation_bias,
            learning_timing_bias=effective_learning_signal.timing_bias,
            learning_blocked_state_bias=effective_learning_signal.blocked_state_bias,
            learning_suppression_bias=effective_learning_signal.suppression_bias,
            learning_thread_preference_bias=effective_learning_signal.thread_preference_bias,
            learning_multi_day_positive_days=effective_learning_signal.multi_day_positive_days,
            learning_multi_day_negative_days=effective_learning_signal.multi_day_negative_days,
            learning_scheduled_positive_days=effective_learning_signal.scheduled_positive_days,
            learning_scheduled_negative_days=effective_learning_signal.scheduled_negative_days,
        )

        event_details = {
            "user_state": ctx.user_state,
            "interruption_mode": ctx.interruption_mode,
            "attention_budget_remaining": ctx.attention_budget_remaining,
            "data_quality": ctx.data_quality,
            "observer_confidence": ctx.observer_confidence,
            "salience_level": ctx.salience_level,
            "salience_reason": ctx.salience_reason,
            "interruption_cost": ctx.interruption_cost,
            "guardian_confidence": guardian_confidence,
            "learning_signal_source": learning_signal_source,
            "live_learning_scope": live_learning_scope,
            "live_learning_scope_axes": live_learning_scope_axes,
            "learning_bias": effective_learning_signal.bias,
            "learning_phrasing_bias": effective_learning_signal.phrasing_bias,
            "learning_cadence_bias": effective_learning_signal.cadence_bias,
            "learning_channel_bias": effective_learning_signal.channel_bias,
            "learning_escalation_bias": effective_learning_signal.escalation_bias,
            "learning_timing_bias": effective_learning_signal.timing_bias,
            "learning_blocked_state_bias": effective_learning_signal.blocked_state_bias,
            "learning_suppression_bias": effective_learning_signal.suppression_bias,
            "learning_thread_preference_bias": effective_learning_signal.thread_preference_bias,
            "learning_helpful_count": learning_signal.helpful_count,
            "learning_not_helpful_count": learning_signal.not_helpful_count,
            "learning_acknowledged_count": learning_signal.acknowledged_count,
            "learning_failed_count": learning_signal.failed_count,
            "learning_multi_day_positive_days": learning_signal.multi_day_positive_days,
            "learning_multi_day_negative_days": learning_signal.multi_day_negative_days,
            "learning_scheduled_positive_days": learning_signal.scheduled_positive_days,
            "learning_scheduled_negative_days": learning_signal.scheduled_negative_days,
            "learning_arbitration_mode": "evidence_weighted",
            "learning_arbitration_sources": learning_arbitration_sources,
            "learning_arbitration_reasons": learning_arbitration_reasons,
            "learning_arbitration_weights": learning_arbitration_weights,
            "policy_action": policy_decision.action.value,
            "policy_reason": policy_decision.reason,
            "notification_budget_bound": _has_notification_budget(notification_budget),
            "notification_budget_goal_id": (
                notification_budget.get("goal_id")
                if isinstance(notification_budget, dict)
                else None
            ),
        }
        intervention_id = await _create_intervention_record(
            session_id=session_id,
            message=message,
            is_scheduled=is_scheduled,
            guardian_confidence=guardian_confidence,
            user_state=ctx.user_state,
            active_project=ctx.active_project,
            interruption_mode=ctx.interruption_mode,
            data_quality=ctx.data_quality,
            policy_decision=policy_decision,
        )
        message.intervention_id = intervention_id
        if intervention_id is not None:
            # The canonical transport copy is made before the intervention
            # row exists; carry its durable id into the actual frame/receipt.
            delivery_message = delivery_message.model_copy(
                update={"intervention_id": intervention_id}
            )
        if intervention_id is not None:
            event_details["intervention_id"] = intervention_id
        if procedural_lesson_types:
            event_details["procedural_learning_lesson_types"] = list(procedural_lesson_types)

        if policy_decision.action.value == "act":
            route_name = _delivery_route_name(message=message, is_scheduled=is_scheduled)
            route_binding, transport_order = _route_transport_order(
                route_name,
                active_channel_adapters=active_channel_adapters,
            )
            adjusted_transport_order = _apply_native_channel_preference(
                transport_order=transport_order,
                message=message,
                is_scheduled=is_scheduled,
                channel_bias=effective_learning_signal.channel_bias,
            )
            event_details.update(
                {
                    "active_channel_adapters": sorted(active_channel_adapters),
                    "channel_route": route_binding["route"],
                    "primary_transport": route_binding["primary_transport"],
                    "fallback_transport": route_binding["fallback_transport"],
                    "configured_transport_order": route_binding.get("configured_order") or [],
                    "route_status": route_binding.get("status"),
                    "route_summary": route_binding.get("summary"),
                    "route_failure_reason": route_binding.get("failure_reason"),
                    "route_repair_hint": route_binding.get("repair_hint"),
                    "transport_statuses": route_binding.get("transports") or [],
                    "transport_order": adjusted_transport_order,
                }
            )
            if adjusted_transport_order != transport_order:
                event_details["transport_order_adjustment"] = "learned_native_channel_preference"
            goal_bound_notification = _has_notification_budget(notification_budget)
            if goal_bound_notification:
                # A budgeted goal notification must reserve its native outbox
                # row before delivery. WebSocket has no reservation boundary,
                # so it can never be a primary or fallback transport here.
                transport_order = ["native_notification"]
                event_details["notification_budget_transport_fence"] = "native_notification_only"
                event_details["transport_order"] = transport_order
            else:
                transport_order = adjusted_transport_order

            last_error = (
                str(route_binding.get("failure_reason"))
                if isinstance(route_binding.get("failure_reason"), str) and route_binding.get("failure_reason")
                else _route_disabled_error(primary_transport=route_binding["primary_transport"])
            )
            for transport in transport_order:
                if transport == "websocket":
                    websocket_enabled = "websocket" in active_channel_adapters
                    if websocket_enabled:
                        broadcast_result = await ws_manager.broadcast(delivery_message)
                    else:
                        from src.scheduler.connection_manager import BroadcastResult

                        broadcast_result = BroadcastResult(
                            attempted_connections=0,
                            delivered_connections=0,
                            failed_connections=0,
                        )
                    event_details.update(
                        {
                            "attempted_connections": broadcast_result.attempted_connections,
                            "delivered_connections": broadcast_result.delivered_connections,
                            "failed_connections": broadcast_result.failed_connections,
                        }
                    )
                    if broadcast_result.delivered_connections > 0:
                        if policy_decision.should_cost_budget:
                            context_manager.decrement_attention_budget()
                        logger.info("Delivered proactive message (type=%s)", message.type)
                        await _update_intervention_outcome(
                            intervention_id,
                            latest_outcome="delivered",
                            transport="websocket",
                        )
                        await log_observer_delivery_event(
                            decision="delivered",
                            message_type=message.type,
                            intervention_type=intervention_type,
                            urgency=urgency,
                            is_scheduled=is_scheduled,
                            details={**event_details, "transport": "websocket"},
                        )
                        return policy_decision
                    last_error = _prefer_delivery_error(
                        last_error,
                        _transport_failure_reason(
                        attempted_connections=broadcast_result.attempted_connections,
                        failed_connections=broadcast_result.failed_connections,
                        websocket_enabled=websocket_enabled,
                        ),
                    )
                    continue

                if transport == "native_notification":
                    if not context_manager.is_daemon_connected():
                        last_error = _prefer_delivery_error(last_error, "daemon_unavailable")
                        continue
                    try:
                        native_principal = _delegated_native_principal(
                            principal_for_delivery,
                            owner_principal_id=owner_principal_id,
                            session_id=session_id,
                            operator_session_id=operator_session_id,
                        )
                        with _native_delivery_runtime(native_principal):
                            notification = await native_notification_queue.enqueue(
                                intervention_id=intervention_id,
                                title=_native_notification_title(message, is_scheduled=is_scheduled),
                                body=message.content,
                                intervention_type=intervention_type,
                                urgency=urgency,
                                surface=(
                                    "action_card"
                                    if effective_learning_signal.escalation_bias == "prefer_async_native"
                                    else "notification"
                                ),
                                session_id=session_id,
                                thread_id=session_id,
                                thread_source="session" if session_id else "ambient",
                                continuation_mode="resume_thread" if session_id else "open_thread",
                                resume_message=f"Continue from this guardian intervention: {message.content}",
                                owner_principal_id=owner_principal_id,
                                operator_session_id=operator_session_id,
                                device_id=message.device_id,
                                channel="native_notification",
                                transport="native_notification",
                                conversation_id=identity.conversation_id if identity is not None else None,
                                correlation_id=message.correlation_id,
                                causation_id=message.causation_id,
                                attachment_refs=delivery_message.attachment_refs,
                                goal_id=(notification_budget or {}).get("goal_id"),
                                budget_period_key=(notification_budget or {}).get("budget_period_key"),
                                budget_limit=(notification_budget or {}).get("budget_limit"),
                            )
                    except NativeNotificationBudgetDenied as exc:
                        last_error = _prefer_delivery_error(last_error, str(exc))
                        event_details.update(
                            {
                                "notification_budget_denied": True,
                                "notification_budget_goal_id": exc.goal_id,
                                "notification_budget_period_key": exc.budget_period_key,
                                "notification_budget_limit": exc.budget_limit,
                                "notification_budget_partial_reservation": False,
                            }
                        )
                        # A denied reservation is a bounded failure. Never
                        # turn it into an unreserved WebSocket delivery.
                        if goal_bound_notification:
                            break
                        continue
                    context_manager.record_native_notification(
                        title=notification.title,
                        outcome="queued",
                    )
                    event_details.update(
                        {
                            "attempted_connections": 1,
                            "delivered_connections": 0,
                            "failed_connections": 0,
                            "queued_connections": 1,
                        }
                    )
                    await _update_intervention_outcome(
                        intervention_id,
                        latest_outcome="queued",
                        transport="native_notification",
                        notification_id=notification.id,
                    )
                    logger.info(
                        "Queued proactive message for native notification (type=%s, notification_id=%s)",
                        message.type,
                        notification.id,
                    )
                    await log_observer_delivery_event(
                        decision="queued",
                        message_type=message.type,
                        intervention_type=intervention_type,
                        urgency=urgency,
                        is_scheduled=is_scheduled,
                        details={
                            **event_details,
                            "transport": "native_notification",
                            "notification_id": notification.id,
                            "delivery_decision": policy_decision.delivery_decision.value
                            if policy_decision.delivery_decision is not None
                            else None,
                        },
                    )
                    return policy_decision

            logger.warning(
                "Failed to deliver proactive message (type=%s, route=%s, error=%s)",
                message.type,
                route_name,
                last_error,
            )
            await _update_intervention_outcome(
                intervention_id,
                latest_outcome="failed",
                transport=transport_order[0] if transport_order else None,
            )
            await log_observer_delivery_event(
                decision="failed",
                message_type=message.type,
                intervention_type=intervention_type,
                urgency=urgency,
                is_scheduled=is_scheduled,
                details={
                    **event_details,
                    "transport": transport_order[0] if transport_order else None,
                    "delivery_decision": policy_decision.delivery_decision.value
                    if policy_decision.delivery_decision is not None
                    else None,
                    "error": last_error,
                },
            )
            return policy_decision

        elif policy_decision.action.value == "bundle":
            insight_kwargs = {
                "content": message.content,
                "intervention_type": intervention_type,
                "urgency": urgency,
                "reasoning": message.reasoning or "",
                "intervention_id": intervention_id,
                "session_id": session_id,
            }
            if session_id is not None or owner_principal_id is not None or operator_session_id is not None:
                insight_kwargs["owner_principal_id"] = owner_principal_id
                insight_kwargs["operator_session_id"] = operator_session_id
            if notification_budget:
                insight_kwargs.update(notification_budget)
            await insight_queue.enqueue(
                **insight_kwargs,
            )
            logger.info("Queued proactive message (state=%s, mode=%s)", ctx.user_state, ctx.interruption_mode)
            await log_observer_delivery_event(
                decision="queued",
                message_type=message.type,
                intervention_type=intervention_type,
                urgency=urgency,
                is_scheduled=is_scheduled,
                details=event_details,
            )

        else:
            logger.info(
                "Suppressed proactive message (type=%s, action=%s, reason=%s)",
                message.type,
                policy_decision.action.value,
                policy_decision.reason,
            )
            await log_observer_delivery_event(
                decision=policy_decision.audit_decision,
                message_type=message.type,
                intervention_type=intervention_type,
                urgency=urgency,
                is_scheduled=is_scheduled,
                details=event_details,
            )

        return policy_decision
    except Exception as exc:
        await _update_intervention_outcome(
            locals().get("intervention_id"),
            latest_outcome="failed",
        )
        await log_observer_delivery_event(
            decision="failed",
            message_type=message.type,
            intervention_type=intervention_type,
            urgency=urgency,
            is_scheduled=is_scheduled,
            details={
                "user_state": ctx.user_state,
                "interruption_mode": ctx.interruption_mode,
                "attention_budget_remaining": ctx.attention_budget_remaining,
                "data_quality": ctx.data_quality,
                "guardian_confidence": guardian_confidence,
                "policy_action": policy_decision.action.value if policy_decision is not None else None,
                "policy_reason": policy_decision.reason if policy_decision is not None else None,
                "delivery_decision": (
                    policy_decision.delivery_decision.value
                    if policy_decision is not None and policy_decision.delivery_decision is not None
                    else None
                ),
                "error": str(exc),
            },
        )
        raise


async def deliver_queued_bundle() -> int:
    """Drain the insight queue and deliver as a bundle message.

    Called on state transitions from blocked → unblocked.
    Returns the number of items delivered.
    """
    from src.observer.insight_queue import insight_queue
    from src.observer.manager import context_manager
    from src.scheduler.connection_manager import ws_manager

    items = await insight_queue.peek_all()
    if not items:
        return 0
    active_channel_adapters = _active_channel_adapters()
    route_binding, transport_order = _route_transport_order(
        "bundle_delivery",
        active_channel_adapters=active_channel_adapters,
    )
    bundle_content = _bundle_content(items)
    native_bundle_groups = _group_native_bundle_items(items)
    group_continuations = [
        _bundle_continuation_payload(group, bundle_content=_bundle_content(group))
        for group in native_bundle_groups
    ]
    details = {
        "bundle_item_count": len(items),
        "intervention_ids": [item.intervention_id for item in items if item.intervention_id],
        "delivery_decision": "deliver",
        "active_channel_adapters": sorted(active_channel_adapters),
        "channel_route": route_binding["route"],
        "primary_transport": route_binding["primary_transport"],
        "fallback_transport": route_binding["fallback_transport"],
        "configured_transport_order": route_binding.get("configured_order") or [],
        "route_status": route_binding.get("status"),
        "route_summary": route_binding.get("summary"),
        "route_failure_reason": route_binding.get("failure_reason"),
        "route_repair_hint": route_binding.get("repair_hint"),
        "transport_statuses": route_binding.get("transports") or [],
        "transport_order": transport_order,
        "bundle_group_count": len(native_bundle_groups),
        "bundle_thread_ids": [item["thread_id"] for item in group_continuations if item.get("thread_id")],
        "bundle_continuation_modes": [
            str(item.get("continuation_mode") or "open_thread")
            for item in group_continuations
        ],
    }
    message = WSResponse(
        type="proactive",
        content=bundle_content,
        intervention_type="proactive_bundle",
        urgency=3,
        reasoning=f"Bundle of {len(items)} queued insight(s) handed off to the durable native outbox",
    )

    last_error = (
        str(route_binding.get("failure_reason"))
        if isinstance(route_binding.get("failure_reason"), str) and route_binding.get("failure_reason")
        else _route_disabled_error(primary_transport=route_binding["primary_transport"])
    )
    owner_bound_items = any(_is_owner_bound_bundle_item(item) for item in items)
    for transport in transport_order:
        if transport == "native_notification":
            if not context_manager.is_daemon_connected():
                last_error = _prefer_delivery_error(last_error, "daemon_unavailable")
                continue
            notifications = []
            denied_groups: list[dict[str, object]] = []
            owner_binding_failures: list[dict[str, object]] = []
            for group_items in native_bundle_groups:
                group_content = _bundle_content(group_items)
                group_continuation = _bundle_continuation_payload(group_items, bundle_content=group_content)
                source_insight_ids = [
                    str(item.id)
                    for item in group_items
                    if getattr(item, "id", None)
                ]
                first_item = group_items[0] if group_items else None
                try:
                    group_owner_principal_id, group_operator_session_id = await _bundle_owner_binding(
                        group_items
                    )
                except ConversationIdentityError as exc:
                    last_error = _prefer_delivery_error(last_error, exc.code)
                    owner_binding_failures.append(
                        {
                            "source_insight_ids": source_insight_ids,
                            "reason": exc.code,
                        }
                    )
                    continue
                group_goal_id = _queued_item_text(first_item, "goal_id") if first_item is not None else ""
                group_budget_period_key = (
                    _queued_item_text(first_item, "budget_period_key") if first_item is not None else ""
                )
                group_budget_limit = getattr(first_item, "budget_limit", None) if first_item is not None else None
                if not isinstance(group_budget_limit, int) or isinstance(group_budget_limit, bool):
                    group_budget_limit = None
                try:
                    native_principal = _delegated_native_principal(
                        _current_trust_principal(),
                        owner_principal_id=group_owner_principal_id,
                        session_id=group_continuation["session_id"],
                        operator_session_id=group_operator_session_id,
                    )
                    with _native_delivery_runtime(native_principal):
                        notification = await native_notification_queue.enqueue(
                            intervention_id=None,
                            title="Seraph update",
                            body=group_content,
                            intervention_type="proactive_bundle",
                            urgency=3,
                            surface="action_card",
                            session_id=group_continuation["session_id"],
                            thread_id=group_continuation["thread_id"],
                            thread_source=str(group_continuation["thread_source"] or "ambient"),
                            continuation_mode=str(group_continuation["continuation_mode"] or "open_thread"),
                            resume_message=group_continuation["resume_message"],
                            idempotency_key=_bundle_idempotency_key(group_items),
                            owner_principal_id=group_owner_principal_id,
                            operator_session_id=group_operator_session_id,
                            source_insight_ids=source_insight_ids,
                            goal_id=group_goal_id or None,
                            budget_period_key=group_budget_period_key or None,
                            budget_limit=group_budget_limit,
                        )
                except NativeNotificationBudgetDenied as exc:
                    last_error = _prefer_delivery_error(last_error, str(exc))
                    denied_groups.append(
                        {
                            "goal_id": exc.goal_id,
                            "budget_period_key": exc.budget_period_key,
                            "budget_limit": exc.budget_limit,
                            "source_insight_ids": source_insight_ids,
                        }
                    )
                    continue
                context_manager.record_native_notification(
                    title=notification.title,
                    outcome="queued",
                )
                notifications.append((notification, group_items))
            for notification, group_items in notifications:
                for item in group_items:
                    await _update_intervention_outcome(
                        item.intervention_id,
                        latest_outcome="bundle_queued",
                        transport="native_notification_bundle",
                        notification_id=notification.id,
                    )
            queued_item_count = sum(len(group_items) for _, group_items in notifications)
            denied_item_count = sum(
                len(group.get("source_insight_ids") or []) for group in denied_groups
            )
            details.update(
                {
                    "attempted_connections": len(notifications),
                    "delivered_connections": 0,
                    "failed_connections": len(owner_binding_failures),
                    "queued_connections": len(notifications),
                    "queued_item_count": queued_item_count,
                    "notification_budget_denied_item_count": denied_item_count,
                    "notification_budget_denied": bool(denied_groups),
                    "notification_budget_partial_reservation": bool(
                        denied_groups and notifications
                    ),
                    "notification_budget_denied_groups": denied_groups,
                    "recoverable_source_insight_ids": [
                        source_id
                        for group in denied_groups
                        for source_id in group.get("source_insight_ids") or []
                    ],
                    "owner_binding_failures": owner_binding_failures,
                }
            )
            if notifications:
                await log_observer_delivery_event(
                    decision="queued",
                    message_type="proactive",
                    intervention_type="proactive_bundle",
                    urgency=3,
                    is_scheduled=False,
                    details={
                        **details,
                        "transport": "native_notification",
                        "notification_id": notifications[0][0].id,
                        "notification_ids": [notification.id for notification, _ in notifications],
                        "queue_retained": bool(denied_groups or owner_binding_failures),
                    },
                )
                return queued_item_count

            await log_observer_delivery_event(
                decision="failed",
                message_type="proactive",
                intervention_type="proactive_bundle",
                urgency=3,
                is_scheduled=False,
                details={
                    **details,
                    "transport": "native_notification",
                    "notification_id": None,
                    "notification_ids": [],
                    "queue_retained": True,
                },
            )
            return 0

        if transport == "websocket":
            if owner_bound_items:
                # ConnectionManager broadcasts globally and has no per-session
                # recipient fence. A bound bundle must stay on the owner-aware
                # native outbox until a scoped WebSocket transport exists.
                last_error = _prefer_delivery_error(
                    last_error,
                    "bound_bundle_requires_owner_scoped_transport",
                )
                continue
            broadcast_result = await ws_manager.broadcast(message)
            details.update(
                {
                    "attempted_connections": broadcast_result.attempted_connections,
                    "delivered_connections": broadcast_result.delivered_connections,
                    "failed_connections": broadcast_result.failed_connections,
                }
            )
            if broadcast_result.delivered_connections > 0:
                await insight_queue.delete_many(
                    [item.id for item in items if getattr(item, "id", None)]
                )
                for item in items:
                    await _update_intervention_outcome(
                        item.intervention_id,
                        latest_outcome="bundle_delivered",
                        transport="websocket_bundle",
                    )
                logger.info("Delivered bundle of %d queued insight(s)", len(items))
                await log_observer_delivery_event(
                    decision="delivered",
                    message_type=message.type,
                    intervention_type=message.intervention_type,
                    urgency=message.urgency,
                    is_scheduled=False,
                    details={**details, "transport": "websocket"},
                )
                return len(items)
            last_error = _prefer_delivery_error(
                last_error,
                _transport_failure_reason(
                attempted_connections=broadcast_result.attempted_connections,
                failed_connections=broadcast_result.failed_connections,
                websocket_enabled=True,
                ),
            )

    await log_observer_delivery_event(
        decision="failed",
        message_type=message.type,
        intervention_type=message.intervention_type,
        urgency=message.urgency,
        is_scheduled=False,
        details={
            **details,
            "queue_retained": True,
            "transport": transport_order[0] if transport_order else None,
            "error": last_error,
        },
    )
    return 0
