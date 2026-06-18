"""One excellent reach-channel canary receipts.

This canary intentionally selects one existing channel surface and proves its
operator-visible contract before broad channel expansion.
"""

from __future__ import annotations

from typing import Any


ONE_REACH_CHANNEL_CANARY_SUITE_NAME = "one_excellent_reach_channel_canary"
ONE_REACH_CHANNEL_CANARY_SCENARIO_NAMES = (
    "one_reach_channel_selection_scope_behavior",
    "native_notification_pairing_revocation_behavior",
    "native_notification_health_retry_degraded_behavior",
    "native_notification_continuity_approval_audit_behavior",
    "operator_one_reach_channel_canary_surface_behavior",
)

ONE_REACH_CHANNEL_CANARY_CLAIM_BOUNDARY = (
    "deterministic_native_notification_canary_not_broad_live_channel_reach"
)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def one_reach_channel_canary_policy_payload() -> dict[str, Any]:
    return {
        "benchmark_suite": ONE_REACH_CHANNEL_CANARY_SUITE_NAME,
        "selected_channel": "native_notification",
        "selected_channel_id": "seraph.core-channel-adapters/native-notification",
        "selection_reason": (
            "native notifications are the highest-value currently shipped outside-browser reach surface "
            "because they can carry action-card style operator handoff while remaining local and auditable"
        ),
        "channel_sprawl_policy": "prove_one_channel_before_adding_or_claiming_broad_external_channel_reach",
        "claim_boundary": ONE_REACH_CHANNEL_CANARY_CLAIM_BOUNDARY,
        "not_claimed": [
            "broad_mobile_or_messaging_channel_coverage",
            "production_grade_pairing_protocol",
            "live_slack_discord_telegram_delivery",
            "unsupervised_external_mutation",
            "competitor_reach_superiority",
        ],
        "approval_policy": "external_channel_actions_require_operator_approval_handoff_before_mutation",
        "privacy_policy": "channel_receipts_carry_ids_hashes_and_thread_keys_not_message_secret_payloads",
        "receipt_surfaces": [
            "/api/operator/one-reach-channel-canary",
            "/api/operator/control-plane",
            "/api/operator/benchmark-proof",
            "/api/activity",
        ],
    }


def one_reach_channel_canary_protocol() -> dict[str, Any]:
    return {
        "protocol_id": "seraph-one-reach-channel-native-notification-canary-v1",
        "time_anchor": "2026-05-11T11:00:00Z",
        "seed": "seraph-438-one-excellent-reach-channel-canary",
        "selected_channel": "native_notification",
        "replay_command": (
            "uv run python -m src.evals.harness --benchmark-suite "
            f"{ONE_REACH_CHANNEL_CANARY_SUITE_NAME} --indent 0"
        ),
        "operator_receipt_endpoint": "/api/operator/one-reach-channel-canary",
    }


