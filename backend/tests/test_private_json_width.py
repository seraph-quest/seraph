"""Private JSON encoded-width proof; no runtime or authority substitution."""
import json
import os

import pytest

from src.memory.header_bounds import HeaderReadBudget, MAX_BYTES
from src.workspace.production import (
    ProductionWorkspaceError, _private_json_value_upper, _read_private_checkpoint,
)


@pytest.mark.parametrize("raw", [
    b'{"a": [null, true, false, -123, 0], "b": "plain ASCII"}',
    b' { "duplicate":1, "duplicate":2 } ',
    b'{"text":"decimal 1.2 exponent 3e4 NaN Infinity", "n":-9223372036854775808}',
    b'{"nested":{"list":[{},[],"",123456789012345678901234567890]}}',
])
def test_private_json_known_width_preserves_canonical_bound(raw):
    upper = _private_json_value_upper(raw)
    assert upper == len(raw)
    original = json.loads(raw)
    canonical = json.dumps(original, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    assert len(canonical) <= upper


@pytest.mark.parametrize("raw", [
    b'{"n":1.0}', b'{"n":1e9}', b'{"n":NaN}', b'{"n":Infinity}',
    b'{"text":"\\n"}', b'{"text":"\\u263a"}',
    '{"text":"é"}'.encode(), b'{"text":"\x7f"}', b'{"text":"\x01"}',
    b'{"unterminated":"value}',
])
def test_private_json_unclosed_shapes_keep_original_upper(raw):
    assert _private_json_value_upper(raw) == 6 * len(raw) + 2


def test_checkpoint_preserves_physical_read_and_three_value_charges(tmp_path):
    raw = b'{"secret_values_included":false,"schema_version":1}'
    path = tmp_path / "checkpoint.json"
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    budget = HeaderReadBudget()
    value = _read_private_checkpoint(path, header_budget=budget)
    assert value == json.loads(raw)
    assert budget.remaining == MAX_BYTES - (len(raw) + 1) - 3 * len(raw)
    assert path.read_bytes() == raw


def test_invalid_json_keeps_original_parser_rejection(tmp_path):
    raw = b'{"secret_values_included":false,"n":--1}'
    path = tmp_path / "checkpoint.json"
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    budget = HeaderReadBudget()
    with pytest.raises(ProductionWorkspaceError, match="checkpoint is invalid"):
        _read_private_checkpoint(path, header_budget=budget)
    assert budget.remaining == MAX_BYTES - (len(raw) + 1) - 3 * len(raw)
    assert path.read_bytes() == raw
