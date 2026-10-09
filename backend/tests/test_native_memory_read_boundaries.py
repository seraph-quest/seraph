"""Real selected-owner SQL boundaries; these are not native Source grants."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text

from src.db.models import Memory, MemoryTombstone, Session
from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
from src.runtime_plugins.dispatch import NativeServiceBlocked
from src.runtime_plugins.memory_producer import NativeMemoryMutationAdmission, validate_original_memory_owner


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("condition", ["ordinary", "missing", "memory_overflow", "tombstone_overflow", "tombstoned"])
async def test_forget_certifies_selected_original_rows_before_owner_bodies(async_db, condition):
    budget = HeaderReadBudget()
    candidate = {"schema_version": 1, "method": "memory.forget",
        "operator_principal_id": "bounded-owner", "operator_session_id": "bounded-session",
        "opaque_ref": "native-memory:boundaries", "idempotency_key": "boundaries",
        "original_deadline": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
        "host_boot_nonce": "a" * 64, "composition_binding_digest": "b" * 64,
        "record_ref": "bounded-memory", "mode": "redact", "privacy_boundary": "private",
        "reason": None, "prepared_reason": None}
    admission = replace(NativeMemoryMutationAdmission.from_candidate(candidate), header_budget=budget)
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        db.add(Session(id="bounded-session", owner_principal_id="bounded-owner"))
        if condition != "missing":
            db.add(Memory(id="bounded-memory", source_session_id="bounded-session",
                content="x" * 200_000 if condition == "memory_overflow" else "Original bounded text"))
        await db.flush()
        if condition in {"tombstone_overflow", "tombstoned"}:
            db.add(MemoryTombstone(id="bounded-tombstone", memory_id="bounded-memory",
                reason="x" * 200_000 if condition == "tombstone_overflow" else "Original deletion"))
            await db.flush()
        statements = []
        connection = await db.connection()
        def observe(_connection, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(connection.sync_connection, "before_cursor_execute", observe)
        try:
            if condition in {"memory_overflow", "tombstone_overflow"}:
                with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
                    await validate_original_memory_owner(db, admission)
            elif condition in {"missing", "tombstoned"}:
                with pytest.raises(NativeServiceBlocked, match="native_memory_original_record_changed"):
                    await validate_original_memory_owner(db, admission)
            else:
                assert await validate_original_memory_owner(db, admission) == candidate
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", observe)
        assert admission.header_budget is budget
        assert budget.remaining < 1_048_576
        assert any("octet_length" in sql for sql in statements)
        if condition in {"memory_overflow", "missing"}:
            assert not any("SELECT memories." in sql for sql in statements)
        if condition == "tombstone_overflow":
            assert not any("SELECT memory_tombstones." in sql for sql in statements)
        assert not any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
