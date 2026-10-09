"""Fixed source reads under the original authenticated parent authority."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select

from config.settings import settings
from src.db.models import WorkBoardAttempt, WorkBoardStatus, WorkBoardTask
from src.work_board.contracts import WorkBoardOwner
from src.work_board.research_artifacts import read, normalized_source, write_verified
from src.work_board.research_contracts import PARENT_KIND, ResearchDossierInput, SOURCE_BYTES
from src.workflows.research_native import checkpoint


async def current_inputs_in_db(jobs, db, parent_id):
    """Current native authority in the caller's canonical writer transaction."""
    from src.auth.service import authenticate_principal
    from src.model_fabric.effective_policy import current_inference_policy
    from src.work_board.input_artifacts import resolve_input_artifact_for_task
    from src.work_board.pipelines import root_binding
    from src.work_board.pipeline_contracts import digest
    from src.workflows.job_runtime import _assert_canonical_goal_fence
    from src.workflows.research_guard import assert_research_operator_session
    parent = await jobs._fetch(db, parent_id)
    authority = json.loads(parent.declared_authority_json)
    await assert_research_operator_session(db, parent, now=datetime.now(timezone.utc))
    await authenticate_principal(parent.owner_principal_id, db=db)
    await _assert_canonical_goal_fence(db, goal_id=parent.goal_id, goal_revision=parent.goal_revision,
        owner_kind=parent.owner_kind, owner_principal_id=parent.owner_principal_id,
        session_id=parent.session_id, authority=parent.declared_authority_json)
    if (parent.job_kind != PARENT_KIND or parent.status not in {"running", "paused"}
        or parent.deadline_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
        or authority.get("live_root_digest") != digest(root_binding())
        or authority.get("model_policy_digest") != current_inference_policy()[1]
        or authority.get("source_egress_acknowledged") is not True):
        raise ValueError("original research Root/Goal/source-egress authority changed")
    attempts = list((await db.scalars(select(WorkBoardAttempt).where(WorkBoardAttempt.workflow_run_id == parent_id))).all())
    attempt = attempts[0] if len(attempts) == 1 else None
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == attempt.task_id)) if attempt else None
    latest = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id)
        .order_by(WorkBoardAttempt.created_at.desc(), WorkBoardAttempt.attempt_id.desc()).limit(1)) if task else None
    if (task is None or latest is None or latest.attempt_id != attempt.attempt_id or attempt.ended_at or attempt.cancel_requested_at
        or task.status not in {WorkBoardStatus.running, WorkBoardStatus.blocked}
        or task.goal_id != parent.goal_id or task.goal_revision != parent.goal_revision
        or task.capability_id != "work.research-dossier.v1"
        or task.owner_principal_id != parent.owner_principal_id or task.owner_session_id != parent.session_id
        or task.typed_input_digest != authority.get("typed_input_digest")
        or task.input_artifact_id != authority.get("input_artifact_id")):
        raise ValueError("current research Board input/attempt changed")
    resolved = await resolve_input_artifact_for_task(db,
        WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id),
        artifact_id=task.input_artifact_id, goal_id=task.goal_id, goal_revision=task.goal_revision,
        capability_id=task.capability_id, expected_task_id=task.task_id)
    if resolved.row.payload_sha256 != task.typed_input_digest:
        raise ValueError("research physical input envelope differs from original admission")
    return ResearchDossierInput.model_validate(resolved.input)


async def current_inputs(jobs, parent_id):
    from src.auth.service import authenticate_session
    parent = await jobs.get_job(parent_id)
    operator = await authenticate_session(parent["operator_session_id"], touch=False)
    if operator.principal.principal_id != parent["owner"]["principal_id"] or operator.session_id != parent["session_id"]:
        raise ValueError("research requires its exact original active operator Root session")
    async with jobs._session() as db:
        return await current_inputs_in_db(jobs, db, parent_id)


async def _completed_local_source_in_db(jobs, db, *, parent_id, source):
    from src.work_board.input_artifacts import _safe_file_bytes, _open_input_artifact_parent
    from src.work_board.review import _verified_workflow_readback
    from src.workspace import canonical_workspace_root
    parent = await jobs._fetch(db, parent_id)
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == source.producer_task_ref))
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == source.producer_attempt_ref))
    if (task is None or attempt is None or attempt.task_id != task.task_id
        or task.owner_principal_id != parent.owner_principal_id or task.owner_session_id != parent.session_id
        or task.status != WorkBoardStatus.done or attempt.ended_at is None):
        raise ValueError("local source requires the current owner's verified completed task")
    proof = await _verified_workflow_readback(db, task, attempt)
    if not proof or proof.get("content_sha256") != source.source_sha256:
        raise ValueError("local source independent readback digest changed")
    reference = str(proof.get("target_path") or "")
    if not reference.startswith("artifacts/work-board/") or ".." in reference.split("/"):
        raise ValueError("local source is outside the fixed Board artifact directory")
    path = canonical_workspace_root(settings.workspace_dir)/reference
    # A completed label or caller path never supplies the physical source.
    import os
    parent_fd, leaf = _open_input_artifact_parent(path, create=False)
    try:
        descriptor = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        try:
            size = os.fstat(descriptor).st_size
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_fd)
    if not 0 < size <= SOURCE_BYTES:
        raise ValueError("selected local source exceeds 64 KiB")
    return _safe_file_bytes(path, expected_digest=source.source_sha256, expected_size=size)


