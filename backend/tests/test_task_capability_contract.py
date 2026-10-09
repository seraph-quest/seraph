"""Finite source contract negatives; these are not native producer receipts."""
import copy
import hashlib
import json

import pytest

from src.runtime_plugins.ownership import (
    CompositionBindingError, CompositionDependency, RuntimeCompositionBinding,
    method_closure, method_dependencies,
)
from src.runtime_plugins.task_capability import (
    REPORT_CHECKPOINT_IDS, ReportAdmissionCandidate, preflight_report_journal,
    validate_report_spec,
)
from src.work_board.repository import BoardError


def test_exact_report_manifest_and_old_capture_cannot_refresh_authority():
    report = method_closure("tasks.admit", "artifact")
    assert report == tuple(sorted({"tasks.admit", "tasks.inspect", "tasks.cancel", "tasks.checkpoint", "tasks.settle",
        "capabilities.invoke", "artifacts.read", "artifacts.stage", "artifacts.adopt"}))
    assert method_closure("tasks.admit", "workflow") == tuple(sorted(set(report) - {"capabilities.invoke"}))
    domains = set(method_dependencies("tasks.admit", native_branch="artifact"))
    for method in report:
        domains.update(method_dependencies(method))
    assert "seraph.capabilities.v1" in domains
    binding = RuntimeCompositionBinding("seraph.tasks.v1", "tasks.admit", "artifact", report,
        tuple(CompositionDependency(domain, "cordis", 1, "a" * 64) for domain in sorted(domains)), "b" * 64, "c" * 64)
    payload = binding.payload()
    old_methods = tuple(method for method in report if method != "capabilities.invoke")
    payload["allowed_child_methods"] = list(old_methods)
    source = {"schema_version": "runtime-service-methods.v1", "origin_method": "tasks.admit", "native_branch": "artifact",
        "allowed_child_methods": old_methods, "origin_dependencies": method_dependencies("tasks.admit", native_branch="artifact"),
        "child_dependencies": [[method, method_dependencies(method)] for method in old_methods]}
    canonical = lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload["method_manifest_digest"] = hashlib.sha256(canonical(source).encode()).hexdigest()
    payload["binding_digest"] = hashlib.sha256(canonical(payload).encode()).hexdigest()
    with pytest.raises(CompositionBindingError):
        RuntimeCompositionBinding.from_json(canonical(payload))
    assert not ("capabilities.invoke" in old_methods)


def test_all_missing_phase_slots_reserved_without_truncating_history():
    ordinary = [{"checkpoint_id": f"ordinary:{index}", "payload": {}} for index in range(44)]
    preflight_report_journal(ordinary)
    unchanged = copy.deepcopy(ordinary)
    with pytest.raises(ValueError, match="capacity unavailable"):
        preflight_report_journal(ordinary + [{"checkpoint_id": "ordinary:44", "payload": {}}])
    assert ordinary == unchanged
    with pytest.raises(ValueError, match="capacity unavailable"):
        preflight_report_journal([{"checkpoint_id": "ordinary:large", "payload": "x" * 1048576}])
    with pytest.raises(ValueError, match="duplicate protected"):
        preflight_report_journal([{"checkpoint_id": REPORT_CHECKPOINT_IDS[0], "payload": {}}] * 2)


@pytest.mark.asyncio
async def test_forged_or_copied_report_candidate_denied_before_canonical_contact():
    from tests.test_durable_job_runtime import _spec
    spec = _spec()
    class NoContact:
        info = {"native_writer_started": True, "composition_writer_owner": "durable_jobs"}
        def __getattr__(self, name):
            raise AssertionError("forged candidate reached canonical contact")
    forged = ReportAdmissionCandidate(spec, None, b"{}")
    for candidate in (forged, copy.copy(forged), copy.deepcopy(forged)):
        original_spec = candidate.original_spec
        with pytest.raises(BoardError, match="Original owner-issued report source required"):
            await validate_report_spec(NoContact(), original_spec, candidate, "d" * 64)
