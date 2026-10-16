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
# Work Board proposal routes (no legacy alias: a proposal binds its route for
# at most 15 minutes, so pre-split proposals expire or fail drift checks).
PROPOSAL_ROUTES = {
    "work_board_proposal": (InferenceWorkload.INTERACTIVE, "agent_reasoning"),
    "guardian_opportunity_plan": (InferenceWorkload.BACKGROUND, "agent_reasoning"),
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


@pytest.mark.parametrize("route", sorted(PROPOSAL_ROUTES))
def test_proposal_route_spec_states_external_origin_without_legacy_alias(route):
    spec = CANONICAL_ROUTE_SPECS[route]
    assert spec.origin is ContentOrigin.EXTERNAL_UNTRUSTED
    assert (spec.workload, spec.task_class) == PROPOSAL_ROUTES[route]
    assert spec.capabilities == CANONICAL_ROUTE_SPECS["strategist_agent"].capabilities
    assert route_slot_for_task_class(spec.task_class) == "text"
    assert route_lineage(route) == {route}


def test_strategist_route_and_unsplit_routes_keep_a_single_lineage():
    assert CANONICAL_ROUTE_SPECS["strategist_agent"].origin is ContentOrigin.CANONICAL_MEMORY
    assert route_lineage("strategist_agent") == {"strategist_agent"}
    assert route_lineage("general_task_planner") == {"general_task_planner"}


def test_proposal_callers_bind_their_external_content_routes():
    from src.guardian import opportunity_plans, opportunity_runtime
    from src.work_board import triage
    from src.workflows import repo_repair
    assert triage._PROPOSAL_ROUTE == "work_board_proposal"
    assert opportunity_plans.PLAN_RUNTIME_PATH == "guardian_opportunity_plan"
    assert opportunity_runtime.ASSESSMENT_RUNTIME_PATH == "guardian_opportunity_assessment"
    assert repo_repair.REPO_REPAIR_RUNTIME_PATH == "repository_repair"


@pytest.mark.parametrize("route", sorted({**SPLIT_ROUTES, **PROPOSAL_ROUTES}))
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
    workload, task_class = {**SPLIT_ROUTES, **PROPOSAL_ROUTES}[route]
    assert context.workload is workload
    assert context.requirements.task_class == task_class
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


@pytest.mark.parametrize(("route", "priority"), [
    ("guardian_opportunity_assessment", GpuPriority.ACCEPTED_SCHEDULED_GOAL),
    # The operator waits on a specify/decompose request.
    ("work_board_proposal", GpuPriority.INTERACTIVE_CHAT),
    # Plans already pass this tier explicitly; the derived one agrees.
    ("guardian_opportunity_plan", GpuPriority.REPORTS_RESEARCH_MEMORY),
])
def test_external_content_routes_keep_their_admission_priority(route, priority):
    class Context:
        runtime_path = route
        workload = CANONICAL_ROUTE_SPECS[route].workload

    assert priority_for_inference_context(Context()) is priority


# Guard: no caller over external or staged evidence may use the strategist
# route. The strategist route is CANONICAL_MEMORY; a caller that sends it task
# cards, mail, calendar events, public evidence or repository source would
# record a false origin. Every model call site that names a route (literal or
# module constant) is resolved statically; only the strategist itself may
# resolve to ``strategist_agent``.
_ROUTE_ARGUMENTS = {"build_canonical_inference_context": 0, "effective_workload_policy": 0,
    "canonical_route_spec": 0, "openrouter_policy_for_setup": 1, "_policy_for_runtime": 0,
    "_route_binding_for": 0, "repository_transport_route_digest": 1}
_STRATEGIST_CALLERS = {"agent/strategist.py"}
# Modules whose model input is external or staged evidence, and the only
# routes their call sites may resolve to.
_EXTERNAL_CONTENT_CALLERS = {
    "guardian/opportunity_runtime.py": {"guardian_opportunity_assessment"},
    "guardian/opportunity_plans.py": {"guardian_opportunity_plan"},
    "work_board/triage.py": {"work_board_proposal"},
    "work_board/dispatcher.py": {"mail_reply_draft", "calendar_meeting_prep", "readonly_research_child"},
    "workflows/repo_repair.py": {"repository_repair"},
    "workflows/repo_repair_source.py": {"repository_repair"},
}
# Files that may still spell the strategist id: the strategist, its profile,
# the route registry and priority map, legacy column defaults and eval fixtures.
_STRATEGIST_LITERAL_ALLOWED = {
    "agent/strategist.py", "local_runtime_profiles.py", "db/models.py", "evals/harness.py",
    "model_fabric/caller_context.py", "model_fabric/gpu_admission.py",
}


def _route_call_sites():
    import ast
    modules = {str(path.relative_to(SRC)): ast.parse(path.read_text(encoding="utf-8")) for path in SRC.rglob("*.py")}

    def constants(tree):
        return {target.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                for target in node.targets if isinstance(target, ast.Name)}

    every = {}
    for tree in modules.values():
        for name, value in constants(tree).items():
            every.setdefault(name, set()).add(value)
    for relative, tree in modules.items():
        local = constants(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            arguments = [keyword.value for keyword in node.keywords if keyword.arg == "runtime_path"]
            if name in _ROUTE_ARGUMENTS and len(node.args) > _ROUTE_ARGUMENTS[name]:
                arguments.append(node.args[_ROUTE_ARGUMENTS[name]])
            for argument in arguments:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    values = {argument.value}
                elif isinstance(argument, ast.Name) and argument.id in local:
                    values = {local[argument.id]}
                elif isinstance(argument, ast.Attribute):
                    values = every.get(argument.attr, set())
                else:
                    continue  # A runtime value; covered by the module constant checks.
                yield relative, node.lineno, name, values


def test_no_external_content_caller_uses_the_strategist_route():
    sites = list(_route_call_sites())
    strategist = {(relative, line) for relative, line, name, values in sites
                  if "strategist_agent" in values and name != "LocalRuntimeProfile"}
    assert {relative for relative, _ in strategist} <= _STRATEGIST_CALLERS, sorted(strategist)
    for relative, expected in _EXTERNAL_CONTENT_CALLERS.items():
        resolved = set().union(*(values for path, _, _, values in sites if path == relative))
        assert resolved == expected, (relative, resolved)
        assert all(CANONICAL_ROUTE_SPECS[route].origin is ContentOrigin.EXTERNAL_UNTRUSTED for route in resolved)
    assert all(CANONICAL_ROUTE_SPECS[route].origin is ContentOrigin.EXTERNAL_UNTRUSTED
               for route in COMMUNICATION_ROUTE_BY_CAPABILITY.values())


def test_strategist_id_is_spelled_only_where_allowed():
    named = {str(path.relative_to(SRC)) for path in SRC.rglob("*.py")
             if '"strategist_agent"' in path.read_text(encoding="utf-8")}
    triage = [line for line in (SRC / "work_board/triage.py").read_text(encoding="utf-8").splitlines()
              if '"strategist_agent"' in line]
    # Triage keeps it only as the proposal capability identity, not a route.
    assert triage == ['_PROPOSAL_CAPABILITY = "strategist_agent"']
    assert named - {"work_board/triage.py"} <= _STRATEGIST_LITERAL_ALLOWED, sorted(named - _STRATEGIST_LITERAL_ALLOWED)


async def assert_split_route_receipt(route, contexts, *, extra_origins=frozenset()):
    """Assert one actual governed call ran on ``route``, as its receipt and context state.

    Shared by the caller vertical tests: ``contexts`` are the request contexts
    their intercepted transport received; the receipt is the persisted route
    receipt of that same run.
    """
    from src.model_fabric.repository import model_fabric_repository

    workload = {**SPLIT_ROUTES, **PROPOSAL_ROUTES}[route][0]
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
