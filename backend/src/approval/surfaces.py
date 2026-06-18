"""Helpers for shaping approval data into operator-facing surfaces."""

from __future__ import annotations

from typing import Any, Mapping


def approval_surface_metadata(approval: Mapping[str, Any]) -> dict[str, Any]:
    approval_profile = approval.get("approval_profile")
    if not isinstance(approval_profile, dict):
        approval_profile = {}

    lifecycle_boundaries = approval.get("lifecycle_boundaries")
    if not isinstance(lifecycle_boundaries, list):
        profile_boundaries = approval_profile.get("lifecycle_boundaries")
        lifecycle_boundaries = profile_boundaries if isinstance(profile_boundaries, list) else []

    approval_scope = approval.get("approval_scope")
    if not isinstance(approval_scope, dict):
        approval_scope = None

    approval_context = approval.get("approval_context")
    if not isinstance(approval_context, dict):
        approval_context = None

    return {
        "approval_id": approval.get("id"),
        "tool_name": approval.get("tool_name"),
        "risk_level": approval.get("risk_level"),
        "extension_id": approval.get("extension_id"),
        "extension_display_name": approval.get("extension_display_name"),
        "extension_action": approval.get("action") or approval.get("extension_action"),
        "package_path": approval.get("package_path"),
        "permissions": approval.get("permissions"),
        "requires_lifecycle_approval": bool(
            approval_profile.get("requires_lifecycle_approval", False)
        ),
        "lifecycle_boundaries": lifecycle_boundaries,
        "approval_scope": approval_scope,
        "approval_context": approval_context,
    }
