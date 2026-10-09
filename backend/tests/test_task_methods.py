"""Canonical method owner mechanics; these are not consumer capability proof."""
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.test_task_lessons import failed_local_task, no_inference
from src.memory import task_methods as methods
from src.memory.task_lessons import LessonScope, create_task_lesson
from src.db.models import Memory, MemoryProposal, MemoryTombstone
from src.db.task_method_models import TaskMethodActive
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardError

pytestmark = pytest.mark.parametrize("async_db", ["file"], indirect=True)


def review(preview, action, key):
    return methods.TaskMethodReview(proposal_id=preview["proposal_id"], expected_revision=preview["expected_revision"],
        artifact_digest=preview["artifact_digest"], scope_digest=preview["scope_digest"], action=action,
        reason="Future selection rollback" if action == "rollback" else "Reviewed typed source guard", idempotency_key=key)


@pytest.mark.asyncio
async def test_actual_local_failure_adoption_signed_pin_rollback_and_tombstone(async_db, monkeypatch, tmp_path, no_inference):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    from src.auth.ownership import enroll
    await enroll(operator)
    request = request.model_copy(update={"scope": LessonScope(goal_id="goal", goal_revision=1, family="general")})
    proposed = await create_task_lesson(operator, request)
    current = methods.CurrentMethod()
    await current.start()
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    assert (await current.resolve(owner, "goal", "work.general-task.v1")).status == "none"
    preview = await methods.inspect_method(operator, proposed["proposal_id"])
    action = review(preview, "accept", "adopt-exact-method")
    adopted = await methods.review_method(operator, action)
    binding = await current.resolve(owner, "goal", "work.general-task.v1")
    assert binding.status == "active", binding.reason
    assert binding.method_id == proposed["proposal_id"]
    assert binding.typed_data["steps"][0] == {"kind": "guard", "check": "source_exists"}
    staged_key = current._key
    current._key = None
    denied_key = await current.resolve(owner, "goal", "work.general-task.v1")
    assert denied_key.status == "blocked" and denied_key.reason == "method_signing_key_unavailable"
    with pytest.raises(BoardError) as denied_pin:
        await current.validate_pinned(owner, "goal", binding)
    assert denied_pin.value.code == "method_pin_identity_or_key_unavailable"
    current._key = staged_key
    assert (await methods.review_method(operator, action))["idempotent_replay"] is True
    async with async_db() as db:
        row = await db.get(MemoryProposal, binding.method_id)
        memory = await db.get(Memory, binding.version)
        pointer = (await db.execute(select(TaskMethodActive))).scalar_one()
        signed = json.loads(memory.metadata_json)["work_board_provenance"]["memory_scope"]
        assert signed["candidate_version"] == memory.id == row.accepted_memory_id
        assert json.loads(pointer.binding_json)["version"] == memory.id
        assert methods._pointer_valid(pointer, current._key)
    rollback = review(await methods.inspect_method(operator, binding.method_id), "rollback", "rollback-exact-method")
    await methods.review_method(operator, rollback)
    assert (await current.resolve(owner, "goal", "work.general-task.v1")).status == "none"
    assert await current.validate_pinned(owner, "goal", binding) == binding
    async with async_db() as db:
        db.add(MemoryTombstone(memory_id=binding.version))
    with pytest.raises(BoardError) as denied:
        await current.validate_pinned(owner, "goal", binding)
    assert denied.value.code == "method_version_unavailable"
    await current.stop()


