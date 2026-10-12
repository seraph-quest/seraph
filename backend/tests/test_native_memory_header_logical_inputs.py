"""Logical template bounds; these tests issue no positive Memory authority."""

import json

import pytest


@pytest.mark.parametrize("key", ['native-memory:' + 'a' * 64, '"\\' * 128, '😀' * 128])
def test_exact_input_bound_covers_original_quoted_key_and_schema_sql(key):
    from src.memory import header_bounds as bounds

    descriptor = bounds.WRS_BY_RUN
    # Independent original caller inputs, including escaping and maximum
    # UTF8 key size, must fit before the actual schema/header constructions.
    parameters = json.dumps({"key": key}, ensure_ascii=True, separators=(",", ":")).encode()
    schema_parameters = json.dumps({"table": descriptor.table,
        "limit": len(descriptor.columns) + 1}, separators=(",", ":")).encode()
    assert bounds._native_memory_exact_logical_upper(descriptor, (key,)) >= (
        2 * len(parameters) + 2 * len(schema_parameters) + 2 * 247)


def test_exact_logical_sizing_rejects_unregistered_descriptor():
    from src.memory import header_bounds as bounds

    with pytest.raises(bounds.HeaderBoundsError, match="header_descriptor_unavailable"):
        bounds._native_memory_exact_logical_upper(object(), ("row",))


def test_keyed_locator_bound_covers_two_original_return_rows():
    from src.memory import composition_headers as headers
    from src.memory import header_bounds as bounds

    descriptor = bounds.WORK_BOARD_TASK
    key = '"\\' * 128
    rows = [[2**63 - 1, "text", 512, key, None]] * 2
    encoded = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode()
    assert headers._native_memory_discover_logical_upper(descriptor, key, False, None) >= 3 * len(encoded)


def test_metadata_input_scalar_rejects_opaque_parameter_before_copy():
    from src.memory import composition_headers as headers
    from src.memory import header_bounds as bounds

    with pytest.raises(bounds.HeaderBoundsError, match="memory_header_logical_input_unavailable"):
        headers._native_memory_metadata_logical_width((object(),))
