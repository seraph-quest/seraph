"""Repository Stop metadata must fit before the original native child contacts."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.work_board.contracts import (GeneralTaskCurrentManifestV1, GeneralTaskNativeCancelV1,
    RepositoryNativeLimitEvidenceV1, RepositoryNativeStopClosureV1)
from src.workflows import general_task_guard as guard
from src.workflows.job_runtime import _canonical, DurableJobTransitionError
from tests.test_repository_stop_closure_contract import _binding, _child


def _counting_scope(binding, *, existing):
    parent = SimpleNamespace(run_identity=binding.parent_job_id, authority_digest="a" * 64,
        input_digest="b" * 64, checkpoint_receipts_json="[]")
    task = SimpleNamespace(input_artifact_id="input", typed_input_ref="input-ref",
        typed_input_digest="c" * 64, goal_id=binding.goal_id, goal_revision=1)
    manifest = GeneralTaskCurrentManifestV1(task_id=binding.task_id,
        original_root_id=binding.original_root_id, owner_principal_id=binding.owner_principal_id,
        attempt_id=binding.attempt_id, run_id=binding.parent_job_id,
        task_revision=1, manifest_revision=1, board_fence=1, job_fence=1,
        original_envelope_artifact_id="envelope", original_envelope_digest="a" * 64,
        original_input_digest="a" * 64, selected_grant_digest=binding.selected_grant_digest,
        group_id="a" * 64, group_digest="a" * 64, original_limits_digest="a" * 64,
        creation_digest=binding.creation_digest, original_deadline_at=binding.original_deadline_at,
        phase="native_wait", native_deadline_at=binding.native_deadline_at,
        phase_revision=1, phase_digest=binding.phase_digest, plan_revision=1,
        current_plan_artifact_id="envelope", current_plan_digest=binding.plan_digest,
        revision_numbers=[1], revision_artifact_ids=["envelope"],
        revision_artifact_digests=["a" * 64], revision_artifact_schemas=["GeneralTaskEnvelope.v1"],
        admitted_invocation_ids=[binding.invocation_id])
    row = SimpleNamespace(run_identity=binding.invocation_id,
        arguments_json='{"tool_id":"repository_work"}')

    class DB:
        async def execute(self, statement):
            return SimpleNamespace(scalars=lambda: [row] if existing else [])

    return DB(), parent, task, manifest


def _maximum_stop(binding):
    scalar = "\U00010000"
    deadline = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    evidence = RepositoryNativeLimitEvidenceV1(original_limits_digest="a" * 64,
        original_deadline_at=deadline, original_server_bound_microusd=10 ** 1500,
        root_liability_microusd=10 ** 1500, group_liability_microusd=10 ** 1500,
        group_calls=12, original_root_max_cost_microusd=10 ** 1500,
        original_group_max_cost_microusd=10 ** 1500, original_group_max_calls=12,
        goal_cutoff_at=deadline, cause="shared_group_exhausted")
    assert len(_canonical(evidence.model_dump(mode="json")).encode()) < 16384
    return RepositoryNativeStopClosureV1(original_binding=binding,
        repository_job_id=scalar * 256, repository_attempt_id=scalar * 128,
        repository_fence=2 ** 63 - 1, original_input_digest=binding.input_digest,
        source_checkpoint_digest="a" * 64, original_group_digest="a" * 64,
        original_deadline_at=deadline, original_claim_fence=2 ** 63 - 1,
        iteration_ids=[scalar * 127 + chr(0x10001 + index) for index in range(3)],
        stop_reason="shared_group_exhausted", limit_evidence=evidence, limit_evidence_digest="a" * 64,
        **{name: "a" * 64 for name in ("stop_intent_digest", "model_quiescence_digest",
            "process_quiescence_digest", "all_original_accounting_digest",
            "request_response_approval_digest", "source_binding_digest")})


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_repository_stop_closed_metadata_is_bounded_by_precontact_count(monkeypatch, existing):
    binding = _binding()
    db, parent, task, manifest = _counting_scope(binding, existing=existing)
    counted = []

    def capture(value):
        if isinstance(value, dict) and value.get("schema_version") == "general_task.native_cancel.v1":
            counted.append(value)
        return _canonical(value)

    monkeypatch.setattr(guard, "child_binding", lambda row: binding)
    monkeypatch.setattr("src.workflows.job_runtime._canonical", capture)
    await guard._ensure_future_cancel_capacity(db, parent, task, None, manifest,
        candidate=None if existing else binding, candidate_tool_id="repository_work")
    reserved = counted[-1]["children"][0]
    assert reserved["closure"] is None
    assert reserved["repository_closure"]["original_binding"] == binding.model_dump(mode="json")

    # Exercise actual closed contracts, including all three longest escaped
    # identities and very large valid integers with no invented numeric cap.
    closure = _maximum_stop(binding)
    actual = _child(binding=binding, repository_closure=closure).model_dump(mode="json")
    assert len(_canonical(actual).encode()) <= len(_canonical(reserved).encode())


@pytest.mark.asyncio
async def test_repository_precontact_blocks_history_that_generic_closure_can_fit(monkeypatch):
    binding = _binding()
    db, parent, task, manifest = _counting_scope(binding, existing=False)
    counted = []

    def capture(value):
        if isinstance(value, dict) and value.get("schema_version") == "general_task.native_cancel.v1":
            counted.append(value)
        return _canonical(value)

    monkeypatch.setattr("src.workflows.job_runtime._canonical", capture)
    # Each retained identity stays within the closed 128-codepoint identity
    # limit, and history remains below both the 50-record and 4-MiB bounds.
    history = [
        {"checkpoint_id": "general:" + str(index) + "\U00010000" * 117,
            "payload": {}, "safe": True} for index in range(38)]
    retained_repository = "repository:" + "\U00010000" * 117
    history.append({"checkpoint_id": retained_repository, "payload": {}, "safe": True})
    parent.checkpoint_receipts_json = _canonical(history)
    before = parent.checkpoint_receipts_json
    await guard._ensure_future_cancel_capacity(db, parent, task, None, manifest,
        candidate=binding, candidate_tool_id="web_search")
    generic_bytes = len(_canonical(counted[-1]).encode())
    assert generic_bytes + 512 <= 65536
    with pytest.raises(DurableJobTransitionError, match="future cancellation witness capacity"):
        await guard._ensure_future_cancel_capacity(db, parent, task, None, manifest,
            candidate=binding, candidate_tool_id="repository_work")
    repository_bytes = len(_canonical(counted[-1]).encode())
    assert repository_bytes > 65536
    assert repository_bytes - generic_bytes > 512
    required = counted[-1]["original_manifest"]["required_checkpoint_ids"]
    assert retained_repository in required
    future_ids = [identity for identity in required if identity not in {item["checkpoint_id"] for item in history}]
    assert len(future_ids) == 6
    for prefix, constructor in ((guard._REPOSITORY_CHILD_WAIT_PREFIX, guard.repository_child_wait_checkpoint_id),
            (guard._REPOSITORY_CHILD_FINAL_PREFIX, guard.repository_child_final_checkpoint_id)):
        assert len([identity for identity in future_ids if identity.startswith(prefix)]) == 3
        for iteration in _maximum_stop(binding).iteration_ids:
            actual_id = constructor(binding, iteration)
            assert len(actual_id) == len(prefix) + 64
            assert len(_canonical(actual_id).encode()) == len(_canonical(next(
                identity for identity in future_ids if identity.startswith(prefix))).encode())
    actual_payload = {**counted[-1], "children": [{**counted[-1]["children"][0],
        "repository_closure": _maximum_stop(binding).model_dump(mode="json")} ]}
    actual_witness = GeneralTaskNativeCancelV1.model_validate(actual_payload)
    assert 65536 < len(_canonical(actual_witness.model_dump(mode="json")).encode()) <= repository_bytes
    assert parent.checkpoint_receipts_json == before


@pytest.mark.asyncio
async def test_repository_future_checkpoint_count_blocks_full_original_history(monkeypatch):
    binding = _binding()
    db, parent, task, manifest = _counting_scope(binding, existing=False)
    history = [{"checkpoint_id": "general:existing:" + str(index),
        "payload": {"retained": True}, "safe": True} for index in range(42)]
    manifest = GeneralTaskCurrentManifestV1.model_validate({**manifest.model_dump(mode="json"),
        "required_checkpoint_ids": sorted(item["checkpoint_id"] for item in history)})
    parent.checkpoint_receipts_json = _canonical(history)
    parent.parent_job_id = None
    parent.job_kind = "general_task_v1"
    parent.artifact_receipts_json = "[]"
    before_original = parent.checkpoint_receipts_json
    # Exercise the real preceding reservation writer: its seven original
    # cancellation/continuation/tool slots bring 42 current IDs to 49, plus
    # its implicit manifest sentinel to the existing protected limit of 50.
    reserved_parent = guard._reserve_native_capacity(parent, manifest, binding)
    reserved_history = guard._history(reserved_parent)
    assert len(reserved_history) == 49
    assert len({guard.GENERAL_TASK_MANIFEST_KEY,
        *(item["checkpoint_id"] for item in reserved_history)}) == 50
    before_reserved = reserved_parent.checkpoint_receipts_json
    await guard._ensure_future_cancel_capacity(db, reserved_parent, task, None, manifest,
        candidate=binding, candidate_tool_id="web_search")
    with pytest.raises(DurableJobTransitionError, match="future cancellation checkpoint count capacity"):
        await guard._ensure_future_cancel_capacity(db, reserved_parent, task, None, manifest,
            candidate=binding, candidate_tool_id="repository_work")
    assert parent.checkpoint_receipts_json == before_original
    assert reserved_parent.checkpoint_receipts_json == before_reserved
