"""Paired fixed research funding in the existing accounting ledger/witness."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from config.settings import settings
from src.db.models import InferenceCostReservation
from src.model_fabric.effective_policy import current_inference_policy
from src.work_board.research_contracts import CHILD_KIND, PARENT_KIND, PROMPT_READY
from src.workflows.inference_accounting import (InferenceAccountingError, _continuity_lock,
    _json, _operation_payload, _utc, integer_amount, period_id)
from src.workflows.research_guard import assert_research_parent_current
from src.workspace.accounting_witness import period_state, unreviewed_overruns


def payload_checkpoint(run, identifier):
    matches = [item.get("payload") for item in json.loads(run.checkpoint_receipts_json)
        if item.get("checkpoint_id") == identifier]
    if len(matches) != 1 or not isinstance(matches[0], dict):
        raise InferenceAccountingError("research_checkpoint_unavailable")
    return matches[0]


async def fund_fixed_group(jobs, *, parent_id, owner, fencing_token):
    """Both exact prompt-ready slot rows or neither; no independent counter."""
    from src.workflows.job_runtime import _assert_canonical_goal_fence
    now = datetime.now(timezone.utc)
    async with jobs._session() as db:
        await jobs._accounting_begin(db)
        account, rows = await jobs._accounting_rows(db)
        with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
            jobs._assert_accounting_continuity(workspace, account, rows)
            parent = await jobs._fetch(db, parent_id)
            jobs._assert_lease(parent, owner=owner, fencing_token=fencing_token)
            if parent.status != "running" or parent.job_kind != PARENT_KIND or _utc(parent.deadline_at) <= now:
                raise InferenceAccountingError("research_funding_parent_invalid")
            await _assert_canonical_goal_fence(db, goal_id=parent.goal_id, goal_revision=parent.goal_revision,
                owner_kind=parent.owner_kind, owner_principal_id=parent.owner_principal_id,
                session_id=parent.session_id, authority=parent.declared_authority_json)
            creation = payload_checkpoint(parent, "research:creation")
            phase = payload_checkpoint(parent, "research:phase")
            if phase.get("phase") != "research_funding" or phase.get("creation_digest") != creation["creation_digest"]:
                raise InferenceAccountingError("research_funding_phase_invalid")
            authority = json.loads(parent.declared_authority_json)
            configured, policy_digest = current_inference_policy()
            setup = configured.openrouter_setup
            bound = integer_amount(setup.request_cost_bound_microusd or setup.spend_ceiling_microusd, positive=True)
            allowance = integer_amount(authority["research_allowance_microusd"], positive=True)
            slot_allowance = integer_amount(authority["research_slot_allowance_microusd"], positive=True)
            owner_ceiling = integer_amount(authority["research_owner_ceiling_microusd"], positive=True)
            if (policy_digest != creation["model_policy_digest"] or bound > slot_allowance
                or len(creation["child_ids"]) not in {1, 2} or bound * len(creation["child_ids"]) > allowance
                or account.ceiling_microusd != setup.spend_ceiling_microusd):
                raise InferenceAccountingError("research_reviewed_allowance_changed")
            period = period_id(now)
            operations = [_operation_payload(row) for row in rows]
            period_status = period_state(account.model_dump(mode="json"), operations, period)
            if period_status["reason_code"] or unreviewed_overruns(account.model_dump(mode="json"), operations):
                raise InferenceAccountingError(period_status["reason_code"] or "provider_charge_exceeded_reservation")
            candidates = []
            for slot, child_id in enumerate(creation["child_ids"]):
                child = await jobs._fetch(db, child_id)
                await assert_research_parent_current(db, child)
                ready = payload_checkpoint(child, "research:prompt-ready")
                child_authority = json.loads(child.declared_authority_json)
                if (child.job_kind != CHILD_KIND or child.status != "paused" or child.failure_reason != PROMPT_READY
                    or child.lease_owner or child.lease_expires_at or child.parent_fencing_token != creation["creation_job_fence"]
                    or child_authority.get("parent_creation_digest") != creation["creation_digest"]
                    or ready.get("creation_digest") != creation["creation_digest"]
                    or ready.get("prompt_ready_fence") != child.fencing_token or ready.get("slot") != slot
                    or _utc(child.deadline_at) <= now or ready.get("policy_digest") != policy_digest):
                    raise InferenceAccountingError("research_prompt_ready_binding_invalid")
                from src.work_board.research_artifacts import read
                from src.security.trust_contract import canonical_digest
                prompt = json.loads(read(ready["file_path"], ready["content_sha256"]))
                if canonical_digest(prompt) != ready["payload_digest"]:
                    raise InferenceAccountingError("research_prompt_digest_changed")
                operation_id = "remote:"+child_id
                group_binding = {"kind": "research_group_reservation", "parent_job_id": parent_id,
                    "creation_digest": creation["creation_digest"], "slot": slot,
                    "prompt_ready_fence": child.fencing_token, "group_call_ids": ["remote:"+item for item in creation["child_ids"]]}
                existing = next((row for row in rows if row.operation_id == operation_id), None)
                expected = {"job_id": child_id, "owner_id": child.owner_principal_id,
                    "goal_id": child.goal_id, "goal_revision": child.goal_revision,
                    "payload_digest": ready["payload_digest"], "policy_digest": policy_digest,
                    "runtime_path": "readonly_research_child", "profile_id": setup.profile_id,
                    "bound_microusd": bound, "owner_ceiling_microusd": owner_ceiling,
                    "job_fencing_token": child.fencing_token}
                if existing is not None and (existing.state != "reserved" or existing.contact_started_at is not None
                    or any(getattr(existing, key) != value for key, value in expected.items())
                    or group_binding not in json.loads(existing.evidence_json)):
                    raise InferenceAccountingError("research_group_reservation_conflict")
                candidates.append((child, expected, group_binding, existing))
            existing_count = sum(item[3] is not None for item in candidates)
            if existing_count:
                if existing_count != len(candidates):
                    raise InferenceAccountingError("research_group_partial_reservation")
                return [_operation_payload(item[3]) for item in candidates]
            def held(row):
                return row.bound_microusd if row.state in {"reserved", "contact_started", "unknown"} else (
                    (row.actual_cost_microusd or 0) if row.state == "settled" and row.period_id >= period else 0)
            total = bound * len(candidates)
            if sum(held(row) for row in rows) + total > account.ceiling_microusd:
                raise InferenceAccountingError("deployment_cost_budget_exhausted")
            if sum(held(row) for row in rows if row.owner_id == parent.owner_principal_id) + total > owner_ceiling:
                raise InferenceAccountingError("owner_cost_budget_exhausted")
            created = []
            for child, expected, group_binding, _existing in candidates:
                row = InferenceCostReservation(operation_id="remote:"+child.run_identity,
                    deployment_id=account.deployment_id, period_id=period, settings_revision=account.settings_revision,
                    ceiling_microusd=account.ceiling_microusd, sequence=account.revision+len(created)+1,
                    priority=parent.priority, deadline_at=child.deadline_at,
                    created_at=now.replace(tzinfo=None), updated_at=now.replace(tzinfo=None),
                    evidence_json=_json([{"kind": "reservation", "bound_microusd": bound, "memory_status": "no_learning"}, group_binding]), **expected)
                db.add(row)
                rows.append(row)
                created.append(row)
            await jobs._persist_accounting_witness(db, workspace, account, rows)
            return [_operation_payload(row) for row in created]


async def rebind_funded_child(jobs, *, request, owner, fencing_token, policy_digest, profile_id, bound):
    """Rebind the exact untouched row to a fresh child lease; never reserve again."""
    async with jobs._session() as db:
        await jobs._accounting_begin(db)
        account, rows = await jobs._accounting_rows(db)
        with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
            jobs._assert_accounting_continuity(workspace, account, rows)
            child = await jobs._fetch(db, request.job_id)
            jobs._assert_lease(child, owner=owner, fencing_token=fencing_token)
            await assert_research_parent_current(db, child)
            ready = payload_checkpoint(child, "research:prompt-ready")
            parent = await jobs._fetch(db, child.parent_job_id)
            creation = payload_checkpoint(parent, "research:creation")
            expected_calls = ["remote:"+item for item in creation["child_ids"]]
            for slot, operation_id in enumerate(expected_calls):
                funded = next((item for item in rows if item.operation_id == operation_id), None)
                if (funded is None or funded.job_id != creation["child_ids"][slot]
                    or not any(item.get("kind") == "research_group_reservation"
                        and item.get("creation_digest") == creation["creation_digest"]
                        and item.get("slot") == slot and item.get("group_call_ids") == expected_calls
                        for item in json.loads(funded.evidence_json))):
                    raise InferenceAccountingError("research_group_readback_incomplete")
            row = next((item for item in rows if item.operation_id == request.operation_id), None)
            if (child.job_kind != CHILD_KIND or child.status != "running" or row is None
                or row.state != "reserved" or row.contact_started_at is not None or row.recovery_reason
                or row.job_id != child.run_identity or row.owner_id != request.owner_id
                or row.payload_digest != request.data_digest or ready["payload_digest"] != request.data_digest
                or row.policy_digest != policy_digest or row.profile_id != profile_id or row.bound_microusd != bound
                or _utc(row.deadline_at) != _utc(child.deadline_at) or request.deadline_at > _utc(child.deadline_at).timestamp()
                or not any(item.get("kind") == "research_group_reservation"
                    and item.get("creation_digest") == ready["creation_digest"] and item.get("slot") == ready["slot"]
                    and item.get("prompt_ready_fence") == ready["prompt_ready_fence"] for item in json.loads(row.evidence_json))):
                raise InferenceAccountingError("research_funded_binding_invalid")
            history = json.loads(row.evidence_json)
            if row.job_fencing_token != fencing_token:
                row.job_fencing_token = fencing_token
                row.revision += 1
                row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                history.append({"kind": "research_current_child_fence", "fencing_token": fencing_token,
                    "creation_digest": ready["creation_digest"], "memory_status": "no_learning"})
                row.evidence_json = _json(history)
                db.add(row)
                await jobs._persist_accounting_witness(db, workspace, account, rows)
            return _operation_payload(row)


from contextlib import asynccontextmanager


@asynccontextmanager
async def discovery_accounting_scope(jobs, *, job_id=None, operation_id=None):
    """Stage current policy before only a programme's existing ledger writer."""
    from src.db.models import WorkflowRunState
    from sqlalchemy import select
    from src.work_board.research_parent import DISCOVERY_KIND
    if ((job_id is not None and not str(job_id).startswith("goal-discovery:"))
            or (job_id is None and not str(operation_id).startswith("remote:goal-discovery:"))):
        yield None
        return
    async with jobs._session() as db:
        if job_id is None:
            operation = await db.scalar(select(InferenceCostReservation).where(InferenceCostReservation.operation_id == operation_id))
            job_id = operation.job_id if operation is not None else None
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id)) if job_id else None
        programme = run is not None and run.job_kind == DISCOVERY_KIND
    if not programme:
        yield None
        return
    from src.workflows.research_guard import discovery_writer_scope
    from src.workflows.research_sources import physical_discovery_inputs
    witness = await physical_discovery_inputs(jobs, job_id)
    async with discovery_writer_scope(witness=witness) as policy:
        yield policy