async def _completed_local_source(jobs, *, parent_id, source):
    async with jobs._session() as db:
        return await _completed_local_source_in_db(jobs, db, parent_id=parent_id, source=source)


async def canonical_sources_in_db(jobs, db, child, inputs, *, require_current_local=True):
    """Recompute quotes from the original producer's physical source output."""
    from src.workflows.job_runtime import _serialize
    parent = await jobs._fetch(db, child["parent_job_id"])
    creation = checkpoint(_serialize(parent), "research:creation")
    authority = child["declared_authority"]
    slot = authority["research_slot"]
    if (child["job_kind"] != "readonly_research_child" or creation is None
        or slot not in range(len(inputs.perspectives)) or creation["child_ids"][slot] != child["job_id"]
        or child["root_run_identity"] != parent.root_run_identity
        or authority["parent_creation_digest"] != creation["creation_digest"]
        or child["parent_fencing_token"] != creation["creation_job_fence"]):
        raise ValueError("research quoted-source immutable lineage changed")
    sources = []
    for source_slot in inputs.perspectives[slot].source_slots:
        selected = inputs.sources[source_slot]
        producer_slot = min(index for index, perspective in enumerate(inputs.perspectives) if source_slot in perspective.source_slots)
        producer = await jobs._fetch(db, creation["child_ids"][producer_slot])
        binding = checkpoint(_serialize(producer), f"research:artifact:source:{source_slot}")
        if (binding is None or binding["job_id"] != producer.run_identity
            or binding["creation_digest"] != creation["creation_digest"] or binding["slot"] != source_slot
            or binding["kind"] != "source" or binding["no_learning"] is not True
            or not any(effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded"
                and effect.get("target_path") == binding["file_path"]
                and effect.get("content_sha256") == binding["content_sha256"]
                for effect in json.loads(producer.effect_receipts_json))):
            raise ValueError("research quoted source lacks its exact settled physical readback")
        raw = read(binding["file_path"], binding["content_sha256"], max_bytes=SOURCE_BYTES)
        if len(raw) != binding["byte_count"]:
            raise ValueError("research source output size changed")
        if require_current_local and selected.kind == "completed_board_artifact":
            original = await _completed_local_source_in_db(jobs, db, parent_id=parent.run_identity, source=selected)
            if original != raw:
                raise ValueError("research completed source permission or physical bytes changed")
        sources.append(normalized_source(raw, source_slot=source_slot, first_line=selected.first_line, last_line=selected.last_line))
    ready = checkpoint(child, "research:prompt-ready")
    if ready is None or json.loads(read(ready["source_manifest_path"], ready["source_manifest_sha256"])) != sources:
        raise ValueError("research source manifest differs from the actual admitted spans")
    return sources


async def verify_current_prompt_in_db(jobs, db, child):
    from src.model_fabric.contracts import finalized_openai_compatible_body
    from src.security.trust_contract import canonical_digest
    from src.work_board.research_artifacts import prompt_messages
    from src.workflows.research_provider import _target
    inputs = await current_inputs_in_db(jobs, db, child["parent_job_id"])
    sources = await canonical_sources_in_db(jobs, db, child, inputs)
    ready = checkpoint(child, "research:prompt-ready")
    setup, policy_digest, target = _target()
    expected = finalized_openai_compatible_body(model_id=target["model_id"],
        messages=prompt_messages(inputs.question, inputs.perspectives[ready["slot"]].instruction, sources),
        options=target["options"], temperature=setup.temperature,
        max_tokens=min(1024, setup.max_output_tokens), stream=False)
    body = json.loads(read(ready["file_path"], ready["content_sha256"]))
    if (body != expected or canonical_digest(body) != ready["payload_digest"]
        or ready["policy_digest"] != policy_digest or ready["creation_digest"] != child["declared_authority"]["parent_creation_digest"]
        or datetime.now(timezone.utc).timestamp() >= ready["contact_deadline_at"]):
        raise ValueError("research original prompt, policy or contact deadline changed")
    return body, sources


