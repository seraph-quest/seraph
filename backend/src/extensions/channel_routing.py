"""Channel routing bindings and runtime reach status for proactive delivery surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUPPORTED_CHANNEL_ROUTE_TRANSPORTS = ("websocket", "native_notification")
_TRANSPORT_LABELS = {
    "websocket": "browser websocket",
    "native_notification": "native notification",
}


@dataclass(frozen=True)
class ChannelRouteSpec:
    route: str
    label: str
    default_primary_transport: str
    default_fallback_transport: str | None
    description: str


@dataclass(frozen=True)
class ChannelRouteBinding:
    route: str
    label: str
    primary_transport: str
    fallback_transport: str | None
    description: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "label": self.label,
            "primary_transport": self.primary_transport,
            "fallback_transport": self.fallback_transport,
            "description": self.description,
        }


_CHANNEL_ROUTE_SPECS = (
    ChannelRouteSpec(
        route="live_delivery",
        label="Live delivery",
        default_primary_transport="websocket",
        default_fallback_transport="native_notification",
        description="Immediate advisory delivery while the workspace is live.",
    ),
    ChannelRouteSpec(
        route="alert_delivery",
        label="Alert delivery",
        default_primary_transport="native_notification",
        default_fallback_transport="websocket",
        description="Urgent alerts that should break through with the strongest surface first.",
    ),
    ChannelRouteSpec(
        route="scheduled_delivery",
        label="Scheduled delivery",
        default_primary_transport="native_notification",
        default_fallback_transport="websocket",
        description="Scheduled proactive updates and reminders.",
    ),
    ChannelRouteSpec(
        route="bundle_delivery",
        label="Bundle delivery",
        default_primary_transport="websocket",
        default_fallback_transport="native_notification",
        description="Queued bundle delivery when blocked work becomes visible again.",
    ),
)

_CHANNEL_ROUTE_SPECS_BY_NAME = {item.route: item for item in _CHANNEL_ROUTE_SPECS}


def channel_route_specs() -> tuple[ChannelRouteSpec, ...]:
    return _CHANNEL_ROUTE_SPECS


def _route_bindings_bucket(payload: dict[str, Any], *, create: bool = False) -> dict[str, Any]:
    channel_routing = payload.get("channel_routing")
    if not isinstance(channel_routing, dict):
        if not create:
            return {}
        channel_routing = {}
        payload["channel_routing"] = channel_routing
    bindings = channel_routing.get("bindings")
    if not isinstance(bindings, dict):
        if not create:
            return {}
        bindings = {}
        channel_routing["bindings"] = bindings
    return bindings


def list_channel_route_bindings(payload: dict[str, Any] | None) -> list[ChannelRouteBinding]:
    payload = payload if isinstance(payload, dict) else {}
    bindings = _route_bindings_bucket(payload, create=False)
    resolved: list[ChannelRouteBinding] = []
    for spec in _CHANNEL_ROUTE_SPECS:
        raw_entry = bindings.get(spec.route)
        primary_transport = spec.default_primary_transport
        fallback_transport = spec.default_fallback_transport
        if isinstance(raw_entry, dict):
            raw_primary = raw_entry.get("primary_transport")
            raw_fallback = raw_entry.get("fallback_transport")
            if isinstance(raw_primary, str) and raw_primary in SUPPORTED_CHANNEL_ROUTE_TRANSPORTS:
                primary_transport = raw_primary
            if raw_fallback is None:
                fallback_transport = None
            elif isinstance(raw_fallback, str) and raw_fallback in SUPPORTED_CHANNEL_ROUTE_TRANSPORTS:
                fallback_transport = raw_fallback
        if fallback_transport == primary_transport:
            fallback_transport = None
        resolved.append(
            ChannelRouteBinding(
                route=spec.route,
                label=spec.label,
                primary_transport=primary_transport,
                fallback_transport=fallback_transport,
                description=spec.description,
            )
        )
    return resolved


def get_channel_route_binding(
    payload: dict[str, Any] | None,
    route: str,
) -> ChannelRouteBinding:
    bindings = list_channel_route_bindings(payload)
    for item in bindings:
        if item.route == route:
            return item
    raise KeyError(route)


def set_channel_route_binding(
    payload: dict[str, Any],
    *,
    route: str,
    primary_transport: str,
    fallback_transport: str | None,
) -> None:
    if route not in _CHANNEL_ROUTE_SPECS_BY_NAME:
        raise ValueError(f"Unknown channel route '{route}'.")
    if primary_transport not in SUPPORTED_CHANNEL_ROUTE_TRANSPORTS:
        raise ValueError(f"Unsupported channel route transport '{primary_transport}'.")
    if fallback_transport is not None and fallback_transport not in SUPPORTED_CHANNEL_ROUTE_TRANSPORTS:
        raise ValueError(f"Unsupported channel route fallback transport '{fallback_transport}'.")
    bindings = _route_bindings_bucket(payload, create=True)
    bindings[route] = {
        "primary_transport": primary_transport,
        "fallback_transport": (
            None if fallback_transport is None or fallback_transport == primary_transport else fallback_transport
        ),
    }


def ordered_route_transports(
    payload: dict[str, Any] | None,
    *,
    route: str,
    active_transports: set[str],
) -> tuple[ChannelRouteBinding, list[str]]:
    binding = get_channel_route_binding(payload, route)
    ordered: list[str] = []
    if binding.primary_transport in active_transports:
        ordered.append(binding.primary_transport)
    if (
        binding.fallback_transport is not None
        and binding.fallback_transport in active_transports
        and binding.fallback_transport not in ordered
    ):
        ordered.append(binding.fallback_transport)
    return binding, ordered


def _transport_label(transport: str) -> str:
    return _TRANSPORT_LABELS.get(transport, transport.replace("_", " "))


def _normalized_websocket_connection_count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(value, 0)
    return 0


def _normalized_daemon_connected(value: Any) -> bool:
    return value if isinstance(value, bool) else False


def transport_runtime_status(
    transport: str,
    *,
    active_transports: set[str],
    websocket_connection_count: int,
    daemon_connected: bool,
) -> dict[str, Any]:
    websocket_connection_count = _normalized_websocket_connection_count(websocket_connection_count)
    daemon_connected = _normalized_daemon_connected(daemon_connected)
    if transport not in active_transports:
        return {
            "transport": transport,
            "label": _transport_label(transport),
            "status": "inactive",
            "available": False,
            "summary": "No active channel adapter currently owns this transport.",
            "repair_hint": "Enable a packaged channel adapter for this transport or keep the builtin route active.",
        }
    if transport == "websocket":
        if websocket_connection_count > 0:
            return {
                "transport": transport,
                "label": _transport_label(transport),
                "status": "ready",
                "available": True,
                "summary": (
                    f"{websocket_connection_count} live browser session"
                    f"{'' if websocket_connection_count == 1 else 's'} can receive delivery."
                ),
                "repair_hint": None,
                "active_connection_count": websocket_connection_count,
            }
        return {
            "transport": transport,
            "label": _transport_label(transport),
            "status": "waiting_for_browser",
            "available": False,
            "summary": "No live browser session is connected for websocket delivery.",
            "repair_hint": "Keep a cockpit tab connected or route this delivery class to native notifications first.",
            "active_connection_count": websocket_connection_count,
        }
    if transport == "native_notification":
        if daemon_connected:
            return {
                "transport": transport,
                "label": _transport_label(transport),
                "status": "ready",
                "available": True,
                "summary": "Native daemon is connected and ready for desktop delivery.",
                "repair_hint": None,
                "daemon_connected": daemon_connected,
            }
        return {
            "transport": transport,
            "label": _transport_label(transport),
            "status": "daemon_offline",
            "available": False,
            "summary": "Native daemon is offline, so desktop delivery is unavailable.",
            "repair_hint": "Reconnect the native daemon or route this delivery class to websocket first.",
            "daemon_connected": daemon_connected,
        }
    return {
        "transport": transport,
        "label": _transport_label(transport),
        "status": "unsupported",
        "available": False,
        "summary": f"Transport '{transport}' is not supported by the current runtime.",
        "repair_hint": None,
    }


def route_runtime_status(
    payload: dict[str, Any] | None,
    *,
    route: str,
    active_transports: set[str],
    websocket_connection_count: int,
    daemon_connected: bool,
) -> tuple[ChannelRouteBinding, dict[str, Any]]:
    binding = get_channel_route_binding(payload, route)
    configured_order: list[str] = []
    for candidate in (binding.primary_transport, binding.fallback_transport):
        if candidate and candidate not in configured_order:
            configured_order.append(candidate)

    transport_statuses = [
        transport_runtime_status(
            transport,
            active_transports=active_transports,
            websocket_connection_count=websocket_connection_count,
            daemon_connected=daemon_connected,
        )
        for transport in configured_order
    ]
    available_order = [
        item["transport"]
        for item in transport_statuses
        if bool(item.get("available"))
    ]
    selected_transport = available_order[0] if available_order else None
    selected_mode: str | None = None
    status = "unavailable"
    summary: str
    repair_hint: str | None = None
    failure_reason: str | None = None

    if selected_transport == binding.primary_transport:
        selected_mode = "primary"
        status = "ready"
        summary = f"{binding.label} will use {_transport_label(selected_transport)}."
    elif selected_transport is not None:
        selected_mode = "fallback"
        status = "fallback_active"
        primary_status = transport_statuses[0] if transport_statuses else None
        failure_reason = (
            str(primary_status.get("status"))
            if isinstance(primary_status, dict) and isinstance(primary_status.get("status"), str)
            else None
        )
        repair_hint = (
            str(primary_status.get("repair_hint"))
            if isinstance(primary_status, dict) and isinstance(primary_status.get("repair_hint"), str)
            else None
        )
        summary = (
            f"{binding.label} is falling back to {_transport_label(selected_transport)}"
            f" because {str(primary_status.get('summary') or '').rstrip('.') if isinstance(primary_status, dict) else 'the primary transport is unavailable'}."
        )
    else:
        if transport_statuses:
            failure_reason = "+".join(
                str(item.get("status"))
                for item in transport_statuses
                if isinstance(item.get("status"), str)
            ) or None
            repair_hint = next(
                (
                    str(item.get("repair_hint"))
                    for item in transport_statuses
                    if isinstance(item.get("repair_hint"), str) and str(item.get("repair_hint")).strip()
                ),
                None,
            )
            joined = " ".join(str(item.get("summary") or "").rstrip(".") for item in transport_statuses).strip()
            summary = f"{binding.label} has no available transport. {joined}."
        else:
            failure_reason = "no_configured_route_transport"
            summary = f"{binding.label} has no configured transport."

    payload = {
        "route": binding.route,
        "label": binding.label,
        "description": binding.description,
        "primary_transport": binding.primary_transport,
        "fallback_transport": binding.fallback_transport,
        "configured_order": configured_order,
        "delivery_order": available_order,
        "selected_transport": selected_transport,
        "selected_mode": selected_mode,
        "status": status,
        "summary": summary,
        "repair_hint": repair_hint,
        "failure_reason": failure_reason,
        "transports": transport_statuses,
    }
    return binding, payload


def route_runtime_statuses(
    payload: dict[str, Any] | None,
    *,
    active_transports: set[str],
    websocket_connection_count: int,
    daemon_connected: bool,
) -> list[dict[str, Any]]:
    return [
        route_runtime_status(
            payload,
            route=spec.route,
            active_transports=active_transports,
            websocket_connection_count=websocket_connection_count,
            daemon_connected=daemon_connected,
        )[1]
        for spec in _CHANNEL_ROUTE_SPECS
    ]
