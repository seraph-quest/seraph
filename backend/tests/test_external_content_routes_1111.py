"""#1111: external-content model callers run on routes that state their real origin.

Offline: contexts are built against the governed OpenRouter route without any
transport; no provider is contacted.
"""
from pathlib import Path
import re

import pytest

from src.model_fabric.caller_context import (
    CANONICAL_ROUTE_SPECS,
    COMMUNICATION_ROUTE_BY_CAPABILITY,
    general_task_group_route,
    route_lineage,
)
from src.model_fabric.configuration import route_slot_for_task_class
from src.model_fabric.contracts import InferenceWorkload, ModelCapability
from src.model_fabric.gpu_admission import GpuPriority, priority_for_inference_context
from src.security.trust_contract import ContentOrigin

SPLIT_ROUTES = {
    "mail_reply_draft": (InferenceWorkload.REPORT, "report_synthesis"),
    "calendar_meeting_prep": (InferenceWorkload.REPORT, "report_synthesis"),
    "guardian_opportunity_assessment": (InferenceWorkload.BACKGROUND, "agent_reasoning"),
    "repository_repair": (InferenceWorkload.INTERACTIVE, "agent_reasoning"),
}
SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.mark.parametrize("route", sorted(SPLIT_ROUTES))
def test_split_route_spec_states_external_origin_and_matching_workload(route):
    spec = CANONICAL_ROUTE_SPECS[route]
    workload, task_class = SPLIT_ROUTES[route]
    assert spec.origin is ContentOrigin.EXTERNAL_UNTRUSTED
    assert (spec.workload, spec.task_class) == (workload, task_class)
    # Same capabilities and text slot as the strategist route they left, so
    # model selection and spend bounds do not change.
    assert spec.capabilities == CANONICAL_ROUTE_SPECS["strategist_agent"].capabilities
    assert route_slot_for_task_class(spec.task_class) == "text"
    assert route_lineage(route) == {route, "strategist_agent"}


def test_strategist_route_and_unsplit_routes_keep_a_single_lineage():
    assert CANONICAL_ROUTE_SPECS["strategist_agent"].origin is ContentOrigin.CANONICAL_MEMORY
    assert route_lineage("strategist_agent") == {"strategist_agent"}
    assert route_lineage("general_task_planner") == {"general_task_planner"}


@pytest.mark.parametrize("route", sorted(SPLIT_ROUTES))
def test_built_context_carries_untrusted_provenance_without_instruction_authority(route, monkeypatch):
    from src.model_fabric import caller_context
    from src.model_fabric.configuration import WorkloadPolicy
    from src.security.trust_contract import AuthorityGrant, EgressClass, PrincipalType, TrustPrincipal

    monkeypatch.setattr(caller_context, "effective_workload_policy", lambda runtime_path: WorkloadPolicy(
        runtime_path=runtime_path, egress_class=EgressClass.CLOUD_ALLOWED_FULL, cloud_egress_acknowledged=True,
        allowed_profile_ids=("openrouter.text",), allowed_provider_kinds=("openrouter",),
        fallback_allowed=False, max_cost_microusd=50_000))
    principal = TrustPrincipal(principal_id="operator:owner", principal_type=PrincipalType.OPERATOR,
        authenticated=True, revoked=False, grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id="session-1", operator_session_id="session-1", job_id="job-1")
    context = caller_context.build_canonical_inference_context(route, payload={"external": "data"},
        output_tokens=128, timeout_seconds=10, principal=principal, session_id="session-1", job_id="job-1")
    assert context.runtime_path == route
    assert context.workload is SPLIT_ROUTES[route][0]
    assert context.requirements.task_class == SPLIT_ROUTES[route][1]
    assert ModelCapability.STRUCTURED_OUTPUT.value in context.requirements.capabilities
    assert [(item.origin, item.source_id, item.instruction_authority) for item in context.provenance] == [
        (ContentOrigin.EXTERNAL_UNTRUSTED, route, False)]


