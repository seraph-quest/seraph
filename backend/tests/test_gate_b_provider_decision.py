"""Focused proof for the deterministic #753 Gate B provider boundary."""

import json
from unittest.mock import patch

from src.memory.gate_b_provider_decision import (
    GATE_B_PROVIDER_DECISION_BLOCKED_CLAIMS,
    GATE_B_PROVIDER_DECISION_CLAIM_BOUNDARY,
    GATE_B_PROVIDER_DECISION_VERSION,
    build_gate_b_canonical_decision_record,
    build_gate_b_provider_decision_receipt,
)
from src.memory.gate_a_baseline import build_gate_a_baseline_receipt


def test_gate_b_records_a_deterministic_no_pilot_decision():
    first = build_gate_b_provider_decision_receipt()
    second = build_gate_b_provider_decision_receipt()

    assert first == second
    assert first["receipt_version"] == GATE_B_PROVIDER_DECISION_VERSION
    assert first["summary"] == {
        "status": "degraded",
        "provider_status": "blocked",
        "decision": "deferred",
        "reason_code": "openrouter_pilot_deferred_credential_not_provided",
        "measurement_status": "blocked",
        "pilot_status": "not_run",
        "operator_status": "gate_b_provider_pilot_deferred_canonical_memory_usable",
        "claim_boundary": GATE_B_PROVIDER_DECISION_CLAIM_BOUNDARY,
    }
    assert first["provider"] == {
        "provider_id": "openrouter",
        "credential_state": "not_provided_to_deterministic_boundary",
        "probe_status": "not_run",
        "call_attempted": False,
        "retrieval_payload_sent": False,
        "observed_quality": None,
    }
    assert first["canonical_memory"]["status"] == "usable"
    assert first["canonical_memory"]["provider_override"] == "blocked"
    assert first["contract_evidence"] == [
        "gate_a_frozen_canonical_memory_contract",
        "goal_loop_correction_influence_contract",
        "goal_loop_unresolved_strategy_delta_no_learning_contract",
    ]
    assert first["decision_record_contract"]["status_values"] == [
        "verified",
        "no_learning",
        "blocked",
    ]


def test_gate_b_receipt_is_content_and_credential_free():
    receipt = build_gate_b_provider_decision_receipt()
    encoded = json.dumps(receipt, sort_keys=True)

    assert receipt["safe_receipt"] == {
        "contains_memory_content": False,
        "contains_provider_payload": False,
        "contains_secret": False,
        "contains_private_path": False,
        "redaction": "versions_hashes_statuses_and_opaque_contract_handles_only",
    }
    assert "OPENROUTER_API_KEY" not in encoded
    assert "sk-" not in encoded
    assert "/home/" not in encoded
    assert receipt["provider"]["retrieval_payload_sent"] is False
    assert receipt["policy"]["blocked_claims"] == list(GATE_B_PROVIDER_DECISION_BLOCKED_CLAIMS)


def test_gate_b_invalid_gate_a_artifact_blocks_provider_decision():
    baseline = build_gate_a_baseline_receipt()

    with patch(
        "src.memory.gate_b_provider_decision.build_gate_a_baseline_receipt",
        return_value={
            **baseline,
            "artifact": {
                **baseline["artifact"],
                "corpus_sha256": "not-a-sha",
            },
            "summary": {
                **baseline["summary"],
                "artifact_status": "blocked",
            },
        },
    ):
        receipt = build_gate_b_provider_decision_receipt()

    assert receipt["summary"]["status"] == "blocked"
    assert receipt["summary"]["provider_status"] == "blocked"
    assert receipt["summary"]["reason_code"] == "canonical_gate_a_artifact_invalid"
    assert receipt["canonical_memory"]["status"] == "blocked"
    assert receipt["provider"]["call_attempted"] is False
    assert receipt["provider"]["retrieval_payload_sent"] is False


def test_canonical_correction_binds_later_decision_to_revision_and_owner():
    old = build_gate_b_canonical_decision_record(
        goal_id="goal-correction",
        goal_revision=1,
        plan_revision=1,
        decision_input_digest="a" * 64,
        decision="defer",
    )
    corrected = build_gate_b_canonical_decision_record(
        goal_id="goal-correction",
        goal_revision=2,
        plan_revision=2,
        decision_input_digest="b" * 64,
        memory_delta_id="delta-correction-1",
        memory_delta_provenance="verified",
        memory_control_owner="operator:test",
        decision="act",
        verification="passed",
    )

    assert old.status == "no_learning"
    assert old.learning == "no_learning"
    assert old.reason_code == "canonical_memory_delta_not_present"
    assert corrected.status == "verified"
    assert corrected.goal_revision == 2
    assert corrected.plan_revision == 2
    assert corrected.memory_delta_id == "delta-correction-1"
    assert corrected.memory_control_owner == "operator:test"
    assert corrected.decision_input_digest == "b" * 64
    assert corrected.decision == "act"
    assert corrected.record_id != old.record_id


