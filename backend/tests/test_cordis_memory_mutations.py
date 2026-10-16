"""#1064 acceptance: operator-admitted memory mutations through Cordis.

Propose, reviewed apply and forget run through the real reviewed Cordis child
over owned pipes, on a real SQLite file with the real durable job repository.
All external transport is denied (the shared ``service_db`` fixture), and no
model route exists on this path.
"""
from __future__ import annotations

import ast
import re
import dataclasses
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select, update

from src.db.engine import get_session
from src.db.models import (AuditEvent, Goal, Memory, MemoryProposal, MemorySnapshot, MemoryTombstone,
                           OperatorSession, Session, WorkBoardAttempt, WorkBoardStatus, WorkBoardTask,
                           WorkflowRunState)
from src.memory import m5
from src.runtime_plugins import memory_admission, memory_mutations
from src.runtime_plugins.bridge import HostBlocked
from src.runtime_plugins.dispatch import BLOCKED_METHODS, SUPPORTED_METHODS, NativeServiceBlocked
from src.runtime_plugins.memory_admission import (build_candidate, execute_memory_mutation,
                                                  outstanding_admission_check)
from src.runtime_plugins.memory_mutations import (DERIVED_VECTOR_PURGE, MEMORY_JOB_KIND, MUTATION_METHODS,
                                                  MemoryMutationBlocked, _ADMISSION_SEAL, _MemoryAdmission,
                                                  _mint_admission, validate_mutation_candidate, wire_inputs)
from src.runtime_plugins.service_jobs import admit_service_job, claim_service_job, open_invocation
from tests.test_cordis_services_core import _operator, live_host, needs_host, service_db  # noqa: F401

SOURCE_CAPABILITY = "workflow.goal-snapshot-to-file"
BACKEND = Path(__file__).resolve().parents[1]


def _now():
    return datetime.now(timezone.utc)


async def _verified_source(operator, *, prefix="m1064"):
    """The smallest durable proof the existing M5 verifier accepts."""
    principal, session = operator.principal.principal_id, operator.session_id
    inputs = {"file_path": "reports/m1064-source.md"}
    async with get_session() as db:
        if await db.get(Session, session) is None:
            db.add(Session(id=session, owner_principal_id=principal))
        goal = Goal(id=f"{prefix}-goal", title="Verified goal", revision=1, status="active",
                    owner_principal_id=principal, owner_session_id=session)
        task = WorkBoardTask(
            task_id=f"{prefix}-task", owner_principal_id=principal, owner_session_id=session,
            origin_session_id=session, goal_id=goal.id, goal_revision=1, title="Comparable task",
            body="Bounded context", capability_id=SOURCE_CAPABILITY, typed_input_ref="input:m1064",
            typed_input_digest=m5.m5_digest({"version": m5.M5_DIGEST_VERSION, "inputs": inputs}),
            executor_id=f"seraph-work-board:{SOURCE_CAPABILITY}", idempotency_key=f"{prefix}-key",
            task_revision=1, status=WorkBoardStatus.done, completed_at=_now())
        attempt = WorkBoardAttempt(
            attempt_id=f"{prefix}-attempt", task_id=task.task_id, workflow_run_id=f"{prefix}-run",
            task_revision_at_claim=1, lease_owner="seraph-work-board", fencing_token=4, started_at=_now(),
            ended_at=_now(), outcome="succeeded",
            receipt_refs_json=json.dumps([{
                "receipt_kind": "readback", "status": "succeeded", "verified": True,
                "workflow_run_id": f"{prefix}-run", "readback_id": "readback:m1064",
                "artifact_id": "artifact:m1064", "content_sha256": "a" * 64, "verified_at": _now().isoformat()}]))
        run = WorkflowRunState(
            run_identity=f"{prefix}-run", root_run_identity=f"{prefix}-run", workflow_name="goal-snapshot-to-file",
            tool_name="goal-snapshot-to-file", session_id=session, operator_session_id=session, owner_kind="user",
            owner_principal_id=principal, goal_id=goal.id, goal_revision=1, status="succeeded",
            job_kind=SOURCE_CAPABILITY, capability_version="1", idempotency_scope="work-board-attempt",
            idempotency_key=f"{task.task_id}:{prefix}-attempt",
            arguments_json=json.dumps({"redacted": True, "shape": "dict", "keys": ["file_path"]}),
            input_digest="b" * 64, run_fingerprint="c" * 64, started_at=_now(), finished_at=_now())
        for row in (goal, task, run, attempt):
            db.add(row)
            await db.flush()
    return {"task_id": task.task_id, "attempt_id": attempt.attempt_id, "expected_task_revision": 1}


