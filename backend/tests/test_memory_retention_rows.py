"""Whole selected Memory closure bounds on actual canonical SQLite rows."""
import json

import pytest
from sqlalchemy import event, text

from src.db.models import Memory, MemorySource
from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES
from src.memory.retention import read_memory_rows


@pytest.mark.asyncio
async def test_later_oversized_header_denies_before_first_private_tuple(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        memory = Memory(content="Visible")
        db.add(memory)
        await db.flush()
        source = MemorySource(memory_id=memory.id, source_type="message", snippet="x" * 200_000)
        db.add(source)
        await db.flush()
        statements = []
        connection = await db.connection()
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(connection.sync_connection, "before_cursor_execute", observe)
        try:
            with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
                await read_memory_rows(db, (("memories", memory.id), ("memory_sources", source.id)),
                                       remaining_bytes=MAX_BYTES)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", observe)
        assert all(not statement.startswith('SELECT "id",') for statement in statements)
        assert any("octet_length" in statement and '"snippet"' in statement for statement in statements)
        assert any("octet_length" in statement and '"content"' in statement for statement in statements)


@pytest.mark.asyncio
async def test_exact_private_bytes_and_float_tags_preserved_and_deduplicated(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        body = 'Private\x00😀\nbody'
        metadata = '{ "original": "bytes", "nested": [1] }'
        memory = Memory(content=body, metadata_json=metadata, confidence=0.125)
        db.add(memory)
        await db.flush()
        ref = ("memories", memory.id)
        result = await read_memory_rows(db, (ref, ref), remaining_bytes=MAX_BYTES, reserved_bytes=4096)
        assert result.references == (ref,)
        assert result.rows[0]["content"] == body
        assert result.rows[0]["metadata_json"] == metadata
        assert result.encoded_bytes <= result.certified_upper_bytes
        from src.memory.retention import encode_memory_row
        encoded = json.loads(encode_memory_row(*ref, result.rows[0]))
        assert dict(encoded[3])["confidence"] == {"type": "float64", "hex": (0.125).hex()}
        with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
            await read_memory_rows(db, (ref,), remaining_bytes=4096, reserved_bytes=4096)


@pytest.mark.asyncio
async def test_whole_ref_budget_includes_existing_core_references(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        memory = Memory(content="finite")
        db.add(memory)
        await db.flush()
        existing = tuple(("workflow_run_states", f"job-{index}") for index in range(128))
        with pytest.raises(HeaderBoundsError, match="memory_closure_reference_bound"):
            await read_memory_rows(db, (("memories", memory.id),), remaining_bytes=MAX_BYTES,
                                   existing_references=existing)