@pytest.mark.asyncio
async def test_pointer_tamper_blocks_without_baseline_and_generic_recall_excludes_method(async_db, monkeypatch, tmp_path, no_inference):
    operator, request = await failed_local_task(async_db, monkeypatch, tmp_path)
    from src.auth.ownership import enroll
    await enroll(operator)
    request = request.model_copy(update={"scope": LessonScope(goal_id="goal", goal_revision=1, family="general")})
    proposed = await create_task_lesson(operator, request)
    await methods.review_method(operator, review(await methods.inspect_method(operator, proposed["proposal_id"]), "accept", "adopt"))
    current = methods.CurrentMethod()
    await current.start()
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    from src.memory.repository import memory_repository
    assert await memory_repository.list_memories(for_model_context=True) == []
    assert await memory_repository.list_memories_for_reindex() == []
    original = await current.resolve(owner, "goal", "work.general-task.v1")
    from src.db.models import MemoryKind
    from src.memory.snapshots import render_bounded_guardian_snapshot
    async with async_db() as db:
        memory = await db.get(Memory, original.version)
        kind, original_metadata, content = memory.kind, memory.metadata_json, memory.content
        memory.kind, memory.metadata_json = MemoryKind.goal, "{}"
        db.add(memory)
    # The canonical proposal link keeps accepted method content out of model
    # snapshots even if untrusted kind and namespace metadata are rewritten.
    snapshot, _ = await render_bounded_guardian_snapshot(soul_context="")
    assert content not in snapshot and "source_exists" not in snapshot
    assert await memory_repository.list_memories(for_model_context=True) == []
    assert await memory_repository.list_memories_for_reindex() == []
    async with async_db() as db:
        memory = await db.get(Memory, original.version)
        memory.kind, memory.metadata_json = kind, original_metadata
        db.add(memory)
    async with async_db() as db:
        memory = await db.get(Memory, original.version)
        metadata = memory.metadata_json
        memory.metadata_json = "{invalid"
        db.add(memory)
    with pytest.raises(BoardError) as denied_metadata:
        await current.validate_pinned(owner, "goal", original)
    assert denied_metadata.value.code == "method_pin_metadata_invalid"
    async with async_db() as db:
        memory = await db.get(Memory, original.version)
        memory.metadata_json = metadata
        db.add(memory)
    async with async_db() as db:
        pointer = (await db.execute(select(TaskMethodActive))).scalar_one()
        pointer.baseline, pointer.binding_json = True, "null"
        db.add(pointer)
    blocked = await current.resolve(owner, "goal", "work.general-task.v1")
    assert blocked.status == "blocked" and blocked.reason == "method_pointer_invalid"
    assert (await current.resolve(owner, "goal", "work.research-dossier.v1")).status == "none"
    await current.stop()


@pytest.mark.asyncio
async def test_real_unverified_root_general_card_preserves_proven_absent_baseline_and_history_blocks(async_db, monkeypatch, tmp_path, no_inference):
    operator, source = await failed_local_task(async_db, monkeypatch, tmp_path)
    from src.native_tools.registry import ToolRegistry
    from src.work_board.general_task import GeneralTaskService
    from src.work_board.dispatcher import _parse_typed_input
    from tests.test_general_task_schema import native_request
    current = methods.CurrentMethod()
    await current.start()
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    registry = ToolRegistry()
    registry.start()
    service = GeneralTaskService(registry, strategy_resolver=methods.TaskMethodStrategyResolver(current))
    service.start()
    value = native_request(registry, key="ordinary-no-method")
    value = value.model_copy(update={"accept": False, "input": value.input.model_copy(update={"goal_ref": "goal"})})
    async with async_db() as db:
        created = await service.create(db, owner, value)
        assert created.task.status.value == "triage"
        assert _parse_typed_input(created.task)["strategy"]["status"] == "none"
        assert (await current.resolve(owner, "goal", "work.general-task.v1")).status == "none"
    assert not (tmp_path / "workspace" / "schema-result.txt").exists()
    # Key absence may preserve only this demonstrated never-selected baseline.
    current._key = None
    assert (await current.resolve(owner, "goal", "work.general-task.v1")).status == "none"
    async with async_db() as db:
        db.add(MemoryProposal(schema_version="task_method_proposal.v1", owner_principal_id=owner.principal_id,
            owner_session_id=owner.session_id, goal_id="goal", goal_revision=1,
            source_task_id="negative-history-source", source_attempt_id="negative-history-attempt",
            capability_id="negative-history-capability",
            accepted_memory_id="INVALID_HISTORY_MUST_NOT_BECOME_BASELINE", memory_scope_json="{malformed"))
    blocked = await current.resolve(owner, "goal", "work.general-task.v1")
    assert blocked.status == "blocked" and blocked.reason == "method_stable_identity_required"
    await current.stop()
    service.stop()
    registry.stop()


