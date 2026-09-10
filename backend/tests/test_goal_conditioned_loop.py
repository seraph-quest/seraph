"""Focused receipts and revision safety for the goal-conditioned v1 slice."""

from src.goals.contracts import (
    GoalCandidateAction,
    GoalCandidateRequest,
    GoalExecutionResult,
    GoalSuccessCriterion,
)
from src.goals.repository import goal_repository
from src.goals.repository import deserialize_success_criterion, serialize_success_criterion
from src.guardian.goal_conditioned_loop import (
    _existing_receipt,
    _redact_receipt_details,
    _sanitize_strategy_delta_receipt,
    _safe_digest,
    build_goal_candidate_decision,
    dispatch_goal_candidate,
    list_goal_loop_receipts,
    propose_goal_candidate,
    _resolve_strategy_delta_provenance,
)


def test_malformed_stored_criterion_can_never_act():
    from src.db.models import Goal

    goal = Goal(id="legacy", title="Legacy", success_criterion_json="not-json")
    decision = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(capability_id="workflow.goal-snapshot-to-file", evidence_refs=["obs-1"]),
    )
    assert decision.action is GoalCandidateAction.clarify
    assert decision.reason == "missing_success_criterion"


def test_repository_and_strategist_seams_share_typed_criterion():
    from src.agent.strategist import build_goal_conditioned_candidate
    from src.db.models import Goal

    criterion = GoalSuccessCriterion(
        criterion_id="file-present",
        description="A readable file is present",
        verifier_kind="artifact_readback",
    )
    goal = Goal(id="goal-1", title="Write a brief")
    goal.success_criterion_json = serialize_success_criterion(criterion)

    assert deserialize_success_criterion(goal) == criterion
    decision = build_goal_conditioned_candidate(
        goal,
        GoalCandidateRequest(
            capability_id="workflow.goal-snapshot-to-file",
            evidence_refs=["observation-1"],
        ),
    )
    assert decision.action is GoalCandidateAction.act
    assert decision.goal_revision == 1


def test_goals_api_registers_inspection_and_candidate_routes(app):
    paths = {route.path for route in app.routes}
    assert "/api/goals/{goal_id}/loop" in paths
    assert "/api/goals/{goal_id}/candidates" in paths


async def test_missing_criterion_clarifies_and_records_no_learning(async_db):
    goal = await goal_repository.create("Write a brief")

    decision = await propose_goal_candidate(
        goal.id,
        GoalCandidateRequest(capability_id="workflow.goal-snapshot-to-file"),
    )

    assert decision.action is GoalCandidateAction.clarify
    assert decision.reason == "missing_success_criterion"
    receipts = await list_goal_loop_receipts(goal.id)
    assert {item["event_type"] for item in receipts} == {
        "goal_loop_candidate",
        "goal_loop_no_learning",
    }


async def test_duplicate_evidence_produces_one_candidate_receipt(async_db):
    goal = await goal_repository.create(
        "Write a brief",
        success_criterion=GoalSuccessCriterion(
            description="A readable file is present",
            verifier_kind="artifact_readback",
        ),
    )
    request = GoalCandidateRequest(
        capability_id="workflow.goal-snapshot-to-file",
        evidence_refs=["obs-1", "obs-1"],
    )

    first = await propose_goal_candidate(goal.id, request)
    second = await propose_goal_candidate(
        goal.id,
        request.model_copy(update={"evidence_refs": ["obs-1"]}),
    )

    assert first.candidate_id == second.candidate_id
    receipts = await list_goal_loop_receipts(goal.id)
    assert sum(item["event_type"] == "goal_loop_candidate" for item in receipts) == 1


