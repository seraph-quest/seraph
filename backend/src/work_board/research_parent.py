"""Server-derived fixed research parent identity and admission contract."""
from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
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


def input_projection(task, model):
    return {"typed_input_digest": task.typed_input_digest,
        "input_artifact_id": task.input_artifact_id, "child_count": len(model.perspectives),
        "source_count": len(model.sources), "no_learning": True}


def strategy_projection(value):
    """Closed canonical original strategy; never reconstruct redacted values."""
    import json
    import re
    from src.work_board.contracts import TaskStrategyBinding
    from src.memory.task_lessons import ResearchStrategy
    if isinstance(value, TaskStrategyBinding):
        value = value.model_dump(mode="python")
    fields = {"schema_version", "status", "method_id", "version", "digest", "typed_data", "reason"}
    if (type(value) is not dict or set(value) != fields
        or type(value["schema_version"]) is not int or value["schema_version"] != 1
        or value["status"] not in {"none", "active"}):
        raise ValueError("research original strategy projection unavailable")
    result = TaskStrategyBinding.model_validate(value).model_dump(mode="json")
    if result != value:
        raise ValueError("research strategy normalization is forbidden")
    if result["status"] == "none" and result["reason"] != "baseline":
        raise ValueError("research baseline strategy reason unavailable")
    if result["status"] == "active":
        for key in ("method_id", "version"):
            if not isinstance(result[key], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", result[key]):
                raise ValueError("research strategy reference unavailable")
        if not re.fullmatch(r"[0-9a-f]{64}", result["digest"]):
            raise ValueError("research strategy digest unavailable")
        typed = ResearchStrategy.model_validate(result["typed_data"]).model_dump(mode="json")
        if typed != result["typed_data"]:
            raise ValueError("research strategy canonical data unavailable")
    nodes = 0
    def visit(item, depth=0):
        nonlocal nodes
        nodes += 1
        if nodes > 256 or depth > 4:
            raise ValueError("research strategy bound exceeded")
        if isinstance(item, dict):
            for child in item.values(): visit(child, depth+1)
        elif isinstance(item, list):
            for child in item: visit(child, depth+1)
        elif isinstance(item, str):
            if len(item.encode("utf-8")) > 4000 or "[redacted" in item.lower():
                raise ValueError("research strategy text unavailable")
    visit(result)
    if result["reason"] is not None and len(result["reason"].encode("utf-8")) > 512:
        raise ValueError("research strategy reason bound exceeded")
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 65536:
        raise ValueError("research strategy byte bound exceeded")
    return result


_PROJECTION_SEAL = object()

@dataclass(frozen=True, init=False)
class NativeResearchProjection:
    """Private owner-produced unchanged strategy proof, never a wire field."""
    authority_digest: str
    kind: str

    def __init__(self, authority, kind, seal):
        if seal is not _PROJECTION_SEAL:
            raise ValueError("native research projection owner required")
        from src.workflows.job_runtime import _digest
        strategy_projection(authority["task_strategy_binding"])
        object.__setattr__(self, "authority_digest", _digest(authority))
        object.__setattr__(self, "kind", kind)

    def matches(self, authority, kind):
        from src.workflows.job_runtime import _digest
        return self.kind == kind and self.authority_digest == _digest(authority)


async def stage_native_projection(db, spec, *, task, attempt, inputs):
    """Stage vault checks before the admission writer, for this fixed parent."""
    if (spec.identity.job_kind != PARENT_KIND or spec.identity.capability_version != "1"
        or spec.parent_job_id or spec.declared_authority.get("capability_id") != PARENT_CAPABILITY
        or type(spec.declared_authority.get("research_authority_schema_version")) is not int
        or spec.declared_authority["research_authority_schema_version"] != 2):
        raise ValueError("native research projection owner unavailable")
    model = ResearchDossierInput.model_validate(inputs)
    authority = spec.declared_authority
    expected = authority_projection(task, model, policy_digest=authority["model_policy_digest"],
        policy_revision=authority["model_policy_revision"], bound=authority["research_slot_allowance_microusd"],
        ceiling=authority["research_owner_ceiling_microusd"], strategy=authority["task_strategy_binding"])
    if (spec.identity.job_id != job_id(task, attempt)
        or spec.identity.owner_kind != "user" or spec.identity.owner_principal_id != task.owner_principal_id
        or spec.identity.idempotency_scope != "work-board-attempt"
        or spec.identity.idempotency_key != f"{task.task_id}:{attempt.attempt_id}"
        or spec.session_id != task.owner_session_id or spec.operator_session_id != task.owner_session_id
        or spec.goal_id != task.goal_id or spec.goal_revision != task.goal_revision
        or type(spec.max_attempts) is not int or spec.max_attempts != 1
        or type(spec.max_outstanding_jobs) is not int or spec.max_outstanding_jobs != 1
        or type(spec.budget_microusd) is not int or spec.budget_microusd != 0 or spec.budget_digest is not None
        or spec.resource_claims or spec.dependencies or authority != expected
        or spec.inputs != input_projection(task, model)
        or spec.run_fingerprint != fingerprint(task, attempt, spec.inputs, expected)):
        raise ValueError("research original owner projection mismatch")
    await secret_safe_strategy_projection(db, spec.declared_authority["task_strategy_binding"])
    return NativeResearchProjection(spec.declared_authority, PARENT_KIND, _PROJECTION_SEAL)


async def secret_safe_strategy_projection(db, value):
    """Validate unchanged original data before preflight or admission contact."""
    from src.vault.redaction import redact_secrets_in_text_readonly
    from src.memory.m5 import sanitize_m5_memory_text
    binding = strategy_projection(value)
    async def scan(item):
        if isinstance(item, dict):
            for child in item.values(): await scan(child)
        elif isinstance(item, list):
            for child in item: await scan(child)
        elif isinstance(item, str):
            if sanitize_m5_memory_text(item) != item:
                raise ValueError("research strategy normalization is forbidden")
            if await redact_secrets_in_text_readonly(db, item, fail_closed=True) != item:
                raise ValueError("research strategy secret scan unavailable")
    await scan(binding)
    return binding


def fixed_child_projection(parent, authority):
    """Only the fixed child writer calls this after its exact parent fences."""
    import json
    from src.workflows.job_runtime import _digest
    from src.work_board.research_contracts import CHILD_KIND
    original = json.loads(parent.declared_authority_json)
    if (parent.job_kind != PARENT_KIND or parent.authority_digest != _digest(original)
        or original.get("research_authority_schema_version") != 2
        or authority.get("task_strategy_binding") != original.get("task_strategy_binding")
        or not authority.get("parent_creation_digest")):
        raise ValueError("research fixed child projection unavailable")
    return NativeResearchProjection(authority, CHILD_KIND, _PROJECTION_SEAL)


def authority_projection(task, model, *, policy_digest, policy_revision, bound, ceiling, strategy=None):
    authority = {"principal": task.owner_principal_id, "owner_kind": "user",
        "session_id": task.owner_session_id, "goal_id": task.goal_id,
        "goal_revision": task.goal_revision, "capability_id": PARENT_CAPABILITY,
        "capability_version": "1", "typed_input_digest": task.typed_input_digest,
        "input_artifact_id": task.input_artifact_id, "live_root_digest": digest(root_binding()),
        "model_policy_digest": policy_digest, "model_policy_revision": policy_revision,
        "source_egress_acknowledged": True, "research_allowance_microusd": bound*len(model.perspectives),
        "research_slot_allowance_microusd": bound, "research_owner_ceiling_microusd": ceiling,
        "permissions": ["workspace_read", "workspace_write", "model_inference", "public_https_text_read"],
        "limits": {"max_seconds": 300, "max_children": 2, "max_depth": 1,
            "max_sources": 4, "max_output_bytes": 65536, "max_attempts": 1}, "no_learning": True}
    if strategy is not None:
        authority["task_strategy_binding"] = strategy_projection(strategy)
        authority["research_authority_schema_version"] = 2
    return authority


def fingerprint(task, attempt, inputs, authority):
    return digest({"task_ref": task.task_id, "attempt_ref": attempt.attempt_id,
        "inputs": inputs, "authority": authority})


def spec_for(task, attempt, inputs: Mapping[str, Any], *, deadline: datetime, strategy=None):
    from src.work_board.contracts import TaskStrategyBinding
    if task.capability_id != PARENT_CAPABILITY or not task.input_artifact_id:
        raise ValueError("research requires its server-bound typed input artifact")
    model = ResearchDossierInput.model_validate(dict(inputs))
    configured, policy_digest = current_inference_policy()
    setup = configured.openrouter_setup
    bound = setup.request_cost_bound_microusd or setup.spend_ceiling_microusd
    if type(bound) is not int or bound <= 0 or bound*len(model.perspectives) > setup.spend_ceiling_microusd:
        raise ValueError("fixed research slots exceed the existing reviewed allowance")
    safe_inputs = input_projection(task, model)
    authority = authority_projection(task, model, policy_digest=policy_digest,
        policy_revision=configured.egress_revision, bound=bound, ceiling=setup.spend_ceiling_microusd,
        strategy=strategy if strategy is not None else TaskStrategyBinding(status="none", reason="baseline"))
    return DurableJobSpec(identity=DurableJobIdentity(job_id=job_id(task, attempt), owner_kind="user",
        owner_principal_id=task.owner_principal_id, job_kind=PARENT_KIND, capability_version="1",
        idempotency_scope="work-board-attempt", idempotency_key=f"{task.task_id}:{attempt.attempt_id}"),
        inputs=safe_inputs, session_id=task.owner_session_id, operator_session_id=task.owner_session_id,
        goal_id=task.goal_id, goal_revision=task.goal_revision, priority=task.priority,
        declared_authority=authority, deadline_at=deadline, max_attempts=1,
        max_outstanding_jobs=1, budget_microusd=0,
        run_fingerprint=fingerprint(task, attempt, safe_inputs, authority))
