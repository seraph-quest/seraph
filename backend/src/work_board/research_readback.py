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


def binds(task, attempt, run):
    """Recompute fixed input, authority and fingerprint from admitted input."""
    from src.work_board.dispatcher import _parse_typed_input
    from src.workflows.job_runtime import _digest
    try:
        inputs = ResearchDossierInput.model_validate(_parse_typed_input(task))
        authority = json.loads(run.declared_authority_json)
        bound = authority["research_slot_allowance_microusd"]
        ceiling = authority["research_owner_ceiling_microusd"]
        if (task.capability_id != PARENT_CAPABILITY or run.job_kind != PARENT_KIND or run.capability_version != "1"
            or run.run_identity != job_id(task, attempt) or attempt.workflow_run_id != run.run_identity
            or run.owner_kind != "user" or run.service_id or run.owner_principal_id != task.owner_principal_id
            or run.session_id != task.owner_session_id or run.operator_session_id != task.owner_session_id
            or run.goal_id != task.goal_id or run.goal_revision != task.goal_revision
            or run.parent_job_id or run.parent_run_identity or run.branch_depth != 0
            or run.idempotency_scope != "work-board-attempt" or run.idempotency_key != f"{task.task_id}:{attempt.attempt_id}"
            or type(bound) is not int or bound <= 0 or type(ceiling) is not int or ceiling < bound*len(inputs.perspectives)
            or type(authority["model_policy_revision"]) is not int or authority["model_policy_revision"] <= 0
            or not re.fullmatch(r"[0-9a-f]{64}", authority["model_policy_digest"])):
            return False
        expected_authority = {"principal": task.owner_principal_id, "owner_kind": "user",
            "session_id": task.owner_session_id, "goal_id": task.goal_id, "goal_revision": task.goal_revision,
            "capability_id": PARENT_CAPABILITY, "capability_version": "1", "typed_input_digest": task.typed_input_digest,
            "input_artifact_id": task.input_artifact_id, "live_root_digest": digest(root_binding()),
            "model_policy_digest": authority["model_policy_digest"], "model_policy_revision": authority["model_policy_revision"],
            "source_egress_acknowledged": True, "research_allowance_microusd": bound*len(inputs.perspectives),
            "research_slot_allowance_microusd": bound, "research_owner_ceiling_microusd": ceiling,
            "permissions": ["workspace_read", "workspace_write", "model_inference", "public_https_text_read"],
            "limits": {"max_seconds": 300, "max_children": 2, "max_depth": 1, "max_sources": 4,
                "max_output_bytes": 65536, "max_attempts": 1}, "no_learning": True}
        expected_inputs = {"typed_input_digest": task.typed_input_digest, "input_artifact_id": task.input_artifact_id,
            "child_count": len(inputs.perspectives), "source_count": len(inputs.sources), "no_learning": True}
        return (authority == expected_authority and run.input_digest == _digest(expected_inputs)
            and run.authority_digest == _digest(expected_authority)
            and run.run_fingerprint == digest({"task_ref": task.task_id, "attempt_ref": attempt.attempt_id,
                "inputs": expected_inputs, "authority": expected_authority}))
    except (ValueError, TypeError, KeyError, OSError):
        return False


def _checkpoint(run, identifier):
    records = [item.get("payload") for item in json.loads(run.checkpoint_receipts_json)
        if item.get("checkpoint_id") == identifier]
    if len(records) != 1 or not isinstance(records[0], dict):
        raise ValueError("research canonical checkpoint is unavailable")
    return records[0]


async def verified_dossier(db, task, attempt, run):
    """Reopen exact files, child schemas, lineage and the existing cost witness."""
    from src.security.trust_contract import canonical_digest
    from src.work_board.dispatcher import _parse_typed_input
    from src.workflows.job_runtime import DurableJobRepository, _digest
    from src.workflows.inference_accounting import _continuity_lock
    if run is None or run.status != "succeeded" or not binds(task, attempt, run):
        raise ValueError("research completed root binding changed")
    inputs = ResearchDossierInput.model_validate(_parse_typed_input(task))
    creation = _checkpoint(run, "research:creation")
    if (creation["board_task_id"] != task.task_id or creation["board_attempt_id"] != attempt.attempt_id
        or creation["parent_input_digest"] != run.input_digest
        or creation["creation_digest"] != _digest({key: value for key, value in creation.items() if key != "creation_digest"})
        or creation["child_ids"] != [f"{run.run_identity}:child:{slot}" for slot in range(len(inputs.perspectives))]):
        raise ValueError("research immutable creation proof changed")
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
        ready = _checkpoint(child, "research:prompt-ready")
        output = _checkpoint(child, f"research:artifact:child:{slot}")
        sources = json.loads(read(ready["source_manifest_path"], ready["source_manifest_sha256"]))
        body = json.loads(read(ready["file_path"], ready["content_sha256"]))
        if canonical_digest(body) != ready["payload_digest"]:
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
