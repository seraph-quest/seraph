"""Deny-only closure of an exact discovery occurrence never claimed/contacted."""
import json
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from src.db.models import Goal, OperatorIdentity, OperatorSession, InferenceCostReservation, WorkflowRunState
from src.goals.contracts import GoalProgramme, GoalProgrammeAuthorityBinding
from src.guardian.goal_programmes import _load
from src.work_board.research_parent import discovery_authority, GoalDiscoveryInputs, DISCOVERY_KIND, DISCOVERY_SERVICE
from src.workflows.job_runtime import _digest, _effect_ledger_or_raise, _job_has_unsafe_effects, _utc_now


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def untouched_denial(db, run):
    """Local canonical DB evidence only; possession never grants execution."""
    authority = discovery_authority(run.declared_authority_json)
    binding = authority.programme_binding
    inputs = GoalDiscoveryInputs.model_validate(json.loads(run.arguments_json))
    raw_inputs = inputs.model_dump(mode="json")
    raw_authority = authority.model_dump(mode="json")
    identifier = uuid5(NAMESPACE_URL,
        f"seraph:public-discovery:{binding.owner_identity_id}:{binding.programme_id}:{authority.occurrence_day}")
    if (run.job_kind != DISCOVERY_KIND or run.owner_kind != "service"
            or run.owner_principal_id != DISCOVERY_SERVICE or run.service_id != DISCOVERY_SERVICE
            or run.status not in {"accepted", "queued"} or run.attempt_count != 0 or run.fencing_token != 0
            or run.lease_owner is not None or run.lease_expires_at is not None
            or run.session_id is not None or run.operator_session_id is not None
            or run.parent_job_id is not None or run.parent_run_identity is not None
            or run.root_run_identity != run.run_identity or run.parent_fencing_token is not None
            or run.capability_version != "1" or run.record_schema_version != 2
            or json.loads(run.dependencies_json) != []
            or run.budget_digest != _digest({"budget_microusd": authority.budget_microusd})
            or run.run_identity != "goal-discovery:" + identifier.hex
            or authority.original_job_id != run.run_identity
            or run.goal_id != binding.goal_id or run.goal_revision != binding.goal_revision
            or run.idempotency_scope != "goal-programme-daily" or run.idempotency_key != identifier.hex
            or json.loads(run.resource_claims_json) != ["goal-discovery:" + binding.owner_identity_id]
            or run.input_digest != _digest(raw_inputs) or run.authority_digest != _digest(raw_authority)
            or run.run_fingerprint != _digest({"inputs": raw_inputs, "authority": raw_authority})
            or run.result_digest is not None or run.result_summary is not None
            or run.deadline_at is None or run.max_attempts != 1):
        raise ValueError("programme_unclaimed_cleanup_binding_denied")
    if (inputs.plan_ref != authority.plan_ref or inputs.public_brief_ref.digest != binding.brief_digest
            or _aware(run.deadline_at) > _aware(binding.expires_at)):
        raise ValueError("programme_unclaimed_cleanup_input_denied")
    if await db.scalar(select(WorkflowRunState.run_identity).where(
            (WorkflowRunState.parent_job_id == run.run_identity)
            | (WorkflowRunState.parent_run_identity == run.run_identity)).limit(1)):
        raise ValueError("programme_unclaimed_cleanup_descendant_denied")
    # Even released or settled cost proves an attempted stage: never erase it.
    if await db.scalar(select(InferenceCostReservation.operation_id).where(
            InferenceCostReservation.job_id == run.run_identity).limit(1)):
        raise ValueError("programme_unclaimed_cleanup_cost_denied")
    history = json.loads(run.checkpoint_receipts_json)
    artifacts = json.loads(run.artifact_receipts_json)
    effects = _effect_ledger_or_raise(run.effect_receipts_json)
    if (len(history) != 2 or len(artifacts) != 2 or len(effects) != 2
            or _job_has_unsafe_effects(effects)):
        raise ValueError("programme_unclaimed_cleanup_history_denied")
    issued = [_aware(datetime.fromisoformat(item["recorded_at"])) for item in history]
    if (issued[0] != issued[1] or _aware(run.deadline_at) != min(
            _aware(binding.expires_at), issued[0] + timedelta(seconds=300))):
        raise ValueError("programme_unclaimed_cleanup_original_deadline_changed")
    from src.artifacts.registry import artifact_id_for
    for kind, reference, path, limit in (
            ("public_brief", inputs.public_brief_ref, inputs.public_brief_file_path, 8000),
            ("plan", inputs.plan_ref, inputs.plan_file_path, 65536)):
        expected_id = artifact_id_for(file_path=path, artifact_type="goal_discovery_" + kind,
            content_sha256=reference.digest, producer=DISCOVERY_KIND, run_id=run.run_identity)
        checkpoints = [item for item in history if item.get("checkpoint_id") == f"discovery:artifact:{kind}:0"]
        receipts = [item for item in artifacts if item.get("artifact_id") == expected_id]
        readbacks = [item for item in effects if item.get("effect_id") == "discovery-artifact:" + expected_id]
        if (len(checkpoints) != 1 or len(receipts) != 1 or len(readbacks) != 1
                or reference.artifact_id != expected_id or not path.startswith("goal-programmes/" + binding.programme_id + "/")):
            raise ValueError("programme_unclaimed_cleanup_initial_refs_denied")
        payload, receipt, effect = checkpoints[0]["payload"], receipts[0], readbacks[0]
        size = payload.get("byte_count")
        if (type(size) is not int or not 0 < size <= limit
                or payload != {"artifact_ref": reference.model_dump(mode="json"), "file_path": path,
                    "kind": kind, "slot": 0, "job_id": run.run_identity, "programme_id": binding.programme_id,
                    "byte_count": size, "producer_fence": 0, "no_learning": True}
                or receipt != {"artifact_id": expected_id, "artifact_type": "goal_discovery_" + kind,
                    "file_path": path, "content_sha256": reference.digest, "size_bytes": size,
                    "producer": DISCOVERY_KIND, "exists": True}
                or effect != {"effect_id": "discovery-artifact:" + expected_id, "receipt_kind": "readback",
                    "effect_type": "research_artifact_readback", "status": "succeeded", "target_path": path,
                    "content_sha256": reference.digest, "target_digest": reference.digest,
                    "verified_at": checkpoints[0]["recorded_at"], "readback_id": "discovery-readback-" + expected_id,
                    "reconciled": True, "reconciliation_status": "resolved",
                    "details": {"verified": True, "no_learning": True}}):
            raise ValueError("programme_unclaimed_cleanup_initial_receipt_denied")
    goal = await db.get(Goal, binding.goal_id, populate_existing=True)
    if goal is None:
        raise ValueError("programme_unclaimed_cleanup_original_goal_missing")
    raw = next((item for item in _load(goal)["generations"] if item["id"] == binding.programme_id), None)
    if raw is None:
        raise ValueError("programme_unclaimed_cleanup_original_generation_missing")
    programme = GoalProgramme.model_validate(raw)
    if GoalProgrammeAuthorityBinding.from_programme(programme, authority.capability_id) != binding:
        raise ValueError("programme_unclaimed_cleanup_original_generation_changed")
    issuer = await db.get(OperatorSession, binding.issuer_root_id, populate_existing=True)
    identity = await db.get(OperatorIdentity, binding.owner_identity_id, populate_existing=True)
    if (issuer is None or identity is None or issuer.is_bearer_tombstone
            or issuer.operator_identity_id != identity.id or issuer.principal_id != binding.issuer_principal_id):
        raise ValueError("programme_unclaimed_cleanup_original_issuer_unproved")
    # Logout/expired browser Root is deliberately not a denial cause.
    if programme.state in {"paused", "revoked"}:
        return "programme_unclaimed_original_" + programme.state
    if identity.revoked_at is not None:
        return "programme_unclaimed_identity_revoked"
    if (goal.revision != binding.goal_revision or goal.owner_principal_id != binding.issuer_principal_id
            or goal.owner_session_id != binding.issuer_root_id
            or str(getattr(goal.status, "value", goal.status)) != "active"):
        return "programme_unclaimed_goal_changed"
    if _utc_now() >= min(_aware(binding.expires_at), _aware(run.deadline_at)):
        return "programme_unclaimed_original_expired"
    raise ValueError("programme_unclaimed_cleanup_no_denial")


async def close_untouched_occurrence(jobs, job_id):
    # Derive the safe cause from canonical rows, then recheck the same exact
    # cause in the existing serialized cancellation writer. No filesystem,
    # policy contact, artifact adoption, ledger reconciliation or callback await.
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        cause = await untouched_denial(db, run)
        revision = run.revision

    async def deny_only(db, current):
        if await untouched_denial(db, current) != cause:
            raise ValueError("programme_unclaimed_cleanup_cause_changed")

    return await jobs.cancel_job(job_id, expected_revision=revision, reason=cause,
        cancellation_authority_check=deny_only,
        result={"denial_cause": cause, "never_claimed": True, "no_learning": True},
        result_summary=cause)
