"""Real canonical DB/artifact/API proof for task evidence; no model spend."""
import hashlib
import asyncio
from dataclasses import replace
import json
import sys
from types import ModuleType
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from config.settings import settings
from src.artifacts.registry import build_artifact_record
from src.auth.service import test_bypass_operator as bypass_operator
from src.db.models import (
    CalendarEventBinding, CalendarPrepReceipt, CalendarReadConsent, Goal, GoogleServiceConnection, GuardianSourceWatch,
    MailMessageBinding, MailReadConsent, Memory, MemorySource, MemoryTombstone, WorkBoardAttempt, WorkBoardEvent,
    WorkBoardStatus, WorkBoardTask, WorkflowRunState,
)
from src.memory.evidence_working_set import EvidenceRequest, EvidenceAdoptionRequest, adopt_evidence, evidence_for_task_context, read_evidence, refresh_evidence
from src.memory.evidence_sources import output_artifact_scope
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardError


@pytest.fixture
def owner():
    operator = bypass_operator()
    return WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)


@pytest.fixture
def evidence_storage(async_db, monkeypatch, tmp_path, client):
    client.headers["origin"] = "http://localhost:3001"
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    with patch("src.api.task_evidence.get_session", async_db), patch("src.api.mail.get_session", async_db):
        yield tmp_path


async def seed(async_db, owner, root):
    async with async_db() as db:
        goal = Goal(id="goal-evidence", title="Project aurora", owner_principal_id=owner.principal_id,
                    owner_session_id=owner.session_id)
        task = WorkBoardTask(task_id="task-evidence", title="Aurora launch", goal_id=goal.id,
                             owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
                             idempotency_key="task-evidence")
        db.add(goal); db.add(task); await db.flush()
        memory = Memory(id="memory-evidence", content="Aurora launch date is October 20", confidence=.95,
                        source_session_id=owner.session_id, metadata_json=json.dumps({"goal_id": goal.id}))
        db.add(memory); await db.flush()
        db.add(MemorySource(memory_id=memory.id, source_session_id=owner.session_id, source_type="operator"))
        run = WorkflowRunState(run_identity="job-evidence-document", root_run_identity="job-evidence-document",
            workflow_name="research", status="succeeded", job_kind="document_summary", owner_kind="user",
            owner_principal_id=owner.principal_id, operator_session_id=owner.session_id,
            goal_id=goal.id, goal_revision=1, revision=3)
        db.add(run)
        db.add(WorkBoardAttempt(task_id=task.task_id, workflow_run_id=run.run_identity,
                               ended_at=datetime.now(timezone.utc), executor_id="research"))
        relative = "artifacts/research/aurora.md"
        (root / relative).parent.mkdir(parents=True)
        (root / relative).write_text("Aurora launch checklist has three reviewed actions\nAurora budget is unknown")
        record = build_artifact_record(file_path=relative, artifact_type="document_summary",
                                      producer=run.job_kind, run_id=run.run_identity)
        record.pop("run_id")  # Actual durable artifact receipts omit this field.
        run.artifact_receipts_json = json.dumps([record])
        run.effect_receipts_json = json.dumps([{"receipt_kind": "readback", "status": "succeeded",
            "target_path": relative, "target_digest": record["content_sha256"],
            "content_sha256": record["content_sha256"], "details": {"verified": True}}])
    return task, memory, record


async def post_packet(client, revision=0, **extra):
    adopt = extra.pop("allow_model_context", False)
    result = await client.post("/api/work-board/tasks/task-evidence/evidence", json={
        "expected_task_revision": 1, "expected_packet_revision": revision, "query": "Aurora launch", **extra})
    if adopt and result.status_code == 200:
        packet = result.json()
        assert packet["allow_model_context"] is False
        return await client.post("/api/work-board/tasks/task-evidence/evidence/adoption", json={
            "expected_task_revision": 1, "expected_packet_revision": packet["revision"],
            "expected_packet_digest": packet["digest"], "allow_model_context": True})
    return result