async def test_stale_revision_and_pause_block_adapter(async_db):
    goal = await goal_repository.create(
        "Write a brief",
        success_criterion=GoalSuccessCriterion(
            description="A readable file is present",
            verifier_kind="artifact_readback",
        ),
    )
    candidate = await propose_goal_candidate(
        goal.id,
        GoalCandidateRequest(capability_id="workflow.goal-snapshot-to-file", evidence_refs=["obs-1"]),
    )
    await goal_repository.update(goal.id, title="Revised", expected_revision=1)

    calls = 0

    async def adapter(**_kwargs):
        nonlocal calls
        calls += 1
        return GoalExecutionResult(
            verification="passed",
            artifact_ref="reports/brief.md",
            evidence_refs=["readback-1"],
        )

    stale = await dispatch_goal_candidate(candidate, adapter=adapter)
    assert stale.execution_status == "blocked"
    assert stale.reason == "stale_goal_revision"
    assert calls == 0

    paused_goal = await goal_repository.create(
        "Another brief",
        success_criterion=GoalSuccessCriterion(
            description="A readable file is present",
            verifier_kind="artifact_readback",
        ),
    )
    paused_candidate = await propose_goal_candidate(
        paused_goal.id,
        GoalCandidateRequest(capability_id="workflow.goal-snapshot-to-file", evidence_refs=["obs-2"]),
    )
    await goal_repository.update(paused_goal.id, status="paused", expected_revision=1)
    paused = await dispatch_goal_candidate(paused_candidate, adapter=adapter)
    assert paused.execution_status == "blocked"
    assert paused.reason == "goal_not_active"
    assert calls == 0


async def test_success_requires_readback_and_receipt_redacts_inputs(async_db):
    goal = await goal_repository.create(
        "Write a brief",
        success_criterion=GoalSuccessCriterion(
            description="A readable file is present",
            verifier_kind="artifact_readback",
        ),
    )
    candidate = await propose_goal_candidate(
        goal.id,
        GoalCandidateRequest(
            capability_id="workflow.goal-snapshot-to-file",
            evidence_refs=["obs-1"],
            inputs={"api_key": "do-not-store", "file_path": "reports/brief.md"},
        ),
    )

    async def adapter(**_kwargs):
        # A claimed pass without artifact readback must stay unknown.
        return {"verification": "passed", "evidence_refs": ["readback-1"]}

    outcome = await dispatch_goal_candidate(candidate, adapter=adapter)
    assert outcome.verification == "unknown"
    assert outcome.learning == "no_learning"
    receipts = await list_goal_loop_receipts(goal.id)
    candidate_receipt = next(item for item in receipts if item["event_type"] == "goal_loop_candidate")
    assert candidate_receipt["input_keys"] == ["api_key", "file_path"]
    assert "do-not-store" not in str(candidate_receipt)


async def test_unresolved_web_brief_correction_blocks_adapter_and_records_no_learning():
    from unittest.mock import AsyncMock, patch

    from src.db.models import Goal

    delta_id = "delta-unresolved"
    goal = Goal(
        id="goal-correction-gate",
        title="Research a source",
        revision=2,
        success_criterion_json=GoalSuccessCriterion(
            description="A readable source brief is present",
            verifier_kind="artifact_readback",
            evidence_refs=["operator:source-consent"],
            target={
                "query": "corrected source",
                "file_path": "briefs/corrected.md",
                "priority": 80,
                "strategy_delta_id": delta_id,
            },
        ).model_dump_json(),
    )
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id="workflow.web-brief-to-file",
            inputs={
                "query": "corrected source",
                "file_path": "briefs/corrected.md",
                "priority": 80,
            },
            evidence_refs=[f"strategy-delta:{delta_id}"],
        ),
    )
    persisted: list[dict[str, object]] = []
    adapter_calls = 0

    async def adapter(**_kwargs):
        nonlocal adapter_calls
        adapter_calls += 1
        return GoalExecutionResult(verification="passed", artifact_ref="should-not-exist")

    async def persist(**kwargs):
        persisted.append(kwargs)
        return kwargs["details"]

    import src.guardian.goal_conditioned_loop as goal_loop

    with (
        patch.object(goal_loop.goal_repository, "get", new=AsyncMock(return_value=goal)),
        patch.object(goal_loop, "_existing_receipt", new=AsyncMock(return_value=None)),
        patch.object(goal_loop, "get_strategy_delta", new=AsyncMock(return_value=None)),
        patch.object(goal_loop, "_persist_receipt", new=persist),
    ):
        outcome = await dispatch_goal_candidate(candidate, adapter=adapter)

    assert outcome.execution_status == "blocked"
    assert outcome.verification == "unknown"
    assert outcome.learning == "no_learning"
    assert outcome.strategy_delta_id is None
    assert outcome.strategy_delta_provenance == "unresolved"
    assert adapter_calls == 0
    no_learning = next(
        item["details"] for item in persisted if item["event_type"] == "goal_loop_no_learning"
    )
    assert no_learning["reason"] == "strategy_delta_unresolved"
    assert no_learning["strategy_delta_provenance"] == "unresolved"
    assert no_learning["canonical_decision_record"]["status"] == "blocked"
    assert no_learning["canonical_decision_record"]["learning"] == "no_learning"
    assert no_learning["canonical_decision_record"]["goal_revision"] == candidate.goal_revision
    assert no_learning["canonical_decision_record"]["plan_revision"] is None
    assert no_learning["canonical_decision_record"]["memory_state"] == "unknown"
    assert no_learning["canonical_decision_record"]["recovery_state"] == "restart_unverified"