async def acquire_source(jobs, *, child_id, owner, fence, source_slot, transport=None):
    """One durable GET intent or exact completed artifact, never a reread."""
    child = await jobs.get_job(child_id)
    parent_id = child["parent_job_id"]
    inputs = await current_inputs(jobs, parent_id)
    source = inputs.sources[source_slot]
    producer_slot = min(slot for slot, perspective in enumerate(inputs.perspectives) if source_slot in perspective.source_slots)
    parent = await jobs.get_job(parent_id)
    creation = checkpoint(parent, "research:creation")
    producer_id = creation["child_ids"][producer_slot]
    producer = await jobs.get_job(producer_id)
    existing = checkpoint(producer, f"research:artifact:source:{source_slot}")
    if existing:
        raw = read(existing["file_path"], existing["content_sha256"], max_bytes=SOURCE_BYTES)
        return normalized_source(raw, source_slot=source_slot, first_line=source.first_line, last_line=source.last_line), existing
    if producer_id != child_id:
        return None
    identifier = f"research:source-intent:{source_slot}"
    if checkpoint(child, identifier):
        raise ValueError("source contact may have happened; exact verified output is required, never a second GET")
    intent = {"creation_digest":creation["creation_digest"], "source_slot":source_slot,
        "selection_digest":hashlib.sha256(json.dumps(source.model_dump(),sort_keys=True).encode()).hexdigest(),
        "kind":source.kind,"no_learning":True}
    await jobs.record_checkpoint(child_id, checkpoint_id=identifier, state=intent,
        checkpoint_payload=intent, owner=owner, fencing_token=fence)
    if source.kind == "completed_board_artifact":
        raw = await _completed_local_source(jobs, parent_id=parent_id, source=source)
    else:
        from src.browser.pinned_transport import PinnedBrowserRequest, PinnedBrowserTransport, parse_public_https_url
        parsed = parse_public_https_url(source.url)
        selected = transport or PinnedBrowserTransport(timeout_seconds=20, max_response_bytes=SOURCE_BYTES)
        try:
            remaining = min(20, datetime.fromisoformat(child["deadline_at"]).replace(tzinfo=timezone.utc).timestamp()-datetime.now(timezone.utc).timestamp())
            if remaining <= 0:
                raise TimeoutError("original source deadline expired")
            import asyncio
            async with asyncio.timeout(remaining):
                response = await selected.resolve_and_fetch(PinnedBrowserRequest(url=source.url, method="GET",
                    headers={"accept":"text/plain", "accept-encoding":"identity"},resource_type="document",
                    is_navigation=True,redirect_count=0),allowed_hosts=[parsed.hostname],approved_url_prefixes=[source.url])
            content_type = response.headers.get("content-type", "").lower().replace(" ", "")
            if (response.request_url != source.url or response.status_code != 200 or response.redirect_location or content_type not in {
                "text/plain", "text/plain;charset=utf-8", "text/plain;charset=us-ascii"}):
                raise ValueError("source must be exact nonredirected UTF-8 text/plain")
            raw = response.content
        finally:
            selected.cancel_pending_blocking()
    normalized = normalized_source(raw,source_slot=source_slot,first_line=source.first_line,last_line=source.last_line)
    await current_inputs(jobs,parent_id)
    binding = await write_verified(jobs,job_id=child_id,owner=owner,fence=fence,
        creation_digest=creation["creation_digest"],slot=source_slot,kind="source",content=raw,max_bytes=SOURCE_BYTES)
    return normalized,binding


from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoveryInputWitness:
    job_id: str
    input_digest: str
    authority_digest: str
    checkpoint_digest: str
    artifact_digest: str
    plan: object
    public_brief: str
    artifacts: dict


def discovery_stage_inputs(plan, artifacts, stage_id):
    """Resolve only declared whole-output refs to their exact physical owners."""
    from src.guardian.research_plan_contracts import ArtifactRef
    step = next(item for item in plan.steps if item.step_id == stage_id)
    resolved = []
    for ref in step.input_refs:
        if isinstance(ref, ArtifactRef):
            candidates = [item for item in artifacts.values()
                if item["reference"] == ref and item["kind"] == "public_brief"]
        else:
            producer = next(item for item in plan.steps if item.step_id == ref.producer_step_id)
            declared = next(item for item in producer.output_slots if item.slot == ref.output_slot)
            candidates = [item for item in artifacts.values()
                if item["kind"] == declared.slot and item.get("slot") == 0]
        if len(candidates) != 1:
            raise ValueError("programme declared stage input has no unique original physical output")
        resolved.append(candidates[0])
    return resolved


def is_local_unsupported_discovery_brief(value, artifacts, effects):
    """A no-contact negative receipt does not claim executed upstream stages."""
    return (type(value) is dict and value.get("findings") == [] and value.get("citations") == []
        and value.get("prepared_artifact_refs") == [] and value.get("proposed_next_steps") == []
        and value.get("coverage", {}).get("status") == "unsupported"
        and value["coverage"].get("outcome_state") == "empty"
        and value["coverage"].get("sources") == [] and value["coverage"].get("source_spans") == []
        and value["coverage"].get("no_learning") is True
        and not any(item["kind"] in {"queries", "manifest", "selection", "snapshot", "snapshots", "child"} for item in artifacts.values())
        and not any(item.get("effect_type") in {"public_https_read", "remote_inference_admission"} for item in effects))