async def _proposal(proposal_id):
    async with get_session() as db:
        return await db.get(MemoryProposal, proposal_id, populate_existing=True)


async def _memory(memory_id, *, session_id, content="private memory text 1064"):
    async with get_session() as db:
        db.add(Memory(id=memory_id, content=content, summary=content, source_session_id=session_id))


async def _propose(host, jobs, operator, *, key="p-1", prefix="m1064"):
    source = await _verified_source(operator, prefix=prefix)
    value = await execute_memory_mutation(operator=operator, method="memory.propose", request=source,
                                          idempotency_key=key, host=host, jobs=jobs)
    proposal = await _proposal(value["result"]["value"]["proposal_ref"])
    return value, proposal


async def _job_rows():
    async with get_session() as db:
        rows = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == MEMORY_JOB_KIND))).scalars().all()
    return [json.dumps({column: str(getattr(row, column)) for column in WorkflowRunState.model_fields})
            for row in rows]


# ------------------------------------------------------------------ static

def test_mutations_are_supported_and_no_longer_deferred():
    assert MUTATION_METHODS <= SUPPORTED_METHODS
    assert not MUTATION_METHODS & set(BLOCKED_METHODS)


def test_candidates_are_closed_and_carry_no_text():
    with pytest.raises(Exception):
        validate_mutation_candidate("memory.forget", {"schema_version": 1, "method": "memory.forget",
                                                      "memory_id": "m", "mode": "tombstone",
                                                      "acknowledge_irreversible": False})
    with pytest.raises(Exception):
        validate_mutation_candidate("memory.propose", {"schema_version": 1, "method": "memory.propose",
                                                       "task_id": "t", "attempt_id": "a",
                                                       "expected_task_revision": 1, "text": "smuggled"})
    candidate = validate_mutation_candidate("memory.forget", {
        "schema_version": 1, "method": "memory.forget", "memory_id": "m", "mode": "tombstone",
        "acknowledge_irreversible": True})
    assert wire_inputs(candidate, "job-1") == {"request_ref": "job-1"}


_ADMISSION_HANDLERS = {"api/memory.py", "runtime_plugins/memory_admission.py"}
_TOKEN_NAMES = {"_mint_admission", "_MemoryAdmission", "_ADMISSION_SEAL"}


def _imported_names(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            yield module
            for alias in node.names:
                yield f"{module}.{alias.name}"
                yield alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name


def test_only_operator_http_handlers_may_import_the_mutation_admission():
    """Agent tools, the agent loop, native tools and the Cordis path never reach it."""
    offenders = []
    for path in sorted((BACKEND / "src").rglob("*.py")):
        relative = path.relative_to(BACKEND / "src").as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set(_imported_names(tree))
        # Strings used as dynamic import targets: call arguments (importlib,
        # __import__, getattr) and subscripts (sys.modules[...]), not docstrings.
        literals = {item.value for node in ast.walk(tree)
                    for item in ([*node.args, *[keyword.value for keyword in node.keywords]]
                                 if isinstance(node, ast.Call) else [node.slice] if isinstance(node, ast.Subscript)
                                 else [])
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)}
        if relative not in _ADMISSION_HANDLERS:
            # Imports, and importlib/__import__ string literals, of the admission.
            if any(re.search(r"(^|\.)memory_admission$", name) or name.endswith("execute_memory_mutation")
                   for name in names):
                offenders.append(f"{relative}: imports the admission")
            if any(re.search(r"(^|[./])memory_admission(\.py)?$", value) or value == "execute_memory_mutation"
                   for value in literals):
                offenders.append(f"{relative}: names the admission in a string")
        # Only the admission may mint the sealed token; only memory_mutations defines it.
        if relative not in {"runtime_plugins/memory_admission.py", "runtime_plugins/memory_mutations.py"}:
            if any(name.rsplit(".", 1)[-1] in _TOKEN_NAMES for name in names) or literals & _TOKEN_NAMES:
                offenders.append(f"{relative}: reaches the admission token")
        # Agent tools and the agent loop never reach the service-job admission either.
        if relative.startswith(("tools/", "agent/", "native_tools/")):
            if any(name.endswith("service_jobs") or name.endswith("admit_service_job") for name in names):
                offenders.append(f"{relative}: imports service job admission")
    assert offenders == []


