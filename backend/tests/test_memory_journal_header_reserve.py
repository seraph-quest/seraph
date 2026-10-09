"""Resource evidence only; inventory rows are not original Memory effects."""
import pytest
from sqlalchemy import event, text

from src.db.models import RuntimeCompositionState, WorkflowRunState
from src.memory.header_bounds import HeaderBoundsError, MAX_BYTES, validate_certificate
from src.workspace.accounting_witness import _preflight_native_memory_journal_headers
from src.workspace.accounting_witness import CompositionSessionGuard
from src.workspace.accounting_witness import composition_closure
from src.workspace.production import ProductionWorkspaceReconciliationError
from src.workflows.job_runtime import _protected_composition_checkpoint


def inventory_row(identity, *, private_body="unsealed"):
    return WorkflowRunState(run_identity=identity, root_run_identity=identity,
        workflow_name="native-memory", job_kind="runtime_service_memory_v1",
        status="unknown_external_effect", owner_principal_id="foreign",
        checkpoint_context_json=private_body)


@pytest.mark.asyncio
async def test_incoming_job_shares_complete_reference_budget(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        db.add(inventory_row("old"))
        await db.flush()
        existing = tuple(("memories", f"record-{index}") for index in range(127))
        with pytest.raises(HeaderBoundsError, match="memory_closure_reference_bound"):
            await _preflight_native_memory_journal_headers(db,
                existing_references=existing, incoming_identity="new")
        # Replaying the same job does not gain a new distinct reference.
        certificate = await _preflight_native_memory_journal_headers(db,
            existing_references=existing, incoming_identity="old")
        assert certificate.row_ids == ("old",)


@pytest.mark.asyncio
async def test_all_headers_precede_any_private_journal_body(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        db.add_all((inventory_row("a"), inventory_row("z", private_body="x" * 200_000)))
        await db.flush()
        connection = await db.connection()
        statements = []
        def observe(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)
        event.listen(connection.sync_connection, "before_cursor_execute", observe)
        try:
            with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
                await _preflight_native_memory_journal_headers(db)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", observe)
        assert any('octet_length("checkpoint_context_json")' in query for query in statements)
        assert not any(query.startswith('SELECT "id",') for query in statements)


@pytest.mark.asyncio
async def test_reserve_charges_remaining_bytes_and_certificate_expires(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        db.add(inventory_row("one"))
        await db.flush()
        with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
            await _preflight_native_memory_journal_headers(db, reserved_bytes=MAX_BYTES)
        certificate = await _preflight_native_memory_journal_headers(db, reserved_bytes=4096)
        await validate_certificate(db, certificate)
        await db.execute(text("UPDATE workflow_run_states SET revision=revision+1 WHERE run_identity='one'"))
        with pytest.raises(HeaderBoundsError, match="header_certificate_stale"):
            await validate_certificate(db, certificate)


@pytest.mark.asyncio
async def test_overfull_inventory_has_no_journal_fetch_fallback(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        db.add_all(inventory_row(f"job-{index}") for index in range(129))
        await db.flush()
        with pytest.raises(HeaderBoundsError, match="memory_universe_bound"):
            await _preflight_native_memory_journal_headers(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("reserve", [-1, MAX_BYTES + 1, True])
async def test_invalid_resource_inputs_are_not_coerced(async_db, reserve):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        with pytest.raises(HeaderBoundsError, match="memory_journal_reserve_invalid"):
            await _preflight_native_memory_journal_headers(db, reserved_bytes=reserve)


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["memory:original-reference.v2", "memory:current-reference.v2"])
async def test_generic_publication_cannot_mint_or_replace_memory_journal(async_db, identifier):
    # Exact protected-ID deny path, not a claimed original native effect.
    from types import SimpleNamespace
    import json
    assert _protected_composition_checkpoint(identifier)
    record = {"checkpoint_id": identifier, "state_digest": "a" * 64,
              "safe": True, "payload": {"forged": True}}
    async with async_db() as db:
        fake_guard = SimpleNamespace(db=db)
        with pytest.raises(ProductionWorkspaceReconciliationError,
                           match="composition_memory_retention_unavailable"):
            CompositionSessionGuard._check_private_journal(fake_guard, "[]",
                json.dumps([record]), run_id="old")
        changed = {**record, "payload": {"forged": "replacement"}}
        with pytest.raises(ProductionWorkspaceReconciliationError,
                           match="composition_memory_retention_unavailable"):
            CompositionSessionGuard._check_private_journal(fake_guard,
                json.dumps([record]), json.dumps([changed]), run_id="old")
        db.info["composition_native_claim_receipt"] = record
        with pytest.raises(ProductionWorkspaceReconciliationError,
                           match="composition_memory_retention_unavailable"):
            CompositionSessionGuard._check_private_journal(fake_guard, "[]",
                json.dumps([record]), run_id="old")


@pytest.mark.asyncio
async def test_initialized_closure_denies_unbound_memory_before_private_body(async_db):
    from src.runtime_plugins.ownership import DOMAINS
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        db.add_all(RuntimeCompositionState(runtime_domain=domain, owner_kind="legacy",
            epoch=1, composition_digest="a" * 64, state="ready") for domain in DOMAINS)
        db.add(inventory_row("unbound", private_body="x" * 2_000_000))
        await db.flush()
        connection = await db.connection()
        statements = []
        def observe(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)
        event.listen(connection.sync_connection, "before_cursor_execute", observe)
        try:
            with pytest.raises(ProductionWorkspaceReconciliationError,
                               match="composition_native_memory_retention_unavailable"):
                await connection.run_sync(composition_closure)
        finally:
            event.remove(connection.sync_connection, "before_cursor_execute", observe)
        assert any("INDEXED BY ix_workflow_run_states_job_kind" in query for query in statements)
        assert not any("checkpoint_context_json" in query for query in statements)
