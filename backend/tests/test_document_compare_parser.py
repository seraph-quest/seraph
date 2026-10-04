"""Literal grammar and allocation-sensitive stream checks for ADR017."""
import zlib
import pytest
from src.work_board.document_compare_parser import DocumentParseError, csv_rows, strict_flate


def test_internal_hyphen_and_exact_decimal_operands():
    rows = csv_rows(b"SKU,QTY,UNIT_PRICE\nPEN-01,3,0.10\n")
    assert rows["PEN-01"]["total"] == "0.30"
    assert rows["PEN-01"]["citation"] == {"csv_row": 2}
    assert rows["PEN-01"]["formula"] == "3 * 0.10"


@pytest.mark.parametrize("sku", ["-PEN", "+PEN", "@PEN", "=PEN", "PEN/01", "PEN 01", "PEN\t01", "PÉN", "PEN\x00"])
def test_literal_sku_rejects_formula_controls_and_non_ascii(sku):
    with pytest.raises(DocumentParseError):
        csv_rows(("SKU,QTY,UNIT_PRICE\n" + sku + ",2,1.25\n").encode())


@pytest.mark.parametrize("quantity,price", [("0", "1.00"), ("1000001", "1.00"), ("01", "1.00"), ("1", "1e2"), ("1", "-1.00"), ("1", "1.001")])
def test_no_numeric_coercion(quantity, price):
    with pytest.raises(DocumentParseError):
        csv_rows(f"SKU,QTY,UNIT_PRICE\nA,{quantity},{price}\n".encode())


def test_flate_requires_complete_single_member_and_enforces_remaining_budget():
    encoded = zlib.compress(b"a" * 100)
    assert strict_flate(encoded, 100) == b"a" * 100
    for invalid, remaining in [(encoded[:-1], 100), (encoded + b"tail", 100), (encoded, 99), (encoded, 0)]:
        with pytest.raises(DocumentParseError):
            strict_flate(invalid, remaining)


def test_csv_duplicate_and_embedded_quoted_newline_are_rejected():
    for source in [b"sku,qty,unit_price\nA,1,1.00\n", b"SKU,QTY,UNIT_PRICE\nA,1,1.00\nA,2,1.00\n", b'SKU,QTY,UNIT_PRICE\n"A\nB",1,1.00\n']:
        with pytest.raises(DocumentParseError):
            csv_rows(source)
