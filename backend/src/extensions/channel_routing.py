"""Channel routing bindings for proactive delivery surfaces."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


SUPPORTED_CHANNEL_ROUTE_TRANSPORTS = ("websocket", "native_notification")


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
