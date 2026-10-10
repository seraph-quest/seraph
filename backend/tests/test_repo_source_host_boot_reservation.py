"""Missing/closed-shape and genuine same-boot copied-journal negatives only.

No accepted cross-boot proof is created. Independent digest, readback, outcome,
scope and identity-crossover gates need lawful changed-boot evidence elsewhere;
adding those mutations to an already invalid same-boot proof would mask them.
"""
import copy
import hashlib
import json
import os

import pytest

from src.execution import repo_original_producer as producer
from src.workflows.job_runtime import DurableJobTransitionError, _canonical, _digest
from src.workflows import repo_repair_source as source
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_source_recovered_finalizer import _signed_ownerless_fixture, _rows


def _private_files(service):
    """Private byte equality by hash, plus literal links; never emitted."""
    root = service._workspace()
    return {str(path.relative_to(root)):
        ("symlink", os.readlink(path)) if path.is_symlink() else
        ("file", path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in root.rglob("*") if path.is_symlink() or path.is_file()}


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_physical", "physical_shape", "same_boot"])
async def test_original_host_boot_release_parser_rejects_negative_copied_journals(
        accounting_db, monkeypatch, repository_admission_signer, damage):
    flow = await _signed_ownerless_fixture(accounting_db, monkeypatch, "test_python", unknown=True)
    jobs, registration = flow["jobs"], flow["registration"]
    async with jobs._session() as db:
        original = await jobs._fetch(db, flow["kwargs"]["job_id"])
        assert original.status == "unknown_external_effect"
        held = jobs._repo_repair_reservation_state(original)
        assert held["status"] == "held"
        assert source._repository_record(original, "repository:physical-cleanup:v1") is None
        assert source._repository_record(original, "repository:terminal:v1") is None
        assert source.read_repository_inventory(original)["schema"] in {
            "repository.checkpoint_inventory.v3", "repository.checkpoint_inventory.v4"}
        db.expunge(original)
    rows, files = await _rows(jobs), _private_files(flow["service"])
    original_bytes = _canonical(original.model_dump(mode="json"))
    observed_boot = producer._host_boot_id()
    assert observed_boot == registration["native_host_binding"]["boot_id"]

    # This detached model-copy is never written or supplied to a mutation owner.
    # Its same-boot value intentionally cannot satisfy changed-host authority.
    physical = {"readback_scope": "original_host_boot_cleanup", "cleanup_proven": True,
        "readback_verified": False, "outcome_status": "unknown_external_effect",
        "producer_registration_digest": _digest(registration),
        "original_boot_id": registration["native_host_binding"]["boot_id"],
        "observed_boot_id": observed_boot, "stage_identity": registration["stage_identity"],
        "job_id": original.run_identity, "attempt_id": registration["repository_attempt_id"],
        "fence": original.fencing_token, "authority_digest": original.authority_digest,
        "execution_deadline_at": held["execution_deadline_at"],
        "iteration_id": registration["iteration_id"], "before_revision": original.revision,
        "post_revision": original.revision + 1}
    release = {"kind": "repo_repair_execution_reservation", "status": "released",
        "job_id": physical["job_id"], "attempt_id": physical["attempt_id"], "fence": physical["fence"],
        "authority_digest": physical["authority_digest"], "execution_deadline_at": physical["execution_deadline_at"],
        "outcome_status": physical["outcome_status"], "cleanup_proven": True,
        "readback_verified": physical["readback_verified"], "readback_scope": "original_host_boot_cleanup",
        "physical_cleanup_digest": _digest(physical), "operator_visible": True,
        "recorded_at": held["recorded_at"]}
    journal = copy.deepcopy(json.loads(original.checkpoint_receipts_json))
    if damage != "missing_physical":
        # Closed shape is rejected before fields (including boot IDs) are read.
        # Keeping no boot field makes this independent of same-boot denial.
        record = {} if damage == "physical_shape" else physical
        journal.append({"checkpoint_id": "repository:physical-cleanup:v1", "safe": True,
            "payload": record, "state_digest": _digest(record)})
    journal.append({"checkpoint_id": "repo-repair-execution-release", "safe": True,
        "payload": release, "state_digest": _digest(release)})
    negative = original.model_copy(update={"revision": physical["post_revision"],
        "checkpoint_receipts_json": _canonical(journal)})
    with pytest.raises(DurableJobTransitionError,
            match="^original repository host boot cleanup release proof is malformed$"):
        jobs._repo_repair_reservation_state(negative)
    assert _canonical(original.model_dump(mode="json")) == original_bytes
    assert jobs._repo_repair_reservation_state(original) == held
    assert await _rows(jobs) == rows
    assert _private_files(flow["service"]) == files
