"""Pure constructor bounds do not issue Memory ownership or body authority."""
import json

import pytest

from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins import memory_producer


@pytest.mark.parametrize("value", [None, 0, 1, "true", (), {}])
def test_source_constructor_bound_requires_exact_result_branch(value):
    with pytest.raises(HeaderBoundsError, match="memory_dispatch_planning_shape_unavailable"):
        memory_producer._original_native_memory_source_schedule_planning_upper(
            result_present=value)


@pytest.mark.parametrize("result_present", [False, True])
def test_source_constructor_bound_covers_actual_closed_vectors_and_binding_builders(
        monkeypatch, result_present):
    from src.workflows import inference_accounting
    observed = []
    original = inference_accounting._original_native_binding_numeric_entries

    def observe(*args, **kwargs):
        actual = original(*args, **kwargs)
        observed.append(actual)
        return actual

    monkeypatch.setattr(inference_accounting, "_original_native_binding_numeric_entries", observe)
    candidate = memory_producer.original_native_memory_source_candidate_numeric_entries(
        candidate_upper=98306)
    context = memory_producer.original_native_memory_source_context_numeric_entries(
        context_upper=524288, candidate_upper=98306, binding_text_bytes=32768,
        result_upper=524288, effect_upper=524288, locator_upper=524288,
        result_present=result_present)
    assert len(candidate) == 9
    assert len(context) == (120 if result_present else 108)
    assert len(observed) == 2
    assert all(len(vector) == 60 for vector in observed)
    # These are genuine existing pure numeric builders, never a fake Source,
    # future certificate or supplied fee list. Even their returned containers
    # alone must fit; the bound also reserves their intermediates separately.
    encoded = sum(len(json.dumps(vector, separators=(",", ":"), ensure_ascii=True))
        for vector in (candidate, context, *observed))
    bound = memory_producer._original_native_memory_source_schedule_planning_upper(
        result_present=result_present)
    assert type(bound) is int and bound >= encoded
    if result_present:
        assert bound > memory_producer._original_native_memory_source_schedule_planning_upper(
            result_present=False)


@pytest.mark.parametrize("typed", [False, True])
def test_dispatch_constructor_rejects_unissued_owner_before_any_private_access(typed):
    if typed:
        operation = object.__new__(memory_producer._MemoryOwnerSourceOperation)
    else:
        class Unissued:
            @property
            def db(self):
                raise AssertionError("planning reached an unissued database")

            @property
            def source(self):
                raise AssertionError("planning reached an unissued Source")
        operation = Unissued()
    with pytest.raises(HeaderBoundsError, match="memory_dispatch_validation_numeric_owner_changed"):
        memory_producer._original_native_memory_dispatch_planning_upper(
            operation, object(), certificate=object(), facts=object(), retention_widths=object())
