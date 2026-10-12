"""Fixed source reads under the original authenticated parent authority."""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
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
    # Each retained record contains its bytes, decoded value and fixed ref.
    upper = 2 + sum(12 * len(item["content"]) + 141 + 64 + 3 for item in artifacts.values())
    for index, _ in enumerate(next(item for item in plan.steps if item.step_id == stage_id).input_refs):
        _discovery_physical_debit(("stage-input-candidates", stage_id, index), upper, job_id=plan.plan_id)
    _discovery_physical_debit(("stage-input-list", stage_id), upper, job_id=plan.plan_id)
    _discovery_physical_debit(("stage-input-return", stage_id), upper, job_id=plan.plan_id)
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
    from src.workflows.job_runtime import _discovery_utf8_width, _discovery_encoded_upper
    query_bytes = sum(_discovery_utf8_width(query) for query in queries) + max(0, len(queries) - 1)
    for name, upper in (("query-join", 6 * query_bytes + 2), ("query-encoded", query_bytes), ("query-digest", 66),
            ("responses-list", 2 + _discovery_encoded_upper(effects))):
        _discovery_physical_debit(("manifest-derivation", name), upper, job_id=job_id)
    query_digest = hashlib.sha256("\n".join(queries).encode()).hexdigest()
    if manifest.query_digest != query_digest or manifest.run_id != plan.plan_id or len(manifest.results) > plan.limits.max_results:
        raise ValueError("programme manifest derivation differs from original query outputs")
    responses = []
    for index, query in enumerate(queries):
        for name, upper in (("matching-responses", _discovery_encoded_upper(effects)),
                ("query-encoded", _discovery_utf8_width(query)), ("query-digest", 66),
                ("search-url-encoded", len(SEARCH_URL)), ("search-url-digest", 66)):
            _discovery_physical_debit(("manifest-response", index, name), upper, job_id=job_id)
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
        _discovery_physical_debit(("manifest-response", index, "receipt-copy"), _discovery_encoded_upper(receipt), job_id=job_id)
        responses.append(dict(receipt))
    _discovery_physical_debit("manifest-reference-dump", 141, job_id=job_id)
    _discovery_physical_debit("manifest-derivation-return", 141 + _discovery_encoded_upper(responses) + 130, job_id=job_id)
    return {"manifest_ref": reference.model_dump(mode="json"), "query_digest": query_digest, "responses": responses}