@pytest.mark.asyncio
async def test_real_multisource_api_correction_and_tombstone_fence(client, async_db, owner, evidence_storage):
    task, memory, record = await seed(async_db, owner, evidence_storage)
    response = await post_packet(client, allow_model_context=True)
    assert response.status_code == 200, response.text
    packet = response.json()
    assert packet["mode"] == "lexical_degraded"
    assert {claim["source_kind"] for claim in packet["claims"]} == {"canonical_memory", "document_summary"}
    assert all(claim["line_start"] >= 1 and len(claim["source_digest"]) == 64 for claim in packet["claims"])
    assert packet["memory_status"] == "no_learning"
    memory_claim = next(c for c in packet["claims"] if c["memory_id"] == memory.id)
    inspected = await client.get(f"/api/work-board/tasks/{task.task_id}/evidence/sources/{memory_claim['source_id']}")
    assert "October 20" in inspected.text
    async with async_db() as db:
        adopted = await evidence_for_task_context(db, owner, task.task_id, "proposal-job-one")
        assert adopted["revision"] == 1 and adopted["digest"] == packet["digest"]
        assert "October 20" in json.dumps(adopted)
    correction = await client.post("/api/memory/corrections", json={
        "content": "Aurora launch date is October 25", "corrects_memory_id": memory.id,
        "metadata": {"goal_id": task.goal_id}})
    assert correction.status_code == 200, correction.text
    stale = (await client.get(f"/api/work-board/tasks/{task.task_id}/evidence")).json()
    assert stale["invalidated_count"] == 1
    assert "October 20" not in json.dumps(stale)
    refreshed = (await post_packet(client, revision=1)).json()
    assert "October 25" in json.dumps(refreshed)
    assert refreshed["digest"] != packet["digest"]
    new_memory = next(c["memory_id"] for c in refreshed["claims"] if c["memory_id"])
    async with async_db() as db:
        db.add(MemoryTombstone(memory_id=new_memory, actor=owner.principal_id))
    final = (await client.get(f"/api/work-board/tasks/{task.task_id}/evidence")).json()
    assert "October 25" not in json.dumps(final)
    async with async_db() as db:
        with pytest.raises(BoardError, match="Refresh the task evidence"):
            await evidence_for_task_context(db, owner, task.task_id, "proposal-job-two")
    events = await client.get("/api/work-board/events")
    assert events.status_code == 200
    assert "October" not in events.text and "artifacts/research" not in events.text
    files = list(evidence_storage.glob("artifacts/work-board/evidence/**/*.json"))
    assert len(files) == 2
    assert all("October" not in path.read_text() and "aurora.md" not in path.read_text() for path in files)


@pytest.mark.asyncio
async def test_scope_forgery_exclusion_cas_and_file_drift(client, async_db, owner, evidence_storage):
    task, _memory, record = await seed(async_db, owner, evidence_storage)
    async with async_db() as db:
        foreign = WorkBoardTask(task_id="foreign-task", goal_id=task.goal_id, title="Foreign",
            owner_principal_id="other", owner_session_id="other", idempotency_key="foreign")
        db.add(foreign)
        other_memory = Memory(id="foreign-memory", content="Aurora confidential foreign", source_session_id="other",
                              metadata_json=json.dumps({"goal_id": task.goal_id}))
        unrelated = Memory(id="unrelated-memory", content="Aurora unrelated project", source_session_id=owner.session_id,
                           metadata_json=json.dumps({"goal_id": "different-goal"}))
        db.add(other_memory); db.add(unrelated); await db.flush()
        db.add(MemorySource(memory_id=other_memory.id, source_session_id="other"))
        db.add(MemorySource(memory_id=unrelated.id, source_session_id=owner.session_id))
    assert (await client.get("/api/work-board/tasks/foreign-task/evidence")).status_code in {403, 404}
    packet = (await post_packet(client)).json()
    assert "confidential" not in json.dumps(packet) and "unrelated" not in json.dumps(packet)
    assert (await post_packet(client)).status_code == 409
    source = next(c for c in packet["claims"] if c["source_kind"] == "document_summary")
    excluded = await client.patch(f"/api/work-board/tasks/{task.task_id}/evidence", json={
        "expected_task_revision": 1, "expected_packet_revision": 1, "excluded_source_ids": [source["source_id"]]})
    assert excluded.status_code == 200
    assert source["source_id"] in excluded.json()["excluded_source_ids"]
    assert all(c["source_id"] != source["source_id"] for c in excluded.json()["claims"])
    assert (await client.get(f"/api/work-board/tasks/{task.task_id}/evidence/sources/{source['source_id']}")).status_code == 404
    (evidence_storage / record["file_path"]).write_text("forged changed Aurora output")
    async with async_db() as db:
        assert await output_artifact_scope(db, record["artifact_id"]) is None
    refreshed = (await post_packet(client, revision=2)).json()
    assert "forged" not in json.dumps(refreshed)
    assert not any(c["source_kind"] == "document_summary" for c in refreshed["claims"])


@pytest.mark.asyncio
async def test_symlink_ancestor_is_rejected(async_db, owner, evidence_storage, tmp_path):
    task, _, record = await seed(async_db, owner, evidence_storage)
    async with async_db() as db:
        assert await output_artifact_scope(db, record["artifact_id"]) is not None
    directory = evidence_storage / "artifacts/research"
    displaced = evidence_storage / "real-research"
    directory.rename(displaced)
    directory.symlink_to(displaced, target_is_directory=True)
    async with async_db() as db:
        assert await output_artifact_scope(db, record["artifact_id"]) is None