async def test_stale_corrected_outcome_is_not_replayed_before_delta_validation():
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, patch

    from src.db.models import Goal

    delta_id = "delta-stale-replay"
    goal = Goal(
        id="goal-stale-replay",
        title="Research a source",
        revision=2,
        success_criterion_json=GoalSuccessCriterion(
            description="A readable source brief is present",
            verifier_kind="artifact_readback",
            evidence_refs=["operator:source-consent"],
            target={
                "query": "corrected source",
                "file_path": "briefs/corrected.md",
                "priority": 80,
                "strategy_delta_id": delta_id,
            },
        ).model_dump_json(),
    )
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id="workflow.web-brief-to-file",
            inputs={
                "query": "corrected source",
                "file_path": "briefs/corrected.md",
                "priority": 80,
            },
            evidence_refs=[f"strategy-delta:{delta_id}"],
        ),
    )
    stale_outcome = {
        "dedupe_key": candidate.dedupe_key,
        "goal_id": goal.id,
        "goal_revision": candidate.goal_revision,
        "capability_id": candidate.capability_id,
        "input_digest": _safe_digest(candidate.inputs),
        "strategy_delta_id": delta_id,
        "strategy_delta_provenance": "verified",
        "evidence_refs": [f"strategy-delta:{delta_id}"],
    }
    persisted: list[dict[str, object]] = []
    adapter_calls = 0

    async def adapter(**_kwargs):
        nonlocal adapter_calls
        adapter_calls += 1
        return GoalExecutionResult(verification="passed", artifact_ref="should-not-run")

    async def persist(**kwargs):
        persisted.append(kwargs)
        return kwargs["details"]

    import src.guardian.goal_conditioned_loop as goal_loop

    with (
        patch.object(goal_loop.goal_repository, "get", new=AsyncMock(return_value=goal)),
        patch.object(
            goal_loop.audit_repository,
            "list_events",
            new=AsyncMock(
                return_value=[
                    {
                        "event_type": "goal_loop_outcome",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "details": stale_outcome,
                    }
                ]
            ),
        ),
        patch.object(goal_loop, "get_strategy_delta", new=AsyncMock(return_value=None)),
        patch.object(goal_loop, "_persist_receipt", new=persist),
    ):
        outcome = await dispatch_goal_candidate(candidate, adapter=adapter)

    assert outcome.execution_status == "blocked"
    assert outcome.reason == "strategy_delta_unresolved"
    assert outcome.learning == "no_learning"
    assert outcome.strategy_delta_id is None
    assert outcome.strategy_delta_provenance == "unresolved"
    assert adapter_calls == 0
    assert any(item["event_type"] == "goal_loop_no_learning" for item in persisted)


