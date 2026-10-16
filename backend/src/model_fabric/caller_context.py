"""Canonical caller-side construction of immutable inference contexts."""

from __future__ import annotations

from dataclasses import dataclass
import time
from uuid import uuid4

from src.approval.runtime import get_current_trust_principal
from src.security.trust_contract import (
    AuthorityGrant,
    ContentOrigin,
    DigestSentinel,
    TrustPrincipal,
    TrustProvenance,
    canonical_digest,
)

from .contracts import (
    InferenceRequestContext,
    InferenceRequirements,
    InferenceWorkload,
    ModelCapability,
)
from .configuration import effective_workload_policy


@dataclass(frozen=True)
class CanonicalRouteSpec:
    workload: InferenceWorkload
    capabilities: tuple[ModelCapability, ...]
    task_class: str
    origin: ContentOrigin
    gpu_priority: str


CANONICAL_ROUTE_SPECS: dict[str, CanonicalRouteSpec] = {
    "general_task_planner": CanonicalRouteSpec(
        InferenceWorkload.INTERACTIVE,
        (ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT),
        "interactive_chat",
        ContentOrigin.OPERATOR_INPUT,
        "interactive",
    ),
    "readonly_research_child": CanonicalRouteSpec(
        InferenceWorkload.REPORT,
        (ModelCapability.TEXT,),
        "report_synthesis",
        ContentOrigin.EXTERNAL_UNTRUSTED,
        "normal",
    ),
    # #1018: Cordis inference.request purposes, through the same broker and ledger.
    "cordis_interactive_inference": CanonicalRouteSpec(
        InferenceWorkload.INTERACTIVE,
        (ModelCapability.TEXT,),
        "interactive_chat",
        ContentOrigin.OPERATOR_INPUT,
        "interactive",
    ),
    "cordis_background_inference": CanonicalRouteSpec(
        InferenceWorkload.BACKGROUND,
        (ModelCapability.TEXT,),
        "agent_reasoning",
        ContentOrigin.OPERATOR_INPUT,
        "normal",
    ),
    "chat_agent": CanonicalRouteSpec(
        InferenceWorkload.INTERACTIVE,
        (ModelCapability.TEXT,),
        "interactive_chat",
        ContentOrigin.OPERATOR_INPUT,
        "interactive",
    ),
    "onboarding_agent": CanonicalRouteSpec(
        InferenceWorkload.INTERACTIVE,
        (ModelCapability.TEXT,),
        "interactive_chat",
        ContentOrigin.OPERATOR_INPUT,
        "interactive",
    ),
    "orchestrator_agent": CanonicalRouteSpec(
        InferenceWorkload.INTERACTIVE,
        (ModelCapability.TEXT, ModelCapability.TOOL_USE),
        "interactive_chat",
        ContentOrigin.OPERATOR_INPUT,
        "interactive",
    ),
    "strategist_agent": CanonicalRouteSpec(
        InferenceWorkload.BACKGROUND,
        (ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT),
        "agent_reasoning",
        ContentOrigin.CANONICAL_MEMORY,
        "high",
    ),
    "session_title_generation": CanonicalRouteSpec(
        InferenceWorkload.BACKGROUND,
        (ModelCapability.TEXT,),
        "memory_synthesis",
        ContentOrigin.CANONICAL_MEMORY,
        "normal",
    ),
    "session_consolidation": CanonicalRouteSpec(
        InferenceWorkload.BACKGROUND,
        (ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT),
        "memory_synthesis",
        ContentOrigin.CANONICAL_MEMORY,
        "normal",
    ),
    "context_window_summary": CanonicalRouteSpec(
        InferenceWorkload.BACKGROUND,
        (ModelCapability.TEXT,),
        "memory_synthesis",
        ContentOrigin.CANONICAL_MEMORY,
        "high",
    ),
    "daily_briefing": CanonicalRouteSpec(
        InferenceWorkload.REPORT,
        (ModelCapability.TEXT,),
        "report_synthesis",
        ContentOrigin.CANONICAL_MEMORY,
        "high",
    ),
    "activity_digest": CanonicalRouteSpec(
        InferenceWorkload.REPORT,
        (ModelCapability.TEXT,),
        "report_synthesis",
        ContentOrigin.CANONICAL_MEMORY,
        "high",
    ),
    "evening_review": CanonicalRouteSpec(
        InferenceWorkload.REPORT,
        (ModelCapability.TEXT,),
        "report_synthesis",
        ContentOrigin.CANONICAL_MEMORY,
        "high",
    ),
    "weekly_activity_review": CanonicalRouteSpec(
        InferenceWorkload.REPORT,
        (ModelCapability.TEXT,),
        "report_synthesis",
        ContentOrigin.CANONICAL_MEMORY,
        "high",
    ),
    "end_of_day_goal_report": CanonicalRouteSpec(
        InferenceWorkload.REPORT,
        (ModelCapability.TEXT,),
        "report_synthesis",
        ContentOrigin.PAIRED_EDGE,
        "high",
    ),
    "screenshot_observation_digest": CanonicalRouteSpec(
        InferenceWorkload.REPORT,
        (ModelCapability.TEXT,),
        "report_synthesis",
        ContentOrigin.PAIRED_EDGE,
        "high",
    ),
    "screenshot_image_analysis": CanonicalRouteSpec(
        InferenceWorkload.VISION,
        (ModelCapability.VISION, ModelCapability.STRUCTURED_OUTPUT),
        "vision_analysis",
        ContentOrigin.PAIRED_EDGE,
        "normal",
    ),
    "memory_embedding": CanonicalRouteSpec(
        InferenceWorkload.BACKGROUND,
        (ModelCapability.EMBEDDING,),
        "memory_embedding",
        ContentOrigin.CANONICAL_MEMORY,
        "normal",
    ),
    # #1111: callers over external content, split off ``strategist_agent``
    # so their receipts state the real origin and workload. All of them stay
    # on the text slot, so model selection and spend bounds are unchanged.
    "mail_reply_draft": CanonicalRouteSpec(
        InferenceWorkload.REPORT,
        (ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT),
        "report_synthesis",
        ContentOrigin.EXTERNAL_UNTRUSTED,
        "normal",
    ),
    "calendar_meeting_prep": CanonicalRouteSpec(
        InferenceWorkload.REPORT,
        (ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT),
        "report_synthesis",
        ContentOrigin.EXTERNAL_UNTRUSTED,
        "normal",
    ),
    "guardian_opportunity_assessment": CanonicalRouteSpec(
        InferenceWorkload.BACKGROUND,
        (ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT),
        "agent_reasoning",
        ContentOrigin.EXTERNAL_UNTRUSTED,
        "high",
    ),
    "repository_repair": CanonicalRouteSpec(
        InferenceWorkload.INTERACTIVE,
        (ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT),
        "agent_reasoning",
        ContentOrigin.EXTERNAL_UNTRUSTED,
        "interactive",
    ),
    # Work Board specify/decompose: started only by the authenticated owner,
    # whose request waits on the call, so interactive. The task card and the
    # staged evidence working set are untrusted data, never instructions.
    "work_board_proposal": CanonicalRouteSpec(
        InferenceWorkload.INTERACTIVE,
        (ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT),
        "agent_reasoning",
        ContentOrigin.EXTERNAL_UNTRUSTED,
        "interactive",
    ),
    # Guardian opportunity plans: also started without the operator when a
    # policy auto-stages plans after an assessment, and always admitted at the
    # reports tier, so background. Input is staged public evidence.
    "guardian_opportunity_plan": CanonicalRouteSpec(
        InferenceWorkload.BACKGROUND,
        (ModelCapability.TEXT, ModelCapability.STRUCTURED_OUTPUT),
        "agent_reasoning",
        ContentOrigin.EXTERNAL_UNTRUSTED,
        "normal",
    ),
}

