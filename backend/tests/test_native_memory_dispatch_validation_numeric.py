"""One-call Source allocation forecast cannot grant an original owner."""
import json
from types import MappingProxyType
import pytest
from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins.memory_producer import (
    _native_memory_dispatch_witness_upper, _native_memory_dispatch_witness_entries,
    _native_memory_dispatch_binding_json_entries,
    original_native_memory_dispatch_validation_numeric_schedule,
)


def test_original_witness_width_covers_escaping_and_signed_integer_values():
    witness = MappingProxyType({"invocation_ref":"ł\n\"😀", "revision":-(2**63),
        "allowed_child_methods":("memory.propose", "memory.forget")})
    upper = _native_memory_dispatch_witness_upper(witness)
    actual = json.dumps(dict(witness), ensure_ascii=True, separators=(",",":"))
    assert upper >= len(actual.encode("utf-8"))
    entries = _native_memory_dispatch_witness_entries(91, upper)
    assert len(entries) == 11
    assert entries[:2] == (("witness-json-parser",548),("witness-selected-list",548))
    assert dict(entries)["witness-digest-return"] == 66
    assert dict(entries)["witness-return-copy"] == upper


def test_to_json_prices_both_payloads_and_manifests_without_a_fictitious_parser():
    entries = _native_memory_dispatch_binding_json_entries(2048)
    names = [name for name, upper in entries]
    assert ("payload",0,"binding-payload") in names
    assert ("payload",1,"binding-payload") in names
    assert ("manifest",0,"manifest-canonical") in names
    assert ("manifest",1,"manifest-canonical") in names
    assert "json-parser" not in names
    assert names.count("digest-return") == 1
    assert dict(entries)["final-dict-merge"] == 12290


@pytest.mark.parametrize("result_present",[False,True])
def test_unissued_one_call_numeric_request_denies_before_private_source(result_present):
    class Unissued:
        @property
        def source(self):
            raise AssertionError("unissued owner reached original Source")
    with pytest.raises(HeaderBoundsError, match="memory_dispatch_validation_numeric_owner_changed"):
        original_native_memory_dispatch_validation_numeric_schedule(Unissued(), object(),
            certificate=object(),facts=MappingProxyType({}),retention_widths=MappingProxyType({}),
            result_present=result_present)