@pytest.mark.asyncio
async def test_structured_research_producer_rejects_actual_unsupported_failed_source(async_db, monkeypatch, tmp_path, no_inference):
    operator, source = await failed_local_task(async_db, monkeypatch, tmp_path)
    from src.memory.task_lessons import ResearchMethodRequest, ResearchStrategy, create_research_method
    strategy = ResearchStrategy(query_templates=["{goal}"], source_preferences=["primary", "dated"],
        required_evidence_fields=["url", "claim", "limitation"], draft_sections=["Evidence", "Limitations"],
        stop_conditions=["Stop after the admitted source limit"])
    request = ResearchMethodRequest(task_id=source.task_id, attempt_id=source.attempt_id,
        source_refs=source.source_refs, scope=LessonScope(goal_id="goal", goal_revision=1, family="research"),
        expected_revision=source.expected_revision, strategy=strategy)
    with pytest.raises(BoardError) as denied:
        await create_research_method(operator, request)
    assert denied.value.code == "research_method_source_unsupported"
    async with async_db() as db:
        assert list((await db.execute(select(MemoryProposal))).scalars()) == []
    assert not list((tmp_path / "workspace" / "artifacts" / "memory" / "task-lessons").glob("*.json"))


@pytest.mark.asyncio
async def test_real_source_distinct_immutable_versions_capacity_and_pointer_cas(async_db, monkeypatch, tmp_path, no_inference):
    operator, source = await failed_local_task(async_db, monkeypatch, tmp_path)
    from src.auth.ownership import enroll
    await enroll(operator)
    source = source.model_copy(update={"scope": LessonScope(goal_id="goal", goal_revision=1, family="general")})
    assert methods.MAX_VERSIONS == 16
    # Exercise the exact writer boundary with distinct observed guard proposals;
    # ordinary correction grammar has three variants, so use a ceiling of two.
    monkeypatch.setattr(methods, "MAX_VERSIONS", 2)
    versions = []
    delayed = None
    corrections = ["Check source file exists before execution", "Require verified readback", "Preserve source attribution"]
    for index, correction in enumerate(corrections):
        candidate = await create_task_lesson(operator, source.model_copy(update={
            "correction": correction}))
        preview = await methods.inspect_method(operator, candidate["proposal_id"])
        if index == 0:
            delayed = review(preview, "accept", "delayed-review")
        if index == 2:
            with pytest.raises(BoardError) as denied:
                await methods.review_method(operator, review(preview, "accept", "seventeenth-review"))
            assert denied.value.code == "method_version_capacity_requires_review"
        else:
            await methods.review_method(operator, review(preview, "accept", f"review-{index}"))
            async with async_db() as db:
                row = await db.get(MemoryProposal, candidate["proposal_id"])
                versions.append(row.accepted_memory_id)
    assert len(set(versions)) == 2
    with pytest.raises(BoardError) as stale:
        await methods.review_method(operator, delayed)
    assert stale.value.code == "method_review_stale"
    async with async_db() as db:
        assert len(list((await db.execute(select(Memory))).scalars())) == 2
        pointer = (await db.execute(select(TaskMethodActive))).scalar_one()
        assert json.loads(pointer.binding_json)["version"] == versions[-1]
    duplicate = await create_task_lesson(operator, source.model_copy(update={
        "correction": "Check source file exists before execution; same typed candidate"}))
    with pytest.raises(BoardError) as denied_duplicate:
        await methods.review_method(operator, review(await methods.inspect_method(operator, duplicate["proposal_id"]),
            "accept", "duplicate-review"))
    assert denied_duplicate.value.code == "method_candidate_duplicate"