def _discovery_physical_function_entries(function, *, job_id, widths, count=2, plan_bytes=0, brief_bytes=0):
    """Source-derived encoded appearances, excluding separately paid callees."""
    from src.workflows.job_runtime import _discovery_object_width
    j = lambda name: 6 * widths[name] + 2
    ref = _discovery_object_width({"artifact_id": 30, "digest": 66, "schema_version": 1})
    path = len("goal-programmes/") + 32 + 1 + 64 + 1 + 64 + len(".json") + 2
    metadata = 2 + path + 66 + 20 + len("public_brief") + 2 + 3
    entries = []
    def add(name, upper, repeats=1):
        entries.extend((("discovery-physical", job_id, function, name), upper) for _ in range(repeats))
    if function == "original-rows":
        for name in ("declared_authority_json", "arguments_json", "checkpoint_receipts_json",
                "artifact_receipts_json", "effect_receipts_json", "declared_authority_json"):
            add("json-" + name, j(name))
        add("authority-dto", j("declared_authority_json"))
        add("artifact-ref", ref, 2)
        for name in ("arguments_json", "declared_authority_json"):
            add("digest-canonical-" + name, j(name))
            add("digest-encoded-" + name, j(name))
            add("digest-return", 66)
        add("return-tuple", 2 + 6 * widths["run_identity"] + 2 + sum(j(name) for name in
            ("declared_authority_json", "arguments_json", "checkpoint_receipts_json",
             "artifact_receipts_json", "effect_receipts_json")) + 5)
    elif function == "records":
        add("empty-selected", 2)
        add("slot-set", 2 + count * (2 + len("public_brief") + 2 + 1 + 1) + max(0, count - 1))
        record = _discovery_object_width({"checkpoint_id": j("checkpoint_receipts_json"),
            "payload": j("checkpoint_receipts_json"), "artifact": j("artifact_receipts_json"),
            "effect": j("effect_receipts_json")})
        seed = 2 + 6 * widths["run_identity"] + 2 + len("public_brief") + 2 + 1 + 2
        identifier_seed = len("goal_public_discovery_v1") + len("goal_discovery_public_brief") + widths["run_identity"] + path + 64 + 4
        for name, upper in (("artifact-ref", ref), ("path-seed-list", seed),
                ("path-seed-json", seed), ("path-seed-encoded", seed), ("path-seed-digest", 66),
                ("expected-path", path), ("identifier-seed-list", identifier_seed),
                ("identifier-seed-string", identifier_seed), ("identifier-seed-encoded", identifier_seed),
                ("identifier-digest", 66), ("identifier-slice", 26), ("identifier-return", 30),
                ("matching-artifacts", j("artifact_receipts_json")),
                ("matching-effects", j("effect_receipts_json")), ("selected-record", record)):
            add(name, upper, count)
        add("return-tuple", 2 + count * record + max(0, count - 1))
    elif function == "references":
        selected = 2 + count * metadata + max(0, count - 1)
        all_refs = 2 + (count + 2) * metadata + count + 1
        add("selected-metadata-list", selected)
        add("initial-metadata-list", 2 + 2 * metadata + 1)
        add("artifact-ref", ref, 2)
        add("matching-list", selected, 2)
        add("concat-list", all_refs)
        add("return-tuple", all_refs)
    elif function in {"provenance", "input-reader"}:
        add("plan-json", 6 * plan_bytes + 2)
        add("plan-dto", 6 * plan_bytes + 2)
        for name in ("plan-model-dump", "plan-size-json", "plan-size-encoded"):
            add(name, 6 * plan_bytes + 2)
        add("artifact-ref", ref, 2 + count if function == "input-reader" else 2 + count)
        if function == "input-reader":
            add("contents-map", 2 + 2 * (6 * path + 1) + 6 * (plan_bytes + brief_bytes) + 4 + 1)
            add("row-copy", widths["row_header_bytes"])
        else:
            add("authority-digest-json", j("declared_authority_json"))
            for name in ("arguments_json", "declared_authority_json", "checkpoint_receipts_json", "artifact_receipts_json"):
                add("digest-canonical-" + name, j(name))
                add("digest-encoded-" + name, j(name))
                add("digest-return", 66)
            add("brief-decode", 6 * brief_bytes + 2)
            for size in (plan_bytes, brief_bytes, brief_bytes, plan_bytes):
                add("content-hash", size)
                add("content-hash-digest", 66)
            add("adopted-plan-json", 6 * plan_bytes + 2)
            identifier_seed = len("goal_public_discovery_v1") + len("goal_discovery_public_brief") + widths["run_identity"] + path + 64 + 4
            for name, upper in (("identifier-seed-list", identifier_seed), ("identifier-seed-string", identifier_seed),
                    ("identifier-seed-encoded", identifier_seed), ("identifier-digest", 66),
                    ("identifier-slice", 26), ("identifier-return", 30)):
                add(name, upper, 2 + count)
            artifact = _discovery_object_width({"reference": ref, "content": 6 * max(plan_bytes, brief_bytes) + 2,
                "parsed": 6 * plan_bytes + 2, "kind": len("public_brief") + 2,
                "slot": 1, "search_derivation": 4})
            add("artifact-record", artifact, 2 + count)
            add("artifacts-map", 2 + (count + 2) * (30 + 1 + artifact) + count + 1)
            add("adopted-slot-set", 2 + count * (2 + len("public_brief") + 2 + 1 + 1) + max(0, count - 1))
            add("matching-artifacts", j("artifact_receipts_json"), count)
            for name in ("manifest-list", "selection-list", "snapshot-sorted-list"):
                add(name, 2)
            witness = _discovery_object_width({"job_id": 6 * widths["run_identity"] + 2,
                "input_digest": 66, "authority_digest": 66, "checkpoint_digest": 66, "artifact_digest": 66,
                "plan": 6 * plan_bytes + 2, "public_brief": 6 * brief_bytes + 2,
                "artifacts": 2 + (count + 2) * (30 + 1 + artifact) + count + 1})
            add("witness-construction", witness)
            add("witness-return", witness)
    return tuple(entries)


_DISCOVERY_PHYSICAL_CAPACITY = ContextVar("original_discovery_physical_capacity", default=None)
_DISCOVERY_VALIDATED_PLAN_OWNER = ContextVar("original_discovery_validated_plan_owner", default=None)


