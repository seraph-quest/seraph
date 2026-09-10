"""Provider-free tests for the bounded harness-improvement runtime."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from src.evolution.runtime import (
    EvaluationRecord,
    EvolutionRuntime,
    EvolutionRuntimeError,
    binding_digest,
    candidate_hash,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _create(runtime: EvolutionRuntime, **changes):
    values = {
        "owner_id": "operator:test",
        "goal_id": "goal:brief",
        "goal_revision": 2,
        "baseline_pack_id": "pack:research",
        "baseline_pack_version": "1.0.0",
        "baseline_content_hash": _digest("baseline"),
        "candidate_hash": candidate_hash("candidate-v1"),
        "allowed_target_paths": ("skills/research.md",),
        "source_failure_ids": ("failure:1",),
        "authority_digest": _digest("authority"),
        "effective_model_binding": _digest("model-binding"),
        "corpus_manifest_hash": _digest("corpus"),
        "evaluator_hash": _digest("evaluator"),
        "development_split_hash": _digest("development"),
        "hidden_split_hash": _digest("hidden"),
        "egress_manifest_hash": _digest("egress"),
        "budget_microusd": 50_000,
        "deadline_seconds": 600,
        "token_budget": 10_000,
    }
    values.update(changes)
    return runtime.create_proposal(**values)


def test_proposal_persists_and_staged_state_is_not_active(tmp_path):
    state = tmp_path / "evolution" / "runtime-state.json"
    runtime = EvolutionRuntime(state)
    proposal = _create(runtime)
    assert proposal["state"] == "proposed"
    assert proposal["measured_result"] == {"status": "not_run"}
    assert proposal["result"] == "no_learning_no_promotion"

    restored = EvolutionRuntime(state)
    assert restored.get(proposal["proposal_id"])["proposal_id"] == proposal["proposal_id"]
    assert restored.list()[0]["active_version_after"] == ""


def test_measured_candidate_requires_exact_bindings_and_rolls_back(tmp_path):
    runtime = EvolutionRuntime(tmp_path / "state.json")
    proposal = _create(runtime)
    proposal = runtime.screen(
        proposal["proposal_id"], structural_pass=True, safety_pass=True,
        candidate_hash=proposal["candidate_hash"],
    )
    records = [
        EvaluationRecord("task:1", "correctness", "pass", True, True, tokens=100),
        EvaluationRecord("task:2", "correctness", "pass", False, True, tokens=100),
    ]
    proposal = runtime.evaluate(
        proposal["proposal_id"], records=records,
        candidate_hash=proposal["candidate_hash"],
        evaluator_hash=proposal["evaluator_hash"],
        corpus_manifest_hash=proposal["corpus_manifest_hash"],
        hidden_split_hash=proposal["hidden_split_hash"],
    )
    assert proposal["measured_result"]["status"] == "measured"
    assert proposal["measured_result"]["promotion"] == "no_promotion"

    expires = (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    bindings = {
        "approval_id": "approval:1",
        "owner_id": proposal["owner_id"],
        "goal_id": proposal["goal_id"],
        "goal_revision": proposal["goal_revision"],
        "baseline_content_hash": proposal["baseline_content_hash"],
        "candidate_hash": proposal["candidate_hash"],
        "authority_digest": proposal["authority_digest"],
        "evaluator_hash": proposal["evaluator_hash"],
        "expires_at": expires,
        "active_version_before": "1.0.0",
        "active_version_after": "candidate-v1",
    }
    proposal = runtime.approve_canary(
        proposal["proposal_id"], approval_id="approval:1",
        approval_digest=binding_digest(bindings), owner_id=proposal["owner_id"],
        goal_id=proposal["goal_id"], goal_revision=proposal["goal_revision"],
        baseline_content_hash=proposal["baseline_content_hash"],
        candidate_hash=proposal["candidate_hash"], authority_digest=proposal["authority_digest"],
        evaluator_hash=proposal["evaluator_hash"], expires_at=expires,
        active_version_before="1.0.0", active_version_after="candidate-v1",
    )
    assert proposal["state"] == "canary"
    rolled = runtime.record_canary(
        proposal["proposal_id"], approval_id="approval:1", job_ids=("job:1",),
        outcome="success", baseline_still_permitted=True,
    )
    assert rolled["state"] == "rolled_back"
    assert rolled["result"] == "no_learning_no_promotion"
    assert rolled["rollback_version"] == "1.0.0"
    assert rolled["job_ids"] == ["job:1"]


def test_failures_pause_and_terminal_proposals_cannot_reactivate(tmp_path):
    runtime = EvolutionRuntime(tmp_path / "state.json")
    proposal = _create(runtime, token_budget=1)
    proposal = runtime.screen(
        proposal["proposal_id"], structural_pass=True, safety_pass=True,
        candidate_hash=proposal["candidate_hash"],
    )
    paused = runtime.evaluate(
        proposal["proposal_id"],
        records=[EvaluationRecord("task:1", "recovery", "pass", True, True, tokens=2)],
        candidate_hash=proposal["candidate_hash"], evaluator_hash=proposal["evaluator_hash"],
        corpus_manifest_hash=proposal["corpus_manifest_hash"], hidden_split_hash=proposal["hidden_split_hash"],
    )
    assert paused["state"] == "paused"
    assert paused["measured_result"]["reason"] == "token_budget_exhausted"
    resumed = runtime.screen(
        proposal["proposal_id"], structural_pass=True, safety_pass=True,
        candidate_hash=proposal["candidate_hash"],
    )
    assert resumed["state"] == "awaiting_review"

    rejected = _create(runtime, proposal_id="evo-rejected")
    rejected = runtime.reject(rejected["proposal_id"], reason="unsafe_candidate")
    with pytest.raises(EvolutionRuntimeError, match="illegal evolution transition"):
        runtime.screen(rejected["proposal_id"], structural_pass=True, safety_pass=True, candidate_hash=rejected["candidate_hash"])


def test_hash_drift_paths_and_limits_fail_closed(tmp_path):
    runtime = EvolutionRuntime(tmp_path / "state.json")
    with pytest.raises(EvolutionRuntimeError, match="authority or secret"):
        _create(runtime, allowed_target_paths=("policy/active.yaml",))
    with pytest.raises(EvolutionRuntimeError, match="three candidate"):
        _create(runtime, variant_count=4)

    proposal = _create(runtime, proposal_id="evo-drift")
    with pytest.raises(EvolutionRuntimeError, match="candidate hash changed"):
        runtime.screen(proposal["proposal_id"], structural_pass=True, safety_pass=True, candidate_hash=_digest("other"))
    with pytest.raises(EvolutionRuntimeError, match="illegal evolution transition"):
        runtime.rollback(proposal["proposal_id"])
