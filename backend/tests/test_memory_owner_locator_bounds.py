"""Locator resource checks only: constructed inputs are not owner authority."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text

from src.db.models import Memory, MemoryEntity
from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
from src.memory.retention import read_memory_rows
from src.runtime_plugins.memory_producer import (
    MemoryOwnerEffect, NativeMemoryMutationAdmission, memory_owner_references,
)


@pytest.mark.asyncio
async def test_locator_does_not_materialize_private_memory_or_global_aliases(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        entity = MemoryEntity(canonical_key="project:original", canonical_name="Original",
                              aliases_json='["' + "private" * 30_000 + '"]', entity_type="project")
        db.add(entity)
        await db.flush()
        memory = Memory(content="private" * 30_000, source_session_id="original-owner",
                        project_entity_id=entity.id)
        db.add(memory)
        await db.flush()
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
        with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
            await read_memory_rows(db, refs, remaining_bytes=MAX_BYTES)