# Rows persisted before #1111 name ``strategist_agent`` for these callers.
# Readback and recovery validators accept that legacy id for existing rows;
# new admissions always use the split route. Spend stays on the one
# deployment ledger under the same owner ids. The two Work Board proposal
# routes have no alias: a proposal binds its route for at most 15 minutes, so
# one made before the split expires or fails its route drift check.
LEGACY_ROUTE_ALIASES: dict[str, str] = {
    "mail_reply_draft": "strategist_agent",
    "calendar_meeting_prep": "strategist_agent",
    "guardian_opportunity_assessment": "strategist_agent",
    "repository_repair": "strategist_agent",
}


COMMUNICATION_ROUTE_BY_CAPABILITY: dict[str, str] = {
    "work.mail-reply-draft.v1": "mail_reply_draft",
    "calendar.meeting-prep.v1": "calendar_meeting_prep",
}


def general_task_group_route(role: str, preparation_binding: object = None) -> str:
    """Return the one route a general-task group member may reserve under."""
    if role == "repository_iteration":
        return "repository_repair"
    if role == "communication_preparation":
        route = COMMUNICATION_ROUTE_BY_CAPABILITY.get(str(getattr(preparation_binding, "capability_id", "")))
        if route is None:
            raise ValueError("communication_preparation_route_unknown")
        return route
    return "general_task_planner"


