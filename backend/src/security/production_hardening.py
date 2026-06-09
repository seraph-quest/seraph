"""Production secure-host hardening proof receipts.

This module is the Batch BW gate above the deterministic M3 secure-host
foundation. It records the live privileged-path controls that must stay visible
before Seraph can make stronger security or parity claims.
"""

from __future__ import annotations

from typing import Any

from src.security.site_policy import evaluate_site_access


PRODUCTION_SECURE_HOST_HARDENING_SUITE_NAME = "production_secure_host_hardening"
SECURE_CAPABILITY_HOST_LIVE_ISOLATION_V2_SUITE_NAME = "secure_capability_host_live_isolation_v2"

PRODUCTION_SECURE_HOST_HARDENING_SCENARIO_NAMES = (
    "production_secure_host_batch_contract_behavior",
    "production_secure_host_receipt_schema_behavior",
    "production_secure_host_claim_boundary_behavior",
    "operator_production_secure_host_hardening_surface_behavior",
)

SECURE_CAPABILITY_HOST_LIVE_ISOLATION_V2_SCENARIO_NAMES = (
    "secure_host_live_secret_redaction_replay_behavior",
    "secure_host_live_browser_recovery_partition_behavior",
    "secure_host_live_private_network_egress_behavior",
    "secure_host_live_extension_revocation_behavior",
    "secure_host_live_workflow_replay_trust_drift_behavior",
)

PRODUCTION_SECURE_HOST_HARDENING_CLAIM_BOUNDARY = (
    "production_secure_host_hardening_proof_not_secure_private_by_default_or_ironclaw_class"
)

PRODUCTION_SECURE_HOST_BLOCKED_CLAIMS = (
    "secure_private_by_default",
    "production_security",
    "production_ready_execution",
    "ironclaw_class_secure_execution",
    "safe_autonomous_computer_use",
    "production_secure_marketplace",
    "full_parity",
    "reference_systems_exceeded",
)

PRODUCTION_SECURE_HOST_RECEIPT_SCHEMA = (
    "attempted_action",
    "authority_source",
    "actor_or_session",
    "trust_boundary",
    "isolation_mode",
    "credential_or_evidence_exposure",
    "egress_target",
    "policy_decision",
    "redaction_status",
    "blocked_claims",
    "residual_risk",
    "recovery_action",
    "linked_proof_run",
)

_TRUST_CLASS_ORDER = {
    "same_or_narrower_trust_class": 0,
    "local": 0,
    "workspace": 1,
    "managed_connector": 2,
    "remote_provider": 3,
    "external_untrusted": 4,
}


def _recovery_action_for(reasons: list[str]) -> str:
    if "redaction_failure" in reasons or "raw_secret_exposure" in reasons:
        return "redact_or_drop_result_and_issue_fresh_session_bound_ref"
    if "private_network_egress" in reasons:
        return "require_explicit_private_network_policy_and_operator_review"
    if "extension_revoked" in reasons:
        return "rollback_or_review_extension_before_contribution_reenable"
    if "trust_class_expansion" in reasons:
        return "resume_from_verified_checkpoint_under_same_or_narrower_trust_class"
    if "missing_required_receipt_field" in reasons:
        return "deny_until_privileged_path_receipt_is_complete"
    return "continue_under_current_policy"


