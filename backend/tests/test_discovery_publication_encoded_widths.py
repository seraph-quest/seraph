import json

import pytest

from src.workspace.production import _publication_json_upper


@pytest.mark.parametrize("text", [
    "", "a" * 64, '"\\', "\b\f\n\r\t", "\x00\x1f\x7f", "é中", "😀", "\ud800",
])
def test_publication_string_width_matches_original_ascii_json(text):
    encoded = json.dumps(text, ensure_ascii=True, separators=(",", ":")).encode()
    assert _publication_json_upper(text) == len(encoded)


def test_publication_width_counts_mapping_values_and_container_separators():
    value = {"target": {"digest": "a" * 64}, "files": ["é", "\x7f", None, False]}
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    assert _publication_json_upper(value) == len(encoded)
