"""Server-derived fixed research parent identity and admission contract."""
from __future__ import annotations

from datetime import datetime
from typing import Mapping, Any

from src.model_fabric.effective_policy import current_inference_policy
from src.work_board.pipeline_contracts import digest
from src.work_board.pipelines import root_binding
from src.work_board.research_contracts import PARENT_CAPABILITY, PARENT_KIND, ResearchDossierInput
from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec


def job_id(task, attempt):
    return "research:"+digest([task.task_id, attempt.attempt_id])[:40]


def expected_identity(task, attempt, spec):
    from src.workflows.job_runtime import _digest
    return {"job_id": spec.identity.job_id, "job_kind": PARENT_KIND,
        "owner_kind": "user", "owner_principal_id": task.owner_principal_id,
        "session_id": task.owner_session_id, "operator_session_id": task.owner_session_id,
        "capability_id": PARENT_CAPABILITY, "capability_version": "1",
        "goal_id": task.goal_id, "goal_revision": task.goal_revision,
        "input_digest": _digest(spec.inputs), "authority_digest": _digest(spec.declared_authority),
        "run_fingerprint": spec.run_fingerprint, "idempotency_scope": "work-board-attempt",
        "idempotency_key": f"{task.task_id}:{attempt.attempt_id}"}


def spec_for(task, attempt, inputs: Mapping[str, Any], *, deadline: datetime):
    if task.capability_id != PARENT_CAPABILITY or not task.input_artifact_id:
        raise ValueError("research requires its server-bound typed input artifact")
    model = ResearchDossierInput.model_validate(dict(inputs))
    configured, policy_digest = current_inference_policy()
    setup = configured.openrouter_setup
    bound = setup.request_cost_bound_microusd or setup.spend_ceiling_microusd
    allowance = bound*len(model.perspectives)
    if type(bound) is not int or bound <= 0 or allowance > setup.spend_ceiling_microusd:
        raise ValueError("fixed research slots exceed the existing reviewed allowance")
    safe_inputs = {"typed_input_digest": task.typed_input_digest,
        "input_artifact_id": task.input_artifact_id, "child_count": len(model.perspectives),
        "source_count": len(model.sources), "no_learning": True}
    authority = {"principal": task.owner_principal_id, "owner_kind": "user",
        "session_id": task.owner_session_id, "goal_id": task.goal_id,
        "goal_revision": task.goal_revision, "capability_id": PARENT_CAPABILITY,
        "capability_version": "1", "typed_input_digest": task.typed_input_digest,
        "input_artifact_id": task.input_artifact_id, "live_root_digest": digest(root_binding()),
        "model_policy_digest": policy_digest, "model_policy_revision": configured.egress_revision,
        "source_egress_acknowledged": True, "research_allowance_microusd": allowance,
        "research_slot_allowance_microusd": bound, "research_owner_ceiling_microusd": setup.spend_ceiling_microusd,
        "permissions": ["workspace_read", "workspace_write", "model_inference", "public_https_text_read"],
        "limits": {"max_seconds": 300, "max_children": 2, "max_depth": 1,
            "max_sources": 4, "max_output_bytes": 65536, "max_attempts": 1}, "no_learning": True}
    return DurableJobSpec(identity=DurableJobIdentity(job_id=job_id(task, attempt), owner_kind="user",
        owner_principal_id=task.owner_principal_id, job_kind=PARENT_KIND, capability_version="1",
        idempotency_scope="work-board-attempt", idempotency_key=f"{task.task_id}:{attempt.attempt_id}"),
        inputs=safe_inputs, session_id=task.owner_session_id, operator_session_id=task.owner_session_id,
        goal_id=task.goal_id, goal_revision=task.goal_revision, priority=task.priority,
        declared_authority=authority, deadline_at=deadline, max_attempts=1,
        max_outstanding_jobs=1, budget_microusd=0,
        run_fingerprint=digest({"task_ref":task.task_id,"attempt_ref":attempt.attempt_id,
            "inputs":safe_inputs,"authority":authority}))


# This branch is selected only by the native public programme owner. Generic
# Work requests cannot select a service principal or fabricate an issuer Root.
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from typing import Literal
from src.goals.contracts import GoalProgrammeAuthorityBinding
from src.guardian.research_plan_contracts import ArtifactRef

DISCOVERY_KIND = "goal_public_discovery_v1"
DISCOVERY_CAPABILITY = "guardian.goal-discovery.v1"
DISCOVERY_SERVICE = "service:guardian-goal-programmes"


class GoalDiscoveryAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    authority_type: Literal["goal_programme_discovery_v1"]
    principal: Literal["service:guardian-goal-programmes"]
    owner_kind: Literal["service"]
    service_id: Literal["service:guardian-goal-programmes"]
    capability_id: Literal["guardian.goal-discovery.v1"]
    capability_version: Literal["1"]
    goal_owner_principal_id: str
    goal_owner_session_id: str
    programme_binding: GoalProgrammeAuthorityBinding
    plan_ref: ArtifactRef
    occurrence_day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    original_job_id: str
    budget_microusd: int = Field(strict=True, ge=0)
    no_learning: Literal[True]

    @field_validator("no_learning", mode="before")
    @classmethod
    def explicit_no_learning(cls, value):
        if value is not True:
            raise ValueError("programme execution never implicitly learns")
        return value

    @model_validator(mode="after")
    def immutable_owner(self):
        binding = self.programme_binding
        if (binding.capability_id != self.capability_id
                or self.goal_owner_principal_id != binding.issuer_principal_id
                or self.goal_owner_session_id != binding.issuer_root_id
                or self.budget_microusd > binding.cost_ceiling_microusd):
            raise ValueError("programme native authority widens or changes its immutable issuer")
        from datetime import date
        date.fromisoformat(self.occurrence_day)
        if not self.original_job_id.startswith("goal-discovery:"):
            raise ValueError("programme job requires its native lineage")
        return self


def discovery_authority(value):
    if isinstance(value, str):
        import json
        value = json.loads(value)
    return GoalDiscoveryAuthority.model_validate(value)


class GoalDiscoveryInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plan_ref: ArtifactRef
    plan_file_path: str
    public_brief_ref: ArtifactRef
    public_brief_file_path: str
    no_learning: Literal[True]

    @field_validator("no_learning", mode="before")
    @classmethod
    def literal_no_learning(cls, value):
        if value is not True:
            raise ValueError("explicit no-learning required")
        return value

    @field_validator("plan_file_path", "public_brief_file_path")
    @classmethod
    def native_namespace(cls, value):
        import re
        if re.fullmatch(r"goal-programmes/[0-9a-f]{32}/[0-9a-f]{64}-[0-9a-f]{64}\.json", value) is None:
            raise ValueError("original native programme artifact path required")
        return value
