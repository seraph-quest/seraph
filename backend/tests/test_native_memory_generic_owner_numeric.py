"""Generic owner custody boundary; no fabricated REPORT inventory for forget."""
import pytest

from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins.memory_producer import original_native_memory_source_review_or_forget_numeric_schedule


@pytest.mark.parametrize("construction", [True, False])
def test_generic_owner_unissued_denies_before_private_access(construction):
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued generic owner reached DB")

        @property
        def source(self):
            raise AssertionError("unissued generic owner reached Source")

    with pytest.raises(HeaderBoundsError):
        original_native_memory_source_review_or_forget_numeric_schedule(
            Poison(), None, certificate=None, facts=None, retention_widths=None,
            _construction_only=construction)


@pytest.mark.parametrize("mode", [None, 0, 1, "true"])
def test_generic_owner_constructor_requires_literal_bool(mode):
    with pytest.raises(HeaderBoundsError, match="memory_generic_owner_constructor_mode_changed"):
        original_native_memory_source_review_or_forget_numeric_schedule(
            None, None, certificate=None, facts=None, retention_widths=None,
            _construction_only=mode)
