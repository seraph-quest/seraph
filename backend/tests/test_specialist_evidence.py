"""Closed copied handoff contracts; lifecycle proof lives in runtime tests."""
import hashlib
import pytest
from src.work_board.contracts import SpecialistEvidenceHandoffV1
from src.workflows.specialist_evidence import validate_evidence_pointers


def handoff(content='{"output":{"content":"selected bytes"}}'):
    raw = content.encode()
    return dict(parent_job_id="parent", creation_digest="a" * 64,
        invocation_id="native", request_digest="b" * 64, child_task_id="child",
        owner_principal_id="owner", original_root_id="root", group_digest="c" * 64,
        producer_tokens=["d" * 64] * 5, vault_state_digest="e" * 64,
        entries=[dict(reference="board-output:producer", producer_revision=1,
            producer_attempt_ref="attempt", file_path="artifacts/selected.json",
            content_sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw), content=content)])


def test_exact_copied_bytes_and_rows():
    copied = SpecialistEvidenceHandoffV1.model_validate(handoff())
    assert copied.entries[0].content == '{"output":{"content":"selected bytes"}}'


@pytest.mark.parametrize("change", ["digest", "size", "rows", "extra", "oversize"])
def test_changed_or_expanded_copy_rejected(change):
    payload = handoff()
    if change == "digest":
        payload["entries"][0]["content_sha256"] = "f" * 64
    elif change == "size":
        payload["entries"][0]["size_bytes"] += 1
    elif change == "rows":
        payload["producer_tokens"].pop()
    elif change == "extra":
        payload["entries"][0]["secret"] = "private"
    else:
        payload = handoff('"' + "x" * 32760 + '"')
    with pytest.raises(ValueError):
        SpecialistEvidenceHandoffV1.model_validate(payload)


@pytest.mark.parametrize("pointer", [
    {"from_evidence":"board-output:other","pointer":"/output"},
    {"from_evidence":"board-output:producer","pointer":"/bad~escape"},
    {"from_evidence":"board-output:producer","pointer":"/output","literal":"sibling"},
])
def test_pointer_scope_and_shape_rejected(pointer):
    with pytest.raises(ValueError):
        validate_evidence_pointers({"content":pointer}, ["board-output:producer"])


def test_pointer_only_selected_reference():
    validate_evidence_pointers({"content":{"from_evidence":"board-output:producer",
        "pointer":"/output/content"}}, ["board-output:producer"])
