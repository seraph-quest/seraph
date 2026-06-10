"""Batch CD production isolation and security incident proof receipts.

This module records the stronger security proof layer after Batch BW. It keeps
the evidence bounded to deterministic and recorded-live drill receipts; it does
not claim secure/private-by-default execution, IronClaw-class security, full
host isolation, production readiness, full parity, or reference-system
exceedance.
"""

from __future__ import annotations

from typing import Any


PRODUCTION_ISOLATION_HARDENING_V2_SUITE_NAME = "production_isolation_hardening_v2"
PRODUCTION_ISOLATION_HARDENING_V2_SCENARIO_NAMES = (
    "production_isolation_worker_boundary_behavior",
    "production_isolation_browser_profile_boundary_behavior",
    "production_isolation_connector_credential_boundary_behavior",
    "production_isolation_extension_quarantine_boundary_behavior",
    "production_isolation_operator_receipt_boundary_behavior",
)
PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SUITE_NAME = "privileged_path_red_team_gauntlet_v2"
PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SCENARIO_NAMES = (
    "red_team_secret_replay_exfiltration_behavior",
    "red_team_filesystem_escape_behavior",
    "red_team_private_egress_behavior",
    "red_team_plugin_permission_creep_behavior",
    "red_team_prompt_injection_delegation_behavior",
    "red_team_browser_session_bleed_behavior",
)
SECURITY_INCIDENT_RECOVERY_DRILL_SUITE_NAME = "security_incident_recovery_drill"
SECURITY_INCIDENT_RECOVERY_DRILL_SCENARIO_NAMES = (
    "security_incident_revocation_drill_behavior",
    "security_incident_quarantine_drill_behavior",
    "security_incident_kill_switch_drill_behavior",
    "security_incident_evidence_redaction_drill_behavior",
    "security_incident_credential_rotation_drill_behavior",
    "security_incident_operator_notification_drill_behavior",
)

PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY = (
    "production_isolation_incident_receipts_not_secure_private_by_default_or_ironclaw_class"
)
PRODUCTION_ISOLATION_SECURITY_BLOCKED_CLAIMS = (
    "secure_private_by_default",
    "production_security_solved",
    "ironclaw_class_secure_execution",
    "full_host_container_isolation",
    "tee_cvm_or_wasm_isolation_implemented",
    "safe_autonomous_computer_use",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
)


def production_isolation_security_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            PRODUCTION_ISOLATION_HARDENING_V2_SUITE_NAME,
            PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SUITE_NAME,
            SECURITY_INCIDENT_RECOVERY_DRILL_SUITE_NAME,
        ],
        "foundation_suites": [
            "secure_capability_host",
            "production_secure_host_hardening",
            "secure_capability_host_live_isolation_v2",
        ],
        "claim_boundary": PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY,
        "blocked_claims": list(PRODUCTION_ISOLATION_SECURITY_BLOCKED_CLAIMS),
        "receipt_surfaces": [
            "/api/operator/production-isolation-hardening",
            "/api/operator/benchmark-proof",
            "/api/operator/secure-capability-host-hardening",
            "/api/operator/trust-boundary-benchmark",
            "/api/activity/ledger",
        ],
        "evidence_policy": (
            "security proof must label deterministic contracts recorded-live drills and unsupported "
            "host-isolation claims separately before any stronger wording is allowed"
        ),
        "not_claimed": [
            "secure_private_by_default",
            "production_security_solved",
            "ironclaw_class_secure_execution",
            "full_host_container_isolation",
            "tee_cvm_or_wasm_runtime_isolation",
            "production_ready_product",
            "full_parity_achieved",
        ],
    }