async def test_correction_changes_later_choice_and_persists_provenance():
    from unittest.mock import AsyncMock, patch
    from types import SimpleNamespace

    from src.db.models import Goal
    import src.guardian.goal_conditioned_loop as goal_loop

    old_criterion = GoalSuccessCriterion(
        description="A readable source brief is present",
        verifier_kind="artifact_readback",
        evidence_refs=["operator:source-consent"],
        target={"query": "old source", "file_path": "briefs/old.md"},
    )
    goal = Goal(
        id="goal-correction",
        title="Research a source",
        revision=1,
        success_criterion_json=old_criterion.model_dump_json(),
    )
    old_candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id="workflow.web-brief-to-file",
            inputs={"query": "old source", "file_path": "briefs/old.md"},
        ),
    )

    async def adapter(**_kwargs):
        return GoalExecutionResult(
            execution_status="succeeded",
            verification="passed",
            artifact_ref="artifact-old",
            evidence_refs=["readback:old"],
        )

    corrected_criterion = old_criterion.model_copy(
        update={
            "target": {
                "query": "new source",
                "file_path": "briefs/new.md",
                "priority": 0,
                "strategy_delta_id": "delta-correction-1",
            }
        }
    )
    corrected_goal = Goal(
        id=goal.id,
        title=goal.title,
        revision=2,
        success_criterion_json=corrected_criterion.model_dump_json(),
    )
    new_candidate = build_goal_candidate_decision(
        corrected_goal,
        GoalCandidateRequest(
            capability_id="workflow.web-brief-to-file",
            inputs={"query": "new source", "file_path": "briefs/new.md", "priority": 0},
            evidence_refs=["strategy-delta:delta-correction-1"],
        ),
    )

    persisted: list[dict[str, object]] = []

    async def persist(**kwargs):
        persisted.append(kwargs)
        return kwargs["details"]

    delta = SimpleNamespace(
        delta_id="delta-correction-1",
        goal_id=goal.id,
        scope="goal",
        field_name="web_brief_target",
        author_id="operator:test",
        status="applied",
        goal_revision_before=1,
        goal_revision_after=2,
        after=corrected_criterion.target,
    )

    with (
        patch.object(goal_loop.goal_repository, "get", new=AsyncMock(side_effect=[goal, corrected_goal])),
        patch.object(goal_loop, "_existing_receipt", new=AsyncMock(return_value=None)),
        patch.object(goal_loop, "get_strategy_delta", new=AsyncMock(return_value=delta)),
        patch.object(goal_loop, "_persist_receipt", new=persist),
    ):
        old_outcome = await dispatch_goal_candidate(old_candidate, adapter=adapter)
        new_outcome = await dispatch_goal_candidate(candidate=new_candidate, adapter=adapter)

    assert old_outcome.strategy_delta_id is None

    assert new_candidate.candidate_id != old_candidate.candidate_id
    assert new_outcome.decision_input_digest != old_outcome.decision_input_digest
    assert new_outcome.strategy_delta_id == "delta-correction-1"
    later = next(
        item["details"]
        for item in persisted
        if item["event_type"] == "goal_loop_outcome"
        and item["details"]["candidate_id"] == new_candidate.candidate_id
    )
    assert later["strategy_delta_id"] == "delta-correction-1"
    assert later["strategy_delta_provenance"] == "verified"
    assert later["decision_input_digest"] == new_outcome.decision_input_digest
    assert later["canonical_decision_record"]["status"] == "blocked"
    assert later["canonical_decision_record"]["learning"] == "no_learning"
    assert later["canonical_decision_record"]["goal_revision"] == 2
    assert later["canonical_decision_record"]["plan_revision"] is None
    assert later["canonical_decision_record"]["memory_delta_id"] == "delta-correction-1"
    assert later["canonical_decision_record"]["memory_control_owner"] is None
    assert later["canonical_decision_record"]["memory_state"] == "unknown"
    assert later["canonical_decision_record"]["recovery_state"] == "restart_unverified"


