"""Source-only regressions; genuine journey remains bounded before effects."""
import pytest

from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth


@pytest.mark.asyncio
async def test_genuine_report_source_uses_bounded_second_schema_read(
        accounting_db, real_auth, monkeypatch, tmp_path):
    from src.memory import composition_headers as headers
    from tests.test_native_memory_genuine_numeric_prefix import (
        test_genuine_terminal_report_observes_original_pre_effect_numeric_stop as journey,
    )

    original = headers._native_memory_exact_source_schema_rows
    observed = []

    def observe(connection, descriptor, budget):
        # The observer forwards the real owner call; it neither supplies an
        # owner nor alters its budget, returned rows, or failure.
        assert headers._compiler_bounds._native_memory_header_logical_frame(connection, budget) is budget
        before = budget.remaining
        rows = original(connection, descriptor, budget)
        n, name, sql_type = headers._DESCRIPTOR_XINFO_WIDTHS[descriptor.table]
        upper = (n + 1) * (name + sql_type + 16) + 1
        assert len(rows) == n
        assert headers._descriptor_xinfo_size(rows) <= upper
        assert before - budget.remaining >= 3 * upper
        observed.append(descriptor.table)
        return rows

    monkeypatch.setattr(headers, "_native_memory_exact_source_schema_rows", observe)
    await journey(accounting_db, real_auth, monkeypatch, tmp_path)
    assert "sessions" in observed
    assert "work_board_tasks" in observed
    assert "work_board_attempts" in observed


@pytest.mark.asyncio
async def test_existing_bounded_schema_rejects_extra_column(accounting_db):
    from sqlalchemy import text
    from src.memory.composition_headers import validate_descriptor_schema
    from src.memory.header_bounds import COMPOSITION_DESCRIPTORS, HeaderReadBudget, HeaderBoundsError

    _, _, sessions = accounting_db
    async with sessions() as db:
        await db.execute(text('ALTER TABLE sessions ADD COLUMN "unexpected_schema_column" TEXT'))
        connection = await db.connection()
        with pytest.raises(HeaderBoundsError, match="header_schema_changed"):
            await connection.run_sync(lambda c: validate_descriptor_schema(
                c, COMPOSITION_DESCRIPTORS["sessions"], _header_budget=HeaderReadBudget()))
        await db.rollback()


def test_second_schema_helper_rejects_absent_source_before_query():
    import sqlite3
    from src.memory.composition_headers import _native_memory_exact_source_schema_rows
    from src.memory.header_bounds import COMPOSITION_DESCRIPTORS, HeaderReadBudget, HeaderBoundsError

    connection = sqlite3.connect(":memory:")
    try:
        queries = []
        connection.set_trace_callback(queries.append)
        with pytest.raises(HeaderBoundsError, match="memory_header_exact_schema_owner_unavailable"):
            _native_memory_exact_source_schema_rows(
                connection, COMPOSITION_DESCRIPTORS["sessions"], HeaderReadBudget())
        assert queries == []
    finally:
        connection.close()
