"""Original auth remains authoritative; unissued REPORT payers cannot enter it."""

import pytest
from sqlalchemy import event

from tests.test_research_native_vertical import real_auth


@pytest.mark.asyncio
@pytest.mark.parametrize("subclass", [False, True])
async def test_unissued_report_numeric_scope_denies_before_auth_query(
        async_db, subclass):
    from src.auth import service
    from src.memory.header_bounds import HeaderBoundsError
    from src.work_board import review

    cls = review._MemoryReportDependencyNumericInventory
    if subclass:
        class UnissuedSubclass(cls):
            pass
        cls = UnissuedSubclass
    # This deliberately unissued object is negative input, never a Source,
    # certificate, principal or permission. Registry denial precedes fields.
    unissued = object.__new__(cls)
    async with async_db() as db:
        connection = await db.connection()
        observed = []
        def record_sql(*args):
            observed.append(True)
        event.listen(connection.sync_connection, "before_cursor_execute", record_sql)
        token = review._MEMORY_REPORT_PRINCIPAL_SCOPE.set(unissued)
        try:
            with pytest.raises(HeaderBoundsError,
                    match="memory_report_principal_numeric_owner_changed"):
                await service.authenticate_principal("operator:unissued-report-payer", db=db)
            assert observed == []
        finally:
            review._MEMORY_REPORT_PRINCIPAL_SCOPE.reset(token)
            event.remove(connection.sync_connection, "before_cursor_execute", record_sql)


@pytest.mark.asyncio
async def test_original_principal_authentication_without_report_scope_is_unchanged(
        async_db, real_auth, monkeypatch):
    from src.auth import service
    from src.work_board import review

    monkeypatch.setattr(service, "get_session", async_db)
    _bearer, issued = await service.create_session()
    assert service.auth_enabled() is True
    assert issued.principal.principal_id != "operator:test-bypass"
    assert review._MEMORY_REPORT_PRINCIPAL_SCOPE.get() is None
    async with async_db() as db:
        current = await service.authenticate_principal(issued.principal.principal_id, db=db)
        assert current.session_id == issued.session_id
        assert current.principal == issued.principal