async def test_strategy_delta_provenance_requires_applied_goal_target_linkage():
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, patch

    from src.db.models import Goal
    from src.memory.control import StrategyDeltaReceipt

    delta_id = "delta-correction-1"
    target = {
        "query": "new source",
        "file_path": "briefs/new.md",
        "priority": 0,
        "strategy_delta_id": delta_id,
    }
    goal = Goal(
        id="goal-correction",
        title="Research a source",
        revision=2,
        success_criterion_json=GoalSuccessCriterion(
            description="A readable source brief is present",
            verifier_kind="artifact_readback",
            target=target,
        ).model_dump_json(),
    )
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id="workflow.web-brief-to-file",
            inputs={"query": "new source", "file_path": "briefs/new.md"},
            evidence_refs=[f"strategy-delta:{delta_id}"],
        ),
    )

    def delta(**overrides):
        values = {
            "delta_id": delta_id,
            "goal_id": goal.id,
            "scope": "goal",
            "field_name": "web_brief_target",
            "before": {
                "query": "old source",
                "file_path": "briefs/old.md",
                "priority": 0,
            },
            "after": target,
            "source_event_id": "corr-1",
            "author_id": "operator:test",
            "evaluator_id": None,
            "goal_revision_before": 1,
            "goal_revision_after": 2,
            "status": "applied",
            "rollback_target_id": None,
            "reason": "source correction",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        values.update(overrides)
        return StrategyDeltaReceipt(**values)

    async def resolve(value, candidate_value=candidate):
        with patch(
            "src.guardian.goal_conditioned_loop.get_strategy_delta",
            new=AsyncMock(return_value=value),
        ):
            return await _resolve_strategy_delta_provenance(candidate=candidate_value, goal=goal)

    assert await resolve(None) == (None, "unresolved")
    assert await resolve(delta(goal_id="other-goal")) == (None, "unresolved")
    assert await resolve(delta(goal_revision_after=3)) == (None, "unresolved")
    assert await resolve(delta(author_id="")) == (None, "unresolved")
    valid_candidate = candidate.model_copy(
        update={
            "inputs": {
                "query": "new source",
                "file_path": "briefs/new.md",
                "priority": 0,
            }
        }
    )
    assert await resolve(delta(), valid_candidate) == (delta_id, "verified")

    # A correction that only changes scheduler priority must be linked into
    # the candidate input digest; omitting it cannot produce verified
    # provenance.
    assert await resolve(delta(), candidate) == (None, "unresolved")
    wrong_priority = valid_candidate.model_copy(
        update={"inputs": {**valid_candidate.inputs, "priority": 1}}
    )
    assert await resolve(delta(), wrong_priority) == (None, "unresolved")

    wrong_inputs = candidate.model_copy(update={"inputs": {"query": "other source"}})
    assert await resolve(delta(), wrong_inputs) == (
        None,
        "unresolved",
    )

    ambiguous = candidate.model_copy(
        update={"evidence_refs": [f"strategy-delta:{delta_id}", "strategy-delta:other"]}
    )
    assert await resolve(delta(), ambiguous) == (
        None,
        "unresolved",
    )


def test_result_contract_exposes_verified_correction_receipt_fields():
    from src.guardian.goal_snapshot_to_file import GoalSnapshotToFileResult

    result = GoalSnapshotToFileResult(
        goal_id="goal-correction",
        goal_revision=2,
        file_path="briefs/new.md",
        execution_status="succeeded",
        verification="passed",
        decision_input_digest="a" * 64,
        strategy_delta_id="delta-correction-1",
        strategy_delta_provenance="verified",
    )
    assert result.model_dump()["strategy_delta_provenance"] == "verified"


def test_legacy_receipt_ids_are_sanitized_without_verified_provenance():
    assert _sanitize_strategy_delta_receipt({"strategy_delta_id": "fabricated"}) == {
        "strategy_delta_id": None,
        "strategy_delta_provenance": "unresolved",
    }
    assert _sanitize_strategy_delta_receipt(
        {"strategy_delta_id": "delta-1", "strategy_delta_provenance": "verified"}
    )["strategy_delta_id"] == "delta-1"


def test_unresolved_outer_receipt_downgrades_nested_canonical_record():
    from src.memory.gate_b_provider_decision import build_gate_b_canonical_decision_record

    nested = build_gate_b_canonical_decision_record(
        goal_id="goal-readback",
        goal_revision=2,
        plan_revision=2,
        decision_input_digest="a" * 64,
        memory_delta_id="delta-readback",
        memory_delta_provenance="verified",
        memory_control_owner="operator:test",
        memory_state="available",
        recovery_state="steady",
        decision="act",
        verification="passed",
        usefulness="helpful",
        learning_writeback_id="writeback-readback",
        requested_learning="applied",
    )

    safe = _sanitize_strategy_delta_receipt(
        {
            "strategy_delta_id": "delta-readback",
            "strategy_delta_provenance": "unresolved",
            "canonical_decision_record": nested.as_payload(),
        }
    )

    record = safe["canonical_decision_record"]
    assert safe["strategy_delta_id"] is None
    assert safe["strategy_delta_provenance"] == "unresolved"
    assert record["status"] == "blocked"
    assert record["learning"] == "no_learning"
    assert record["memory_delta_id"] is None
    assert record["memory_control_owner"] is None
    assert record["memory_state"] == "unknown"
    assert record["recovery_state"] == "restart_unverified"


async def test_list_readback_downgrades_nested_canonical_record_with_outer_provenance():
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, patch

    from src.memory.gate_b_provider_decision import build_gate_b_canonical_decision_record

    nested = build_gate_b_canonical_decision_record(
        goal_id="goal-list-readback",
        goal_revision=1,
        plan_revision=1,
        decision_input_digest="b" * 64,
        memory_delta_id="delta-list-readback",
        memory_delta_provenance="verified",
        memory_control_owner="operator:test",
        memory_state="available",
        recovery_state="steady",
        decision="act",
        verification="passed",
        usefulness="helpful",
        learning_writeback_id="writeback-list",
        requested_learning="applied",
    )
    event = {
        "id": "audit-list-readback",
        "event_type": "goal_loop_outcome",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "details": {
            "goal_id": "goal-list-readback",
            "goal_revision": 1,
            "decision_input_digest": "b" * 64,
            "strategy_delta_id": "delta-list-readback",
            "strategy_delta_provenance": "verified",
            "learning": "applied",
            "learning_record_id": "writeback-list",
            "canonical_decision_record": nested.as_payload(),
        },
    }

    with (
        patch(
            "src.guardian.goal_conditioned_loop.audit_repository.list_events",
            new=AsyncMock(return_value=[event]),
        ),
        patch(
            "src.guardian.goal_conditioned_loop.goal_repository.get",
            new=AsyncMock(return_value=None),
        ),
    ):
        listed = await list_goal_loop_receipts("goal-list-readback")

    record = listed[0]["canonical_decision_record"]
    assert listed[0]["strategy_delta_id"] is None
    assert listed[0]["strategy_delta_provenance"] == "unresolved"
    assert listed[0]["learning"] == "no_learning"
    assert listed[0]["learning_record_id"] is None
    assert record["status"] == "blocked"
    assert record["learning"] == "no_learning"
    assert record["memory_delta_id"] is None
    assert record["memory_state"] == "unknown"
    assert record["recovery_state"] == "restart_unverified"


def test_mismatched_nested_canonical_record_clears_outer_learning_claim():
    from src.memory.gate_b_provider_decision import build_gate_b_canonical_decision_record

    nested = build_gate_b_canonical_decision_record(
        goal_id="goal-cross-check",
        goal_revision=1,
        plan_revision=1,
        decision_input_digest="c" * 64,
        memory_delta_id="delta-other",
        memory_delta_provenance="verified",
        memory_control_owner="operator:test",
        memory_state="available",
        recovery_state="steady",
        decision="act",
        verification="passed",
        usefulness="helpful",
        learning_writeback_id="writeback-1",
        requested_learning="applied",
    )

    safe = _sanitize_strategy_delta_receipt(
        {
            "goal_id": "goal-cross-check",
            "goal_revision": 1,
            "decision_input_digest": "c" * 64,
            "strategy_delta_id": "delta-outer",
            "strategy_delta_provenance": "verified",
            "learning": "applied",
            "learning_record_id": "writeback-1",
            "canonical_decision_record": nested.as_payload(),
        }
    )

    assert safe["strategy_delta_id"] is None
    assert safe["strategy_delta_provenance"] == "unresolved"
    assert safe["learning"] == "no_learning"
    assert safe["learning_record_id"] is None
    assert safe["canonical_decision_record"]["status"] == "blocked"
    assert safe["canonical_decision_record"]["learning"] == "no_learning"
    assert safe["canonical_decision_record"]["memory_delta_id"] is None


async def test_outcome_learning_is_coerced_without_complete_canonical_binding():
    from unittest.mock import AsyncMock, patch

    from src.db.models import Goal

    goal = Goal(
        id="goal-learning-coercion",
        title="Write a brief",
        revision=1,
        success_criterion_json=GoalSuccessCriterion(
            description="A readable brief is present",
            verifier_kind="artifact_readback",
            evidence_refs=["operator:readback"],
        ).model_dump_json(),
    )
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id="workflow.goal-snapshot-to-file",
            evidence_refs=["operator:readback"],
        ),
    )
    persisted: list[dict[str, object]] = []

    async def adapter(**_kwargs):
        return GoalExecutionResult(
            execution_status="succeeded",
            verification="passed",
            usefulness="helpful",
            learning="applied",
            learning_record_id="writeback-1",
            artifact_ref="reports/brief.md",
            evidence_refs=["readback:brief"],
        )

    async def persist(**kwargs):
        persisted.append(kwargs)
        return kwargs["details"]

    import src.guardian.goal_conditioned_loop as goal_loop

    with (
        patch.object(goal_loop.goal_repository, "get", new=AsyncMock(return_value=goal)),
        patch.object(goal_loop, "_existing_receipt", new=AsyncMock(return_value=None)),
        patch.object(goal_loop, "_persist_receipt", new=persist),
    ):
        outcome = await dispatch_goal_candidate(candidate, adapter=adapter)

    assert outcome.learning == "no_learning"
    assert outcome.learning_record_id is None
    saved = next(item["details"] for item in persisted if item["event_type"] == "goal_loop_outcome")
    assert saved["learning"] == "no_learning"
    assert saved["learning_record_id"] is None
    assert saved["canonical_decision_record"]["status"] == "blocked"
    assert saved["canonical_decision_record"]["learning"] == "no_learning"


