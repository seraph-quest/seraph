"""Frozen, deterministic Gate A contract for canonical-memory evaluation.

Gate A is deliberately smaller than a benchmark platform.  This module keeps
the case identifiers, expected authority, exclusion rules, and metric
thresholds in source control so a later runtime measurement can be compared
with the same input contract.  It does not call a model, a provider, or the
database.  The operator receipt therefore reports fixture coverage separately
from runtime measurement and never presents the fixture as a quality result.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping


GATE_A_BASELINE_VERSION = "guardian-memory-gate-a-v1"
GATE_A_BASELINE_METRIC_VERSION = "guardian-memory-metrics-v1"
GATE_A_BASELINE_CLAIM_BOUNDARY = (
    "frozen_deterministic_memory_contract_and_fixture_coverage_not_runtime_quality_or_provider_superiority"
)
GATE_A_BASELINE_BLOCKED_CLAIMS = (
    "memory_superiority",
    "guardian_intelligence_superiority",
    "provider_parity",
    "live_provider_quality",
    "later_goal_decision_usefulness",
)

_CASE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{2,63}$")
_EVIDENCE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
_RECEIPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SAFE_RECEIPT_FIELDS = frozenset(
    {
        "provenance",
        "source_id",
        "confidence",
        "freshness",
        "scope",
        "authority",
        "conflict_state",
        "tombstone_state",
        "redaction_state",
        "trust_class",
        "rebuild_state",
        "outage_state",
    }
)
_ALLOWED_AUTHORITIES = frozenset({"canonical", "advisory", "none"})
_ALLOWED_FIXTURE_CLASSES = frozenset({"positive", "negative"})
_METRIC_DIMENSIONS = {
    "exact_evidence_recall": "exact_recall",
    "temporal_freshness": "temporal_freshness",
    "semantic_recall": "semantic_recall",
    "contradiction_suppression": "contradiction_suppression",
    "provenance_coverage": "exact_recall",
    "delete_exclusion": "deletion_exclusion",
    "restore_tombstone_exclusion": "restore_safety",
    "provider_outage_canonical_continuity": "provider_outage_continuity",
}
_REQUIRED_NEGATIVE_CASES = frozenset(
    {
        "malformed_provider_hit",
        "poisoned_observation",
        "stale_canonical_conflict",
        "unknown_vector_identity",
        "deleted_memory_exclusion",
    }
)


@dataclass(frozen=True)
class GateABaselineCase:
    """Content-free description of one deterministic baseline case."""

    case_id: str
    dimension: str
    query_class: str
    expected_evidence_ids: tuple[str, ...]
    excluded_evidence_ids: tuple[str, ...]
    expected_authority: str
    fixture_class: str
    required_receipt_fields: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GateAMetric:
    """Predeclared metric threshold used by a later runtime measurement."""

    name: str
    description: str
    unit: str
    direction: str
    threshold: float

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GateAMeasurementReceipt:
    """Typed binding for a runtime measurement of the frozen contract."""

    corpus_sha256: str
    metric_contract_sha256: str
    runner_id: str
    execution_receipt_id: str

    def as_payload(self) -> dict[str, Any]:
        return asdict(self)


# The fixture is intentionally made from handles and classifications.  It has
# no user text, credentials, workspace paths, or provider payloads.
_GATE_A_BASELINE_CASES: tuple[GateABaselineCase, ...] = (
    GateABaselineCase(
        case_id="goal_context_recall",
        dimension="exact_recall",
        query_class="goal_context",
        expected_evidence_ids=("canon_goal_001", "canon_commitment_001"),
        excluded_evidence_ids=("tomb_goal_001",),
        expected_authority="canonical",
        fixture_class="positive",
        required_receipt_fields=("provenance", "source_id", "confidence", "freshness", "scope", "authority"),
    ),
    GateABaselineCase(
        case_id="temporal_freshness",
        dimension="temporal_freshness",
        query_class="current_project_state",
        expected_evidence_ids=("canon_project_current_001",),
        excluded_evidence_ids=("canon_project_stale_001",),
        expected_authority="canonical",
        fixture_class="positive",
        required_receipt_fields=("provenance", "source_id", "confidence", "freshness", "scope"),
    ),
    GateABaselineCase(
        case_id="semantic_project_recall",
        dimension="semantic_recall",
        query_class="project_alias_and_goal_context",
        expected_evidence_ids=("canon_project_current_001", "canon_goal_001"),
        excluded_evidence_ids=("unknown_vector_001",),
        expected_authority="canonical",
        fixture_class="positive",
        required_receipt_fields=("provenance", "source_id", "confidence", "scope", "authority"),
    ),
    GateABaselineCase(
        case_id="stale_canonical_conflict",
        dimension="contradiction_suppression",
        query_class="corrected_preference",
        expected_evidence_ids=("canon_preference_current_001",),
        excluded_evidence_ids=("canon_preference_stale_001", "adv_preference_conflict_001"),
        expected_authority="canonical",
        fixture_class="negative",
        required_receipt_fields=("provenance", "source_id", "confidence", "freshness", "conflict_state", "authority"),
    ),
    GateABaselineCase(
        case_id="provider_advisory_conflict",
        dimension="contradiction_suppression",
        query_class="provider_conflict_with_canonical",
        expected_evidence_ids=("canon_project_current_001",),
        excluded_evidence_ids=("adv_project_conflict_001",),
        expected_authority="canonical",
        fixture_class="negative",
        required_receipt_fields=("provenance", "source_id", "confidence", "conflict_state", "authority"),
    ),
    GateABaselineCase(
        case_id="advisory_unrelated_context",
        dimension="advisory_authority",
        query_class="unrelated_provider_evidence",
        expected_evidence_ids=("adv_unrelated_001",),
        excluded_evidence_ids=(),
        expected_authority="advisory",
        fixture_class="positive",
        required_receipt_fields=("provenance", "source_id", "confidence", "conflict_state", "authority"),
    ),
    GateABaselineCase(
        case_id="deleted_memory_exclusion",
        dimension="deletion_exclusion",
        query_class="deleted_preference",
        expected_evidence_ids=(),
        excluded_evidence_ids=("canon_preference_deleted_001", "tomb_preference_001"),
        expected_authority="none",
        fixture_class="negative",
        required_receipt_fields=("tombstone_state", "redaction_state", "authority"),
    ),
    GateABaselineCase(
        case_id="restore_tombstone_exclusion",
        dimension="restore_safety",
        query_class="restored_row_with_current_tombstone",
        expected_evidence_ids=(),
        excluded_evidence_ids=("canon_restored_deleted_001", "tomb_restored_deleted_001"),
        expected_authority="none",
        fixture_class="negative",
        required_receipt_fields=("tombstone_state", "rebuild_state", "authority"),
    ),
    GateABaselineCase(
        case_id="rebuild_tombstone_filter",
        dimension="rebuild_safety",
        query_class="derived_index_rebuild",
        expected_evidence_ids=("canon_project_current_001",),
        excluded_evidence_ids=("canon_restored_deleted_001", "tomb_restored_deleted_001"),
        expected_authority="canonical",
        fixture_class="positive",
        required_receipt_fields=("tombstone_state", "rebuild_state", "provenance"),
    ),
    GateABaselineCase(
        case_id="provider_outage_canonical_continuity",
        dimension="provider_outage_continuity",
        query_class="provider_unavailable",
        expected_evidence_ids=("canon_goal_001",),
        excluded_evidence_ids=("adv_provider_only_001",),
        expected_authority="canonical",
        fixture_class="positive",
        required_receipt_fields=("provenance", "outage_state", "authority"),
    ),
    GateABaselineCase(
        case_id="malformed_provider_hit",
        dimension="malformed_input_rejection",
        query_class="malformed_advisory_record",
        expected_evidence_ids=(),
        excluded_evidence_ids=("adv_malformed_001",),
        expected_authority="none",
        fixture_class="negative",
        required_receipt_fields=("provenance", "trust_class", "authority"),
    ),
    GateABaselineCase(
        case_id="poisoned_observation",
        dimension="untrusted_observation_isolation",
        query_class="tool_or_ocr_instruction_injection",
        expected_evidence_ids=(),
        excluded_evidence_ids=("poison_instruction_001",),
        expected_authority="none",
        fixture_class="negative",
        required_receipt_fields=("provenance", "trust_class", "authority"),
    ),
    GateABaselineCase(
        case_id="unknown_vector_identity",
        dimension="unknown_identity_rejection",
        query_class="derived_hit_without_canonical_identity",
        expected_evidence_ids=(),
        excluded_evidence_ids=("unknown_vector_001",),
        expected_authority="none",
        fixture_class="negative",
        required_receipt_fields=("provenance", "authority", "rebuild_state"),
    ),
    GateABaselineCase(
        case_id="export_redaction",
        dimension="export_privacy",
        query_class="deleted_memory_export",
        expected_evidence_ids=(),
        excluded_evidence_ids=("canon_preference_deleted_001", "tomb_preference_001"),
        expected_authority="none",
        fixture_class="negative",
        required_receipt_fields=("tombstone_state", "redaction_state"),
    ),
)

_GATE_A_METRICS: tuple[GateAMetric, ...] = (
    GateAMetric(
        name="exact_evidence_recall",
        description="Expected canonical evidence is present for each positive recall case.",
        unit="ratio",
        direction="higher_is_better",
        threshold=1.0,
    ),
    GateAMetric(
        name="temporal_freshness",
        description="Stale or superseded evidence is excluded when current evidence exists.",
        unit="ratio",
        direction="higher_is_better",
        threshold=1.0,
    ),
    GateAMetric(
        name="semantic_recall",
        description="Relevant canonical evidence is recovered for an equivalent project or goal query.",
        unit="ratio",
        direction="higher_is_better",
        threshold=1.0,
    ),
    GateAMetric(
        name="contradiction_suppression",
        description="Contradictory canonical or advisory evidence is suppressed from the selected context.",
        unit="ratio",
        direction="higher_is_better",
        threshold=1.0,
    ),
    GateAMetric(
        name="provenance_coverage",
        description="Every recalled or suppressed case has the required provenance and authority receipt fields.",
        unit="ratio",
        direction="higher_is_better",
        threshold=1.0,
    ),
    GateAMetric(
        name="delete_exclusion",
        description="Deleted canonical identities and their tombstones remain absent from recall and export content.",
        unit="ratio",
        direction="higher_is_better",
        threshold=1.0,
    ),
    GateAMetric(
        name="restore_tombstone_exclusion",
        description="A stale restored row cannot re-enter a derived index while a current tombstone exists.",
        unit="ratio",
        direction="higher_is_better",
        threshold=1.0,
    ),
    GateAMetric(
        name="provider_outage_canonical_continuity",
        description="Canonical retrieval remains usable when advisory provider retrieval is unavailable.",
        unit="ratio",
        direction="higher_is_better",
        threshold=1.0,
    ),
)

# These literals are changed only when the frozen corpus or metric contract
# changes.  They are checked at runtime so an accidental edit cannot silently
# become a new baseline version.
GATE_A_BASELINE_CORPUS_SHA256 = (
    "24fd397636667b64ee52c76b53316dbe2e084d9c5206cd5f2a43f6893192a946"
)
GATE_A_BASELINE_METRIC_SHA256 = (
    "f056bd7d4b559a1f0a610043568450605cca84aeaa9058658ccd961931db99b0"
)


def gate_a_baseline_corpus() -> list[dict[str, Any]]:
    """Return a JSON-safe copy of the frozen content-free corpus."""

    return [case.as_payload() for case in _GATE_A_BASELINE_CASES]


def gate_a_baseline_metric_contract() -> list[dict[str, Any]]:
    """Return a JSON-safe copy of the predeclared metric thresholds."""

    return [metric.as_payload() for metric in _GATE_A_METRICS]


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def gate_a_baseline_corpus_sha256() -> str:
    return hashlib.sha256(_canonical_json(gate_a_baseline_corpus())).hexdigest()


def gate_a_baseline_metric_sha256() -> str:
    return hashlib.sha256(_canonical_json(gate_a_baseline_metric_contract())).hexdigest()


def _validate_metric_contract() -> list[str]:
    errors: list[str] = []
    if not _GATE_A_METRICS:
        return ["metric_contract_empty"]
    seen_names: set[str] = set()
    for metric in _GATE_A_METRICS:
        if not metric.name or metric.name in seen_names:
            errors.append(f"duplicate_or_missing_metric_name:{metric.name or 'unknown'}")
        seen_names.add(metric.name)
        if metric.unit != "ratio" or metric.direction != "higher_is_better":
            errors.append(f"unsupported_metric_shape:{metric.name}")
        if not math.isfinite(metric.threshold) or not 0.0 <= metric.threshold <= 1.0:
            errors.append(f"invalid_metric_threshold:{metric.name}")
    return errors


def _validate_corpus() -> list[str]:
    errors: list[str] = []
    seen_case_ids: set[str] = set()
    for case in _GATE_A_BASELINE_CASES:
        if not _CASE_ID_RE.fullmatch(case.case_id):
            errors.append(f"invalid_case_id:{case.case_id}")
        if case.case_id in seen_case_ids:
            errors.append(f"duplicate_case_id:{case.case_id}")
        seen_case_ids.add(case.case_id)
        if not case.dimension or not case.query_class:
            errors.append(f"missing_case_classification:{case.case_id}")
        if case.expected_authority not in _ALLOWED_AUTHORITIES:
            errors.append(f"invalid_authority:{case.case_id}")
        if case.fixture_class not in _ALLOWED_FIXTURE_CLASSES:
            errors.append(f"invalid_fixture_class:{case.case_id}")
        if not case.required_receipt_fields or not set(case.required_receipt_fields) <= _SAFE_RECEIPT_FIELDS:
            errors.append(f"invalid_receipt_fields:{case.case_id}")
        for evidence_id in (*case.expected_evidence_ids, *case.excluded_evidence_ids):
            if not _EVIDENCE_ID_RE.fullmatch(evidence_id):
                errors.append(f"invalid_evidence_id:{case.case_id}")
        if set(case.expected_evidence_ids) & set(case.excluded_evidence_ids):
            errors.append(f"expected_and_excluded_overlap:{case.case_id}")

    case_ids = {case.case_id for case in _GATE_A_BASELINE_CASES}
    missing_negative_cases = sorted(_REQUIRED_NEGATIVE_CASES - case_ids)
    errors.extend(f"missing_negative_case:{case_id}" for case_id in missing_negative_cases)
    if len(seen_case_ids) != len(_GATE_A_BASELINE_CASES):
        errors.append("case_count_mismatch")
    return errors


def _fixture_coverage() -> dict[str, float | int | str]:
    case_ids = {case.case_id for case in _GATE_A_BASELINE_CASES}
    required_dimensions = {metric.name for metric in _GATE_A_METRICS}
    covered = sum(
        1
        for metric_name in required_dimensions
        if any(
            case.dimension == _METRIC_DIMENSIONS.get(metric_name)
            for case in _GATE_A_BASELINE_CASES
        )
    )
    return {
        "case_schema_coverage": 1.0 if not _validate_corpus() else 0.0,
        "metric_dimension_coverage": covered / len(required_dimensions) if required_dimensions else 0.0,
        "negative_case_coverage": (
            len(_REQUIRED_NEGATIVE_CASES & case_ids) / len(_REQUIRED_NEGATIVE_CASES)
            if _REQUIRED_NEGATIVE_CASES
            else 0.0
        ),
        "safe_receipt_field_coverage": (
            sum(bool(set(case.required_receipt_fields) <= _SAFE_RECEIPT_FIELDS) for case in _GATE_A_BASELINE_CASES)
            / len(_GATE_A_BASELINE_CASES)
            if _GATE_A_BASELINE_CASES
            else 0.0
        ),
        "case_count": len(_GATE_A_BASELINE_CASES),
        "dimension_count": len({case.dimension for case in _GATE_A_BASELINE_CASES}),
    }


def _safe_observed_metrics(observed_metrics: Mapping[str, Any]) -> tuple[dict[str, float], list[str]]:
    if not isinstance(observed_metrics, Mapping):
        return {}, ["observed_metrics_not_mapping"]
    values: dict[str, float] = {}
    errors: list[str] = []
    metric_names = {metric.name for metric in _GATE_A_METRICS}
    unknown_names = sorted(
        (str(name) for name in set(observed_metrics) - metric_names),
        key=str,
    )
    errors.extend(f"unknown_observed_metric:{name}" for name in unknown_names)
    for metric in _GATE_A_METRICS:
        raw_value = observed_metrics.get(metric.name)
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            errors.append(f"missing_or_non_numeric_observed_metric:{metric.name}")
            continue
        value = float(raw_value)
        if not math.isfinite(value) or value < 0.0 or value > 1.0:
            errors.append(f"observed_metric_out_of_range:{metric.name}")
            continue
        values[metric.name] = value
    return values, errors


def _validate_measurement_receipt(
    measurement_receipt: GateAMeasurementReceipt | None,
) -> list[str]:
    """Require a safe, frozen-contract binding for supplied measurements."""

    if measurement_receipt is None:
        return ["measurement_receipt_required"]
    if not isinstance(measurement_receipt, GateAMeasurementReceipt):
        return ["measurement_receipt_type_invalid"]

    errors: list[str] = []
    if measurement_receipt.corpus_sha256 != GATE_A_BASELINE_CORPUS_SHA256:
        errors.append("measurement_corpus_hash_mismatch")
    if measurement_receipt.metric_contract_sha256 != GATE_A_BASELINE_METRIC_SHA256:
        errors.append("measurement_metric_contract_hash_mismatch")
    if not isinstance(measurement_receipt.runner_id, str) or not _RECEIPT_ID_RE.fullmatch(
        measurement_receipt.runner_id
    ):
        errors.append("measurement_runner_id_invalid")
    if not isinstance(measurement_receipt.execution_receipt_id, str) or not _RECEIPT_ID_RE.fullmatch(
        measurement_receipt.execution_receipt_id
    ):
        errors.append("measurement_execution_receipt_id_invalid")
    return errors


def build_gate_a_baseline_receipt(
    *,
    observed_metrics: Mapping[str, Any] | None = None,
    measurement_receipt: GateAMeasurementReceipt | None = None,
) -> dict[str, Any]:
    """Build the operator-readable Gate A artifact and optional measurement.

    With no observed metrics, ``status=degraded`` and
    ``measurement_status=blocked`` are intentional: the frozen contract is
    valid, but runtime measurements have not been supplied.  Callers may pass
    a complete ratio mapping in a later deterministic runner to obtain a
    ``pass`` or ``degraded`` measurement.  Supplied measurements must carry a
    typed receipt bound to both frozen hashes, a runner identity, and an
    execution receipt.  Malformed, incomplete, unbound, or drifted input is
    ``blocked`` and never coerced into a passing result.
    """

    corpus_errors = _validate_corpus()
    metric_errors = _validate_metric_contract()
    corpus_hash = gate_a_baseline_corpus_sha256()
    metric_hash = gate_a_baseline_metric_sha256()
    hash_matches = (
        corpus_hash == GATE_A_BASELINE_CORPUS_SHA256
        and GATE_A_BASELINE_METRIC_SHA256 != "__filled_after_metric_freeze__"
        and metric_hash == GATE_A_BASELINE_METRIC_SHA256
    )
    fixture_coverage = _fixture_coverage()
    fixture_status = "pass" if not corpus_errors and not metric_errors and hash_matches else "blocked"

    metric_payloads: list[dict[str, Any]] = []
    observed_values: dict[str, float] = {}
    observed_errors: list[str] = []
    measurement_binding_errors: list[str] = []
    measurement_status = "blocked"
    artifact_valid = fixture_status == "pass"
    if observed_metrics is None:
        observed_errors = ["runtime_measurement_not_supplied"]
    else:
        observed_values, observed_errors = _safe_observed_metrics(observed_metrics)
        if observed_errors:
            measurement_status = "blocked"
        elif not artifact_valid:
            measurement_binding_errors = _validate_measurement_receipt(measurement_receipt)
            measurement_status = "blocked"
        else:
            measurement_binding_errors = _validate_measurement_receipt(measurement_receipt)
            if measurement_binding_errors:
                measurement_status = "blocked"
            else:
                metric_results = [
                    observed_values[metric.name] >= metric.threshold
                    for metric in _GATE_A_METRICS
                ]
                measurement_status = "pass" if all(metric_results) else "degraded"

    measurement_blocked = bool(
        observed_errors or measurement_binding_errors or not artifact_valid
    )

    for metric in _GATE_A_METRICS:
        item = metric.as_payload()
        metric_dimension = _METRIC_DIMENSIONS.get(metric.name)
        item["fixture_coverage"] = (
            1.0
            if metric_dimension
            and any(case.dimension == metric_dimension for case in _GATE_A_BASELINE_CASES)
            else 0.0
        )
        if metric.name == "provenance_coverage":
            item["fixture_coverage"] = float(fixture_coverage["safe_receipt_field_coverage"])
        item["fixture_status"] = (
            "pass"
            if artifact_valid and item["fixture_coverage"] >= metric.threshold
            else "blocked"
        )
        item["observed_value"] = None if measurement_blocked else observed_values.get(metric.name)
        item["observed_status"] = (
            "not_run"
            if observed_metrics is None
            else "blocked"
            if measurement_blocked
            else "pass"
            if observed_values[metric.name] >= metric.threshold
            else "degraded"
        )
        metric_payloads.append(item)

    if fixture_status == "blocked":
        status = "blocked"
    elif observed_metrics is None:
        status = "degraded"
    else:
        status = measurement_status

    blocked_reasons = [*corpus_errors, *metric_errors]
    if not hash_matches:
        if corpus_hash != GATE_A_BASELINE_CORPUS_SHA256:
            blocked_reasons.append("frozen_corpus_hash_mismatch")
        if metric_hash != GATE_A_BASELINE_METRIC_SHA256:
            blocked_reasons.append("frozen_metric_contract_hash_mismatch")
    blocked_reasons.extend([*observed_errors, *measurement_binding_errors])
    measurement_binding_status = (
        "not_run"
        if observed_metrics is None
        else "blocked"
        if measurement_blocked
        else "verified"
    )
    return {
        "artifact": {
            "artifact_id": f"{GATE_A_BASELINE_VERSION}:{corpus_hash[:16]}",
            "artifact_kind": "versioned_content_free_memory_corpus",
            "corpus_version": GATE_A_BASELINE_VERSION,
            "metric_contract_version": GATE_A_BASELINE_METRIC_VERSION,
            "corpus_sha256": corpus_hash,
            "metric_contract_sha256": metric_hash,
            "case_count": len(_GATE_A_BASELINE_CASES),
            "dimension_count": len({case.dimension for case in _GATE_A_BASELINE_CASES}),
            "measurement_policy": "freeze_contract_before_runtime_or_provider_measurement",
            "canonical_record_schema": {
                "identity": ["memory_id", "evidence_id"],
                "authority": ["canonical_or_advisory", "source_trust", "scope"],
                "temporal": ["created_at", "updated_at", "freshness_state"],
                "reconciliation": ["conflict_state", "tombstone_state", "redaction_state"],
                "derived_index": ["canonical_identity_required", "rebuild_version"],
            },
        },
        "summary": {
            "status": status,
            "artifact_status": fixture_status,
            "measurement_status": measurement_status,
            "measurement_binding_status": measurement_binding_status,
            "operator_status": (
                "gate_a_baseline_ready_measurement_blocked"
                if observed_metrics is None and status == "degraded"
                else "gate_a_baseline_measurement_degraded"
                if status == "degraded"
                else "gate_a_baseline_passed"
                if status == "pass"
                else "gate_a_baseline_blocked"
            ),
            "claim_boundary": GATE_A_BASELINE_CLAIM_BOUNDARY,
        },
        "corpus": gate_a_baseline_corpus(),
        "metrics": metric_payloads,
        "measurement_receipt": (
            measurement_receipt.as_payload()
            if isinstance(measurement_receipt, GateAMeasurementReceipt)
            and observed_metrics is not None
            and not measurement_blocked
            else None
        ),
        "fixture_coverage": fixture_coverage,
        "blocked_reasons": blocked_reasons,
        "policy": {
            "canonical_authority": "guardian_canonical_memory",
            "advisory_provider_authority": "never_canonical",
            "provider_outage_policy": "canonical_retrieval_remains_usable_and_degraded_state_is_visible",
            "restore_policy": "current_tombstone_ledger_must_be_reconciled_before_recall_or_rebuild",
            "privacy_policy": "receipt_contains_handles_and_metrics_only_no_memory_content_or_secrets",
            "measurement_policy": "runtime_values_are_supplied_by_a_separate_deterministic_runner_after_freeze",
            "blocked_claims": list(GATE_A_BASELINE_BLOCKED_CLAIMS),
            "receipt_surfaces": [
                "/api/operator/memory-benchmark",
                "/api/memory/providers",
            ],
        },
        "safe_receipt": {
            "contains_memory_content": False,
            "contains_secret": False,
            "contains_private_path": False,
            "redaction": "case_handles_and_metric_values_only",
        },
    }