@pytest.mark.asyncio
async def test_actual_calendar_private_derived_artifact_revocation_and_no_new_egress(client, async_db, owner, evidence_storage, monkeypatch):
    task, _, _ = await seed(async_db, owner, evidence_storage)
    from src.integrations.google_calendar import calendar_artifact_path_for_job, write_calendar_result_bytes
    from src.api import calendar
    # The test bypass session is an authenticated API fixture; capability's
    # live-session check is independently covered by its existing real-auth suite.
    async def live(*args): pass
    monkeypatch.setattr(calendar, "_assert_live_operator_session", live)
    async with async_db() as db:
        source_task = WorkBoardTask(task_id="calendar-source-task", goal_id=task.goal_id,
            goal_revision=1, capability_id="calendar.meeting-prep.v1", owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, idempotency_key="calendar-source")
        connection = GoogleServiceConnection(connection_id="calendar-connection", owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, vault_secret_key="unused-calendar-key", state="active")
        consent = CalendarReadConsent(consent_id="calendar-consent", owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, connection_id=connection.connection_id, goal_id=task.goal_id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1), allow_remote_model=True)
        db.add(source_task); db.add(connection); db.add(consent); await db.flush()
        db.add(CalendarEventBinding(event_binding_id="event", owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, connection_id=connection.connection_id, consent_id=consent.consent_id))
        job_id = "calendar-prep:local-artifact-proof"
        relative = calendar_artifact_path_for_job(job_id)
        data = json.dumps({"schema_version": 1, "summary": "Aurora launch meeting preparation",
                           "agenda": ["Confirm launch"], "questions": [], "risks": [], "preparation_steps": []}).encode()
        write_calendar_result_bytes(relative, data)
        record = build_artifact_record(file_path=relative, artifact_type="calendar_meeting_prep_result",
                                      producer="calendar_meeting_prep", run_id=job_id)
        run = WorkflowRunState(run_identity=job_id, root_run_identity=job_id, workflow_name="calendar",
            owner_kind="user", owner_principal_id=owner.principal_id, operator_session_id=owner.session_id,
            goal_id=task.goal_id, goal_revision=1, job_kind="calendar_meeting_prep", status="succeeded",
            artifact_receipts_json=json.dumps([record]), effect_receipts_json=json.dumps([{
                "receipt_kind": "readback", "status": "succeeded", "target_path": relative,
                "target_digest": record["content_sha256"], "content_sha256": record["content_sha256"],
                "details": {"verified": True}}]))
        db.add(run); db.add(WorkBoardAttempt(attempt_id="calendar-attempt", task_id=source_task.task_id,
            workflow_run_id=job_id, ended_at=datetime.now(timezone.utc)))
        db.add(CalendarPrepReceipt(owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
            task_id=source_task.task_id, attempt_id="calendar-attempt", durable_job_id=job_id, goal_id=task.goal_id,
            connection_id=connection.connection_id, consent_id=consent.consent_id, event_binding_id="event",
            status="succeeded", artifact_id=record["artifact_id"], file_path=relative, content_sha256=record["content_sha256"]))
    packet = (await post_packet(client, allow_model_context=True)).json()
    private = next(c for c in packet["claims"] if c["source_kind"] == "calendar_meeting_prep_result")
    assert "Aurora launch meeting preparation" in private["text"]
    assert private["model_context_allowed"] is False
    from src.work_board import triage
    from src.db.models import WorkBoardProposal
    transport = AsyncMock(side_effect=AssertionError("Private evidence reached model transport"))
    monkeypatch.setattr(triage, "completion_with_fallback", transport)
    specified = await client.post(f"/api/work-board/tasks/{task.task_id}/specify", json={
        "expected_revision": 1, "idempotency_key": "private-calendar-purpose-proof"})
    assert specified.status_code == 403, specified.text
    assert specified.json()["detail"]["code"] == "evidence_source_purpose_consent_required"
    transport.assert_not_awaited()
    async with async_db() as db:
        # The pre-writer source guard refuses private model purpose before
        # admitting a proposal/native job, rather than fabricating a blocked
        # post-admission provider receipt.
        proposal = (await db.execute(select(WorkBoardProposal).where(
            WorkBoardProposal.parent_task_id == task.task_id))).scalar_one_or_none()
        assert proposal is None
        with pytest.raises(BoardError, match="Private Mail/Calendar"):
            await evidence_for_task_context(db, owner, task.task_id, "no-private-egress")
        from src.memory.evidence_dependencies import canonical_source_token
        for consumer in ('browser.public-task.v1','work.evidence-dossier.v1','work.local-evidence-report.v1'):
            target = WorkBoardTask(**{**task.model_dump(), 'capability_id': consumer})
            with pytest.raises(BoardError) as unsupported:
                await canonical_source_token(db, owner, target, 'calendar_meeting_prep_result', record['artifact_id'])
            assert unsupported.value.code == 'evidence_dependency_unsupported'
        consent = await db.get(CalendarReadConsent, "calendar-consent")
        consent.state = "revoked"
    after = (await client.get(f"/api/work-board/tasks/{task.task_id}/evidence")).json()
    assert "meeting preparation" not in json.dumps(after)
    assert after["invalidated_count"] >= 1
    assert any("calendar_meeting_prep_result" in reason for reason in after["blocked_sources"])


