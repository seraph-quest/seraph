"""Secure capability-host decision receipts.

The secure host is the common policy envelope around tools, extensions,
delegated work, credential egress, provider fallback, and prompt-bearing
capability content. Existing runtime paths own the actual work; this module
keeps their least-privilege decision shape consistent and auditable.
"""

from __future__ import annotations

import hashlib
from typing import Any

from src.security.context_scan import scan_text_for_suspicious_context
from src.tools.policy import (
    get_tool_credential_egress_policy,
    get_tool_execution_boundaries,
    get_tool_risk_level,
    get_tool_source_context,
    tool_accepts_secret_refs,
)


def _stable_id(prefix: str, parts: list[str]) -> str:
    seed = "|".join(parts)
    return f"{prefix}_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def _dedupe_strings(values: list[Any] | tuple[Any, ...] | set[Any] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in list(values or []):
        item = str(value).strip()
        if not item or item in seen:
            continue
        normalized.append(item)
        seen.add(item)
    return normalized


def _contains_secret_ref(value: Any) -> bool:
    if isinstance(value, str):
        return "secret://" in value
    if isinstance(value, dict):
        return any(_contains_secret_ref(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_contains_secret_ref(item) for item in value)
    return False


def _secret_ref_fields(arguments: dict[str, Any] | None) -> list[str]:
    return sorted(
        key
        for key, value in (arguments or {}).items()
        if isinstance(key, str) and _contains_secret_ref(value)
    )


def _permission_profile(extension_permission_profile: dict[str, Any] | None) -> dict[str, Any]:
    profile = extension_permission_profile or {}
    missing = profile.get("missing")
    if isinstance(missing, dict):
        missing_tools = _dedupe_strings(missing.get("tools"))
        missing_boundaries = _dedupe_strings(missing.get("execution_boundaries"))
        missing_network = bool(missing.get("network"))
    else:
        missing_tools = _dedupe_strings(profile.get("missing_tools"))
        missing_boundaries = _dedupe_strings(profile.get("missing_execution_boundaries"))
        missing_network = bool(profile.get("missing_network"))
    status = str(profile.get("status") or ("granted" if not any([missing_tools, missing_boundaries, missing_network]) else "insufficient"))
    ok = bool(profile.get("ok", status == "granted"))
    return {
        "status": status,
        "ok": ok and not any([missing_tools, missing_boundaries, missing_network]),
        "missing_tools": missing_tools,
        "missing_execution_boundaries": missing_boundaries,
        "missing_network": missing_network,
        "permission_creep_blocked": bool(missing_tools or missing_boundaries or missing_network),
    }


def prompt_injection_receipt(text: str | None, *, include_fenced_blocks: bool = False) -> dict[str, Any]:
    findings = scan_text_for_suspicious_context(text or "", include_fenced_blocks=include_fenced_blocks)
    return {
        "status": "blocked" if findings else "clear",
        "finding_count": len(findings),
        "finding_codes": [finding.code for finding in findings],
        "findings": [
            {
                "code": finding.code,
                "description": finding.description,
                "excerpt": finding.excerpt,
            }
            for finding in findings
        ],
    }


def build_secure_capability_receipt(
    *,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    is_mcp: bool = False,
    tool: object | None = None,
    source: str = "tool",
    prompt_content: str | None = None,
    extension_permission_profile: dict[str, Any] | None = None,
    delegated_context: dict[str, Any] | None = None,
    provider_route: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic least-privilege decision receipt for a capability call."""
    canonical_tool = str(tool_name or "unknown")
    boundaries = _dedupe_strings(get_tool_execution_boundaries(canonical_tool, is_mcp=is_mcp, tool=tool))
    risk_level = get_tool_risk_level(canonical_tool, is_mcp=is_mcp)
    source_context = get_tool_source_context(tool)
    egress_policy = get_tool_credential_egress_policy(canonical_tool, is_mcp=is_mcp, tool=tool)
    secret_fields = _secret_ref_fields(arguments)
    accepts_secret_refs = tool_accepts_secret_refs(canonical_tool, is_mcp=is_mcp, tool=tool)
    credential_required = bool(secret_fields)
    allowed_hosts = _dedupe_strings((egress_policy or {}).get("allowed_hosts") if egress_policy else None)
    egress_allowed = (
        not credential_required
        or (
            ("external_mcp" in boundaries or is_mcp)
            and accepts_secret_refs
            and str((egress_policy or {}).get("mode") or "") == "explicit_host_allowlist"
            and bool(allowed_hosts)
        )
    )
    prompt_receipt = prompt_injection_receipt(prompt_content)
    permissions = _permission_profile(extension_permission_profile)
    delegation = delegated_context or {}
    route = provider_route or {}
    fallback_blocked = bool(route.get("fallback_blocked") or route.get("policy_violation"))
    blocked_reasons: list[str] = []
    if prompt_receipt["status"] == "blocked":
        blocked_reasons.append("prompt_injection_content")
    if not permissions["ok"]:
        blocked_reasons.append("permission_creep")
    if not egress_allowed:
        blocked_reasons.append("credential_egress_missing_allowlist")
    delegation_target_unresolved = bool(delegation.get("delegation_target_unresolved")) or bool(
        delegation
        and delegation.get("delegated_specialist")
        and not delegation.get("delegated_tool_names")
    )
    if delegation_target_unresolved:
        blocked_reasons.append("delegation_target_unresolved")
    if fallback_blocked:
        blocked_reasons.append("provider_fallback_trust_violation")

    decision = "blocked" if blocked_reasons else ("needs_approval" if risk_level == "high" else "allowed")
    receipt_id = _stable_id(
        "sch",
        [
            canonical_tool,
            source,
            risk_level,
            ",".join(boundaries),
            ",".join(secret_fields),
            ",".join(blocked_reasons),
        ],
    )
    return {
        "receipt_id": receipt_id,
        "decision": decision,
        "blocked_reasons": blocked_reasons,
        "tool_name": canonical_tool,
        "source": source,
        "risk_level": risk_level,
        "execution_boundaries": boundaries,
        "accepts_secret_refs": accepts_secret_refs,
        "credential_egress": {
            "required": credential_required,
            "secret_ref_fields": secret_fields,
            "allowed": egress_allowed,
            "mode": str((egress_policy or {}).get("mode") or "not_required"),
            "transport": str((egress_policy or {}).get("transport") or "not_required"),
            "allowed_hosts": allowed_hosts,
            "authenticated_source": bool((source_context or {}).get("authenticated_source")) if isinstance(source_context, dict) else False,
        },
        "prompt_injection": prompt_receipt,
        "extension_permissions": permissions,
        "delegation_partition": {
            "mode": str(delegation.get("trust_partition", {}).get("mode") or delegation.get("mode") or "not_delegated")
            if isinstance(delegation.get("trust_partition"), dict) or delegation
            else "not_delegated",
            "delegated_specialist": delegation.get("delegated_specialist"),
            "execution_boundaries": _dedupe_strings(delegation.get("execution_boundaries")),
            "blocked": delegation_target_unresolved,
        },
        "provider_fallback": {
            "selected_provider": str(route.get("selected_provider") or route.get("selected_source") or "default"),
            "fallback_used": bool(route.get("fallback_used")),
            "fallback_allowed": not fallback_blocked,
            "trust_state": "blocked" if fallback_blocked else str(route.get("trust_state") or "policy_checked"),
        },
        "operator_receipt": {
            "surface": "/api/operator/secure-capability-host-benchmark",
            "summary": f"{canonical_tool} {decision} under {risk_level} capability-host policy",
            "recoverable": bool(blocked_reasons),
        },
    }


def redact_secret_values_from_payload(value: Any, secret_values: list[str] | tuple[str, ...]) -> tuple[Any, bool]:
    """Recursively redact known secret values from tool output payloads."""
    filtered_values = sorted(
        {str(item) for item in secret_values if isinstance(item, str) and len(item) >= 6},
        key=len,
        reverse=True,
    )
    if not filtered_values:
        return value, False
    if isinstance(value, str):
        redacted = value
        for secret_value in filtered_values:
            redacted = redacted.replace(secret_value, "[redacted secret]")
        return redacted, redacted != value
    if isinstance(value, list):
        changed = False
        redacted_items: list[Any] = []
        for item in value:
            redacted_item, item_changed = redact_secret_values_from_payload(item, filtered_values)
            changed = changed or item_changed
            redacted_items.append(redacted_item)
        return redacted_items, changed
    if isinstance(value, tuple):
        redacted_list, changed = redact_secret_values_from_payload(list(value), filtered_values)
        return tuple(redacted_list), changed
    if isinstance(value, dict):
        changed = False
        redacted_dict: dict[Any, Any] = {}
        for key, item in value.items():
            redacted_item, item_changed = redact_secret_values_from_payload(item, filtered_values)
            changed = changed or item_changed
            redacted_dict[key] = redacted_item
        return redacted_dict, changed
    return value, False