async def discovery_generation_budget(jobs, db, run, rows, *, new_bound=0, operation_id=None, payload_digest=None):
    """All-period generation spend and liabilities in the sole cost writer."""
    from src.work_board.research_parent import DISCOVERY_KIND, DISCOVERY_SERVICE, discovery_authority
    from src.workflows.research_guard import assert_discovery_authority
    from src.db.models import WorkflowRunState
    from sqlalchemy import select
    if run.job_kind != DISCOVERY_KIND:
        return None
    await assert_discovery_authority(db, run.declared_authority_json, run=run)
    native = discovery_authority(run.declared_authority_json)
    binding = native.programme_binding
    if operation_id is not None:
        prefix = "remote:" + run.run_identity + ":"
        if not operation_id.startswith(prefix) or operation_id[len(prefix):] not in {"0", "1", "2", "3"}:
            raise InferenceAccountingError("programme_operation_lineage_invalid")
        from src.workflows.research_guard import current_discovery_witness
        from src.security.trust_contract import canonical_digest
        witness = current_discovery_witness()
        prompts = [a for a in witness.artifacts.values() if a["kind"] == "prompt" and a["slot"] == int(operation_id[len(prefix):])]
        if len(prompts) != 1 or (payload_digest is not None and canonical_digest(prompts[0]["parsed"]) != payload_digest):
            raise InferenceAccountingError("programme_original_prompt_readback_changed")
    own_operations = [row for row in rows if row.job_id == run.run_identity]
    if operation_id is not None and operation_id not in {row.operation_id for row in own_operations} and len(own_operations) >= 4:
        raise InferenceAccountingError("programme_request_limit_exhausted")
    ids = {row.job_id for row in rows}
    runs = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity.in_(ids)))).scalars()) if ids else []
    parents = {parent.run_identity: parent for parent in runs}
    used = 0
    for row in rows:
        parent = parents.get(row.job_id)
        if parent is None:
            if row.owner_id == DISCOVERY_SERVICE:
                raise InferenceAccountingError("programme_cost_original_job_missing")
            continue
        if parent.job_kind != DISCOVERY_KIND:
            continue
        original = discovery_authority(parent.declared_authority_json).programme_binding
        if original.programme_id != binding.programme_id or original.owner_identity_id != binding.owner_identity_id:
            continue
        if original != binding:
            raise InferenceAccountingError("programme_original_generation_binding_changed")
        if (row.owner_id != DISCOVERY_SERVICE or row.goal_id != original.goal_id
                or row.goal_revision != original.goal_revision
                or row.operation_id not in {"remote:" + parent.run_identity + ":" + str(i) for i in range(4)}):
            raise InferenceAccountingError("programme_cost_original_lineage_changed")
        if row.state in {"reserved", "contact_started", "unknown"}:
            used += integer_amount(row.bound_microusd, positive=True)
        elif row.state == "settled":
            if row.actual_cost_microusd is None:
                raise InferenceAccountingError("programme_settlement_cost_missing")
            used += integer_amount(row.actual_cost_microusd)
        elif row.state != "released":
            raise InferenceAccountingError("programme_cost_state_unknown")
    if used + new_bound > binding.cost_ceiling_microusd:
        raise InferenceAccountingError("programme_generation_cost_budget_exhausted")
    return {"binding": binding.model_dump(mode="json"), "native_job_id": run.run_identity,
        "generation_used_microusd": used, "generation_ceiling_microusd": binding.cost_ceiling_microusd,
        "memory_status": "no_learning"}