@pytest.mark.asyncio
async def test_actual_encrypted_mail_draft_uses_existing_consent_fences(client, async_db, owner, evidence_storage):
    task, _, _ = await seed(async_db, owner, evidence_storage)
    from src.workflows.mail_reply_draft import write_private_draft
    from src.work_board.input_artifacts import prepare_input_artifact
    from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardTaskCreate
    from src.work_board.repository import WorkBoardRepository

    async with async_db() as db:
        connection = GoogleServiceConnection(connection_id="mail-connection", owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, service="gmail_readonly", vault_secret_key="unused-mail-key", state="active")
        consent = MailReadConsent(consent_id="mail-consent", owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, connection_id=connection.connection_id, goal_id=task.goal_id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1), model_egress_allowed=True,
            model_digest="sha256:" + "a" * 64, allowed_body_fields_json='["subject","plainbody","replyintent"]')
        binding = MailMessageBinding(message_binding_id="message", owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, connection_id=connection.connection_id,
            source_consent_id=consent.consent_id, source_consent_revision=1, message_revision="sha256:" + "e" * 64)
        db.add(connection); db.add(consent); db.add(binding)
        await db.flush()
        inputs = {"schema_version": 1, "connection_id": connection.connection_id,
            "expected_connection_revision": 1, "message_binding_id": binding.message_binding_id,
            "expected_message_revision": binding.message_revision, "mail_consent_id": consent.consent_id,
            "expected_source_consent_revision": 1, "expected_model_consent_revision": 1,
            "goal_id": task.goal_id, "expected_goal_revision": 1, "reply_intent": "Confirm Aurora launch", "style": "brief"}
        metadata = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(
            schema_version=1, capability_id="work.mail-reply-draft.v1", goal_id=task.goal_id, goal_revision=1,
            input=inputs, idempotency_key="mail-source-input"))
        mutation = await WorkBoardRepository().create_task(db, owner, WorkBoardTaskCreate(
            title="Aurora draft", goal_id=task.goal_id, goal_revision=1, status="todo",
            capability_id="work.mail-reply-draft.v1", input_artifact_id=metadata.artifact_id,
            idempotency_key="mail-source-task"))
        source_task = mutation.task
        job_id = "mail-reply:local-derived-proof"
        relative, _, _ = write_private_draft(job_id, {"schema_version": 1,
            "subject": "Aurora launch confirmation", "plainbody": "Aurora launch reply is ready",
            "caveats": [], "message_revision": binding.message_revision,
            "source_body_digest": "e" * 64, "effective_route": {}})
        record = build_artifact_record(file_path=relative, artifact_type="mail_reply_draft", producer="mail_reply_draft", run_id=job_id)
        run = WorkflowRunState(run_identity=job_id, root_run_identity=job_id, workflow_name="mail",
            owner_kind="user", owner_principal_id=owner.principal_id, operator_session_id=owner.session_id,
            goal_id=task.goal_id, goal_revision=1, job_kind="mail_reply_draft", status="succeeded",
            artifact_receipts_json=json.dumps([record]), effect_receipts_json=json.dumps([{
                "receipt_kind": "readback", "status": "succeeded", "target_path": relative,
                "target_digest": record["content_sha256"], "content_sha256": record["content_sha256"],
                "details": {"verified": True}}]))
        db.add(run); db.add(WorkBoardAttempt(task_id=source_task.task_id, workflow_run_id=job_id,
                                           ended_at=datetime.now(timezone.utc)))
    assert "Aurora" not in (evidence_storage / relative).read_text()
    packet_response = await post_packet(client, allow_model_context=True)
    assert packet_response.status_code == 200, packet_response.text
    packet = packet_response.json()
    private = next(c for c in packet["claims"] if c["source_kind"] == "mail_reply_draft")
    assert private["model_context_allowed"] is False
    assert "Aurora" in private["text"]
    async with async_db() as db:
        with pytest.raises(BoardError, match="Private Mail/Calendar"):
            await evidence_for_task_context(db, owner, task.task_id, "never-send-private")
        from src.memory.evidence_dependencies import canonical_source_token
        for consumer in ('browser.public-task.v1','work.evidence-dossier.v1','work.local-evidence-report.v1'):
            target = WorkBoardTask(**{**task.model_dump(), 'capability_id': consumer})
            with pytest.raises(BoardError) as unsupported:
                await canonical_source_token(db, owner, target, 'mail_reply_draft', record['artifact_id'])
            assert unsupported.value.code == 'evidence_dependency_unsupported'
        connection = await db.get(GoogleServiceConnection, "mail-connection")
        connection.state = "revoked"
    after = (await client.get(f"/api/work-board/tasks/{task.task_id}/evidence")).json()
    assert not any(c["source_kind"] == "mail_reply_draft" for c in after["claims"])
    assert after["invalidated_count"] >= 1


