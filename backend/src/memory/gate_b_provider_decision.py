"""Deterministic Gate B provider-decision boundary.

Gate B is intentionally split into two questions.  The canonical-memory and
goal-loop contracts can be exercised locally without a model, while an
OpenRouter pilot needs a separately governed credential, capability, egress,
and outcome receipt.  This module records the latter as a measured
no-pilot/deferred decision until that proof exists.

The builder never reads a secret, calls a provider, performs retrieval, or
changes memory.  It binds the decision to the already frozen Gate A artifact
by version and digest only, so a provider receipt cannot silently become a
quality or superiority claim.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

from src.memory.gate_a_baseline import build_gate_a_baseline_receipt


GATE_B_PROVIDER_DECISION_VERSION = "guardian-memory-gate-b-provider-v1"
GATE_B_PROVIDER_DECISION_CLAIM_BOUNDARY = (
    "deterministic_no_pilot_admission_decision_not_live_provider_quality_or_memory_superiority"
)
GATE_B_PROVIDER_DECISION_BLOCKED_CLAIMS = (
    "memory_superiority",
    "guardian_intelligence_superiority",
    "provider_parity",
    "live_provider_quality",
    "later_goal_decision_usefulness_without_runtime_outcome_receipt",
)
GATE_B_CANONICAL_DECISION_RECORD_VERSION = "guardian-memory-canonical-decision-v1"
GATE_B_CANONICAL_DECISION_CLAIM_BOUNDARY = (
    "canonical_goal_decision_binding_not_adaptive_learning_or_live_provider_proof"
)

_SAFE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_CONTROL_OWNER = re.compile(r"^(?:operator|service):[A-Za-z0-9][A-Za-z0-9._:-]{0,126}$")

GateBStatus = Literal["pass", "degraded", "blocked"]
GateBProviderStatus = Literal["blocked"]
GateBDecision = Literal["deferred"]
GateBMeasurementStatus = Literal["blocked"]
GateBPilotStatus = Literal["not_run"]
GateBCanonicalDecisionStatus = Literal["verified", "no_learning", "blocked"]
GateBCanonicalMemoryState = Literal["available", "tombstoned", "revoked", "unknown"]
GateBRecoveryState = Literal["steady", "restart_reconciled", "restart_unverified"]
GateBCanonicalLearning = Literal["applied", "no_learning"]


@dataclass(frozen=True)
class GateBCanonicalDecisionRecord:
    """Safe binding for one goal decision and its canonical memory authority."""

    record_id: str
    record_version: str
    goal_id: str | None
    goal_revision: int | None
    plan_revision: int | None
    decision_input_digest: str | None
    memory_delta_id: str | None
    memory_delta_provenance: str
    memory_control_owner: str | None
    memory_state: GateBCanonicalMemoryState
    tombstone_ledger_revision: str | None
    recovery_state: GateBRecoveryState
    decision: str | None
    verification: str
    learning: GateBCanonicalLearning
    status: GateBCanonicalDecisionStatus
    reason_code: str
    authority: Literal["guardian_canonical_memory"]
    provider_override: Literal["blocked"]
    claim_boundary: str

    def as_payload(self) -> dict[str, Any]:
        """Return only bounded identifiers, digests, statuses, and policy."""

        return {
            "record_id": self.record_id,
            "record_version": self.record_version,
            "goal_id": self.goal_id,
            "goal_revision": self.goal_revision,
            "plan_revision": self.plan_revision,
            "decision_input_digest": self.decision_input_digest,
            "memory_delta_id": self.memory_delta_id,
            "memory_delta_provenance": self.memory_delta_provenance,
            "memory_control_owner": self.memory_control_owner,
            "memory_state": self.memory_state,
            "tombstone_ledger_revision": self.tombstone_ledger_revision,
            "recovery_state": self.recovery_state,
            "decision": self.decision,
            "verification": self.verification,
            "learning": self.learning,
            "status": self.status,
            "reason_code": self.reason_code,
            "authority": self.authority,
            "provider_override": self.provider_override,
            "claim_boundary": self.claim_boundary,
            "content_redacted": True,
        }


def gate_b_canonical_decision_contract_payload() -> dict[str, Any]:
    """Describe the shared decision binding without creating a user record."""

    return {
        "record_version": GATE_B_CANONICAL_DECISION_RECORD_VERSION,
        "required_bindings": [
            "goal_id",
            "goal_revision",
            "plan_revision",
            "decision_input_digest",
            "memory_delta_id_and_provenance",
            "memory_control_owner",
            "memory_state",
            "tombstone_ledger_revision",
            "recovery_state",
        ],
        "status_values": ["verified", "no_learning", "blocked"],
        "learning_policy": "unresolved_or_unverified_canonical_evidence_forces_no_learning",
        "deletion_policy": "tombstoned_or_revoked_memory_blocks_decision_and_learning",
        "restart_policy": "restart_requires_current_tombstone_ledger_reconciliation",
        "authority": "guardian_canonical_memory",
        "provider_override": "blocked",
        "claim_boundary": GATE_B_CANONICAL_DECISION_CLAIM_BOUNDARY,
    }


def _safe_opaque_id(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_OPAQUE_ID.fullmatch(value) else None


def _safe_control_owner(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_CONTROL_OWNER.fullmatch(value) else None


def _safe_revision(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _safe_choice(value: object, allowed: set[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def _decision_record_id(values: dict[str, Any]) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "decision_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def build_gate_b_canonical_decision_record(
    *,
    goal_id: object,
    goal_revision: object,
    plan_revision: object,
    decision_input_digest: object,
    memory_delta_id: object = None,
    memory_delta_provenance: object = "not_present",
    memory_control_owner: object = None,
    memory_state: object = "available",
    tombstone_ledger_revision: object = None,
    recovery_state: object = "steady",
    decision: object = "act",
    verification: object = "unknown",
    requested_learning: object = "no_learning",
) -> GateBCanonicalDecisionRecord:
    """Build a fail-closed canonical decision binding.

    The helper is deliberately pure.  A caller must supply the current goal
    and plan revisions, the digest used for the choice, the strategy-delta
    provenance and its authenticated control owner, plus the current
    tombstone/restart state.  Missing or contradictory bindings become an
    explicit no-learning or blocked record; they never become a positive
    memory update.
    """

    safe_goal_id = _safe_opaque_id(goal_id)
    safe_goal_revision = _safe_revision(goal_revision)
    safe_plan_revision = _safe_revision(plan_revision)
    safe_digest = _safe_sha256(decision_input_digest)
    safe_delta_id = _safe_opaque_id(memory_delta_id) if memory_delta_id is not None else None
    safe_owner = _safe_control_owner(memory_control_owner) if memory_control_owner is not None else None
    safe_ledger_revision = (
        _safe_opaque_id(tombstone_ledger_revision)
        if tombstone_ledger_revision is not None
        else None
    )
    safe_provenance = _safe_choice(
        memory_delta_provenance,
        {"verified", "unresolved", "not_present"},
        "unresolved",
    )
    safe_memory_state: GateBCanonicalMemoryState = (
        _safe_choice(
            memory_state,
            {"available", "tombstoned", "revoked", "unknown"},
            "unknown",
        )
    )
    safe_recovery_state: GateBRecoveryState = (
        _safe_choice(
            recovery_state,
            {"steady", "restart_reconciled", "restart_unverified"},
            "restart_unverified",
        )
    )
    safe_decision = (
        decision
        if isinstance(decision, str)
        and decision in {"act", "clarify", "defer", "silent", "request_approval"}
        else None
    )
    safe_verification = _safe_choice(verification, {"passed", "failed", "unknown"}, "unknown")
    safe_requested_learning = _safe_choice(
        requested_learning,
        {"applied", "no_learning"},
        "no_learning",
    )

    binding_values = {
        "goal_id": safe_goal_id,
        "goal_revision": safe_goal_revision,
        "plan_revision": safe_plan_revision,
        "decision_input_digest": safe_digest,
        "memory_delta_id": safe_delta_id,
        "memory_delta_provenance": safe_provenance,
        "memory_control_owner": safe_owner,
        "memory_state": safe_memory_state,
        "tombstone_ledger_revision": safe_ledger_revision,
        "recovery_state": safe_recovery_state,
        "decision": safe_decision,
        "verification": safe_verification,
    }
    record_id = _decision_record_id(binding_values)

    reason_code = "canonical_decision_verified"
    status: GateBCanonicalDecisionStatus = "verified"
    learning: GateBCanonicalLearning = "no_learning"
    if (
        safe_goal_id is None
        or safe_goal_revision is None
        or safe_plan_revision is None
        or safe_digest is None
        or safe_decision is None
    ):
        status = "blocked"
        reason_code = "invalid_canonical_decision_binding"
    elif safe_recovery_state == "restart_unverified":
        status = "blocked"
        reason_code = "canonical_restart_reconciliation_unverified"
    elif safe_memory_state in {"tombstoned", "revoked"}:
        status = "blocked"
        reason_code = f"canonical_{safe_memory_state}_blocks_decision"
    elif safe_memory_state == "unknown":
        status = "blocked"
        reason_code = "canonical_memory_state_unknown"
    elif safe_recovery_state == "restart_reconciled" and safe_ledger_revision is None:
        status = "blocked"
        reason_code = "canonical_tombstone_ledger_revision_missing"
    elif safe_provenance == "verified" and (safe_delta_id is None or safe_owner is None):
        status = "blocked"
        reason_code = "verified_memory_delta_owner_binding_missing"
    elif safe_provenance != "verified":
        status = "no_learning"
        reason_code = (
            "canonical_memory_delta_unresolved"
            if safe_provenance == "unresolved"
            else "canonical_memory_delta_not_present"
        )
    elif safe_requested_learning == "applied" and safe_verification == "passed":
        learning = "applied"

    if status in {"blocked", "no_learning"}:
        learning = "no_learning"
    return GateBCanonicalDecisionRecord(
        record_id=record_id,
        record_version=GATE_B_CANONICAL_DECISION_RECORD_VERSION,
        goal_id=safe_goal_id,
        goal_revision=safe_goal_revision,
        plan_revision=safe_plan_revision,
        decision_input_digest=safe_digest,
        memory_delta_id=safe_delta_id,
        memory_delta_provenance=safe_provenance,
        memory_control_owner=safe_owner,
        memory_state=safe_memory_state,
        tombstone_ledger_revision=safe_ledger_revision,
        recovery_state=safe_recovery_state,
        decision=safe_decision,
        verification=safe_verification,
        learning=learning,
        status=status,
        reason_code=reason_code,
        authority="guardian_canonical_memory",
        provider_override="blocked",
        claim_boundary=GATE_B_CANONICAL_DECISION_CLAIM_BOUNDARY,
    )


@dataclass(frozen=True)
class GateBProviderDecisionReceipt:
    """Immutable, metadata-only receipt for the provider admission decision."""

    receipt_id: str
    receipt_version: str
    provider_id: str
    status: GateBStatus
    provider_status: GateBProviderStatus
    decision: GateBDecision
    reason_code: str
    measurement_status: GateBMeasurementStatus
    pilot_status: GateBPilotStatus
    credential_state: str
    provider_probe_status: str
    provider_call_attempted: bool
    retrieval_payload_sent: bool
    observed_quality: None
    canonical_memory_status: str
    canonical_decision_contract_status: str
    canonical_decision_runtime_status: str
    gate_a_artifact_status: str
    gate_a_corpus_version: str | None
    gate_a_metric_contract_version: str | None
    gate_a_corpus_sha256: str | None
    gate_a_metric_contract_sha256: str | None
    contract_evidence: tuple[str, ...]
    claim_boundary: str
    blocked_claims: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        """Return the stable operator payload without content or credentials."""

        return {
            "receipt_id": self.receipt_id,
            "receipt_version": self.receipt_version,
            "summary": {
                "status": self.status,
                "provider_status": self.provider_status,
                "decision": self.decision,
                "reason_code": self.reason_code,
                "measurement_status": self.measurement_status,
                "pilot_status": self.pilot_status,
                "operator_status": (
                    "gate_b_provider_pilot_deferred_canonical_memory_usable"
                    if self.status == "degraded"
                    else "gate_b_provider_decision_blocked"
                ),
                "claim_boundary": self.claim_boundary,
            },
            "provider": {
                "provider_id": self.provider_id,
                "credential_state": self.credential_state,
                "probe_status": self.provider_probe_status,
                "call_attempted": self.provider_call_attempted,
                "retrieval_payload_sent": self.retrieval_payload_sent,
                "observed_quality": self.observed_quality,
            },
            "canonical_memory": {
                "status": self.canonical_memory_status,
                "decision_contract_status": self.canonical_decision_contract_status,
                "runtime_status": self.canonical_decision_runtime_status,
                "authority": "guardian_canonical_memory",
                "provider_override": "blocked",
            },
            "decision_record_contract": gate_b_canonical_decision_contract_payload(),
            "gate_a_binding": {
                "artifact_status": self.gate_a_artifact_status,
                "corpus_version": self.gate_a_corpus_version,
                "metric_contract_version": self.gate_a_metric_contract_version,
                "corpus_sha256": self.gate_a_corpus_sha256,
                "metric_contract_sha256": self.gate_a_metric_contract_sha256,
            },
            "contract_evidence": list(self.contract_evidence),
            "policy": {
                "admission": "credential_capability_egress_and_consent_proof_required_before_pilot",
                "pilot": "at_most_one_bounded_advisory_pilot_after_admission_proof",
                "canonical_first": "provider_evidence_is_advisory_and_cannot_override_canonical_memory",
                "outage": "canonical_memory_remains_usable_and_provider_state_is_visible",
                "learning": "no_learning_without_verified_outcome_and_governed_writeback",
                "blocked_claims": list(self.blocked_claims),
                "receipt_surfaces": [
                    "/api/operator/memory-benchmark",
                    "/api/memory/providers",
                ],
            },
            "safe_receipt": {
                "contains_memory_content": False,
                "contains_provider_payload": False,
                "contains_secret": False,
                "contains_private_path": False,
                "redaction": "versions_hashes_statuses_and_opaque_contract_handles_only",
            },
        }


def _safe_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _safe_sha256(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_SHA256.fullmatch(value) else None


def _gate_a_binding() -> tuple[dict[str, Any], bool]:
    """Read only the content-free Gate A binding needed by this receipt."""

    baseline = build_gate_a_baseline_receipt()
    artifact = baseline.get("artifact") if isinstance(baseline, dict) else None
    summary = baseline.get("summary") if isinstance(baseline, dict) else None
    artifact = artifact if isinstance(artifact, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    corpus_sha256 = _safe_sha256(artifact.get("corpus_sha256"))
    metric_sha256 = _safe_sha256(artifact.get("metric_contract_sha256"))
    artifact_status = _safe_string(summary.get("artifact_status")) or "blocked"
    binding = {
        "artifact_status": artifact_status,
        "corpus_version": _safe_string(artifact.get("corpus_version")),
        "metric_contract_version": _safe_string(artifact.get("metric_contract_version")),
        "corpus_sha256": corpus_sha256,
        "metric_contract_sha256": metric_sha256,
    }
    valid = (
        artifact_status == "pass"
        and corpus_sha256 is not None
        and metric_sha256 is not None
        and binding["corpus_version"] is not None
        and binding["metric_contract_version"] is not None
    )
    return binding, valid


def build_gate_b_provider_decision_receipt() -> dict[str, Any]:
    """Build the current no-pilot decision without touching OpenRouter.

    ``status=degraded`` means the canonical local contract remains usable while
    the provider lane is blocked.  If the frozen Gate A artifact is invalid,
    the whole receipt becomes ``blocked`` so a drifted corpus cannot be used as
    an apparently healthy provider baseline.
    """

    binding, artifact_valid = _gate_a_binding()
    status: GateBStatus = "degraded" if artifact_valid else "blocked"
    reason_code = (
        "openrouter_pilot_deferred_credential_not_provided"
        if artifact_valid
        else "canonical_gate_a_artifact_invalid"
    )
    corpus_sha256 = binding["corpus_sha256"]
    metric_sha256 = binding["metric_contract_sha256"]
    receipt_id = (
        f"{GATE_B_PROVIDER_DECISION_VERSION}:"
        f"{(corpus_sha256 or 'unbound')[:16]}:"
        f"{(metric_sha256 or 'unbound')[:16]}"
    )
    receipt = GateBProviderDecisionReceipt(
        receipt_id=receipt_id,
        receipt_version=GATE_B_PROVIDER_DECISION_VERSION,
        provider_id="openrouter",
        status=status,
        provider_status="blocked",
        decision="deferred",
        reason_code=reason_code,
        measurement_status="blocked",
        pilot_status="not_run",
        credential_state="not_provided_to_deterministic_boundary",
        provider_probe_status="not_run",
        provider_call_attempted=False,
        retrieval_payload_sent=False,
        observed_quality=None,
        canonical_memory_status="usable" if artifact_valid else "blocked",
        canonical_decision_contract_status="covered",
        canonical_decision_runtime_status="not_run",
        gate_a_artifact_status=binding["artifact_status"],
        gate_a_corpus_version=binding["corpus_version"],
        gate_a_metric_contract_version=binding["metric_contract_version"],
        gate_a_corpus_sha256=corpus_sha256,
        gate_a_metric_contract_sha256=metric_sha256,
        contract_evidence=(
            "gate_a_frozen_canonical_memory_contract",
            "goal_loop_correction_influence_contract",
            "goal_loop_unresolved_strategy_delta_no_learning_contract",
        ),
        claim_boundary=GATE_B_PROVIDER_DECISION_CLAIM_BOUNDARY,
        blocked_claims=GATE_B_PROVIDER_DECISION_BLOCKED_CLAIMS,
    )
    return receipt.as_payload()
