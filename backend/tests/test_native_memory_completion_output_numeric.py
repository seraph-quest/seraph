"""Completion output capacity uses the original sealed plan, never scalars."""
from types import MappingProxyType
import pytest
from src.memory.header_bounds import HeaderBoundsError
from src.workflows.job_runtime import (
    _native_memory_plain_copy_upper, _native_plain, _discovery_encoded_upper,
    original_native_memory_completion_output_numeric_schedule,
    original_native_memory_completion_startup_call_vector,
)


def test_original_plain_copy_prices_every_frozen_nested_container():
    children = (MappingProxyType({"ref":"ł😀"}), MappingProxyType({"revision":-(2**63)}))
    frozen = MappingProxyType({"payload":MappingProxyType({"refs":children}),"safe":True})
    actual = _native_plain(frozen)
    assert actual == {"payload":{"refs":[{"ref":"ł😀"},{"revision":-(2**63)}]},"safe":True}
    upper = (_discovery_encoded_upper(frozen) + _discovery_encoded_upper(frozen["payload"])
        + _discovery_encoded_upper(children) + sum(_discovery_encoded_upper(item) for item in children))
    assert _native_memory_plain_copy_upper(frozen) == upper
    assert upper > _discovery_encoded_upper(actual)


def test_unissued_completion_forecast_denies_before_source_or_resource_debit():
    class Unissued:
        @property
        def source(self):
            raise AssertionError("unissued completion forecast accessed Source")
        @property
        def db(self):
            raise AssertionError("unissued completion forecast reached DB")
    with pytest.raises(HeaderBoundsError,match="native_memory_completion_numeric_owner_changed"):
        original_native_memory_completion_output_numeric_schedule(Unissued(),object(),
            certificate=object(),facts=MappingProxyType({}),retention_widths=MappingProxyType({}))


def test_original_completion_counts_keep_reader_and_both_writer_snapshot_checks():
    vector = original_native_memory_completion_startup_call_vector()
    assert len(vector) == 16
    assert dict(vector)["prepare_composition_session.checked_snapshot"] == 1
    assert dict(vector)["begin_native_writer.certify_composition_superset"] == 1
    assert dict(vector)["begin_native_writer._prepare_composition_writer_session"] == 1
    assert dict(vector)["original_engine.publish"] == 1
