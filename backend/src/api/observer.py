"""Observer API — context state and daemon integration endpoints."""

import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from src.audit.runtime import log_integration_event
from src.agent.session import session_manager
from src.observer.manager import context_manager
from src.observer.native_notification_queue import native_notification_queue

logger = logging.getLogger(__name__)

router = APIRouter()


class ScreenObservationData(BaseModel):
    app: str | None = None
    window_title: str | None = None
    activity: str | None = None
    project: str | None = None
    summary: str | None = None
    details: list[str] | None = None
    blocked: bool = False


class ScreenContextRequest(BaseModel):
    active_window: str | None = None
    screen_context: str | None = None
    observation: ScreenObservationData | None = None
    switch_timestamp: float | None = None


class NativeNotificationResponse(BaseModel):
    id: str
    intervention_id: str | None = None
    title: str
    body: str
    intervention_type: str | None = None
    urgency: int | None = None
    surface: str = "notification"
    session_id: str | None = None
    thread_id: str | None = None
    thread_label: str | None = None
    resume_message: str | None = None
    created_at: str


class NativeNotificationPollResponse(BaseModel):
    notification: NativeNotificationResponse | None = None


class NotificationAckResponse(BaseModel):
    acked: bool


class NativeNotificationListResponse(BaseModel):
    notifications: list[NativeNotificationResponse]
    pending_count: int


class NotificationDismissResponse(BaseModel):
    dismissed: bool


class NotificationDismissAllResponse(BaseModel):
    dismissed_count: int


class DaemonStatusResponse(BaseModel):
    connected: bool
    last_post: float | None
    active_window: str | None
    has_screen_context: bool
    capture_mode: str
    pending_notification_count: int
    last_native_notification_at: str | None = None
    last_native_notification_title: str | None = None
    last_native_notification_outcome: str | None = None


class QueuedInsightResponse(BaseModel):
    id: str
    intervention_id: str | None = None
    content_excerpt: str
    intervention_type: str
    urgency: int
    reasoning: str
    thread_id: str | None = None
    thread_label: str | None = None
    created_at: str


class RecentInterventionResponse(BaseModel):
    id: str
    session_id: str | None = None
    intervention_type: str
    content_excerpt: str
    policy_action: str
    policy_reason: str
    delivery_decision: str | None = None
    latest_outcome: str
    transport: str | None = None
    notification_id: str | None = None
    feedback_type: str | None = None
    thread_id: str | None = None
    thread_label: str | None = None
    updated_at: str
    continuity_surface: str


class ObserverContinuityResponse(BaseModel):
    daemon: DaemonStatusResponse
    notifications: list[NativeNotificationResponse]
    queued_insights: list[QueuedInsightResponse]
    queued_insight_count: int
    recent_interventions: list[RecentInterventionResponse]


class InterventionFeedbackRequest(BaseModel):
    feedback_type: str
    note: str | None = None


class InterventionFeedbackResponse(BaseModel):
    recorded: bool


@router.get("/observer/state")
async def get_observer_state():
    """Return the current context snapshot."""
    return context_manager.get_context().to_dict()