@pytest.mark.asyncio
async def test_admit_service_job_refuses_any_authority_but_the_sealed_token(service_db):
    """A plain callable cannot skip the gate, candidate binding, source role or cap."""
    operator = await _operator()
    await _memory("token-target", session_id=operator.session_id)
    candidate = {"schema_version": 1, "method": "memory.forget", "memory_id": "token-target",
                 "mode": "tombstone", "acknowledge_irreversible": True}

    async def anything(_db, _run):
        return None
    forged = _MemoryAdmission("memory.forget", operator.session_id, operator.principal.principal_id,
                              _mint_admission("memory.forget", operator, candidate).input_digest, object())
    other_method = _MemoryAdmission("memory.propose", operator.session_id, operator.principal.principal_id,
                                    "0" * 64, _ADMISSION_SEAL)
    for authority in (None, anything, lambda _db, _run: None, outstanding_admission_check, forged, other_method):
        with pytest.raises(NativeServiceBlocked) as excinfo:
            await admit_service_job(operator=operator, origin_method="memory.forget", native_branch="base",
                                    candidate=candidate, idempotency_key="token-check", reviewed_composition=None,
                                    admission_authority_check=authority)
        assert excinfo.value.reason_code == "native_memory_admission_required"
    async with get_session() as db:
        assert (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.job_kind == MEMORY_JOB_KIND))).scalars().all() == []


# ------------------------------------------------------------------ gate and candidates

@pytest.mark.asyncio
async def test_admission_gate_repeats_the_memory_route_operator_checks(service_db):
    operator = await _operator()
    principal = operator.principal
    shapes = {
        "not_an_operator": type("Fake", (), {"session_id": operator.session_id, "principal": principal})(),
        "missing_grant": dataclasses.replace(operator, principal=dataclasses.replace(principal, grants=())),
        "revoked": dataclasses.replace(operator, principal=dataclasses.replace(principal, revoked=True)),
        "session_mismatch": dataclasses.replace(operator, session_id="another-root"),
    }
    for operator_shape in shapes.values():
        with pytest.raises(NativeServiceBlocked) as excinfo:
            await execute_memory_mutation(operator=operator_shape, method="memory.forget",
                                          request={"memory_id": "m", "acknowledge_irreversible": True},
                                          idempotency_key="gate", host=object(), jobs=None)
        assert excinfo.value.reason_code == "native_memory_operator_required"
    with pytest.raises(NativeServiceBlocked) as excinfo:
        await execute_memory_mutation(operator=operator, method="memory.forget", source_role="assistant",
                                      request={"memory_id": "m", "acknowledge_irreversible": True},
                                      idempotency_key="gate-role", host=object(), jobs=None)
    assert excinfo.value.reason_code == "memory_source_role_forbidden"


@pytest.mark.asyncio
async def test_candidates_bind_current_rows_and_refuse_unreviewed_shapes(service_db):
    operator = await _operator()
    await _memory("mine", session_id=operator.session_id)
    await _memory("theirs", session_id="other-root")
    await _memory("unbound", session_id="")
    cases = [
        ("memory.forget", {"memory_id": "mine", "acknowledge_irreversible": False},
         "native_memory_forget_acknowledgement_required"),
        ("memory.forget", {"memory_id": "theirs", "acknowledge_irreversible": True}, "memory_owner_session_forbidden"),
        ("memory.forget", {"memory_id": "unbound", "acknowledge_irreversible": True}, "memory_owner_session_unbound"),
        ("memory.forget", {"memory_id": "absent", "acknowledge_irreversible": True}, "native_memory_unknown"),
        ("memory.applyReviewed", {"proposal_id": "p", "action": "accept", "expected_revision": 1,
                                  "expected_preview_text_digest": None, "expected_task_revision": 1,
                                  "expected_goal_revision": 1}, "native_memory_preview_digest_required"),
        ("memory.applyReviewed", {"proposal_id": "p", "action": "edit_accept", "expected_revision": 1,
                                  "expected_preview_text_digest": "a" * 64, "expected_task_revision": 1,
                                  "expected_goal_revision": 1}, "native_memory_review_action_unsupported"),
    ]
    for method, request, reason in cases:
        with pytest.raises(NativeServiceBlocked) as excinfo:
            await build_candidate(operator, method, request)
        assert excinfo.value.reason_code == reason


# ------------------------------------------------------------------ journey

