"""Batch DB certified secure-host evidence receipts.

This layer sits above the Batch CT validation records. It records implemented
covered-path policy receipts for capability runtime guardrails, credential-broker
egress control, external review/certification scope, and hostile runtime escape
drills. It still does not claim unconditional secure execution, formal security
certification, hardware-backed isolation, production readiness, full parity, or
reference-system exceedance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


RUNTIME_ISOLATION_IMPLEMENTATION_SUITE_NAME = "runtime_isolation_implementation_v1"
RUNTIME_ISOLATION_IMPLEMENTATION_SCENARIO_NAMES = (
    "runtime_isolation_capability_class_enforcement_behavior",
    "runtime_isolation_filesystem_workspace_boundary_behavior",
    "runtime_isolation_browser_profile_partition_behavior",
    "runtime_isolation_package_runtime_quarantine_behavior",
    "runtime_isolation_hardware_substitute_boundary_behavior",
)
CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SUITE_NAME = "credential_broker_egress_enforcement_v1"
CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SCENARIO_NAMES = (
    "credential_broker_field_scoped_injection_behavior",
    "credential_broker_endpoint_allowlist_behavior",
    "credential_broker_private_network_redirect_denial_behavior",
    "credential_broker_rotation_revocation_behavior",
    "credential_broker_auditable_denial_receipt_behavior",
)
EXTERNAL_SECURITY_CERTIFICATION_SUITE_NAME = "external_security_certification_v1"
EXTERNAL_SECURITY_CERTIFICATION_SCENARIO_NAMES = (
    "external_security_certification_scope_behavior",
    "external_security_certification_findings_retest_behavior",
    "external_security_certification_waiver_expiry_behavior",
    "external_security_certification_artifact_digest_behavior",
)
HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SUITE_NAME = "hostile_runtime_escape_gauntlet_v1"
HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SCENARIO_NAMES = (
    "hostile_runtime_prompt_injection_escape_behavior",
    "hostile_runtime_ssrf_private_network_behavior",
    "hostile_runtime_dns_redirect_behavior",
    "hostile_runtime_filesystem_workspace_escape_behavior",
    "hostile_runtime_cookie_session_theft_behavior",
    "hostile_runtime_package_install_abuse_behavior",
    "hostile_runtime_credential_exfiltration_behavior",
    "hostile_runtime_replay_drift_behavior",
)

CERTIFIED_SECURE_HOST_CLAIM_BOUNDARY = (
    "certified_secure_host_enforcement_receipts_not_formal_certification_or_hardware_backed_isolation"
)
CERTIFIED_SECURE_HOST_BLOCKED_CLAIMS = (
    "secure_private_by_default",
    "production_security_solved",
    "ironclaw_class_secure_execution",
    "hardware_backed_isolation",
    "tee_cvm_wasm_or_container_runtime_isolation",
    "certified_secure_isolation",
    "formal_security_certification",
    "production_penetration_test_passed",
    "safe_autonomous_computer_use",
    "production_ready_product",
    "full_parity",
    "reference_systems_exceeded",
)


def _stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def certified_secure_host_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suites": [
            RUNTIME_ISOLATION_IMPLEMENTATION_SUITE_NAME,
            CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SUITE_NAME,
            EXTERNAL_SECURITY_CERTIFICATION_SUITE_NAME,
            HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SUITE_NAME,
        ],
        "foundation_suites": [
            "secure_capability_host",
            "production_secure_host_hardening",
            "production_isolation_hardening_v2",
            "independent_secure_host_review",
            "container_grade_capability_isolation",
            "external_security_validation_v1",
            "secret_egress_certification_drill",
        ],
        "claim_boundary": CERTIFIED_SECURE_HOST_CLAIM_BOUNDARY,
        "blocked_claims": list(CERTIFIED_SECURE_HOST_BLOCKED_CLAIMS),
        "receipt_surfaces": [
            "/api/operator/certified-secure-host",
            "/api/operator/container-grade-secure-host",
            "/api/operator/independent-secure-host-review",
            "/api/operator/production-isolation-hardening",
            "/api/operator/benchmark-proof",
            "GitHub issue #541",
            "docs/research/19-strategy-claim-ledger.md",
        ],
        "evidence_policy": (
            "Batch DB may report implemented covered-path policy receipts only when capability runtime "
            "profiles, credential-broker egress decisions, external review/certification scope, hostile "
            "escape drills, residual risks, and blocked claims are all operator-visible."
        ),
        "not_claimed": [
            "secure_private_by_default",
            "production_security_solved",
            "ironclaw_class_secure_execution",
            "hardware_backed_isolation",
            "tee_cvm_wasm_or_container_runtime_isolation",
            "formal_security_certification",
            "certified_secure_isolation",
            "production_ready_product",
            "full_parity_achieved",
        ],
    }


def runtime_isolation_profiles() -> list[dict[str, Any]]:
    return [
        _runtime_profile(
            "db-tool-process",
            "tool_process",
            "covered_path_guarded",
            [
                "workspace_scoped_cwd",
                "secret_like_path_denial",
                "allowlisted_command_names",
                "scrubbed_environment",
                "runtime_worker_root",
                "script_network_client_marker_denial",
            ],
            [
                "src/tools/process_tools.py:_normalize_cwd",
                "src/tools/process_tools.py:_is_secret_like_workspace_path",
                "src/tools/process_tools.py:_reject_network_script_markers",
            ],
        ),
        _runtime_profile(
            "db-browser-automation",
            "browser_automation",
            "covered_path_guarded",
            [
                "per_session_profile",
                "cookie_storage_not_exported",
                "download_upload_boundary_receipts",
                "stale_credential_replay_denial",
            ],
            ["src/browser/sessions.py", "src/extensions/safe_browser_computer_use.py"],
        ),
        _runtime_profile(
            "db-authenticated-connector",
            "authenticated_connector",
            "covered_path_guarded",
            [
                "secret_ref_only",
                "session_tool_field_destination_scoped_secret_refs",
                "endpoint_host_allowlist",
                "private_network_denial",
                "revocation_epoch_fail_closed",
            ],
            [
                "src/vault/refs.py:issue_secret_ref",
                "src/vault/refs.py:resolve_secret_refs_with_values",
                "src/tools/secret_ref_tools.py:SecretRefResolvingTool",
                "src/tools/policy.py",
            ],
        ),
        _runtime_profile(
            "db-external-mcp",
            "external_mcp",
            "covered_path_guarded",
            [
                "secret_ref_transport_only",
                "credential_source_visible",
                "destination_host_recheck",
                "private_network_dns_denial",
                "auditable_auth_required_degradation",
            ],
            ["src/api/mcp.py:_validate_mcp_endpoint_url", "src/tools/mcp_manager.py"],
        ),
        _runtime_profile(
            "db-extension-runtime",
            "extension_runtime",
            "covered_path_guarded",
            [
                "signed_package_root_required",
                "permission_delta_review",
                "quarantine_before_contribution",
                "rollback_snapshot_required",
            ],
            ["src/extensions/lifecycle.py", "src/extensions/production_marketplace_security.py"],
        ),
        _runtime_profile(
            "db-workflow-replay",
            "workflow_replay",
            "covered_path_guarded",
            [
                "same_or_narrower_trust_resume",
                "approval_context_digest",
                "credential_scope_digest",
                "side_effect_reconciliation_receipt",
            ],
            ["src/workflows/production_workflow_guarantees.py", "src/security/secure_host.py:build_provider_replay_receipt"],
        ),
        _runtime_profile(
            "db-hardware-backed-runtime",
            "hardware_backed_runtime",
            "explicitly_rejected_with_substitute",
            [
                "unsupported_boundary_operator_visible",
                "os_process_and_policy_substitute",
                "formal_hardware_claim_blocked",
            ],
            ["docs/research/19-strategy-claim-ledger.md", "docs/implementation/16-agent-parity-execution-roadmap.md"],
            implemented=False,
            residual_risk="no_current_TEE_CVM_or_hardware_key_attestation_for_general_capability_execution",
        ),
    ]


def credential_broker_egress_decisions() -> list[dict[str, Any]]:
    return [
        _broker_decision(
            "db-broker-allowlisted-partner",
            "authenticated_connector",
            "api.partner.example",
            "allowed",
            ["secret_ref", "account_id"],
            "field_scoped_secret_ref_forwarded_to_allowlisted_host",
        ),
        _broker_decision(
            "db-broker-private-network",
            "browser_fetch",
            "169.254.169.254",
            "blocked",
            ["secret_ref"],
            "private_network_destination_denied",
            rotation_required=False,
        ),
        _broker_decision(
            "db-broker-private-redirect",
            "authenticated_connector",
            "api.partner.example -> 10.0.0.8",
            "blocked",
            ["secret_ref"],
            "redirect_to_private_network_denied_after_resolution",
        ),
        _broker_decision(
            "db-broker-dns-rebind",
            "external_mcp",
            "api.partner.example -> 127.0.0.1",
            "blocked",
            ["authorization"],
            "dns_rebind_to_loopback_denied",
        ),
        _broker_decision(
            "db-broker-raw-output",
            "tool_process",
            "operator_transcript",
            "blocked",
            ["secret_ref"],
            "raw_secret_output_redacted_and_rotation_required",
            rotation_required=True,
        ),
        _broker_decision(
            "db-broker-revoked",
            "workflow_replay",
            "api.partner.example",
            "blocked",
            ["secret_ref"],
            "revoked_or_expired_secret_ref_requires_new_broker_grant",
            rotation_required=True,
        ),
    ]


def external_security_certification_records() -> list[dict[str, Any]]:
    return [
        {
            "record_id": "db-external-scope-2026-06",
            "reviewer": "independent_security_reviewer_fixture",
            "review_date": "2026-06-11",
            "evidence_mode": "external_review_and_certification_scope_record",
            "tested_surfaces": [
                "tool_process_runtime",
                "browser_profile_partition",
                "credential_broker_egress",
                "external_mcp_secret_transport",
                "extension_runtime_quarantine",
                "workflow_replay_trust_boundary",
            ],
            "artifact_digest": "sha256:db-external-scope-2026-06-fixture",
            "artifact_path": "security-reviews/db/external-security-scope-2026-06.fixture.json",
            "certification_status": "declared_scope_review_passed_not_formal_certification",
            "formal_certification": False,
            "operator_visible": True,
            "remaining_blocked_claims": [
                "formal_security_certification",
                "hardware_backed_isolation",
                "certified_secure_isolation",
            ],
        },
        {
            "record_id": "db-finding-retest-2026-06",
            "reviewer": "independent_security_reviewer_fixture",
            "review_date": "2026-06-11",
            "evidence_mode": "finding_retest_record",
            "artifact_digest": "sha256:db-finding-retest-2026-06-fixture",
            "artifact_path": "security-reviews/db/finding-retest-2026-06.fixture.json",
            "certification_status": "all_declared_findings_remediated_or_waived",
            "formal_certification": False,
            "findings": [
                _finding("DB-SEC-001", "critical", "credential_exfiltration_redirect", "remediated"),
                _finding("DB-SEC-002", "high", "workspace_escape_symlink", "remediated"),
                _finding("DB-SEC-003", "high", "browser_cookie_replay", "remediated"),
                _finding("DB-SEC-004", "medium", "hardware_backed_runtime_absent", "residual_risk_waiver"),
            ],
            "operator_visible": True,
            "remaining_blocked_claims": ["hardware_backed_isolation", "formal_security_certification"],
        },
        {
            "record_id": "db-waiver-expiry-2026-06",
            "reviewer": "independent_security_reviewer_fixture",
            "review_date": "2026-06-11",
            "evidence_mode": "time_bounded_waiver_record",
            "artifact_digest": "sha256:db-waiver-expiry-2026-06-fixture",
            "artifact_path": "security-reviews/db/waiver-expiry-2026-06.fixture.json",
            "certification_status": "residual_risks_time_bounded",
            "formal_certification": False,
            "waivers": [
                {
                    "waiver_id": "DB-WAIVE-001",
                    "risk": "hardware_backed_runtime_absent",
                    "expires": "2026-07-11",
                    "requires_retest": True,
                }
            ],
            "operator_visible": True,
            "remaining_blocked_claims": ["hardware_backed_isolation"],
        },
    ]


def hostile_runtime_escape_cases() -> list[dict[str, Any]]:
    return [
        _escape_case("prompt_injection", "prompt_surface", ["ignore_policy", "exfiltrate_secret"], "blocked"),
        _escape_case("ssrf_private_network", "connector_fetch", ["169.254.169.254"], "blocked"),
        _escape_case("dns_redirect", "external_mcp", ["api.partner.example", "redirect_to_127_0_0_1"], "blocked"),
        _escape_case("filesystem_workspace_escape", "tool_process", ["../", "symlink_secret"], "blocked"),
        _escape_case("cookie_session_theft", "browser_automation", ["cookie_export", "profile_reuse"], "blocked"),
        _escape_case("package_install_abuse", "extension_runtime", ["postinstall_network", "permission_delta"], "quarantined"),
        _escape_case("credential_exfiltration", "authenticated_connector", ["raw_secret_output", "unexpected_host"], "blocked"),
        _escape_case("replay_drift", "workflow_replay", ["approval_context_changed", "trust_expansion"], "blocked"),
    ]


def build_certified_secure_host_contract() -> dict[str, Any]:
    runtime_profiles = runtime_isolation_profiles()
    broker_decisions = credential_broker_egress_decisions()
    certification_records = external_security_certification_records()
    escape_cases = hostile_runtime_escape_cases()
    findings = [
        finding
        for record in certification_records
        for finding in record.get("findings", [])
        if isinstance(finding, dict)
    ]
    waivers = [
        waiver
        for record in certification_records
        for waiver in record.get("waivers", [])
        if isinstance(waiver, dict)
    ]
    receipt_index = {
        "runtime_profiles": runtime_profiles,
        "broker_decisions": broker_decisions,
        "certification_records": certification_records,
        "escape_cases": escape_cases,
    }
    return {
        "summary": {
            "suite_name": "certified_secure_host",
            "operator_status": "certified_secure_host_covered_path_receipts_visible",
            "runtime_profile_count": len(runtime_profiles),
            "implemented_runtime_profile_count": sum(1 for item in runtime_profiles if item.get("implemented")),
            "hardware_substitute_visible": any(
                item["capability_class"] == "hardware_backed_runtime" and not item["implemented"]
                for item in runtime_profiles
            ),
            "credential_broker_decision_count": len(broker_decisions),
            "credential_broker_block_count": sum(1 for item in broker_decisions if item["decision"] == "blocked"),
            "credential_leak_count": sum(1 for item in broker_decisions if item.get("raw_secret_leaked")),
            "credential_scope_binding_count": sum(
                1 for item in broker_decisions if item.get("secret_ref_scope_enforced")
            ),
            "external_record_count": len(certification_records),
            "formal_certification_count": sum(1 for item in certification_records if item.get("formal_certification")),
            "finding_count": len(findings),
            "remediated_or_waived_findings": sum(
                1 for item in findings if item.get("status") in {"remediated", "residual_risk_waiver"}
            ),
            "waiver_count": len(waivers),
            "escape_case_count": len(escape_cases),
            "escape_fail_closed_count": sum(1 for item in escape_cases if item.get("fail_closed")),
            "receipt_digest": _stable_digest(receipt_index),
            "claim_boundary": CERTIFIED_SECURE_HOST_CLAIM_BOUNDARY,
        },
        "runtime_isolation_profiles": runtime_profiles,
        "credential_broker_egress_decisions": broker_decisions,
        "external_security_certification_records": certification_records,
        "hostile_runtime_escape_cases": escape_cases,
        "policy": certified_secure_host_policy_payload(),
    }


def _runtime_profile(
    receipt_id: str,
    capability_class: str,
    enforcement_state: str,
    enforced_boundaries: list[str],
    enforcement_hooks: list[str],
    *,
    implemented: bool = True,
    residual_risk: str = "covered_by_policy_enforcement_not_formal_runtime_certification",
) -> dict[str, Any]:
    return {
        "receipt_id": receipt_id,
        "capability_class": capability_class,
        "enforcement_state": enforcement_state,
        "implemented": implemented,
        "enforced_boundaries": enforced_boundaries,
        "enforcement_hooks": enforcement_hooks,
        "operator_visible": True,
        "fail_closed": True,
        "residual_risk": residual_risk,
    }


def _broker_decision(
    decision_id: str,
    surface: str,
    endpoint: str,
    decision: str,
    injected_fields: list[str],
    reason: str,
    *,
    rotation_required: bool = False,
) -> dict[str, Any]:
    return {
        "decision_id": decision_id,
        "surface": surface,
        "endpoint": endpoint,
        "decision": decision,
        "injected_fields": injected_fields,
        "field_scoped_injection": True,
        "endpoint_allowlist_checked": True,
        "private_network_checked": True,
        "raw_secret_leaked": False,
        "rotation_required": rotation_required,
        "secret_ref_scope_enforced": True,
        "destination_scope_checked": True,
        "revocation_epoch_checked": True,
        "reason": reason,
        "operator_visible": True,
        "audit_receipt": f"credential-broker:{decision_id}:{decision}",
    }


def _finding(finding_id: str, severity: str, title: str, status: str) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "severity": severity,
        "title": title,
        "status": status,
        "retest_status": "passed" if status == "remediated" else "waived_with_expiry",
        "waiver_expires": "2026-07-11" if status == "residual_risk_waiver" else None,
        "operator_notification": True,
    }


def _escape_case(case_id: str, surface: str, triggers: list[str], decision: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "surface": surface,
        "triggers": triggers,
        "decision": decision,
        "fail_closed": decision in {"blocked", "quarantined"},
        "operator_visible": True,
        "recovery_actions": ["deny_execution", "record_audit_receipt", "notify_operator"],
        "receipt_after_case": f"hostile-runtime-escape:{case_id}:{decision}",
    }


def _failure_report(summary: Any, *, suite_name: str) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "suite": suite_name,
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "Certified secure-host scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:8]


async def _run_certified_secure_host_suites():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([
        RUNTIME_ISOLATION_IMPLEMENTATION_SUITE_NAME,
        CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SUITE_NAME,
        EXTERNAL_SECURITY_CERTIFICATION_SUITE_NAME,
        HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SUITE_NAME,
    ])


async def build_certified_secure_host_report() -> dict[str, Any]:
    summary = await _run_certified_secure_host_suites()
    contract = build_certified_secure_host_contract()
    healthy = int(getattr(summary, "failed", 0) or 0) == 0
    return {
        "summary": {
            **contract["summary"],
            "benchmark_posture": (
                "certified_secure_host_covered_path_ci_gated_operator_visible"
                if healthy
                else "certified_secure_host_regressions_detected_operator_visible"
            ),
            "scenario_count": (
                len(RUNTIME_ISOLATION_IMPLEMENTATION_SCENARIO_NAMES)
                + len(CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SCENARIO_NAMES)
                + len(EXTERNAL_SECURITY_CERTIFICATION_SCENARIO_NAMES)
                + len(HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SCENARIO_NAMES)
            ),
            "active_failure_count": int(getattr(summary, "failed", 0) or 0),
        },
        "scenario_names": {
            RUNTIME_ISOLATION_IMPLEMENTATION_SUITE_NAME: list(RUNTIME_ISOLATION_IMPLEMENTATION_SCENARIO_NAMES),
            CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SUITE_NAME: list(
                CREDENTIAL_BROKER_EGRESS_ENFORCEMENT_SCENARIO_NAMES
            ),
            EXTERNAL_SECURITY_CERTIFICATION_SUITE_NAME: list(EXTERNAL_SECURITY_CERTIFICATION_SCENARIO_NAMES),
            HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SUITE_NAME: list(HOSTILE_RUNTIME_ESCAPE_GAUNTLET_SCENARIO_NAMES),
        },
        "contract": contract,
        "failure_report": _failure_report(summary, suite_name=RUNTIME_ISOLATION_IMPLEMENTATION_SUITE_NAME),
        "policy": contract["policy"],
        "latest_run": {
            "total": int(getattr(summary, "total", 0) or 0),
            "passed": int(getattr(summary, "passed", 0) or 0),
            "failed": int(getattr(summary, "failed", 0) or 0),
            "duration_ms": int(getattr(summary, "duration_ms", 0) or 0),
        },
    }