@pytest.mark.asyncio
async def test_governed_prompt_contains_exact_packet_and_stale_adoption_fails(async_db, owner, evidence_storage, monkeypatch):
    task, _, _ = await seed(async_db, owner, evidence_storage)
    from src.work_board import triage
    from src.memory.evidence_working_set import verify_evidence_use
    operator = bypass_operator()
    monkeypatch.setattr(triage, "build_canonical_inference_context", lambda *args, **kwargs: kwargs)
    async with async_db() as db:
        packet = await refresh_evidence(db, owner, task.task_id, EvidenceRequest(
            expected_task_revision=1, expected_packet_revision=0, query="Aurora launch"))
        await adopt_evidence(db, owner, task.task_id, EvidenceAdoptionRequest(
            expected_task_revision=1, expected_packet_revision=packet["revision"],
            expected_packet_digest=packet["digest"], allow_model_context=True))
    prepared = await triage._prepare_governed_proposal(task, kind="specify", operator=operator,
        job_id="exact-context-job", route_id="strategist_agent")
    data = json.loads(prepared[0][1]["content"])
    assert data["evidence_data"]["digest"] == packet["digest"]
    assert "October 20" in prepared[0][1]["content"]
    async with async_db() as db:
        await verify_evidence_use(db, owner, task.task_id, "exact-context-job", data["evidence_data"])
    async with async_db() as db:
        event = (await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind == "task.evidence.used"))).scalar_one()
        assert json.loads(event.metadata_json) == {"packet_revision": 1, "packet_digest": packet["digest"], "job_id": "exact-context-job"}
        await refresh_evidence(db, owner, task.task_id, EvidenceRequest(expected_task_revision=1,
            expected_packet_revision=1, query="Aurora budget"))
    async with async_db() as db:
        with pytest.raises(BoardError, match="evidence changed"):
            await verify_evidence_use(db, owner, task.task_id, "stale-context-job", data["evidence_data"])


@pytest.mark.asyncio
async def test_injected_identity_boundary_preserves_exact_selected_lineage_and_read_only_history(client, async_db, owner, evidence_storage, monkeypatch):
    """Real sources/API, injected identity proof seam; #900 owns proof itself."""
    task, _, _ = await seed(async_db, owner, evidence_storage)
    async with async_db() as db:
        old_goal = Goal(id="old-goal", title="Aurora historical", owner_principal_id="original-principal",
                        owner_session_id="original-root")
        old_task = WorkBoardTask(task_id="old-task", goal_id=old_goal.id, title="Aurora past",
            owner_principal_id="original-principal", owner_session_id="original-root", idempotency_key="old-task")
        db.add(old_goal); db.add(old_task); await db.flush()
        old_memory = Memory(id="old-memory", content="Aurora historical decision is reviewed", source_session_id="original-root",
                            metadata_json=json.dumps({"goal_id": old_goal.id}))
        unselected = Memory(id="unselected-old-memory", content="Aurora unselected secret", source_session_id="original-root",
                            metadata_json=json.dumps({"goal_id": old_goal.id}))
        db.add(old_memory); db.add(unselected); await db.flush()
        db.add(MemorySource(memory_id=old_memory.id, source_type="operator", source_session_id="original-root"))
        db.add(MemorySource(memory_id=unselected.id, source_type="operator", source_session_id="original-root"))
        fresh_unlinked = WorkBoardTask(task_id="unlinked-task", goal_id=task.goal_id, title="Aurora unrelated",
            owner_principal_id=owner.principal_id, owner_session_id=owner.session_id, idempotency_key="unlinked-task")
        db.add(fresh_unlinked)
    historical_owner = WorkBoardOwner(principal_id="original-principal", session_id="original-root")
    async with async_db() as db:
        await refresh_evidence(db, historical_owner, "old-task", EvidenceRequest(
            expected_task_revision=1, expected_packet_revision=0, query="Aurora historical"))
    module = ModuleType("src.auth.ownership")
    selections = {"goal": {"old-goal": "original-root"}, "task": {"old-task": "original-root"},
                  "memory": {"old-memory": "original-root"}, "output_artifact": {}}
    async def selected(operator, kind, *, db=None): return selections.get(kind, {})
    async def lineage(operator, current_task_id, kind, record_id, *, db=None):
        return selections.get(kind, {}).get(record_id) if current_task_id == task.task_id else None
    module.selected_read_scopes = selected
    module.fresh_work_source_scope = lineage
    monkeypatch.setitem(sys.modules, "src.auth.ownership", module)
    packet = (await post_packet(client, allow_model_context=True)).json()
    historical = next(c for c in packet["claims"] if c["memory_id"] == "old-memory")
    assert historical["owner_principal_id"] == "original-principal"
    assert historical["owner_session_id"] == "original-root"
    assert historical["ownership_access"] == "recovered_read_only"
    assert historical["model_context_allowed"] is False
    assert "unselected secret" not in json.dumps(packet)
    async with async_db() as db:
        with pytest.raises(BoardError, match="source-purpose"):
            await evidence_for_task_context(db, owner, task.task_id, "no-historical-egress", operator=bypass_operator())
    old_packet = await client.get("/api/work-board/tasks/old-task/evidence")
    assert old_packet.status_code == 200, old_packet.text
    assert old_packet.json()["ownership_access"] == "recovered_read_only"
    assert "unselected secret" not in old_packet.text
    assert (await client.post("/api/work-board/tasks/old-task/evidence", json={
        "expected_task_revision": 1, "expected_packet_revision": 1})).status_code in {403, 404}
    unlinked = await client.post("/api/work-board/tasks/unlinked-task/evidence", json={
        "expected_task_revision": 1, "expected_packet_revision": 0, "query": "Aurora historical"})
    assert "old-memory" not in unlinked.text
    selections["memory"] = {}
    readback = (await client.get(f"/api/work-board/tasks/{task.task_id}/evidence")).json()
    assert "historical decision" not in json.dumps(readback)
    assert readback["invalidated_count"] >= 1


