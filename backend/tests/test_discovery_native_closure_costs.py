"""Source-owned native codecs: prospective capacity is never domain authority."""
import hashlib
import json
from types import MappingProxyType

import pytest

from src.memory import composition_headers
from src.memory.header_bounds import HeaderBoundsError
from src.workspace import accounting_witness as witness
from src.workspace.production import _publication_json_upper


def test_row_codec_prepays_every_appearance_before_original_json(monkeypatch):
    fields = witness.COMPOSITION_FIELDS["runtime_composition_states"]
    row = {field: "é\n\\\"😀" if field != "epoch" else 2 for field in fields}
    key = row["runtime_domain"]
    encoded = json.dumps([witness.COMPOSITION_SCHEMA, "runtime_composition_states", key,
        [[field, row[field]] for field in fields]], ensure_ascii=False, separators=(",", ":")).encode()
    expected = hashlib.sha256(b"seraph-continuity-row-v1\0" + encoded).hexdigest()
    paid = []
    class Budget:
        def debit(self, amount, *, appearance):
            paid.append((appearance, amount))
    monkeypatch.setattr(composition_headers, "current_budget", lambda: Budget())
    original = witness.json.dumps
    def checked(*args, **kwargs):
        stages = {appearance[3]: upper for appearance, upper in paid}
        assert {"field-pairs", "payload", "json", "utf8", "hash-preimage", "hash-input", "digest-return"} <= stages.keys()
        assert stages["json"] >= len(encoded)
        assert stages["utf8"] >= len(encoded)
        assert stages["hash-input"] >= len(encoded) + len(b"seraph-continuity-row-v1\0")
        return original(*args, **kwargs)
    monkeypatch.setattr(witness.json, "dumps", checked)
    assert witness.composition_row_digest("runtime_composition_states", key, row) == expected


def test_row_codec_rejects_before_json_when_frame_cannot_pay(monkeypatch):
    row = {field: None for field in witness.COMPOSITION_FIELDS["runtime_composition_states"]}
    class Budget:
        def debit(self, amount, *, appearance):
            raise HeaderBoundsError("canonical_bound_not_certified")
    monkeypatch.setattr(composition_headers, "current_budget", lambda: Budget())
    monkeypatch.setattr(witness.json, "dumps", lambda *args, **kwargs: pytest.fail("unpaid JSON allocation"))
    with pytest.raises(HeaderBoundsError):
        witness.composition_row_digest("runtime_composition_states", "runtime", row)


@pytest.mark.parametrize("persisted", [False, True])
def test_future_native_schedule_keeps_retained_roots_and_counts_new_body_twice(persisted):
    job_id = "goal-discovery:" + "a" * 32
    constructor = MappingProxyType({"job_id": job_id, "row_header_bytes": 4096,
        "persisted_queue_only": persisted})
    widths = MappingProxyType({"native_bytes": 2000, "target_bytes": 3000, "members_bytes": 512})
    retained = (("native-row-digest", "workflow_run_states", "retained", "json"), 100)
    tail = ("closure-prefix", "visited-sort", "table-counts", "inventory-list",
        "inventory-json", "inventory-utf8", "inventory-hash", "inventory-digest-return", "role-counts",
        "relations-sort", "relations-json", "relations-utf8", "relations-hash", "artifacts-sort",
        "artifacts-json", "artifacts-utf8", "artifacts-hash", "closure-digest-return",
        "relations-digest-return", "artifacts-digest-return", "result", "visited-return")
    trace = (retained, *((("native-closure-tail", stage), 100) for stage in tail),
        *((("programme-snapshot-envelope", stage), 2000)
            for stage in ("construction", "return", "leaves-set", "members-union")))
    if persisted:
        trace += witness._original_native_row_digest_entries("workflow_run_states", job_id, 2048)
    result = witness._original_native_closure_projected_entries(trace,
        constructor=constructor, publication_widths=widths)
    assert retained in result
    assert result.count((("body", "workflow_run_states", job_id), 4096)) == (0 if persisted else 2)
    assert (("native-closure-tail", "result"), 2000) in result
    assert (("programme-snapshot-envelope", "construction"), 3000) in result
    sort = next(upper for appearance, upper in result if appearance == ("native-closure-tail", "visited-sort"))
    assert sort == 100 + (0 if persisted else _publication_json_upper(("workflow_run_states", job_id)) + 1)
    assert sum(appearance[:3] == ("native-row-digest", "workflow_run_states", job_id)
        for appearance, upper in result) == 10


def test_future_native_schedule_fails_closed_without_component_width():
    constructor = MappingProxyType({"job_id": "goal-discovery:" + "a" * 32, "row_header_bytes": 4096})
    with pytest.raises(HeaderBoundsError, match="programme_native_closure_numeric_unavailable"):
        witness._original_native_closure_projected_entries((), constructor=constructor,
            publication_widths=MappingProxyType({"target_bytes": 3000, "members_bytes": 512}))
