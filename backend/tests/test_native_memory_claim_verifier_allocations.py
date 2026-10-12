"""Independent equality to original60-price grammar, no production tests run here."""
import pytest
from src.workflows.job_runtime import _native_memory_claim_binding_parser_upper, _native_memory_claim_producer_roster_upper
from src.workflows.inference_accounting import _original_native_binding_numeric_entries

@pytest.mark.parametrize("text_bytes",[0,1,8192])
def test_scalar_preallocation_price_equals_complete_original_binding_grammar(text_bytes):
    original = _original_native_binding_numeric_entries(None,text_bytes)
    assert len(original) == 60
    assert _native_memory_claim_binding_parser_upper(text_bytes) == sum(amount for label,amount in original)

def test_closed_producer_roster_includes_all_nine_original_branches_and_copies():
    #22 actual original tuples, both sets and full dict are separate appearances.
    pair = 3 + 2*(6*32+2)
    assert _native_memory_claim_producer_roster_upper() >= 22*pair*2 + pair + 2
