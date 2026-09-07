"""Typed contracts for the first goal-conditioned decision slice.

The contracts in this module are deliberately independent from the execution
runtime.  A candidate is a proposal, never a permission or a durable job.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class CriterionVerifierKind(str, Enum):
    """Supported verifier seams for a goal success criterion."""

    artifact_readback = "artifact_readback"
    external_readback = "external_readback"
    operator_attestation = "operator_attestation"


class GoalSuccessCriterion(BaseModel):
    """A bounded, inspectable condition that can establish goal progress."""

    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(
        default="criterion-1",
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("criterion_id", "id"),
    )
    description: str = Field(min_length=1, max_length=1_000)
    verifier_kind: CriterionVerifierKind | None = Field(
        default=None,
        validation_alias=AliasChoices("verifier_kind", "verifier"),
    )
    target: str | dict[str, Any] = ""
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("criterion_id", "description", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _normalize_evidence_refs(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple, set)):
            raise TypeError("evidence_refs must be a list of strings")
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            ref = str(item or "").strip()
            if ref and ref not in seen:
                seen.add(ref)
                result.append(ref)
        return result

    @property
    def verifier_configured(self) -> bool:
        return self.verifier_kind is not None


class GoalCandidateAction(str, Enum):
    """The bounded choices available to goal-conditioned planning."""

    act = "act"
    clarify = "clarify"
    defer = "defer"
    silent = "silent"


class GoalCandidateRequest(BaseModel):
    """Input for proposing a capability candidate for a goal."""

    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(min_length=1, max_length=160)
    capability_version: str = Field(default="1", min_length=1, max_length=80)
    inputs: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    reason: str = Field(default="", max_length=1_000)
    expected_outcome: str = Field(default="", max_length=1_000)
    expires_at: datetime | None = None

    @field_validator("capability_id", "capability_version", "reason", "expected_outcome", mode="before")
    @classmethod
    def _strip_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def _normalize_evidence_refs(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, (list, tuple, set)):
            raise TypeError("evidence_refs must be a list of strings")
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            ref = str(item or "").strip()
            if ref and ref not in seen:
                seen.add(ref)
                result.append(ref)
        return result


class GoalCandidateDecision(BaseModel):
    """An inspectable candidate decision tied to one goal revision."""

    model_config = ConfigDict(extra="forbid")

    receipt_version: Literal["goal_conditioned_loop_v1"] = "goal_conditioned_loop_v1"
    proposal_only: Literal[True] = True
    candidate_id: str
    dedupe_key: str
    goal_id: str
    goal_revision: int = Field(ge=1)
    criterion_id: str | None = None
    action: GoalCandidateAction
    reason: str
    evidence_refs: list[str] = Field(default_factory=list)
    capability_id: str | None = None
    capability_version: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_outcome: str = ""
    expires_at: datetime | None = None

    @property
    def dispatchable(self) -> bool:
        return self.action is GoalCandidateAction.act


class GoalExecutionResult(BaseModel):
    """Adapter result after governed execution and evidence readback."""

    model_config = ConfigDict(extra="forbid")

    execution_status: Literal["succeeded", "failed", "blocked"] = "succeeded"
    verification: Literal["passed", "failed", "unknown"] = "unknown"
    usefulness: Literal["helpful", "harmful", "ignored", "corrected", "unknown"] = "unknown"
    learning: Literal["applied", "proposed", "no_learning"] = "no_learning"
    learning_record_id: str | None = None
    artifact_ref: str | None = None
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    reason: str = Field(default="", max_length=1_000)


class GoalOutcomeReceipt(BaseModel):
    """Separate execution, verification, usefulness, and learning axes."""

    model_config = ConfigDict(extra="forbid")

    receipt_version: Literal["goal_conditioned_loop_v1"] = "goal_conditioned_loop_v1"
    receipt_type: Literal["outcome", "no_learning"]
    outcome_id: str
    candidate_id: str
    dedupe_key: str
    goal_id: str
    goal_revision: int = Field(ge=1)
    execution_status: Literal["succeeded", "failed", "blocked"]
    verification: Literal["passed", "failed", "unknown"]
    usefulness: Literal["helpful", "harmful", "ignored", "corrected", "unknown"]
    learning: Literal["applied", "proposed", "no_learning"]
    learning_record_id: str | None = None
    artifact_ref: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    reason: str = ""


def normalized_evidence_refs(*refs: str | None) -> tuple[str, ...]:
    """Return stable, duplicate-free evidence references."""

    values: set[str] = set()
    for raw in refs:
        if raw is None:
            continue
        if isinstance(raw, str):
            candidates = [raw]
        else:
            candidates = [str(raw)]
        for value in candidates:
            item = value.strip()
            if item:
                values.add(item)
    return tuple(sorted(values))


def stable_candidate_key(
    *,
    goal_id: str,
    goal_revision: int,
    criterion_id: str | None,
    capability_id: str | None,
    capability_version: str | None,
    evidence_refs: list[str] | tuple[str, ...],
    expected_outcome: str,
) -> str:
    """Build a deterministic key so unchanged evidence cannot re-admit work."""

    payload = {
        "goal_id": goal_id,
        "goal_revision": goal_revision,
        "criterion_id": criterion_id,
        "capability_id": capability_id,
        "capability_version": capability_version,
        "evidence_refs": sorted(set(evidence_refs)),
        "expected_outcome": expected_outcome.strip(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"gcl:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"
