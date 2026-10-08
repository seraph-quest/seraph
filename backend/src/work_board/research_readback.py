"""Canonical research identity and physical dossier proof for existing Review."""
from __future__ import annotations

import json
from pathlib import Path
import re
from sqlalchemy import select

from config.settings import settings
from src.db.models import WorkflowRunState
from src.work_board.pipeline_contracts import digest
from src.work_board.pipelines import root_binding
from src.work_board.research_artifacts import dossier_bytes, json_bytes, read, verified_child
from src.work_board.research_contracts import PARENT_CAPABILITY, PARENT_KIND, ResearchDossierInput
from src.work_board.research_parent import job_id


def binds(task, attempt, run, *, typed_inputs=None):
    """Recompute fixed input, authority and fingerprint from admitted input."""
    from src.work_board.dispatcher import _parse_typed_input
    from src.workflows.job_runtime import _digest
    try:
        inputs = ResearchDossierInput.model_validate(typed_inputs if typed_inputs is not None else _parse_typed_input(task))
        authority = json.loads(run.declared_authority_json)
        bound = authority["research_slot_allowance_microusd"]
        ceiling = authority["research_owner_ceiling_microusd"]
        if (task.capability_id != PARENT_CAPABILITY or run.job_kind != PARENT_KIND or run.capability_version != "1"
            or run.run_identity != job_id(task, attempt) or attempt.workflow_run_id != run.run_identity
            or run.owner_kind != "user" or run.service_id or run.owner_principal_id != task.owner_principal_id
            or run.session_id != task.owner_session_id or run.operator_session_id != task.owner_session_id
            or run.goal_id != task.goal_id or run.goal_revision != task.goal_revision
            or run.parent_job_id or run.parent_run_identity or run.branch_depth != 0
            or run.max_attempts != 1 or run.budget_digest != _digest({"budget_microusd": 0})
            or json.loads(run.resource_claims_json) != []
            or run.idempotency_scope != "work-board-attempt" or run.idempotency_key != f"{task.task_id}:{attempt.attempt_id}"
            or type(bound) is not int or bound <= 0 or type(ceiling) is not int or ceiling < bound*len(inputs.perspectives)
            or type(authority["model_policy_revision"]) is not int or authority["model_policy_revision"] <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", authority["model_policy_digest"])):
            return False
        from src.work_board.research_parent import authority_projection, input_projection, fingerprint, strategy_projection
        base = authority_projection(task, inputs, policy_digest=authority["model_policy_digest"],
            policy_revision=authority["model_policy_revision"], bound=bound, ceiling=ceiling)
        expected_inputs = input_projection(task, inputs)
        marker = authority.get("research_authority_schema_version")
        if "research_authority_schema_version" in authority:
            if type(marker) is not int or marker != 2:
                return False
            expected_authority = authority_projection(task, inputs,
                policy_digest=authority["model_policy_digest"], policy_revision=authority["model_policy_revision"],
                bound=bound, ceiling=ceiling, strategy=strategy_projection(authority["task_strategy_binding"]))
            expected_fingerprint = fingerprint(task, attempt, expected_inputs, expected_authority)
        else:
            expected_authority = dict(base)
            if "task_strategy_binding" in authority:
                expected_authority["task_strategy_binding"] = strategy_projection(authority["task_strategy_binding"])
            expected_fingerprint = fingerprint(task, attempt, expected_inputs, base)
        if (authority != expected_authority or run.input_digest != _digest(expected_inputs)
            or run.authority_digest != _digest(expected_authority) or run.run_fingerprint != expected_fingerprint):
            return False
        history = json.loads(run.checkpoint_receipts_json)
        if type(history) is not list or any(type(item) is not dict for item in history):
            return False
        records = [item for item in history if item.get("checkpoint_id") == "research:creation"]
        if records:
            creation = _checkpoint(run, "research:creation")
            if not creation_binds(run, creation, marker):
                return False
        elif marker is None:
            # Legacy recovery requires immutable original group evidence.
            return False
        return True
    except (ValueError, TypeError, KeyError, OSError):
        return False


def _checkpoint(run, identifier):
    from src.workflows.job_runtime import _digest
    history = json.loads(run.checkpoint_receipts_json)
    if type(history) is not list or any(type(item) is not dict for item in history):
        raise ValueError("research canonical checkpoint history is unavailable")
    records = [item for item in history
        if item.get("checkpoint_id") == identifier]
    if (len(records) != 1 or type(records[0].get("payload")) is not dict
        or records[0].get("safe") is not True
        or records[0].get("state_digest") != _digest(records[0]["payload"])):
        raise ValueError("research canonical checkpoint is unavailable")
    return records[0]["payload"]


def creation_binds(run, creation, marker):
    from src.workflows.job_runtime import _digest
    authority = json.loads(run.declared_authority_json)
    fields = {"schema_version", "board_task_id", "board_attempt_id", "creation_board_fence",
        "creation_job_fence", "parent_input_digest", "live_root_digest", "child_ids",
        "model_policy_digest", "no_learning", "creation_digest"}
    if marker == 2:
        fields |= {"parent_authority_digest", "parent_run_fingerprint", "research_authority_schema_version"}
        if (type(creation.get("research_authority_schema_version")) is not int
            or creation["research_authority_schema_version"] != 2
            or creation.get("parent_authority_digest") != run.authority_digest
            or creation.get("parent_run_fingerprint") != run.run_fingerprint):
            return False
    return (set(creation) == fields and type(creation.get("schema_version")) is int
        and creation["schema_version"] == (2 if marker == 2 else 1)
        and type(creation.get("board_task_id")) is str
        and type(creation.get("board_attempt_id")) is str
        and run.idempotency_key == f'{creation["board_task_id"]}:{creation["board_attempt_id"]}'
        and type(creation.get("creation_board_fence")) is int and creation["creation_board_fence"] > 0
        and type(creation.get("creation_job_fence")) is int and creation["creation_job_fence"] > 0
        and type(creation.get("child_ids")) is list and 1 <= len(creation["child_ids"]) <= 2
        and creation["child_ids"] == [f"{run.run_identity}:child:{slot}" for slot in range(len(creation["child_ids"]))]
        and creation.get("live_root_digest") == authority["live_root_digest"]
        and creation.get("model_policy_digest") == authority["model_policy_digest"]
        and creation.get("no_learning") is True
        and creation.get("parent_input_digest") == run.input_digest
        and creation.get("creation_digest") == _digest({k: v for k, v in creation.items() if k != "creation_digest"}))


async def original_group_binds(db, run, *, typed_inputs):
    """Read exact original child/accounting group without renewing any authority."""
    from datetime import timezone
    from src.workflows.job_runtime import DurableJobRepository, _digest
    from src.workflows.inference_accounting import _continuity_lock, InferenceAccountingError
    model = ResearchDossierInput.model_validate(typed_inputs)
    authority = json.loads(run.declared_authority_json)
    creation = _checkpoint(run, "research:creation")
    if (not creation_binds(run, creation, authority.get("research_authority_schema_version"))
        or len(creation["child_ids"]) != len(model.perspectives)):
        raise ValueError("research creation binding unavailable")
    rows = list((await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.parent_job_id == run.run_identity))).scalars())
    if sorted(row.run_identity for row in rows) != sorted(creation["child_ids"]):
        raise ValueError("research original child group unavailable")
    for slot, child_id in enumerate(creation["child_ids"]):
        row = next(row for row in rows if row.run_identity == child_id)
        expected = {**authority, "capability_id": "work.readonly-research-child.v1",
            "parent_creation_digest": creation["creation_digest"], "research_slot": slot,
            "parent_board_task_id": creation["board_task_id"], "parent_board_attempt_id": creation["board_attempt_id"],
            "creation_board_fence": creation["creation_board_fence"], "creation_job_fence": creation["creation_job_fence"]}
        child_inputs = {"parent_input_digest": run.input_digest, "research_slot": slot,
            "parent_creation_digest": creation["creation_digest"], "source_slots": model.perspectives[slot].source_slots,
            "source_manifest_digest": _digest([model.sources[index].model_dump(mode="json")
                for index in model.perspectives[slot].source_slots]),
            "source_permission_revision": authority["model_policy_revision"],
            "model_policy_revision": authority["model_policy_revision"],
            "slot_allowance_microusd": authority["research_slot_allowance_microusd"], "no_learning": True}
        if (json.loads(row.declared_authority_json) != expected or row.authority_digest != _digest(expected)
            or row.input_digest != _digest(child_inputs)
            or row.run_fingerprint != _digest({"input": child_inputs, "authority": expected})
            or row.job_kind != "readonly_research_child" or row.capability_version != "1"
            or row.parent_fencing_token != creation["creation_job_fence"] or row.branch_depth != 1
            or row.owner_principal_id != run.owner_principal_id or row.session_id != run.session_id
            or row.operator_session_id != run.operator_session_id or row.goal_id != run.goal_id
            or row.goal_revision != run.goal_revision or row.max_attempts != 1
            or row.deadline_at is None or row.deadline_at.replace(tzinfo=timezone.utc) > run.deadline_at.replace(tzinfo=timezone.utc)):
            raise ValueError("research original child identity changed")
    jobs = DurableJobRepository()
    account, costs = await jobs._accounting_rows(db)
    try:
        with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
            jobs._assert_accounting_continuity(workspace, account, costs)
    except InferenceAccountingError as exc:
        raise ValueError("research original accounting proof unavailable") from exc
    group = [cost for cost in costs if cost.job_id in creation["child_ids"]]
    if group:
        call_ids = ["remote:"+child for child in creation["child_ids"]]
        if len(group) != len(call_ids):
            raise ValueError("research original accounting group incomplete")
        for slot, child in enumerate(creation["child_ids"]):
            cost = next((cost for cost in group if cost.job_id == child), None)
            evidence = json.loads(cost.evidence_json) if cost is not None else []
            if type(evidence) is not list or any(type(item) is not dict for item in evidence):
                raise ValueError("research original accounting evidence unavailable")
            if (cost is None or cost.operation_id != call_ids[slot] or cost.owner_id != run.owner_principal_id
                or cost.policy_digest != creation["model_policy_digest"]
                or cost.goal_id != run.goal_id or cost.goal_revision != run.goal_revision
                or cost.runtime_path != "readonly_research_child"
                or cost.bound_microusd != authority["research_slot_allowance_microusd"]
                or cost.owner_ceiling_microusd != authority["research_owner_ceiling_microusd"]
                or not any(item.get("kind") == "research_group_reservation"
                    and item.get("creation_digest") == creation["creation_digest"]
                    and item.get("slot") == slot and item.get("group_call_ids") == call_ids
                    for item in evidence)):
                raise ValueError("research original accounting provenance changed")
    return True


