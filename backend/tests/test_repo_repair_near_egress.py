"""Single-shot repository repair sends only its consented exact NEAR request (#1184).

Real owners end to end: the canonical repair rows, the source packet, the
consent (which pins the NEAR body and route digests), ``generate_proposal``,
``FallbackLiteLLMModel.generate``, the NEAR TEE adapter, the shared broker
and durable accounting. Only the NEAR gateway is intercepted; every other
host fails the test. A refusal leaves no NEAR completion and no contacted
reservation; the clean request then sends exactly the consented body.
"""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
from sqlmodel import select

from config.settings import settings
from src.db.models import InferenceCostReservation, RepoRepairEgressConsent
from src.execution.repo_sandbox import RootlessDockerRepoSandbox
from src.security.trust_contract import canonical_digest
from src.work_board.contracts import WorkBoardOwner
from src.workflows.repo_repair import RepoRepairError, RepoRepairInput, RepoRepairService, single_shot_transport
from tests.near_tee_support import near_completion_requests, prepare_repository_near_route
from tests.test_near_tee_text_slot import isolated_broker  # noqa: F401  (fresh broker per test)
from tests.test_repo_repair import _repair_input, _seed_canonical_repair


def _proposal(body):
    prompt = json.loads(body["messages"][1]["content"])
    return json.dumps({"summary": "Update the bounded value.",
        "base_snapshot_sha256": prompt["source_packet"]["base_snapshot_sha256"],
        "patch_unified_diff": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n",
        "allowed_paths": ["src/app.py", "tests/test_app.py"], "test_args": ["pytest", "tests/test_app.py"],
        "expected_outcome": "The focused test passes."}, sort_keys=True)


async def _consented(async_db, tmp_path, monkeypatch, key):
    from src.workspace.production import ProductionWorkspace, prepare_lifecycle_directory
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "src").mkdir(parents=True)
    (workspace / "repo" / "tests").mkdir()
    (workspace / "repo" / "src" / "app.py").write_text("VALUE = 1\n")
    (workspace / "repo" / "tests" / "test_app.py").write_text("def test_value():\n    assert True\n")
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "deployment-lifecycle"))
    prepare_lifecycle_directory(ProductionWorkspace(host_root=workspace))
    near = await prepare_repository_near_route(monkeypatch)
    near.script = _proposal
    now = datetime.now(timezone.utc)
    sandbox = RootlessDockerRepoSandbox()
    sandbox.preflight = lambda: SimpleNamespace(ok=True, status="verified", reason="",
        as_receipt=lambda: {"ok": True, "status": "verified", "reason": "", "operator_visible": True})
    service = RepoRepairService(sandbox=sandbox, secret_scanner=lambda value: value, session_factory=async_db,
        workspace_dir=str(workspace), clock=lambda: now)
    import uuid
    from src.auth.service import _principal
    owner = WorkBoardOwner(principal_id="operator:root:" + uuid.uuid4().hex, session_id=f"session-{key}")
    request = _repair_input()
    async with async_db() as db:
        await _seed_canonical_repair(db, workspace, request, owner=owner, task_id=f"task:{key}",
            attempt_id=f"attempt:{key}", run_id=f"job:{key}", goal_id=f"goal:{key}", now=now)
        packet = await service.inspect_and_prepare(request, owner=owner, work_board_task_id=f"task:{key}",
            work_board_attempt_id=f"attempt:{key}", workflow_run_id=f"job:{key}", goal_id=f"goal:{key}",
            goal_revision=1, db=db)
        await db.commit()
        consent = await service.grant_egress_consent(owner=owner, work_board_task_id=f"task:{key}",
            work_board_attempt_id=f"attempt:{key}", workflow_run_id=f"job:{key}", packet=packet,
            effective_profile_id="near_tee.text", effective_upstream="near_tee", request_key=f"request:{key}",
            expires_at=now + timedelta(minutes=5), db=db)
        await db.commit()
    # The seeded live operator session's own principal, bound to this job.
    principal = replace(_principal(owner.session_id, owner.principal_id), job_id=f"job:{key}")
    return SimpleNamespace(near=near, service=service, owner=owner, request=request, packet=packet,
        consent=consent, principal=principal, db=async_db)


async def _generate(flow):
    async with flow.db() as db:
        return await flow.service.generate_proposal(flow.packet, flow.request, owner=flow.owner,
            principal=flow.principal, lease_owner="worker:repo-repair", fencing_token=1,
            consent=flow.consent, db=db)