def build_production_secure_host_enforcement_receipt(
    *,
    attempted_action: str,
    authority_source: str,
    actor_or_session: str,
    trust_boundary: str,
    isolation_mode: str,
    credential_or_evidence_exposure: str,
    egress_target: str = "",
    redaction_status: str = "not_required",
    source_trust_class: str = "local",
    target_trust_class: str = "local",
    governance_status: dict[str, Any] | None = None,
    linked_proof_run: str = SECURE_CAPABILITY_HOST_LIVE_ISOLATION_V2_SUITE_NAME,
) -> dict[str, Any]:
    """Return an allow/deny/recover receipt for a privileged path.

    This is intentionally conservative: incomplete context, redaction failure,
    private-network egress, revoked extension governance, and trust expansion
    all fail closed before the action can be represented as allowed.
    """

    blocked_reasons: list[str] = []
    required_values = {
        "attempted_action": attempted_action,
        "authority_source": authority_source,
        "actor_or_session": actor_or_session,
        "trust_boundary": trust_boundary,
        "isolation_mode": isolation_mode,
    }
    if any(not str(value or "").strip() for value in required_values.values()):
        blocked_reasons.append("missing_required_receipt_field")

    exposure = str(credential_or_evidence_exposure or "").lower()
    if "raw_secret" in exposure or "unredacted_secret" in exposure:
        blocked_reasons.append("raw_secret_exposure")
    if redaction_status in {"failed", "redaction_failed", "unverified"}:
        blocked_reasons.append("redaction_failure")

    if egress_target:
        decision = evaluate_site_access(egress_target, resolve_dns=False)
        if not decision.allowed and decision.reason == "internal_private":
            blocked_reasons.append("private_network_egress")

    governance = governance_status or {}
    if bool(governance.get("fail_closed")) or str(governance.get("revocation_status") or "") == "revoked":
        blocked_reasons.append("extension_revoked")

    source_rank = _TRUST_CLASS_ORDER.get(str(source_trust_class), 999)
    target_rank = _TRUST_CLASS_ORDER.get(str(target_trust_class), 999)
    if target_rank > source_rank:
        blocked_reasons.append("trust_class_expansion")

    blocked_reasons = list(dict.fromkeys(blocked_reasons))
    policy_decision = "blocked" if blocked_reasons else "allowed"
    return {
        "attempted_action": attempted_action,
        "authority_source": authority_source,
        "actor_or_session": actor_or_session,
        "trust_boundary": trust_boundary,
        "isolation_mode": isolation_mode or "missing",
        "credential_or_evidence_exposure": credential_or_evidence_exposure or "unknown",
        "egress_target": egress_target or "not_requested",
        "policy_decision": policy_decision,
        "redaction_status": redaction_status,
        "blocked_claims": list(PRODUCTION_SECURE_HOST_BLOCKED_CLAIMS),
        "residual_risk": PRODUCTION_SECURE_HOST_HARDENING_CLAIM_BOUNDARY,
        "recovery_action": _recovery_action_for(blocked_reasons),
        "linked_proof_run": linked_proof_run,
        "blocked_reasons": blocked_reasons,
    }


def production_secure_host_operator_surfaces() -> list[str]:
    return [
        "/api/operator/secure-capability-host-hardening",
        "/api/operator/secure-capability-host-benchmark",
        "/api/operator/trust-boundary-benchmark",
        "/api/operator/benchmark-proof",
        "/api/activity/ledger",
    ]


def production_secure_host_negative_cases() -> list[dict[str, Any]]:
    return [
        {
            "case": "secret_ref_replay_or_redaction_failure",
            "privileged_path": "secret_ref_connector_call",
            "blocked_reasons": [
                "expired_or_cross_session_ref",
                "destination_host_mismatch",
                "undeclared_secret_ref_field",
                "redaction_failure",
            ],
            "policy_decision": "blocked",
            "recovery_action": "issue_fresh_session_bound_ref_and_retry_allowed_host",
        },
        {
            "case": "browser_profile_recovery_bleed",
            "privileged_path": "browser_computer_use",
            "blocked_reasons": [
                "owner_session_mismatch",
                "cookie_or_storage_state_visible",
                "recovery_reuses_profile_partition",
            ],
            "policy_decision": "blocked",
            "recovery_action": "open_new_per_session_context_without_storage_state",
        },
        {
            "case": "private_network_or_ssrf_egress",
            "privileged_path": "browser_connector_provider_extension",
            "blocked_reasons": [
                "loopback_or_private_ip_destination",
                "dns_resolves_private_address",
                "redirect_or_subrequest_private_target",
            ],
            "policy_decision": "blocked",
            "recovery_action": "require_explicit_private_network_policy_and_operator_review",
        },
        {
            "case": "revoked_extension_runtime_contribution",
            "privileged_path": "extension_tool_prompt_connector_browser_provider_delegation",
            "blocked_reasons": [
                "governance_revoked",
                "permission_creep",
                "runtime_contribution_after_revocation",
            ],
            "policy_decision": "blocked",
            "recovery_action": "rollback_or_review_extension_before_contribution_reenable",
        },
        {
            "case": "workflow_replay_or_provider_trust_expansion",
            "privileged_path": "workflow_replay_provider_fallback_delegation",
            "blocked_reasons": [
                "trust_class_expansion",
                "checkpoint_context_boundary_drift",
                "sensitive_context_replay",
            ],
            "policy_decision": "blocked",
            "recovery_action": "resume_from_verified_checkpoint_under_same_or_narrower_trust_class",
        },
    ]


