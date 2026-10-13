import json
import sqlite3
import pytest


@pytest.mark.parametrize("value", [
    "ascii", 'quote"slash\\', "nul\0suffix", "\b\t\n\f\r\x1f\x7f",
    "é", "雪", "😀", "é" * 256, "😀" * 128, "a" * 512,
    "a" * 513, "é" * 257, "😀" * 129,
])
def test_pointer_hex_text_preserves_original_utf8_bytes_and_bound(value):
    # Pure SQL/codec equivalence; this creates no Source, owner or permission.
    with sqlite3.connect(":memory:") as db:
        original, encoded = db.execute(
            "SELECT CASE WHEN typeof(?)='text' AND octet_length(?) BETWEEN 1 AND 512 THEN ? END,"
            "CASE WHEN typeof(?)='text' AND octet_length(?) BETWEEN 1 AND 512 THEN hex(?) END",
            (value, value, value, value, value, value)).fetchone()
    if len(value.encode("utf-8")) > 512:
        assert original is None and encoded is None
    else:
        assert bytes.fromhex(encoded).decode("utf-8") == original == value
        assert len(encoded) <= 1024
        assert len(json.dumps(encoded)) <= 1026