async def verified_dossier(db, task, attempt, run):
    """Reopen exact files, child schemas, lineage and the existing cost witness."""
    if run is None or run.status != "succeeded" or not binds(task, attempt, run):
        raise ValueError("research completed root binding changed")
    return await _materialized_dossier(db, task, attempt, run)


async def _materialized_dossier(db, task, attempt, run):
    """Physical proof shared by completed readback and guarded terminal CAS."""
    from src.security.trust_contract import canonical_digest
    from src.work_board.dispatcher import _parse_typed_input
    from src.workflows.job_runtime import DurableJobRepository, _digest
    from src.workflows.inference_accounting import _continuity_lock
    inputs = ResearchDossierInput.model_validate(_parse_typed_input(task))
    await original_group_binds(db, run, typed_inputs=inputs)
    creation = _checkpoint(run, "research:creation")
    if (creation["board_task_id"] != task.task_id or creation["board_attempt_id"] != attempt.attempt_id
        or creation["parent_input_digest"] != run.input_digest
        or creation["creation_digest"] != _digest({key: value for key, value in creation.items() if key != "creation_digest"})
        or creation["child_ids"] != [f"{run.run_identity}:child:{slot}" for slot in range(len(inputs.perspectives))]):
        raise ValueError("research immutable creation proof changed")
    authority = json.loads(run.declared_authority_json)
    if not creation_binds(run, creation, authority.get("research_authority_schema_version")):
        raise ValueError("research original creation identity changed")
    jobs = DurableJobRepository()
    account, costs = await jobs._accounting_rows(db)
    with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
        jobs._assert_accounting_continuity(workspace, account, costs)
    children = []
    refs = []
    for slot, child_id in enumerate(creation["child_ids"]):
        child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == child_id))
        if (child is None or child.status != "succeeded" or child.job_kind != "readonly_research_child"
            or child.capability_version != "1" or child.parent_job_id != run.run_identity or child.branch_depth != 1
            or child.parent_fencing_token != creation["creation_job_fence"]
            or child.owner_principal_id != run.owner_principal_id or child.session_id != run.session_id
            or child.operator_session_id != run.operator_session_id or child.goal_id != run.goal_id
            or child.goal_revision != run.goal_revision):
            raise ValueError("research completed child lineage changed")
        child_authority = json.loads(child.declared_authority_json)
        expected_child_authority = {**authority, "capability_id": "work.readonly-research-child.v1",
            "parent_creation_digest": creation["creation_digest"], "research_slot": slot,
            "parent_board_task_id": task.task_id, "parent_board_attempt_id": attempt.attempt_id,
            "creation_board_fence": creation["creation_board_fence"], "creation_job_fence": creation["creation_job_fence"]}
        if child_authority != expected_child_authority or child.authority_digest != _digest(expected_child_authority):
            raise ValueError("research original child authority changed")
        ready = _checkpoint(child, "research:prompt-ready")
        output = _checkpoint(child, f"research:artifact:child:{slot}")
        from src.workflows.research_sources import canonical_sources_in_db
        from src.workflows.job_runtime import _serialize
        from src.work_board.research_artifacts import prompt_messages
        sources = await canonical_sources_in_db(jobs, db, _serialize(child), inputs, require_current_local=False)
        body = json.loads(read(ready["file_path"], ready["content_sha256"]))
        if (canonical_digest(body) != ready["payload_digest"]
            or body["messages"] != prompt_messages(inputs.question, inputs.perspectives[slot].instruction, sources)
            or body.get("stream") is not False or type(body.get("max_tokens")) is not int
            or not 1 <= body["max_tokens"] <= 1024
            or ready["slot"] != slot or ready["creation_digest"] != creation["creation_digest"]
            or output["job_id"] != child_id or output["creation_digest"] != creation["creation_digest"]
            or output["slot"] != slot or output["kind"] != "child" or output["no_learning"] is not True
            or not any(effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded"
                and effect.get("target_path") == output["file_path"]
                and effect.get("content_sha256") == output["content_sha256"]
                for effect in json.loads(child.effect_receipts_json))):
            raise ValueError("research exact provider body digest changed")
        row = next((cost for cost in costs if cost.operation_id == "remote:"+child_id), None)
        if (row is None or row.job_id != child_id or row.owner_id != run.owner_principal_id or row.state != "settled"
            or row.contact_started_at is None or row.actual_cost_microusd is None
            or row.payload_digest != ready["payload_digest"] or row.policy_digest != creation["model_policy_digest"]):
            raise ValueError("research actual child settlement readback changed")
        raw = read(output["file_path"], output["content_sha256"], max_bytes=16384)
        children.append(verified_child(raw, sources))
        refs.append({"child_id": child_id, "output_sha256": output["content_sha256"],
            "source_manifest_sha256": ready["source_manifest_sha256"], "payload_digest": ready["payload_digest"]})
    manifest = {"schema_version": 1, "parent_id": run.run_identity, "creation_digest": creation["creation_digest"],
        "children": refs, "adopted_claims": [{"slot": slot, "claim_index": index}
            for slot, child in enumerate(children) for index, claim in enumerate(child["claims"])
            if claim["evidence_status"] == "mechanically_verified"],
        "unverified_claims_adopted": False, "semantic_truth_verified": False, "no_learning": True}
    stored_manifest = _checkpoint(run, "research:artifact:manifest:0")
    dossier = _checkpoint(run, "research:artifact:dossier:0")
    if read(stored_manifest["file_path"], stored_manifest["content_sha256"]) != json_bytes(manifest):
        raise ValueError("research adoption manifest changed")
    raw = read(dossier["file_path"], dossier["content_sha256"])
    if raw != dossier_bytes(inputs.question, children):
        raise ValueError("research deterministic dossier readback changed")
    effects = json.loads(run.effect_receipts_json)
    if not any(item.get("receipt_kind") == "readback" and item.get("status") == "succeeded"
        and item.get("target_path") == dossier["file_path"] and item.get("content_sha256") == dossier["content_sha256"] for item in effects):
        raise ValueError("research dossier lacks its actual settled readback effect")
    return dossier, raw
