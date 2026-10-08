"""Actual Memory/M5 owner transactions; not stock-host producer acceptance."""
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from src.db.models import AuditEvent, Memory, MemoryProposal, MemorySource, MemoryStatus, Secret
from src.memory import m5
from src.runtime_plugins.memory_producer import (
    NativeMemoryMutationAdmission, perform_memory_mutation, source_binding,
)
from src.runtime_plugins.dispatch import NativeServiceBlocked
from src.work_board.repository import _begin_sqlite_immediate
from tests.test_work_board_memory import OWNER, _goal_and_tasks, _verified_attempt, _patch_m5_sessions


async def source(factory, monkeypatch):
    _patch_m5_sessions(monkeypatch, factory)
    async with factory() as db:
        goal, task, _ = await _goal_and_tasks(db)
        attempt = await _verified_attempt(db, task)
    return goal, task, attempt


def common(method):
    return {"schema_version": 1, "method": method,
        "operator_principal_id": OWNER.principal_id, "operator_session_id": OWNER.session_id,
        "opaque_ref": "native-memory:original", "idempotency_key": "original",
        "original_deadline": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        "host_boot_nonce": "a" * 64, "composition_binding_digest": "b" * 64}


async def proposal_candidate(factory, task, attempt):
    async with factory() as db:
        binding, proof = await source_binding(db, principal_id=OWNER.principal_id,
            session_id=OWNER.session_id, task_id=task.task_id, revision=task.task_revision,
            attempt_id=attempt.attempt_id)
        prepared = await m5.prepare_m5_text(db, m5._structured_source_candidate(proof))
    return NativeMemoryMutationAdmission.from_candidate({**common("memory.propose"),
        "source": binding, "prepared_text": asdict(prepared)})


async def effect(factory, admission, *, fail_after=False):
    calls = []
    async def authority(db):
        calls.append(db)
    async with factory() as db:
        await _begin_sqlite_immediate(db)
        db.info["native_writer_started"] = True
        result = await perform_memory_mutation(db, admission, authority_check=authority)
        assert calls == [db, db]
        if fail_after:
            raise RuntimeError("after owner effect before protected receipt")
    return result


@pytest.mark.asyncio
async def test_actual_proposal_and_review_share_caller_writer(async_db, monkeypatch):
    goal, task, attempt = await source(async_db, monkeypatch)
    admission = await proposal_candidate(async_db, task, attempt)
    assert admission.wire_inputs() == {"request_ref": "native-memory:original"}
    proposed = await effect(async_db, admission)
    assert proposed.status == "succeeded" and proposed.record_id is None
    async with async_db() as db:
        row = await db.get(MemoryProposal, proposed.proposal_id)
        assert row.status.value == "proposed" and row.owner_session_id == OWNER.session_id
        assert (await db.get(AuditEvent, proposed.audit_event_id)).event_type == "memory_learning_proposed"
        assert not list((await db.execute(select(Memory))).scalars())
        prepared = await m5.prepare_m5_text(db, row.preview_text)
        review = NativeMemoryMutationAdmission.from_candidate({**common("memory.applyReviewed"),
            "source": admission.candidate()["source"], "proposal_id": row.proposal_id,
            "proposal_schema": row.schema_version, "action": "accept", "expected_revision": row.revision,
            "expected_preview_text_digest": row.preview_text_digest,
            "edited_text": None, "decision_effect": "none", "preferred_capability_id": None,
            "corrects_memory_id": None, "reason": None,
            "proposal_expires_at": m5._utc(row.expires_at).isoformat(), "prepared_text": asdict(prepared)})
    assert review.wire_inputs() == {"review_ref": "native-memory:original"}
    adopted = await effect(async_db, review)
    assert adopted.status == "succeeded" and adopted.audit_event_id
    async with async_db() as db:
        memory = await db.get(Memory, adopted.record_id)
        row = await db.get(MemoryProposal, proposed.proposal_id)
        assert memory.source_session_id == OWNER.session_id and memory.status == MemoryStatus.active
        assert row.accepted_memory_id == memory.id and row.revision == 2
        assert len(list((await db.execute(select(MemorySource))).scalars())) >= 1
        assert (await db.get(AuditEvent, adopted.audit_event_id)).event_type == "memory_learning_accepted"
        assert len(list((await db.execute(select(AuditEvent))).scalars())) == 2
    # A fresh owner candidate cannot reinterpret legacy accepted replay as a new effect.
    with pytest.raises(NativeServiceBlocked, match="native_memory_original_review_changed"):
        await effect(async_db, review)


