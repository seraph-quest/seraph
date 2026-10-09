"""Actual SQLite metadata preflight; certificates do not grant row access."""
from dataclasses import replace

import pytest
from sqlalchemy import event, text

from src.db.models import Memory
from src.memory.header_bounds import (
    HeaderBoundsError, MAX_BYTES, MEMORY_DESCRIPTORS,
    preflight_exact_rows, strict_json_loads, validate_certificate,
)


@pytest.mark.asyncio
async def test_oversized_private_row_is_denied_without_body_select(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        row = Memory(content="x" * (MAX_BYTES + 1))
        db.add(row)
        await db.flush()
        statements = []
        connection = await db.connection()
        def observe(_conn, _cursor, statement, _parameters, _context, _many):
            statements.append(statement)
        event.listen(connection.sync_connection, "before_cursor_execute", observe)
        try:
            with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
                await preflight_exact_rows(db, MEMORY_DESCRIPTORS["memories"], (row.id,), MAX_BYTES)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", observe)
        assert any("octet_length" in statement and '"content"' in statement for statement in statements)
        assert all('SELECT "content"' not in statement and "SELECT memories." not in statement
                   for statement in statements)
        assert await db.scalar(text("SELECT octet_length(content) FROM memories WHERE id=:id"),
                               {"id": row.id}) == MAX_BYTES + 1


@pytest.mark.asyncio
async def test_shared_budget_and_actual_write_invalidate_certificate(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        first, second = Memory(content="\x00😀" * 100), Memory(content="control\n" * 100)
        db.add_all([first, second])
        await db.flush()
        descriptor = MEMORY_DESCRIPTORS["memories"]
        certificate = await preflight_exact_rows(db, descriptor, (first.id,), MAX_BYTES)
        await validate_certificate(db, certificate)
        with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
            await preflight_exact_rows(db, descriptor, (first.id, second.id), certificate.upper_bytes)
        with pytest.raises(HeaderBoundsError, match="header_certificate_unavailable"):
            await validate_certificate(db, replace(certificate))
        await db.execute(text("UPDATE memories SET content='changed' WHERE id=:id"), {"id": second.id})
        with pytest.raises(HeaderBoundsError, match="header_certificate_stale"):
            await validate_certificate(db, certificate)


@pytest.mark.asyncio
async def test_rollback_and_new_writer_require_new_certificate(async_db):
    async with async_db() as db:
        row = Memory(content="retained")
        db.add(row)
        await db.commit()
        identity = row.id
        await db.execute(text("BEGIN IMMEDIATE"))
        certificate = await preflight_exact_rows(db, MEMORY_DESCRIPTORS["memories"], (identity,), MAX_BYTES)
        await db.rollback()
        await db.execute(text("BEGIN IMMEDIATE"))
        with pytest.raises(HeaderBoundsError, match="header_certificate_stale"):
            await validate_certificate(db, certificate)
        await validate_certificate(db, await preflight_exact_rows(
            db, MEMORY_DESCRIPTORS["memories"], (identity,), MAX_BYTES))


@pytest.mark.parametrize("value", ['{"lease":1,"lease":2}', '{"x":NaN}', '{"x":1e999}', '\ud800'])
def test_private_json_rejects_ambiguous_or_unbounded_scalars(value):
    with pytest.raises(HeaderBoundsError):
        strict_json_loads(value)


def test_json_bound_uses_utf8_not_character_count():
    assert strict_json_loads('{"x":"😀"}', max_utf8_bytes=12) == {"x": "😀"}
    with pytest.raises(HeaderBoundsError, match="header_json_bound"):
        strict_json_loads('{"x":"😀"}', max_utf8_bytes=11)