def secure_host_live_isolation_controls() -> list[dict[str, Any]]:
    return [
        {
            "control": "session_bound_secret_ref_resolution",
            "covers": ["expiry", "cross_session_replay", "destination_binding", "field_allowlist"],
            "receipt_required": True,
        },
        {
            "control": "redaction_verified_persistence",
            "covers": ["tool_output", "audit_payload", "operator_receipt", "provider_replay"],
            "receipt_required": True,
        },
        {
            "control": "per_session_browser_recovery_partition",
            "covers": ["owner_session", "cookie_omission", "storage_state_omission", "recovery_profile"],
            "receipt_required": True,
        },
        {
            "control": "private_network_egress_deny",
            "covers": ["loopback", "rfc1918", "link_local", "dns_resolution", "redirects"],
            "receipt_required": True,
        },
        {
            "control": "revoked_extension_contribution_cutoff",
            "covers": ["tools", "prompts", "connectors", "browser_providers", "background_tasks", "delegation"],
            "receipt_required": True,
        },
        {
            "control": "same_or_narrower_trust_replay",
            "covers": ["workflow_checkpoint", "provider_fallback", "delegated_specialist", "sensitive_context"],
            "receipt_required": True,
        },
    ]


def production_secure_host_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": PRODUCTION_SECURE_HOST_HARDENING_SUITE_NAME,
        "child_suite_names": [
            PRODUCTION_SECURE_HOST_HARDENING_SUITE_NAME,
            SECURE_CAPABILITY_HOST_LIVE_ISOLATION_V2_SUITE_NAME,
        ],
        "foundation_suite": "secure_capability_host",
        "claim_boundary": PRODUCTION_SECURE_HOST_HARDENING_CLAIM_BOUNDARY,
        "blocked_claims": list(PRODUCTION_SECURE_HOST_BLOCKED_CLAIMS),
        "receipt_schema": list(PRODUCTION_SECURE_HOST_RECEIPT_SCHEMA),
        "receipt_surfaces": production_secure_host_operator_surfaces(),
        "current_source_policy": (
            "Any new IronClaw, TEE/CVM, Wasm/container, vault, endpoint-allowlist, leak-detection, "
            "or security-parity claim must cite current official URLs and access dates or remain Unknown."
        ),
        "ci_gate_mode": "required_benchmark_suites",
        "not_claimed": [
            "secure_private_by_default",
            "production_ready_execution",
            "ironclaw_class_secure_execution",
            "full_host_container_isolation",
            "tee_or_wasm_runtime_isolation",
            "production_secure_marketplace",
        ],
    }


def production_secure_host_summary(*, failed: int) -> dict[str, Any]:
    healthy = failed == 0
    degraded_state = "regressions_detected"
    return {
        "suite_name": PRODUCTION_SECURE_HOST_HARDENING_SUITE_NAME,
        "benchmark_posture": (
            "production_secure_host_hardening_ci_gated_operator_visible"
            if healthy
            else "production_secure_host_hardening_regressions_detected_operator_visible"
        ),
        "operator_status": "production_secure_host_hardening_receipts_visible",
        "claim_boundary": PRODUCTION_SECURE_HOST_HARDENING_CLAIM_BOUNDARY,
        "child_suite_count": 2,
        "scenario_count": (
            len(PRODUCTION_SECURE_HOST_HARDENING_SCENARIO_NAMES)
            + len(SECURE_CAPABILITY_HOST_LIVE_ISOLATION_V2_SCENARIO_NAMES)
        ),
        "active_failure_count": failed,
        "live_isolation_state": "privileged_paths_fail_closed_with_receipts" if healthy else degraded_state,
        "secret_redaction_state": "replay_and_redaction_fail_closed" if healthy else degraded_state,
        "browser_recovery_partition_state": "per_session_recovery_without_profile_bleed" if healthy else degraded_state,
        "private_network_egress_state": "private_targets_blocked_with_receipts" if healthy else degraded_state,
        "extension_revocation_state": "runtime_contributions_cut_off_after_revocation" if healthy else degraded_state,
        "workflow_provider_replay_state": "same_or_narrower_trust_replay_required" if healthy else degraded_state,
        "operator_receipt_state": "allow_deny_recover_receipts_visible" if healthy else degraded_state,
    }