@pytest.mark.asyncio
@pytest.mark.parametrize("key_failure", [False, True])
async def test_actual_app_lifecycle_stages_method_key_before_consumer_and_stops(client, monkeypatch, tmp_path, no_inference, key_failure):
    from contextlib import contextmanager
    from unittest.mock import AsyncMock
    import src.app as application
    from src.memory import repository
    from src.work_board import general_task
    from tests.test_app import _isolate_unrelated_app_startup
    _isolate_unrelated_app_startup(application, monkeypatch, tmp_path)
    monkeypatch.setattr(application, "cordis_host", type("Host", (), {"start": AsyncMock(), "stop": AsyncMock()})())
    if key_failure:
        def unavailable_key():
            raise OSError("test-only unavailable private key")
        monkeypatch.setattr(repository, "_effect_mac_key", unavailable_key)
    original = general_task.current_task_service
    entered = []
    @contextmanager
    def guarded_consumer(*args, **kwargs):
        assert methods.current_method._started
        assert (methods.current_method._key is None) == key_failure
        entered.append(True)
        with original(*args, **kwargs) as service:
            yield service
    monkeypatch.setattr(general_task, "current_task_service", guarded_consumer)
    async with application.lifespan(client._transport.app):
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert methods.current_method._started
    assert entered == [True]
    assert not methods.current_method._started and methods.current_method._key is None


@pytest.mark.asyncio
async def test_current_root_revocation_denies_new_selection_and_original_pin(async_db, monkeypatch, tmp_path, no_inference):
    from datetime import datetime, timezone
    from src.auth.ownership import enroll
    from src.db.models import OperatorSession
    operator, source = await failed_local_task(async_db, monkeypatch, tmp_path)
    await enroll(operator)
    source = source.model_copy(update={"scope": LessonScope(goal_id="goal", goal_revision=1, family="general")})
    proposed = await create_task_lesson(operator, source)
    await methods.review_method(operator, review(await methods.inspect_method(operator, proposed["proposal_id"]), "accept", "adopt"))
    current = methods.CurrentMethod()
    await current.start()
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    original = await current.resolve(owner, "goal", "work.general-task.v1")
    assert original.status == "active"
    async with async_db() as db:
        root = await db.get(OperatorSession, operator.session_id)
        root.revoked_at = datetime.now(timezone.utc)
        db.add(root)
    assert (await current.resolve(owner, "goal", "work.general-task.v1")).status == "blocked"
    with pytest.raises(BoardError) as denied:
        await current.validate_pinned(owner, "goal", original)
    assert denied.value.code == "method_current_owner_required"
    await current.stop()


@pytest.mark.asyncio
async def test_unavailable_private_method_key_returns_finite_review_recovery(async_db, monkeypatch, tmp_path, no_inference):
    from src.auth.ownership import enroll
    from src.memory import repository
    operator, source = await failed_local_task(async_db, monkeypatch, tmp_path)
    await enroll(operator)
    proposed = await create_task_lesson(operator, source.model_copy(update={
        "scope": LessonScope(goal_id="goal", goal_revision=1, family="general")}))
    def unavailable():
        raise methods.CapabilityJournalError("private key unavailable")
    monkeypatch.setattr(repository, "_effect_mac_key", unavailable)
    with pytest.raises(BoardError) as denied:
        await methods.inspect_method(operator, proposed["proposal_id"])
    assert denied.value.code == "method_review_store_unavailable" and denied.value.status_code == 503
    async with async_db() as db:
        assert list((await db.execute(select(Memory))).scalars()) == []
        assert list((await db.execute(select(TaskMethodActive))).scalars()) == []


@pytest.mark.asyncio
async def test_existing_unsupported_family_candidate_can_be_rejected_without_activation(async_db, monkeypatch, tmp_path, no_inference):
    from src.auth.ownership import enroll
    operator, source = await failed_local_task(async_db, monkeypatch, tmp_path)
    await enroll(operator)
    proposed = await create_task_lesson(operator, source)
    preview = await methods.inspect_method(operator, proposed["proposal_id"])
    with pytest.raises(BoardError) as denied:
        await methods.review_method(operator, review(preview, "accept", "unsupported-accept"))
    assert denied.value.code == "method_consumer_schema_unsupported"
    rejected = await methods.review_method(operator, review(preview, "reject", "reject-data-only"))
    assert rejected["status"] == "rejected" and rejected["behavior_changed"] is False
    async with async_db() as db:
        assert list((await db.execute(select(Memory))).scalars()) == []
        assert list((await db.execute(select(TaskMethodActive))).scalars()) == []


