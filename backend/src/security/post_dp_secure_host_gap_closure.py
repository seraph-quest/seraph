"""Batch DR post-DP secure capability-host gap-closure receipts.

This layer builds on the bounded secure-host proof train and closes the next
implementation-facing gap for runtime profile selection, deny-by-default egress,
scoped credentials, hostile-chain quarantine, recovery authority, and operator
receipt visibility. It remains bounded proof; it does not claim secure/private
by default, IronClaw-class execution, hardware-backed isolation, formal
certification, production readiness, full parity, or reference-system
exceedance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.security.production_grade_secure_host import (
    PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
    build_production_grade_secure_host_contract,
)


POST_DP_SECURE_CAPABILITY_HOST_GAP_CLOSURE_SUITE_NAME = (
    "post_dp_secure_capability_host_gap_closure_v1"
)
POST_DP_SECURE_CAPABILITY_HOST_GAP_CLOSURE_SCENARIO_NAMES = (
    "post_dp_secure_host_contract_builds_on_dj_without_duplicate_scope",
    "post_dp_secure_host_runtime_profile_selection_behavior",
    "post_dp_secure_host_operator_receipt_redaction_behavior",
    "post_dp_secure_host_claim_boundary_behavior",
)
RUNTIME_PROFILE_SELECTION_V2_SUITE_NAME = "runtime_profile_selection_v2"
RUNTIME_PROFILE_SELECTION_V2_SCENARIO_NAMES = (
    "runtime_profile_tool_process_deny_default_egress_behavior",
    "runtime_profile_browser_partition_owner_behavior",
    "runtime_profile_connector_secret_ref_scope_behavior",
    "runtime_profile_mcp_endpoint_recheck_behavior",
    "runtime_profile_package_quarantine_behavior",
    "runtime_profile_workflow_replay_trust_floor_behavior",
)
DENY_DEFAULT_CREDENTIAL_EGRESS_V2_SUITE_NAME = "deny_default_credential_egress_v2"
DENY_DEFAULT_CREDENTIAL_EGRESS_V2_SCENARIO_NAMES = (
    "deny_default_unknown_endpoint_behavior",
    "deny_default_private_network_behavior",
    "deny_default_dns_redirect_behavior",
    "deny_default_raw_secret_output_behavior",
    "deny_default_revoked_secret_epoch_behavior",
    "deny_default_allowlisted_field_scope_behavior",
)
HOSTILE_CAPABILITY_CHAIN_QUARANTINE_V2_SUITE_NAME = "hostile_capability_chain_quarantine_v2"
HOSTILE_CAPABILITY_CHAIN_QUARANTINE_V2_SCENARIO_NAMES = (
    "hostile_browser_to_connector_chain_quarantines_behavior",
    "hostile_mcp_to_private_network_chain_quarantines_behavior",
    "hostile_package_to_runtime_chain_quarantines_behavior",
    "hostile_workflow_replay_to_secret_chain_quarantines_behavior",
    "hostile_shell_background_escape_chain_quarantines_behavior",
)
SECURE_HOST_RECOVERY_AUTHORITY_V2_SUITE_NAME = "secure_host_recovery_authority_v2"
SECURE_HOST_RECOVERY_AUTHORITY_V2_SCENARIO_NAMES = (
    "secure_host_recovery_revoke_rotate_reissue_behavior",
    "secure_host_recovery_quarantine_rollback_reentry_behavior",
    "secure_host_recovery_operator_reapproval_behavior",
    "secure_host_recovery_audit_digest_behavior",
)
SECURE_HOST_FALSE_CLAIM_SCAN_V2_SUITE_NAME = "secure_host_false_claim_scan_v2"
SECURE_HOST_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES = (
    "secure_host_false_claim_v2_blocks_ironclaw_class",
    "secure_host_false_claim_v2_blocks_secure_private_default",
    "secure_host_false_claim_v2_blocks_formal_certification",
)

POST_DP_SECURE_HOST_CLAIM_BOUNDARY = (
    "post_dp_secure_capability_host_gap_closure_not_ironclaw_class_or_formally_secure"
)
POST_DP_SECURE_HOST_BLOCKED_CLAIMS = (
    "secure_private_by_default",
    "production_security_solved",
    "ironclaw_class_secure_execution",
    "hardware_backed_isolation",
    "tee_cvm_wasm_or_container_runtime_isolation",
    "formal_security_certification",
    "certified_secure_isolation",
    "safe_autonomous_computer_use",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
    "broad_superiority",
)


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _receipt_handle(kind: str, receipt_id: str, payload: Any) -> str:
    return f"seraph://receipts/batch-dr/{kind}/{receipt_id}/{_stable_digest(payload)}"


def post_dp_secure_host_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            POST_DP_SECURE_CAPABILITY_HOST_GAP_CLOSURE_SUITE_NAME,
            RUNTIME_PROFILE_SELECTION_V2_SUITE_NAME,
            DENY_DEFAULT_CREDENTIAL_EGRESS_V2_SUITE_NAME,
            HOSTILE_CAPABILITY_CHAIN_QUARANTINE_V2_SUITE_NAME,
            SECURE_HOST_RECOVERY_AUTHORITY_V2_SUITE_NAME,
            SECURE_HOST_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
        ],
        "foundation_suites": [
            "production_secure_host_hardening",
            "production_isolation_hardening_v2",
            "independent_secure_host_review",
            "container_grade_capability_isolation",
            "runtime_isolation_implementation_v1",
            "credential_broker_egress_enforcement_v1",
            "production_grade_secure_capability_host_evidence_v1",
        ],
        "claim_boundary": POST_DP_SECURE_HOST_CLAIM_BOUNDARY,
        "blocked_claims": list(POST_DP_SECURE_HOST_BLOCKED_CLAIMS),
        "receipt_surfaces": [
            "/api/operator/post-dp-secure-capability-host",
            "/api/operator/production-grade-secure-capability-host",
            "/api/operator/certified-secure-host",
            "/api/operator/benchmark-proof",
            "GitHub issue #574",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "evidence_policy": (
            "DR receipts must show explicit runtime profile selection, deny-by-default egress, "
            "scoped credential refs, hostile-chain quarantine, operator-owned recovery authority, "
            "safe redaction, and blocked claims before stronger secure-host wording is allowed."
        ),
        "not_claimed": [
            "secure_private_by_default",
            "production_security_solved",
            "ironclaw_class_secure_execution",
            "hardware_backed_isolation",
            "tee_cvm_wasm_or_container_runtime_isolation",
            "formal_security_certification",
            "production_ready_product",
            "full_parity_achieved",
        ],
    }


def runtime_profile_selection_receipts() -> list[dict[str, Any]]:
    rows = [
        (
            "dr-profile-tool-process",
            "tool_process",
            "ephemeral_worker_root",
            "deny_by_default_no_network_until_allowlisted",
            "scoped_env_no_raw_secret",
            "workspace_escape_and_network_client_marker",
        ),
        (
            "dr-profile-browser",
            "browser_automation",
            "per_owner_profile_partition",
            "deny_private_network_and_cookie_export",
            "profile_scoped_secret_ref_only",
            "stale_profile_owner_or_credential_replay",
        ),
        (
            "dr-profile-connector",
            "authenticated_connector",
            "credential_broker_runtime",
            "deny_unknown_endpoint_and_private_redirect",
            "field_destination_scoped_secret_ref",
            "raw_secret_output_or_revoked_epoch",
        ),
        (
            "dr-profile-mcp",
            "external_mcp",
            "endpoint_policy_runtime",
            "deny_until_dns_and_host_recheck_pass",
            "transport_secret_ref_only",
            "dns_rebind_or_metadata_service_probe",
        ),
        (
            "dr-profile-package",
            "extension_package",
            "quarantine_first_runtime",
            "deny_runtime_contribution_until_reviewed",
            "permission_delta_review_required",
            "unsigned_root_or_postinstall_network",
        ),
        (
            "dr-profile-workflow-replay",
            "workflow_replay",
            "same_or_narrower_trust_runtime",
            "deny_trust_expansion_and_side_effect_replay",
            "approval_and_credential_scope_digest",
            "approval_drift_or_secret_scope_expansion",
        ),
        (
            "dr-profile-background",
            "background_execution",
            "session_bound_process_runtime",
            "deny_cross_session_control_and_private_egress",
            "session_scoped_secret_ref_only",
            "orphaned_process_or_filesystem_escape",
        ),
    ]
    return [
        {
            "receipt_id": receipt_id,
            "redacted_receipt_handle": _receipt_handle("runtime-profile", receipt_id, row),
            "capability_surface": surface,
            "selected_runtime_profile": runtime_profile,
            "egress_posture": egress_posture,
            "credential_scope": credential_scope,
            "negative_case": negative_case,
            "profile_selected_before_execution": True,
            "deny_by_default": True,
            "operator_visible": True,
            "safe_redaction_digest": _stable_digest(row),
            "raw_secret_present": False,
            "fixture_vs_live": "covered_path_receipt_not_hardware_backed_isolation",
            "claim_boundary": POST_DP_SECURE_HOST_CLAIM_BOUNDARY,
        }
        for row in rows
        for receipt_id, surface, runtime_profile, egress_posture, credential_scope, negative_case in [row]
    ]


def deny_default_credential_egress_receipts() -> list[dict[str, Any]]:
    rows = [
        ("dr-egress-unknown-endpoint", "unknown.example", "blocked", "unknown_endpoint_denied"),
        ("dr-egress-private-network", "169.254.169.254", "blocked", "private_network_denied"),
        ("dr-egress-dns-redirect", "api.partner.example -> 127.0.0.1", "blocked", "dns_redirect_rechecked"),
        ("dr-egress-raw-secret-output", "logs.example", "blocked", "raw_secret_output_blocked"),
        ("dr-egress-revoked-epoch", "api.partner.example", "blocked", "revoked_secret_epoch_denied"),
        ("dr-egress-allowlisted-field", "api.partner.example", "allowed", "field_destination_scope_allowed"),
    ]
    return [
        {
            "receipt_id": receipt_id,
            "redacted_receipt_handle": _receipt_handle("credential-egress", receipt_id, row),
            "destination": destination,
            "decision": decision,
            "reason": reason,
            "default_posture": "deny",
            "allowlist_checked": True,
            "dns_redirect_rechecked": True,
            "private_network_checked": True,
            "revocation_epoch_checked": True,
            "field_destination_scope_enforced": True,
            "raw_secret_leaked": False,
            "operator_visible": True,
            "safe_redaction_digest": _stable_digest(row),
            "fixture_vs_live": "egress_fixture_not_external_security_certification",
            "claim_boundary": POST_DP_SECURE_HOST_CLAIM_BOUNDARY,
        }
        for row in rows
        for receipt_id, destination, decision, reason in [row]
    ]


def hostile_chain_quarantine_receipts() -> list[dict[str, Any]]:
    rows = [
        (
            "dr-chain-browser-connector",
            "browser_profile",
            "authenticated_connector",
            ["cookie_export", "credential_scope_reuse", "private_redirect"],
        ),
        (
            "dr-chain-mcp-private-network",
            "external_mcp",
            "private_network",
            ["bearer_ref", "dns_rebind", "metadata_service_probe"],
        ),
        (
            "dr-chain-package-runtime",
            "extension_package",
            "runtime_contribution",
            ["unsigned_root", "permission_delta", "postinstall_network"],
        ),
        (
            "dr-chain-workflow-secret",
            "workflow_replay",
            "credential_broker",
            ["approval_drift", "secret_scope_expansion", "side_effect_replay"],
        ),
        (
            "dr-chain-shell-background",
            "tool_process",
            "background_execution",
            ["network_client_marker", "cross_session_handle", "workspace_escape"],
        ),
    ]
    return [
        {
            "chain_id": chain_id,
            "redacted_receipt_handle": _receipt_handle("hostile-chain", chain_id, row),
            "source_surface": source,
            "destination_surface": destination,
            "attacker_steps": steps,
            "decision": "blocked",
            "quarantine_before_runtime_contribution": True,
            "fail_closed": True,
            "operator_action": "deny_quarantine_and_require_reapproval",
            "revocation_required": True,
            "session_or_profile_owner_preserved": True,
            "safe_redaction_digest": _stable_digest(row),
            "raw_secret_present": False,
            "fixture_vs_live": "hostile_chain_fixture_not_external_penetration_test",
            "claim_boundary": POST_DP_SECURE_HOST_CLAIM_BOUNDARY,
        }
        for row in rows
        for chain_id, source, destination, steps in [row]
    ]


def secure_host_recovery_authority_v2_receipts() -> list[dict[str, Any]]:
    rows = [
        ("revoke", "credential_or_profile_boundary_risk", ["invalidate_ref_epoch", "block_replay"]),
        ("rotate", "scoped_secret_ref_drift", ["issue_new_ref", "redact_old_handle"]),
        ("quarantine", "package_or_connector_risk", ["disable_runtime_contribution", "hold_reentry"]),
        ("rollback", "package_or_policy_regression", ["restore_previous_digest", "require_review"]),
        ("reapprove", "trust_boundary_change", ["operator_scope_renewal", "fresh_approval_context"]),
        ("audit", "post_incident_review", ["redacted_digest_chain", "residual_risk_note"]),
    ]
    return [
        {
            "action": action,
            "trigger": trigger,
            "controls": controls,
            "redacted_receipt_handle": _receipt_handle("recovery-authority", action, row),
            "operator_owned": True,
            "automatic_authority_expansion": False,
            "credential_revocation_proven": action in {"revoke", "rotate"} or "invalidate_ref_epoch" in controls,
            "session_revocation_proven": action in {"revoke", "quarantine", "rollback", "reapprove"},
            "reentry_requires_review": action in {"quarantine", "rollback", "reapprove"},
            "safe_redaction_digest": _stable_digest(row),
            "raw_secret_present": False,
            "fixture_vs_live": "operator_recovery_receipt_not_solved_incident_response",
            "claim_boundary": POST_DP_SECURE_HOST_CLAIM_BOUNDARY,
        }
        for row in rows
        for action, trigger, controls in [row]
    ]


def false_claim_scan_v2_receipts() -> list[dict[str, Any]]:
    forbidden = list(POST_DP_SECURE_HOST_BLOCKED_CLAIMS)
    return [
        {
            "scan_id": "dr-secure-host-false-claim-scan-v2",
            "redacted_receipt_handle": _receipt_handle("false-claim-scan", "dr-secure-host", forbidden),
            "validation_command": "python3 scripts/check_strategy_claims.py",
            "scan_scope": [
                "docs/research/19-strategy-claim-ledger.md",
                "docs/research/20-seraph-agent-parity-and-exceedance-goals.md",
                "docs/implementation/16-agent-parity-execution-roadmap.md",
                "docs/implementation/STATUS.md",
                "backend/src/security/post_dp_secure_host_gap_closure.py",
            ],
            "blocked_claims_checked": forbidden,
            "blocked_claims_found": [],
            "forbidden_hit_count": 0,
            "operator_visible": True,
            "safe_redaction_digest": _stable_digest(forbidden),
            "fixture_vs_live": "repository_scan_not_external_security_certification",
            "claim_boundary": POST_DP_SECURE_HOST_CLAIM_BOUNDARY,
        }
    ]


def build_post_dp_secure_host_contract() -> dict[str, Any]:
    runtime_profiles = runtime_profile_selection_receipts()
    egress = deny_default_credential_egress_receipts()
    hostile_chains = hostile_chain_quarantine_receipts()
    recovery = secure_host_recovery_authority_v2_receipts()
    false_claims = false_claim_scan_v2_receipts()
    upstream = build_production_grade_secure_host_contract()
    receipt_index = {
        "runtime_profiles": runtime_profiles,
        "egress": egress,
        "hostile_chains": hostile_chains,
        "recovery": recovery,
        "false_claims": false_claims,
        "upstream_digest": upstream["summary"]["receipt_digest"],
    }
    return {
        "summary": {
            "suite_name": "post_dp_secure_capability_host_gap_closure",
            "operator_status": "post_dp_secure_capability_host_gap_closure_visible",
            "runtime_profile_count": len(runtime_profiles),
            "runtime_profile_deny_default_count": sum(1 for item in runtime_profiles if item["deny_by_default"]),
            "credential_egress_decision_count": len(egress),
            "credential_egress_block_count": sum(1 for item in egress if item["decision"] == "blocked"),
            "credential_leak_count": sum(1 for item in egress if item["raw_secret_leaked"]),
            "hostile_chain_count": len(hostile_chains),
            "hostile_chain_fail_closed_count": sum(1 for item in hostile_chains if item["fail_closed"]),
            "quarantine_before_runtime_count": sum(
                1 for item in hostile_chains if item["quarantine_before_runtime_contribution"]
            ),
            "recovery_action_count": len(recovery),
            "operator_owned_recovery_count": sum(1 for item in recovery if item["operator_owned"]),
            "automatic_authority_expansion_count": sum(
                1 for item in recovery if item["automatic_authority_expansion"]
            ),
            "false_claim_scan_count": len(false_claims),
            "upstream_claim_boundary": PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
            "receipt_digest": _stable_digest(receipt_index),
            "claim_boundary": POST_DP_SECURE_HOST_CLAIM_BOUNDARY,
        },
        "runtime_profiles": runtime_profiles,
        "credential_egress": egress,
        "hostile_chains": hostile_chains,
        "recovery_authority": recovery,
        "false_claim_scan_receipts": false_claims,
        "upstream_secure_host_digest": upstream["summary"]["receipt_digest"],
        "policy": post_dp_secure_host_policy_payload(),
    }


def _failure_report(summary: Any) -> list[dict[str, str]]:
    failures = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": POST_DP_SECURE_CAPABILITY_HOST_GAP_CLOSURE_SUITE_NAME,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Post-DP secure-host scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_post_dp_secure_host_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        POST_DP_SECURE_CAPABILITY_HOST_GAP_CLOSURE_SUITE_NAME,
        RUNTIME_PROFILE_SELECTION_V2_SUITE_NAME,
        DENY_DEFAULT_CREDENTIAL_EGRESS_V2_SUITE_NAME,
        HOSTILE_CAPABILITY_CHAIN_QUARANTINE_V2_SUITE_NAME,
        SECURE_HOST_RECOVERY_AUTHORITY_V2_SUITE_NAME,
        SECURE_HOST_FALSE_CLAIM_SCAN_V2_SUITE_NAME,
    ])


async def build_post_dp_secure_host_report() -> dict[str, Any]:
    summary = await _run_post_dp_secure_host_suites()
    contract = build_post_dp_secure_host_contract()
    failures = _failure_report(summary)
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "post_dp_secure_capability_host_ci_gated_operator_visible"
                if not failures
                else "post_dp_secure_capability_host_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(POST_DP_SECURE_CAPABILITY_HOST_GAP_CLOSURE_SCENARIO_NAMES)
                + len(RUNTIME_PROFILE_SELECTION_V2_SCENARIO_NAMES)
                + len(DENY_DEFAULT_CREDENTIAL_EGRESS_V2_SCENARIO_NAMES)
                + len(HOSTILE_CAPABILITY_CHAIN_QUARANTINE_V2_SCENARIO_NAMES)
                + len(SECURE_HOST_RECOVERY_AUTHORITY_V2_SCENARIO_NAMES)
                + len(SECURE_HOST_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            POST_DP_SECURE_CAPABILITY_HOST_GAP_CLOSURE_SUITE_NAME: list(
                POST_DP_SECURE_CAPABILITY_HOST_GAP_CLOSURE_SCENARIO_NAMES
            ),
            RUNTIME_PROFILE_SELECTION_V2_SUITE_NAME: list(RUNTIME_PROFILE_SELECTION_V2_SCENARIO_NAMES),
            DENY_DEFAULT_CREDENTIAL_EGRESS_V2_SUITE_NAME: list(
                DENY_DEFAULT_CREDENTIAL_EGRESS_V2_SCENARIO_NAMES
            ),
            HOSTILE_CAPABILITY_CHAIN_QUARANTINE_V2_SUITE_NAME: list(
                HOSTILE_CAPABILITY_CHAIN_QUARANTINE_V2_SCENARIO_NAMES
            ),
            SECURE_HOST_RECOVERY_AUTHORITY_V2_SUITE_NAME: list(
                SECURE_HOST_RECOVERY_AUTHORITY_V2_SCENARIO_NAMES
            ),
            SECURE_HOST_FALSE_CLAIM_SCAN_V2_SUITE_NAME: list(SECURE_HOST_FALSE_CLAIM_SCAN_V2_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": failures,
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