async def _no_contact(flow):
    assert near_completion_requests(flow.near) == []
    async with flow.db() as db:
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
    # Nothing contacted: no reservation, or a durable never-contacted denial
    # (reserved, provider_contact_denied, no contact recorded).
    assert all(row.contact_started_at is None and (row.state == "released" or (
        row.state == "reserved" and row.recovery_reason == "provider_contact_denied")) for row in rows), rows
    return rows


@pytest.mark.asyncio
async def test_consent_pins_the_exact_near_request_and_only_it_is_sent(async_db, tmp_path, monkeypatch):
    flow = await _consented(async_db, tmp_path, monkeypatch, "near-exact")
    expected = single_shot_transport(flow.service, flow.packet, RepoRepairInput.model_validate(flow.request))
    async with flow.db() as db:
        row = await db.get(RepoRepairEgressConsent, flow.consent.id)
    assert row.serialized_request_sha256 == expected["body_digest"]
    assert row.request_route_digest == expected["route_digest"]
    assert row.effective_profile_id == "near_tee.text" and row.effective_upstream == "near_tee"
    await _generate(flow)
    (sent,) = near_completion_requests(flow.near)
    body = json.loads(sent.content)
    assert body == expected["body"] and canonical_digest(body) == row.serialized_request_sha256
    assert sent.headers["x-no-aliasing"] == "true"


@pytest.mark.asyncio
async def test_a_slot_change_after_consent_is_refused_before_any_reservation(async_db, tmp_path, monkeypatch):
    """A new reviewed slot (another contract hash) is another route: fresh consent required."""
    from src.model_fabric.configuration import read_model_fabric_configuration, write_model_fabric_configuration
    flow = await _consented(async_db, tmp_path, monkeypatch, "near-slot-change")
    configured = read_model_fabric_configuration()
    revision = configured.egress_revision + 1
    slot = replace(configured.near_tee.slots["text"], temperature=0.7, purpose_consent_revision=revision)
    write_model_fabric_configuration(replace(configured, egress_revision=revision,
        near_tee=replace(configured.near_tee, slots={"text": slot})), expected_revision=configured.egress_revision)
    with pytest.raises(RepoRepairError) as refused:
        await _generate(flow)
    assert refused.value.code == "repair_finalized_request_changed"
    await _no_contact(flow)
    async with flow.db() as db:
        assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []


@pytest.mark.asyncio
async def test_a_body_changed_after_preadmission_is_refused_at_the_contact_guard(async_db, tmp_path, monkeypatch):
    """The precontact guard rechecks under the contact writer: a body changed
    after the pre-admission check is a never-contacted denial."""
    from src import llm_runtime
    from src.model_fabric.near_tee import NearTeeCall
    flow = await _consented(async_db, tmp_path, monkeypatch, "near-guard")
    real = llm_runtime._execute_sync_with_gpu_admission

    def mutate_then_admit(**kwargs):
        (call,) = [cell.cell_contents for cell in kwargs["operation"].__closure__
            if isinstance(cell.cell_contents, NearTeeCall)]
        call.body["messages"].append({"role": "user", "content": "unconsented extra source"})
        return real(**kwargs)

    monkeypatch.setattr(llm_runtime, "_execute_sync_with_gpu_admission", mutate_then_admit)
    with pytest.raises(Exception) as refused:
        await _generate(flow)
    assert "repository_repair_final_transport_changed" in repr(refused.value) or \
        "repository_repair_final_transport_changed" in repr(refused.value.__cause__)
    (denied,) = await _no_contact(flow)
    assert "repository_repair_final_transport_changed" in denied.evidence_json


@pytest.mark.asyncio
async def test_a_consent_without_the_pinned_request_never_reaches_near(async_db, tmp_path, monkeypatch):
    """A consent granted with no usable NEAR route (or before #1184) pins
    nothing; generation refuses before any reservation."""
    flow = await _consented(async_db, tmp_path, monkeypatch, "near-unpinned")
    async with flow.db() as db:
        row = await db.get(RepoRepairEgressConsent, flow.consent.id)
        row.serialized_request_sha256 = row.request_route_digest = None
        await db.commit()
    flow.consent = row
    with pytest.raises(RepoRepairError) as refused:
        await _generate(flow)
    assert refused.value.code in {"repair_finalized_request_changed", "repair_authority_changed",
        "egress_consent_authority_invalid"}
    await _no_contact(flow)
    async with flow.db() as db:
        assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []
