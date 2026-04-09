"""Observer API — context state and daemon integration endpoints."""

import logging
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from src.audit.runtime import log_integration_event
from src.agent.session import session_manager
from src.observer.manager import context_manager
from src.observer.native_notification_queue import native_notification_queue

logger = logging.getLogger(__name__)

router = APIRouter()


def _timeline_timestamp(value: str | datetime | None) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


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
    thread_source: str = "ambient"
    continuation_mode: str = "open_thread"
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
    session_id: str | None = None
    content_excerpt: str
    intervention_type: str
    urgency: int
    reasoning: str
    thread_id: str | None = None
    thread_label: str | None = None
    thread_source: str = "ambient"
    continuation_mode: str = "open_thread"
    resume_message: str | None = None
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
    thread_source: str = "ambient"
    continuation_mode: str = "open_thread"
    resume_message: str | None = None
    updated_at: str
    continuity_surface: str


class ObserverReachResponse(BaseModel):
    transport_statuses: list[dict[str, Any]]
    route_statuses: list[dict[str, Any]]


class ObserverImportedReachFamilyResponse(BaseModel):
    type: str
    label: str
    total: int
    installed: int
    ready: int
    attention: int
    approval: int
    packages: list[str]


class ObserverImportedReachSummaryResponse(BaseModel):
    family_count: int
    active_family_count: int
    attention_family_count: int
    approval_family_count: int


class ObserverImportedReachResponse(BaseModel):
    summary: ObserverImportedReachSummaryResponse
    families: list[ObserverImportedReachFamilyResponse]


class ObserverSourceAdapterResponse(BaseModel):
    name: str
    provider: str
    source_kind: str
    authenticated: bool
    runtime_state: str
    adapter_state: str
    contracts: list[str]
    degraded_reason: str | None = None
    next_best_sources: list[dict[str, str]]


class ObserverSourceAdapterSummaryResponse(BaseModel):
    adapter_count: int
    ready_adapter_count: int
    degraded_adapter_count: int
    authenticated_adapter_count: int
    authenticated_ready_adapter_count: int
    authenticated_degraded_adapter_count: int


class ObserverSourceAdapterInventoryResponse(BaseModel):
    summary: ObserverSourceAdapterSummaryResponse
    adapters: list[ObserverSourceAdapterResponse]


class ObserverPresenceSurfaceResponse(BaseModel):
    id: str
    kind: str
    label: str
    package_label: str
    package_id: str | None = None
    status: str
    active: bool
    ready: bool
    attention: bool
    detail: str
    repair_hint: str | None = None
    follow_up_hint: str | None = None
    follow_up_prompt: str | None = None
    transport: str | None = None
    source_type: str | None = None
    provider_kind: str | None = None
    execution_mode: str | None = None
    adapter_kind: str | None = None
    selected: bool = False
    requires_network: bool = False
    requires_daemon: bool = False


class ObserverPresenceSummaryResponse(BaseModel):
    surface_count: int
    active_surface_count: int
    ready_surface_count: int
    attention_surface_count: int


class ObserverPresenceInventoryResponse(BaseModel):
    summary: ObserverPresenceSummaryResponse
    surfaces: list[ObserverPresenceSurfaceResponse]


class ObserverContinuitySummaryResponse(BaseModel):
    continuity_health: str
    primary_surface: str
    recommended_focus: str | None = None
    actionable_thread_count: int
    ambient_item_count: int
    pending_notification_count: int
    queued_insight_count: int
    recent_intervention_count: int
    degraded_route_count: int
    degraded_source_adapter_count: int
    attention_family_count: int
    presence_surface_count: int = 0
    attention_presence_surface_count: int = 0


class ObserverContinuityThreadResponse(BaseModel):
    id: str
    thread_id: str | None = None
    thread_label: str | None = None
    thread_source: str = "ambient"
    continuation_mode: str = "open_thread"
    continue_message: str | None = None
    item_count: int
    pending_notification_count: int
    queued_insight_count: int
    recent_intervention_count: int
    latest_updated_at: str | None = None
    primary_surface: str
    surfaces: list[str]
    summary: str
    open_thread_available: bool


class ObserverContinuityRecoveryActionResponse(BaseModel):
    id: str
    kind: str
    label: str
    detail: str
    status: str
    surface: str
    route: str | None = None
    repair_hint: str | None = None
    thread_id: str | None = None
    continue_message: str | None = None
    open_thread_available: bool = False


class ObserverContinuityResponse(BaseModel):
    daemon: DaemonStatusResponse
    notifications: list[NativeNotificationResponse]
    queued_insights: list[QueuedInsightResponse]
    queued_insight_count: int
    recent_interventions: list[RecentInterventionResponse]
    reach: ObserverReachResponse
    imported_reach: ObserverImportedReachResponse
    source_adapters: ObserverSourceAdapterInventoryResponse
    presence_surfaces: ObserverPresenceInventoryResponse | None = None
    summary: ObserverContinuitySummaryResponse
    threads: list[ObserverContinuityThreadResponse]
    recovery_actions: list[ObserverContinuityRecoveryActionResponse]


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


def _continuity_surface(
    *,
    latest_outcome: str | None,
    transport: str | None,
    policy_action: str | None,
) -> str:
    normalized_outcome = latest_outcome or ""
    normalized_action = policy_action or ""
    if transport == "native_notification" or normalized_outcome.startswith("notification_"):
        return "native_notification"
    if normalized_action == "bundle" or normalized_outcome.startswith("bundle") or normalized_outcome == "queued":
        return "bundle_queue"
    if normalized_outcome == "failed":
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


def _thread_label(thread_id: str | None, session_titles: dict[str, str]) -> str | None:
    if not thread_id:
        return None
    return session_titles.get(thread_id)


def _continuity_timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _continuity_surface_priority(surface: str | None) -> int:
    if surface == "reach":
        return 0
    if surface == "source_adapter":
        return 1
    if surface == "imported_reach":
        return 2
    if surface == "native_notification":
        return 3
    if surface == "bundle_queue":
        return 4
    if surface == "delivery_failed":
        return 5
    return 6