def route_lineage(runtime_path: str) -> frozenset[str]:
    """Return the route id plus the legacy id that persisted rows may carry."""
    legacy = LEGACY_ROUTE_ALIASES.get(runtime_path)
    return frozenset({runtime_path, legacy} if legacy else {runtime_path})

CANONICAL_SPECIALIST_ROUTES = (
    "memory_keeper",
    "vault_keeper",
    "goal_planner",
    "web_researcher",
    "file_worker",
    "workflow_runner",
)
_SPECIALIST_ROUTE_SPEC = CanonicalRouteSpec(
    InferenceWorkload.INTERACTIVE,
    (ModelCapability.TEXT, ModelCapability.TOOL_USE),
    "interactive_chat",
    ContentOrigin.OPERATOR_INPUT,
    "interactive",
)
CANONICAL_ROUTE_SPECS.update(
    {runtime_path: _SPECIALIST_ROUTE_SPEC for runtime_path in CANONICAL_SPECIALIST_ROUTES}
)


def canonical_route_spec(runtime_path: str) -> CanonicalRouteSpec:
    """Resolve fixed and connected-MCP specialist inference routes."""
    spec = CANONICAL_ROUTE_SPECS.get(runtime_path)
    if spec is not None:
        return spec
    if runtime_path.startswith("mcp_") and len(runtime_path) > len("mcp_"):
        return _SPECIALIST_ROUTE_SPEC
    raise ValueError(f"unregistered canonical inference route: {runtime_path}")


def is_canonical_inference_route(runtime_path: str | None) -> bool:
    if not runtime_path:
        return False
    try:
        canonical_route_spec(runtime_path)
    except ValueError:
        return False
    return True


