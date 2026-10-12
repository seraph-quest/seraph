"""The genuine original rotation supplies the early-return record."""

import json

import pytest
from sqlalchemy import select

from tests.test_research_native_vertical import real_auth


@pytest.mark.asyncio
async def test_original_rotated_tombstone_preserves_legacy_tuple_and_numeric_cover(
        async_db, real_auth, monkeypatch):
    from src.auth import service
    from src.db.models import OperatorSession
    from src.work_board import review

    monkeypatch.setattr(service, "get_session", async_db)
    _bearer, original = await service.create_session()
    await service.create_session(replace_session_id=original.session_id,
        expected_token_hash=original._token_hash)
    async with async_db() as db:
        retired = (await db.scalars(select(OperatorSession).where(
            OperatorSession.replaced_by_id == original.session_id,
            OperatorSession.is_bearer_tombstone.is_(True)))).one()
        assert retired.revoked_at is not None
        # No synthetic REPORT8 inventory or scope: this is a genuine original
        # early-return record and the legacy API remains unchanged.
        assert review._MEMORY_REPORT_PRINCIPAL_SCOPE.get() is None
        result = await service._ownership_metadata(db, retired)
        assert result == (service._OWNERSHIP_LEGACY, service._OWNERSHIP_RECOVERY_ACTION)
        encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()
        assert len(encoded) <= service._native_memory_report_legacy_tuple_upper()
