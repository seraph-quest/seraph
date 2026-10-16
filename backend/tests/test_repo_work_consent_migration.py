"""Populated legacy source consent never becomes diagnostic consent."""
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from src.db.engine import _ensure_repo_repair_columns


@pytest.mark.asyncio
async def test_populated_source_consent_migration_preserves_binding_and_denies_diagnostics(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///" + str(tmp_path / "legacy.sqlite"))
    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql("CREATE TABLE repo_repair_egress_consents (id VARCHAR PRIMARY KEY, owner_principal_id VARCHAR, request_digest VARCHAR, state VARCHAR)")
            await conn.exec_driver_sql("INSERT INTO repo_repair_egress_consents VALUES (?, ?, ?, ?)",
                ("original-consent", "operator:original", "a" * 64, "active"))
            await _ensure_repo_repair_columns(conn)
            await _ensure_repo_repair_columns(conn)
            row = (await conn.exec_driver_sql("SELECT owner_principal_id, request_digest, state, iteration_id, egress_envelope_sha256, diagnostics_sha256, diagnostics_acknowledged, combined_input_bytes FROM repo_repair_egress_consents WHERE id = ?", ("original-consent",))).one()
            assert tuple(row) == ("operator:original", "a" * 64, "active", None, None, None, 0, None)
    finally:
        await engine.dispose()
