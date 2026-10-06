"""Closed M2 contracts. Citations establish provenance, never authority."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

PositiveInt = Annotated[int, Field(strict=True, ge=1)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class OpportunityError(ValueError):
    def __init__(self, code: str, status_code: int = 409):
        self.code, self.status_code = code, status_code
        super().__init__(code)


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GuardianPolicy(Closed):
    schema_version: Literal["seraph.guardian.policy.v1"]
    assessment_enabled: StrictBool
    auto_stage_plan: StrictBool = False
    confirmed_at: datetime
    review_due_at: datetime
    grant_id: str = Field(min_length=1, max_length=160)
    original_root_id: str = Field(min_length=1, max_length=160)
    goal_revision: PositiveInt
    source_watch_ids: list[UUID] = Field(min_length=1, max_length=3)
    max_assessments_per_utc_day: Annotated[int, Field(strict=True, ge=1, le=4)]
    max_plan_proposals_per_utc_day: Annotated[int, Field(strict=True, ge=0, le=2)] = 0
    max_notification_per_utc_day: Annotated[int, Field(strict=True, ge=0, le=2)] = 0
    minimum_gap_seconds: Annotated[int, Field(strict=True, ge=1800)] = 1800

    @field_validator("confirmed_at", "review_due_at")
    @classmethod
    def utc_date(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("explicit UTC time required")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def finite_policy(self):
        if len(set(self.source_watch_ids)) != len(self.source_watch_ids):
            raise ValueError("duplicate source watches")
        if self.review_due_at <= self.confirmed_at:
            raise ValueError("review must follow confirmation")
        if self.auto_stage_plan and not self.max_plan_proposals_per_utc_day:
            raise ValueError("automatic staging needs a finite proposal cap")
        return self


class GuardianPolicySave(Closed):
    expected_goal_revision: PositiveInt
    expected_policy_revision: Annotated[int, Field(strict=True, ge=0)]
    policy: GuardianPolicy
    idempotency_key: UUID
    acknowledge_auto_stage_plan: StrictBool = False
    acknowledge_notifications: StrictBool = False


class VerifiedSourcePacket(Closed):
    schema_version: Literal["guardian.source_packet_verified.v1"] = "guardian.source_packet_verified.v1"
    packet_id: UUID
    watch_revision: PositiveInt
    goal_revision: PositiveInt


class EvidenceSource(Closed):
    source_key: str = Field(min_length=1, max_length=128)
    identity_digest: Digest
    target: str = Field(min_length=1, max_length=2048)
    new_hash: Digest
    excerpt: str
    excerpt_sha256: Digest


class OpportunityEvidence(Closed):
    schema_version: Literal["seraph.opportunity.evidence.v1"] = "seraph.opportunity.evidence.v1"
    packet_id: UUID
    checkpoint_sha256: Digest
    watch_revision: PositiveInt
    goal_revision: PositiveInt
    sources: list[EvidenceSource] = Field(min_length=1, max_length=2)


class Citation(Closed):
    source_id: str = Field(min_length=1, max_length=128)
    start_line: Annotated[int, Field(strict=True, ge=1, le=200)]
    end_line: Annotated[int, Field(strict=True, ge=1, le=200)]
    span_sha256: Digest


class OpportunityAssessment(Closed):
    schema_version: Literal["seraph.opportunity.assessment.v1"]
    relevance: Annotated[int, Field(strict=True, ge=0, le=4)]
    confidence: Literal["low", "medium", "high"]
    summary: str = Field(min_length=1, max_length=240)
    reason: str = Field(min_length=1, max_length=1000)
    citations: list[Citation] = Field(min_length=1, max_length=4)
    suggested_blueprint: Literal["public-evidence-report", "public-browser-check", "none"]
    abstain_reason: str | None = Field(max_length=240)

    @property
    def proposed(self):
        return (self.relevance >= 3 and self.confidence != "low"
                and self.suggested_blueprint != "none" and self.abstain_reason is None)


class OpportunityCancel(Closed):
    expected_opportunity_revision: PositiveInt
    idempotency_key: UUID


class OpportunityPlanRequest(Closed):
    expected_opportunity_revision: PositiveInt
    expected_goal_revision: PositiveInt
    idempotency_key: UUID


class OpportunityPlanResult(Closed):
    schema_version: Literal["seraph.opportunity.plan.v1"]
    blueprint_id: Literal["public-browser-check", "public-evidence-report"]
    title: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=1000)
    citations: list[Citation] = Field(min_length=1, max_length=4)


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: bytes):
    return hashlib.sha256(value).hexdigest()


def validate_assessment(raw: str, evidence: OpportunityEvidence):
    if len(raw.encode("utf-8")) > 16384:
        raise ValueError("assessment_size_limit")
    # Duplicate JSON keys are rejected rather than silently taking the last.
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("assessment_duplicate_key")
            result[key] = value
        return result
    model = OpportunityAssessment.model_validate(json.loads(raw, object_pairs_hook=unique_pairs))
    sources = {source.source_key: source for source in evidence.sources}
    for citation in model.citations:
        source = sources.get(citation.source_id)
        if source is None:
            raise ValueError("citation_source_mismatch")
        lines = source.excerpt.split("\n")
        if not citation.start_line <= citation.end_line <= len(lines):
            raise ValueError("citation_span_mismatch")
        span = "\n".join(lines[citation.start_line - 1:citation.end_line]).encode("utf-8")
        if digest(span) != citation.span_sha256:
            raise ValueError("citation_digest_mismatch")
    return model