_IMPORTED_REACH_FAMILY_DEFS: tuple[tuple[str, str], ...] = (
    ("toolset_presets", "toolsets"),
    ("context_packs", "context packs"),
    ("browser_providers", "browser providers"),
    ("automation_triggers", "automation triggers"),
    ("messaging_connectors", "messaging"),
    ("speech_profiles", "speech"),
    ("node_adapters", "node adapters"),
    ("canvas_outputs", "canvas outputs"),
    ("workflow_runtimes", "workflow runtimes"),
    ("channel_adapters", "channel adapters"),
    ("observer_definitions", "observer sources"),
)

_PRESENCE_CONTRIBUTION_TYPES = {
    "browser_providers",
    "channel_adapters",
    "messaging_connectors",
    "node_adapters",
    "observer_definitions",
}


def _extension_contribution_active(contribution: dict[str, Any]) -> bool:
    health = contribution.get("health")
    health_state = health.get("state") if isinstance(health, dict) else ""
    status = str(contribution.get("status") or health_state or "").strip().lower()
    if contribution.get("loaded") is False:
        return False
    if contribution.get("enabled") is False:
        return False
    if isinstance(health, dict):
        if health.get("enabled") is False:
            return False
        if health.get("configured") is False:
            return False
    if contribution.get("configured") is False:
        return False
    return status not in {
        "planned",
        "requires_config",
        "invalid",
        "invalid_config",
        "overridden",
        "disabled",
        "unloaded",
    }


def _observer_imported_reach_payload() -> dict[str, Any]:
    from src.extensions.lifecycle import list_extensions

    payload = list_extensions()
    extensions = [
        item
        for item in payload.get("extensions", [])
        if isinstance(item, dict)
    ]
    families: list[dict[str, Any]] = []

    for family_type, label in _IMPORTED_REACH_FAMILY_DEFS:
        entries: list[dict[str, Any]] = []
        for extension in extensions:
            contributions = extension.get("contributions")
            if not isinstance(contributions, list):
                continue
            for contribution in contributions:
                if not isinstance(contribution, dict):
                    continue
                if str(contribution.get("type") or "") != family_type:
                    continue
                entries.append(
                    {
                        "package_label": str(extension.get("display_name") or extension.get("id") or ""),
                        "contribution": contribution,
                    }
                )

        if not entries:
            continue

        active_entries = [
            item
            for item in entries
            if _extension_contribution_active(item["contribution"])
        ]
        ready = 0
        attention = 0
        approval = 0
        packages = sorted(
            {
                str(item["package_label"])
                for item in entries
                if str(item["package_label"]).strip()
            }
        )
        for item in entries:
            contribution = item["contribution"]
            health = contribution.get("health")
            health_state = health.get("state") if isinstance(health, dict) else ""
            status = str(contribution.get("status") or health_state or "").strip().lower()
            permission_profile = contribution.get("permission_profile")
            if _extension_contribution_active(contribution) and (
                (bool(health.get("ready")) if isinstance(health, dict) else False)
                or status in {"ready", "active"}
            ):
                ready += 1
            if status in {
                "degraded",
                "invalid",
                "invalid_config",
                "requires_config",
                "planned",
                "overridden",
            } or (
                isinstance(permission_profile, dict)
                and str(permission_profile.get("status") or "") == "missing_permissions"
            ):
                attention += 1
            if (
                isinstance(permission_profile, dict)
                and bool(permission_profile.get("requires_approval"))
            ) or str(contribution.get("approval_behavior") or "") == "always":
                approval += 1

        families.append(
            {
                "type": family_type,
                "label": label,
                "total": len(active_entries),
                "installed": len(entries),
                "ready": ready,
                "attention": attention,
                "approval": approval,
                "packages": packages,
            }
        )

    return {
        "summary": {
            "family_count": len(families),
            "active_family_count": sum(1 for item in families if int(item.get("total") or 0) > 0),
            "attention_family_count": sum(1 for item in families if int(item.get("attention") or 0) > 0),
            "approval_family_count": sum(1 for item in families if int(item.get("approval") or 0) > 0),
        },
        "families": families,
    }


def _observer_source_adapter_payload() -> dict[str, Any]:
    from src.extensions.source_operations import list_source_adapter_inventory

    inventory = list_source_adapter_inventory()
    adapters = [
        {
            "name": str(item.get("name") or ""),
            "provider": str(item.get("provider") or ""),
            "source_kind": str(item.get("source_kind") or ""),
            "authenticated": bool(item.get("authenticated")),
            "runtime_state": str(item.get("runtime_state") or "unknown"),
            "adapter_state": str(item.get("adapter_state") or "unknown"),
            "contracts": [
                str(contract)
                for contract in item.get("contracts", [])
                if isinstance(contract, str) and contract.strip()
            ],
            "degraded_reason": str(item.get("degraded_reason") or "").strip() or None,
            "next_best_sources": [
                {
                    "name": str(candidate.get("name") or ""),
                    "reason": str(candidate.get("reason") or ""),
                    "description": str(candidate.get("description") or ""),
                }
                for candidate in item.get("next_best_sources", [])
                if isinstance(candidate, dict)
            ],
        }
        for item in inventory.get("adapters", [])
        if isinstance(item, dict)
    ]
    authenticated_adapters = [item for item in adapters if item["authenticated"]]
    return {
        "summary": {
            "adapter_count": len(adapters),
            "ready_adapter_count": sum(1 for item in adapters if item["adapter_state"] == "ready"),
            "degraded_adapter_count": sum(1 for item in adapters if item["adapter_state"] != "ready"),
            "authenticated_adapter_count": len(authenticated_adapters),
            "authenticated_ready_adapter_count": sum(1 for item in authenticated_adapters if item["adapter_state"] == "ready"),
            "authenticated_degraded_adapter_count": sum(1 for item in authenticated_adapters if item["adapter_state"] != "ready"),
        },
        "adapters": adapters,
    }


def _presence_surface_status(contribution: dict[str, Any]) -> str:
    health = contribution.get("health")
    health_state = health.get("state") if isinstance(health, dict) else ""
    return str(contribution.get("status") or health_state or "unknown").strip().lower() or "unknown"


def _presence_surface_attention(contribution: dict[str, Any]) -> bool:
    permission_profile = contribution.get("permission_profile")
    status = _presence_surface_status(contribution)
    return status in {
        "degraded",
        "disabled",
        "invalid",
        "invalid_config",
        "overridden",
        "planned",
        "requires_config",
    } or (
        isinstance(permission_profile, dict)
        and str(permission_profile.get("status") or "") == "missing_permissions"
    )