def one_reach_channel_canary_receipt() -> dict[str, Any]:
    channel_id = "seraph.core-channel-adapters/native-notification"
    thread_id = "reach-thread-native-001"
    memory_context_id = "memctx-reach-native-001"
    approval_id = "approval-reach-native-reply-001"
    audit_id = "audit-reach-native-response-001"
    return {
        "selected_channel": {
            "channel_id": channel_id,
            "name": "native-notification",
            "transport": "native_notification",
            "surface_kind": "operator_channel",
            "selected": True,
            "selection_rank": 1,
            "rejected_channel_sprawl": [
                "slack",
                "discord",
                "telegram",
                "sms",
                "voice",
            ],
            "selection_boundary": "one_channel_canary_only",
        },
        "pairing": {
            "pairing_id": "pair-native-daemon-001",
            "device_id": "desktop-native-daemon",
            "pairing_state": "paired",
            "trust_state": "trusted",
            "paired_at": "2026-05-11T11:00:00Z",
            "scopes": ["notify", "reply_action", "approval_handoff"],
            "safe_follow_up_ready": True,
            "audit_receipt_id": "audit:reach-native:paired",
        },
        "revocation_probe": {
            "pairing_id": "pair-native-daemon-revoked",
            "device_id": "desktop-native-daemon-old",
            "pairing_state": "revoked",
            "trust_state": "untrusted",
            "revoked_at": "2026-05-11T11:04:00Z",
            "revocation_reason": "operator_revoked_old_daemon_pairing",
            "safe_follow_up_hidden": True,
            "audit_receipt_id": "audit:reach-native:revoked",
        },
        "health": {
            "ready_probe": {
                "transport": "native_notification",
                "status": "ready",
                "daemon_connected": True,
                "available": True,
                "checked_at": "2026-05-11T11:05:00Z",
            },
            "degraded_probe": {
                "transport": "native_notification",
                "status": "daemon_offline",
                "daemon_connected": False,
                "available": False,
                "degraded_state_ui": "Reconnect the native daemon or keep the response queued.",
                "follow_up_hidden": True,
            },
            "retry_policy": {
                "retry_id": "retry-native-notification-001",
                "max_attempts": 3,
                "backoff": "bounded_exponential",
                "fallback_transport": "websocket",
                "audit_receipt_id": "audit:reach-native:retry-scheduled",
            },
        },
        "continuity": {
            "thread_id": thread_id,
            "channel_thread_key": "native://desktop-native-daemon/reach-thread-native-001",
            "session_id": "session-reach-native-001",
            "previous_message_id": "native-msg-inbound-001",
            "response_message_id": "native-msg-response-001",
            "memory_context_id": memory_context_id,
            "memory_context_summary": "Project release question and operator approval preference retained.",
            "context_receipts": [
                "session:session-reach-native-001",
                f"memory_context:{memory_context_id}",
                f"channel_thread:{thread_id}",
            ],
        },
        "e2e_flow": [
            {
                "step": "external_message_received",
                "message_id": "native-msg-inbound-001",
                "channel_id": channel_id,
                "thread_id": thread_id,
                "payload_hash": "sha256:native-inbound-001",
                "content_redacted": True,
            },
            {
                "step": "seraph_decision",
                "decision": "request_approval_before_channel_response",
                "guardian_action_posture": "approval_first",
                "memory_context_id": memory_context_id,
                "reason": "external channel response could commit operator intent",
            },
            {
                "step": "approval_handoff",
                "approval_id": approval_id,
                "fingerprint": "fp-reach-native-response-001",
                "risk_level": "medium",
                "status": "pending_operator_approval",
                "mutation_boundary": "response_draft_only_until_approved",
            },
            {
                "step": "audited_response",
                "audit_id": audit_id,
                "action": "queue_native_notification_response",
                "status": "queued_after_approval_handoff",
                "response_message_id": "native-msg-response-001",
            },
        ],
        "approval_handoff": {
            "approval_id": approval_id,
            "fingerprint": "fp-reach-native-response-001",
            "risk_level": "medium",
            "status": "pending_operator_approval",
            "mutation_boundary": "response_draft_only_until_approved",
            "operator_surface": "/api/operator/control-plane",
        },
        "audit_receipts": [
            "audit:reach-native:paired",
            "audit:reach-native:revoked",
            "audit:reach-native:inbound",
            "audit:reach-native:decision",
            "audit:reach-native:approval-handoff",
            audit_id,
            "audit:reach-native:retry-scheduled",
        ],
        "degraded_state_ui": {
            "status": "visible",
            "primary_degraded_reason": "daemon_offline",
            "repair_action": "reconnect_native_daemon",
            "unsafe_follow_up_hidden": True,
            "queued_response_state": "held_until_channel_ready_or_operator_redirects",
        },
        "claim_boundary": ONE_REACH_CHANNEL_CANARY_CLAIM_BOUNDARY,
    }