@router.post("/observer/context")
async def post_screen_context(body: ScreenContextRequest):
    """Receive screen context from native daemon."""
    context_manager.update_screen_context(body.active_window, body.screen_context)
    await log_integration_event(
        integration_type="observer_daemon",
        name="screen_context",
        outcome="received",
        details={
            "has_active_window": body.active_window is not None,
            "has_screen_context": body.screen_context is not None,
            "has_observation": body.observation is not None,
            "blocked": body.observation.blocked if body.observation is not None else None,
        },
    )

    # Persist structured observation if present
    if body.observation is not None:
        try:
            from src.observer.screen_repository import screen_observation_repo

            obs_data = body.observation
            timestamp = (
                datetime.fromtimestamp(body.switch_timestamp, tz=timezone.utc)
                if body.switch_timestamp
                else None
            )

            await screen_observation_repo.create(
                app_name=obs_data.app or "",
                window_title=obs_data.window_title or "",
                activity_type=obs_data.activity or "other",
                project=obs_data.project,
                summary=obs_data.summary,
                details=obs_data.details,
                blocked=obs_data.blocked,
                timestamp=timestamp,
            )
            await log_integration_event(
                integration_type="observer_daemon",
                name="screen_context",
                outcome="persisted",
                details={
                    "app": obs_data.app or "",
                    "activity_type": obs_data.activity or "other",
                    "blocked": obs_data.blocked,
                    "has_switch_timestamp": body.switch_timestamp is not None,
                },
            )
        except Exception as exc:
            await log_integration_event(
                integration_type="observer_daemon",
                name="screen_context",
                outcome="persist_failed",
                details={
                    "app": body.observation.app or "",
                    "activity_type": body.observation.activity or "other",
                    "blocked": body.observation.blocked,
                    "error": str(exc),
                },
            )
            logger.exception("Failed to persist screen observation")

    return {"status": "ok"}


@router.get("/observer/daemon-status", response_model=DaemonStatusResponse)
async def daemon_status():
    """Return daemon connectivity status based on heartbeat timestamp."""
    return await _daemon_status_payload()


def _continuity_surface(*, latest_outcome: str, transport: str | None, policy_action: str) -> str:
    if transport == "native_notification" or latest_outcome.startswith("notification_"):
        return "native_notification"
    if policy_action == "bundle" or latest_outcome.startswith("bundle") or latest_outcome == "queued":
        return "bundle_queue"
    if latest_outcome == "failed":
        return "delivery_failed"
    return "browser"


async def _daemon_status_payload() -> dict[str, str | int | float | bool | None]:
    ctx = context_manager.get_context()
    connected = context_manager.is_daemon_connected()
    pending_notification_count = await native_notification_queue.count()
    return {
        "connected": connected,
        "last_post": ctx.last_daemon_post,
        "active_window": ctx.active_window,
        "has_screen_context": bool(ctx.screen_context),
        "capture_mode": ctx.capture_mode,
        "pending_notification_count": pending_notification_count,
        "last_native_notification_at": (
            ctx.last_native_notification_at.isoformat()
            if ctx.last_native_notification_at is not None
            else None
        ),
        "last_native_notification_title": ctx.last_native_notification_title,
        "last_native_notification_outcome": ctx.last_native_notification_outcome,
    }


@router.get("/observer/continuity", response_model=ObserverContinuityResponse)
async def get_observer_continuity():
    """Return a single continuity snapshot for browser and daemon surfaces."""
    from src.guardian.feedback import guardian_feedback_repository
    from src.observer.insight_queue import insight_queue

    notifications = [item.to_dict() for item in await native_notification_queue.list()]
    queued_insights = await insight_queue.peek_all()
    recent_interventions = await guardian_feedback_repository.list_recent(limit=8)
    session_titles = {
        str(session["id"]): str(session.get("title") or "Untitled session")
        for session in await session_manager.list_sessions()
        if isinstance(session, dict) and session.get("id")
    }
    intervention_thread_map: dict[str, tuple[str | None, str | None]] = {}
    for item in recent_interventions:
        thread_id = item.session_id
        intervention_thread_map[item.id] = (
            thread_id,
            session_titles.get(thread_id) if thread_id else None,
        )

    return {
        "daemon": await _daemon_status_payload(),
        "notifications": [
            {
                **item,
                "thread_id": item.get("session_id"),
                "thread_label": (
                    session_titles.get(str(item["session_id"]))
                    if item.get("session_id")
                    else None
                ),
            }
            for item in notifications
        ],
        "queued_insights": [
            {
                "id": item.id,
                "intervention_id": item.intervention_id,
                "content_excerpt": item.content[:157] + "..." if len(item.content) > 160 else item.content,
                "intervention_type": item.intervention_type,
                "urgency": item.urgency,
                "reasoning": item.reasoning,
                "thread_id": (
                    intervention_thread_map.get(item.intervention_id or "", (None, None))[0]
                    if item.intervention_id
                    else None
                ),
                "thread_label": (
                    intervention_thread_map.get(item.intervention_id or "", (None, None))[1]
                    if item.intervention_id
                    else None
                ),
                "created_at": item.created_at.isoformat(),
            }
            for item in queued_insights
        ],
        "queued_insight_count": len(queued_insights),
        "recent_interventions": [
            {
                "id": item.id,
                "session_id": item.session_id,
                "intervention_type": item.intervention_type,
                "content_excerpt": item.content_excerpt,
                "policy_action": item.policy_action,
                "policy_reason": item.policy_reason,
                "delivery_decision": item.delivery_decision,
                "latest_outcome": item.latest_outcome,
                "transport": item.transport,
                "notification_id": item.notification_id,
                "feedback_type": item.feedback_type,
                "thread_id": item.session_id,
                "thread_label": session_titles.get(item.session_id) if item.session_id else None,
                "updated_at": item.updated_at.isoformat(),
                "continuity_surface": _continuity_surface(
                    latest_outcome=item.latest_outcome,
                    transport=item.transport,
                    policy_action=item.policy_action,
                ),
            }
            for item in recent_interventions
        ],
    }