def build_canonical_inference_context(
    runtime_path: str,
    *,
    payload: object,
    output_tokens: int,
    timeout_seconds: float,
    principal: TrustPrincipal | None = None,
    session_id: str = "",
    job_id: str = "",
    request_id: str = "",
    streaming: bool = False,
    extra_capabilities: tuple[ModelCapability, ...] = (),
    transformation_digest: str = DigestSentinel.NO_TRANSFORMATION,
    redaction_applied: bool = False,
) -> InferenceRequestContext:
    """Build one fail-closed context from a registered production route."""
    spec = canonical_route_spec(runtime_path)

    effective_principal = principal or get_current_trust_principal()
    if effective_principal is None:
        raise PermissionError("inference context requires an authenticated runtime principal")
    if AuthorityGrant.MODEL_INFERENCE not in effective_principal.grants:
        raise PermissionError("inference principal lacks model_inference authority")

    normalized_session_id = str(session_id or effective_principal.session_id or "").strip()
    normalized_job_id = str(job_id or effective_principal.job_id or "").strip()
    if not normalized_session_id and not normalized_job_id:
        raise ValueError("inference context requires an explicit session or job identity")
    if (
        effective_principal.session_id != normalized_session_id
        or effective_principal.job_id != normalized_job_id
    ):
        raise ValueError("inference principal must exactly match session and job identity")

    data_digest = canonical_digest(payload)
    policy = effective_workload_policy(runtime_path)
    if bool(getattr(policy, "fallback_allowed", False)):
        raise PermissionError("active OpenRouter inference forbids provider fallback")
    allowed_provider_kinds = tuple(getattr(policy, "allowed_provider_kinds", ()) or ())
    if allowed_provider_kinds and set(allowed_provider_kinds) != {"openrouter"}:
        raise PermissionError("active OpenRouter inference requires an OpenRouter provider policy")
    egress_class = policy.egress_class
    capabilities = spec.capabilities
    if streaming and ModelCapability.STREAMING not in capabilities:
        capabilities = (*capabilities, ModelCapability.STREAMING)
    for capability in extra_capabilities:
        if capability not in capabilities:
            capabilities = (*capabilities, capability)

    context_tokens = _estimated_context_tokens(payload)
    normalized_output_tokens = max(int(output_tokens), 1)
    owner_budget_microusd = getattr(policy, "max_cost_microusd", None)
    estimated_cost_microusd = _bounded_admission_cost(
        context_tokens,
        normalized_output_tokens,
        owner_budget_microusd,
    )

    return InferenceRequestContext(
        principal=effective_principal,
        session_id=normalized_session_id,
        job_id=normalized_job_id,
        provenance=(
            TrustProvenance(
                origin=spec.origin,
                source_id=runtime_path,
                data_digest=data_digest,
                egress_class=egress_class,
                instruction_authority=spec.origin is ContentOrigin.OPERATOR_INPUT,
            ),
        ),
        data_digest=data_digest,
        egress_class=egress_class,
        transformation_digest=transformation_digest,
        request_id=str(request_id or f"inference:{uuid4().hex}"),
        runtime_path=runtime_path,
        workload=spec.workload,
        requirements=InferenceRequirements(
            capabilities=tuple(capability.value for capability in capabilities),
            context_tokens=context_tokens,
            output_tokens=normalized_output_tokens,
            max_cost_microusd=owner_budget_microusd,
            max_local_resource_ms=max(int(timeout_seconds * 1000), 1),
            max_latency_ms=max(int(timeout_seconds * 1000), 1),
            task_class=spec.task_class,
        ),
        deadline_at=time.time() + max(float(timeout_seconds), 0.1),
        fallback_allowed=False,
        gpu_priority=spec.gpu_priority,
        allowed_profile_ids=tuple(getattr(policy, "allowed_profile_ids", ()) or ()),
        allowed_provider_kinds=("openrouter",),
        redaction_applied=redaction_applied,
        estimated_cost_microusd=estimated_cost_microusd,
        owner_budget_microusd=owner_budget_microusd,
    )


def _estimated_context_tokens(payload: object) -> int:
    """Return a conservative transport-independent input-token requirement."""
    serialized = str(payload)
    return max(1, (len(serialized.encode("utf-8")) + 2) // 3)


def _bounded_admission_cost(
    context_tokens: int,
    output_tokens: int,
    owner_budget_microusd: int | None,
) -> int | None:
    """Derive a deterministic admission estimate from the bounded token request.

    No provider price table is consulted at context construction time.  The
    one-microusd-per-token envelope is deliberately a conservative local
    admission proxy and is capped by the persisted owner ceiling, so the
    remote broker never receives an unknown estimate or a caller-created
    budget.  Final provider cost remains a reconciliation concern.
    """
    if owner_budget_microusd is None:
        return None
    budget = max(int(owner_budget_microusd), 0)
    requested_tokens = max(int(context_tokens), 1) + max(int(output_tokens), 1)
    return min(max(requested_tokens, 1), budget)