@needs_host
@pytest.mark.asyncio
async def test_propose_reviewed_apply_and_forget_run_through_the_real_child(live_host, monkeypatch):
    host, jobs, calls = live_host
    operator = await _operator()
    invalidated = []
    monkeypatch.setattr("src.memory.snapshots.invalidate_bounded_guardian_snapshot_cache",
                        lambda: invalidated.append(True))

    start = len(calls)
    proposed, proposal = await _propose(host, jobs, operator)
    assert [call["method"] for call in calls[start:]] == ["memory.propose"]
    assert calls[start]["payload"] == {"request_ref": proposed["job"]["job_id"]}
    assert proposed["job"]["status"] == "succeeded" and proposed["effect_committed"] is True
    assert proposed["result"]["memory_status"] == "proposal_only"
    assert proposed["result"]["value"]["state"] == "proposed" and proposal.status.value == "proposed"
    assert proposed["readback"]["verified"] is True
    async with get_session() as db:
        assert await db.scalar(select(Memory).where(Memory.source_session_id == operator.session_id)) is None

    # The admission is the review: it binds the revision and preview the operator saw.
    start = len(calls)
    applied = await execute_memory_mutation(
        operator=operator, method="memory.applyReviewed", idempotency_key="a-1", host=host, jobs=jobs,
        request={"proposal_id": proposal.proposal_id, "action": "accept", "expected_revision": proposal.revision,
                 "expected_preview_text_digest": proposal.preview_text_digest,
                 "expected_task_revision": proposal.source_task_revision,
                 "expected_goal_revision": proposal.goal_revision})
    assert [call["method"] for call in calls[start:]] == ["memory.applyReviewed"]
    assert calls[start]["payload"] == {"review_ref": applied["job"]["job_id"]}
    assert applied["result"]["memory_status"] == "reviewed_update"
    memory_id = applied["result"]["value"]["record_ref"]
    accepted = await _proposal(proposal.proposal_id)
    assert accepted.status.value == "accepted" and accepted.accepted_memory_id == memory_id
    async with get_session() as db:
        memory = await db.get(Memory, memory_id)
        assert memory is not None and memory.source_session_id == operator.session_id
        db.add(MemorySnapshot(content="snapshot of private memory text 1064"))

    start = len(calls)
    forgotten = await execute_memory_mutation(
        operator=operator, method="memory.forget", idempotency_key="f-1", host=host, jobs=jobs,
        request={"memory_id": memory_id, "acknowledge_irreversible": True})
    assert [call["method"] for call in calls[start:]] == ["memory.forget"]
    assert forgotten["result"]["memory_status"] == "forgotten"
    assert forgotten["readback"]["operator_label"] == "delete_permanently"
    assert forgotten["readback"]["derived_vector_purge"] == DERIVED_VECTOR_PURGE == "not_covered"
    assert invalidated, "snapshot cache must be invalidated after the tombstone commits"
    async with get_session() as db:
        tombstone = await db.scalar(select(MemoryTombstone).where(MemoryTombstone.memory_id == memory_id))
        memory = await db.get(Memory, memory_id, populate_existing=True)
        snapshots = (await db.execute(select(MemorySnapshot))).scalars().all()
        audit = (await db.execute(select(AuditEvent).where(AuditEvent.event_type == "memory_forgotten"))).scalars().all()
    assert tombstone is not None and forgotten["result"]["value"]["receipt_ref"] == tombstone.id
    assert memory.status.value == "archived" and "private memory" not in memory.content
    # A tombstone clears every bounded snapshot row, not only this memory's.
    assert snapshots and all(item.content == "" for item in snapshots)
    assert len(audit) == 1 and json.loads(audit[0].details_json)["derived_vector_purge"] == "not_covered"

    # Plaintext never rests in job rows: neither the preview nor its plain digest.
    preview = proposal.preview_text
    rows = await _job_rows()
    assert len(rows) == 3
    for row in rows:
        assert preview not in row and proposal.preview_text_digest not in row
        assert hashlib.sha256(preview.encode()).hexdigest() not in row

    # Exact retries reuse the job and never re-execute.
    start = len(calls)
    retry = await execute_memory_mutation(operator=operator, method="memory.forget", idempotency_key="f-1",
                                          host=host, jobs=jobs,
                                          request={"memory_id": memory_id, "acknowledge_irreversible": True})
    assert retry["replayed"] is True and len(calls) == start


