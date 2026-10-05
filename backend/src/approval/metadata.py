"""Bounded, server-owned approval metadata for interactive transports.

Approval rows may contain private tool arguments and workflow details.  Chat
transports must expose only the small typed posture/permission receipt needed
to render an approval boundary.  This module deliberately drops every other
field and never infers a host permission from a summary string.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_EXECUTOR_KINDS = frozenset({"local", "docker_rootless", "docker_rootful"})
_STATUS_VALUES = frozenset({"pending", "approved", "consumed", "expired", "rejected"})
_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_SAFE_TEXT_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")


def _nested_value(details: Mapping[str, Any], key: str) -> Any:
    if key in details:
        return details.get(key)
    nested = details.get("approval_context")
    if isinstance(nested, Mapping):
        return nested.get(key)
    return None


def _safe_permissions(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in list(value)[:16]:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text or len(text) > 128 or not _SAFE_TEXT_RE.fullmatch(text):
            continue
        if text not in result:
            result.append(text)
    return result


def approval_wire_metadata(details: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return the safe typed approval receipt from server-persisted details.

    A local host boundary is authoritative only when both the explicit boolean
    and the exact ``local_host_execution`` permission are present.  Missing or
    contradictory metadata stays absent/false so callers render a generic,
    locked approval rather than adopting a posture from free-form text.
    """

    if not isinstance(details, Mapping):
        return {}
    permissions = _safe_permissions(
        _nested_value(details, "required_permissions")
    )
    local_flag = _nested_value(details, "local_host_execution_required")
    local_required: bool | None
    if type(local_flag) is bool:
        local_required = bool(local_flag and "local_host_execution" in permissions)
    else:
        local_required = None

    output: dict[str, Any] = {}
    if permissions:
        output["required_permissions"] = permissions
    if local_required is not None:
        output["local_host_execution_required"] = local_required

    executor_kind = _nested_value(details, "executor_kind")
    if isinstance(executor_kind, str) and executor_kind in _EXECUTOR_KINDS:
        output["executor_kind"] = executor_kind
    for field_name in ("executor_profile", "status"):
        value = _nested_value(details, field_name)
        if isinstance(value, str) and len(value) <= 256:
            if field_name == "status" and value not in _STATUS_VALUES:
                continue
            if field_name == "executor_profile" and not _SAFE_TEXT_RE.fullmatch(value):
                continue
            output[field_name] = value
    posture_digest = _nested_value(details, "executor_posture_digest")
    if isinstance(posture_digest, str) and _DIGEST_RE.fullmatch(posture_digest):
        output["executor_posture_digest"] = posture_digest.lower()
    for field_name in ("preparation_ready", "execution_ready", "operator_visible"):
        value = _nested_value(details, field_name)
        if type(value) is bool:
            output[field_name] = value

    expires = _nested_value(details, "expires_at")
    if expires is not None:
        try:
            value = float(expires)
        except (TypeError, ValueError, OverflowError):
            value = None
        if value is not None and value > 0:
            output["expires_at"] = value
    return output


__all__ = ["approval_wire_metadata"]