def test_receipt_evidence_refs_are_typed_opaque_digests():
    details = _redact_receipt_details(
        {
            "evidence_refs": [
                "https://private.example/search?q=secret-query",
                "operator:secret correction prose",
                "strategy-delta:delta-safe-1",
                "strategy-delta:https://private.example/correction",
            ]
        }
    )

    refs = details["evidence_refs"]
    assert "private.example" not in str(refs)
    assert "secret-query" not in str(refs)
    assert "secret correction prose" not in str(refs)
    assert "strategy-delta:delta-safe-1" in refs
    assert any(ref.startswith("evidence:") for ref in refs)
    assert any(ref.startswith("strategy-delta:invalid:") for ref in refs)


async def test_legacy_verified_receipt_is_downgraded_without_revalidation():
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, patch

    from src.db.models import Goal

    delta_id = "delta-legacy-1"
    goal = Goal(
        id="goal-legacy",
        title="Legacy receipt",
        revision=2,
        success_criterion_json=GoalSuccessCriterion(
            description="A readable brief is present",
            verifier_kind="artifact_readback",
            target={
                "query": "safe query",
                "file_path": "briefs/safe.md",
                "priority": 0,
                "strategy_delta_id": delta_id,
            },
        ).model_dump_json(),
    )
    candidate = build_goal_candidate_decision(
        goal,
        GoalCandidateRequest(
            capability_id="workflow.web-brief-to-file",
            inputs={
                "query": "safe query",
                "file_path": "briefs/safe.md",
                "priority": 0,
            },
            evidence_refs=[f"strategy-delta:{delta_id}"],
        ),
    )
    details = {
        "dedupe_key": candidate.dedupe_key,
        "goal_id": candidate.goal_id,
        "goal_revision": candidate.goal_revision,
        "capability_id": candidate.capability_id,
        "input_digest": _safe_digest(candidate.inputs),
        "strategy_delta_id": delta_id,
        "strategy_delta_provenance": "verified",
        "evidence_refs": [f"strategy-delta:{delta_id}"],
    }
    event = {
        "id": "audit-legacy",
        "event_type": "goal_loop_outcome",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "details": details,
    }

    with (
        patch(
            "src.guardian.goal_conditioned_loop.audit_repository.list_events",
            new=AsyncMock(return_value=[event]),
        ),
        patch(
            "src.guardian.goal_conditioned_loop.get_strategy_delta",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.guardian.goal_conditioned_loop.goal_repository.get",
            new=AsyncMock(return_value=goal),
        ),
    ):
        replay = await _existing_receipt(
            event_type="goal_loop_outcome",
            dedupe_key=candidate.dedupe_key,
            candidate=candidate,
            goal=goal,
        )
        listed = await list_goal_loop_receipts(goal.id)

    assert replay is not None
    assert replay["strategy_delta_id"] is None
    assert replay["strategy_delta_provenance"] == "unresolved"
    assert listed[0]["strategy_delta_id"] is None
    assert listed[0]["strategy_delta_provenance"] == "unresolved"