@needs_host
@pytest.mark.parametrize("change", ["preview_text", "preferred_capability", "correction_target"])
@pytest.mark.asyncio
async def test_changed_review_binding_is_refused_and_nothing_is_applied(live_host, monkeypatch, change):
    host, jobs, _calls = live_host
    operator = await _operator()
    _, proposal = await _propose(host, jobs, operator)
    await _memory("late-correction-target", session_id=operator.session_id)
    request = {"proposal_id": proposal.proposal_id, "action": "accept", "expected_revision": proposal.revision,
               "expected_preview_text_digest": proposal.preview_text_digest,
                 "expected_task_revision": proposal.source_task_revision,
                 "expected_goal_revision": proposal.goal_revision}
    stale = await build_candidate(operator, "memory.applyReviewed", request)
    # A reviewed field changes after the operator reviewed it, same revision.
    values = {
        "preview_text": {"preview_text": "a different preview"},
        "preferred_capability": {"memory_scope_json": json.dumps(
            {**json.loads(proposal.memory_scope_json or "{}"), "preferred_capability_id": "capability.changed"})},
        "correction_target": {"corrects_memory_id": "late-correction-target"},
    }[change]
    async with get_session() as db:
        await db.execute(update(MemoryProposal).where(MemoryProposal.proposal_id == proposal.proposal_id)
                         .values(**values))

    async def reviewed(*_args, **_kwargs):
        return stale
    monkeypatch.setattr(memory_admission, "build_candidate", reviewed)
    with pytest.raises(NativeServiceBlocked) as excinfo:
        await execute_memory_mutation(operator=operator, method="memory.applyReviewed", request=request,
                                      idempotency_key=f"a-stale-{change}", host=host, jobs=jobs)
    assert excinfo.value.reason_code == "native_memory_proposal_changed"
    assert isinstance(excinfo.value, MemoryMutationBlocked) and excinfo.value.effect_committed is False
    current = await _proposal(proposal.proposal_id)
    assert current.status.value == "proposed" and current.accepted_memory_id is None
    rows = await _job_rows()
    assert any('"status": "blocked"' in row for row in rows)


@needs_host
@pytest.mark.asyncio
async def test_unseen_correction_target_is_refused_at_admission(live_host):
    host, jobs, _calls = live_host
    operator = await _operator()
    _, proposal = await _propose(host, jobs, operator)
    await _memory("corrected-row", session_id=operator.session_id)
    async with get_session() as db:
        await db.execute(update(MemoryProposal).where(MemoryProposal.proposal_id == proposal.proposal_id)
                         .values(corrects_memory_id="corrected-row"))
    with pytest.raises(NativeServiceBlocked) as excinfo:
        await build_candidate(operator, "memory.applyReviewed", {
            "proposal_id": proposal.proposal_id, "action": "accept", "expected_revision": proposal.revision,
            "expected_preview_text_digest": proposal.preview_text_digest,
                 "expected_task_revision": proposal.source_task_revision,
                 "expected_goal_revision": proposal.goal_revision})
    assert excinfo.value.reason_code == "native_memory_correction_target_unseen"


@needs_host
@pytest.mark.parametrize("field, reason", [("expected_task_revision", "native_memory_stale_task_revision"),
                                           ("expected_goal_revision", "native_memory_stale_goal_revision")])
@pytest.mark.asyncio
async def test_stale_task_or_goal_revision_is_refused_at_admission(live_host, field, reason):
    host, jobs, _calls = live_host
    operator = await _operator()
    _, proposal = await _propose(host, jobs, operator)
    request = {"proposal_id": proposal.proposal_id, "action": "accept", "expected_revision": proposal.revision,
               "expected_preview_text_digest": proposal.preview_text_digest,
               "expected_task_revision": proposal.source_task_revision,
               "expected_goal_revision": proposal.goal_revision}
    request[field] += 1
    with pytest.raises(NativeServiceBlocked) as excinfo:
        await build_candidate(operator, "memory.applyReviewed", request)
    assert excinfo.value.reason_code == reason
    missing = {key: value for key, value in request.items() if key != field}
    with pytest.raises(NativeServiceBlocked) as excinfo:
        await build_candidate(operator, "memory.applyReviewed", missing)
    assert excinfo.value.reason_code == "native_memory_request_invalid"


