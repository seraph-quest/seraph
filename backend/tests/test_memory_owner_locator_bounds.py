"""Locator resource checks only: constructed inputs are not owner authority."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text

from src.db.models import Memory, MemoryEntity
from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
from tests.test_memory_retention_writer import accounting_db
from src.runtime_plugins.ownership import begin_native_writer
from src.runtime_plugins.memory_producer import (
    MemoryOwnerEffect, NativeMemoryMutationAdmission, memory_owner_references,
)


@pytest.mark.asyncio
async def test_locator_does_not_materialize_private_memory_or_global_aliases(accounting_db):
    _root, engine, factory = accounting_db
    async with factory() as db:
        entity = MemoryEntity(canonical_key="project:original", canonical_name="Original",
                              aliases_json='["' + "private" * 30_000 + '"]', entity_type="project")
        db.add(entity)
        await db.flush()
        memory = Memory(content="private" * 30_000, source_session_id="original-owner",
                        project_entity_id=entity.id)
        db.add(memory)
        await db.flush()
        await db.commit()
    # Preparatory read proves locator mechanics only; it grants no native effect.
    async with factory() as db:
        admission = NativeMemoryMutationAdmission.from_candidate({
            "schema_version": 1, "method": "memory.forget", "operator_principal_id": "operator",
            "operator_session_id": "original-owner", "opaque_ref": "original", "idempotency_key": "original",
            "original_deadline": (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
            "host_boot_nonce": "a" * 64, "composition_binding_digest": "b" * 64,
            "record_ref": memory.id, "mode": "archive", "privacy_boundary": "private",
            "reason": None, "prepared_reason": None})
        effect = MemoryOwnerEffect("memory.forget", admission.candidate_digest, "succeeded",
                                   None, None, None, memory.id)
        statements = []
        connection = await db.connection()
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(connection.sync_connection, "before_cursor_execute", observe)
        try:
            references = await memory_owner_references(db, admission, effect)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", observe)
        refs = tuple((ref.table, ref.row_ref) for ref in references)
        assert set(refs) == {("memories", memory.id), ("memory_entities", entity.id)}
        assert all(".content" not in query and ".aliases_json" not in query for query in statements)
        await db.rollback()
    # A genuine original frame must reject those already-committed huge rows.
    statements = []
    def observe_admission(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(engine.sync_engine, "before_cursor_execute", observe_admission)
    try:
        async with factory() as db:
            with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
                await begin_native_writer(db, owner="durable_jobs", fresh=True,
                                          header_budget=HeaderReadBudget())
            await db.rollback()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", observe_admission)
    assert not any(statement.startswith('SELECT "id",') or "SELECT memories." in statement
                   or "SELECT memory_entities." in statement for statement in statements)