def compile_discovery_search_derivation(plan, artifacts, effects, manifest, reference, *, job_id):
    """Compile protected derivation from original physical query/effect owners."""
    import re
    from src.guardian.discovery_search import SEARCH_URL
    queries = discovery_stage_inputs(plan, artifacts, "search_public")[0]["parsed"]["queries"]
    query_digest = hashlib.sha256("\n".join(queries).encode()).hexdigest()
    if manifest.query_digest != query_digest or manifest.run_id != plan.plan_id or len(manifest.results) > plan.limits.max_results:
        raise ValueError("programme manifest derivation differs from original query outputs")
    responses = []
    for index, query in enumerate(queries):
        original = [item for item in effects if item.get("receipt_kind") == "readback"
            and item.get("effect_type") == "public_https_read" and item.get("status") == "succeeded"
            and item.get("details", {}).get("search_response_receipt", {}).get("query_index") == index]
        if len(original) != 1:
            raise ValueError("programme manifest lacks its exact original HTTP response receipts")
        record = original[0]
        receipt = record.get("details", {}).get("search_response_receipt")
        if (type(receipt) is not dict or set(receipt) != {"query_index", "query_digest", "response_digest", "byte_count"}
                or type(receipt["query_index"]) is not int or receipt["query_index"] != index
                or receipt["query_digest"] != hashlib.sha256(query.encode()).hexdigest()
                or type(receipt["byte_count"]) is not int or not 0 <= receipt["byte_count"] <= plan.limits.max_search_bytes
                or not isinstance(receipt["response_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", receipt["response_digest"])
                or record.get("content_sha256") != receipt["response_digest"]
                or record.get("target_path") != SEARCH_URL
                or record.get("target_digest") != hashlib.sha256(SEARCH_URL.encode()).hexdigest()
                or record.get("effect_id") != f"discovery-search:{job_id}:{index}"):
            raise ValueError("programme manifest lacks its exact original HTTP response receipts")
        responses.append(dict(receipt))
    return {"manifest_ref": reference.model_dump(mode="json"), "query_digest": query_digest, "responses": responses}


def _discovery_original_rows(raw_workflow_row):
    """Decode original finite provenance, without current lifecycle checks."""
    from src.work_board.research_parent import discovery_authority, DISCOVERY_KIND, DISCOVERY_SERVICE
    from src.guardian.research_plan_contracts import ArtifactRef
    from src.workflows.job_runtime import _digest
    if type(raw_workflow_row) is not dict or raw_workflow_row.get("job_kind") != DISCOVERY_KIND:
        raise ValueError("programme physical inputs require native lineage")
    authority = discovery_authority(raw_workflow_row["declared_authority_json"])
    job_id = raw_workflow_row["run_identity"]
    if (job_id != authority.original_job_id or raw_workflow_row.get("owner_principal_id") != DISCOVERY_SERVICE
            or raw_workflow_row.get("owner_kind") != "service"
            or raw_workflow_row.get("service_id") != DISCOVERY_SERVICE
            or raw_workflow_row.get("goal_id") != authority.programme_binding.goal_id
            or type(raw_workflow_row.get("goal_revision")) is not int
            or raw_workflow_row["goal_revision"] != authority.programme_binding.goal_revision
            or raw_workflow_row.get("operator_session_id") is not None
            or raw_workflow_row.get("session_id") is not None):
        raise ValueError("programme original service or job binding changed")
    inputs = json.loads(raw_workflow_row["arguments_json"])
    history = json.loads(raw_workflow_row["checkpoint_receipts_json"])
    artifacts = json.loads(raw_workflow_row["artifact_receipts_json"])
    effects = json.loads(raw_workflow_row["effect_receipts_json"])
    if (_digest(inputs) != raw_workflow_row["input_digest"]
            or _digest(json.loads(raw_workflow_row["declared_authority_json"])) != raw_workflow_row["authority_digest"]
            or type(inputs) is not dict
            or set(inputs) != {"plan_ref", "plan_file_path", "public_brief_ref", "public_brief_file_path", "no_learning"}
            or inputs["no_learning"] is not True
            or any(type(rows) is not list or any(type(item) is not dict for item in rows) for rows in (history, artifacts, effects))):
        raise ValueError("programme original input or receipt provenance changed")
    if (ArtifactRef.model_validate(inputs["plan_ref"]) != authority.plan_ref
            or ArtifactRef.model_validate(inputs["public_brief_ref"]).digest != authority.programme_binding.brief_digest):
        raise ValueError("programme physical input ref changed")
    return job_id, authority, inputs, history, artifacts, effects


def discovery_physical_records(raw_workflow_row):
    """Pure original adoption checks over exact retained provenance records."""
    from src.work_board.research_parent import DISCOVERY_KIND
    from src.work_board.research_artifacts import DISCOVERY_ARTIFACT_LIMITS, discovery_prefix, json_bytes, sha
    from src.guardian.research_plan_contracts import ArtifactRef
    from src.artifacts.registry import artifact_id_for
    job_id, authority, inputs, history, artifacts, effects = _discovery_original_rows(raw_workflow_row)
    programme_id = authority.programme_binding.programme_id
    selected = []
    slots = set()
    for entry in history:
        if not str(entry.get("checkpoint_id", "")).startswith("discovery:artifact:"):
            continue
        payload = entry.get("payload")
        if (type(payload) is not dict or payload.get("job_id") != job_id
                or payload.get("programme_id") != programme_id or payload.get("no_learning") is not True):
            raise ValueError("programme artifact checkpoint lineage changed")
        kind, slot = payload.get("kind"), payload.get("slot")
        if (type(kind) is not str or kind not in DISCOVERY_ARTIFACT_LIMITS or type(slot) is not int or not 0 <= slot < 4
                or entry["checkpoint_id"] != f"discovery:artifact:{kind}:{slot}"
                or (kind, slot) in slots
                or (kind in {"plan", "public_brief", "queries", "manifest", "selection", "snapshots", "brief"} and slot != 0)):
            raise ValueError("programme artifact shape or slot changed")
        slots.add((kind, slot))
        reference = ArtifactRef.model_validate(payload["artifact_ref"])
        path = payload["file_path"]
        expected_path = f"{discovery_prefix(programme_id)}{sha(json_bytes([job_id, kind, slot]))}-{reference.digest}.json"
        identifier = artifact_id_for(file_path=path, artifact_type="goal_discovery_" + kind,
            producer=DISCOVERY_KIND, run_id=job_id, content_sha256=reference.digest)
        size = payload.get("byte_count")
        records = [item for item in artifacts if item.get("artifact_id") == reference.artifact_id]
        readbacks = [item for item in effects if item.get("effect_id") == "discovery-artifact:" + reference.artifact_id]
        if (path != expected_path or reference.artifact_id != identifier or type(size) is not int
                or not 0 < size <= DISCOVERY_ARTIFACT_LIMITS[kind] or len(records) != 1 or len(readbacks) != 1):
            raise ValueError("programme physical artifact has no exact original adoption")
        record, effect = records[0], readbacks[0]
        if (record.get("file_path") != path or record.get("content_sha256") != reference.digest
                or record.get("size_bytes") != size or type(record.get("size_bytes")) is not int
                or record.get("producer") != DISCOVERY_KIND or record.get("artifact_type") != "goal_discovery_" + kind
                or record.get("exists") is not True or effect.get("receipt_kind") != "readback"
                or effect.get("effect_type") != "research_artifact_readback" or effect.get("status") != "succeeded"
                or effect.get("target_path") != path or effect.get("target_digest") != reference.digest
                or effect.get("content_sha256") != reference.digest
                or effect.get("readback_id") != "discovery-readback-" + reference.artifact_id
                or type(effect.get("details")) is not dict
                or effect["details"].get("verified") is not True
                or effect.get("details", {}).get("no_learning") is not True):
            raise ValueError("programme physical artifact readback provenance changed")
        selected.append({"checkpoint_id": entry["checkpoint_id"], "payload": payload,
            "artifact": record, "effect": effect})
    return tuple(selected)


def discovery_physical_references(raw_workflow_row):
    """Return each original read appearance, including repeated initial refs."""
    from src.guardian.research_plan_contracts import ArtifactRef
    _, _, inputs, _, _, _ = _discovery_original_rows(raw_workflow_row)
    selected = [(item["payload"]["file_path"], item["artifact"]["content_sha256"],
        item["payload"]["byte_count"], item["payload"]["kind"])
        for item in discovery_physical_records(raw_workflow_row)]
    initial = []
    for kind in ("plan", "public_brief"):
        reference = ArtifactRef.model_validate(inputs[kind + "_ref"])
        matches = [item for item in selected if item[0] == inputs[kind + "_file_path"] and item[1] == reference.digest and item[3] == kind]
        if len(matches) != 1:
            raise ValueError("programme first physical input lacks original adoption")
        initial.append(matches[0])
    return tuple(initial + selected)


def physical_discovery_closure(raw_workflow_row, *, root, header_budget):
    """Original retained owner checks every appearance on its actual root."""
    from src.work_board.research_artifacts import read_discovery, DISCOVERY_ARTIFACT_LIMITS
    from src.memory.header_bounds import HeaderReadBudget
    if type(header_budget) is not HeaderReadBudget:
        raise ValueError("programme physical closure requires its original budget")
    appearances = discovery_physical_references(raw_workflow_row)
    authority = _discovery_original_rows(raw_workflow_row)[1]
    contents = {}
    for path, digest, size, kind in appearances:
        contents[path] = read_discovery(path, digest, programme_id=authority.programme_binding.programme_id,
            max_bytes=DISCOVERY_ARTIFACT_LIMITS[kind], root=root, expected_size=size, header_budget=header_budget)
    validate_discovery_physical_provenance(raw_workflow_row, contents, historical=True)
    return appearances


async def physical_discovery_inputs(jobs, job_id, *, completed_read=False, stage_id=None):
    """Reopen immutable programme inputs BEFORE a short native writer."""
    from src.work_board.research_parent import DISCOVERY_KIND
    from src.work_board.research_artifacts import read_discovery, DISCOVERY_ARTIFACT_LIMITS
    from src.guardian.research_plan_contracts import validate_goal_research_plan, ArtifactRef
    from src.workflows.job_runtime import _utc_now
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        if run.job_kind != DISCOVERY_KIND:
            raise ValueError("programme physical inputs require native lineage")
        if completed_read and run.status not in {"succeeded", "degraded"}:
            raise ValueError("programme completed readback requires its original positive terminal job")
        raw_row = run.model_dump(mode="python")
    _, authority, inputs, history, _, _ = _discovery_original_rows(raw_row)
    discovery_physical_references(raw_row)
    binding = authority.programme_binding
    contents = {}
    raw_plan = read_discovery(inputs["plan_file_path"], ArtifactRef.model_validate(inputs["plan_ref"]).digest,
        programme_id=binding.programme_id, max_bytes=65536)
    contents[inputs["plan_file_path"]] = raw_plan
    plan_payload = json.loads(raw_plan)
    observed = datetime.fromisoformat(plan_payload["issued_at"].replace("Z", "+00:00")) if completed_read else _utc_now()
    plan = validate_goal_research_plan(plan_payload, now=observed)
    from src.work_board.dispatcher import _dispatcher
    current_service = _dispatcher.goal_discovery
    if current_service is not None:
        async with jobs._session() as db:
            await current_service._validate_pinned_strategy(binding, plan.strategy_binding, db=db)
    elif plan.strategy_binding.status != "none":
        raise ValueError("programme active strategy requires its current native lifecycle owner")
    contents[inputs["public_brief_file_path"]] = read_discovery(inputs["public_brief_file_path"],
        ArtifactRef.model_validate(inputs["public_brief_ref"]).digest, programme_id=binding.programme_id, max_bytes=8000)
    for entry in history:
        if str(entry.get("checkpoint_id", "")).startswith("discovery:artifact:"):
            payload = entry["payload"]
            reference = ArtifactRef.model_validate(payload["artifact_ref"])
            contents[payload["file_path"]] = read_discovery(payload["file_path"], reference.digest,
                programme_id=binding.programme_id, max_bytes=DISCOVERY_ARTIFACT_LIMITS[payload["kind"]])
    return validate_discovery_physical_provenance(raw_row, contents, historical=completed_read, stage_id=stage_id)


def validate_discovery_physical_provenance(raw_workflow_row, contents, *, historical=False, stage_id=None):
    """Pure original plan/output derivation over already staged exact bytes.

    This returns data, never a live Source or current execution authority.
    The original owner supplies fully charged retained rows and verifies the
    canonical Goal/issuer/generation independently.
    """
    from src.work_board.research_parent import DISCOVERY_KIND
    from src.work_board.research_artifacts import DISCOVERY_ARTIFACT_LIMITS
    from src.guardian.research_plan_contracts import validate_goal_research_plan, ArtifactRef, SearchManifestV1, SourceSelectionV1, PublicSnapshotV1, DiscoveryBriefV1
    from src.artifacts.registry import artifact_id_for
    from src.workflows.job_runtime import _digest, _utc_now
    job_id, authority, inputs, history, canonical_artifacts, effects = _discovery_original_rows(raw_workflow_row)
    for path, digest, size, _ in discovery_physical_references(raw_workflow_row):
        content = contents[path]
        if type(content) is not bytes or len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("programme staged original physical bytes changed")
    input_digest, authority_digest = raw_workflow_row["input_digest"], raw_workflow_row["authority_digest"]
    deadline = raw_workflow_row["deadline_at"]
    if isinstance(deadline, str):
        deadline = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    deadline = deadline.replace(tzinfo=timezone.utc)
    binding = authority.programme_binding
    if _digest(inputs) != input_digest or _digest(json.loads(raw_workflow_row["declared_authority_json"])) != authority_digest:
        raise ValueError("programme original input or authority digest changed")
    if set(inputs) != {"plan_ref", "plan_file_path", "public_brief_ref", "public_brief_file_path", "no_learning"} or inputs["no_learning"] is not True:
        raise ValueError("programme input shape is not native and closed")
    plan_ref = ArtifactRef.model_validate(inputs["plan_ref"])
    brief_ref = ArtifactRef.model_validate(inputs["public_brief_ref"])
    if plan_ref != authority.plan_ref or brief_ref.digest != binding.brief_digest:
        raise ValueError("programme physical input ref changed")
    raw_plan = contents[inputs["plan_file_path"]]
    plan_payload = json.loads(raw_plan)
    observed = datetime.fromisoformat(plan_payload["issued_at"].replace("Z", "+00:00")) if historical else _utc_now()
    plan = validate_goal_research_plan(plan_payload, now=observed)
    if (plan.programme_id.hex != binding.programme_id or plan.goal_id != binding.goal_id
            or plan.programme_revision != binding.grant_revision or plan.goal_revision != binding.goal_revision
            or plan.grant_id != binding.programme_id or plan.grant_revision != binding.grant_revision
            or plan.public_brief_digest != binding.brief_digest or plan.route_epoch != binding.route_epoch
            or plan.deadline_at != deadline or plan.limits.cost_limit_microusd > binding.cost_ceiling_microusd
            or plan.steps[0].input_refs != [brief_ref]):
        raise ValueError("programme plan aliases, original deadline or first input changed")
    raw_brief = contents[inputs["public_brief_file_path"]]
    public_brief = raw_brief.decode("utf-8", errors="strict")
    artifacts = {brief_ref.artifact_id: {"reference": brief_ref, "content": raw_brief, "kind": "public_brief"},
        plan_ref.artifact_id: {"reference": plan_ref, "content": raw_plan, "kind": "plan"}}
    for kind, reference, path in [("plan", plan_ref, inputs["plan_file_path"]), ("public_brief", brief_ref, inputs["public_brief_file_path"])]:
        expected = artifact_id_for(file_path=path, artifact_type="goal_discovery_" + kind,
            producer=DISCOVERY_KIND, run_id=job_id, content_sha256=reference.digest)
        if reference.artifact_id != expected:
            raise ValueError("programme first physical input is not owned by the original job")
    adopted_slots = set()
    for entry in history:
        if not str(entry.get("checkpoint_id", "")).startswith("discovery:artifact:"):
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict) or payload.get("job_id") != job_id or payload.get("programme_id") != binding.programme_id or payload.get("no_learning") is not True:
            raise ValueError("programme artifact checkpoint lineage changed")
        kind, slot = payload["kind"], payload["slot"]
        if kind not in DISCOVERY_ARTIFACT_LIMITS or type(slot) is not int or not 0 <= slot < 4:
            raise ValueError("programme artifact shape changed")
        if (kind, slot) in adopted_slots or (kind in {"queries", "manifest", "selection", "snapshots", "brief"} and slot != 0):
            raise ValueError("programme physical output slot is ambiguous")
        adopted_slots.add((kind, slot))
        reference = ArtifactRef.model_validate(payload["artifact_ref"])
        expected = artifact_id_for(file_path=payload["file_path"], artifact_type="goal_discovery_" + kind,
            producer=DISCOVERY_KIND, run_id=job_id, content_sha256=reference.digest)
        if reference.artifact_id != expected:
            raise ValueError("programme physical stage output belongs to another original job")
        records = [a for a in canonical_artifacts if a.get("artifact_id") == reference.artifact_id]
        if (len(records) != 1 or records[0].get("file_path") != payload["file_path"] or records[0].get("content_sha256") != reference.digest
                or records[0].get("producer") != DISCOVERY_KIND or records[0].get("artifact_type") != "goal_discovery_" + kind):
            raise ValueError("programme physical input lacks canonical artifact adoption")
        content = contents[payload["file_path"]]
        if len(content) != payload["byte_count"]:
            raise ValueError("programme adopted input byte count changed")
        output_slot = next((output for step in plan.steps for output in step.output_slots if output.slot == kind), None)
        if (output_slot is not None and len(content) > min(output_slot.max_bytes, plan.limits.max_output_bytes)
                or kind in {"snapshot", "draft"} and len(content) > plan.limits.max_output_bytes):
            raise ValueError("programme physical output exceeds its declared byte allowance")
        parsed = json.loads(content) if kind != "public_brief" else None
        if kind == "queries":
            if (type(parsed) is not dict or set(parsed) != {"queries"} or type(parsed["queries"]) is not list
                    or not 1 <= len(parsed["queries"]) <= plan.limits.max_queries
                    or len(set(parsed["queries"])) != len(parsed["queries"])
                    or any(type(q) is not str or not q.strip() or len(q.encode()) > 2048 or any(ord(c) < 32 for c in q) for q in parsed["queries"])):
                raise ValueError("programme physical query plan exceeds its original allowance")
        elif kind == "manifest":
            parsed = SearchManifestV1.model_validate(parsed)
            if parsed.run_id != plan.plan_id or len(parsed.results) > plan.limits.max_results:
                raise ValueError("programme search manifest run or cap changed")
        elif kind == "selection":
            parsed = SourceSelectionV1.model_validate(parsed)
            if len(parsed.selected_result_ids) > plan.limits.max_sources:
                raise ValueError("programme selected source count exceeds its original allowance")
        elif kind == "snapshot":
            parsed = PublicSnapshotV1.model_validate(parsed)
        artifacts[reference.artifact_id] = {"reference": reference, "content": content, "parsed": parsed, "kind": kind, "slot": slot,
            "search_derivation": payload.get("search_derivation")}
    for kind, producer in {"queries": "plan_queries", "manifest": "search_public", "selection": "search_public",
            "snapshot": "extract_sources", "snapshots": "extract_sources", "brief": "prepare_brief"}.items():
        if any(item["kind"] == kind for item in artifacts.values()):
            if kind == "brief" and any(is_local_unsupported_discovery_brief(item["parsed"], artifacts, effects)
                    for item in artifacts.values() if item["kind"] == "brief"):
                discovery_stage_inputs(plan, artifacts, "plan_queries")
                continue
            discovery_stage_inputs(plan, artifacts, producer)
    if stage_id is not None:
        discovery_stage_inputs(plan, artifacts, stage_id)
    manifests = [a for a in artifacts.values() if a["kind"] == "manifest"]
    selections = [a for a in artifacts.values() if a["kind"] == "selection"]
    for manifest_artifact in manifests:
        manifest = manifest_artifact["parsed"]
        expected = compile_discovery_search_derivation(plan, artifacts, effects, manifest,
            manifest_artifact["reference"], job_id=job_id)
        if manifest_artifact["search_derivation"] != expected:
            raise ValueError("programme protected manifest derivation changed")
    if selections:
        if len(manifests) != 1 or len(selections) != 1:
            raise ValueError("programme selection provenance is ambiguous")
        selections[0]["parsed"].validate_manifest(manifests[0]["parsed"], manifests[0]["reference"])
        known = {r.result_id: r.exact_url for r in manifests[0]["parsed"].results}
        for artifact in artifacts.values():
            if artifact["kind"] == "snapshot":
                snapshot = artifact["parsed"]
                selected_ids = selections[0]["parsed"].selected_result_ids
                slot = artifact["slot"]
                if (slot >= len(selected_ids) or selected_ids[slot] != snapshot.result_id
                        or known.get(snapshot.result_id) != snapshot.url):
                    raise ValueError("programme snapshot slot, result and URL were not an exact selected manifest member")
    snapshots = sorted((item for item in artifacts.values() if item["kind"] == "snapshot"), key=lambda item: item["slot"])
    if len(snapshots) > plan.limits.max_sources or any(item["slot"] >= plan.limits.max_sources for item in snapshots):
        raise ValueError("programme physical snapshot count exceeds its original allowance")
    for container in (item for item in artifacts.values() if item["kind"] == "snapshots"):
        summary = container["parsed"]
        expected = [{"url": item["parsed"].url, "digest": item["parsed"].digest,
            "result_id": item["parsed"].result_id, "fetched_at": item["parsed"].fetched_at.isoformat()} for item in snapshots]
        if type(summary) is not dict or set(summary) != {"snapshots", "denied"} or summary["snapshots"] != expected:
            raise ValueError("programme declared snapshots output differs from its physical source artifacts")
    for artifact in artifacts.values():
        if artifact["kind"] != "brief":
            continue
        output = DiscoveryBriefV1.model_validate(artifact["parsed"])
        coverage = output.coverage
        if (coverage.original_public_brief_digest != brief_ref.digest
                or coverage.original_public_brief_byte_count != len(raw_brief)):
            raise ValueError("programme output does not bind its entire original public brief")
        for span in coverage.source_spans:
            adopted = artifacts.get(span.snapshot_ref.artifact_id)
            if adopted is None or adopted["kind"] != "snapshot" or adopted["reference"] != span.snapshot_ref:
                raise ValueError("programme output span has no original adopted snapshot")
            snapshot = adopted["parsed"]
            normalized = "\n".join(snapshot.lines).encode()
            if (span.result_id != snapshot.result_id or span.normalized_digest != snapshot.digest
                    or span.normalized_byte_count != len(normalized)):
                raise ValueError("programme output normalized coverage changed")
            first, last = span.included_first_line, span.included_last_line
            count = last - first + 1 if first else 0
            if ((first == 0) != (last == 0) or (first and not 1 <= first <= last <= len(snapshot.lines))
                    or span.omitted_lines != len(snapshot.lines) - count
                    or (first and hashlib.sha256("\n".join(snapshot.lines[first-1:last]).encode()).hexdigest() != span.included_span_digest)
                    or (not first and span.included_span_digest is not None)):
                raise ValueError("programme represented source span is not mechanically verified")
    return DiscoveryInputWitness(job_id=job_id, input_digest=input_digest, authority_digest=authority_digest,
        checkpoint_digest=_digest(history), artifact_digest=_digest(canonical_artifacts),
        plan=plan, public_brief=public_brief, artifacts=artifacts)