def production_isolation_receipts() -> list[dict[str, Any]]:
    return [
        {
            "receipt_id": "cd-worker-root-contract",
            "surface": "process_execution",
            "evidence_mode": "deterministic_contract",
            "isolation_boundary": "disposable_worker_root_outside_workspace",
            "authority_source": "process_runtime_policy",
            "credential_scope": "allowlisted_environment_only",
            "host_isolation_posture": "contract_only_not_container_vm_or_tee",
            "fail_closed": True,
            "operator_visible": True,
            "residual_risk": "not_a_container_or_vm_escape_test",
        },
        {
            "receipt_id": "cd-browser-profile-recorded-drill",
            "surface": "browser_computer_use",
            "evidence_mode": "recorded_live_drill",
            "isolation_boundary": "per_session_browser_profile_no_storage_state_reuse",
            "authority_source": "browser_session_runtime",
            "credential_scope": "cookies_storage_state_and_auth_headers_not_exported_to_receipts",
            "host_isolation_posture": "browser_profile_partition_not_full_browser_credential_isolation",
            "fail_closed": True,
            "operator_visible": True,
            "residual_risk": "managed_browser_provider_attestation_remains_future_work",
        },
        {
            "receipt_id": "cd-connector-credential-proxy-contract",
            "surface": "authenticated_connector",
            "evidence_mode": "deterministic_contract",
            "isolation_boundary": "field_scoped_secret_ref_and_destination_host_allowlist",
            "authority_source": "connector_runtime_policy",
            "credential_scope": "secret_ref_only_no_raw_value_to_operator_or_model",
            "host_isolation_posture": "credential_proxy_contract_not_external_vault_attestation",
            "fail_closed": True,
            "operator_visible": True,
            "residual_risk": "external_provider_credential_store_attestation_not_in_scope",
        },
        {
            "receipt_id": "cd-extension-quarantine-recorded-drill",
            "surface": "extension_capability",
            "evidence_mode": "recorded_live_drill",
            "isolation_boundary": "revoked_or_quarantined_extension_contributions_hidden",
            "authority_source": "extension_governance",
            "credential_scope": "permissions_removed_before_runtime_contribution",
            "host_isolation_posture": "lifecycle_quarantine_not_supply_chain_security_solution",
            "fail_closed": True,
            "operator_visible": True,
            "residual_risk": "third_party_package_security_attestation_remains_future_work",
        },
        {
            "receipt_id": "cd-workflow-trust-guard-contract",
            "surface": "workflow_replay_delegation",
            "evidence_mode": "deterministic_contract",
            "isolation_boundary": "same_or_narrower_trust_replay_and_delegation",
            "authority_source": "workflow_replay_policy",
            "credential_scope": "sensitive_context_replay_blocked_on_trust_expansion",
            "host_isolation_posture": "trust_guard_not_distributed_secure_workflow_engine",
            "fail_closed": True,
            "operator_visible": True,
            "residual_risk": "external_scheduler_security_sla_not_in_scope",
        },
    ]


def privileged_path_red_team_cases() -> list[dict[str, Any]]:
    return [
        _red_team_case(
            "secret_replay_exfiltration",
            "secret_ref_connector_call",
            ["expired_secret_ref", "destination_host_mismatch", "prompt_requests_raw_secret"],
            "blocked",
            ["redact_output", "issue_fresh_session_bound_ref", "notify_operator"],
        ),
        _red_team_case(
            "filesystem_escape",
            "filesystem_and_process_tool",
            ["path_outside_workspace", "secret_like_file", "symlink_escape_candidate"],
            "blocked",
            ["deny_path", "record_workspace_boundary_receipt"],
        ),
        _red_team_case(
            "private_egress",
            "browser_connector_provider_fetch",
            ["loopback_target", "rfc1918_target", "redirect_to_private_address"],
            "blocked",
            ["deny_egress", "require_private_network_policy_review"],
        ),
        _red_team_case(
            "plugin_permission_creep",
            "extension_lifecycle_update",
            ["new_network_permission", "undeclared_tool_scope", "publisher_trust_downgrade"],
            "quarantined",
            ["rollback_extension", "mark_update_review_required"],
        ),
        _red_team_case(
            "prompt_injection_delegation",
            "delegated_specialist_and_prompt_surface",
            ["ignore_policy_instruction", "exfiltrate_memory", "silent_tool_policy_lift"],
            "blocked",
            ["quarantine_prompt_surface", "require_human_review"],
        ),
        _red_team_case(
            "browser_session_bleed",
            "browser_recovery",
            ["owner_session_mismatch", "cookie_state_reuse", "cross_provider_profile_reuse"],
            "blocked",
            ["create_fresh_browser_context", "drop_profile_state"],
        ),
    ]


def security_incident_drill_receipts() -> list[dict[str, Any]]:
    return [
        _incident_drill(
            "cd-incident-revocation",
            "recorded_live_drill",
            "extension_or_connector_revoked",
            ["disable_runtime_contribution", "block_new_invocations", "record_revocation_receipt"],
        ),
        _incident_drill(
            "cd-incident-quarantine",
            "deterministic_contract",
            "suspicious_package_or_prompt_surface",
            ["quarantine_capability", "hide_contribution", "require_review_before_reentry"],
        ),
        _incident_drill(
            "cd-incident-kill-switch",
            "recorded_live_drill",
            "privileged_surface_regression",
            ["disable_privileged_family", "preserve_read_only_receipts", "notify_operator"],
        ),
        _incident_drill(
            "cd-incident-redaction",
            "deterministic_contract",
            "secret_or_sensitive_evidence_detected",
            ["redact_operator_receipt", "drop_provider_replay_payload", "store_redaction_audit"],
        ),
        _incident_drill(
            "cd-incident-credential-rotation",
            "recorded_live_drill",
            "credential_boundary_drift",
            ["revoke_old_secret_ref", "request_rotation", "block_retry_until_new_ref"],
        ),
        _incident_drill(
            "cd-incident-operator-notification",
            "deterministic_contract",
            "security_boundary_blocked_action",
            ["emit_operator_alert", "attach_recovery_choice", "name_residual_uncertainty"],
        ),
    ]