@contextmanager
def _original_discovery_physical_capacity_scope(budget, *, _validated_plan_owner=None):
    """An original reader's capacity facts; no row or execution authority."""
    from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError
    if type(budget) is not HeaderReadBudget:
        raise HeaderBoundsError("programme_physical_capacity_unavailable")
    prior = _DISCOVERY_PHYSICAL_CAPACITY.get()
    if prior is not None and prior[0] is not budget:
        raise HeaderBoundsError("programme_physical_capacity_changed")
    facts = prior[1] if prior is not None else {}
    owner_token = None
    if _validated_plan_owner is not None:
        owned = _validated_plan_owner.jobs._validate_discovery_session(_validated_plan_owner, header_budget=budget)
        if owned._candidate is None or owned._candidate_phase != "reader":
            raise HeaderBoundsError("programme_original_queue_candidate_changed")
        if _DISCOVERY_VALIDATED_PLAN_OWNER.get() not in (None, owned):
            raise HeaderBoundsError("programme_original_queue_candidate_changed")
        owner_token = _DISCOVERY_VALIDATED_PLAN_OWNER.set(owned)
    token = _DISCOVERY_PHYSICAL_CAPACITY.set((budget, facts))
    try:
        yield facts
    finally:
        _DISCOVERY_PHYSICAL_CAPACITY.reset(token)
        if owner_token is not None:
            _DISCOVERY_VALIDATED_PLAN_OWNER.reset(owner_token)


def _discovery_physical_budget(explicit=None):
    from src.memory.header_bounds import HeaderBoundsError
    scope = _DISCOVERY_PHYSICAL_CAPACITY.get()
    if scope is not None and explicit is not None and scope[0] is not explicit:
        raise HeaderBoundsError("programme_physical_capacity_changed")
    return explicit if explicit is not None else scope[0] if scope is not None else None


def _discovery_physical_debit(name, upper, *, job_id, header_budget=None):
    budget = _discovery_physical_budget(header_budget)
    if budget is not None:
        budget.debit(upper, appearance=("discovery-physical", job_id, name))


def _discovery_physical_prepay(budget, function, raw, *, count=2, contents=None, plan_bytes=None, brief_bytes=None):
    budget = _discovery_physical_budget(budget)
    if budget is None:
        return
    from src.workflows.job_runtime import _discovery_utf8_width
    from src.memory.header_bounds import _DISCOVERY_NUMERIC_READS, HeaderBoundsError
    active = _DISCOVERY_NUMERIC_READS.get()
    if active is None or active.budget is not budget:
        # An existing original closure's paid trace is supplied by its owner;
        # this helper is not permission to stage or use a bare frame.
        from src.workflows.job_runtime import _discovery_encoded_upper
        widths = {name: _discovery_utf8_width(value) for name, value in raw.items() if type(value) is str}
        widths["row_header_bytes"] = _discovery_encoded_upper(raw)
        sizes = tuple(len(value) for value in (contents or {}).values())
        plan = plan_bytes if plan_bytes is not None else max(sizes, default=0)
        brief = brief_bytes if brief_bytes is not None else max(sizes, default=0)
        scope = _DISCOVERY_PHYSICAL_CAPACITY.get()
        if scope is not None:
            prior = scope[1].get(raw["run_identity"])
            scope[1][raw["run_identity"]] = {"widths": widths,
                "count": max(count, prior["count"] if prior is not None else 0),
                "plan_bytes": max(plan, prior["plan_bytes"] if prior is not None else 0),
                "brief_bytes": max(brief, prior["brief_bytes"] if prior is not None else 0)}
    else:
        active._checked()
        if raw["run_identity"] == active.constructor["job_id"]:
            widths = active.constructor["text_bytes"]
            widths = {**widths, "row_header_bytes": active.constructor["row_header_bytes"]}
            plan, brief = active.constructor["plan_bytes"], active.constructor["brief_bytes"]
            count = active.constructor.get("physical_count", count)
        else:
            retained = active.physical_widths.get(raw["run_identity"])
            if retained is None:
                raise HeaderBoundsError("programme_physical_numeric_row_unreserved")
            widths, count = retained["widths"], retained["count"]
            plan, brief = retained["plan_bytes"], retained["brief_bytes"]
    for appearance, upper in _discovery_physical_function_entries(function, job_id=raw["run_identity"],
            widths=widths, count=count, plan_bytes=plan, brief_bytes=brief):
        budget.debit(upper, appearance=appearance)