def _presence_surface_ready(contribution: dict[str, Any]) -> bool:
    health = contribution.get("health")
    status = _presence_surface_status(contribution)
    return _extension_contribution_active(contribution) and (
        (bool(health.get("ready")) if isinstance(health, dict) else False)
        or status in {"ready", "active", "connected", "loaded", "enabled"}
    )


def _presence_surface_kind(contribution_type: str) -> str:
    return {
        "channel_adapters": "channel_adapter",
        "messaging_connectors": "messaging_connector",
        "node_adapters": "node_adapter",
        "browser_providers": "browser_provider",
        "observer_definitions": "observer_definition",
    }.get(contribution_type, contribution_type)


def _presence_surface_label(contribution: dict[str, Any]) -> str:
    contribution_type = str(contribution.get("type") or "")
    name = str(contribution.get("name") or "").strip()
    if contribution_type == "browser_providers":
        return name or "browser provider"
    if contribution_type == "channel_adapters":
        transport = str(contribution.get("transport") or "channel").replace("_", " ")
        return name or f"{transport} channel"
    if contribution_type == "messaging_connectors":
        platform = str(contribution.get("platform") or "messaging").replace("_", " ")
        return name or f"{platform} messaging"
    if contribution_type == "observer_definitions":
        source_type = str(contribution.get("source_type") or "observer").replace("_", " ")
        return name or f"{source_type} observer"
    if contribution_type == "node_adapters":
        return name or "node adapter"
    return name or contribution_type.replace("_", " ")


def _presence_surface_detail(contribution: dict[str, Any], *, package_label: str) -> str:
    contribution_type = str(contribution.get("type") or "")
    label = _presence_surface_label(contribution)
    status = _presence_surface_status(contribution).replace("_", " ")
    if contribution_type == "channel_adapters":
        transport = str(contribution.get("transport") or "channel").replace("_", " ")
        return f"{package_label} exposes {label} for {transport} delivery ({status})."
    if contribution_type == "messaging_connectors":
        platform = str(contribution.get("platform") or "messaging").replace("_", " ")
        return f"{package_label} exposes {label} on {platform} ({status})."
    if contribution_type == "observer_definitions":
        source_type = str(contribution.get("source_type") or "observer").replace("_", " ")
        return f"{package_label} adds {label} for {source_type} observation ({status})."
    if contribution_type == "browser_providers":
        return f"{package_label} exposes {label} for packaged browser reach ({status})."
    if contribution_type == "node_adapters":
        return f"{package_label} adds {label} for companion execution or device reach ({status})."
    return f"{package_label} exposes {label} ({status})."


def _presence_surface_repair_hint(contribution: dict[str, Any]) -> str | None:
    status = _presence_surface_status(contribution)
    contribution_type = str(contribution.get("type") or "")
    if status in {"ready", "active", "connected", "loaded", "enabled"}:
        return None
    if status == "requires_config":
        return "Finish connector configuration in the operator surface before routing follow-through here."
    if status == "planned":
        return "Enable the packaged contribution and confirm its runtime prerequisites in the operator surface."
    if status == "overridden":
        return "Inspect the competing packaged contribution that currently owns this surface."
    if status == "disabled":
        return "Re-enable this packaged contribution in extension lifecycle state."
    if contribution_type == "channel_adapters":
        return "Inspect channel routing and extension diagnostics in the operator surface."
    if contribution_type == "browser_providers":
        return "Inspect browser provider diagnostics and remote-browser prerequisites in the operator surface."
    if contribution_type == "observer_definitions":
        return "Inspect observer package state and manifest diagnostics in the operator surface."
    if contribution_type == "node_adapters":
        return "Inspect node-adapter diagnostics and daemon prerequisites in the operator surface."
    return "Inspect extension diagnostics and runtime prerequisites in the operator surface."


def _presence_surface_follow_up_prompt(contribution: dict[str, Any]) -> str | None:
    contribution_type = str(contribution.get("type") or "")
    if contribution_type not in {"channel_adapters", "messaging_connectors"}:
        return None
    if not _presence_surface_ready(contribution):
        return None
    label = _presence_surface_label(contribution)
    return (
        f"Plan guarded follow-through for {label}. Confirm the audience, target reference, "
        "channel scope, and approval boundaries before acting."
    )


def _browser_provider_surface_detail(item: Any, *, package_label: str) -> str:
    provider_kind = str(getattr(item, "provider_kind", "") or "browser").replace("_", " ")
    runtime_state = str(getattr(item, "runtime_state", "") or "unknown").replace("_", " ")
    name = str(getattr(item, "name", "") or "browser provider")
    if str(getattr(item, "runtime_state", "") or "") == "staged_local_fallback":
        return (
            f"{package_label} exposes {name} as a {provider_kind} browser provider, "
            "but remote browser reach still falls back to the local runtime."
        )
    return f"{package_label} exposes {name} as a {provider_kind} browser provider ({runtime_state})."


def _browser_provider_surface_repair_hint(item: Any) -> str | None:
    runtime_state = str(getattr(item, "runtime_state", "") or "")
    if runtime_state == "ready":
        return None
    if runtime_state == "requires_config":
        return "Finish browser provider configuration before routing browser-assisted follow-through here."
    if runtime_state == "disabled":
        return "Re-enable this browser provider in extension lifecycle state."
    if runtime_state == "staged_local_fallback":
        return "Inspect remote browser transport prerequisites before relying on this packaged browser reach."
    return "Inspect browser provider diagnostics and runtime prerequisites in the operator surface."


def _browser_provider_follow_up_prompt(item: Any) -> str | None:
    if not bool(getattr(item, "selected", False)):
        return None
    runtime_state = str(getattr(item, "runtime_state", "") or "")
    if runtime_state not in {"ready", "staged_local_fallback"}:
        return None
    name = str(getattr(item, "name", "") or "browser provider")
    if runtime_state == "staged_local_fallback":
        return (
            f"Plan guarded browser-assisted follow-through via {name}. Remote browser reach still falls back "
            "to the local runtime, so confirm the target page, authentication boundary, and fallback expectations before acting."
        )
    return (
        f"Plan guarded browser-assisted follow-through via {name}. Confirm the target page, "
        "authentication boundary, and fallback expectations before acting."
    )