@pytest.mark.asyncio
async def test_owner_effect_rolls_back_proposal_and_real_audit(async_db, monkeypatch):
    _, task, attempt = await source(async_db, monkeypatch)
    admission = await proposal_candidate(async_db, task, attempt)
    with pytest.raises(RuntimeError, match="after owner effect"):
        await effect(async_db, admission, fail_after=True)
    async with async_db() as db:
        assert not list((await db.execute(select(MemoryProposal))).scalars())
        assert not list((await db.execute(select(AuditEvent))).scalars())


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["new_secret", "foreign_session", "stale_task", "expired"])
async def test_original_candidate_races_deny_before_effect(async_db, monkeypatch, change):
    _, task, attempt = await source(async_db, monkeypatch)
    admission = await proposal_candidate(async_db, task, attempt)
    async with async_db() as db:
        if change == "new_secret":
            db.add(Secret(key="inserted-after-staging", encrypted_value="opaque-new-ciphertext"))
        elif change == "foreign_session":
            from src.db.models import Session
            (await db.get(Session, OWNER.session_id)).owner_principal_id = "foreign"
        elif change == "stale_task":
            from src.db.models import WorkBoardTask
            current = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id))
            current.task_revision += 1
    if change == "expired":
        candidate = admission.candidate()
        candidate["original_deadline"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        admission = NativeMemoryMutationAdmission.from_candidate(candidate)
    with pytest.raises((ValueError, PermissionError, NativeServiceBlocked)):
        await effect(async_db, admission)
    async with async_db() as db:
        assert not list((await db.execute(select(MemoryProposal))).scalars())
        assert not list((await db.execute(select(AuditEvent))).scalars())


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["archive", "redact"])
async def test_actual_forget_and_audit_rollback_with_pending_native_result(async_db, monkeypatch, mode):
    _, _, _ = await source(async_db, monkeypatch)
    async with async_db() as db:
        db.add(Memory(id="original-record", source_session_id=OWNER.session_id,
            content="Original private record", summary="Original summary"))
    admission = NativeMemoryMutationAdmission.from_candidate({**common("memory.forget"),
        "record_ref": "original-record", "mode": mode, "privacy_boundary": "private",
        "reason": None, "prepared_reason": None})
    with pytest.raises(RuntimeError, match="after owner effect"):
        await effect(async_db, admission, fail_after=True)
    async with async_db() as db:
        assert (await db.get(Memory, "original-record")).status == MemoryStatus.active
        assert not list((await db.execute(select(AuditEvent))).scalars())
    result = await effect(async_db, admission)
    assert result.record_id == "original-record" and result.audit_event_id
    async with async_db() as db:
        memory = await db.get(Memory, "original-record")
        assert memory.status == MemoryStatus.archived
        assert memory.content == ("[forgotten by operator]" if mode == "redact" else "Original private record")
        assert (await db.get(AuditEvent, result.audit_event_id)).event_type == "memory_forgotten"


@pytest.mark.asyncio
async def test_actual_secret_redaction_stages_outside_writer_and_replacement_denies(async_db, monkeypatch):
    from cryptography.fernet import Fernet
    from config.settings import settings
    key = Fernet.generate_key()
    monkeypatch.setattr(settings, "vault_encryption_key", key.decode())
    async with async_db() as db:
        db.add(Secret(key="actual-secret", encrypted_value=Fernet(key).encrypt(b'known-private-value').decode()))
    async with async_db() as db:
        prepared = await m5.prepare_m5_text(db, "Reviewed known-private-value fact")
    assert prepared.sanitized_text == "Reviewed [redacted secret] fact"
    async with async_db() as db:
        await _begin_sqlite_immediate(db)
        assert await m5._consume_prepared_m5_text(db, "Reviewed known-private-value fact", prepared) == prepared.sanitized_text
    async with async_db() as db:
        secret = await db.scalar(select(Secret).where(Secret.key == "actual-secret"))
        secret.encrypted_value = Fernet(key).encrypt(b'replaced-private-value').decode()
    with pytest.raises(ValueError, match="staged Vault binding changed"):
        async with async_db() as db:
            await _begin_sqlite_immediate(db)
            await m5._consume_prepared_m5_text(db, "Reviewed known-private-value fact", prepared)


@pytest.mark.asyncio
async def test_physical_source_profile_denied_before_readback(async_db, monkeypatch):
    _, task, attempt = await source(async_db, monkeypatch)
    from src.db.models import WorkBoardTask
    async with async_db() as db:
        current = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task.task_id))
        current.capability_id = "research.v1"
    calls = []
    async def unexpected_readback(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("unsupported physical owner must not be called")
    monkeypatch.setattr(m5, "_verified_source", unexpected_readback)
    with pytest.raises(NativeServiceBlocked, match="native_memory_source_profile_unsupported"):
        async with async_db() as db:
            await source_binding(db, principal_id=OWNER.principal_id,
                session_id=OWNER.session_id, task_id=task.task_id,
                revision=task.task_revision, attempt_id=attempt.attempt_id)
    assert calls == []
    async with async_db() as db:
        assert not list((await db.execute(select(MemoryProposal))).scalars())
        assert not list((await db.execute(select(AuditEvent))).scalars())


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "foreign", "tombstoned"])
async def test_forget_original_record_changes_deny_without_audit(async_db, monkeypatch, change):
    await source(async_db, monkeypatch)
    async with async_db() as db:
        db.add(Memory(id="original-record", source_session_id=OWNER.session_id,
                      content="Original private record", summary="Original summary"))
    admission = NativeMemoryMutationAdmission.from_candidate({**common("memory.forget"),
        "record_ref": "original-record", "mode": "redact", "privacy_boundary": "private",
        "reason": None, "prepared_reason": None})
    async with async_db() as db:
        record = await db.get(Memory, "original-record")
        if change == "deleted":
            await db.delete(record)
        elif change == "foreign":
            record.source_session_id = "foreign-owner"
        else:
            from src.db.models import MemoryTombstone
            db.add(MemoryTombstone(memory_id=record.id))
    async with async_db() as db:
        before = list((await db.execute(select(AuditEvent.id))).scalars())
    with pytest.raises(NativeServiceBlocked, match="native_memory_original_record_changed"):
        await effect(async_db, admission)
    async with async_db() as db:
        assert list((await db.execute(select(AuditEvent.id))).scalars()) == before
