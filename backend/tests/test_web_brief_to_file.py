"""Focused proof for the governed public-source goal journey."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any

import pytest

from config.settings import settings
from src.db.models import Goal
from src.goals.contracts import GoalSuccessCriterion
from src.goals.repository import serialize_success_criterion
from src.guardian import goal_conditioned_loop
from src.guardian.web_brief_to_file import (
    CAPABILITY_ID,
    WebBriefToFileRequest,
    WebBriefToFileService,
)
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal


class _Goals:
    def __init__(self, goal: Goal):
        self.goal = goal

    async def get(self, _goal_id: str) -> Goal:
        return self.goal


class _Jobs:
    def __init__(self):
        self.jobs: dict[str, dict[str, Any]] = {}
        self.specs: dict[str, Any] = {}
        self.effects: list[dict[str, Any]] = []

    def _projection(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        return {
            "job_id": job_id,
            "status": job["status"],
            "failure_reason": job.get("failure_reason"),
            "lease": {
                "owner": job.get("owner"),
                "fencing_token": job.get("fencing_token", 0),
            },
            "artifacts": list(job.get("artifacts", [])),
            "effects": list(job.get("effects", [])),
            "receipt": {"status": "recorded"},
        }

    async def admit_job(self, spec):
        self.jobs[spec.identity.job_id] = {"status": "accepted", "artifacts": [], "effects": []}
        self.specs[spec.identity.job_id] = spec
        return {**self._projection(spec.identity.job_id), "receipt": {"status": "accepted"}}

    async def queue_job(self, job_id: str, **_kwargs):
        self.jobs[job_id]["status"] = "queued"
        return self._projection(job_id)

    async def claim_job(self, job_id: str, *, owner: str, lease_seconds: int):
        self.jobs[job_id].update({"status": "running", "owner": owner, "fencing_token": 1})
        return self._projection(job_id)

    async def transition_job(self, job_id: str, status: str, **kwargs):
        self.jobs[job_id]["status"] = status
        if status in {"failed", "blocked"}:
            self.jobs[job_id]["failure_reason"] = kwargs.get("reason")
        return self._projection(job_id)

    async def record_artifact(self, job_id: str, *, file_path: str, content: bytes, **_kwargs):
        record = {
            "artifact_id": "art_web_brief",
            "artifact_type": "web_brief",
            "file_path": file_path,
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "exists": True,
        }
        self.jobs[job_id]["artifacts"].append(record)
        return {**self._projection(job_id), "receipt": record}

    async def record_effect(self, job_id: str, **kwargs):
        self.effects.append({"job_id": job_id, **kwargs})
        self.jobs[job_id]["effects"].append(kwargs)
        return self._projection(job_id)

    async def record_readback(self, job_id: str, **kwargs):
        self.jobs[job_id]["effects"].append(kwargs)
        return self._projection(job_id)


class _BriefWorkflow:
    name = "workflow_web_brief_to_file"

    def __init__(self, root: Path, *, source_available: bool = True):
        self.root = root
        self.source_available = source_available
        self.calls: list[dict[str, str]] = []

    def get_approval_context(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "workflow_name": "web-brief-to-file",
            "risk_level": "medium",
            "execution_boundaries": ["external_read", "workspace_write"],
            "step_tools": ["web_search", "write_file"],
        }

    def __call__(self, *, query: str, file_path: str, sanitize_inputs_outputs: bool = False) -> str:
        self.calls.append({"query": query, "file_path": file_path})
        target = self.root / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.source_available:
            content = (
                f'Web brief for "{query}"\n\n'
                "1. Public source\n"
                "   URL: https://example.com/source\n"
                "   Source summary\n"
            )
        else:
            content = f'Web brief for "{query}"\n\nNo allowed results found for: {query}\n'
        target.write_text(content, encoding="utf-8")
        return f'Saved a web brief for "{query}" to {file_path}.'

    def get_audit_result_payload(self, _arguments: dict[str, Any], _result: Any):
        return "brief executed", {"durable_run_identity": "workflow-brief-test"}


def _goal() -> Goal:
    return Goal(
        id="goal-brief",
        title="Track a public research topic",
        status="active",
        revision=2,
        success_criterion_json=serialize_success_criterion(
            GoalSuccessCriterion(
                criterion_id="brief-present",
                description="A source-backed brief is readable",
                verifier_kind="artifact_readback",
                evidence_refs=["operator:source-consent"],
                target={"query": "Seraph project", "file_path": "briefs/goal-brief.md"},
            )
        ),
    )


def _request(**updates: Any) -> WebBriefToFileRequest:
    values = {
        "goal_id": "goal-brief",
        "goal_revision": 2,
        "query": "Seraph project",
        "file_path": "briefs/goal-brief.md",
        "owner_principal_id": "service:web-brief",
        "service_id": "service:web-brief",
        "session_id": "brief-session",
        "evidence_refs": ["operator:source-consent"],
        "deadline_at": datetime.now(timezone.utc) + timedelta(seconds=60),
    }
    values.update(updates)
    return WebBriefToFileRequest.model_validate(values)


def _principal() -> TrustPrincipal:
    return TrustPrincipal(
        principal_id="service:web-brief",
        principal_type=PrincipalType.SERVICE,
        authenticated=True,
        grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
        session_id="brief-session",
    )


@pytest.mark.asyncio
async def test_web_brief_executes_public_source_workflow_and_reads_back_artifact(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goal = _goal()
    goals = _Goals(goal)
    jobs = _Jobs()
    workflow = _BriefWorkflow(tmp_path)

    async def no_existing(**_kwargs):
        return None

    async def persist(**kwargs):
        return kwargs

    monkeypatch.setattr(goal_conditioned_loop, "goal_repository", goals)
    monkeypatch.setattr(goal_conditioned_loop, "_existing_receipt", no_existing)
    monkeypatch.setattr(goal_conditioned_loop, "_persist_receipt", persist)

    result = await WebBriefToFileService(
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
        authority_principal=_principal(),
    ).run(_request())

    assert result.capability_id == CAPABILITY_ID
    assert result.execution_status == "succeeded"
    assert result.verification == "passed"
    assert result.source_read is True
    assert result.query_read_back is True
    assert result.artifact_ref == "art_web_brief"
    assert result.decision_input_digest
    assert result.strategy_delta_id is None
    assert result.strategy_delta_provenance == "not_present"
    assert result.authority_receipt is not None
    assert result.authority_receipt["allowed"] is True
    assert result.authority_receipt["scope"]["egress_class"] == "cloud_allowed_redacted"
    assert workflow.calls == [{"query": "Seraph project", "file_path": "briefs/goal-brief.md"}]
    assert jobs.specs[result.job_id].identity.job_kind == CAPABILITY_ID
    assert jobs.specs[result.job_id].identity.idempotency_scope == "web-brief-to-file"


@pytest.mark.asyncio
async def test_web_brief_with_no_allowed_source_fails_verification_and_does_not_learn(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    goal = _goal()
    goals = _Goals(goal)
    jobs = _Jobs()
    workflow = _BriefWorkflow(tmp_path, source_available=False)

    async def no_existing(**_kwargs):
        return None

    async def persist(**kwargs):
        return kwargs

    monkeypatch.setattr(goal_conditioned_loop, "goal_repository", goals)
    monkeypatch.setattr(goal_conditioned_loop, "_existing_receipt", no_existing)
    monkeypatch.setattr(goal_conditioned_loop, "_persist_receipt", persist)

    result = await WebBriefToFileService(
        goals=goals,
        jobs=jobs,
        workflow_tool_provider=lambda _name: workflow,
        authority_principal=_principal(),
    ).run(_request())

    assert result.execution_status == "failed"
    assert result.verification == "failed"
    assert result.source_read is False
    assert result.learning == "no_learning"