def _node_adapter_surface_detail(item: Any, *, package_label: str) -> str:
    name = str(getattr(item, "name", "") or "node adapter")
    adapter_kind = str(getattr(item, "adapter_kind", "") or "companion").replace("_", " ")
    runtime_state = str(getattr(item, "runtime_state", "") or "unknown").replace("_", " ")
    return f"{package_label} adds {name} for {adapter_kind} device or companion reach ({runtime_state})."


def _node_adapter_surface_repair_hint(item: Any) -> str | None:
    runtime_state = str(getattr(item, "runtime_state", "") or "")
    if runtime_state in {"staged_link", "staged_canvas"}:
        return None
    if runtime_state == "requires_config":
        return "Finish node-adapter configuration before routing companion or device follow-through here."
    if runtime_state == "disabled":
        return "Re-enable this node adapter in extension lifecycle state."
    return "Inspect node-adapter diagnostics and daemon prerequisites in the operator surface."


def _node_adapter_follow_up_prompt(item: Any) -> str | None:
    if str(getattr(item, "runtime_state", "") or "") not in {"staged_link", "staged_canvas"}:
        return None
    name = str(getattr(item, "name", "") or "node adapter")
    return (
        f"Plan guarded companion follow-through via {name}. Confirm the target device or canvas scope, "
        "execution boundary, and approval posture before acting."
    )


def _observer_presence_surface_payload() -> dict[str, Any]:
    from src.extensions.lifecycle import list_extensions
    from src.extensions.browser_providers import list_browser_provider_inventory
    from src.extensions.node_adapters import list_node_adapter_inventory
    from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
    from src.extensions.state import connector_enabled_overrides, load_extension_state_payload
    from config.settings import settings

    payload = list_extensions()
    extensions = [
        item
        for item in payload.get("extensions", [])
        if isinstance(item, dict)
    ]
    package_label_by_id = {
        str(item.get("id") or "").strip(): (
            str(item.get("display_name") or item.get("id") or "").strip() or "Extension package"
        )
        for item in extensions
        if str(item.get("id") or "").strip()
    }

    surfaces_by_id: dict[str, dict[str, Any]] = {}
    for extension in extensions:
        contributions = extension.get("contributions")
        if not isinstance(contributions, list):
            continue
        package_label = str(extension.get("display_name") or extension.get("id") or "").strip() or "Extension package"
        package_id = str(extension.get("id") or "").strip() or None
        for contribution in contributions:
            if not isinstance(contribution, dict):
                continue
            contribution_type = str(contribution.get("type") or "")
            if contribution_type not in _PRESENCE_CONTRIBUTION_TYPES:
                continue
            status = _presence_surface_status(contribution)
            active = _extension_contribution_active(contribution)
            ready = _presence_surface_ready(contribution)
            attention = _presence_surface_attention(contribution)
            follow_up_prompt = _presence_surface_follow_up_prompt(contribution)
            if contribution_type in {"browser_providers", "node_adapters"} and status not in {
                "planned",
                "overridden",
                "invalid",
                "invalid_config",
            }:
                continue
            if not (
                active
                or attention
                or follow_up_prompt
                or status in {"planned", "overridden", "disabled"}
            ):
                continue
            surface_id = (
                f"{contribution_type}:{package_id or package_label}:"
                f"{str(contribution.get('reference') or contribution.get('name') or contribution.get('transport') or contribution.get('source_type') or '')}"
            )
            surfaces_by_id[surface_id] = {
                "id": surface_id,
                "kind": _presence_surface_kind(contribution_type),
                "label": _presence_surface_label(contribution),
                "package_label": package_label,
                "package_id": package_id,
                "status": status,
                "active": active,
                "ready": ready,
                "attention": attention,
                "detail": _presence_surface_detail(contribution, package_label=package_label),
                "repair_hint": _presence_surface_repair_hint(contribution),
                "follow_up_hint": (
                    "Use operator review before routing external follow-through through this surface."
                    if follow_up_prompt
                    else None
                ),
                "follow_up_prompt": follow_up_prompt,
                "transport": (
                    str(contribution.get("transport") or "").strip() or None
                    if contribution_type == "channel_adapters"
                    else None
                ),
                "source_type": (
                    str(contribution.get("source_type") or "").strip() or None
                    if contribution_type == "observer_definitions"
                    else None
                ),
                "provider_kind": (
                    str(contribution.get("provider_kind") or "").strip() or None
                    if contribution_type == "browser_providers"
                    else None
                ),
                "execution_mode": None,
                "adapter_kind": (
                    str(contribution.get("adapter_kind") or "").strip() or None
                    if contribution_type == "node_adapters"
                    else None
                ),
                "selected": False,
                "requires_network": bool(contribution.get("requires_network", False)),
                "requires_daemon": bool(contribution.get("requires_daemon", False)),
            }

    state_payload = load_extension_state_payload()
    state_by_id = state_payload.get("extensions") if isinstance(state_payload, dict) else None
    snapshot = ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()
    enabled_overrides = connector_enabled_overrides(state_by_id if isinstance(state_by_id, dict) else None)

    browser_inventory = list_browser_provider_inventory(
        snapshot.list_contributions("browser_providers"),
        state_by_id=state_by_id if isinstance(state_by_id, dict) else None,
        enabled_overrides=enabled_overrides,
    )
    packaged_browser_inventory = [item for item in browser_inventory if item.extension_id != "seraph.runtime-browser"]
    for item in packaged_browser_inventory:
        runtime_state = str(getattr(item, "runtime_state", "") or "unknown")
        ready = runtime_state == "ready"
        attention = runtime_state in {"requires_config", "disabled", "staged_local_fallback"}
        follow_up_prompt = _browser_provider_follow_up_prompt(item)
        package_id = str(getattr(item, "extension_id", "") or "").strip()
        package_label = package_label_by_id.get(package_id, package_id or "Extension package")
        if not (bool(getattr(item, "enabled", False)) or attention or follow_up_prompt):
            continue
        surface_id = f"browser_providers:{package_id}:{item.reference}"
        surfaces_by_id[surface_id] = {
            "id": surface_id,
            "kind": "browser_provider",
            "label": str(getattr(item, "name", "") or "browser provider"),
            "package_label": package_label,
            "package_id": package_id or None,
            "status": runtime_state,
            "active": bool(getattr(item, "enabled", False)),
            "ready": ready,
            "attention": attention,
            "detail": _browser_provider_surface_detail(item, package_label=package_label),
            "repair_hint": _browser_provider_surface_repair_hint(item),
            "follow_up_hint": (
                "Use operator review before routing browser-assisted follow-through through this provider."
                if follow_up_prompt
                else None
            ),
            "follow_up_prompt": follow_up_prompt,
            "transport": None,
            "source_type": None,
            "provider_kind": str(getattr(item, "provider_kind", "") or "") or None,
            "execution_mode": str(getattr(item, "execution_mode", "") or "") or None,
            "adapter_kind": None,
            "selected": bool(getattr(item, "selected", False)),
            "requires_network": bool(getattr(item, "requires_network", False)),
            "requires_daemon": bool(getattr(item, "requires_daemon", False)),
        }

    node_inventory = list_node_adapter_inventory(
        snapshot.list_contributions("node_adapters"),
        state_by_id=state_by_id if isinstance(state_by_id, dict) else None,
        enabled_overrides=enabled_overrides,
    )
    for item in node_inventory:
        runtime_state = str(getattr(item, "runtime_state", "") or "unknown")
        ready = runtime_state in {"staged_link", "staged_canvas"}
        attention = runtime_state in {"requires_config", "disabled"}
        follow_up_prompt = _node_adapter_follow_up_prompt(item)
        package_id = str(getattr(item, "extension_id", "") or "").strip()
        package_label = package_label_by_id.get(package_id, package_id or "Extension package")
        if not (bool(getattr(item, "enabled", False)) or attention or follow_up_prompt):
            continue
        surface_id = f"node_adapters:{package_id}:{item.reference}"
        surfaces_by_id[surface_id] = {
            "id": surface_id,
            "kind": "node_adapter",
            "label": str(getattr(item, "name", "") or "node adapter"),
            "package_label": package_label,
            "package_id": package_id or None,
            "status": runtime_state,
            "active": bool(getattr(item, "enabled", False)),
            "ready": ready,
            "attention": attention,
            "detail": _node_adapter_surface_detail(item, package_label=package_label),
            "repair_hint": _node_adapter_surface_repair_hint(item),
            "follow_up_hint": (
                "Use operator review before routing companion or device follow-through through this surface."
                if follow_up_prompt
                else None
            ),
            "follow_up_prompt": follow_up_prompt,
            "transport": None,
            "source_type": None,
            "provider_kind": None,
            "execution_mode": None,
            "adapter_kind": str(getattr(item, "adapter_kind", "") or "") or None,
            "selected": False,
            "requires_network": bool(getattr(item, "requires_network", False)),
            "requires_daemon": bool(getattr(item, "requires_daemon", False)),
        }

    surfaces = sorted(
        surfaces_by_id.values(),
        key=lambda item: (
            0 if bool(item.get("attention")) else 1 if not bool(item.get("ready")) else 2,
            str(item.get("kind") or ""),
            str(item.get("label") or ""),
            str(item.get("package_label") or ""),
        ),
    )
    return {
        "summary": {
            "surface_count": len(surfaces),
            "active_surface_count": sum(1 for item in surfaces if bool(item.get("active"))),
            "ready_surface_count": sum(1 for item in surfaces if bool(item.get("ready"))),
            "attention_surface_count": sum(1 for item in surfaces if bool(item.get("attention"))),
        },
        "surfaces": surfaces,
    }


