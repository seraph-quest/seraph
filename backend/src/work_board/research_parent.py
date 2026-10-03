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
