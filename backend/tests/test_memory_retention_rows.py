"""Whole selected Memory closure bounds on actual canonical SQLite rows."""
import json

import pytest
from sqlalchemy import event, text

from src.db.models import Memory, MemorySource
from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, MEMORY_DESCRIPTORS
from src.memory.retention import read_memory_rows
from tests.test_memory_retention_writer import accounting_db, retention_writer


@pytest.mark.asyncio
async def test_later_oversized_header_denies_before_first_private_tuple(accounting_db, monkeypatch):
    _root, _engine, factory = accounting_db
    async with factory() as db:
        memory = Memory(content="Visible")
        db.add(memory)
        await db.flush()
        source = MemorySource(memory_id=memory.id, source_type="message", snippet="x" * 2_000)
        db.add(source)
        await db.flush()
        await db.commit()
    async with retention_writer(accounting_db) as (db, budget):
        first = await budget.certify(db, MEMORY_DESCRIPTORS["memories"], (memory.id,))
        local_remaining = first.upper_bytes + 1
        certify = budget.certify
        completed = []
        async def observe_original_certify(db, descriptor, row_ids):
            before = budget.remaining
            certificate = await certify(db, descriptor, row_ids)
            completed.append((descriptor.table, certificate, before, budget.remaining))
            return certificate
        monkeypatch.setattr(budget, "certify", observe_original_certify)
        statements = []
        connection = await db.connection()
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(connection.sync_connection, "before_cursor_execute", observe)
        try:
            with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
                await read_memory_rows(db, (("memories", memory.id), ("memory_sources", source.id)),
                                       header_budget=budget, remaining_bytes=local_remaining)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", observe)
        assert all(not statement.startswith('SELECT "id",') for statement in statements)
        assert any("octet_length" in statement and '"snippet"' in statement for statement in statements)
        assert any("octet_length" in statement and '"content"' in statement for statement in statements)
        assert [table for table, _, _, _ in completed] == ["memories", "memory_sources"]
        assert all(before > after > 0 for _, _, before, after in completed)
        assert sum(certificate.upper_bytes for _, certificate, _, _ in completed) > local_remaining
        assert completed[0][1].upper_bytes <= local_remaining
        assert budget.remaining > 0


@pytest.mark.asyncio
async def test_exact_private_bytes_and_float_tags_preserved_and_deduplicated(accounting_db):
    _root, _engine, factory = accounting_db
    async with factory() as db:
        body = 'Private\x00😀\nbody'
        metadata = '{ "original": "bytes", "nested": [1] }'
        memory = Memory(content=body, metadata_json=metadata, confidence=0.125)
        db.add(memory)
        await db.flush()
        await db.commit()
    async with retention_writer(accounting_db) as (db, budget):
        ref = ("memories", memory.id)
        result = await read_memory_rows(db, (ref, ref), header_budget=budget, remaining_bytes=MAX_BYTES, reserved_bytes=4096)
        assert result.references == (ref,)
        assert result.rows[0]["content"] == body
        assert result.rows[0]["metadata_json"] == metadata
        assert result.encoded_bytes <= result.certified_upper_bytes
        from src.memory.retention import encode_memory_row
        encoded = json.loads(encode_memory_row(*ref, result.rows[0]))
        assert dict(encoded[3])["confidence"] == {"type": "float64", "hex": (0.125).hex()}
        with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
            await read_memory_rows(db, (ref,), header_budget=budget, remaining_bytes=4096, reserved_bytes=4096)


@pytest.mark.asyncio
async def test_whole_ref_budget_includes_existing_core_references(accounting_db):
    _root, _engine, factory = accounting_db
    async with factory() as db:
        memory = Memory(content="finite")
        db.add(memory)
        await db.flush()
        await db.commit()
    async with retention_writer(accounting_db) as (db, budget):
        existing = tuple(("workflow_run_states", f"job-{index}") for index in range(128))
        with pytest.raises(HeaderBoundsError, match="memory_closure_reference_bound"):
            await read_memory_rows(db, (("memories", memory.id),), header_budget=budget, remaining_bytes=MAX_BYTES,
                                   existing_references=existing)