@pytest.mark.asyncio
async def test_service_owned_browser_output_requires_exact_delegation_and_current_site_policy(client, async_db, owner, evidence_storage, monkeypatch):
    task, _, _ = await seed(async_db, owner, evidence_storage)
    from src.browser.task_runner import browser_artifact_path_for_job, _write_browser_artifact_bytes
    from src.workflows.job_runtime import _digest
    async with async_db() as db:
        source_task = WorkBoardTask(task_id="browser-source", goal_id=task.goal_id, title="Aurora public research",
            owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
            capability_id="browser.public-task.v1", idempotency_key="browser-source",
            input_artifact_id="browser-input", typed_input_digest="a" * 64)
        db.add(source_task); await db.flush()
        attempt = WorkBoardAttempt(attempt_id="browser-attempt", task_id=source_task.task_id,
            workflow_run_id="browser-task:browser-source:browser-attempt", fencing_token=1,
            task_revision_at_claim=1, ended_at=datetime.now(timezone.utc))
        db.add(attempt)
        authority = {"principal": "service:browser-task", "owner_kind": "service", "service_id": "service:browser-task",
            "capability_id": source_task.capability_id, "goal_owner_principal_id": owner.principal_id,
            "goal_owner_session_id": owner.session_id, "operator_owner_principal_id": owner.principal_id,
            "operator_owner_session_id": owner.session_id, "goal_id": task.goal_id, "goal_revision": 1,
            "board_fencing_token": 1, "board_task_revision": 2, "input_artifact_id": source_task.input_artifact_id,
            "input_artifact_digest": source_task.typed_input_digest}
        relative = browser_artifact_path_for_job(attempt.workflow_run_id)
        payload = json.dumps({"schema_version": 1, "task_id": source_task.task_id, "attempt_id": attempt.attempt_id,
            "final_url": "https://example.com/research", "extracts": [{"kind": "extract", "value": "Aurora public browser finding"}]}).encode()
        _write_browser_artifact_bytes(relative, payload, workspace_root=evidence_storage)
        record = build_artifact_record(file_path=relative, artifact_type="browser_public_task_result",
            producer="browser_public_task", run_id=attempt.workflow_run_id)
        record.pop("run_id")
        run = WorkflowRunState(run_identity=attempt.workflow_run_id, root_run_identity=attempt.workflow_run_id,
            workflow_name="browser", owner_kind="service", owner_principal_id="service:browser-task",
            service_id="service:browser-task", job_kind="browser_public_task", status="succeeded",
            operator_session_id=owner.session_id, goal_id=task.goal_id, goal_revision=1,
            idempotency_scope="work-board-attempt", idempotency_key=f"{source_task.task_id}:{attempt.attempt_id}",
            declared_authority_json=json.dumps(authority), authority_digest=_digest(authority),
            artifact_receipts_json=json.dumps([record]), effect_receipts_json=json.dumps([{
                "receipt_kind": "readback", "status": "succeeded", "target_path": relative,
                "target_digest": record["content_sha256"], "content_sha256": record["content_sha256"],
                "details": {"verified": True}}]))
        db.add(run)
    monkeypatch.setattr(settings, "browser_site_blocklist", "")
    packet = (await post_packet(client)).json()
    assert any(c["text"] == "Aurora public browser finding" for c in packet["claims"])
    async with async_db() as db:
        assert await output_artifact_scope(db, record["artifact_id"]) is not None
        run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))).scalar_one()
        forged = {**authority, "operator_owner_principal_id": "foreign-owner"}
        run.declared_authority_json = json.dumps(forged)
        run.authority_digest = _digest(forged)
    after = (await client.get(f"/api/work-board/tasks/{task.task_id}/evidence")).json()
    assert "browser finding" not in json.dumps(after)
    async with async_db() as db:
        assert await output_artifact_scope(db, record["artifact_id"]) is None
        run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))).scalar_one()
        run.declared_authority_json = json.dumps(authority); run.authority_digest = _digest(authority)
    monkeypatch.setattr(settings, "browser_site_blocklist", "example.com")
    blocked = (await post_packet(client, revision=1)).json()
    assert "browser finding" not in json.dumps(blocked)
    assert any("browser_public_task_result" in reason for reason in blocked["blocked_sources"])


