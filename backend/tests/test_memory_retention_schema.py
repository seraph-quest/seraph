"""A valid row body cannot compensate for changed canonical DDL."""
import pytest
from sqlalchemy import ForeignKeyConstraint, MetaData, text
from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateTable

from src.db.models import MemorySource
from src.memory.header_bounds import HeaderBoundsError
from src.memory.retention_schema import validate_memory_schema


@pytest.mark.asyncio
async def test_original_schema_and_partial_unique_indices_are_validated(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        await validate_memory_schema(db)
        await db.execute(text("DROP INDEX ux_memory_proposals_owner_attempt_preview"))
        await db.execute(text(
            "CREATE UNIQUE INDEX ux_memory_proposals_owner_attempt_preview ON memory_proposals "
            "(owner_principal_id,owner_session_id,source_task_id,source_attempt_id,preview_text_digest)"))
        with pytest.raises(HeaderBoundsError, match="memory_retained_index_changed"):
            await validate_memory_schema(db)


@pytest.mark.asyncio
async def test_missing_memory_foreign_key_refuses_even_empty_valid_shaped_table(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        await db.execute(text("DROP TABLE memory_sources"))
        table = MemorySource.__table__.to_metadata(MetaData())
        for constraint in tuple(table.constraints):
            if isinstance(constraint, ForeignKeyConstraint):
                table.constraints.remove(constraint)
        await db.execute(text(str(CreateTable(table).compile(dialect=dialect()))))
        with pytest.raises(HeaderBoundsError, match="memory_retained_foreign_key_changed"):
            await validate_memory_schema(db)
