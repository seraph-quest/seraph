"""Deletion marker resource boundary; genuine positives live in dispatch test."""
import pytest

from src.memory.header_bounds import HeaderBoundsError, HeaderReadBudget
from src.memory import repository


def test_unissued_marker_payer_denies_before_original_parser(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unissued resource payer reached original parser")
    monkeypatch.setattr(repository.json, "loads", forbidden)
    budget = HeaderReadBudget()
    before = budget.remaining
    with pytest.raises(HeaderBoundsError, match="memory_generic_marker_numeric_owner_changed"):
        repository._canonical_memory_deletion_marker(None,
            _memory_header_budget=budget, _memory_header_certificate=None)
    assert budget.remaining == before


@pytest.mark.parametrize("metadata, expected", [
    ('{"archived_reason":" OPERATOR_DELETE_EXPORT "}', "archived_reason=operator_delete_export"),
    ('{"operator_control":{"delete_export_state":"canonical_memory_redacted"}}',
        "delete_export_state=canonical_memory_redacted"),
    ('{"last_action":"PROPAGATE_DELETE_EXPORT"}', "last_action=propagate_delete_export"),
    ("{}", None),
    ("broken", "metadata=malformed_suppressed_memory"),
])
def test_original_unpaid_marker_sibling_semantics(metadata, expected):
    from src.db.models import Memory, MemoryStatus
    memory = Memory(content="original", summary="original", metadata_json=metadata,
        status=MemoryStatus.archived)
    assert repository._canonical_memory_deletion_marker(memory) == expected