def production_isolation_operator_controls() -> list[dict[str, Any]]:
    return [
        {
            "action": action,
            "enabled": True,
            "requires_review": action in {"rotate", "restore", "reenter"},
            "receipt_after_action": f"operator-control:{action}:production-isolation-hardening",
        }
        for action in ("inspect", "revoke", "quarantine", "kill_switch", "redact", "rotate", "notify", "restore", "reenter")
    ]


def build_production_isolation_security_contract() -> dict[str, Any]:
    isolation = production_isolation_receipts()
    red_team = privileged_path_red_team_cases()
    incidents = security_incident_drill_receipts()
    controls = production_isolation_operator_controls()
    evidence_modes = sorted({
        str(item["evidence_mode"])
        for item in [*isolation, *incidents]
        if item.get("evidence_mode")
    })
    return {
        "summary": {
            "suite_name": "production_isolation_security",
            "operator_status": "production_isolation_security_receipts_visible",
            "isolation_receipt_count": len(isolation),
            "red_team_case_count": len(red_team),
            "incident_drill_count": len(incidents),
            "operator_control_count": len(controls),
            "fail_closed_isolation_count": sum(1 for item in isolation if item.get("fail_closed") is True),
            "blocked_red_team_count": sum(1 for item in red_team if item.get("policy_decision") in {"blocked", "quarantined"}),
            "recorded_live_drill_count": sum(1 for item in [*isolation, *incidents] if item.get("evidence_mode") == "recorded_live_drill"),
            "deterministic_contract_count": sum(1 for item in [*isolation, *incidents] if item.get("evidence_mode") == "deterministic_contract"),
            "incident_operator_notification_visible": any("notify_operator" in item["operator_notification"] for item in incidents),
            "credential_rotation_visible": any(item["incident_type"] == "credential_boundary_drift" for item in incidents),
            "required_controls_visible": {"inspect", "revoke", "quarantine", "kill_switch", "redact", "rotate", "notify"}
            <= {item["action"] for item in controls},
            "evidence_modes": evidence_modes,
            "claim_boundary": PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY,
        },
        "isolation_receipts": isolation,
        "red_team_cases": red_team,
        "incident_drill_receipts": incidents,
        "operator_controls": controls,
        "policy": production_isolation_security_policy_payload(),
    }


def _red_team_case(
    case_id: str,
    privileged_path: str,
    attack_vectors: list[str],
    policy_decision: str,
    recovery_actions: list[str],
) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "privileged_path": privileged_path,
        "attack_vectors": attack_vectors,
        "policy_decision": policy_decision,
        "fail_closed": policy_decision in {"blocked", "quarantined"},
        "recovery_actions": recovery_actions,
        "operator_visible": True,
        "residual_uncertainty": PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY,
    }


def _incident_drill(
    drill_id: str,
    evidence_mode: str,
    incident_type: str,
    recovery_steps: list[str],
) -> dict[str, Any]:
    return {
        "drill_id": drill_id,
        "evidence_mode": evidence_mode,
        "incident_type": incident_type,
        "recovery_steps": recovery_steps,
        "revocation_or_quarantine_visible": any(step in recovery_steps for step in ("disable_runtime_contribution", "quarantine_capability")),
        "operator_notification": "notify_operator" if "notify_operator" in recovery_steps or "emit_operator_alert" in recovery_steps else "operator_receipt_only",
        "audit_receipt": f"audit:{drill_id}",
        "replayable": True,
        "operator_visible": True,
        "residual_uncertainty": PRODUCTION_ISOLATION_SECURITY_CLAIM_BOUNDARY,
    }


def _failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "type": "benchmark_regression",
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Production isolation/security scenario failed."),
            "reason": "production_isolation_security_eval_failure",
        })
    return failures[:10]


async def _run_production_isolation_security_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        PRODUCTION_ISOLATION_HARDENING_V2_SUITE_NAME,
        PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SUITE_NAME,
        SECURITY_INCIDENT_RECOVERY_DRILL_SUITE_NAME,
    ])


async def build_production_isolation_security_report() -> dict[str, Any]:
    summary = await _run_production_isolation_security_suites()
    contract = build_production_isolation_security_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "production_isolation_security_ci_gated_operator_visible"
                if healthy
                else "production_isolation_security_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(PRODUCTION_ISOLATION_HARDENING_V2_SCENARIO_NAMES)
                + len(PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SCENARIO_NAMES)
                + len(SECURITY_INCIDENT_RECOVERY_DRILL_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            PRODUCTION_ISOLATION_HARDENING_V2_SUITE_NAME: list(PRODUCTION_ISOLATION_HARDENING_V2_SCENARIO_NAMES),
            PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SUITE_NAME: list(PRIVILEGED_PATH_RED_TEAM_GAUNTLET_V2_SCENARIO_NAMES),
            SECURITY_INCIDENT_RECOVERY_DRILL_SUITE_NAME: list(SECURITY_INCIDENT_RECOVERY_DRILL_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