@needs_host
@pytest.mark.asyncio
async def test_signing_outage_commits_the_recovery_state_and_seals_a_block(live_host, monkeypatch):
    from src.extensions.capability_execution import CapabilityJournalError
    host, jobs, _calls = live_host
    operator = await _operator()
    _, proposal = await _propose(host, jobs, operator)

    async def outage(*_args, **_kwargs):
        raise CapabilityJournalError("signing key unavailable")
    monkeypatch.setattr(m5, "_canonical_accept", outage)
    with pytest.raises(NativeServiceBlocked) as excinfo:
        await execute_memory_mutation(
            operator=operator, method="memory.applyReviewed", idempotency_key="a-outage", host=host, jobs=jobs,
            request={"proposal_id": proposal.proposal_id, "action": "accept", "expected_revision": proposal.revision,
                     "expected_preview_text_digest": proposal.preview_text_digest,
                 "expected_task_revision": proposal.source_task_revision,
                 "expected_goal_revision": proposal.goal_revision})
    assert excinfo.value.reason_code == "native_memory_accepted_binding_unavailable"
    # The owner's recovery state is committed: distinguishable from a no-op.
    assert isinstance(excinfo.value, MemoryMutationBlocked) and excinfo.value.effect_committed is True
    current = await _proposal(proposal.proposal_id)
    assert current.status.value == "blocked" and current.reason_code == "accepted_binding_unavailable"


@needs_host
@pytest.mark.asyncio
async def test_root_revoked_after_commit_still_completes_the_committed_effect(live_host, monkeypatch):
    host, jobs, _calls = live_host
    operator = await _operator()
    await _memory("doomed", session_id=operator.session_id)
    original = host.request_service

    async def revoke_after(method, payload, **kwargs):
        result = await original(method, payload, **kwargs)
        async with get_session() as db:
            await db.execute(update(OperatorSession).where(OperatorSession.id == operator.session_id)
                             .values(revoked_at=_now()))
        return result
    monkeypatch.setattr(host, "request_service", revoke_after)
    value = await execute_memory_mutation(operator=operator, method="memory.forget", idempotency_key="f-rev",
                                          host=host, jobs=jobs,
                                          request={"memory_id": "doomed", "acknowledge_irreversible": True})
    assert value["job"]["status"] == "succeeded" and value["effect_committed"] is True
    readbacks = [item for item in value["job"]["effects"] if item["receipt_kind"] == "readback"]
    assert len(readbacks) == 1 and readbacks[0]["details"]["effect_committed"] is True


@needs_host
@pytest.mark.asyncio
async def test_child_lost_after_commit_completes_from_readback_without_replay(live_host, monkeypatch):
    host, jobs, calls = live_host
    operator = await _operator()
    await _memory("lost-answer", session_id=operator.session_id)
    original = host.request_service

    async def lost(method, payload, **kwargs):
        await original(method, payload, **kwargs)
        raise HostBlocked("child_exited")
    monkeypatch.setattr(host, "request_service", lost)
    start = len(calls)
    value = await execute_memory_mutation(operator=operator, method="memory.forget", idempotency_key="f-lost",
                                          host=host, jobs=jobs,
                                          request={"memory_id": "lost-answer", "acknowledge_irreversible": True})
    assert value["job"]["status"] == "succeeded" and value["readback"]["verified"] is True
    assert len(calls) == start + 1
    async with get_session() as db:
        tombstones = (await db.execute(select(MemoryTombstone).where(
            MemoryTombstone.memory_id == "lost-answer"))).scalars().all()
    assert len(tombstones) == 1


@needs_host
@pytest.mark.asyncio
async def test_callback_refs_must_name_the_job_and_a_second_callback_is_denied(live_host):
    host, jobs, _calls = live_host
    operator = await _operator()
    await _memory("ref-check", session_id=operator.session_id)
    candidate = await build_candidate(operator, "memory.forget",
                                      {"memory_id": "ref-check", "acknowledge_irreversible": True})
    job, replayed = await admit_service_job(
        operator=operator, origin_method="memory.forget", native_branch="base", candidate=candidate,
        idempotency_key="f-ref", reviewed_composition=host.reviewed, jobs=jobs,
        admission_authority_check=_mint_admission("memory.forget", operator, candidate))
    owner, fence = await claim_service_job(job, host=host, jobs=jobs)
    scope = await open_invocation(job_id=job["job_id"], owner=owner, fence=fence, host=host,
                                  admission_replayed=replayed, candidate=candidate, jobs=jobs)
    wrong = await host.request_service("memory.forget", {"request_ref": "some-other-job"}, original_scope=scope)
    assert wrong == {"status": "blocked", "reason_code": "native_memory_reference_not_bound",
                     "memory_status": "no_learning"}
    first = await host.request_service("memory.forget", {"request_ref": job["job_id"]}, original_scope=scope)
    assert first["status"] == "succeeded"
    second = await host.request_service("memory.forget", {"request_ref": job["job_id"]}, original_scope=scope)
    assert second["reason_code"] == "native_memory_replay_denied"