def production_secure_host_enforcement_receipts() -> list[dict[str, Any]]:
    return [
        build_production_secure_host_enforcement_receipt(
            attempted_action="secret_ref_connector_call",
            authority_source="seraph_policy",
            actor_or_session="session-1",
            trust_boundary="secret_ref_connector_call",
            isolation_mode="session_bound_secret_ref_resolution",
            credential_or_evidence_exposure="raw_secret_echo_detected",
            egress_target="https://api.example.com",
            redaction_status="failed",
        ),
        build_production_secure_host_enforcement_receipt(
            attempted_action="browser_recovery",
            authority_source="browser_session_runtime",
            actor_or_session="session-1",
            trust_boundary="browser_computer_use",
            isolation_mode="per_session_browser_recovery_partition",
            credential_or_evidence_exposure="cookie_or_storage_state_omitted",
            egress_target="https://example.com",
            redaction_status="not_required",
        ),
        build_production_secure_host_enforcement_receipt(
            attempted_action="connector_private_network_fetch",
            authority_source="site_policy",
            actor_or_session="session-1",
            trust_boundary="browser_connector_provider_extension",
            isolation_mode="private_network_egress_deny",
            credential_or_evidence_exposure="no_secret_exposure",
            egress_target="http://127.0.0.1/secret",
            redaction_status="not_required",
        ),
        build_production_secure_host_enforcement_receipt(
            attempted_action="extension_runtime_contribution",
            authority_source="extension_governance",
            actor_or_session="session-1",
            trust_boundary="extension_tool_prompt_connector_browser_provider_delegation",
            isolation_mode="revoked_extension_contribution_cutoff",
            credential_or_evidence_exposure="no_secret_exposure",
            egress_target="https://example.com",
            redaction_status="not_required",
            governance_status={"fail_closed": True, "revocation_status": "revoked"},
        ),
        build_production_secure_host_enforcement_receipt(
            attempted_action="workflow_replay_provider_fallback",
            authority_source="workflow_replay_policy",
            actor_or_session="workflow-run-1",
            trust_boundary="workflow_replay_provider_fallback_delegation",
            isolation_mode="same_or_narrower_trust_replay",
            credential_or_evidence_exposure="sensitive_context_replay_blocked",
            egress_target="https://api.example.com",
            redaction_status="verified",
            source_trust_class="workspace",
            target_trust_class="remote_provider",
        ),
    ]


def _production_secure_host_failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append(
            {
                "type": "benchmark_regression",
                "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
                "summary": str(getattr(result, "error", "") or "Production secure-host hardening scenario failed."),
                "reason": "production_secure_host_eval_failure",
            }
        )
    return failures[:10]


async def _run_production_secure_host_hardening_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites(
        [
            PRODUCTION_SECURE_HOST_HARDENING_SUITE_NAME,
            SECURE_CAPABILITY_HOST_LIVE_ISOLATION_V2_SUITE_NAME,
        ]
    )


async def build_production_secure_host_hardening_report() -> dict[str, Any]:
    summary = await _run_production_secure_host_hardening_suites()
    return {
        "summary": production_secure_host_summary(failed=summary.failed),
        "scenario_names": [
            *PRODUCTION_SECURE_HOST_HARDENING_SCENARIO_NAMES,
            *SECURE_CAPABILITY_HOST_LIVE_ISOLATION_V2_SCENARIO_NAMES,
        ],
        "negative_cases": production_secure_host_negative_cases(),
        "live_isolation_controls": secure_host_live_isolation_controls(),
        "enforcement_receipts": production_secure_host_enforcement_receipts(),
        "receipt_schema": list(PRODUCTION_SECURE_HOST_RECEIPT_SCHEMA),
        "operator_surfaces": production_secure_host_operator_surfaces(),
        "policy": production_secure_host_policy_payload(),
        "failure_report": _production_secure_host_failure_report(summary),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