def test_task_group_members_reserve_under_their_role_route():
    class Binding:
        def __init__(self, capability_id):
            self.capability_id = capability_id

    assert general_task_group_route("repository_iteration") == "repository_repair"
    assert general_task_group_route("communication_preparation", Binding("work.mail-reply-draft.v1")) == "mail_reply_draft"
    assert general_task_group_route("communication_preparation", Binding("calendar.meeting-prep.v1")) == "calendar_meeting_prep"
    for role in ("initial_proposal", "continuation", "specialist"):
        assert general_task_group_route(role) == "general_task_planner"
    with pytest.raises(ValueError):
        general_task_group_route("communication_preparation", Binding("unknown.v1"))
    with pytest.raises(ValueError):
        general_task_group_route("communication_preparation", None)
    assert set(COMMUNICATION_ROUTE_BY_CAPABILITY.values()) <= set(SPLIT_ROUTES)


def test_opportunity_assessment_keeps_the_accepted_goal_gpu_priority():
    class Context:
        runtime_path = "guardian_opportunity_assessment"
        workload = InferenceWorkload.BACKGROUND

    assert priority_for_inference_context(Context()) is GpuPriority.ACCEPTED_SCHEDULED_GOAL


# Inventory: the external-content callers must not name the strategist route.
# Entries outside this map that still name it are the strategist itself, the
# Work Board proposal route, legacy column defaults and eval-only facades.
_EXTERNAL_CONTENT_CALLERS = {
    "guardian/opportunity_runtime.py": "guardian_opportunity_assessment",
    "workflows/repo_repair.py": "repository_repair",
    "workflows/repo_repair_source.py": "repository_repair",
    "work_board/communication_preparation.py": None,
}
_STRATEGIST_ROUTE_ALLOWED = {
    "agent/strategist.py", "work_board/triage.py", "guardian/opportunity_plans.py",
    "db/models.py", "db/engine.py", "evals/harness.py", "local_runtime_profiles.py",
    "model_fabric/caller_context.py", "model_fabric/gpu_admission.py",
}


def test_no_external_content_caller_remains_on_the_strategist_route():
    for relative in _EXTERNAL_CONTENT_CALLERS:
        assert '"strategist_agent"' not in (SRC / relative).read_text(encoding="utf-8"), relative
    dispatcher = (SRC / "work_board/dispatcher.py").read_text(encoding="utf-8")
    assert '"strategist_agent"' not in dispatcher
    assert len(re.findall(r'runtime_path="mail_reply_draft"', dispatcher)) == 1
    assert len(re.findall(r'runtime_path="calendar_meeting_prep"', dispatcher)) == 1
    named = {str(path.relative_to(SRC)) for path in SRC.rglob("*.py")
             if '"strategist_agent"' in path.read_text(encoding="utf-8")}
    assert named <= _STRATEGIST_ROUTE_ALLOWED, sorted(named - _STRATEGIST_ROUTE_ALLOWED)


async def assert_split_route_receipt(route, contexts, *, extra_origins=frozenset()):
    """Assert one actual governed call ran on ``route``, as its receipt and context state.

    Shared by the caller vertical tests: ``contexts`` are the request contexts
    their intercepted transport received; the receipt is the persisted route
    receipt of that same run.
    """
    from src.model_fabric.repository import model_fabric_repository

    workload = SPLIT_ROUTES[route][0]
    receipt = (await model_fabric_repository.latest_routes_for_runtime_paths((route,)))[route]
    assert (receipt.runtime_path, receipt.workload) == (route, workload.value)
    assert contexts and all(item.runtime_path == route and item.workload is workload for item in contexts)
    # The final-body binding adds Seraph's own transport payload item; every
    # other item is the caller's content and must carry no instruction authority.
    content = [item for context in contexts for item in context.provenance
               if item.source_id != "model_fabric_transport_payload"]
    origins = {item.origin for item in content}
    assert ContentOrigin.EXTERNAL_UNTRUSTED in origins
    assert origins <= {ContentOrigin.EXTERNAL_UNTRUSTED, *extra_origins}
    assert not any(item.instruction_authority for item in content)
    return receipt
