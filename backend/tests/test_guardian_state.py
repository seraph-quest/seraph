"""Tests for explicit guardian-state synthesis."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.factory import create_agent
from src.agent.session import SessionManager
from src.agent.strategist import create_strategist_agent
from src.db.models import MemoryKind
from src.guardian.feedback import GuardianLearningSignal, guardian_feedback_repository
from src.guardian.learning_evidence import (
    GuardianLearningAxisEvidence,
    learning_field_for_axis,
    neutral_axis_evidence,
    ordered_learning_axes,
)
from src.guardian.state import GuardianState, GuardianStateConfidence, build_guardian_state
from src.guardian.world_model import GuardianWorldModel, build_guardian_world_model
from src.memory.procedural import sync_learning_signal_memories
from src.memory.procedural_guidance import ProceduralMemoryGuidance
from src.memory.retrieval_planner import MemoryRetrievalPlanResult
from src.memory.repository import memory_repository
from src.observer.context import CurrentContext


def _make_guardian_state() -> GuardianState:
    return GuardianState(
        soul_context="# Soul\n\n## Identity\nBuilder",
        observer_context=CurrentContext(
            time_of_day="morning",
            day_of_week="Monday",
            is_working_hours=True,
            active_goals_summary="Ship guardian state",
            active_window="VS Code",
            screen_context="Editing roadmap",
            data_quality="good",
            observer_confidence="grounded",
            salience_level="high",
            salience_reason="active_goals",
            interruption_cost="low",
        ),
        world_model=GuardianWorldModel(
            current_focus="Ship guardian state while in VS Code",
            focus_source="observer_goal_window",
            active_commitments=("Ship guardian state",),
            open_loops_or_pressure=('Prior roadmap: assistant said "Land guardian-state synthesis next"',),
            focus_alignment="medium",
            intervention_receptivity="high",
            active_blockers=("Need workflow write access",),
            next_up=("Close the roadmap review",),
            dominant_thread="Ship guardian state",
            active_projects=("Guardian cockpit",),
            execution_pressure=("Workflow brief-sync degraded at write_file",),
            memory_signals=("Ship guardian state", "Prefers dense dashboards"),
            continuity_threads=('Prior roadmap: assistant said "Land guardian-state synthesis next"',),
            collaborators=("Alice",),
            recurring_obligations=("Weekly roadmap review",),
            project_timeline=("Guardian cockpit", "Workflow brief-sync degraded at write_file"),
        ),
        bounded_memory_context="- Identity: Builder\n- Open todos: Ship guardian state",
        memory_context="- [goal] Ship guardian state\n- [pattern] Prefers dense dashboards",
        episodic_memory_context="- [episode] Workflow brief-sync failed at write_file",
        current_session_history="User: What should Seraph improve next?\nAssistant: Build explicit guardian state.",
        recent_sessions_summary='- Prior roadmap: assistant said "Land guardian-state synthesis next"',
        recent_intervention_feedback="- advisory delivered, feedback=helpful, reason=available_capacity: Stretch and refocus.",
        recent_execution_summary="- Workflow brief-sync degraded at write_file",
        learning_diagnostics=(
            "Live learning is currently anchored to project scope for 'Guardian cockpit'.",
            "Observed outcomes: helpful=2, acknowledged=1, not_helpful=0, failed=0.",
        ),
        confidence=GuardianStateConfidence(
            overall="grounded",
            observer="grounded",
            world_model="grounded",
            memory="grounded",
            current_session="grounded",
            recent_sessions="grounded",
        ),
    )


def _axis_evidence_tuple(
    axis: str,
    *,
    source: str,
    bias: str,
    support_count: int,
    recency_score: float,
    confidence_score: float,
    quality_score: float,
    metadata_complete: bool = True,
) -> tuple[GuardianLearningAxisEvidence, ...]:
    evidence_by_axis = {
        axis: GuardianLearningAxisEvidence(
            axis=axis,
            field_name=learning_field_for_axis(axis),
            source=source,
            bias=bias,
            support_count=support_count,
            recency_score=recency_score,
            confidence_score=confidence_score,
            quality_score=quality_score,
            metadata_complete=metadata_complete,
        )
    }
    return tuple(
        evidence_by_axis.get(item_axis, neutral_axis_evidence(item_axis, source=source))
        for item_axis in ordered_learning_axes()
    )


@pytest.mark.asyncio
async def test_build_guardian_state_collects_memory_and_recent_sessions(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What should Seraph improve next?")
    await sm.add_message("current", "assistant", "Build explicit guardian state.")
    await sm.replace_todos(
        "current",
        [
            {"content": "Clarify missing requirement", "completed": False},
            {"content": "Review prior thread", "completed": True},
        ],
    )
    await sm.get_or_create("prior")
    await sm.update_title("prior", "Prior roadmap")
    await sm.add_message("prior", "assistant", "Land guardian-state synthesis next.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Ship guardian state",
        active_window="VS Code",
        screen_context="Editing roadmap",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch(
            "src.memory.hybrid_retrieval.search_with_status",
            return_value=([{"category": "goal", "text": "Ship guardian state"}], False),
        ),
        patch(
            "src.audit.repository.audit_repository.list_events",
            return_value=[
                {
                    "event_type": "tool_result",
                    "tool_name": "workflow_brief_sync",
                    "details": {
                        "workflow_name": "brief-sync",
                        "continued_error_steps": ["write_file"],
                    },
                }
            ],
        ),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Guardian cockpit", "Docs parity"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="- advisory delivered, feedback=helpful: Stretch and refocus.",
        ),
    ):
        state = await build_guardian_state(session_id="current", user_message="What should Seraph improve next?")

    assert "## Identity\nBuilder" in state.soul_context
    assert state.active_goals_summary == "Ship guardian state"
    assert state.confidence.overall == "grounded"
    assert state.confidence.observer == "grounded"
    assert state.confidence.world_model == "grounded"
    assert state.confidence.memory == "grounded"
    assert state.confidence.current_session == "grounded"
    assert state.confidence.recent_sessions == "grounded"
    assert state.world_model.current_focus == "Ship guardian state while in VS Code"
    assert state.world_model.focus_source == "observer_goal_window"
    assert "Ship guardian state" in state.world_model.active_commitments
    assert "Guardian cockpit" in state.world_model.active_projects
    assert "Ship guardian state" in state.world_model.memory_signals
    assert state.world_model.active_blockers
    assert state.world_model.next_up
    assert state.world_model.dominant_thread
    assert "Prior roadmap" in state.world_model.continuity_threads[0]
    assert "Guardian cockpit" in state.world_model.project_timeline[0]
    assert any(
        "brief-sync degraded" in item
        for item in state.world_model.execution_pressure
    )
    assert state.world_model.focus_alignment == "medium"
    assert state.world_model.intervention_receptivity == "high"
    assert "Build explicit guardian state." in state.current_session_history
    assert "Prior roadmap" in state.recent_sessions_summary
    assert "Clarify missing requirement" in state.bounded_memory_context
    assert "Review prior thread" in state.bounded_memory_context
    assert "Ship guardian state" in state.memory_context
    assert "feedback=helpful" in state.recent_intervention_feedback
    assert "brief-sync degraded" in state.recent_execution_summary


@pytest.mark.asyncio
async def test_build_guardian_state_marks_history_inferred_focus_as_partial(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "Where should attention go next?")
    await sm.add_message("current", "assistant", "Finish the Atlas launch checklist first.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        data_quality="good",
        observer_confidence="grounded",
        salience_level="low",
        salience_reason="background",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=[],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="Where should attention go next?",
        )

    assert state.world_model.current_focus == "Finish the Atlas launch checklist first."
    assert state.world_model.focus_source == "current_session"
    assert (
        "Current focus is inferred from current session instead of live observer signals."
        in state.world_model.judgment_risks
    )
    assert state.confidence.world_model == "partial"
    assert state.confidence.overall == "partial"


@pytest.mark.asyncio
async def test_build_guardian_state_surfaces_memory_provider_diagnostics(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What matters for Atlas today?")
    await sm.add_message("current", "assistant", "Let me ground that in provider-backed memory.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_window="VS Code",
        screen_context="Editing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch(
            "src.memory.retrieval_planner.plan_memory_retrieval",
            AsyncMock(
                return_value=MemoryRetrievalPlanResult(
                    semantic_context="- [project] Atlas launch remains the live project anchor.",
                    episodic_context="",
                    memory_buckets={"project": ("Atlas launch remains the live project anchor.",)},
                    degraded=False,
                    lane="structured_plus_provider_model",
                    provider_diagnostics=(
                        {
                            "name": "graph-memory",
                            "canonical_authority": "guardian",
                            "provenance": "external_advisory",
                            "capabilities_used": ["user_model"],
                            "quality_state": "guarded",
                            "hit_count": 1,
                            "stale_hit_count": 0,
                            "suppressed_irrelevant_hit_count": 1,
                            "topic_matches": ["Atlas launch"],
                        },
                    ),
                )
            ),
        ),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas launch"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
    ):
        state = await build_guardian_state(session_id="current", user_message="What matters for Atlas today?")

    assert state.memory_provider_diagnostics == (
        "graph-memory quality=guarded, capabilities=user_model, hits=1, authority=guardian, "
        "provenance=external_advisory, stale_suppressed=0, "
        "irrelevant_suppressed=1, topic_matches=Atlas launch",
    )
    assert "Memory provider diagnostics:" in state.to_prompt_block()


@pytest.mark.asyncio
async def test_build_guardian_state_degrades_world_model_on_project_mismatch(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What matters for Atlas today?")
    await sm.add_message("current", "assistant", "Let me reconcile the project signals.")

    await memory_repository.create_memory(
        content="Hermes migration remains the live delivery project.",
        kind=MemoryKind.project,
        summary="Hermes migration",
        importance=0.92,
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=[],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="What matters for Atlas today?",
        )

    assert any("Atlas" in item for item in state.world_model.active_projects)
    assert "Hermes migration" in state.world_model.active_projects
    assert (
        "Live observer project 'Atlas' does not match recalled project context."
        in state.world_model.judgment_risks
    )
    assert state.confidence.world_model == "degraded"
    assert state.confidence.overall == "partial"


@pytest.mark.asyncio
async def test_build_guardian_state_degrades_on_stale_supporting_context(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What matters for Atlas today?")
    await sm.add_message("current", "assistant", "Let me reconcile the longer-horizon context.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    retrieval = MemoryRetrievalPlanResult(
        semantic_context="\n".join(
            [
                "- [project] Atlas launch",
                "- [collaborator] Bob owns Hermes migration communications",
                "- [obligation] Weekly Hermes rollout note goes out on Friday",
                "- [timeline] Hermes migration timeline ends on Friday",
            ]
        ),
        episodic_context="",
        memory_buckets={
            "project": ("Atlas launch",),
            "collaborator": ("Bob owns Hermes migration communications",),
            "obligation": ("Weekly Hermes rollout note goes out on Friday",),
            "timeline": ("Hermes migration timeline ends on Friday",),
        },
        degraded=False,
        lane="hybrid",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch(
            "src.memory.retrieval_planner.plan_memory_retrieval",
            AsyncMock(return_value=retrieval),
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.resolve_learning_signal",
            AsyncMock(
                return_value=MagicMock(
                    effective_signal=GuardianLearningSignal.neutral("advisory"),
                    dominant_scope="global",
                )
            ),
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent_for_scope",
            AsyncMock(return_value=""),
        ),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.memory.procedural_guidance.load_procedural_memory_guidance",
            AsyncMock(return_value=ProceduralMemoryGuidance(intervention_type="advisory")),
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="What matters for Atlas today?",
        )

    assert any("Atlas" in item for item in state.world_model.active_projects)
    assert (
        "Recalled collaborator, obligation, or timeline context does not support live project 'Atlas'."
        in state.world_model.judgment_risks
    )
    assert state.confidence.world_model == "degraded"
    assert state.confidence.overall == "partial"


@pytest.mark.asyncio
async def test_build_guardian_state_degrades_on_stale_execution_pressure(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What matters for Atlas today?")
    await sm.add_message("current", "assistant", "Let me reconcile the longer-horizon context.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
        user_state="available",
    )

    retrieval = MemoryRetrievalPlanResult(
        semantic_context="- [project] Atlas launch",
        episodic_context="",
        memory_buckets={"project": ("Atlas launch",)},
        degraded=False,
        lane="hybrid",
    )
    negative_signal = GuardianLearningSignal(
        intervention_type="advisory",
        helpful_count=0,
        not_helpful_count=2,
        acknowledged_count=0,
        failed_count=1,
        bias="reduce_interruptions",
        phrasing_bias="neutral",
        cadence_bias="neutral",
        channel_bias="neutral",
        escalation_bias="neutral",
        timing_bias="neutral",
        blocked_state_bias="neutral",
        suppression_bias="extend_suppression",
        thread_preference_bias="neutral",
        blocked_direct_failure_count=0,
        blocked_native_success_count=0,
        available_direct_success_count=0,
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch(
            "src.memory.retrieval_planner.plan_memory_retrieval",
            AsyncMock(return_value=retrieval),
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.resolve_learning_signal",
            AsyncMock(
                return_value=MagicMock(
                    effective_signal=negative_signal,
                    dominant_scope="project",
                )
            ),
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent_for_scope",
            AsyncMock(return_value=""),
        ),
        patch(
            "src.audit.repository.audit_repository.list_events",
            return_value=[
                {
                    "event_type": "tool_result",
                    "tool_name": "workflow_hermes_migration",
                    "details": {
                        "workflow_name": "Hermes migration",
                        "continued_error_steps": ["notify_release"],
                    },
                }
            ],
        ),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.memory.procedural_guidance.load_procedural_memory_guidance",
            AsyncMock(return_value=ProceduralMemoryGuidance(intervention_type="advisory")),
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="What matters for Atlas today?",
        )

    assert (
        "Recent execution pressure does not line up with live project 'Atlas'."
        in state.world_model.judgment_risks
    )
    assert (
        "Recent execution setbacks and intervention misses suggest follow-through risk."
        in state.world_model.judgment_risks
    )
    assert "Workflow Hermes migration degraded at notify_release" in state.world_model.active_blockers
    assert state.world_model.intervention_receptivity == "low"
    assert state.confidence.world_model == "degraded"
    assert state.confidence.overall == "partial"


@pytest.mark.asyncio
async def test_build_guardian_state_surfaces_memory_reconciliation_diagnostics(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What changed in memory policy?")
    await sm.add_message("current", "assistant", "Let me inspect the current reconciliation posture.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Review guardian memory posture",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas follow-through notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="low",
        user_state="available",
    )

    retrieval = MemoryRetrievalPlanResult(
        semantic_context="- [project] Atlas launch",
        episodic_context="",
        memory_buckets={"project": ("Atlas launch",)},
        degraded=False,
        lane="hybrid",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch(
            "src.memory.retrieval_planner.plan_memory_retrieval",
            AsyncMock(return_value=retrieval),
        ),
        patch(
            "src.memory.decay.summarize_memory_reconciliation_state",
            AsyncMock(
                return_value={
                    "state": "conflict_and_forgetting_active",
                    "active_count": 6,
                    "superseded_count": 2,
                    "archived_count": 1,
                    "contradiction_edge_count": 2,
                    "recent_conflicts": [
                        {
                            "summary": "Atlas launch delayed",
                            "reason": "contradiction",
                            "superseded_by_memory_id": "atlas-current",
                        }
                    ],
                    "recent_archivals": [
                        {
                            "summary": "Weekly recap preference",
                            "reason": "stale_decay_archive",
                        }
                    ],
                    "policy": {
                        "authoritative_memory": "guardian",
                        "reconciliation_policy": "canonical_first",
                        "forgetting_policy": "selective_decay_then_archive",
                    },
                }
            ),
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.resolve_learning_signal",
            AsyncMock(
                return_value=MagicMock(
                    effective_signal=GuardianLearningSignal.neutral("advisory"),
                    dominant_scope="global",
                    decisions=tuple(),
                )
            ),
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent_for_scope",
            AsyncMock(return_value=""),
        ),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.memory.procedural_guidance.load_procedural_memory_guidance",
            AsyncMock(return_value=ProceduralMemoryGuidance(intervention_type="advisory")),
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="What changed in memory policy?",
        )

    assert any(
        "state=conflict_and_forgetting_active, active=6, superseded=2, archived=1, contradictions=2"
        in item
        for item in state.memory_reconciliation_diagnostics
    )
    assert any(
        "policy=authoritative=guardian, reconciliation=canonical_first, forgetting=selective_decay_then_archive"
        in item
        for item in state.memory_reconciliation_diagnostics
    )
    assert any(
        "recent_conflict=summary=Atlas launch delayed, reason=contradiction, superseded_by=atlas-current"
        in item
        for item in state.memory_reconciliation_diagnostics
    )
    assert any(
        "recent_archival=summary=Weekly recap preference, reason=stale_decay_archive"
        in item
        for item in state.memory_reconciliation_diagnostics
    )
    prompt = state.to_prompt_block()
    assert "Memory reconciliation diagnostics:" in prompt
    assert "state=conflict_and_forgetting_active" in prompt


@pytest.mark.asyncio
async def test_build_guardian_state_prioritizes_live_project_cross_thread_continuity(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What matters for Atlas today?")
    await sm.add_message("current", "assistant", "Let me reconcile the recent Atlas threads.")
    await sm.get_or_create("prior-atlas")
    await sm.update_title("prior-atlas", "Atlas follow-up")
    await sm.add_message("prior-atlas", "assistant", "Close the Atlas launch checklist before tomorrow.")
    await sm.get_or_create("prior-hermes")
    await sm.update_title("prior-hermes", "Hermes migration")
    await sm.add_message("prior-hermes", "assistant", "Prepare the Hermes rollout note.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch(
            "src.memory.retrieval_planner.plan_memory_retrieval",
            AsyncMock(
                return_value=MemoryRetrievalPlanResult(
                    semantic_context="",
                    episodic_context="",
                    memory_buckets={},
                    degraded=False,
                    lane="hybrid",
                )
            ),
        ),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="What matters for Atlas today?",
        )

    assert "Atlas" in state.world_model.continuity_threads[0]
    assert "Atlas" in state.world_model.dominant_thread
    assert "Atlas" in state.world_model.next_up[0]
    assert "Atlas" in state.world_model.project_state[1]


@pytest.mark.asyncio
async def test_build_guardian_state_surfaces_follow_through_risk_from_cross_thread_and_execution(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What matters for Atlas today?")
    await sm.add_message("current", "assistant", "Let me reconcile the recent Atlas threads.")
    await sm.get_or_create("prior-atlas")
    await sm.update_title("prior-atlas", "Atlas follow-up")
    await sm.add_message("prior-atlas", "assistant", "Close the Atlas launch checklist before tomorrow.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch(
            "src.audit.repository.audit_repository.list_events",
            return_value=[
                {
                    "event_type": "tool_result",
                    "tool_name": "workflow_atlas_launch",
                    "details": {
                        "workflow_name": "Atlas launch",
                        "continued_error_steps": ["send_update"],
                    },
                }
            ],
        ),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="What matters for Atlas today?",
        )

    assert (
        "Cross-thread commitments and recent execution setbacks suggest live follow-through risk on project 'Atlas'."
        in state.world_model.judgment_risks
    )
    assert (
        "Follow-through risk remains open for live project 'Atlas'."
        in state.world_model.open_loops_or_pressure
    )
    assert state.confidence.world_model == "partial"
    assert state.confidence.overall == "partial"


@pytest.mark.asyncio
async def test_build_guardian_state_treats_current_event_as_live_project_anchor(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        current_event="Atlas launch review",
        active_window="Calendar",
        screen_context="Reviewing release and migration notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="current_event",
        interruption_cost="medium",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas launch", "Hermes migration"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="Should Seraph interrupt during the launch review?",
        )

    assert state.world_model.focus_source == "current_event"
    assert (
        "Multiple active projects are competing without a live observer project anchor."
        not in state.world_model.judgment_risks
    )
    assert state.confidence.world_model == "grounded"


@pytest.mark.asyncio
async def test_build_guardian_state_lowers_receptivity_after_negative_outcome_trend(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "Should Seraph interrupt right now?")
    await sm.add_message("current", "assistant", "Let me check the recent intervention signal.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Protect current flow",
        active_window="VS Code",
        screen_context="Coding through a long task list",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="low",
        user_state="available",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            AsyncMock(
                return_value=GuardianLearningSignal(
                    intervention_type="advisory",
                    helpful_count=0,
                    not_helpful_count=2,
                    acknowledged_count=0,
                    failed_count=1,
                    bias="reduce_interruptions",
                    phrasing_bias="neutral",
                    cadence_bias="neutral",
                    channel_bias="neutral",
                    escalation_bias="neutral",
                    timing_bias="neutral",
                    blocked_state_bias="neutral",
                    suppression_bias="extend_suppression",
                    thread_preference_bias="neutral",
                    blocked_direct_failure_count=0,
                    blocked_native_success_count=0,
                    available_direct_success_count=0,
                )
            ),
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="Should Seraph interrupt right now?",
        )

    assert state.world_model.intervention_receptivity == "medium"
    assert (
        "Recent intervention outcomes skew negative, so the guardian should stay selective."
        in state.world_model.judgment_risks
    )
    assert state.confidence.world_model == "partial"
    assert state.confidence.overall == "partial"


@pytest.mark.asyncio
async def test_build_guardian_state_prefers_project_scoped_negative_learning(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "Should Atlas work be interrupted right now?")
    await sm.add_message("current", "assistant", "Let me check the Atlas-specific intervention history.")

    for content in ("Global success one.", "Global success two."):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content=content,
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
        )
        await guardian_feedback_repository.update_outcome(
            intervention.id,
            latest_outcome="delivered",
            transport="websocket",
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type="helpful")

    for content in ("Atlas interruption failed once.", "Atlas interruption failed twice."):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id=None,
            message_type="proactive",
            intervention_type="advisory",
            urgency=2,
            content=content,
            reasoning="available_capacity",
            is_scheduled=False,
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            policy_action="act",
            policy_reason="available_capacity",
            delivery_decision="deliver",
            latest_outcome="delivered",
            active_project="Atlas",
        )
        await guardian_feedback_repository.update_outcome(
            intervention.id,
            latest_outcome="delivered",
            transport="websocket",
        )
        await guardian_feedback_repository.record_feedback(intervention.id, feedback_type="not_helpful")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Protect Atlas focus blocks",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Deep in Atlas implementation",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="low",
        user_state="available",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="Should Atlas work be interrupted right now?",
        )

    assert state.world_model.intervention_receptivity == "medium"
    assert (
        "Recent intervention outcomes skew negative, so the guardian should stay selective."
        in state.world_model.judgment_risks
    )
    assert "Recent intervention friction is present" in state.world_model.open_loops_or_pressure
    assert state.confidence.world_model == "partial"
    assert state.confidence.overall == "partial"


@pytest.mark.asyncio
async def test_build_guardian_state_lowers_overall_confidence_when_memory_is_degraded(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What should Seraph improve next?")
    await sm.add_message("current", "assistant", "Build explicit guardian state.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Ship guardian state",
        active_window="VS Code",
        screen_context="Editing roadmap",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], True)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Guardian cockpit"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="- advisory delivered, feedback=helpful: Stretch and refocus.",
        ),
    ):
        state = await build_guardian_state(session_id="current", user_message="What should Seraph improve next?")

    assert state.confidence.memory == "degraded"
    assert state.confidence.overall == "partial"


@pytest.mark.asyncio
async def test_build_guardian_state_uses_structured_memory_kinds(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What should I focus on next?")
    await sm.add_message("current", "assistant", "Let me ground that in recent memory.")

    await memory_repository.create_memory(
        content="Review the Atlas brief tomorrow morning.",
        kind=MemoryKind.commitment,
        summary="Review the Atlas brief tomorrow morning",
        importance=0.95,
    )
    await memory_repository.create_memory(
        content="Atlas investor brief",
        kind=MemoryKind.project,
        summary="Atlas investor brief",
        importance=0.9,
    )
    await memory_repository.create_memory(
        content="Alice owns the investor update thread.",
        kind=MemoryKind.collaborator,
        summary="Alice owns investor updates",
        importance=0.85,
    )
    await memory_repository.create_memory(
        content="Weekly investor note goes out on Friday.",
        kind=MemoryKind.obligation,
        summary="Weekly investor note goes out on Friday",
        importance=0.82,
    )
    await memory_repository.create_memory(
        content="Atlas launch timeline ends on Friday.",
        kind=MemoryKind.timeline,
        summary="Atlas launch timeline ends on Friday",
        importance=0.8,
    )
    await memory_repository.create_memory(
        content="User prefers concise morning briefings.",
        kind=MemoryKind.communication_preference,
        summary="Prefers concise morning briefings",
        importance=0.78,
    )
    await memory_repository.create_memory(
        content="For advisory interventions, avoid direct interruption during deep-work windows.",
        kind=MemoryKind.procedural,
        summary="For advisory interventions, avoid direct interruption during deep-work windows.",
        importance=0.79,
    )
    await memory_repository.create_memory(
        content="For advisory interventions, prefer async native continuation when the user is blocked.",
        kind=MemoryKind.procedural,
        summary="For advisory interventions, prefer async native continuation when the user is blocked.",
        importance=0.78,
    )
    await memory_repository.create_memory(
        content="For advisory interventions, bundle lower-urgency check-ins instead of interrupting immediately.",
        kind=MemoryKind.procedural,
        summary="For advisory interventions, bundle lower-urgency check-ins instead of interrupting immediately.",
        importance=0.77,
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Prepare investor brief",
        active_window="VS Code",
        screen_context="Editing Atlas brief",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Guardian cockpit"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
    ):
        state = await build_guardian_state(session_id="current", user_message="What should I focus on next?")

    assert "Review the Atlas brief tomorrow morning" in state.world_model.active_commitments
    assert "Atlas investor brief" in state.world_model.active_projects
    assert "Alice owns investor updates" in state.world_model.collaborators
    assert "Weekly investor note goes out on Friday" in state.world_model.recurring_obligations
    assert "Atlas launch timeline ends on Friday" in state.world_model.project_timeline
    assert "Prefers concise morning briefings" in state.memory_context
    assert "avoid direct interruption during deep-work windows" in state.memory_context
    assert "prefer async native continuation when the user is blocked" in state.memory_context
    assert "bundle lower-urgency check-ins instead of interrupting immediately" in state.memory_context
    assert "For advisory interventions, avoid direct interruption during deep-work windows." in state.world_model.active_constraints
    assert "For advisory interventions, prefer async native continuation when the user is blocked." in state.world_model.active_constraints
    assert "For advisory interventions, bundle lower-urgency check-ins instead of interrupting immediately." in state.world_model.active_constraints
    assert "[commitment] Review the Atlas brief tomorrow morning" in state.memory_context


@pytest.mark.asyncio
async def test_build_guardian_state_uses_procedural_memory_guidance_when_live_signal_is_neutral(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "Should this wait until I am available?")
    await sm.add_message("current", "assistant", "Let me check the guardian guidance.")

    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=1,
            not_helpful_count=0,
            acknowledged_count=2,
            failed_count=0,
            bias="neutral",
            phrasing_bias="neutral",
            cadence_bias="neutral",
            channel_bias="prefer_native_notification",
            escalation_bias="prefer_async_native",
            timing_bias="neutral",
            blocked_state_bias="prefer_async_for_blocked_state",
            suppression_bias="neutral",
            thread_preference_bias="neutral",
            blocked_direct_failure_count=0,
            blocked_native_success_count=2,
            available_direct_success_count=0,
        ),
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Protect focus time",
        active_window="Calendar",
        screen_context="In a long meeting block",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="high",
        user_state="deep_work",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            AsyncMock(return_value=GuardianLearningSignal.neutral("advisory")),
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="Should this wait until I am available?",
        )

    assert state.world_model.intervention_receptivity == "guarded_async"
    assert "Async native delivery is usually tolerated better than browser interruption." in state.learning_guidance
    assert "When the user is blocked, prefer async native continuation instead of browser interruption." in state.learning_guidance
    assert "When the user is explicitly available, direct delivery is usually tolerated." not in state.learning_guidance


@pytest.mark.asyncio
async def test_build_guardian_state_prefers_live_learning_when_stale_memory_conflicts(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "Should this wait until I am available?")
    await sm.add_message("current", "assistant", "Let me check the guardian guidance.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Protect focus time",
        active_window="Calendar",
        screen_context="In a long meeting block",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="high",
        user_state="deep_work",
    )
    live_signal = GuardianLearningSignal(
        intervention_type="advisory",
        helpful_count=0,
        not_helpful_count=2,
        acknowledged_count=0,
        failed_count=0,
        bias="neutral",
        phrasing_bias="neutral",
        cadence_bias="neutral",
        channel_bias="neutral",
        escalation_bias="neutral",
        timing_bias="avoid_focus_windows",
        blocked_state_bias="avoid_blocked_state_interruptions",
        suppression_bias="neutral",
        thread_preference_bias="neutral",
        blocked_direct_failure_count=2,
        blocked_native_success_count=0,
        available_direct_success_count=0,
        axis_evidence=_axis_evidence_tuple(
            "timing",
            source="live_signal",
            bias="avoid_focus_windows",
            support_count=2,
            recency_score=0.95,
            confidence_score=1.0,
            quality_score=1.0,
        ),
    )
    procedural_guidance = ProceduralMemoryGuidance(
        intervention_type="advisory",
        timing_bias="prefer_available_windows",
        lesson_types=("timing",),
        axis_evidence=_axis_evidence_tuple(
            "timing",
            source="procedural_memory",
            bias="prefer_available_windows",
            support_count=1,
            recency_score=0.0,
            confidence_score=0.63,
            quality_score=0.4,
        ),
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            AsyncMock(return_value=live_signal),
        ),
        patch(
            "src.memory.procedural_guidance.load_procedural_memory_guidance",
            AsyncMock(return_value=procedural_guidance),
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="Should this wait until I am available?",
        )

    assert "Avoid direct interruptions during deep-work, meeting, or away windows unless urgency is high." in state.learning_guidance
    assert "When possible, deliver nudges while the user is explicitly available." not in state.learning_guidance


@pytest.mark.asyncio
async def test_build_guardian_state_prefers_scoped_project_guidance_over_global_memory(async_db):
    sm = SessionManager()
    await sm.get_or_create("atlas-thread")
    await sm.add_message("atlas-thread", "user", "Should this interrupt Atlas work?")
    await sm.add_message("atlas-thread", "assistant", "Let me load the scoped guidance.")

    timing_scope = {
        "writer": "guardian_feedback",
        "memory_scope": "procedural_learning",
        "intervention_type": "advisory",
        "lesson_type": "timing",
    }
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope=timing_scope,
        content="Global: prefer available windows.",
        summary="Global: prefer available windows.",
        confidence=0.8,
        reinforcement=1.4,
        metadata={"bias_value": "prefer_available_windows", "support_count": 2},
    )
    await memory_repository.sync_scoped_memory(
        kind=MemoryKind.procedural,
        scope={
            **timing_scope,
            "continuity_thread_id": "atlas-thread",
            "active_project": "Atlas",
        },
        content="Scoped: avoid direct interruption during Atlas focus windows.",
        summary="Scoped: avoid direct interruption during Atlas focus windows.",
        confidence=0.9,
        reinforcement=1.6,
        metadata={"bias_value": "avoid_focus_windows", "support_count": 3},
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Protect Atlas focus blocks",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Deep in Atlas implementation",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="high",
        user_state="deep_work",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            AsyncMock(return_value=GuardianLearningSignal.neutral("advisory")),
        ),
    ):
        state = await build_guardian_state(
            session_id="atlas-thread",
            user_message="Should this interrupt Atlas work?",
        )

    assert "Avoid direct interruptions during deep-work, meeting, or away windows unless urgency is high." in state.learning_guidance
    assert "When possible, deliver nudges while the user is explicitly available." not in state.learning_guidance


def test_build_guardian_world_model_surfaces_long_horizon_watchpoints():
    observer_context = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Prepare investor brief",
        active_project="Investor brief",
        active_window="Arc",
        screen_context="Reviewing investor meeting notes",
        upcoming_events=[{"summary": "Investor sync", "start": "2026-03-18T14:00:00Z"}],
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="aligned_work_activity",
        interruption_cost="medium",
        attention_budget_remaining=2,
    )
    model = build_guardian_world_model(
        observer_context=observer_context,
        memory_context="",
        current_session_history="",
        recent_sessions_summary='- Investor brief follow-up: assistant said "Close the investor brief loop before tomorrow."',
        recent_intervention_feedback="",
        active_projects=("Investor brief",),
        recent_execution_summary="- Workflow investor-brief degraded at write_file",
        memory_buckets={
            "routine": ("Review investor brief every morning",),
            "collaborator": ("Alice owns investor brief updates",),
            "obligation": ("Weekly investor note goes out on Friday",),
            "timeline": ("Investor brief needs a clean draft before sync",),
        },
        learning_signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=0,
            not_helpful_count=1,
            acknowledged_count=0,
            failed_count=0,
            bias="neutral",
            phrasing_bias="neutral",
            cadence_bias="neutral",
            channel_bias="neutral",
            escalation_bias="neutral",
            timing_bias="neutral",
            blocked_state_bias="neutral",
            suppression_bias="extend_suppression",
            thread_preference_bias="neutral",
            blocked_direct_failure_count=0,
            blocked_native_success_count=0,
            available_direct_success_count=0,
            multi_day_positive_days=1,
            multi_day_negative_days=3,
            scheduled_positive_days=1,
            scheduled_negative_days=2,
        ),
    )

    assert any(
        "Live goal summary still aligns with project anchor 'Investor brief'." == item
        for item in model.goal_alignment_signals
    )
    assert any("Scheduled review and briefing outcomes have been unstable" in item for item in model.goal_alignment_signals)
    assert any("Weekly investor note goes out on Friday" in item for item in model.routine_watchpoints)
    assert any("Alice owns investor brief updates" in item for item in model.collaborator_watchpoints)
    assert any("Multi-day intervention outcomes have skewed negative" in item for item in model.judgment_risks)


def test_build_guardian_world_model_lowers_receptivity_on_multi_day_negative_trend():
    observer_context = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Protect focus time",
        active_project="Atlas",
        active_window="Calendar",
        screen_context="Meeting prep",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="medium",
        user_state="available",
    )
    model = build_guardian_world_model(
        observer_context=observer_context,
        memory_context="",
        current_session_history="",
        recent_sessions_summary="",
        recent_intervention_feedback="",
        active_projects=("Atlas",),
        recent_execution_summary="",
        memory_buckets={},
        learning_signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=1,
            not_helpful_count=0,
            acknowledged_count=0,
            failed_count=0,
            bias="neutral",
            phrasing_bias="neutral",
            cadence_bias="neutral",
            channel_bias="neutral",
            escalation_bias="neutral",
            timing_bias="neutral",
            blocked_state_bias="neutral",
            suppression_bias="neutral",
            thread_preference_bias="neutral",
            blocked_direct_failure_count=0,
            blocked_native_success_count=0,
            available_direct_success_count=0,
            multi_day_positive_days=1,
            multi_day_negative_days=3,
            scheduled_positive_days=0,
            scheduled_negative_days=0,
        ),
    )

    assert model.intervention_receptivity == "medium"


def test_build_guardian_world_model_surfaces_user_model_preference_inference():
    observer_context = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Protect Atlas focus time",
        active_project="Atlas",
        active_window="Calendar",
        screen_context="Heads-down Atlas launch work",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="high",
        user_state="deep_work",
    )

    model = build_guardian_world_model(
        observer_context=observer_context,
        memory_context="",
        current_session_history="",
        recent_sessions_summary="",
        recent_intervention_feedback="",
        active_projects=("Atlas",),
        memory_buckets={
            "preference": ("Prefers concise updates during Atlas launch work.",),
            "procedural": (
                "For advisory interventions, avoid direct interruption during deep-work windows.",
                "For advisory interventions, prefer async native continuation when the user is blocked.",
                "For advisory interventions, bundle lower-urgency check-ins instead of interrupting immediately.",
            ),
        },
        learning_signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=0,
            not_helpful_count=2,
            acknowledged_count=0,
            failed_count=0,
            bias="reduce_interruptions",
            phrasing_bias="be_brief_and_literal",
            cadence_bias="bundle_more",
            channel_bias="prefer_native_notification",
            escalation_bias="prefer_async_native",
            timing_bias="avoid_focus_windows",
            blocked_state_bias="prefer_async_for_blocked_state",
            suppression_bias="extend_suppression",
            thread_preference_bias="prefer_existing_thread",
            blocked_direct_failure_count=2,
            blocked_native_success_count=1,
            available_direct_success_count=0,
        ),
    )

    assert model.user_model_confidence == "grounded"
    assert (
        "Interruption preference: prefer async or bundled follow-through when urgency is not high."
        in model.user_model_signals
    )
    assert "Communication preference: prefer brief, literal wording." in model.user_model_signals
    assert "Thread preference: prefer existing-thread continuation for follow-up." in model.user_model_signals
    assert (
        "Cadence preference: slower bundled follow-through is safer than rapid re-interruption."
        in model.user_model_signals
    )
    assert any(
        "Interruption preference inferred from procedural guidance, live learning, observer state."
        == item
        for item in model.preference_inference_diagnostics
    )
    assert any(
        "User-model evidence sources: live learning, observer state, preference memory, procedural guidance."
        == item
        for item in model.preference_inference_diagnostics
    )
    prompt = model.to_prompt_block()
    assert "User-model confidence: grounded" in prompt
    assert "User-model signals:" in prompt
    assert "Preference inference diagnostics:" in prompt


def test_build_guardian_world_model_marks_split_user_model_preference_evidence():
    observer_context = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Check Atlas launch readiness",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas launch notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="medium",
        user_state="available",
    )

    model = build_guardian_world_model(
        observer_context=observer_context,
        memory_context="",
        current_session_history="",
        recent_sessions_summary="",
        recent_intervention_feedback="",
        active_projects=("Atlas",),
        memory_buckets={
            "procedural": (
                "For advisory interventions, avoid direct interruption during deep-work windows.",
            ),
        },
        learning_signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=2,
            not_helpful_count=0,
            acknowledged_count=0,
            failed_count=0,
            bias="prefer_direct_delivery",
            phrasing_bias="be_more_direct",
            cadence_bias="check_in_sooner",
            channel_bias="neutral",
            escalation_bias="neutral",
            timing_bias="prefer_available_windows",
            blocked_state_bias="neutral",
            suppression_bias="resume_faster",
            thread_preference_bias="neutral",
            blocked_direct_failure_count=0,
            blocked_native_success_count=0,
            available_direct_success_count=2,
        ),
    )

    assert model.user_model_confidence == "partial"
    assert (
        "Interruption preference evidence is split between guarded_async, direct_when_available."
        in model.preference_inference_diagnostics
    )
    assert any(
        item.startswith("Communication preference inferred from ")
        for item in model.preference_inference_diagnostics
    )
    assert "Communication preference: prefer direct phrasing over softer framing." in model.user_model_signals


def test_build_guardian_world_model_ignores_unrelated_obligation_watchpoints():
    observer_context = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Prepare investor brief",
        active_project="Investor brief",
        active_window="Arc",
        screen_context="Reviewing investor meeting notes",
        upcoming_events=[{"summary": "Investor sync", "start": "2026-03-18T14:00:00Z"}],
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="aligned_work_activity",
        interruption_cost="medium",
        attention_budget_remaining=2,
    )
    model = build_guardian_world_model(
        observer_context=observer_context,
        memory_context="",
        current_session_history="",
        recent_sessions_summary="",
        recent_intervention_feedback="",
        active_projects=("Investor brief",),
        recent_execution_summary="- Workflow investor-brief degraded at write_file",
        memory_buckets={
            "routine": ("Review investor brief every morning",),
            "obligation": ("Payroll reconciliation closes on Tuesday",),
            "timeline": ("Investor brief needs a clean draft before sync",),
        },
        learning_signal=GuardianLearningSignal.neutral("advisory"),
    )

    assert any("Investor brief needs a clean draft before sync" in item for item in model.routine_watchpoints)
    assert not any("Payroll reconciliation closes on Tuesday" in item for item in model.routine_watchpoints)


@pytest.mark.asyncio
async def test_build_guardian_state_prefers_thread_scoped_procedural_guidance_over_project_scope(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "Should this wait until I am available?")
    await sm.add_message("current", "assistant", "Let me check the guardian guidance.")

    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=2,
            not_helpful_count=0,
            acknowledged_count=0,
            failed_count=0,
            bias="neutral",
            phrasing_bias="neutral",
            cadence_bias="neutral",
            channel_bias="neutral",
            escalation_bias="neutral",
            timing_bias="prefer_available_windows",
            blocked_state_bias="neutral",
            suppression_bias="neutral",
            thread_preference_bias="neutral",
            blocked_direct_failure_count=0,
            blocked_native_success_count=0,
            available_direct_success_count=2,
        ),
        active_project="Atlas",
    )
    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=0,
            not_helpful_count=2,
            acknowledged_count=0,
            failed_count=0,
            bias="neutral",
            phrasing_bias="neutral",
            cadence_bias="neutral",
            channel_bias="neutral",
            escalation_bias="neutral",
            timing_bias="avoid_focus_windows",
            blocked_state_bias="avoid_blocked_state_interruptions",
            suppression_bias="neutral",
            thread_preference_bias="neutral",
            blocked_direct_failure_count=2,
            blocked_native_success_count=0,
            available_direct_success_count=0,
        ),
        continuity_thread_id="current",
        active_project="Atlas",
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Protect focus time",
        active_window="Calendar",
        screen_context="In a long meeting block",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="high",
        user_state="deep_work",
        active_project="Atlas",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            AsyncMock(return_value=GuardianLearningSignal.neutral("advisory")),
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="Should this wait until I am available?",
        )

    assert "Avoid direct interruptions during deep-work, meeting, or away windows unless urgency is high." in state.learning_guidance
    assert "When possible, deliver nudges while the user is explicitly available." not in state.learning_guidance


@pytest.mark.asyncio
async def test_build_guardian_state_prefers_context_scoped_guidance_over_global_memory(async_db):
    sm = SessionManager()
    await sm.get_or_create("atlas-thread")
    await sm.add_message("atlas-thread", "user", "Should this wait until I am available?")
    await sm.add_message("atlas-thread", "assistant", "Let me check the scoped guardian guidance.")

    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=0,
            not_helpful_count=2,
            acknowledged_count=0,
            failed_count=0,
            bias="reduce_interruptions",
            phrasing_bias="neutral",
            cadence_bias="bundle_more",
            channel_bias="neutral",
            escalation_bias="neutral",
            timing_bias="avoid_focus_windows",
            blocked_state_bias="avoid_blocked_state_interruptions",
            suppression_bias="neutral",
            thread_preference_bias="neutral",
            blocked_direct_failure_count=2,
            blocked_native_success_count=0,
            available_direct_success_count=0,
        ),
    )
    await sync_learning_signal_memories(
        intervention_type="advisory",
        signal=GuardianLearningSignal(
            intervention_type="advisory",
            helpful_count=2,
            not_helpful_count=0,
            acknowledged_count=0,
            failed_count=0,
            bias="prefer_direct_delivery",
            phrasing_bias="be_more_direct",
            cadence_bias="neutral",
            channel_bias="neutral",
            escalation_bias="neutral",
            timing_bias="prefer_available_windows",
            blocked_state_bias="neutral",
            suppression_bias="resume_faster",
            thread_preference_bias="prefer_existing_thread",
            blocked_direct_failure_count=0,
            blocked_native_success_count=0,
            available_direct_success_count=2,
        ),
        continuity_thread_id="atlas-thread",
        active_project="Atlas",
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Protect focus time",
        active_window="Calendar",
        active_project="Atlas",
        screen_context="Checking the Atlas launch plan",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="high",
        user_state="available",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            AsyncMock(return_value=GuardianLearningSignal.neutral("advisory")),
        ),
    ):
        state = await build_guardian_state(
            session_id="atlas-thread",
            user_message="Should this wait until I am available?",
        )

    assert "When possible, deliver nudges while the user is explicitly available." in state.learning_guidance
    assert "Avoid direct interruptions during deep-work, meeting, or away windows unless urgency is high." not in state.learning_guidance
    assert "When the user is explicitly available, direct delivery is usually tolerated." in state.learning_guidance


@pytest.mark.asyncio
async def test_build_guardian_state_uses_requested_intervention_type_for_learning_arbitration(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "Should this alert cut through immediately?")
    await sm.add_message("current", "assistant", "Let me check the guidance.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Protect focus time",
        active_window="Calendar",
        screen_context="Heads-down work",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="high",
        user_state="available",
        active_project="Atlas",
    )
    get_learning_signal = AsyncMock(return_value=GuardianLearningSignal.neutral("alert"))
    load_guidance = AsyncMock(return_value=ProceduralMemoryGuidance(intervention_type="alert"))

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.get_learning_signal",
            get_learning_signal,
        ),
        patch(
            "src.memory.procedural_guidance.load_procedural_memory_guidance",
            load_guidance,
        ),
    ):
        await build_guardian_state(
            session_id="current",
            user_message="Should this alert cut through immediately?",
            intervention_type="alert",
        )

    assert get_learning_signal.await_count == 4
    get_learning_signal.assert_any_await(intervention_type="alert", limit=12)
    get_learning_signal.assert_any_await(
        intervention_type="alert",
        limit=12,
        session_id="current",
    )
    get_learning_signal.assert_any_await(
        intervention_type="alert",
        limit=12,
        active_project="Atlas",
    )
    get_learning_signal.assert_any_await(
        intervention_type="alert",
        limit=12,
        session_id="current",
        active_project="Atlas",
    )
    load_guidance.assert_awaited_once_with(
        "alert",
        continuity_thread_id="current",
        active_project="Atlas",
    )


@pytest.mark.asyncio
async def test_build_guardian_state_pulls_project_linked_memories_for_active_project(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What matters for Atlas right now?")
    await sm.add_message("current", "assistant", "Let me ground that in active project memory.")

    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Project Atlas",
        entity_type="project",
        aliases=["Atlas"],
    )
    alice = await memory_repository.get_or_create_entity(
        canonical_name="Alice",
        entity_type="person",
    )

    await memory_repository.create_memory(
        content="Prepare the Hermes budget memo.",
        kind=MemoryKind.commitment,
        summary="Prepare the Hermes budget memo",
        importance=0.99,
    )
    await memory_repository.create_memory(
        content="Atlas launch is the active release project.",
        kind=MemoryKind.project,
        summary="Atlas launch",
        importance=0.35,
        project_entity_id=atlas.id,
    )
    await memory_repository.create_memory(
        content="Send the Atlas checklist before Friday.",
        kind=MemoryKind.commitment,
        summary="Send the Atlas checklist before Friday",
        importance=0.3,
        project_entity_id=atlas.id,
    )
    await memory_repository.create_memory(
        content="Alice owns Atlas launch communications.",
        kind=MemoryKind.collaborator,
        summary="Alice owns Atlas launch communications",
        importance=0.25,
        subject_entity_id=alice.id,
        project_entity_id=atlas.id,
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_window="VS Code",
        screen_context="Editing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
    ):
        state = await build_guardian_state(session_id="current", user_message="What matters for Atlas right now?")

    assert "Atlas launch" in state.world_model.active_projects
    assert "Send the Atlas checklist before Friday" in state.world_model.active_commitments
    assert "Alice owns Atlas launch communications" in state.world_model.collaborators
    assert "[project] Atlas launch" in state.memory_context
    assert "[commitment] Send the Atlas checklist before Friday" in state.memory_context


@pytest.mark.asyncio
async def test_build_guardian_state_uses_unique_project_token_fallback_for_linked_recall(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What matters for Atlas today?")
    await sm.add_message("current", "assistant", "Let me pull the Atlas-linked memory.")

    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Atlas launch",
        entity_type="project",
    )
    await memory_repository.create_memory(
        content="Atlas launch is the active release project.",
        kind=MemoryKind.project,
        summary="Atlas launch",
        importance=0.4,
        project_entity_id=atlas.id,
    )
    await memory_repository.create_memory(
        content="Send the Atlas checklist before Friday.",
        kind=MemoryKind.commitment,
        summary="Send the Atlas checklist before Friday",
        importance=0.35,
        project_entity_id=atlas.id,
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_window="VS Code",
        screen_context="Editing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
    ):
        state = await build_guardian_state(session_id="current", user_message="What matters for Atlas today?")

    assert "Atlas launch" in state.world_model.active_projects
    assert "Send the Atlas checklist before Friday" in state.world_model.active_commitments


@pytest.mark.asyncio
async def test_build_guardian_state_routes_temporal_queries_into_episodic_context(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What happened with Atlas upload yesterday?")
    await sm.add_message("current", "assistant", "Let me check the recent Atlas events.")

    atlas = await memory_repository.get_or_create_entity(
        canonical_name="Atlas launch",
        entity_type="project",
    )
    await memory_repository.create_memory(
        content="Atlas launch is the active release project.",
        kind=MemoryKind.project,
        summary="Atlas launch",
        importance=0.8,
        project_entity_id=atlas.id,
    )
    await memory_repository.create_episode(
        session_id="current",
        episode_type="workflow",
        summary="Workflow Atlas deploy failed at upload step",
        content="Workflow Atlas deploy failed at the upload artifact step.",
        project_entity_id=atlas.id,
        salience=0.9,
        confidence=0.8,
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_window="VS Code",
        screen_context="Reviewing Atlas deploy logs",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="What happened with Atlas upload yesterday?",
        )

    assert "[episode] Workflow Atlas deploy failed at upload step" in state.episodic_memory_context
    assert "[project] Atlas launch" in state.memory_context
    assert state.confidence.memory == "grounded"


@pytest.mark.asyncio
async def test_build_guardian_state_uses_bounded_snapshot_with_todo_overlay(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What should I focus on next?")
    await sm.add_message("current", "assistant", "Let me ground that in bounded recall.")
    await sm.replace_todos(
        "current",
        [
            {"content": "Send the Atlas checklist", "completed": False},
            {"content": "Review launch notes", "completed": True},
        ],
    )

    await memory_repository.create_memory(
        content="Atlas launch is the active release project.",
        kind=MemoryKind.project,
        summary="Atlas launch",
        importance=0.9,
    )
    await memory_repository.create_memory(
        content="User prefers concise morning briefings.",
        kind=MemoryKind.communication_preference,
        summary="Prefers concise morning briefings",
        importance=0.8,
    )

    from src.memory.snapshots import refresh_bounded_guardian_snapshot

    await refresh_bounded_guardian_snapshot(
        soul_context="# Soul\n\n## Identity\nBuilder\n\n## Goals\n- Keep the system grounded",
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_window="VS Code",
        screen_context="Editing Atlas launch checklist",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(
                return_value={
                    "Identity": "Builder",
                    "Goals": "- Keep the system grounded",
                }
            ),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
    ):
        state = await build_guardian_state(session_id="current", user_message="What should I focus on next?")

    assert "Identity: Builder" in state.bounded_memory_context
    assert "Atlas launch" in state.bounded_memory_context
    assert "Prefers concise morning briefings" in state.bounded_memory_context
    assert "Send the Atlas checklist" in state.bounded_memory_context
    assert "Review launch notes" in state.bounded_memory_context


@pytest.mark.asyncio
async def test_build_guardian_state_recomputes_structured_bounded_memory_when_snapshot_load_fails(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What should I focus on next?")
    await sm.add_message("current", "assistant", "Let me ground that in bounded recall.")

    await memory_repository.create_memory(
        content="Atlas launch is the active release project.",
        kind=MemoryKind.project,
        summary="Atlas launch",
        importance=0.9,
    )
    await memory_repository.create_memory(
        content="User prefers concise morning briefings.",
        kind=MemoryKind.communication_preference,
        summary="Prefers concise morning briefings",
        importance=0.8,
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_window="VS Code",
        screen_context="Editing Atlas launch checklist",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
        patch(
            "src.memory.snapshots.get_or_create_bounded_guardian_snapshot",
            AsyncMock(side_effect=RuntimeError("snapshot unavailable")),
        ),
    ):
        state = await build_guardian_state(session_id="current", user_message="What should I focus on next?")

    assert "Identity: Builder" in state.bounded_memory_context
    assert "Atlas launch" in state.bounded_memory_context
    assert "Prefers concise morning briefings" in state.bounded_memory_context


@pytest.mark.asyncio
async def test_build_guardian_state_invalidates_bounded_snapshot_when_session_id_is_reused(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What should I focus on next?")
    await sm.add_message("current", "assistant", "Let me ground that in bounded recall.")

    await memory_repository.create_memory(
        content="Atlas launch is the active release project.",
        kind=MemoryKind.project,
        summary="Atlas launch",
        importance=0.9,
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_window="VS Code",
        screen_context="Editing Atlas launch checklist",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
    ):
        first_state = await build_guardian_state(session_id="current", user_message="What should I focus on next?")

    await memory_repository.create_memory(
        content="Hermes budget memo is now active.",
        kind=MemoryKind.project,
        summary="Hermes budget memo",
        importance=0.95,
    )
    await sm.delete("current")
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "What should I focus on next?")
    await sm.add_message("current", "assistant", "Let me ground that in bounded recall.")

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent",
            return_value="",
        ),
    ):
        second_state = await build_guardian_state(session_id="current", user_message="What should I focus on next?")

    assert "Hermes budget memo" not in first_state.bounded_memory_context
    assert "Hermes budget memo" in second_state.bounded_memory_context


def test_guardian_state_prompt_block_exposes_confidence_and_recent_sessions():
    block = _make_guardian_state().to_prompt_block()

    assert "Overall confidence: grounded" in block
    assert "World-model confidence: grounded" in block
    assert "World model:" in block
    assert "Current focus: Ship guardian state while in VS Code" in block
    assert "Intervention receptivity: high" in block
    assert "Active projects:" in block
    assert "Active blockers:" in block
    assert "Next up:" in block
    assert "Dominant thread: Ship guardian state" in block
    assert "Memory signals:" in block
    assert "Continuity threads:" in block
    assert "Recent execution pressure:" in block
    assert "Collaborators:" in block
    assert "Recurring obligations:" in block
    assert "Project timeline:" in block
    assert "Observer model: confidence=grounded | salience=high (active_goals) | interruption_cost=low" in block
    assert "Observer snapshot:" in block
    assert "Bounded recall:" in block
    assert "Relevant memories:" in block
    assert "Relevant episodes:" in block
    assert "Recent sessions:" in block
    assert "Recent intervention feedback:" in block
    assert "Learning diagnostics:" in block
    assert "Recent execution:" in block
    assert "Ship guardian state" in block
    assert "Prior roadmap" in block
    assert "feedback=helpful" in block
    assert "brief-sync degraded" in block


def test_world_model_does_not_count_session_fallback_focus_as_observer_corroboration():
    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="",
        active_window="",
        screen_context="",
        data_quality="good",
        observer_confidence="partial",
        salience_level="medium",
        salience_reason="none",
        interruption_cost="low",
    )

    model = build_guardian_world_model(
        observer_context=ctx,
        memory_context="",
        current_session_history="User: Continue the deployment review.",
        recent_sessions_summary='- Prior thread: Continue the deployment review.',
        recent_intervention_feedback="",
    )

    assert model.current_focus == "Continue the deployment review."
    assert model.focus_source == "current_session"
    assert (
        "Current focus is inferred from current session instead of live observer signals."
        in model.judgment_risks
    )
    assert "observer" not in model.corroboration_sources
    assert set(model.corroboration_sources) == {"current_session", "recent_sessions"}


def test_world_model_prioritizes_live_project_supporting_context():
    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    model = build_guardian_world_model(
        observer_context=ctx,
        memory_context="",
        current_session_history="",
        recent_sessions_summary="",
        recent_intervention_feedback="",
        active_projects=("Atlas",),
        memory_buckets={
            "project": ("Atlas launch",),
            "collaborator": (
                "Bob owns Hermes migration communications",
                "Alice owns Atlas launch communications",
            ),
            "obligation": (
                "Weekly Hermes rollout note goes out on Friday",
                "Weekly Atlas launch note goes out on Friday",
            ),
            "timeline": (
                "Hermes migration timeline ends on Friday",
                "Atlas launch timeline ends on Friday",
            ),
        },
    )

    assert model.collaborators[0] == "Alice owns Atlas launch communications"
    assert model.recurring_obligations[0] == "Weekly Atlas launch note goes out on Friday"
    assert model.project_timeline[0] == "Atlas launch timeline ends on Friday"
    assert (
        "Some recalled collaborator, obligation, or timeline context still points away from live project 'Atlas'."
        in model.judgment_risks
    )


def test_world_model_flags_stale_execution_pressure_against_live_project():
    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    model = build_guardian_world_model(
        observer_context=ctx,
        memory_context="",
        current_session_history="",
        recent_sessions_summary="",
        recent_intervention_feedback="",
        active_projects=("Atlas",),
        recent_execution_summary="- Workflow Hermes migration degraded at notify_release",
        memory_buckets={
            "project": ("Atlas launch",),
        },
    )

    assert "Workflow Hermes migration degraded at notify_release" in model.open_loops_or_pressure
    assert "Workflow Hermes migration degraded at notify_release" in model.active_blockers
    assert (
        "Recent execution pressure does not line up with live project 'Atlas'."
        in model.judgment_risks
    )


def test_world_model_prefers_project_with_stronger_cross_source_evidence():
    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    model = build_guardian_world_model(
        observer_context=ctx,
        memory_context="",
        current_session_history="",
        recent_sessions_summary='- Hermes migration follow-up: assistant said "Finish the Hermes release note."\n- Atlas review: assistant said "Polish the Atlas launch checklist."',
        recent_intervention_feedback="",
        active_projects=("Atlas", "Hermes migration"),
        recent_execution_summary="- Workflow Hermes migration degraded at notify_release",
        memory_buckets={
            "project": ("Hermes migration",),
            "collaborator": (
                "Bob owns Hermes migration communications",
            ),
            "obligation": (
                "Weekly Hermes rollout note goes out on Friday",
            ),
            "timeline": (
                "Hermes migration timeline ends on Friday",
            ),
        },
    )

    assert model.active_projects[0] == "Hermes migration"
    assert model.dominant_thread.startswith("Hermes migration follow-up")
    assert "Bob owns Hermes migration communications" in model.project_state
    assert "Workflow Hermes migration degraded at notify_release" in model.project_state
    assert (
        "Competing project evidence currently favors 'Hermes migration' over live observer project 'Atlas'."
        in model.judgment_risks
    )
    assert (
        "Recent continuity or execution evidence suggests attention is drifting toward 'Hermes migration' instead of 'Atlas'."
        in model.judgment_risks
    )


def test_world_model_flags_project_anchor_ambiguity_when_scores_split():
    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Keep both launch tracks moving",
        active_window="VS Code",
        screen_context="Reviewing release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="low",
    )

    model = build_guardian_world_model(
        observer_context=ctx,
        memory_context="",
        current_session_history="",
        recent_sessions_summary='- Atlas follow-up: assistant said "Close the Atlas launch checklist."\n- Hermes migration follow-up: assistant said "Ship the Hermes rollout note."',
        recent_intervention_feedback="",
        active_projects=("Atlas", "Hermes migration"),
        recent_execution_summary="- Workflow Atlas launch degraded at write_file\n- Workflow Hermes migration degraded at notify_release",
        memory_buckets={
            "project": ("Atlas launch", "Hermes migration"),
            "timeline": (
                "Atlas launch timeline ends on Friday",
                "Hermes migration timeline ends on Friday",
            ),
        },
    )

    assert "Atlas" in model.active_projects[0]
    assert "Hermes migration" == model.active_projects[1]
    assert (
        "Project-anchor evidence remains ambiguous between 'Atlas' and 'Hermes migration'."
        in model.judgment_risks
    )
    assert any("Atlas: score=" in item for item in model.project_ranking_diagnostics)
    assert any(
        "Competing anchor 'Hermes migration' remains close enough" in item
        for item in model.stale_signal_arbitration
    )


def test_world_model_ranks_anchor_context_and_surfaces_stale_signal_arbitration():
    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas launch release notes",
        upcoming_events=[{"summary": "Atlas launch sync"}],
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="medium",
    )

    model = build_guardian_world_model(
        observer_context=ctx,
        memory_context="",
        current_session_history="",
        recent_sessions_summary='- Hermes migration follow-up: close the rollout note.\n- Atlas launch review: close the launch checklist.',
        recent_intervention_feedback="",
        active_projects=("Atlas", "Hermes migration"),
        recent_execution_summary="- Workflow Hermes migration degraded at notify_release",
        memory_buckets={
            "project": ("Hermes migration", "Atlas launch"),
            "routine": (
                "Weekly Atlas launch review happens before the Atlas launch sync",
                "Weekly Hermes rollout review happens before the Hermes note",
            ),
            "obligation": (
                "Atlas launch note goes out before the Atlas launch sync",
                "Weekly Hermes rollout note goes out on Friday",
            ),
            "collaborator": (
                "Alice owns Atlas launch communications",
                "Bob owns Hermes migration communications",
            ),
            "timeline": (
                "Atlas launch timeline ends after the Atlas launch sync",
                "Hermes migration timeline ends on Friday",
            ),
        },
    )

    assert model.active_routines[0].startswith("Weekly Atlas launch review")
    assert model.recurring_obligations[0].startswith("Atlas launch note goes out")
    assert model.collaborators[0] == "Alice owns Atlas launch communications"
    assert model.project_timeline[0].startswith("Atlas launch timeline")
    assert any(
        "Supporting collaborator/obligation/timeline context is mixed" in item
        for item in model.stale_signal_arbitration
    )
    assert any(
        "Recent execution evidence is stale against the preferred project anchor." == item
        for item in model.stale_signal_arbitration
    )


@pytest.mark.asyncio
async def test_build_guardian_state_surfaces_learning_diagnostics(async_db):
    from datetime import datetime, timedelta, timezone
    from sqlmodel import select
    from src.db.models import GuardianIntervention

    base_time = datetime.now(timezone.utc)
    for offset_days, feedback_type, is_scheduled in (
        (0, "not_helpful", True),
        (1, "not_helpful", True),
        (3, "helpful", False),
    ):
        intervention = await guardian_feedback_repository.create_intervention(
            session_id="current",
            message_type="proactive",
            intervention_type="advisory",
            urgency=3,
            content=f"Intervention {offset_days}",
            reasoning="available_capacity",
            is_scheduled=is_scheduled,
            policy_action="act",
            policy_reason="available_capacity",
            guardian_confidence="grounded",
            data_quality="good",
            user_state="available",
            interruption_mode="balanced",
            delivery_decision="deliver",
            latest_outcome="delivered",
            transport="native_notification",
            active_project="Atlas",
        )
        await guardian_feedback_repository.record_feedback(
            intervention.id,
            feedback_type=feedback_type,
        )
        async with async_db() as db:
            stored = (
                await db.execute(
                    select(GuardianIntervention).where(GuardianIntervention.id == intervention.id)
                )
            ).scalar_one()
            stored.updated_at = base_time - timedelta(days=offset_days)
            if stored.feedback_at is not None:
                stored.feedback_at = stored.updated_at
            db.add(stored)
            await db.flush()

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="medium",
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch("src.observer.screen_repository.screen_observation_repo.get_recent_projects", return_value=["Atlas"]),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="How should I handle Atlas today?",
        )

    assert any("Live learning is currently anchored" in item for item in state.learning_diagnostics)
    assert any("Observed outcomes:" in item for item in state.learning_diagnostics)
    assert any("Long-horizon spread:" in item for item in state.learning_diagnostics)


@pytest.mark.asyncio
async def test_build_guardian_state_surfaces_learning_conflict_diagnostics(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "Should Atlas updates wait for an availability window?")
    await sm.add_message("current", "assistant", "Let me reconcile the live and procedural guidance.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="medium",
    )
    live_signal = GuardianLearningSignal(
        intervention_type="advisory",
        helpful_count=0,
        not_helpful_count=2,
        acknowledged_count=0,
        failed_count=1,
        bias="reduce_interruptions",
        phrasing_bias="neutral",
        cadence_bias="neutral",
        channel_bias="neutral",
        escalation_bias="neutral",
        timing_bias="avoid_focus_windows",
        blocked_state_bias="neutral",
        suppression_bias="extend_suppression",
        thread_preference_bias="neutral",
        blocked_direct_failure_count=0,
        blocked_native_success_count=0,
        available_direct_success_count=0,
        axis_evidence=_axis_evidence_tuple(
            "timing",
            source="live_signal",
            bias="avoid_focus_windows",
            support_count=3,
            recency_score=0.96,
            confidence_score=0.94,
            quality_score=0.95,
        ),
    )
    live_resolution = MagicMock(
        effective_signal=live_signal,
        dominant_scope="project",
        decisions=tuple(),
    )
    procedural_guidance = ProceduralMemoryGuidance(
        intervention_type="advisory",
        timing_bias="prefer_available_windows",
        lesson_types=("timing",),
        axis_evidence=_axis_evidence_tuple(
            "timing",
            source="procedural_memory",
            bias="prefer_available_windows",
            support_count=2,
            recency_score=0.35,
            confidence_score=0.64,
            quality_score=0.72,
        ),
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch("src.memory.hybrid_retrieval.search_with_status", return_value=([], False)),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.resolve_learning_signal",
            AsyncMock(return_value=live_resolution),
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent_for_scope",
            AsyncMock(return_value=""),
        ),
        patch(
            "src.memory.procedural_guidance.load_procedural_memory_guidance",
            AsyncMock(return_value=procedural_guidance),
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="Should Atlas updates wait for an availability window?",
        )

    assert any(
        "Conflicting live vs procedural biases:" in item
        for item in state.learning_diagnostics
    )
    assert any(
        "Fresh live outcomes are overruling older procedural guidance on timing." in item
        for item in state.learning_diagnostics
    )


@pytest.mark.asyncio
async def test_build_guardian_state_marks_ambiguous_project_request_for_clarification(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "Can you finish this today?")
    await sm.add_message("current", "assistant", "Let me reconcile the active project signals first.")
    await sm.get_or_create("prior")
    await sm.update_title("prior", "Hermes migration follow-up")
    await sm.add_message("prior", "assistant", "Ship the Hermes rollout note.")

    await memory_repository.create_memory(
        content="Hermes migration remains the live delivery project.",
        kind=MemoryKind.project,
        summary="Hermes migration",
        importance=0.92,
    )
    await memory_repository.create_memory(
        content="Bob owns Hermes migration communications.",
        kind=MemoryKind.collaborator,
        summary="Bob owns Hermes migration communications",
        importance=0.8,
    )
    await memory_repository.create_memory(
        content="Weekly Hermes rollout note goes out on Friday.",
        kind=MemoryKind.obligation,
        summary="Weekly Hermes rollout note goes out on Friday",
        importance=0.78,
    )
    await memory_repository.create_memory(
        content="Hermes migration timeline ends on Friday.",
        kind=MemoryKind.timeline,
        summary="Hermes migration timeline ends on Friday",
        importance=0.77,
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="medium",
    )
    live_signal = GuardianLearningSignal(
        intervention_type="advisory",
        helpful_count=0,
        not_helpful_count=0,
        acknowledged_count=0,
        failed_count=0,
        bias="neutral",
        phrasing_bias="neutral",
        cadence_bias="neutral",
        channel_bias="neutral",
        escalation_bias="neutral",
        timing_bias="neutral",
        blocked_state_bias="neutral",
        suppression_bias="neutral",
        thread_preference_bias="neutral",
        blocked_direct_failure_count=0,
        blocked_native_success_count=0,
        available_direct_success_count=0,
    )
    live_resolution = MagicMock(
        effective_signal=live_signal,
        dominant_scope="global",
        decisions=tuple(),
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch(
            "src.audit.repository.audit_repository.list_events",
            return_value=[
                {
                    "event_type": "tool_result",
                    "tool_name": "workflow_hermes_migration",
                    "details": {
                        "workflow_name": "Hermes migration",
                        "continued_error_steps": ["notify_release"],
                    },
                }
            ],
        ),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas", "Hermes migration"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.resolve_learning_signal",
            AsyncMock(return_value=live_resolution),
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent_for_scope",
            AsyncMock(return_value=""),
        ),
        patch(
            "src.memory.procedural_guidance.load_procedural_memory_guidance",
            AsyncMock(return_value=None),
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="Can you finish this today?",
        )

    assert state.intent_uncertainty_level == "high"
    assert state.intent_resolution == "clarify"
    assert any(
        "ambiguous referent" in item.lower()
        for item in state.intent_uncertainty_diagnostics
    )
    assert any(
        "project anchor" in item.lower() or "project-anchor" in item.lower()
        for item in state.intent_uncertainty_diagnostics
    )
    assert (
        "Intent uncertainty: high (recommended resolution: clarify)"
        in state.to_prompt_block()
    )


@pytest.mark.asyncio
async def test_build_guardian_state_marks_split_preference_evidence_as_caution(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "Should I nudge the Atlas owner now?")
    await sm.add_message("current", "assistant", "Let me check the delivery guidance.")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="partial",
        salience_level="high",
        salience_reason="active_goals",
        interruption_cost="medium",
    )
    live_signal = GuardianLearningSignal(
        intervention_type="advisory",
        helpful_count=2,
        not_helpful_count=0,
        acknowledged_count=0,
        failed_count=0,
        bias="prefer_direct_delivery",
        phrasing_bias="be_more_direct",
        cadence_bias="check_in_sooner",
        channel_bias="neutral",
        escalation_bias="neutral",
        timing_bias="prefer_available_windows",
        blocked_state_bias="neutral",
        suppression_bias="resume_faster",
        thread_preference_bias="neutral",
        blocked_direct_failure_count=0,
        blocked_native_success_count=0,
        available_direct_success_count=2,
    )
    live_resolution = MagicMock(
        effective_signal=live_signal,
        dominant_scope="thread_project",
        decisions=tuple(),
    )
    procedural_guidance = ProceduralMemoryGuidance(
        intervention_type="advisory",
        timing_bias="neutral",
        lesson_types=("timing",),
        axis_evidence=tuple(),
    )
    retrieval = MemoryRetrievalPlanResult(
        semantic_context="",
        episodic_context="",
        memory_buckets={
            "procedural": (
                "For advisory interventions, avoid direct interruption during deep-work windows.",
            ),
        },
        degraded=False,
        lane="semantic",
    )
    arbitration = MagicMock(
        effective_signal=live_signal,
        decisions=tuple(),
    )
    arbitration.conflicting_decisions.return_value = []
    arbitration.procedural_override_conflicts.return_value = []
    arbitration.live_override_conflicts.return_value = []

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch(
            "src.memory.retrieval_planner.plan_memory_retrieval",
            AsyncMock(return_value=retrieval),
        ),
        patch("src.audit.repository.audit_repository.list_events", return_value=[]),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.resolve_learning_signal",
            AsyncMock(return_value=live_resolution),
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent_for_scope",
            AsyncMock(return_value=""),
        ),
        patch(
            "src.memory.procedural_guidance.load_procedural_memory_guidance",
            AsyncMock(return_value=procedural_guidance),
        ),
        patch(
            "src.guardian.learning_arbitration.arbitrate_learning_signal",
            return_value=arbitration,
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="Should I nudge the Atlas owner now?",
        )

    assert state.intent_uncertainty_level == "medium"
    assert state.intent_resolution == "proceed_with_caution"
    assert any(
        "preference evidence is split" in item.lower()
        for item in state.intent_uncertainty_diagnostics
    )
    assert any(
        "observer confidence is partial" in item.lower()
        for item in state.intent_uncertainty_diagnostics
    )
    assert (
        "Intent uncertainty: medium (recommended resolution: proceed_with_caution)"
        in state.to_prompt_block()
    )


@pytest.mark.asyncio
async def test_build_guardian_state_does_not_mark_explicit_project_reference_as_ambiguous(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")
    await sm.add_message("current", "user", "Can Atlas finish this today?")
    await sm.add_message("current", "assistant", "Let me reconcile the active project signals first.")
    await sm.get_or_create("prior")
    await sm.update_title("prior", "Hermes migration follow-up")
    await sm.add_message("prior", "assistant", "Ship the Hermes rollout note.")

    await memory_repository.create_memory(
        content="Hermes migration remains the live delivery project.",
        kind=MemoryKind.project,
        summary="Hermes migration",
        importance=0.92,
    )

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="medium",
    )
    live_resolution = MagicMock(
        effective_signal=GuardianLearningSignal.neutral("advisory"),
        dominant_scope="global",
        decisions=tuple(),
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch(
            "src.audit.repository.audit_repository.list_events",
            return_value=[
                {
                    "event_type": "tool_result",
                    "tool_name": "workflow_hermes_migration",
                    "details": {
                        "workflow_name": "Hermes migration",
                        "continued_error_steps": ["notify_release"],
                    },
                }
            ],
        ),
        patch(
            "src.observer.screen_repository.screen_observation_repo.get_recent_projects",
            return_value=["Atlas", "Hermes migration"],
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.resolve_learning_signal",
            AsyncMock(return_value=live_resolution),
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent_for_scope",
            AsyncMock(return_value=""),
        ),
        patch(
            "src.memory.procedural_guidance.load_procedural_memory_guidance",
            AsyncMock(return_value=None),
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            user_message="Can Atlas finish this today?",
        )

    assert not any(
        "ambiguous referent" in item.lower()
        for item in state.intent_uncertainty_diagnostics
    )


@pytest.mark.asyncio
async def test_build_guardian_state_ignores_memory_query_only_for_intent_uncertainty(async_db):
    sm = SessionManager()
    await sm.get_or_create("current")

    ctx = CurrentContext(
        time_of_day="morning",
        day_of_week="Monday",
        is_working_hours=True,
        active_goals_summary="Support Atlas launch",
        active_project="Atlas",
        active_window="VS Code",
        screen_context="Reviewing Atlas release notes",
        data_quality="good",
        observer_confidence="grounded",
        salience_level="medium",
        salience_reason="active_goals",
        interruption_cost="medium",
    )
    live_resolution = MagicMock(
        effective_signal=GuardianLearningSignal.neutral("advisory"),
        dominant_scope="global",
        decisions=tuple(),
    )

    with (
        patch("src.observer.manager.context_manager.get_context", return_value=ctx),
        patch(
            "src.profile.service.sync_soul_file_to_profile",
            AsyncMock(return_value={"Identity": "Builder"}),
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.resolve_learning_signal",
            AsyncMock(return_value=live_resolution),
        ),
        patch(
            "src.guardian.feedback.guardian_feedback_repository.summarize_recent_for_scope",
            AsyncMock(return_value=""),
        ),
        patch(
            "src.memory.procedural_guidance.load_procedural_memory_guidance",
            AsyncMock(return_value=None),
        ),
    ):
        state = await build_guardian_state(
            session_id="current",
            memory_query="current priorities, commitments, and recent intervention patterns",
        )

    assert state.intent_uncertainty_level == "clear"
    assert state.intent_resolution == "proceed"
    assert state.intent_uncertainty_diagnostics == ()


@patch("src.agent.factory.ToolCallingAgent")
@patch("src.agent.factory.get_model")
def test_create_agent_injects_guardian_state(mock_get_model, mock_agent_cls):
    mock_get_model.return_value = MagicMock()
    mock_agent_cls.return_value = MagicMock()

    create_agent(guardian_state=_make_guardian_state())

    instructions = mock_agent_cls.call_args[1]["instructions"]
    assert "GUARDIAN STATE" in instructions
    assert "Overall confidence: grounded" in instructions
    assert "Current focus: Ship guardian state while in VS Code" in instructions
    assert "Intervention receptivity: high" in instructions
    assert "Bounded recall:" in instructions
    assert "USER IDENTITY" in instructions
    assert "RELEVANT MEMORIES" in instructions
    assert "CONVERSATION HISTORY" in instructions
    assert "Recent intervention feedback:" in instructions


@patch("src.agent.strategist.LiteLLMModel")
def test_create_strategist_agent_accepts_guardian_state(mock_model_cls):
    mock_model_cls.return_value = MagicMock()

    agent = create_strategist_agent(guardian_state=_make_guardian_state())

    assert "Overall confidence: grounded" in agent.instructions
    assert "Current focus: Ship guardian state while in VS Code" in agent.instructions
    assert "Focus alignment: medium" in agent.instructions
    assert "Bounded recall:" in agent.instructions
    assert "Recent sessions:" in agent.instructions
    assert "Recent intervention feedback:" in agent.instructions