@pytest.mark.asyncio
async def test_actual_recovered_root_api_requires_selected_goal_and_remains_read_only(client, async_db, monkeypatch, tmp_path, no_inference):
    from src.auth import ownership
    from src.auth.service import create_session
    from src.db.models import Goal
    operator, source = await failed_local_task(async_db, monkeypatch, tmp_path)
    _, _, recovery_code = await ownership.enroll(operator)
    source = source.model_copy(update={"scope": LessonScope(goal_id="goal", goal_revision=1, family="general")})
    proposed = await create_task_lesson(operator, source)
    original_preview = await methods.inspect_method(operator, proposed["proposal_id"])
    token, recovered = await create_session(recovery_code=recovery_code)
    from config.settings import settings
    headers = {"cookie": f"{settings.operator_auth_cookie_name}={token}", "origin": "http://localhost:3001"}
    path = f"/api/memory/task-methods/{proposed['proposal_id']}"
    denied = await client.get(path, headers=headers)
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "method_current_owner_required", denied.text
    assert "source_exists" not in denied.text and "new_method" not in denied.text
    selection = ownership.RecoveryRequest(selections=[ownership.RecoverySelection(kind="goal", record_id="goal")])
    preview = await ownership.preview(recovered, selection)
    await ownership.confirm(recovered, ownership.RecoveryConfirmRequest(**selection.model_dump(),
        idempotency_key="selected-method-goal", preview_digest=preview["preview_digest"], acknowledge_read_only=True))
    readable = await client.get(path, headers=headers)
    assert readable.status_code == 200
    assert readable.json()["new_method"] == original_preview["new_method"]
    assert readable.json()["adoption_requires_current_owner"] is True
    for action in ("accept", "reject", "rollback"):
        denied = await client.post("/api/memory/task-methods/actions", headers=headers,
            json=review(readable.json(), action, f"recovered-{action}").model_dump(mode="json"))
        assert denied.status_code == 409 and denied.json()["detail"]["code"] == "method_current_owner_required"
    async with async_db() as db:
        assert list((await db.execute(select(Memory))).scalars()) == []
        goal = await db.get(Goal, "goal")
        await db.delete(goal)
    deleted = await client.get(path, headers=headers)
    assert deleted.status_code == 409 and deleted.json()["detail"]["code"] == "method_original_scope_unavailable"
    assert "new_method" not in deleted.text


@pytest.mark.asyncio
async def test_server_private_method_id_exact_replay_rejects_forged_scope_and_candidate(async_db, monkeypatch, tmp_path, no_inference):
    from src.auth.ownership import enroll
    from src.memory.repository import memory_repository
    from src.work_board.repository import _begin_sqlite_immediate
    operator, source = await failed_local_task(async_db, monkeypatch, tmp_path)
    await enroll(operator)
    source = source.model_copy(update={"scope": LessonScope(goal_id="goal", goal_revision=1, family="general")})
    proposed = await create_task_lesson(operator, source)
    result = await methods.review_method(operator, review(await methods.inspect_method(operator, proposed["proposal_id"]), "accept", "adopt"))
    async with async_db() as db:
        memory = await db.get(Memory, result["active_binding"]["version"])
        arguments = dict(content=memory.content, kind=memory.kind, source_session_id=memory.source_session_id,
            scope_key=memory.scope_key, metadata_json=memory.metadata_json, confidence=memory.confidence,
            proposal_id=proposed["proposal_id"], _memory_id=memory.id)
        await _begin_sqlite_immediate(db)
        replay = await memory_repository.create_m5_memory_in_session(db, **arguments)
        assert replay.id == memory.id and replay.metadata_json == arguments["metadata_json"]
        for key, value in [("goal_id", "forged-other-goal"), ("family", "research"),
                           ("source_scope_digest", "a" * 64), ("candidate_digest", "b" * 64),
                           ("candidate_schema", "ResearchStrategy.v1")]:
            metadata = json.loads(arguments["metadata_json"])
            metadata["work_board_provenance"]["memory_scope"][key] = value
            with pytest.raises(ValueError):
                await memory_repository.create_m5_memory_in_session(db, **{
                    **arguments, "metadata_json": json.dumps(metadata)})
        assert len(list((await db.execute(select(Memory))).scalars())) == 1
        assert replay.metadata_json == arguments["metadata_json"]