@router.get("/observer/notifications", response_model=NativeNotificationListResponse)
async def list_native_notifications():
    """Return pending native notifications for browser-side continuity controls."""
    notifications = [item.to_dict() for item in await native_notification_queue.list()]
    return {
        "notifications": notifications,
        "pending_count": len(notifications),
    }


@router.get("/observer/notifications/next", response_model=NativeNotificationPollResponse)
async def get_next_native_notification():
    """Return the next pending native notification for the daemon, if any."""
    notification = await native_notification_queue.peek()
    if notification is None:
        await log_integration_event(
            integration_type="observer_daemon",
            name="notifications",
            outcome="empty_result",
            details={"pending_count": 0},
        )
        return {"notification": None}

    pending_count = await native_notification_queue.count()
    await log_integration_event(
        integration_type="observer_daemon",
        name="notifications",
        outcome="succeeded",
        details={
            "notification_id": notification.id,
            "pending_count": pending_count,
            "intervention_type": notification.intervention_type,
            "urgency": notification.urgency,
        },
    )
    return {"notification": notification.to_dict()}


@router.post("/observer/notifications/{notification_id}/ack", response_model=NotificationAckResponse)
async def ack_native_notification(notification_id: str):
    """Acknowledge and remove a native notification after the daemon displays it."""
    from src.guardian.feedback import guardian_feedback_repository

    notification = await native_notification_queue.get(notification_id)
    acked = await native_notification_queue.ack(notification_id)
    intervention_id = notification.intervention_id if notification is not None else None
    if acked and intervention_id:
        try:
            await guardian_feedback_repository.record_feedback(
                intervention_id,
                feedback_type="acknowledged",
                latest_outcome="notification_acked",
            )
        except Exception:
            logger.debug("Failed to persist native notification acknowledgement", exc_info=True)
    if acked:
        context_manager.record_native_notification(
            title=notification.title if notification is not None else None,
            outcome="acked",
        )
    await log_integration_event(
        integration_type="observer_daemon",
        name="notifications",
        outcome="acked" if acked else "ack_missing",
        details={
            "notification_id": notification_id,
            "intervention_id": intervention_id,
        },
    )
    return {"acked": acked}


