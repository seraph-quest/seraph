"""Raw tuple byte proof only; transient instances are not Source registrations."""
import pytest
from sqlalchemy import text

from src.db.models import AuditEvent, Memory, MemoryProposal
from src.memory.header_bounds import HeaderBoundsError, MEMORY_DESCRIPTORS
from src.workspace.accounting_witness import _native_memory_transient_sql_row


@pytest.mark.asyncio
async def test_actual_constructor_bindings_equal_postflush_raw_tuple(async_db):
    instance = Memory(content="private\x00😀", confidence=0.5)
    prepared = _native_memory_transient_sql_row(instance)
    assert type(prepared["created_at"]) is str
    assert type(prepared["kind"]) is str
    assert len(prepared["id"]) == 32
    descriptor = MEMORY_DESCRIPTORS["memories"]
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        db.add(instance)
        await db.flush()
        columns = ",".join('"' + name + '"' for name in descriptor.columns)
        result = (await db.execute(text(f'SELECT {columns} FROM memories WHERE id=:key'),
                                   {"key": instance.id})).one()
        assert dict(zip(descriptor.columns, result)) == prepared
        with pytest.raises(HeaderBoundsError, match="memory_transient_row_unavailable"):
            _native_memory_transient_sql_row(instance)


def test_actual_bool_enum_datetime_bindings_are_raw_scalars():
    proposal = MemoryProposal(owner_principal_id="operator", owner_session_id="session",
        source_task_id="task", source_task_revision=1, source_attempt_id="attempt",
        workflow_run_id="run", workflow_run_revision=1, goal_id="goal", goal_revision=1,
        source_context_digest="a" * 64, evidence_digest="b" * 64,
        typed_input_digest="c" * 64, capability_id="capability", capability_version="1")
    row = _native_memory_transient_sql_row(proposal)
    assert row["provider_contact_started"] == 0
    assert type(row["provider_contact_started"]) is int
    assert type(row["created_at"]) is str
    assert type(row["privacy_state"]) is str


def test_missing_required_fields_and_forged_nonmodel_are_refused():
    from types import SimpleNamespace
    with pytest.raises(HeaderBoundsError, match="memory_transient_row_unavailable"):
        _native_memory_transient_sql_row(SimpleNamespace(__tablename__="memories"))
    with pytest.raises(HeaderBoundsError, match="memory_transient_bind_value_invalid"):
        _native_memory_transient_sql_row(Memory())


def test_original_audit_constructor_can_be_projected_without_attachment():
    event = AuditEvent(actor="operator", event_type="memory_forgotten", summary="Complete original audit")
    row = _native_memory_transient_sql_row(event)
    assert row["summary"] == "Complete original audit"
    assert type(row["created_at"]) is str