def original_discovery_physical_numeric_schedule(discovery_session, *, constructor, publication=False):
    """Original queue and publication variants for the real initial artifacts."""
    owned = discovery_session.jobs._validate_discovery_session(discovery_session)
    from src.memory.header_bounds import HeaderBoundsError
    if constructor is not owned._constructor_bounds or constructor["job_id"] != owned._candidate[0]:
        raise HeaderBoundsError("programme_constructor_numeric_inputs_changed")
    widths = {**constructor["text_bytes"], "row_header_bytes": constructor["row_header_bytes"]}
    phase = "publication-admission" if publication else "queue"
    entries = []
    counts = {"original-rows": 7 if publication else 6, "records": 3 if publication else 2,
        "references": 2, "provenance": 1, "input-reader": 0 if publication else 1}
    for function, count in counts.items():
        for _ in range(count):
            entries.extend((phase, appearance, upper) for appearance, upper in
                _discovery_physical_function_entries(function, job_id=constructor["job_id"], widths=widths,
                    count=constructor.get("physical_count", 2),
                    plan_bytes=constructor["plan_bytes"], brief_bytes=constructor["brief_bytes"]))
    if publication:
        path = len("goal-programmes/") + 32 + 1 + 64 + 1 + 64 + len(".json")
        size_sum = 2 * (constructor["plan_bytes"] + constructor["brief_bytes"])
        entries.append((phase, ("discovery-physical", constructor["job_id"], "closure-contents"),
            2 + 4 * (6 * path + 3) + size_sum))
    for size in (constructor["plan_bytes"], constructor["brief_bytes"],
            constructor["brief_bytes"], constructor["plan_bytes"]):
        entries.append((phase, None, size + 1))
    return tuple(entries)


def _discovery_original_rows(raw_workflow_row, *, header_budget=None):
    """Decode original finite provenance, without current lifecycle checks."""
    from src.work_board.research_parent import discovery_authority, DISCOVERY_KIND, DISCOVERY_SERVICE
    from src.guardian.research_plan_contracts import ArtifactRef
    from src.workflows.job_runtime import _digest
    if type(raw_workflow_row) is not dict or raw_workflow_row.get("job_kind") != DISCOVERY_KIND:
        raise ValueError("programme physical inputs require native lineage")
    _discovery_physical_prepay(header_budget, "original-rows", raw_workflow_row)
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


def discovery_physical_records(raw_workflow_row, *, header_budget=None):
    """Pure original adoption checks over exact retained provenance records."""
    from src.work_board.research_parent import DISCOVERY_KIND
    from src.work_board.research_artifacts import DISCOVERY_ARTIFACT_LIMITS, discovery_prefix, json_bytes, sha
    from src.guardian.research_plan_contracts import ArtifactRef
    from src.artifacts.registry import artifact_id_for
    job_id, authority, inputs, history, artifacts, effects = _discovery_original_rows(raw_workflow_row, header_budget=header_budget)
    count = sum(1 for entry in history if str(entry.get("checkpoint_id", "")).startswith("discovery:artifact:"))
    _discovery_physical_prepay(header_budget, "records", raw_workflow_row, count=count)
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


def discovery_physical_references(raw_workflow_row, *, header_budget=None):
    """Return each original read appearance, including repeated initial refs."""
    from src.guardian.research_plan_contracts import ArtifactRef
    _, _, inputs, _, _, _ = _discovery_original_rows(raw_workflow_row, header_budget=header_budget)
    records = discovery_physical_records(raw_workflow_row, header_budget=header_budget)
    _discovery_physical_prepay(header_budget, "references", raw_workflow_row, count=len(records))
    selected = [(item["payload"]["file_path"], item["artifact"]["content_sha256"],
        item["payload"]["byte_count"], item["payload"]["kind"])
        for item in records]
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
    with _original_discovery_physical_capacity_scope(header_budget):
        appearances = discovery_physical_references(raw_workflow_row, header_budget=header_budget)
        authority = _discovery_original_rows(raw_workflow_row, header_budget=header_budget)[1]
        header_budget.debit(2 + sum(6 * len(path) + 3 + size for path, _, size, _ in appearances),
            appearance=("discovery-physical", raw_workflow_row["run_identity"], "closure-contents"))
        contents = {}
        for path, digest, size, kind in appearances:
            contents[path] = read_discovery(path, digest, programme_id=authority.programme_binding.programme_id,
                max_bytes=DISCOVERY_ARTIFACT_LIMITS[kind], root=root, expected_size=size, header_budget=header_budget)
        validate_discovery_physical_provenance(raw_workflow_row, contents, historical=True, header_budget=header_budget)
        return appearances


async def physical_discovery_inputs(jobs, job_id, *, completed_read=False, stage_id=None, discovery_session=None, native_programme_source=None):
    if native_programme_source is not None:
        from src.runtime_plugins.programme_producer import _ProgrammeSource
        source = native_programme_source
        if (type(source) is not _ProgrammeSource or discovery_session is not None
                or source.jobs is not jobs or source.job_id != job_id
                or completed_read or stage_id is not None):
            raise ValueError("native_programme_original_physical_source_changed")
        source.validate()
        with _original_discovery_physical_capacity_scope(source.header_budget):
            # The original owner remains live through files, plan parsing,
            # saved-pin validation and provenance. Its engine closes it only
            # after all of those reads have finished.
            async with jobs._native_programme_read_session(source,
                    header_budget=source.header_budget) as db:
                return await _physical_discovery_inputs(jobs, job_id,
                    native_programme_source=source, _native_reader_db=db)
    if discovery_session is not None:
        owned = jobs._validate_discovery_session(discovery_session)
        with owned.guard._discovery_reservation.phase("queue"):
            return await _physical_discovery_inputs(jobs, job_id, completed_read=completed_read,
                stage_id=stage_id, discovery_session=owned)
    return await _physical_discovery_inputs(jobs, job_id, completed_read=completed_read, stage_id=stage_id)