def _summarize_thread_surfaces(surfaces: list[str]) -> str:
    labels = [surface.replace("_", " ") for surface in surfaces]
    if not labels:
        return "ambient follow-up"
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def _build_continuity_threads(
    *,
    notifications: list[dict[str, Any]],
    queued_insights: list[dict[str, Any]],
    recent_interventions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}

    def ensure_bucket(
        *,
        thread_id: str | None,
        thread_label: str | None,
        thread_source: str | None,
        continuation_mode: str | None,
        continue_message: str | None,
        created_at: str | None,
        surface: str,
    ) -> dict[str, Any]:
        key = thread_id or "ambient"
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "id": f"thread:{key}",
                "thread_id": thread_id,
                "thread_label": thread_label,
                "thread_source": thread_source or "ambient",
                "continuation_mode": continuation_mode or "open_thread",
                "continue_message": continue_message,
                "item_count": 0,
                "pending_notification_count": 0,
                "queued_insight_count": 0,
                "recent_intervention_count": 0,
                "latest_updated_at": created_at,
                "primary_surface": surface,
                "surfaces": [],
                "open_thread_available": bool(thread_id),
            }
            buckets[key] = bucket
        if thread_label and not bucket.get("thread_label"):
            bucket["thread_label"] = thread_label
        if thread_source and bucket.get("thread_source") == "ambient":
            bucket["thread_source"] = thread_source
        if continuation_mode and bucket.get("continuation_mode") == "open_thread":
            bucket["continuation_mode"] = continuation_mode
        if continue_message and not bucket.get("continue_message"):
            bucket["continue_message"] = continue_message
        if thread_id and not bucket.get("thread_id"):
            bucket["thread_id"] = thread_id
            bucket["open_thread_available"] = True
        if created_at and (
            bucket.get("latest_updated_at") is None
            or _continuity_timestamp(created_at) > _continuity_timestamp(bucket.get("latest_updated_at"))
        ):
            bucket["latest_updated_at"] = created_at
        if surface not in bucket["surfaces"]:
            bucket["surfaces"].append(surface)
        if _continuity_surface_priority(surface) < _continuity_surface_priority(bucket.get("primary_surface")):
            bucket["primary_surface"] = surface
        return bucket

    for item in notifications:
        bucket = ensure_bucket(
            thread_id=item.get("thread_id") or item.get("session_id"),
            thread_label=item.get("thread_label"),
            thread_source=item.get("thread_source"),
            continuation_mode=item.get("continuation_mode"),
            continue_message=item.get("resume_message"),
            created_at=item.get("created_at"),
            surface="native_notification",
        )
        bucket["item_count"] += 1
        bucket["pending_notification_count"] += 1

    for item in queued_insights:
        bucket = ensure_bucket(
            thread_id=item.get("thread_id") or item.get("session_id"),
            thread_label=item.get("thread_label"),
            thread_source=item.get("thread_source"),
            continuation_mode=item.get("continuation_mode"),
            continue_message=item.get("resume_message"),
            created_at=item.get("created_at"),
            surface="bundle_queue",
        )
        bucket["item_count"] += 1
        bucket["queued_insight_count"] += 1

    for item in recent_interventions:
        bucket = ensure_bucket(
            thread_id=item.get("thread_id") or item.get("session_id"),
            thread_label=item.get("thread_label"),
            thread_source=item.get("thread_source"),
            continuation_mode=item.get("continuation_mode"),
            continue_message=item.get("resume_message"),
            created_at=item.get("updated_at"),
            surface=str(item.get("continuity_surface") or "browser"),
        )
        bucket["item_count"] += 1
        bucket["recent_intervention_count"] += 1

    threads = list(buckets.values())
    for bucket in threads:
        bucket["surfaces"] = sorted(
            bucket["surfaces"],
            key=lambda value: (_continuity_surface_priority(value), value),
        )
        thread_name = bucket.get("thread_label") or (
            f"thread {str(bucket['thread_id'])[:6]}" if bucket.get("thread_id") else "ambient follow-up"
        )
        bucket["summary"] = (
            f"{bucket['item_count']} continuity item"
            f"{'' if bucket['item_count'] == 1 else 's'} across {_summarize_thread_surfaces(bucket['surfaces'])} for {thread_name}."
        )

    return sorted(
        threads,
        key=lambda item: (
            -int(item.get("pending_notification_count", 0)),
            -int(item.get("queued_insight_count", 0)),
            -int(item.get("recent_intervention_count", 0)),
            -int(item.get("item_count", 0)),
            -_continuity_timestamp(item.get("latest_updated_at")).timestamp(),
        ),
    )