def test_unresolved_correction_records_no_learning_before_any_effect():
    record = build_gate_b_canonical_decision_record(
        goal_id="goal-correction",
        goal_revision=2,
        plan_revision=2,
        decision_input_digest="c" * 64,
        memory_delta_id="delta-correction-1",
        memory_delta_provenance="unresolved",
        decision="act",
    )

    assert record.status == "no_learning"
    assert record.learning == "no_learning"
    assert record.reason_code == "canonical_memory_delta_unresolved"
    assert record.provider_override == "blocked"


def test_tombstone_revocation_and_restart_state_fail_closed():
    for state, reason in (
        ("tombstoned", "canonical_tombstoned_blocks_decision"),
        ("revoked", "canonical_revoked_blocks_decision"),
    ):
        record = build_gate_b_canonical_decision_record(
            goal_id="goal-deleted",
            goal_revision=3,
            plan_revision=3,
            decision_input_digest="d" * 64,
            memory_delta_id="delta-safe",
            memory_delta_provenance="verified",
            memory_control_owner="operator:test",
            memory_state=state,
            tombstone_ledger_revision="ledger-3",
            recovery_state="restart_reconciled",
            decision="act",
            verification="passed",
            requested_learning="applied",
        )
        assert record.status == "blocked"
        assert record.learning == "no_learning"
        assert record.reason_code == reason

    unreconciled = build_gate_b_canonical_decision_record(
        goal_id="goal-restart",
        goal_revision=3,
        plan_revision=3,
        decision_input_digest="e" * 64,
        memory_delta_id="delta-safe",
        memory_delta_provenance="verified",
        memory_control_owner="operator:test",
        recovery_state="restart_unverified",
        decision="act",
        verification="passed",
    )
    missing_ledger = build_gate_b_canonical_decision_record(
        goal_id="goal-restart",
        goal_revision=3,
        plan_revision=3,
        decision_input_digest="e" * 64,
        memory_delta_id="delta-safe",
        memory_delta_provenance="verified",
        memory_control_owner="operator:test",
        recovery_state="restart_reconciled",
        decision="act",
        verification="passed",
    )
    assert unreconciled.status == "blocked"
    assert unreconciled.reason_code == "canonical_restart_reconciliation_unverified"
    assert missing_ledger.status == "blocked"
    assert missing_ledger.reason_code == "canonical_tombstone_ledger_revision_missing"


def test_malformed_canonical_binding_is_redacted_and_blocked():
    record = build_gate_b_canonical_decision_record(
        goal_id=["private-goal-content"],
        goal_revision=None,
        plan_revision="future-plan",
        decision_input_digest="private-decision-content",
        memory_delta_id={"secret": "value"},
        memory_delta_provenance=[],
        memory_control_owner={"actor": "attacker"},
        memory_state=[],
        recovery_state={},
        decision={"command": "act"},
        verification=[],
        requested_learning=object(),
    )
    payload = record.as_payload()

    assert record.status == "blocked"
    assert record.learning == "no_learning"
    assert record.reason_code == "invalid_canonical_decision_binding"
    assert payload["content_redacted"] is True
    assert "private-goal-content" not in str(payload)
    assert "attacker" not in str(payload)


def test_goal_loop_redaction_rebuilds_untrusted_canonical_record():
    from src.guardian.goal_conditioned_loop import _redact_receipt_details

    details = _redact_receipt_details(
        {
            "canonical_decision_record": {
                "goal_id": "private goal content",
                "goal_revision": 2,
                "plan_revision": 2,
                "decision_input_digest": "payload-content",
                "memory_delta_id": "delta-safe",
                "memory_delta_provenance": "verified",
                "memory_control_owner": "attacker",
                "memory_state": "available",
                "recovery_state": "steady",
                "decision": "act",
                "verification": "passed",
                "learning": "applied",
            }
        }
    )

    record = details["canonical_decision_record"]
    assert record["status"] == "blocked"
    assert record["reason_code"] == "invalid_canonical_decision_binding"
    assert "private goal content" not in str(record)
    assert "payload-content" not in str(record)
    assert "attacker" not in str(record)