async def _physical_discovery_inputs(jobs, job_id, *, completed_read=False, stage_id=None,
                                     discovery_session=None, native_programme_source=None,
                                     _native_reader_db=None):
    """Reopen immutable programme inputs BEFORE a short native writer."""
    from src.work_board.research_parent import DISCOVERY_KIND
    from src.work_board.research_artifacts import read_discovery, DISCOVERY_ARTIFACT_LIMITS
    from src.guardian.research_plan_contracts import validate_goal_research_plan, ArtifactRef
    from src.workflows.job_runtime import _utc_now
    owned = (jobs._validate_discovery_session(discovery_session)
        if discovery_session is not None else None)
    if owned is not None:
        if completed_read or stage_id is not None or owned._candidate is None or owned._candidate[0] != job_id:
            raise ValueError("programme original physical read owner changed")
    source = native_programme_source
    if source is not None:
        from src.runtime_plugins.programme_producer import _ProgrammeSource
        if type(source) is not _ProgrammeSource:
            raise ValueError("native_programme_original_physical_source_changed")
        source.validate()
        if source.jobs is not jobs or source.job_id != job_id or _native_reader_db is None:
            raise ValueError("native_programme_original_physical_source_changed")
        source.authority_selection(_native_reader_db)
    elif _native_reader_db is not None:
        raise ValueError("native_programme_original_physical_source_changed")
    frame = source.header_budget if source is not None else owned.header_budget if owned is not None else None
    if frame is None:
        from src.memory.header_bounds import HeaderReadBudget
        # This bounded legacy branch check confers no composed read authority.
        frame = HeaderReadBudget()
    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def reader():
        if source is not None:
            # A DB value alone cannot enter this branch: the exact Source
            # validates the still-live original guard/task/transaction/frame.
            source.validate()
            source.authority_selection(_native_reader_db)
            yield _native_reader_db
        else:
            async with jobs._discovery_read_session(discovery_session,
                    header_budget=frame) as db:
                yield db
    async with reader() as db:
        if source is None and owned is None:
            await jobs._require_uncomposed_discovery_row(db, job_id, frame)
        if source is not None:
            from src.memory.header_bounds import WRS_BY_RUN
            await frame.certify(db, WRS_BY_RUN, (job_id,), _header_budget=frame)

        if owned is not None:
            from src.memory.header_bounds import WRS_BY_RUN
            await owned.header_budget.certify(db, WRS_BY_RUN, (job_id,))
        from contextlib import nullcontext
        async with (jobs._discovery_paid_row(owned, job_id) if owned is not None else
                jobs._native_programme_paid_row(source, db) if source is not None else nullcontext()):
            run = await jobs._fetch(db, job_id)
        if run.job_kind != DISCOVERY_KIND:
            raise ValueError("programme physical inputs require native lineage")
        if completed_read and run.status not in {"succeeded", "degraded"}:
            raise ValueError("programme completed readback requires its original positive terminal job")
        if owned is not None:
            _discovery_physical_prepay(owned.header_budget, "input-reader", {"run_identity": job_id})
        if source is not None:
            from src.guardian.goal_programmes import goal_programme_service
            from src.workflows.research_guard import discovery_writer_scope
            from src.work_board.research_parent import discovery_authority
            guard, identity = await source.prepare_selection(db, run)
            source.authority_selection(db)
            row_upper = source.writer[4].common33.rows[("workflow_run_states", job_id)][1]
            frame.debit(row_upper, appearance=("native-programme-physical", job_id, "authority-model"))
            authority = discovery_authority(run.declared_authority_json)
            async with discovery_writer_scope() as policy:
                await goal_programme_service.validate_current_binding(db=db,
                    binding=authority.programme_binding, policy=policy,
                    original_admission_guard=guard, identity_certificate=identity,
                    native_programme_source=source)
            frame.debit(row_upper, appearance=("native-programme-physical", job_id, "row-copy"))
        raw_row = run.model_dump(mode="python")
    _, authority, inputs, history, _, _ = _discovery_original_rows(raw_row, header_budget=frame)
    references = discovery_physical_references(raw_row, header_budget=frame)
    if source is not None:
        source.validate()
        sizes = {kind: size for _, _, size, kind in references}
        _discovery_physical_prepay(frame, "input-reader", raw_row,
            count=len(references), plan_bytes=sizes["plan"], brief_bytes=sizes["public_brief"])
    def physical_read(path, digest, max_bytes):
        if source is not None:
            source.validate()
            source.authority_selection(_native_reader_db)
        if owned is None and source is None:
            return read_discovery(path, digest, programme_id=binding.programme_id, max_bytes=max_bytes)
        sizes = {size for selected_path, selected_digest, size, _ in references
            if selected_path == path and selected_digest == digest}
        if len(sizes) != 1:
            raise ValueError("programme original physical size changed")
        return read_discovery(path, digest, programme_id=binding.programme_id, max_bytes=max_bytes,
            root=(source.writer[3].workspace.host_root if source is not None else owned.guard.workspace.host_root),
            expected_size=next(iter(sizes)), header_budget=frame)
    binding = authority.programme_binding
    contents = {}
    raw_plan = physical_read(inputs["plan_file_path"], ArtifactRef.model_validate(inputs["plan_ref"]).digest, 65536)
    contents[inputs["plan_file_path"]] = raw_plan
    plan_payload = json.loads(raw_plan)
    observed = datetime.fromisoformat(plan_payload["issued_at"].replace("Z", "+00:00")) if completed_read else _utc_now()
    plan = validate_goal_research_plan(plan_payload, now=observed)
    from src.work_board.dispatcher import _dispatcher
    current_service = _dispatcher.goal_discovery
    if current_service is not None:
        if owned is not None and current_service is not owned.service:
            raise ValueError("programme original physical strategy owner changed")
        if source is not None and current_service is not source.service:
            raise ValueError("native_programme_original_strategy_owner_changed")
        async with reader() as db:
            if source is None and owned is None:
                await jobs._require_uncomposed_discovery_row(db, job_id, frame)
            if source is not None:
                await frame.certify(db, WRS_BY_RUN, (job_id,), _header_budget=frame)
                async with jobs._native_programme_paid_row(source, db):
                    current_run = await jobs._fetch(db, job_id)
                await source.prepare_selection(db, current_run)
            await current_service._validate_pinned_strategy(binding, plan.strategy_binding, db=db,
                **({"discovery_session": owned} if owned is not None else
                    {"native_programme_source": source} if source is not None else {}))
    elif source is not None:
        raise ValueError("native_programme_original_strategy_owner_changed")
    elif plan.strategy_binding.status != "none":
        raise ValueError("programme active strategy requires its current native lifecycle owner")
    contents[inputs["public_brief_file_path"]] = physical_read(inputs["public_brief_file_path"],
        ArtifactRef.model_validate(inputs["public_brief_ref"]).digest, 8000)
    for entry in history:
        if str(entry.get("checkpoint_id", "")).startswith("discovery:artifact:"):
            payload = entry["payload"]
            reference = ArtifactRef.model_validate(payload["artifact_ref"])
            contents[payload["file_path"]] = physical_read(payload["file_path"], reference.digest,
                DISCOVERY_ARTIFACT_LIMITS[payload["kind"]])
    return validate_discovery_physical_provenance(raw_row, contents, historical=completed_read, stage_id=stage_id, header_budget=frame)