def _build_continuity_recovery_actions(
    *,
    route_statuses: list[dict[str, Any]],
    imported_reach: dict[str, Any],
    source_adapters: dict[str, Any],
    presence_surfaces: dict[str, Any],
    threads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for route in route_statuses:
        status = str(route.get("status") or "")
        if status == "ready":
            continue
        label = f"Repair {route.get('label') or str(route.get('route') or 'reach route').replace('_', ' ')}"
        actions.append({
            "id": f"route:{route.get('route')}",
            "kind": "reach_repair",
            "label": label,
            "detail": str(route.get("summary") or ""),
            "status": status,
            "surface": "reach",
            "route": route.get("route"),
            "repair_hint": route.get("repair_hint"),
            "thread_id": None,
            "continue_message": None,
            "open_thread_available": False,
        })

    for adapter in source_adapters.get("adapters", []):
        if not isinstance(adapter, dict):
            continue
        if str(adapter.get("adapter_state") or "unknown") == "ready":
            continue
        next_best_sources = [
            str(item.get("name") or "")
            for item in adapter.get("next_best_sources", [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        degraded_reason = str(adapter.get("degraded_reason") or "").strip()
        detail = (
            f"{adapter.get('provider') or 'provider'} adapter is "
            f"{str(adapter.get('adapter_state') or 'unknown').replace('_', ' ')}"
        )
        if degraded_reason:
            detail += f" ({degraded_reason})"
        detail += "."
        actions.append({
            "id": f"source:{adapter.get('name')}",
            "kind": "source_adapter_repair",
            "label": f"Restore source adapter {adapter.get('name')}",
            "detail": detail,
            "status": str(adapter.get("adapter_state") or "unknown"),
            "surface": "source_adapter",
            "route": None,
            "repair_hint": (
                f"Next best: {', '.join(next_best_sources[:2])}."
                if next_best_sources
                else "Inspect the typed source adapter inventory and runtime bridge."
            ),
            "thread_id": None,
            "continue_message": None,
            "open_thread_available": False,
        })

    for family in imported_reach.get("families", []):
        if not isinstance(family, dict):
            continue
        attention = int(family.get("attention") or 0)
        if attention <= 0:
            continue
        packages = [
            str(item)
            for item in family.get("packages", [])
            if isinstance(item, str) and item.strip()
        ]
        package_count = len(packages)
        actions.append({
            "id": f"imported:{family.get('type')}",
            "kind": "imported_reach_attention",
            "label": f"Review imported {family.get('label')}",
            "detail": (
                f"{attention} imported contribution"
                f"{'' if attention == 1 else 's'} need attention"
                f"{f' across {package_count} package' if package_count else ''}"
                f"{'' if package_count == 1 else 's' if package_count else ''}."
            ),
            "status": "attention",
            "surface": "imported_reach",
            "route": None,
            "repair_hint": (
                f"Inspect {', '.join(packages[:2])} in the operator surface."
                if packages
                else "Inspect imported capability reach in the operator surface."
            ),
            "thread_id": None,
            "continue_message": None,
            "open_thread_available": False,
        })

    for presence_surface in presence_surfaces.get("surfaces", []):
        if not isinstance(presence_surface, dict):
            continue
        if bool(presence_surface.get("attention")):
            actions.append({
                "id": f"presence:{presence_surface.get('id')}",
                "kind": "presence_repair",
                "label": f"Review presence surface {presence_surface.get('label')}",
                "detail": str(presence_surface.get("detail") or ""),
                "status": str(presence_surface.get("status") or "attention"),
                "surface": "presence",
                "route": str(presence_surface.get("kind") or "") or None,
                "repair_hint": presence_surface.get("repair_hint"),
                "thread_id": None,
                "continue_message": None,
                "open_thread_available": False,
            })
            continue
        if str(presence_surface.get("kind") or "") in {"channel_adapter", "messaging_connector", "browser_provider", "node_adapter"} and bool(presence_surface.get("follow_up_prompt")):
            actions.append({
                "id": f"presence-follow:{presence_surface.get('id')}",
                "kind": "presence_follow_up",
                "label": f"Plan follow-up via {presence_surface.get('label')}",
                "detail": str(presence_surface.get("detail") or ""),
                "status": "ready",
                "surface": "presence",
                "route": str(presence_surface.get("kind") or "") or None,
                "repair_hint": presence_surface.get("follow_up_hint"),
                "thread_id": None,
                "continue_message": presence_surface.get("follow_up_prompt"),
                "open_thread_available": False,
            })

    for thread in threads:
        thread_name = thread.get("thread_label") or (
            f"thread {str(thread['thread_id'])[:6]}" if thread.get("thread_id") else "ambient follow-up"
        )
        actions.append({
            "id": f"followup:{thread['id']}",
            "kind": "thread_follow_up",
            "label": f"Continue {thread_name}",
            "detail": thread["summary"],
            "status": "actionable",
            "surface": thread.get("primary_surface") or "browser",
            "route": None,
            "repair_hint": None,
            "thread_id": thread.get("thread_id"),
            "continue_message": thread.get("continue_message"),
            "open_thread_available": bool(thread.get("thread_id")),
        })

    return sorted(
        actions,
        key=lambda item: (
            0 if item["kind"] == "reach_repair" else
            1 if item["kind"] == "source_adapter_repair" else
            2 if item["kind"] == "presence_repair" else
            3 if item["kind"] == "imported_reach_attention" else
            4 if item["kind"] == "thread_follow_up" else
            5,
            _continuity_surface_priority(item.get("surface")),
            item["label"],
        ),
    )[:8]


def _build_continuity_summary(
    *,
    notifications: list[dict[str, Any]],
    queued_insights: list[dict[str, Any]],
    recent_interventions: list[dict[str, Any]],
    route_statuses: list[dict[str, Any]],
    imported_reach: dict[str, Any],
    source_adapters: dict[str, Any],
    presence_surfaces: dict[str, Any],
    threads: list[dict[str, Any]],
) -> dict[str, Any]:
    degraded_routes = [item for item in route_statuses if str(item.get("status") or "") != "ready"]
    degraded_source_adapters = [
        item
        for item in source_adapters.get("adapters", [])
        if isinstance(item, dict) and str(item.get("adapter_state") or "unknown") != "ready"
    ]
    attention_families = [
        item
        for item in imported_reach.get("families", [])
        if isinstance(item, dict) and int(item.get("attention") or 0) > 0
    ]
    attention_presence_surfaces = [
        item
        for item in presence_surfaces.get("surfaces", [])
        if isinstance(item, dict) and bool(item.get("attention"))
    ]
    ambient_item_count = sum(
        int(item.get("item_count", 0))
        for item in threads
        if not item.get("thread_id")
    )
    continuity_health = "ready"
    primary_surface = "browser"
    recommended_focus: str | None = None

    if degraded_routes:
        top_route = sorted(
            degraded_routes,
            key=lambda item: (
                0 if item.get("status") == "unavailable" else 1,
                str(item.get("label") or ""),
            ),
        )[0]
        continuity_health = "degraded" if top_route.get("status") == "unavailable" else "attention"
        primary_surface = "reach"
        recommended_focus = str(top_route.get("label") or "reach recovery")
    elif degraded_source_adapters:
        lead_adapter = degraded_source_adapters[0]
        continuity_health = "attention"
        primary_surface = "source_adapter"
        recommended_focus = str(lead_adapter.get("name") or "source adapter recovery")
    elif attention_presence_surfaces:
        lead_surface = attention_presence_surfaces[0]
        continuity_health = "attention"
        primary_surface = "presence"
        recommended_focus = str(lead_surface.get("label") or "presence surface review")
    elif attention_families:
        lead_family = attention_families[0]
        continuity_health = "attention"
        primary_surface = "imported_reach"
        recommended_focus = str(lead_family.get("label") or "imported reach review")
    elif threads:
        lead_thread = threads[0]
        continuity_health = "attention"
        primary_surface = str(lead_thread.get("primary_surface") or "browser")
        recommended_focus = str(
            lead_thread.get("thread_label")
            or (f"thread {str(lead_thread['thread_id'])[:6]}" if lead_thread.get("thread_id") else "ambient follow-up")
        )

    return {
        "continuity_health": continuity_health,
        "primary_surface": primary_surface,
        "recommended_focus": recommended_focus,
        "actionable_thread_count": len(threads),
        "ambient_item_count": ambient_item_count,
        "pending_notification_count": len(notifications),
        "queued_insight_count": len(queued_insights),
        "recent_intervention_count": len(recent_interventions),
        "degraded_route_count": len(degraded_routes),
        "degraded_source_adapter_count": len(degraded_source_adapters),
        "attention_family_count": len(attention_families),
        "presence_surface_count": int(presence_surfaces.get("summary", {}).get("surface_count") or 0),
        "attention_presence_surface_count": len(attention_presence_surfaces),
    }


def _continuation_mode(thread_id: str | None) -> str:
    return "resume_thread" if thread_id else "open_thread"


def _notification_payload(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    payload = getattr(item, "__dict__", None)
    if isinstance(payload, dict):
        return dict(payload)
    raise TypeError("notification payload is not serializable")


def _observer_reach_payload() -> dict[str, list[dict[str, Any]]]:
    from src.extensions.channel_routing import (
        SUPPORTED_CHANNEL_ROUTE_TRANSPORTS,
        route_runtime_statuses,
        transport_runtime_status,
    )
    from src.extensions.state import load_extension_state_payload
    from src.observer.delivery import _active_channel_adapters
    from src.scheduler.connection_manager import ws_manager

    active_transports = _active_channel_adapters()
    websocket_connection_count = ws_manager.active_count
    daemon_connected = context_manager.is_daemon_connected()
    state_payload = load_extension_state_payload()
    return {
        "transport_statuses": [
            transport_runtime_status(
                transport,
                active_transports=active_transports,
                websocket_connection_count=websocket_connection_count,
                daemon_connected=daemon_connected,
            )
            for transport in SUPPORTED_CHANNEL_ROUTE_TRANSPORTS
        ],
        "route_statuses": route_runtime_statuses(
            state_payload,
            active_transports=active_transports,
            websocket_connection_count=websocket_connection_count,
            daemon_connected=daemon_connected,
        ),
    }


@router.get("/observer/continuity", response_model=ObserverContinuityResponse)
async def build_observer_continuity_snapshot() -> dict[str, Any]:
    """Build a single live continuity snapshot for browser and daemon surfaces."""
    from src.guardian.feedback import guardian_feedback_repository
    from src.observer.insight_queue import insight_queue

    notifications = [_notification_payload(item) for item in await native_notification_queue.list()]
    queued_insights = await insight_queue.peek_all()
    recent_interventions = await guardian_feedback_repository.list_recent(limit=8)
    session_titles = {
        str(session["id"]): str(session.get("title") or "Untitled session")
        for session in await session_manager.list_sessions()
        if isinstance(session, dict) and session.get("id")
    }
    intervention_thread_map: dict[str, tuple[str | None, str | None]] = {}
    for item in recent_interventions:
        thread_id = getattr(item, "session_id", None)
        intervention_thread_map[item.id] = (
            thread_id,
            session_titles.get(thread_id) if thread_id else None,
        )
    missing_intervention_ids = {
        str(item.intervention_id)
        for item in queued_insights
        if item.intervention_id
        and not getattr(item, "session_id", None)
        and str(item.intervention_id) not in intervention_thread_map
    }
    for intervention_id in missing_intervention_ids:
        fallback_intervention = await guardian_feedback_repository.get(intervention_id)
        fallback_session_id = getattr(fallback_intervention, "session_id", None) if fallback_intervention else None
        if fallback_intervention is None or not fallback_session_id:
            continue
        intervention_thread_map[intervention_id] = (
            fallback_session_id,
            session_titles.get(fallback_session_id),
        )

    reach_payload = _observer_reach_payload()
    imported_reach_payload = _observer_imported_reach_payload()
    source_adapter_payload = _observer_source_adapter_payload()
    presence_surface_payload = _observer_presence_surface_payload()

    notifications_payload = [
        {
            **item,
            "created_at": _timeline_timestamp(item.get("created_at")),
            "updated_at": _timeline_timestamp(item.get("updated_at") or item.get("created_at")),
            "thread_id": item.get("thread_id") or item.get("session_id"),
            "thread_label": _thread_label(
                str(item.get("thread_id") or item.get("session_id"))
                if item.get("thread_id") or item.get("session_id")
                else None,
                session_titles,
            ),
        }
        for item in notifications
    ]
    queued_insights_payload = [
        {
            "id": item.id,
            "intervention_id": item.intervention_id,
            "content_excerpt": item.content[:157] + "..." if len(item.content) > 160 else item.content,
            "intervention_type": item.intervention_type,
            "urgency": item.urgency,
            "reasoning": item.reasoning,
            "session_id": getattr(item, "session_id", None),
            "thread_id": getattr(item, "session_id", None) or (
                intervention_thread_map.get(item.intervention_id or "", (None, None))[0]
                if item.intervention_id
                else None
            ),
            "thread_label": _thread_label(
                getattr(item, "session_id", None) or (
                    intervention_thread_map.get(item.intervention_id or "", (None, None))[0]
                    if item.intervention_id
                    else None
                ),
                session_titles,
            ) or (
                intervention_thread_map.get(item.intervention_id or "", (None, None))[1]
                if item.intervention_id
                else None
            ),
            "thread_source": (
                "session"
                if getattr(item, "session_id", None)
                else "intervention_session"
                if item.intervention_id and intervention_thread_map.get(item.intervention_id or "", (None, None))[0]
                else "ambient"
            ),
            "continuation_mode": _continuation_mode(
                getattr(item, "session_id", None) or (
                    intervention_thread_map.get(item.intervention_id or "", (None, None))[0]
                    if item.intervention_id
                    else None
                )
            ),
            "resume_message": (
                f"Follow up on this deferred guardian item: "
                f"{item.content[:157] + '...' if len(item.content) > 160 else item.content}"
            ),
            "created_at": _timeline_timestamp(getattr(item, "created_at", None)),
        }
        for item in queued_insights
    ]
    recent_interventions_payload = [
        {
            "id": getattr(item, "id", None),
            "session_id": getattr(item, "session_id", None),
            "intervention_type": getattr(item, "intervention_type", None),
            "content_excerpt": getattr(item, "content_excerpt", "") or "",
            "policy_action": getattr(item, "policy_action", "") or "",
            "policy_reason": getattr(item, "policy_reason", "") or "",
            "delivery_decision": getattr(item, "delivery_decision", None),
            "latest_outcome": getattr(item, "latest_outcome", "") or "",
            "transport": getattr(item, "transport", None),
            "notification_id": getattr(item, "notification_id", None),
            "feedback_type": getattr(item, "feedback_type", None),
            "thread_id": getattr(item, "session_id", None),
            "thread_label": _thread_label(getattr(item, "session_id", None), session_titles),
            "thread_source": "session" if getattr(item, "session_id", None) else "ambient",
            "continuation_mode": _continuation_mode(getattr(item, "session_id", None)),
            "resume_message": (
                f"Continue from this guardian intervention: {getattr(item, 'content_excerpt', None)}"
                if getattr(item, "content_excerpt", None)
                else "Continue from this guardian intervention."
            ),
            "updated_at": _timeline_timestamp(getattr(item, "updated_at", None)),
            "continuity_surface": _continuity_surface(
                latest_outcome=getattr(item, "latest_outcome", None),
                transport=getattr(item, "transport", None),
                policy_action=getattr(item, "policy_action", None),
            ),
        }
        for item in recent_interventions
    ]
    thread_payload = _build_continuity_threads(
        notifications=notifications_payload,
        queued_insights=queued_insights_payload,
        recent_interventions=recent_interventions_payload,
    )
    recovery_actions_payload = _build_continuity_recovery_actions(
        route_statuses=reach_payload["route_statuses"],
        imported_reach=imported_reach_payload,
        source_adapters=source_adapter_payload,
        presence_surfaces=presence_surface_payload,
        threads=thread_payload,
    )
    summary_payload = _build_continuity_summary(
        notifications=notifications_payload,
        queued_insights=queued_insights_payload,
        recent_interventions=recent_interventions_payload,
        route_statuses=reach_payload["route_statuses"],
        imported_reach=imported_reach_payload,
        source_adapters=source_adapter_payload,
        presence_surfaces=presence_surface_payload,
        threads=thread_payload,
    )

    return {
        "daemon": await _daemon_status_payload(),
        "notifications": notifications_payload,
        "queued_insights": queued_insights_payload,
        "queued_insight_count": len(queued_insights),
        "recent_interventions": recent_interventions_payload,
        "reach": reach_payload,
        "imported_reach": imported_reach_payload,
        "source_adapters": source_adapter_payload,
        "presence_surfaces": presence_surface_payload,
        "summary": summary_payload,
        "threads": thread_payload,
        "recovery_actions": recovery_actions_payload,
    }


@router.get("/observer/continuity", response_model=ObserverContinuityResponse)
async def get_observer_continuity():
    """Return a single continuity snapshot for browser and daemon surfaces."""
    return await build_observer_continuity_snapshot()


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
