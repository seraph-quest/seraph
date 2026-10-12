"""Compare capacity terms with the genuine original header owner, never a grant."""
import pytest


def test_future_row_header_terms_match_original_paid_sql_owner(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from src.audit.repository import audit_repository
    from src.db.models import AuditEvent
    from src.memory.composition_headers import _headers
    from src.memory.header_bounds import (
        COMPOSITION_DESCRIPTORS, HeaderReadBudget,
        _native_memory_projected_row_header_entries, _trace_memory_numeric_charges)

    engine = create_engine("sqlite:///" + str(tmp_path / "original-header.sqlite"))
    AuditEvent.__table__.create(engine)
    event = audit_repository._prepare_event(event_type="memory_learning_proposed",
        summary="escaped:\x7f\"ż😀", actor="operator:header", details={"private": "\x7f"})
    descriptor = COMPOSITION_DESCRIPTORS["audit_events"]
    try:
        with Session(engine) as db:
            db.add(event)
            db.flush()
            connection = db.connection()
            assert connection.connection.driver_connection.in_transaction is True
            frame = HeaderReadBudget()
            before = frame.remaining
            with _trace_memory_numeric_charges(frame) as observed:
                headers = _headers(connection, descriptor, (event.id,), _header_budget=frame)
            # The actual original SQL delivery, row-copy, encoded-copy, skeleton
            # and retained fact allocations are the independent comparison.
            projected = _native_memory_projected_row_header_entries(descriptor, event.id)
            assert tuple(observed) == projected
            assert len(observed) == 5
            assert before - frame.remaining == sum(amount for _, amount in observed)
            assert (descriptor.table, event.id) in headers
            assert not frame.future_references  # Numerical terms issue no reference.
    finally:
        engine.dispose()


def test_future_header_source_rejects_poison_owner_before_any_callback():
    from src.memory.header_bounds import HeaderBoundsError, _native_memory_source_superset_charges
    touches = []
    class Poison:
        def __getattribute__(self, name):
            touches.append(name)
            raise AssertionError("untrusted owner inspected")
    poison = Poison()
    with pytest.raises(HeaderBoundsError, match="memory_future_header_source_unavailable"):
        _native_memory_source_superset_charges(poison, poison, poison, poison, poison)
    assert touches == []
