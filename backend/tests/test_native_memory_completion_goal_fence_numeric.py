"""Completion resource gates reject unissued objects before private properties."""
import pytest
from src.memory.header_bounds import HeaderBoundsError
from src.workflows.job_runtime import _native_memory_completion_goal_fence_payer

def test_unissued_goal_payer_denies_before_claim_or_database_access():
    class Unissued:
        @property
        def claim(self):
            raise AssertionError("unissued source claim accessed")
    class UnownedDB:
        @property
        def info(self):
            raise AssertionError("unissued source reached database")
    with pytest.raises(HeaderBoundsError, match="memory_completion_goal_fence_numeric_owner_changed"):
        _native_memory_completion_goal_fence_payer(UnownedDB(), Unissued())