def validate_discovery_physical_provenance(raw_workflow_row, contents, *, historical=False, stage_id=None, header_budget=None):
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
    job_id, authority, inputs, history, canonical_artifacts, effects = _discovery_original_rows(raw_workflow_row, header_budget=header_budget)
    count = sum(1 for entry in history if str(entry.get("checkpoint_id", "")).startswith("discovery:artifact:"))
    _discovery_physical_prepay(header_budget, "provenance", raw_workflow_row, count=count, contents=contents,
        plan_bytes=len(contents[inputs["plan_file_path"]]), brief_bytes=len(contents[inputs["public_brief_file_path"]]))
    for path, digest, size, _ in discovery_physical_references(raw_workflow_row, header_budget=header_budget):
        content = contents[path]
        if path not in {inputs["plan_file_path"], inputs["public_brief_file_path"]}:
            _discovery_physical_debit(("stage-content-hash", path), size, job_id=job_id, header_budget=header_budget)
            _discovery_physical_debit(("stage-content-digest", path), 66, job_id=job_id, header_budget=header_budget)
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
        if kind not in {"plan", "public_brief"}:
            _discovery_physical_debit(("stage-json", kind, slot), 6 * len(content) + 2,
                job_id=job_id, header_budget=header_budget)
            _discovery_physical_debit(("stage-record", kind, slot),
                6 * len(content) + 6 * len(payload["file_path"]) + 6 * len(raw_workflow_row["checkpoint_receipts_json"]) + 2,
                job_id=job_id, header_budget=header_budget)
        parsed = json.loads(content) if kind != "public_brief" else None
        if kind == "queries":
            _discovery_physical_debit(("query-set", slot), 6 * len(content) + 2, job_id=job_id, header_budget=header_budget)
            if type(parsed) is dict and type(parsed.get("queries")) is list:
                from src.workflows.job_runtime import _discovery_utf8_width
                for index, query in enumerate(parsed["queries"]):
                    if type(query) is str:
                        size = _discovery_utf8_width(query)
                        _discovery_physical_debit(("query-strip", slot, index), 6 * size + 2, job_id=job_id, header_budget=header_budget)
                        _discovery_physical_debit(("query-encoded", slot, index), size, job_id=job_id, header_budget=header_budget)
            if (type(parsed) is not dict or set(parsed) != {"queries"} or type(parsed["queries"]) is not list
                    or not 1 <= len(parsed["queries"]) <= plan.limits.max_queries
                    or len(set(parsed["queries"])) != len(parsed["queries"])
                    or any(type(q) is not str or not q.strip() or len(q.encode()) > 2048 or any(ord(c) < 32 for c in q) for q in parsed["queries"])):
                raise ValueError("programme physical query plan exceeds its original allowance")
        elif kind == "manifest":
            _discovery_physical_debit(("stage-dto", kind, slot), 6 * len(content) + 2, job_id=job_id, header_budget=header_budget)
            for name in ("result-id-set", "result-url-set"):
                _discovery_physical_debit(("manifest-validator", slot, name), 6 * len(content) + 2, job_id=job_id, header_budget=header_budget)
            if type(parsed) is dict and type(parsed.get("results")) is list:
                from src.workflows.job_runtime import _discovery_utf8_width
                for index, result in enumerate(parsed["results"]):
                    if type(result) is dict and type(result.get("exact_url")) is str:
                        _discovery_physical_debit(("manifest-url-parser", slot, index),
                            6 * _discovery_utf8_width(result["exact_url"]) + 2, job_id=job_id, header_budget=header_budget)
            parsed = SearchManifestV1.model_validate(parsed)
            if parsed.run_id != plan.plan_id or len(parsed.results) > plan.limits.max_results:
                raise ValueError("programme search manifest run or cap changed")
        elif kind == "selection":
            _discovery_physical_debit(("stage-dto", kind, slot), 6 * len(content) + 2, job_id=job_id, header_budget=header_budget)
            _discovery_physical_debit(("selection-validator", slot, "id-set"), 6 * len(content) + 2, job_id=job_id, header_budget=header_budget)
            parsed = SourceSelectionV1.model_validate(parsed)
            if len(parsed.selected_result_ids) > plan.limits.max_sources:
                raise ValueError("programme selected source count exceeds its original allowance")
        elif kind == "snapshot":
            _discovery_physical_debit(("stage-dto", kind, slot), 6 * len(content) + 2, job_id=job_id, header_budget=header_budget)
            for name, upper in (("normalized-join", 6 * len(content) + 2), ("normalized-encoded", 6 * len(content) + 2), ("normalized-digest", 66)):
                _discovery_physical_debit(("snapshot-validator", slot, name), upper, job_id=job_id, header_budget=header_budget)
            if type(parsed) is dict and type(parsed.get("url")) is str:
                from src.workflows.job_runtime import _discovery_utf8_width
                _discovery_physical_debit(("snapshot-url-parser", slot), 6 * _discovery_utf8_width(parsed["url"]) + 2,
                    job_id=job_id, header_budget=header_budget)
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
    retained_upper = 2 + sum(12 * len(item["content"]) + 141 + 64 + 3 for item in artifacts.values())
    # These later-stage containers are absent from the initial two-artifact
    # constructor. Their original paid current-root trace retains every fee.
    if count > 2:
        for name in ("manifest-list", "selection-list", "snapshot-sorted-list"):
            _discovery_physical_debit(("retained-stage", name), retained_upper,
                job_id=job_id, header_budget=header_budget)
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
        for name in ("selected-id-set", "manifest-id-set"):
            _discovery_physical_debit(("selection-manifest", name), retained_upper, job_id=job_id, header_budget=header_budget)
        selections[0]["parsed"].validate_manifest(manifests[0]["parsed"], manifests[0]["reference"])
        from src.workflows.job_runtime import _discovery_encoded_upper, _discovery_utf8_width
        known_upper = 2 + sum(_discovery_encoded_upper(r.result_id) + _discovery_encoded_upper(r.exact_url) + 2
            for r in manifests[0]["parsed"].results)
        _discovery_physical_debit("selection-known-map", known_upper, job_id=job_id, header_budget=header_budget)
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
        from src.workflows.job_runtime import _discovery_encoded_upper, _discovery_object_width
        summary_upper = 2 + sum(_discovery_object_width({"url": _discovery_encoded_upper(item["parsed"].url),
            "digest": 66, "result_id": _discovery_encoded_upper(item["parsed"].result_id), "fetched_at": 44}) + 1
            for item in snapshots)
        _discovery_physical_debit("snapshot-summary-list", summary_upper, job_id=job_id, header_budget=header_budget)
        for item in snapshots:
            _discovery_physical_debit(("snapshot-time", item["slot"]), 44, job_id=job_id, header_budget=header_budget)
        expected = [{"url": item["parsed"].url, "digest": item["parsed"].digest,
            "result_id": item["parsed"].result_id, "fetched_at": item["parsed"].fetched_at.isoformat()} for item in snapshots]
        if type(summary) is not dict or set(summary) != {"snapshots", "denied"} or summary["snapshots"] != expected:
            raise ValueError("programme declared snapshots output differs from its physical source artifacts")
    for artifact in artifacts.values():
        if artifact["kind"] != "brief":
            continue
        _discovery_physical_debit("brief-output-dto", 6 * len(artifact["content"]) + 2, job_id=job_id, header_budget=header_budget)
        if type(artifact["parsed"]) is dict:
            coverage_value = artifact["parsed"].get("coverage")
            if type(coverage_value) is dict and type(coverage_value.get("sources")) is list:
                from src.workflows.job_runtime import _discovery_utf8_width
                for index, source in enumerate(coverage_value["sources"]):
                    if type(source) is dict and type(source.get("url")) is str:
                        _discovery_physical_debit(("brief-source-url-parser", index), 6 * _discovery_utf8_width(source["url"]) + 2,
                            job_id=job_id, header_budget=header_budget)
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
            from src.workflows.job_runtime import _discovery_utf8_width
            normalized_bytes = sum(_discovery_utf8_width(line) for line in snapshot.lines) + max(0, len(snapshot.lines) - 1)
            _discovery_physical_debit(("brief-normalized-join", span.snapshot_ref.artifact_id), 6 * normalized_bytes + 2, job_id=job_id, header_budget=header_budget)
            _discovery_physical_debit(("brief-normalized-encoded", span.snapshot_ref.artifact_id), normalized_bytes, job_id=job_id, header_budget=header_budget)
            normalized = "\n".join(snapshot.lines).encode()
            if (span.result_id != snapshot.result_id or span.normalized_digest != snapshot.digest
                    or span.normalized_byte_count != len(normalized)):
                raise ValueError("programme output normalized coverage changed")
            first, last = span.included_first_line, span.included_last_line
            count = last - first + 1 if first else 0
            if first:
                # Full original lines are an upper bound for the selected slice.
                for name, upper in (("slice", 6 * normalized_bytes + 2), ("join", 6 * normalized_bytes + 2),
                        ("encoded", normalized_bytes), ("digest", 66)):
                    _discovery_physical_debit(("brief-span", span.snapshot_ref.artifact_id, name), upper,
                        job_id=job_id, header_budget=header_budget)
            if ((first == 0) != (last == 0) or (first and not 1 <= first <= last <= len(snapshot.lines))
                    or span.omitted_lines != len(snapshot.lines) - count
                    or (first and hashlib.sha256("\n".join(snapshot.lines[first-1:last]).encode()).hexdigest() != span.included_span_digest)
                    or (not first and span.included_span_digest is not None)):
                raise ValueError("programme represented source span is not mechanically verified")
    if len(artifacts) > 2:
        for name in ("witness-construction", "witness-return"):
            _discovery_physical_debit(("retained-stage", name), retained_upper,
                job_id=job_id, header_budget=header_budget)
    retained_owner = _DISCOVERY_VALIDATED_PLAN_OWNER.get()
    if retained_owner is not None:
        retained_owner.jobs._validate_discovery_session(retained_owner, header_budget=_discovery_physical_budget(header_budget))
        if (retained_owner._candidate != (job_id, binding) or retained_owner._candidate_phase != "reader"
                or raw_workflow_row["status"] != "accepted"):
            raise ValueError("programme_original_queue_candidate_changed")
        # Retain the exact already-validated instance, without copying,
        # reparsing, resolving a current strategy or issuing a new pin.
        retained_owner._discovery_persisted_strategy = plan.strategy_binding
    return DiscoveryInputWitness(job_id=job_id, input_digest=input_digest, authority_digest=authority_digest,
        checkpoint_digest=_digest(history), artifact_digest=_digest(canonical_artifacts),
        plan=plan, public_brief=public_brief, artifacts=artifacts)