@router.post("/observer/notifications/{notification_id}/dismiss", response_model=NotificationDismissResponse)
async def dismiss_native_notification(notification_id: str):
    """Dismiss a pending native notification from the browser control surface."""
    from src.guardian.feedback import guardian_feedback_repository

    notification = await native_notification_queue.dismiss(notification_id)
    if notification is not None and notification.intervention_id:
        try:
            await guardian_feedback_repository.update_outcome(
                notification.intervention_id,
                latest_outcome="notification_dismissed",
                transport="native_notification",
                notification_id=notification.id,
            )
        except Exception:
            logger.debug("Failed to persist native notification dismissal", exc_info=True)
    if notification is not None:
        context_manager.record_native_notification(
            title=notification.title,
            outcome="dismissed",
        )
    await log_integration_event(
        integration_type="observer_daemon",
        name="notifications",
        outcome="dismissed" if notification is not None else "dismiss_missing",
        details={
            "notification_id": notification_id,
            "intervention_id": notification.intervention_id if notification is not None else None,
            "source": "browser_controls",
        },
    )
    return {"dismissed": notification is not None}


@router.post("/observer/notifications/dismiss-all", response_model=NotificationDismissAllResponse)
async def dismiss_all_native_notifications():
    """Dismiss all pending native notifications from the browser control surface."""
    from src.guardian.feedback import guardian_feedback_repository

    notifications = await native_notification_queue.dismiss_all()
    for notification in notifications:
        if notification.intervention_id:
            try:
                await guardian_feedback_repository.update_outcome(
                    notification.intervention_id,
                    latest_outcome="notification_dismissed",
                    transport="native_notification",
                    notification_id=notification.id,
                )
            except Exception:
                logger.debug("Failed to persist native notification dismissal", exc_info=True)
    if notifications:
        context_manager.record_native_notification(
            title=notifications[-1].title,
            outcome="dismissed",
        )
    await log_integration_event(
        integration_type="observer_daemon",
        name="notifications",
        outcome="dismissed_all" if notifications else "dismiss_all_empty",
        details={
            "dismissed_count": len(notifications),
            "source": "browser_controls",
        },
    )
    return {"dismissed_count": len(notifications)}


@router.post("/observer/notifications/test", response_model=NativeNotificationResponse)
async def enqueue_test_native_notification():
    """Queue a sample native notification so the operator can verify the desktop path."""
    notification = await native_notification_queue.enqueue(
        intervention_id=None,
        title="Seraph desktop shell",
        body="Native presence is connected. This is a test notification.",
        intervention_type="test",
        urgency=1,
    )
    context_manager.record_native_notification(
        title=notification.title,
        outcome="queued_test",
    )
    pending_count = await native_notification_queue.count()
    await log_integration_event(
        integration_type="observer_daemon",
        name="notifications",
        outcome="queued",
        details={
            "notification_id": notification.id,
            "pending_count": pending_count,
            "intervention_type": notification.intervention_type,
            "urgency": notification.urgency,
            "source": "test_endpoint",
        },
    )
    return notification.to_dict()


@router.post(
    "/observer/interventions/{intervention_id}/feedback",
    response_model=InterventionFeedbackResponse,
)
async def post_intervention_feedback(intervention_id: str, body: InterventionFeedbackRequest):
    """Record explicit user feedback for a proactive intervention."""
    from src.guardian.feedback import guardian_feedback_repository

    updated = await guardian_feedback_repository.record_feedback(
        intervention_id,
        feedback_type=body.feedback_type,
        feedback_note=body.note,
    )
    await log_integration_event(
        integration_type="observer_feedback",
        name="intervention",
        outcome="succeeded" if updated is not None else "empty_result",
        details={
            "intervention_id": intervention_id,
            "feedback_type": body.feedback_type,
            "has_note": bool((body.note or "").strip()),
        },
    )
    return {"recorded": updated is not None}


@router.get("/observer/activity/today")
async def get_activity_today():
    """Return today's activity summary from screen observations."""
    try:
        from src.observer.screen_repository import screen_observation_repo

        summary = await screen_observation_repo.get_daily_summary(date.today())
        return summary
    except Exception:
        logger.exception("Failed to get daily activity summary")
        return {"error": "Failed to retrieve activity summary"}


@router.post("/observer/refresh")
async def post_refresh():
    """Debug endpoint — trigger a full context refresh."""
    ctx = await context_manager.refresh()
    return ctx.to_dict()