def _failure_report(summary: Any) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for result in getattr(summary, "results", []):
        if getattr(result, "passed", True):
            continue
        failures.append({
            "type": "benchmark_regression",
            "scenario_name": str(getattr(result, "name", "") or "unknown_scenario"),
            "summary": str(getattr(result, "error", "") or "One reach-channel canary scenario failed."),
            "reason": "deterministic_eval_failure",
        })
    return failures[:6]


async def _run_one_reach_channel_canary_suite():
    from src.evals.harness import run_benchmark_suites

    return await run_benchmark_suites([ONE_REACH_CHANNEL_CANARY_SUITE_NAME])


async def build_one_reach_channel_canary_report() -> dict[str, Any]:
    summary = await _run_one_reach_channel_canary_suite()
    receipt = one_reach_channel_canary_receipt()
    policy = one_reach_channel_canary_policy_payload()
    e2e_steps = _as_list(receipt.get("e2e_flow"))
    audit_receipts = _as_list(receipt.get("audit_receipts"))
    healthy = summary.failed == 0
    return {
        "summary": {
            "suite_name": ONE_REACH_CHANNEL_CANARY_SUITE_NAME,
            "benchmark_posture": (
                "one_reach_channel_canary_ci_gated_operator_visible"
                if healthy
                else "one_reach_channel_canary_regressions_detected_operator_visible"
            ),
            "operator_status": "one_reach_channel_canary_visible",
            "selected_channel": "native_notification",
            "scenario_count": len(ONE_REACH_CHANNEL_CANARY_SCENARIO_NAMES),
            "active_failure_count": summary.failed,
            "pairing_state": receipt["pairing"]["pairing_state"],
            "revocation_state": receipt["revocation_probe"]["pairing_state"],
            "health_state": receipt["health"]["ready_probe"]["status"],
            "degraded_state": receipt["health"]["degraded_probe"]["status"],
            "retry_state": "bounded_retry_with_fallback_visible",
            "thread_continuity_state": "channel_thread_session_and_memory_context_linked",
            "approval_handoff_state": receipt["approval_handoff"]["status"],
            "audit_receipt_count": len(audit_receipts),
            "e2e_step_count": len(e2e_steps),
            "channel_sprawl_state": "rejected_until_native_notification_canary_meets_bar",
            "claim_boundary": ONE_REACH_CHANNEL_CANARY_CLAIM_BOUNDARY,
        },
        "scenario_names": list(ONE_REACH_CHANNEL_CANARY_SCENARIO_NAMES),
        "protocol": one_reach_channel_canary_protocol(),
        "policy": policy,
        "receipt": receipt,
        "operator_story": {
            "single_channel_selected": receipt["selected_channel"]["transport"] == "native_notification",
            "channel_sprawl_rejected": bool(receipt["selected_channel"]["rejected_channel_sprawl"]),
            "pairing_visible": receipt["pairing"]["pairing_state"] == "paired",
            "revocation_fail_closed_visible": receipt["revocation_probe"]["safe_follow_up_hidden"] is True,
            "health_visible": receipt["health"]["ready_probe"]["status"] == "ready",
            "retry_visible": receipt["health"]["retry_policy"]["max_attempts"] == 3,
            "thread_continuity_visible": bool(receipt["continuity"]["thread_id"]),
            "memory_context_visible": bool(receipt["continuity"]["memory_context_id"]),
            "approval_handoff_visible": receipt["approval_handoff"]["status"] == "pending_operator_approval",
            "audit_trail_visible": len(audit_receipts) >= 6,
            "degraded_state_ui_visible": receipt["degraded_state_ui"]["unsafe_follow_up_hidden"] is True,
            "e2e_flow_visible": [step.get("step") for step in e2e_steps] == [
                "external_message_received",
                "seraph_decision",
                "approval_handoff",
                "audited_response",
            ],
        },
        "failure_report": _failure_report(summary),
        "latest_run": {
            "total": summary.total,
            "passed": summary.passed,
            "failed": summary.failed,
            "duration_ms": summary.duration_ms,
        },
    }