@needs_host
@pytest.mark.asyncio
async def test_outstanding_cap_is_per_root_and_per_principal_and_replays_bypass_it(live_host):
    host, jobs, _calls = live_host
    operator = await _operator()
    for index in range(5):
        await _memory(f"cap-{index}", session_id=operator.session_id)

    async def admit(op, index, key=None):
        candidate = {"schema_version": 1, "method": "memory.forget", "memory_id": f"cap-{index}",
                     "mode": "tombstone", "acknowledge_irreversible": True}
        return await admit_service_job(operator=op, origin_method="memory.forget", native_branch="base",
                                       candidate=candidate, idempotency_key=key or f"cap-{index}",
                                       reviewed_composition=host.reviewed, jobs=jobs,
                                       admission_authority_check=_mint_admission("memory.forget", op, candidate))
    for index in range(4):
        await admit(operator, index)
    with pytest.raises(Exception) as excinfo:
        await admit(operator, 4)
    assert getattr(excinfo.value, "reason", None) == "native_memory_outstanding_limit"
    # An exact replay of an admitted job is not a new outstanding job.
    replayed_job, replayed = await admit(operator, 0)
    assert replayed is True
    # Re-login rotates the Root, but the principal-wide cap still holds.
    from types import SimpleNamespace
    from src.workflows.job_runtime import DurableJobAdmissionDenied
    async with get_session() as db:
        with pytest.raises(DurableJobAdmissionDenied) as excinfo:
            await outstanding_admission_check(db, SimpleNamespace(
                operator_session_id="a-rotated-root", owner_principal_id=operator.principal.principal_id))
        assert excinfo.value.reason == "native_memory_outstanding_limit"
        # A different principal on a fresh Root is unaffected.
        await outstanding_admission_check(db, SimpleNamespace(
            operator_session_id="a-rotated-root", owner_principal_id="operator:root:someone-else"))
    # Expired outstanding jobs no longer count.
    async with get_session() as db:
        await db.execute(update(WorkflowRunState).where(WorkflowRunState.job_kind == MEMORY_JOB_KIND)
                         .values(deadline_at=_now() - timedelta(seconds=1)))
    job, _ = await admit(operator, 4, key="cap-after-expiry")
    assert job["status"] in {"accepted", "failed"}


@needs_host
@pytest.mark.asyncio
async def test_apply_racing_a_forget_of_its_correction_target_is_refused(live_host, monkeypatch):
    host, jobs, _calls = live_host
    operator = await _operator()
    _, proposal = await _propose(host, jobs, operator)
    await _memory("correction-target", session_id=operator.session_id)
    request = {"proposal_id": proposal.proposal_id, "action": "accept", "expected_revision": proposal.revision,
               "expected_preview_text_digest": proposal.preview_text_digest,
                 "expected_task_revision": proposal.source_task_revision,
                 "expected_goal_revision": proposal.goal_revision,
               "corrects_memory_id": "correction-target"}
    reviewed = await build_candidate(operator, "memory.applyReviewed", request)
    # The forget commits after the operator reviewed the correction, before the apply runs.
    await execute_memory_mutation(operator=operator, method="memory.forget", idempotency_key="f-target",
                                  host=host, jobs=jobs,
                                  request={"memory_id": "correction-target", "acknowledge_irreversible": True})

    async def stale(*_args, **_kwargs):
        return reviewed
    monkeypatch.setattr(memory_admission, "build_candidate", stale)
    with pytest.raises(NativeServiceBlocked) as excinfo:
        await execute_memory_mutation(operator=operator, method="memory.applyReviewed", request=request,
                                      idempotency_key="a-target", host=host, jobs=jobs)
    # The forget does not touch the proposal; the apply writer's own
    # correction-target guard refuses the archived, tombstoned row.
    assert excinfo.value.reason_code == "native_memory_correction_target_unavailable"
    current = await _proposal(proposal.proposal_id)
    assert current.status.value == "proposed" and current.accepted_memory_id is None


