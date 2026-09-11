"""A bounded, file-backed local journey for the #745 goal loop.

The workflow tool is an injected deterministic capability boundary.  The goal
repository, goal-loop receipts, durable child jobs, artifact/readback checks,
authority gate, and strategy-delta storage are the real implementations.
The brief tool reads only from the local HTTP server created by this test.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, ExitStack
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from unittest.mock import patch

from config.settings import settings
from src.auth.service import test_bypass_operator as _test_bypass_operator
from src.approval.runtime import reset_runtime_context, set_runtime_context
from src.db.engine import _ensure_search_indexes
from src.goals.contracts import CriterionVerifierKind, GoalAdmissionBudget, GoalSuccessCriterion
from src.goals.repository import goal_repository
from src.extensions.source_operations import collect_source_evidence_bundle
from src.scheduler.jobs import strategist_tick
from src.api.goals import (
    GoalStrategyCorrection,
    GoalStrategyRollback,
    apply_goal_strategy_correction,
    rollback_goal_strategy_correction,
)
from src.workflows.job_runtime import durable_job_repository
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal


_SESSION_PATCH_TARGETS = (
    "src.db.engine.get_session",
    "src.goals.repository.get_session",
    "src.audit.repository.get_session",
    "src.memory.control.get_session",
    "src.workflows.job_runtime.get_session",
)


async def _open_database(path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}",
        connect_args={"check_same_thread": False},
        pool_size=5,
        max_overflow=0,
        pool_timeout=5,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
        await _ensure_search_indexes(connection)

    @asynccontextmanager
    async def _get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return engine, _get_session


def _patch_database(stack: ExitStack, get_session):
    for target in _SESSION_PATCH_TARGETS:
        stack.enter_context(patch(target, get_session))


class _LocalSourceHandler(BaseHTTPRequestHandler):
    requests: list[str] = []

    def do_GET(self):  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query).get("q", [""])[0]
        type(self).requests.append(query)
        payload = json.dumps(
            {
                "title": "Seraph controlled local source",
                "query": query,
                "body": "A deterministic source record for the local goal journey.",
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


class _LocalWorkflow:
    """Deterministic governed workflow tool used at the existing tool seam."""

    def __init__(self, root: Path, *, goal_id: str, kind: str, source_url: str = ""):
        self.root = root
        self.goal_id = goal_id
        self.kind = kind
        self.source_url = source_url
        self.calls = 0
        self.transport_calls: list[str] = []

    @property
    def name(self) -> str:
        return (
            "workflow_goal_snapshot_to_file"
            if self.kind == "snapshot"
            else "workflow_web_brief_to_file"
        )

    def get_approval_context(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "workflow_name": "goal-snapshot-to-file"
            if self.kind == "snapshot"
            else "web-brief-to-file",
            "risk_level": "medium",
            "execution_boundaries": ["workspace_write"]
            if self.kind == "snapshot"
            else ["external_read", "workspace_write"],
            "step_tools": ["get_goals", "write_file"]
            if self.kind == "snapshot"
            else ["web_search", "write_file"],
        }

    def __call__(
        self,
        *,
        file_path: str,
        query: str = "",
        goal_id: str | None = None,
        sanitize_inputs_outputs: bool = False,
        **_kwargs: Any,
    ):
        del sanitize_inputs_outputs
        if goal_id is not None:
            assert goal_id == self.goal_id
        self.calls += 1
        target = self.root / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.kind == "snapshot":
            content = (
                "Goal snapshot\n"
                f"Goal id: {self.goal_id}\n"
                "Status: active\n"
            )
        else:
            source_request_url = f"{self.source_url}?q={quote(query)}"
            bundle = collect_source_evidence_bundle(
                contract="webpage.read",
                source="browse_webpage",
                url=source_request_url,
                transport=self._transport,
                test_destination_grant=f"goal-local-source:{source_request_url}",
            )
            assert bundle["status"] == "ok", bundle
            source = json.loads(bundle["items"][0]["content"])
            content = (
                f'Web brief for "{query}"\n'
                f"Goal id: {self.goal_id}\n"
                f"URL: {source_request_url}\n"
                f"Source: {source['body']}\n"
            )
        target.write_text(content, encoding="utf-8")
        return f"Saved {file_path}"

    def _transport(self, url: str) -> str:
        self.transport_calls.append(url)
        with urlopen(url, timeout=2) as response:
            return response.read().decode("utf-8")

    def get_audit_result_payload(self, _arguments: dict[str, Any], _result: Any):
        return "local workflow executed", {"durable_run_identity": f"local-{self.kind}-run"}


def _request_state():
    return SimpleNamespace(state=SimpleNamespace(operator=_test_bypass_operator()))


async def _new_parent(observed_at: datetime):
    claimed = await strategist_tick._admit_and_claim_tick(observed_at=observed_at)
    assert claimed is not None
    return claimed


def _criterion(*, description: str, target: dict[str, Any] | str = ""):
    return GoalSuccessCriterion(
        criterion_id=description.lower().replace(" ", "-")[:48],
        description=description,
        verifier_kind=CriterionVerifierKind.artifact_readback,
        target=target,
        evidence_refs=["operator:local-test-consent"],
    )


def _journey_budget() -> GoalAdmissionBudget:
    now = datetime.now(timezone.utc)
    return GoalAdmissionBudget(
        reviewed_grant=True,
        grant_id="test-local-goal-grant",
        max_outstanding_jobs=1,
        max_attempts=1,
        max_runtime_seconds=120,
        notifications_per_day=1,
        period_started_at=now - timedelta(minutes=1),
        period_expires_at=now + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_file_backed_goal_journey_survives_restart_and_reversible_correction(
    monkeypatch,
    tmp_path,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database_path = workspace / "seraph.db"
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", True)

    source_handler = type(
        "JourneySourceHandler",
        (_LocalSourceHandler,),
        {"requests": []},
    )
    source_server = ThreadingHTTPServer(("127.0.0.1", 0), source_handler)
    source_thread = threading.Thread(target=source_server.serve_forever, daemon=True)
    source_thread.start()
    source_url = f"http://127.0.0.1:{source_server.server_port}/brief"

    stack = ExitStack()
    engine, get_session = await _open_database(database_path)
    _patch_database(stack, get_session)
    try:
        snapshot = await goal_repository.create(
            "Keep the local goal snapshot current",
            success_criterion=_criterion(description="A readable snapshot exists"),
            proactive_enabled=True,
            owner_principal_id="operator:test-bypass",
            owner_session_id="test-auth-bypass",
            admission_budget=_journey_budget(),
        )
        brief = await goal_repository.create(
            "Produce the controlled source brief",
            success_criterion=_criterion(
                description="A source-backed brief exists",
                target={
                    "query": "baseline source",
                    "file_path": "briefs/baseline.md",
                    "priority": 40,
                },
            ),
            proactive_enabled=True,
            owner_principal_id="operator:test-bypass",
            owner_session_id="test-auth-bypass",
            admission_budget=_journey_budget(),
        )

        snapshot_tool = _LocalWorkflow(workspace, goal_id=snapshot.id, kind="snapshot")
        brief_tool = _LocalWorkflow(
            workspace,
            goal_id=brief.id,
            kind="brief",
            source_url=source_url,
        )
        denied_url = f"{source_url}?q=denied"
        runtime_tokens = set_runtime_context(
            "web-brief:source-policy-test",
            "high_risk",
            trust_principal=TrustPrincipal(
                principal_id="service:web-brief",
                principal_type=PrincipalType.SERVICE,
                authenticated=True,
                revoked=False,
                grants=(AuthorityGrant.CAPABILITY_EXECUTE,),
                session_id="web-brief:source-policy-test",
            ),
        )
        try:
            denied_bundle = collect_source_evidence_bundle(
                contract="webpage.read",
                source="browse_webpage",
                url=denied_url,
                transport=brief_tool._transport,
                test_destination_grant=f"goal-local-source:{source_url}?q=other",
            )
        finally:
            reset_runtime_context(runtime_tokens)
        assert denied_bundle["status"] == "failed"
        assert any("destination denied" in warning for warning in denied_bundle["warnings"])
        assert brief_tool.transport_calls == []
        original_snapshot_service = strategist_tick.GoalSnapshotToFileService
        original_brief_service = strategist_tick.WebBriefToFileService

        def snapshot_service(**kwargs):
            return original_snapshot_service(
                workflow_tool_provider=lambda _name: snapshot_tool,
                **kwargs,
            )

        def brief_service(**kwargs):
            return original_brief_service(
                workflow_tool_provider=lambda _name: brief_tool,
                **kwargs,
            )

        base_time = datetime.now(timezone.utc) + timedelta(hours=1)
        snapshot_parent, snapshot_fence = await _new_parent(base_time)
        with patch.object(strategist_tick, "GoalSnapshotToFileService", snapshot_service):
            snapshot_first = await strategist_tick._run_opted_in_goal_snapshot(
                parent_job_id=snapshot_parent,
                parent_fencing_token=snapshot_fence,
            )
            snapshot_replay = await strategist_tick._run_opted_in_goal_snapshot(
                parent_job_id=snapshot_parent,
                parent_fencing_token=snapshot_fence,
            )
        assert snapshot_first["status"] == snapshot_replay["status"] == "succeeded"
        assert snapshot_first["job_id"] == snapshot_replay["job_id"]
        assert snapshot_first["artifact_ref"]
        assert snapshot_tool.calls == 1
        snapshot_path = workspace / "goal-snapshots" / f"{snapshot.id}.md"
        assert snapshot_path.read_text(encoding="utf-8").find(snapshot.id) >= 0

        brief_parent, brief_fence = await _new_parent(base_time + timedelta(hours=1))
        with patch.object(strategist_tick, "WebBriefToFileService", brief_service):
            brief_first = await strategist_tick._run_opted_in_goal_web_brief(
                parent_job_id=brief_parent,
                parent_fencing_token=brief_fence,
            )
            brief_replay = await strategist_tick._run_opted_in_goal_web_brief(
                parent_job_id=brief_parent,
                parent_fencing_token=brief_fence,
            )
        assert brief_first["status"] == brief_replay["status"] == "succeeded"
        assert brief_first["job_id"] == brief_replay["job_id"]
        assert brief_first["artifact_ref"]
        assert snapshot_first["job_id"] != brief_first["job_id"]
        assert snapshot_first["artifact_ref"] != brief_first["artifact_ref"]
        assert brief_tool.calls == 1
        assert source_handler.requests == ["baseline source"]
        baseline_path = workspace / "briefs" / "baseline.md"
        baseline_text = baseline_path.read_text(encoding="utf-8")
        assert f"URL: {source_url}" in baseline_text
        assert brief.id in baseline_text

        snapshot_job = await durable_job_repository.get_job(snapshot_first["job_id"])
        brief_job = await durable_job_repository.get_job(brief_first["job_id"])
        assert snapshot_job["status"] == brief_job["status"] == "succeeded"
        assert snapshot_job["goal_id"] == snapshot.id
        assert brief_job["goal_id"] == brief.id
        assert snapshot_job["goal_revision"] == brief_job["goal_revision"] == 1
        snapshot_readbacks = [
            effect for effect in snapshot_job["effects"] if effect.get("receipt_kind") == "readback"
        ]
        brief_readbacks = [
            effect for effect in brief_job["effects"] if effect.get("receipt_kind") == "readback"
        ]
        assert len(snapshot_readbacks) == len(brief_readbacks) == 1
        assert snapshot_readbacks[0]["effect_id"] != brief_readbacks[0]["effect_id"]
        assert snapshot_job["artifacts"] and brief_job["artifacts"]

        # Re-open the same on-disk database as a new worker.  Durable job
        # identity and audit receipts must make the same calls replay-safe.
        stack.close()
        await engine.dispose()
        engine, get_session = await _open_database(database_path)
        stack = ExitStack()
        _patch_database(stack, get_session)
        with patch.object(strategist_tick, "GoalSnapshotToFileService", snapshot_service):
            snapshot_after_restart = await strategist_tick._run_opted_in_goal_snapshot(
                parent_job_id=snapshot_parent,
                parent_fencing_token=snapshot_fence,
            )
        with patch.object(strategist_tick, "WebBriefToFileService", brief_service):
            brief_after_restart = await strategist_tick._run_opted_in_goal_web_brief(
                parent_job_id=brief_parent,
                parent_fencing_token=brief_fence,
            )
        assert snapshot_after_restart["job_id"] == snapshot_first["job_id"]
        assert brief_after_restart["job_id"] == brief_first["job_id"]
        for replay in (snapshot_after_restart, brief_after_restart):
            assert replay["content_sha256"]
            assert replay["output_exists"] is True
            assert replay["workspace_contained"] is True
            assert replay["goal_id_read_back"] is True
            assert replay["artifact_ref"]
            assert replay["evidence_refs"]
        assert snapshot_tool.calls == brief_tool.calls == 1

        correction = await apply_goal_strategy_correction(
            brief.id,
            GoalStrategyCorrection(
                correction_id="local-journey-correction",
                expected_revision=1,
                query="corrected source",
                file_path="briefs/corrected.md",
                priority=80,
                reason="Use the explicitly corrected local source.",
            ),
            _request_state(),
        )
        assert correction["status"] == "applied"
        delta_id = correction["delta"]["delta_id"]
        corrected_parent, corrected_fence = await _new_parent(base_time + timedelta(hours=2))
        with patch.object(strategist_tick, "WebBriefToFileService", brief_service):
            corrected = await strategist_tick._run_opted_in_goal_web_brief(
                parent_job_id=corrected_parent,
                parent_fencing_token=corrected_fence,
            )
        assert corrected["status"] == "succeeded"
        assert corrected["job_id"] != brief_first["job_id"]
        assert corrected["strategy_delta_id"] == delta_id
        assert corrected["strategy_delta_provenance"] == "verified"
        assert brief_tool.calls == 2
        assert source_handler.requests[-1] == "corrected source"
        assert (workspace / "briefs" / "corrected.md").read_text(encoding="utf-8").find(
            "corrected source"
        ) >= 0

        rollback = await rollback_goal_strategy_correction(
            brief.id,
            delta_id,
            GoalStrategyRollback(
                expected_revision=2,
                reason="Restore the baseline source choice.",
            ),
            _request_state(),
        )
        assert rollback["status"] == "rolled_back"
        restored_parent, restored_fence = await _new_parent(base_time + timedelta(hours=3))
        with patch.object(strategist_tick, "WebBriefToFileService", brief_service):
            restored = await strategist_tick._run_opted_in_goal_web_brief(
                parent_job_id=restored_parent,
                parent_fencing_token=restored_fence,
            )
        assert restored["status"] == "succeeded"
        assert restored["job_id"] not in {brief_first["job_id"], corrected["job_id"]}
        assert brief_tool.calls == 3
        assert source_handler.requests[-1] == "baseline source"

        # The loop exposes separate candidate, outcome, and no-learning axes;
        # successful local work deliberately makes no learning claim.
        from src.guardian.goal_conditioned_loop import list_goal_loop_receipts

        snapshot_receipts = await list_goal_loop_receipts(snapshot.id)
        receipts = await list_goal_loop_receipts(brief.id)
        snapshot_candidates = [
            item for item in snapshot_receipts if item["event_type"] == "goal_loop_candidate"
        ]
        candidates = [item for item in receipts if item["event_type"] == "goal_loop_candidate"]
        assert snapshot_candidates and candidates
        assert snapshot_candidates[0]["goal_revision"] == 1
        assert {item["goal_revision"] for item in candidates} == {1, 2, 3}
        assert snapshot_candidates[0]["candidate_id"] != candidates[0]["candidate_id"]
        event_types = {receipt["event_type"] for receipt in receipts}
        assert {"goal_loop_candidate", "goal_loop_outcome", "goal_loop_no_learning"} <= event_types
        outcomes = [item for item in receipts if item["event_type"] == "goal_loop_outcome"]
        assert any(item["verification"] == "passed" for item in outcomes)
        assert all(item["learning"] == "no_learning" for item in outcomes)
    finally:
        stack.close()
        await engine.dispose()
        source_server.shutdown()
        source_server.server_close()
        source_thread.join(timeout=2)
