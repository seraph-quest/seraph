"""Sole closed public-programme plan contract; data never grants authority."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from uuid import UUID
import json
import re

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator, field_validator
from src.work_board.contracts import TaskStrategyBinding

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Positive = Annotated[StrictInt, Field(ge=1)]
STAGES = (("plan_queries", "guardian.query-plan.v1", (("queries", "QueryPlan.v1"),)),
          ("search_public", "guardian.public-search.v1", (("manifest", "SearchManifest.v1"), ("selection", "SourceSelection.v1"))),
          ("extract_sources", "source.public-extract.v1", (("snapshots", "PublicSnapshots.v1"),)),
          ("prepare_brief", "guardian.prepare-brief.v1", (("brief", "DiscoveryBrief.v1"),)))


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactRef(Closed):
    artifact_id: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9:_-]+$")]
    digest: Digest
    schema_version: Literal[1]

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema version must be a strict integer")
        return value


class OutputRef(Closed):
    producer_step_id: Literal["plan_queries", "search_public", "extract_sources", "prepare_brief"]
    output_slot: Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[a-z_]+$")]
    json_pointer: Annotated[str, Field(max_length=256)]

    @field_validator("json_pointer")
    @classmethod
    def finite_pointer(cls, value):
        if value != "":
            raise ValueError("fixed research stages consume whole outputs; JSON pointers are unsupported")
        return value


class OutputSlot(Closed):
    slot: Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[a-z_]+$")]
    artifact_type: Annotated[str, Field(min_length=1, max_length=64)]
    max_bytes: Annotated[StrictInt, Field(ge=1, le=1048576)]


class ResearchStep(Closed):
    step_id: Literal["plan_queries", "search_public", "extract_sources", "prepare_brief"]
    capability_id: str
    capability_version: Literal[1]
    input_refs: Annotated[list[ArtifactRef | OutputRef], Field(min_length=1, max_length=8)]
    output_slots: Annotated[list[OutputSlot], Field(min_length=1, max_length=2)]

    @field_validator("capability_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError("capability version must be a strict integer")
        return value


class ResearchLimits(Closed):
    max_queries: Annotated[StrictInt, Field(ge=1, le=3)]
    max_results: Annotated[StrictInt, Field(ge=1, le=15)]
    max_sources: Annotated[StrictInt, Field(ge=1, le=4)]
    max_inference_requests: Annotated[StrictInt, Field(ge=0, le=4)]
    max_wall_seconds: Annotated[StrictInt, Field(ge=1, le=300)]
    max_search_seconds: Annotated[StrictInt, Field(ge=1, le=20)]
    max_search_bytes: Annotated[StrictInt, Field(ge=1, le=524288)]
    max_source_bytes: Annotated[StrictInt, Field(ge=1, le=262144)]
    max_output_bytes: Annotated[StrictInt, Field(ge=1, le=1048576)]
    cost_limit_microusd: Annotated[StrictInt, Field(ge=0)]


class GoalResearchPlanSpecV1(Closed):
    schema_version: Literal[1]
    plan_id: UUID
    programme_id: UUID
    programme_revision: Positive
    goal_id: Annotated[str, Field(min_length=1, max_length=128)]
    goal_revision: Positive
    grant_id: str
    grant_revision: Positive
    public_brief_digest: Digest
    route_epoch: Positive
    strategy_binding: TaskStrategyBinding
    issued_at: datetime
    deadline_at: datetime
    idempotency_key: UUID
    limits: ResearchLimits
    steps: Annotated[list[ResearchStep], Field(min_length=4, max_length=4)]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema version must be a strict integer")
        return value

    @field_validator("goal_id")
    @classmethod
    def canonical_goal_id(cls, value):
        from src.work_board.contracts import _safe_reference
        if _safe_reference(value, field_name="goal_id") != value:
            raise ValueError("canonical Goal ID must remain byte-exact")
        return value

    @field_validator("issued_at", "deadline_at")
    @classmethod
    def utc_timestamp(cls, value):
        if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
            raise ValueError("plan timestamps require UTC")
        return value

    @model_validator(mode="after")
    def fixed_contract(self):
        if UUID(self.grant_id).hex != self.programme_id.hex or self.programme_revision != self.grant_revision:
            raise ValueError("programme and grant are one immutable generation")
        if not 0 < (self.deadline_at - self.issued_at).total_seconds() <= self.limits.max_wall_seconds:
            raise ValueError("plan deadline exceeds its original fixed wall allowance")
        if self.strategy_binding.status == "blocked":
            raise ValueError("blocked strategy cannot create a research plan")
        available = {}
        graph = {"search_public": [("plan_queries", "queries")],
            "extract_sources": [("search_public", "manifest"), ("search_public", "selection")],
            "prepare_brief": [("extract_sources", "snapshots")]}
        for step, (identifier, capability, slots) in zip(self.steps, STAGES, strict=True):
            if step.step_id != identifier or step.capability_id != capability:
                raise ValueError("research stage order and capability are fixed")
            if [(slot.slot, slot.artifact_type) for slot in step.output_slots] != list(slots):
                raise ValueError("research stage output slots are fixed")
            if identifier == "plan_queries":
                if len(step.input_refs) != 1 or not isinstance(step.input_refs[0], ArtifactRef):
                    raise ValueError("query planning requires the sole original public brief artifact")
            elif (any(not isinstance(ref, OutputRef) for ref in step.input_refs)
                    or [(ref.producer_step_id, ref.output_slot) for ref in step.input_refs] != graph[identifier]):
                raise ValueError("research stage inputs must match the exact executable four-stage graph")
            for ref in step.input_refs:
                if isinstance(ref, OutputRef) and ref.output_slot not in available.get(ref.producer_step_id, set()):
                    raise ValueError("forward, cyclic or undeclared output reference")
            if identifier == "extract_sources" and {(r.producer_step_id, r.output_slot) for r in step.input_refs if isinstance(r, OutputRef)} != {("search_public", "manifest"), ("search_public", "selection")}:
                raise ValueError("extraction requires exact manifest and selection outputs")
            if any(slot.max_bytes > self.limits.max_output_bytes for slot in step.output_slots):
                raise ValueError("output slot widens the original output cap")
            available[identifier] = {slot.slot for slot in step.output_slots}
        return self


def validate_goal_research_plan(value, *, now: datetime | None = None) -> GoalResearchPlanSpecV1:
    plan = GoalResearchPlanSpecV1.model_validate(value)
    observed = now or datetime.now(timezone.utc)
    if observed.tzinfo is None or plan.deadline_at <= observed or plan.issued_at > observed:
        raise ValueError("research plan is expired or not issued yet")
    if len(json.dumps(plan.model_dump(mode="json"), separators=(",", ":")).encode()) > 65536:
        raise ValueError("research plan exceeds its finite schema byte cap")
    return plan


class SearchResult(Closed):
    result_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    exact_url: Annotated[str, Field(min_length=1, max_length=4096)]
    title: Annotated[str, Field(min_length=1, max_length=1024)]
    observed_at: datetime

    @field_validator("exact_url")
    @classmethod
    def public_url(cls, value):
        from src.security.http_transport import parse_public_https_url
        parse_public_https_url(value)
        return value

    @field_validator("observed_at")
    @classmethod
    def utc_time(cls, value):
        return GoalResearchPlanSpecV1.utc_timestamp(value)


class SearchManifestV1(Closed):
    run_id: UUID
    query_digest: Digest
    results: Annotated[list[SearchResult], Field(max_length=15)]

    @model_validator(mode="after")
    def unique_results(self):
        if len({r.result_id for r in self.results}) != len(self.results) or len({r.exact_url for r in self.results}) != len(self.results):
            raise ValueError("manifest results must be unique exact search results")
        return self


class SourceSelectionV1(Closed):
    run_id: UUID
    manifest_ref: ArtifactRef
    selected_result_ids: Annotated[list[Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]], Field(max_length=4)]

    @model_validator(mode="after")
    def unique_selection(self):
        if len(set(self.selected_result_ids)) != len(self.selected_result_ids):
            raise ValueError("selected result IDs must be unique")
        return self

    def validate_manifest(self, manifest: SearchManifestV1, reference: ArtifactRef):
        if self.run_id != manifest.run_id or self.manifest_ref != reference or not set(self.selected_result_ids) <= {r.result_id for r in manifest.results}:
            raise ValueError("selection is not bound to the exact physical search manifest")


class PublicSnapshotV1(Closed):
    result_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    url: Annotated[str, Field(min_length=1, max_length=4096)]
    digest: Digest
    lines: Annotated[list[str], Field(min_length=1, max_length=65536)]
    fetched_at: datetime
    mime: Literal["text/plain", "text/html"]

    @field_validator("fetched_at")
    @classmethod
    def utc_time(cls, value):
        return GoalResearchPlanSpecV1.utc_timestamp(value)

    @model_validator(mode="after")
    def normalized_digest(self):
        import hashlib
        raw = "\n".join(self.lines).encode("utf-8")
        if not 0 < len(raw) <= 65536 or hashlib.sha256(raw).hexdigest() != self.digest:
            raise ValueError("snapshot normalized bytes/digest exceed or differ from original allowance")
        from src.security.http_transport import parse_public_https_url
        parse_public_https_url(self.url)
        return self


class DiscoverySourceMetadata(Closed):
    url: str
    digest: Digest
    result_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    fetched_at: datetime

    @field_validator("url")
    @classmethod
    def public_url(cls, value):
        from src.security.http_transport import parse_public_https_url
        parse_public_https_url(value)
        return value


class DiscoveryOutcomeCheckpoint(Closed):
    state: Literal["findings", "quiet", "empty"]
    sources: Annotated[list[DiscoverySourceMetadata], Field(max_length=4)]
    coverage: Literal["complete", "partial", "unchanged", "search_empty", "selection_empty", "sources_unavailable", "unsupported"]
    freshness: Literal["current"]
    no_learning: Literal[True]
    artifact_ref: ArtifactRef

    @field_validator("no_learning", mode="before")
    @classmethod
    def literal_no_learning(cls, value):
        if value is not True:
            raise ValueError("explicit no-learning required")
        return value


class DiscoverySpanCoverage(Closed):
    snapshot_ref: ArtifactRef
    result_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    normalized_digest: Digest
    normalized_byte_count: Annotated[StrictInt, Field(ge=1, le=65536)]
    included_first_line: Annotated[StrictInt, Field(ge=0, le=65536)]
    included_last_line: Annotated[StrictInt, Field(ge=0, le=65536)]
    included_span_digest: Digest | None
    omitted_lines: Annotated[StrictInt, Field(ge=0, le=65536)]


class DiscoveryCoverage(Closed):
    original_public_brief_digest: Digest
    original_public_brief_byte_count: Annotated[StrictInt, Field(ge=1, le=8000)]
    public_brief_fully_represented: bool
    outcome_state: Literal["findings", "quiet", "empty"]
    status: Literal["complete", "partial", "unchanged", "search_empty", "selection_empty", "sources_unavailable", "unsupported"]
    sources: Annotated[list[DiscoverySourceMetadata], Field(max_length=4)]
    source_spans: Annotated[list[DiscoverySpanCoverage], Field(max_length=4)]
    unavailable: Annotated[list[dict[str, str]], Field(max_length=4)]
    native_verified: Literal[True]
    semantic_truth_verified: Literal[False]
    no_learning: Literal[True]


class DiscoveryNextStep(Closed):
    kind: Literal["local_checklist"]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    artifact_ref: ArtifactRef
    inert: Literal[True]
    requires_acceptance: Literal[True]


class DiscoveryBriefV1(Closed):
    findings: Annotated[list[dict], Field(max_length=8)]
    citations: Annotated[list[dict], Field(max_length=16)]
    uncertainties: Annotated[list[Annotated[str, Field(max_length=512)]], Field(max_length=16)]
    prepared_artifact_refs: Annotated[list[ArtifactRef], Field(max_length=4)]
    proposed_next_steps: Annotated[list[DiscoveryNextStep], Field(max_length=4)]
    coverage: DiscoveryCoverage