@pytest.mark.asyncio
async def test_research_watch_output_obeys_current_source_plan_and_readback(client, async_db, owner, evidence_storage):
    task, _, _ = await seed(async_db, owner, evidence_storage)
    from src.workflows.job_runtime import _digest
    async with async_db() as db:
        inputs = json.dumps({"schema_version": 1, "capability_id": "guardian.research-watch.v1",
                             "input": {"watch_id": "watch-local", "expected_plan_revision": 1}}).encode()
        input_relative = "artifacts/research/watch-input.json"
        (evidence_storage / input_relative).write_bytes(inputs)
        source_task = WorkBoardTask(task_id="watch-source", goal_id=task.goal_id, title="Aurora research",
            owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
            capability_id="guardian.research-watch.v1", idempotency_key="watch-source",
            typed_input_ref="workspace-json:" + input_relative, typed_input_digest=hashlib.sha256(inputs).hexdigest())
        db.add(source_task); await db.flush()
        attempt = WorkBoardAttempt(attempt_id="watch-attempt", task_id=source_task.task_id,
            workflow_run_id="source-watch:watch-local:occurrence", ended_at=datetime.now(timezone.utc))
        db.add(attempt)
        watch = GuardianSourceWatch(id="watch-local", goal_id=task.goal_id,
            owner_principal_id=owner.principal_id, owner_session_id=owner.session_id, scheduled_job_id="watch-schedule")
        db.add(watch)
        authority = {"principal": "service:guardian-source-watch", "owner_kind": "service", "service_id": "guardian-source-watch",
            "capability_id": source_task.capability_id, "goal_owner_principal_id": owner.principal_id,
            "goal_owner_session_id": owner.session_id, "session_id": owner.session_id,
            "goal_id": task.goal_id, "goal_revision": 1, "plan_revision": 1}
        relative = "artifacts/research/aurora-dossier.md"
        (evidence_storage / relative).write_text("Aurora source-watch dossier has a reviewed finding")
        record = build_artifact_record(file_path=relative, artifact_type="guardian_decision_dossier",
                                      producer="guardian_source_watch", run_id=attempt.workflow_run_id)
        record.pop("run_id")
        run = WorkflowRunState(run_identity=attempt.workflow_run_id, root_run_identity=attempt.workflow_run_id,
            workflow_name="watch", owner_kind="service", owner_principal_id="service:guardian-source-watch",
            service_id="guardian-source-watch", job_kind="guardian_source_watch", status="succeeded",
            operator_session_id=owner.session_id, goal_id=task.goal_id, goal_revision=1, plan_revision=1,
            idempotency_scope="work-board-attempt", idempotency_key=f"{source_task.task_id}:{attempt.attempt_id}",
            declared_authority_json=json.dumps(authority), authority_digest=_digest(authority),
            artifact_receipts_json=json.dumps([record]), effect_receipts_json=json.dumps([{
                "receipt_kind": "readback", "status": "succeeded", "effect_type": "workspace_write",
                "target_path": relative, "target_digest": record["content_sha256"],
                "content_sha256": record["content_sha256"], "details": {"verified": True}}]))
        db.add(run)
    packet = (await post_packet(client)).json()
    assert any(c["source_kind"] == "guardian_decision_dossier" for c in packet["claims"])
    async with async_db() as db:
        run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))).scalar_one()
        run.effect_receipts_json = "[]"
    assert "reviewed finding" not in (await client.get(f"/api/work-board/tasks/{task.task_id}/evidence")).text
    async with async_db() as db:
        run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))).scalar_one()
        run.effect_receipts_json = json.dumps([{"receipt_kind": "readback", "status": "succeeded", "target_path": relative,
            "target_digest": record["content_sha256"], "content_sha256": record["content_sha256"], "details": {"verified": True}}])
        watch = await db.get(GuardianSourceWatch, "watch-local")
        watch.plan_revision = 2
    after = (await post_packet(client, revision=1)).json()
    assert not any(c["source_kind"] == "guardian_decision_dossier" for c in after["claims"])


@pytest.mark.asyncio
async def test_packet_adoption_is_exact_local_review_and_drift_fenced(client, async_db, owner, evidence_storage):
    task, memory, record = await seed(async_db, owner, evidence_storage)
    endpoint = f"/api/work-board/tasks/{task.task_id}/evidence"
    unseen = await client.post(endpoint, json={"expected_task_revision": 1, "expected_packet_revision": 0,
        "query": "Aurora", "allow_model_context": True})
    assert unseen.status_code == 422
    packet = (await post_packet(client)).json()
    assert packet["allow_model_context"] is False
    body = {"expected_task_revision": 1, "expected_packet_revision": packet["revision"],
            "expected_packet_digest": packet["digest"], "allow_model_context": True}
    wrong = await client.post(endpoint + "/adoption", json={**body, "expected_packet_digest": "0" * 64})
    assert wrong.status_code == 409
    # Replay must not create a second adoption; the separate file-backed test
    # exercises simultaneous transactions on independent SQLite connections.
    first = await client.post(endpoint + "/adoption", json=body)
    assert first.status_code == 200
    duplicate = await client.post(endpoint + "/adoption", json=body)
    assert duplicate.status_code == 409
    newer = (await post_packet(client, revision=1)).json()
    assert newer["allow_model_context"] is False
    assert (await client.post(endpoint + "/adoption", json=body)).status_code == 409
    (evidence_storage / record["file_path"]).write_text("Changed unseen source")
    drift = await client.post(endpoint + "/adoption", json={**body,
        "expected_packet_revision": newer["revision"], "expected_packet_digest": newer["digest"]})
    assert drift.status_code == 409
    assert drift.json()["detail"]["code"] == "evidence_source_changed"


