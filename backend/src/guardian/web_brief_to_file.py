"""Bounded public-source ``web-brief-to-file`` goal execution.

This adapter deliberately reuses the goal snapshot durable execution seam.  It
only changes the typed workflow inputs, exact workflow binding, authority
scope, and readback predicate needed by the public-source brief journey.
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import Field, field_validator

from src.db.models import Goal
from src.goals.contracts import (
    GoalCandidateDecision,
    GoalCandidateRequest,
    GoalExecutionResult,
    GoalOutcomeReceipt,
    normalized_evidence_refs,
    stable_candidate_key,
)
from src.guardian.goal_conditioned_loop import build_goal_candidate_decision
from src.guardian.goal_snapshot_to_file import (
    CAPABILITY_VERSION,
    GoalSnapshotToFileAdapter,
    GoalSnapshotToFileRequest,
    GoalSnapshotToFileResult,
    GoalSnapshotToFileService,
    _Readback,
    _safe_digest,
)
from src.security.trust_contract import EgressClass


CAPABILITY_ID = "workflow.web-brief-to-file"
WORKFLOW_NAME = "web-brief-to-file"
WORKFLOW_TOOL_NAME = "workflow_web_brief_to_file"
ARTIFACT_TYPE = "web_brief"
AUTHORITY_SOURCE_ID = "source:web-brief-to-file"
ALLOWED_WORKFLOW_STEPS = ("web_search", "write_file")
CANONICAL_WORKFLOW_STEPS = (
    {
        "id": "search",
        "tool": "web_search",
        "arguments": {"query": "{{ query }}"},
    },
    {
        "id": "save",
        "tool": "write_file",
        "arguments": {
            "file_path": "{{ file_path }}",
            "content": 'Web brief for "{{ query }}"\n\n{{ steps.search.result }}\n',
        },
    },
)


class WebBriefToFileRequest(GoalSnapshotToFileRequest):
    """Typed query plus workspace output for one bounded public-source brief."""

    query: str = Field(min_length=1, max_length=500)

    @field_validator("query", mode="before")
    @classmethod
    def _strip_query(cls, value: Any) -> str:
        return str(value or "").strip()


class WebBriefToFileResult(GoalSnapshotToFileResult):
    """Outcome with public-source readback explicitly named for operators."""

    capability_id: str = CAPABILITY_ID
    capability_version: str = CAPABILITY_VERSION
    query: str
    query_read_back: bool = False
    source_read: bool = False


def _candidate_for_missing_goal(request: WebBriefToFileRequest) -> GoalCandidateDecision:
    expected = request.expected_outcome.strip() or f"Save a public-source brief for {request.query}"
    evidence_refs = list(normalized_evidence_refs(*request.evidence_refs))
    dedupe = stable_candidate_key(
        goal_id=request.goal_id,
        goal_revision=request.goal_revision,
        criterion_id=None,
        capability_id=CAPABILITY_ID,
        capability_version=request.capability_version,
        evidence_refs=evidence_refs,
        expected_outcome=expected,
        inputs={"query": request.query, "file_path": request.file_path},
    )
    return GoalCandidateDecision(
        candidate_id="cand_" + hashlib.sha256(dedupe.encode("utf-8")).hexdigest()[:24],
        dedupe_key=dedupe,
        goal_id=request.goal_id,
        goal_revision=request.goal_revision,
        criterion_id=None,
        action="act",
        reason=request.reason,
        evidence_refs=evidence_refs,
        capability_id=CAPABILITY_ID,
        capability_version=request.capability_version,
        inputs={"query": request.query, "file_path": request.file_path},
        expected_outcome=expected,
        expires_at=request.deadline_at,
    )


def _candidate_for_goal(goal: Goal, request: WebBriefToFileRequest) -> GoalCandidateDecision:
    decision = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id=CAPABILITY_ID,
            capability_version=request.capability_version,
            inputs={"query": request.query, "file_path": request.file_path},
            evidence_refs=request.evidence_refs,
            reason=request.reason,
            expected_outcome=request.expected_outcome.strip() or f"Save a public-source brief for {request.query}",
            expires_at=request.deadline_at,
        ),
    )
    if decision.goal_revision == request.goal_revision:
        return decision
    evidence_refs = list(decision.evidence_refs)
    dedupe = stable_candidate_key(
        goal_id=goal.id,
        goal_revision=request.goal_revision,
        criterion_id=decision.criterion_id,
        capability_id=decision.capability_id,
        capability_version=decision.capability_version,
        evidence_refs=evidence_refs,
        expected_outcome=decision.expected_outcome,
        inputs=decision.inputs,
    )
    return decision.model_copy(
        update={
            "candidate_id": "cand_" + hashlib.sha256(dedupe.encode("utf-8")).hexdigest()[:24],
            "dedupe_key": dedupe,
            "goal_revision": request.goal_revision,
        }
    )


class WebBriefToFileAdapter(GoalSnapshotToFileAdapter):
    """Run the registered public-source workflow through the snapshot seam."""

    request: WebBriefToFileRequest

    def _capability_identifier(self) -> str:
        return CAPABILITY_ID

    def _workflow_identifier(self) -> str:
        return WORKFLOW_NAME

    def _workflow_tool_identifier(self) -> str:
        return WORKFLOW_TOOL_NAME

    def _allowed_workflow_steps(self) -> tuple[str, ...]:
        return ALLOWED_WORKFLOW_STEPS

    def _canonical_workflow_steps(self) -> tuple[dict[str, Any], ...]:
        return CANONICAL_WORKFLOW_STEPS

    def _authority_source_identifier(self) -> str:
        return AUTHORITY_SOURCE_ID

    def _authority_egress_class(self) -> EgressClass:
        return EgressClass.CLOUD_ALLOWED_REDACTED

    def _authority_operations(self) -> tuple[str, ...]:
        return ("read_public_source", "write_file")

    def _authority_network_hosts(self) -> tuple[str, ...]:
        return ("public-web",)

    def _resource_claims(self) -> tuple[str, ...]:
        return ("public_source_read", "workspace_write")

    def _artifact_type(self) -> str:
        return ARTIFACT_TYPE

    def _workflow_inputs(self, path: str) -> dict[str, Any]:
        return {"query": self.request.query, "file_path": path}

    def _job_identifier(self, candidate: GoalCandidateDecision) -> str:
        return "job_web_brief_" + _safe_digest(
            {
                "candidate": candidate.dedupe_key,
                "owner": self.request.owner_principal_id,
                "service": self.request.service_id,
                "origin": "scheduler" if self.request.parent_job_id else "operator",
            }
        )[:24]

    def _idempotency_scope(self) -> str:
        return "web-brief-to-file-scheduler" if self.request.parent_job_id else "web-brief-to-file"

    def _success_reason(self) -> str:
        return "web_brief_workflow_executed_and_source_readback_verified"

    def _readback(self, path: str, _goal_id: str) -> _Readback:
        readback = super()._readback(path, self.request.query)
        if not readback.goal_id_read_back or readback.content is None:
            return readback
        text = readback.content.decode("utf-8", errors="replace")
        if "No results found" in text or "No allowed results found" in text or "Search error:" in text:
            return _Readback(
                readback.output_exists,
                readback.workspace_contained,
                False,
                readback.content_sha256,
                readback.content,
                "public_source_read_failed",
            )
        if "URL: http" not in text:
            return _Readback(
                readback.output_exists,
                readback.workspace_contained,
                False,
                readback.content_sha256,
                readback.content,
                "public_source_url_missing",
            )
        return readback

    def _extra_evidence_refs(self, readback: _Readback) -> tuple[str, ...]:
        if readback.content_sha256 and readback.goal_id_read_back:
            return (f"source-readback:{readback.content_sha256}",)
        return ()


class WebBriefToFileService(GoalSnapshotToFileService):
    """Proposal, governed execution, artifact readback, and outcome receipt."""

    request_model = WebBriefToFileRequest
    adapter_type = WebBriefToFileAdapter
    result_type = WebBriefToFileResult

    def _candidate_for_request(
        self,
        goal: Goal | None,
        request: WebBriefToFileRequest,
    ) -> GoalCandidateDecision:
        return _candidate_for_missing_goal(request) if goal is None else _candidate_for_goal(goal, request)

    def _result_for_outcome(
        self,
        *,
        request: WebBriefToFileRequest,
        candidate: GoalCandidateDecision,
        outcome: GoalOutcomeReceipt,
        receipt: dict[str, Any],
    ) -> WebBriefToFileResult:
        return WebBriefToFileResult(
            job_id=str(receipt.get("job_id") or "") or None,
            durable_status=str(receipt.get("durable_status") or "") or None,
            goal_id=candidate.goal_id,
            goal_revision=candidate.goal_revision,
            file_path=request.file_path,
            execution_status=outcome.execution_status,
            verification=outcome.verification,
            learning=outcome.learning,
            artifact_ref=outcome.artifact_ref,
            content_sha256=str(receipt.get("content_sha256") or "") or None,
            output_exists=bool(receipt.get("output_exists", False)),
            workspace_contained=bool(receipt.get("workspace_contained", False)),
            goal_id_read_back=bool(receipt.get("goal_id_read_back", False)),
            query=request.query,
            query_read_back=bool(receipt.get("goal_id_read_back", False)),
            source_read=bool(receipt.get("goal_id_read_back", False)),
            reconciliation_required=bool(receipt.get("reconciliation_required", False)),
            recovery_action=str(receipt.get("recovery_action") or "") or None,
            durable_failure=(
                dict(receipt["durable_failure"])
                if isinstance(receipt.get("durable_failure"), dict)
                else None
            ),
            authority_receipt=(
                dict(receipt["authority_receipt"])
                if isinstance(receipt.get("authority_receipt"), dict)
                else None
            ),
            evidence_refs=list(outcome.evidence_refs),
            reason=outcome.reason,
        )


async def run_web_brief_to_file(
    request: WebBriefToFileRequest | dict[str, Any],
    **kwargs: Any,
) -> WebBriefToFileResult:
    return await WebBriefToFileService(**kwargs).run(request)


__all__ = [
    "CAPABILITY_ID",
    "CAPABILITY_VERSION",
    "WORKFLOW_NAME",
    "WORKFLOW_TOOL_NAME",
    "WebBriefToFileRequest",
    "WebBriefToFileResult",
    "WebBriefToFileAdapter",
    "WebBriefToFileService",
    "run_web_brief_to_file",
]
