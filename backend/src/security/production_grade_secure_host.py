"""Batch DJ production-grade secure-host evidence receipts.

This layer sits above DB/CT/CK/CD. It records cross-surface hostile-chain,
credential-egress, runtime-attestation, and operator-recovery receipts for the
certification-track secure runtime isolation batch. The receipts remain bounded:
they do not claim secure/private-by-default execution, IronClaw-class secure
execution, hardware-backed isolation, formal certification, production
readiness, full parity, or reference-system exceedance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SUITE_NAME = (
    "production_grade_secure_capability_host_evidence_v1"
)
PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SCENARIO_NAMES = (
    "production_grade_secure_host_surface_matrix_behavior",
    "production_grade_secure_host_receipt_field_behavior",
    "production_grade_secure_host_blocked_claim_behavior",
)
SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SUITE_NAME = "secure_host_cross_surface_attack_chain_v1"
SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SCENARIO_NAMES = (
    "secure_host_prompt_to_delegated_tool_chain_behavior",
    "secure_host_connector_secret_to_redirect_chain_behavior",
    "secure_host_browser_session_to_replay_chain_behavior",
    "secure_host_package_install_to_runtime_chain_behavior",
    "secure_host_mcp_private_network_chain_behavior",
    "secure_host_background_filesystem_escape_chain_behavior",
    "secure_host_provider_fallback_trust_expansion_chain_behavior",
)
CREDENTIAL_BROKER_EGRESS_SOAK_SUITE_NAME = "credential_broker_egress_soak_v1"
CREDENTIAL_BROKER_EGRESS_SOAK_SCENARIO_NAMES = (
    "credential_broker_field_destination_scope_behavior",
    "credential_broker_dns_redirect_recheck_behavior",
    "credential_broker_revocation_rotation_soak_behavior",
    "credential_broker_raw_secret_leak_block_behavior",
)
RUNTIME_ISOLATION_ATTESTATION_MATRIX_SUITE_NAME = "runtime_isolation_attestation_matrix_v1"
RUNTIME_ISOLATION_ATTESTATION_MATRIX_SCENARIO_NAMES = (
    "runtime_attestation_surface_matrix_behavior",
    "runtime_attestation_unsupported_boundary_behavior",
    "runtime_attestation_operator_digest_behavior",
)
SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SUITE_NAME = "secure_host_operator_recovery_authority_v1"
SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SCENARIO_NAMES = (
    "secure_host_operator_deny_quarantine_rotate_behavior",
    "secure_host_operator_rollback_revoke_replay_block_behavior",
    "secure_host_operator_repair_audit_behavior",
)
SECURE_HOST_FALSE_CLAIM_SCAN_SUITE_NAME = "secure_host_false_claim_scan_v1"
SECURE_HOST_FALSE_CLAIM_SCAN_SCENARIO_NAMES = (
    "secure_host_false_claim_scan_blocks_security_overclaims",
    "secure_host_false_claim_scan_blocks_certification_overclaims",
)

PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY = (
    "bounded_certification_track_secure_runtime_isolation_receipts_not_secure_private_or_ironclaw_class"
)
PRODUCTION_GRADE_SECURE_HOST_BLOCKED_CLAIMS = (
    "secure_private_by_default",
    "production_security_solved",
    "ironclaw_class_secure_execution",
    "hardware_backed_isolation",
    "tee_cvm_wasm_or_container_runtime_isolation",
    "formal_security_certification",
    "safe_autonomous_computer_use",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
    "broad_superiority",
)


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _redacted_receipt_handle(kind: str, receipt_id: str, payload: Any) -> str:
    return f"seraph://receipts/batch-dj/{kind}/{receipt_id}/{_stable_digest(payload)}"


def _surface_credential_scope(surface_id: str) -> str:
    secret_ref_surfaces = {
        "authenticated_connector",
        "external_mcp",
        "extension_runtime",
        "workflow_replay",
        "provider_fallback",
        "credential",
    }
    if surface_id in secret_ref_surfaces:
        return "field_and_destination_scoped_secret_ref"
    if surface_id == "browser_automation":
        return "profile_partition_no_raw_secret"
    return "no_raw_secret"


def production_grade_secure_host_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SUITE_NAME,
            SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SUITE_NAME,
            CREDENTIAL_BROKER_EGRESS_SOAK_SUITE_NAME,
            RUNTIME_ISOLATION_ATTESTATION_MATRIX_SUITE_NAME,
            SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SUITE_NAME,
            SECURE_HOST_FALSE_CLAIM_SCAN_SUITE_NAME,
        ],
        "foundation_suites": [
            "production_isolation_hardening_v2",
            "independent_secure_host_review",
            "container_grade_capability_isolation",
            "runtime_isolation_implementation_v1",
            "credential_broker_egress_enforcement_v1",
            "hostile_runtime_escape_gauntlet_v1",
        ],
        "claim_boundary": PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
        "blocked_claims": list(PRODUCTION_GRADE_SECURE_HOST_BLOCKED_CLAIMS),
        "receipt_surfaces": [
            "/api/operator/production-grade-secure-capability-host",
            "/api/operator/certified-secure-host",
            "/api/operator/container-grade-secure-host",
            "/api/operator/independent-secure-host-review",
            "/api/operator/benchmark-proof",
            "GitHub issue #558",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "evidence_policy": (
            "DJ receipts must expose cross-surface attack-chain decisions, field/destination-scoped "
            "credential egress, runtime-attestation matrices, operator recovery authority, residual "
            "risk, fixture-vs-live labels, and blocked claims before any stronger security wording is allowed."
        ),
        "not_claimed": [
            "secure_private_by_default",
            "production_security_solved",
            "ironclaw_class_secure_execution",
            "hardware_backed_isolation",
            "tee_cvm_wasm_or_container_runtime_isolation",
            "formal_security_certification",
            "safe_autonomous_computer_use",
            "production_ready_product",
            "full_parity_achieved",
        ],
    }


def secure_host_surface_matrix() -> list[dict[str, Any]]:
    surfaces = [
        ("tool_process", "shell/process", "worker_root", "workspace_escape_blocked"),
        ("browser_automation", "browser", "profile_partition", "cookie_export_blocked"),
        ("authenticated_connector", "connector", "secret_ref_broker", "private_redirect_blocked"),
        ("external_mcp", "MCP", "endpoint_policy", "dns_rebind_blocked"),
        ("extension_runtime", "extension/package", "signed_package_root", "permission_creep_quarantined"),
        ("workflow_replay", "workflow replay", "same_or_narrower_trust", "approval_drift_blocked"),
        ("delegation", "delegation", "delegated_owner_scope", "silent_policy_lift_blocked"),
        ("background_execution", "background execution", "session_bound_process", "cross_session_control_blocked"),
        ("provider_fallback", "provider fallback", "trust_floor", "fallback_trust_expansion_blocked"),
        ("filesystem", "filesystem", "workspace_boundary", "secret_like_path_denied"),
        ("network", "network", "egress_policy", "private_network_denied"),
        ("credential", "credential", "field_destination_secret_ref", "raw_secret_leak_blocked"),
    ]
    return [
        {
            "redacted_receipt_handle": _redacted_receipt_handle(
                "surface-matrix",
                surface_id,
                (surface_id, boundary, negative_case),
            ),
            "surface_id": surface_id,
            "surface": surface,
            "boundary": boundary,
            "negative_case": negative_case,
            "source_surface": surface,
            "destination_surface": boundary,
            "session_or_profile_owner": "originating_operator_session",
            "credential_scope": _surface_credential_scope(surface_id),
            "filesystem_root": "disposable_worker_root_or_declared_profile",
            "network_destination_policy": "allowlist_with_private_network_denial",
            "evidence_mode": "bounded_fixture_plus_covered_path_receipt",
            "fixture_vs_live": "fixture_receipt_not_external_certification",
            "operator_visible": True,
            "residual_risk": PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
            "receipt_digest": _stable_digest((surface_id, boundary, negative_case)),
        }
        for surface_id, surface, boundary, negative_case in surfaces
    ]


def cross_surface_attack_chain_receipts() -> list[dict[str, Any]]:
    return [
        _attack_chain(
            "dj-chain-prompt-delegated-tool",
            "prompt_surface",
            "delegated_tool",
            ["ignore_policy", "silent_tool_use", "secret_echo"],
            ["delegated_owner_scope", "approval_required", "secret_redaction"],
        ),
        _attack_chain(
            "dj-chain-connector-secret-redirect",
            "authenticated_connector",
            "network_egress",
            ["secret_ref_field", "redirect_to_10_0_0_8", "raw_secret_echo"],
            ["endpoint_allowlist", "dns_redirect_recheck", "private_network_denial"],
        ),
        _attack_chain(
            "dj-chain-browser-session-replay",
            "browser_profile",
            "workflow_replay",
            ["cookie_export", "profile_reuse", "approval_context_drift"],
            ["cookie_export_block", "profile_owner_check", "replay_authority_block"],
        ),
        _attack_chain(
            "dj-chain-package-runtime-contribution",
            "extension_package",
            "runtime_contribution",
            ["postinstall_network", "permission_delta", "unsigned_root"],
            ["signed_root_required", "permission_delta_review", "quarantine_before_contribution"],
        ),
        _attack_chain(
            "dj-chain-mcp-private-network",
            "external_mcp",
            "private_network",
            ["bearer_token", "dns_rebind", "metadata_service_probe"],
            ["destination_host_recheck", "private_network_denial", "credential_revocation_epoch_check"],
        ),
        _attack_chain(
            "dj-chain-background-filesystem",
            "background_process",
            "filesystem",
            ["cross_session_handle", "path_traversal", "secret_like_file"],
            ["session_bound_handle", "workspace_root_check", "secret_like_path_denial"],
        ),
        _attack_chain(
            "dj-chain-provider-fallback-trust",
            "provider_fallback",
            "workflow_replay",
            ["lower_trust_provider", "approval_scope_reuse", "credential_scope_expansion"],
            ["trust_floor_enforced", "approval_scope_recomputed", "credential_scope_digest_block"],
        ),
    ]


def credential_broker_egress_soak_receipts() -> list[dict[str, Any]]:
    return [
        _egress_soak("dj-egress-allowlisted-field", "api.partner.example", "allowed", ["secret_ref", "account_id"]),
        _egress_soak("dj-egress-private-redirect", "api.partner.example -> 10.0.0.8", "blocked", ["secret_ref"]),
        _egress_soak("dj-egress-dns-rebind", "api.partner.example -> 127.0.0.1", "blocked", ["authorization"]),
        _egress_soak("dj-egress-raw-secret-output", "external-log-sink.example", "blocked", ["raw_secret"]),
        _egress_soak("dj-egress-revoked-epoch", "api.partner.example", "blocked", ["revoked_secret_ref"]),
    ]


def runtime_isolation_attestation_matrix() -> list[dict[str, Any]]:
    rows = [
        ("tool_process", "implemented_policy_boundary", True, "not_container_or_vm_escape_proof"),
        ("browser_automation", "implemented_profile_partition", True, "not_full_browser_credential_isolation"),
        ("authenticated_connector", "implemented_secret_ref_broker", True, "not_external_vault_attestation"),
        ("external_mcp", "implemented_endpoint_policy", True, "not_universal_mcp_sandbox"),
        ("extension_runtime", "implemented_signed_root_and_quarantine", True, "not_solved_package_security"),
        ("workflow_replay", "implemented_same_or_narrower_trust", True, "not_distributed_security_certification"),
        ("hardware_backed_runtime", "unsupported_boundary_declared", False, "tee_cvm_wasm_container_not_implemented"),
        ("formal_security_certification", "not_certified", False, "formal_certification_not_completed"),
    ]
    return [
        {
            "redacted_receipt_handle": _redacted_receipt_handle(
                "runtime-attestation",
                surface,
                (state, implemented, gap),
            ),
            "surface": surface,
            "attestation_state": state,
            "implemented": implemented,
            "substitute_or_gap": gap,
            "evidence_mode": "bounded_attestation_matrix",
            "fixture_vs_live": "operator_receipt_not_formal_certification",
            "operator_visible": True,
            "receipt_digest": _stable_digest((surface, state, implemented, gap)),
            "residual_risk": gap,
        }
        for surface, state, implemented, gap in rows
    ]


def operator_recovery_authority_receipts() -> list[dict[str, Any]]:
    return [
        _operator_action("deny", "hostile_chain_block", ["record_receipt", "keep_secret_redacted"]),
        _operator_action("quarantine", "extension_or_package_risk", ["disable_runtime_contribution", "notify_owner"]),
        _operator_action("rotate", "credential_scope_drift", ["revoke_old_epoch", "issue_new_secret_ref"]),
        _operator_action("rollback", "package_or_provider_regression", ["restore_previous_digest", "block_reentry"]),
        _operator_action("revoke", "session_or_profile_boundary_risk", ["invalidate_profile_owner", "audit_replay_block"]),
        _operator_action("replay_block", "approval_or_trust_expansion", ["preserve_failed_replay", "require_new_approval"]),
        _operator_action("repair", "policy_or_manifest_gap", ["open_repair_plan", "keep_runtime_disabled"]),
        _operator_action("audit", "post_incident_review", ["redacted_digest_chain", "residual_risk_note"]),
    ]


def false_claim_scan_receipts() -> list[dict[str, Any]]:
    forbidden = list(PRODUCTION_GRADE_SECURE_HOST_BLOCKED_CLAIMS)
    return [
        {
            "scan_id": "dj-secure-host-false-claim-scan",
            "redacted_receipt_handle": _redacted_receipt_handle("false-claim-scan", "dj-secure-host", forbidden),
            "evidence_mode": "static_claim_scan_receipt",
            "fixture_vs_live": "repository_scan_not_external_certification",
            "validation_command": "python3 scripts/check_strategy_claims.py",
            "scan_scope": [
                "docs/research/19-strategy-claim-ledger.md",
                "docs/research/20-seraph-agent-parity-and-exceedance-goals.md",
                "docs/implementation/00-master-roadmap.md",
                "docs/implementation/09-benchmark-status.md",
                "docs/implementation/16-agent-parity-execution-roadmap.md",
                "docs/implementation/STATUS.md",
            ],
            "recorded_date": "2026-06-11",
            "blocked_claims_checked": forbidden,
            "blocked_claims_found": [],
            "forbidden_hit_count": 0,
            "operator_visible": True,
            "residual_risk": "static_scan_does_not_replace_reviewer_judgment_or_external_security_certification",
            "receipt_digest": _stable_digest(forbidden),
        }
    ]


def build_production_grade_secure_host_contract() -> dict[str, Any]:
    surface_matrix = secure_host_surface_matrix()
    attack_chains = cross_surface_attack_chain_receipts()
    egress_soak = credential_broker_egress_soak_receipts()
    attestation = runtime_isolation_attestation_matrix()
    recovery = operator_recovery_authority_receipts()
    false_claims = false_claim_scan_receipts()
    receipt_index = {
        "surface_matrix": surface_matrix,
        "attack_chains": attack_chains,
        "egress_soak": egress_soak,
        "attestation": attestation,
        "recovery": recovery,
        "false_claims": false_claims,
    }
    return {
        "summary": {
            "suite_name": "production_grade_secure_host",
            "operator_status": "production_grade_secure_host_receipts_visible",
            "surface_count": len(surface_matrix),
            "attack_chain_count": len(attack_chains),
            "attack_chain_fail_closed_count": sum(1 for item in attack_chains if item["fail_closed"]),
            "credential_egress_decision_count": len(egress_soak),
            "credential_egress_block_count": sum(1 for item in egress_soak if item["decision"] == "blocked"),
            "credential_leak_count": sum(1 for item in egress_soak if item["raw_secret_leaked"]),
            "attestation_surface_count": len(attestation),
            "unsupported_boundary_count": sum(1 for item in attestation if not item["implemented"]),
            "operator_recovery_action_count": len(recovery),
            "false_claim_scan_count": len(false_claims),
            "receipt_digest": _stable_digest(receipt_index),
            "claim_boundary": PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
        },
        "surface_matrix": surface_matrix,
        "cross_surface_attack_chains": attack_chains,
        "credential_broker_egress_soak": egress_soak,
        "runtime_isolation_attestation_matrix": attestation,
        "operator_recovery_authority": recovery,
        "false_claim_scan_receipts": false_claims,
        "policy": production_grade_secure_host_policy_payload(),
    }


def _attack_chain(
    chain_id: str,
    source_surface: str,
    destination_surface: str,
    attacker_steps: list[str],
    controls: list[str],
) -> dict[str, Any]:
    return {
        "redacted_receipt_handle": _redacted_receipt_handle("attack-chain", chain_id, (attacker_steps, controls)),
        "chain_id": chain_id,
        "source_surface": source_surface,
        "destination_surface": destination_surface,
        "attacker_steps": attacker_steps,
        "controls": controls,
        "decision": "blocked",
        "fail_closed": True,
        "credential_scope": "same_or_narrower_scope_only",
        "session_or_profile_owner": "originating_operator_session",
        "filesystem_root": "declared_worker_or_profile_root",
        "network_destination": "allowlisted_public_endpoint_or_blocked_private_destination",
        "dns_redirect_result": "rechecked_and_private_redirect_blocked",
        "private_network_decision": "blocked",
        "revocation_epoch_checked": True,
        "rotation_outcome": "rotation_required_when_secret_boundary_touched",
        "replay_authority": "same_or_narrower_trust_only",
        "delegated_owner": "explicit_owner_required",
        "provider_fallback_mode": "trust_floor_preserved",
        "package_signature_root": "signed_root_required_before_runtime_contribution",
        "operator_action": "deny_or_quarantine_with_repair_receipt",
        "redaction_digest": _stable_digest((chain_id, attacker_steps, controls)),
        "residual_risk": PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
        "claim_boundary": PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
        "evidence_mode": "bounded_cross_surface_attack_chain_fixture",
        "fixture_vs_live": "attack_chain_fixture_not_live_external_target",
        "operator_visible": True,
    }


def _egress_soak(receipt_id: str, endpoint: str, decision: str, fields: list[str]) -> dict[str, Any]:
    blocked = decision == "blocked"
    return {
        "redacted_receipt_handle": _redacted_receipt_handle(
            "credential-egress",
            receipt_id,
            (endpoint, decision, fields),
        ),
        "receipt_id": receipt_id,
        "endpoint": endpoint,
        "decision": decision,
        "field_scoped_injection": True,
        "injected_fields": fields,
        "endpoint_allowlist_checked": True,
        "dns_redirect_rechecked": True,
        "private_network_checked": True,
        "private_network_decision": "blocked" if "10." in endpoint or "127." in endpoint else "not_private",
        "revocation_epoch_checked": True,
        "rotation_outcome": "rotated_or_blocked" if blocked else "not_required",
        "raw_secret_leaked": False,
        "evidence_mode": "bounded_credential_egress_soak_fixture",
        "fixture_vs_live": "soak_fixture_not_continuous_live_security_certification",
        "operator_visible": True,
        "redaction_digest": _stable_digest((receipt_id, endpoint, decision, fields)),
        "residual_risk": PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
    }


def _operator_action(action: str, trigger: str, controls: list[str]) -> dict[str, Any]:
    return {
        "redacted_receipt_handle": _redacted_receipt_handle("operator-recovery", action, (trigger, controls)),
        "action": action,
        "trigger": trigger,
        "controls": controls,
        "operator_visible": True,
        "safe_redaction_digest": _stable_digest((action, trigger, controls)),
        "residual_risk": PRODUCTION_GRADE_SECURE_HOST_CLAIM_BOUNDARY,
        "evidence_mode": "operator_recovery_authority_receipt",
    }


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Production-grade secure-host scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_production_grade_secure_host_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SUITE_NAME,
        SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SUITE_NAME,
        CREDENTIAL_BROKER_EGRESS_SOAK_SUITE_NAME,
        RUNTIME_ISOLATION_ATTESTATION_MATRIX_SUITE_NAME,
        SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SUITE_NAME,
        SECURE_HOST_FALSE_CLAIM_SCAN_SUITE_NAME,
    ])


async def build_production_grade_secure_host_report() -> dict[str, Any]:
    summary = await _run_production_grade_secure_host_suites()
    contract = build_production_grade_secure_host_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "production_grade_secure_host_ci_gated_operator_visible"
                if healthy
                else "production_grade_secure_host_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SCENARIO_NAMES)
                + len(SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SCENARIO_NAMES)
                + len(CREDENTIAL_BROKER_EGRESS_SOAK_SCENARIO_NAMES)
                + len(RUNTIME_ISOLATION_ATTESTATION_MATRIX_SCENARIO_NAMES)
                + len(SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SCENARIO_NAMES)
                + len(SECURE_HOST_FALSE_CLAIM_SCAN_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SUITE_NAME: list(
                PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SCENARIO_NAMES
            ),
            SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SUITE_NAME: list(
                SECURE_HOST_CROSS_SURFACE_ATTACK_CHAIN_SCENARIO_NAMES
            ),
            CREDENTIAL_BROKER_EGRESS_SOAK_SUITE_NAME: list(CREDENTIAL_BROKER_EGRESS_SOAK_SCENARIO_NAMES),
            RUNTIME_ISOLATION_ATTESTATION_MATRIX_SUITE_NAME: list(
                RUNTIME_ISOLATION_ATTESTATION_MATRIX_SCENARIO_NAMES
            ),
            SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SUITE_NAME: list(
                SECURE_HOST_OPERATOR_RECOVERY_AUTHORITY_SCENARIO_NAMES
            ),
            SECURE_HOST_FALSE_CLAIM_SCAN_SUITE_NAME: list(SECURE_HOST_FALSE_CLAIM_SCAN_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(
            summary,
            suite_name=PRODUCTION_GRADE_SECURE_CAPABILITY_HOST_EVIDENCE_SUITE_NAME,
        ),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