@pytest.mark.asyncio
async def test_real_dispatcher_goal_snapshot_enters_api_and_exact_output_scope(client, async_db, monkeypatch, tmp_path):
    from tests.test_work_board_adapters import _run_real_board_goal_snapshot
    from src.api import task_evidence
    outcome, task, attempt, runs = await _run_real_board_goal_snapshot(async_db, monkeypatch, tmp_path)
    assert outcome["completed"] is True
    child = next(run for run in runs if run.run_identity == f"goal-snapshot-work-board:{task.task_id}:{attempt.attempt_id}")
    record = json.loads(child.artifact_receipts_json)[0]
    operator = bypass_operator()
    operator = replace(operator, session_id=task.owner_session_id,
        principal=replace(operator.principal, principal_id=task.owner_principal_id, session_id=task.owner_session_id))
    monkeypatch.setattr(task_evidence, "_operator", lambda request: operator)
    monkeypatch.setattr(task_evidence, "get_session", async_db)
    async with async_db() as db:
        scope = await output_artifact_scope(db, record["artifact_id"])
        assert scope is not None
        assert scope["owner_session_id"] == task.owner_session_id
        current = WorkBoardTask(task_id="snapshot-evidence-current", title="Managed board goal", goal_id=task.goal_id,
            goal_revision=task.goal_revision, owner_principal_id=task.owner_principal_id,
            owner_session_id=task.owner_session_id, idempotency_key="snapshot-evidence-current")
        db.add(current)
    endpoint = f"/api/work-board/tasks/{current.task_id}/evidence"
    response = await client.post(endpoint, json={"expected_task_revision": 1,
        "expected_packet_revision": 0, "query": "Managed"})
    assert response.status_code == 200, response.text
    packet = response.json()
    assert any(c["source_kind"] == "goal_snapshot" and "Managed" in c["text"] for c in packet["claims"]), packet
    assert packet["allow_model_context"] is False
    # Exact child and parent provenance are required; no arbitrary descendant
    # artifact becomes selectable merely because it shares a goal or root.
    for field, forged in (("parent_run_identity", "unrelated-parent"),
                           ("root_run_identity", "unrelated-root"),
                           ("owner_principal_id", "service:other")):
        async with async_db() as db:
            stored = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == child.run_identity))).scalar_one()
            original = getattr(stored, field)
            setattr(stored, field, forged)
            await db.flush()
            assert await output_artifact_scope(db, record["artifact_id"]) is None
            setattr(stored, field, original)
    async with async_db() as db:
        root = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))).scalar_one()
        original = root.input_digest
        root.input_digest = "0" * 64
        await db.flush()
        assert await output_artifact_scope(db, record["artifact_id"]) is None
        root.input_digest = original
    async with async_db() as db:
        stored = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == child.run_identity))).scalar_one()
        stored.parent_fencing_token += 1
    assert not (await client.get(endpoint)).json()["claims"]
    async with async_db() as db:
        assert await output_artifact_scope(db, record["artifact_id"]) is None


@pytest.mark.asyncio
async def test_concurrent_adoption_has_one_canonical_winner(owner, evidence_storage):
    from contextlib import asynccontextmanager
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlmodel import SQLModel
    engine = create_async_engine(f"sqlite+aiosqlite:///{evidence_storage / 'adoption-race.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    @asynccontextmanager
    async def sessions():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    try:
        async with engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)
        task, _, _ = await seed(sessions, owner, evidence_storage)
        async with sessions() as db:
            packet = await refresh_evidence(db, owner, task.task_id, EvidenceRequest(
                expected_task_revision=1, expected_packet_revision=0, query="Aurora"))
        request = EvidenceAdoptionRequest(expected_task_revision=1,
            expected_packet_revision=packet["revision"], expected_packet_digest=packet["digest"],
            allow_model_context=True)
        async def contender():
            try:
                async with sessions() as db:
                    await adopt_evidence(db, owner, task.task_id, request)
                return "adopted"
            except BoardError as error:
                return error.code
        assert sorted(await asyncio.gather(contender(), contender())) == ["adopted", "evidence_adoption_stale"]
        async with sessions() as db:
            assert len((await db.execute(select(WorkBoardEvent).where(
                WorkBoardEvent.kind == "task.evidence.adopted"))).scalars().all()) == 1
    finally:
        await engine.dispose()