@needs_host
@pytest.mark.asyncio
async def test_forget_during_an_in_flight_read_withdraws_the_read(live_host, monkeypatch):
    from src.runtime_plugins.service_jobs import execute_native_read
    host, jobs, _calls = live_host
    operator = await _operator()
    await _memory("in-flight", session_id=operator.session_id, content="cafe in flight 1064")
    original = host.request_service
    forgotten = []

    async def forget_mid_read(method, payload, **kwargs):
        result = await original(method, payload, **kwargs)
        if method == "memory.retrieve" and not forgotten:
            forgotten.append(await execute_memory_mutation(
                operator=operator, method="memory.forget", idempotency_key="f-in-flight", host=host, jobs=jobs,
                request={"memory_id": "in-flight", "acknowledge_irreversible": True}))
        return result
    monkeypatch.setattr(host, "request_service", forget_mid_read)
    with pytest.raises(NativeServiceBlocked) as excinfo:
        await execute_native_read(operator=operator, idempotency_key="read-in-flight", host=host, jobs=jobs,
                                  candidate={"schema_version": 1, "method": "memory.retrieve", "query": "cafe",
                                             "limit": 5, "status": "active"})
    assert forgotten and excinfo.value.reason_code == "native_memory_record_withdrawn"



@needs_host
@pytest.mark.asyncio
async def test_accept_readback_requires_the_accepted_status_and_sealed_acceptance(live_host, monkeypatch):
    """A committed accept whose canonical rows no longer match blocks, effect_committed."""
    host, jobs, _calls = live_host
    operator = await _operator()
    _, proposal = await _propose(host, jobs, operator)
    original = host.request_service

    async def tamper_after_commit(method, payload, **kwargs):
        result = await original(method, payload, **kwargs)
        async with get_session() as db:
            await db.execute(update(MemoryProposal).where(MemoryProposal.proposal_id == proposal.proposal_id)
                             .values(acceptance_binding_digest="f" * 64))
        return result
    monkeypatch.setattr(host, "request_service", tamper_after_commit)
    with pytest.raises(MemoryMutationBlocked) as excinfo:
        await execute_memory_mutation(
            operator=operator, method="memory.applyReviewed", idempotency_key="a-tamper", host=host, jobs=jobs,
            request={"proposal_id": proposal.proposal_id, "action": "accept", "expected_revision": proposal.revision,
                     "expected_preview_text_digest": proposal.preview_text_digest,
                     "expected_task_revision": proposal.source_task_revision,
                     "expected_goal_revision": proposal.goal_revision})
    assert excinfo.value.reason_code == "native_memory_readback_mismatch"
    assert excinfo.value.effect_committed is True
    async with get_session() as db:
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == excinfo.value.job_id))).scalar_one()
    assert run.status == "blocked" and run.failure_reason == "native_memory_readback_mismatch"


@needs_host
@pytest.mark.asyncio
async def test_child_mismatch_after_commit_reports_effect_committed(live_host, monkeypatch):
    host, jobs, _calls = live_host
    operator = await _operator()
    await _memory("mismatch", session_id=operator.session_id)
    original = host.request_service

    async def fabricated(method, payload, **kwargs):
        await original(method, payload, **kwargs)
        return {"status": "succeeded", "memory_status": "forgotten",
                "value": {"record_ref": "mismatch", "receipt_ref": "fabricated"}}
    monkeypatch.setattr(host, "request_service", fabricated)
    with pytest.raises(MemoryMutationBlocked) as excinfo:
        await execute_memory_mutation(operator=operator, method="memory.forget", idempotency_key="f-mismatch",
                                      host=host, jobs=jobs,
                                      request={"memory_id": "mismatch", "acknowledge_irreversible": True})
    assert excinfo.value.reason_code == "native_child_result_mismatch"
    assert excinfo.value.effect_committed is True


@needs_host
@pytest.mark.asyncio
async def test_a_replayed_proposal_writes_no_second_creation_audit(live_host):
    host, jobs, _calls = live_host
    operator = await _operator()
    first, proposal = await _propose(host, jobs, operator, key="p-first")
    source = {"task_id": "m1064-task", "attempt_id": "m1064-attempt", "expected_task_revision": 1}
    replay = await execute_memory_mutation(operator=operator, method="memory.propose", request=source,
                                           idempotency_key="p-second", host=host, jobs=jobs)
    assert replay["result"]["value"]["proposal_ref"] == proposal.proposal_id
    async with get_session() as db:
        created = (await db.execute(select(AuditEvent).where(
            AuditEvent.event_type == "memory_proposal_created"))).scalars().all()
    assert len(created) == 1


def test_core_reasons_never_carry_exception_text():
    from src.runtime_plugins.memory_mutations import _core_reason
    assert _core_reason(ValueError("stale_task_revision")) == "native_memory_stale_task_revision"
    assert _core_reason(ValueError("private text 'cafe' leaked")) == "native_memory_mutation_rejected"
    assert _core_reason(ValueError("unknown_code_x")) == "native_memory_mutation_rejected"
    assert _core_reason(PermissionError("anything")) == "native_memory_owner_forbidden"
