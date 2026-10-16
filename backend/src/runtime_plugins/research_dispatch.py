"""Python owner of the guardian research callbacks (#1008).

Cordis composes the planning provider, research service and guardian consumer;
every byte they read and every output they propose crosses here. Planning may
read only its own staged request and stage one plan, which is validated with
``validate_goal_research_plan`` against the Python-built request before the
canonical plan artifact is written. Discovery may read only the adopted
extraction request, its raw public sources and its own adopted snapshots;
a staged snapshot must bind the exact raw digest and the job's pinned
provider/config digests (else ``stale_output``) and is adopted through the
existing discovery artifact owner. Plugins never supply authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re

from .contracts import succeeded
from .dispatch import (NativeServiceBlocked, _append_receipt, _canonical, _digest, _run_candidate, _writer, cas_run,
                       read_context)

PLAN_REQUEST = "research-plan"
SNAPSHOT_REQUEST = re.compile(r"snapshot:([0-3])\Z")
REJECTION = re.compile(r"source-rejected:([0-3]):([a-z_]{1,64})\Z")
# Source-content rejections the consumer may record per slot (mirror of guardian.ts).
SOURCE_REJECTIONS = frozenset({"source_feed_malformed", "source_feed_dtd_forbidden", "source_feed_entity_forbidden",
                               "source_output_oversize", "source_normalized_empty", "source_envelope_invalid"})
MAX_SNAPSHOT_BYTES = 65536
PLAN_BINDING_KEYS = ("plan_id", "programme_id", "programme_revision", "goal_id", "goal_revision", "grant_id",
                     "grant_revision", "public_brief_digest", "route_epoch", "strategy_binding", "issued_at",
                     "deadline_at", "idempotency_key")


def _strict_json(raw: bytes):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate key")
            value[key] = item
        return value
    return json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=pairs)


def _content(payload) -> bytes:
    """Child bytes are accepted only with their exact digest and finite size."""
    if payload["content"] is None:
        raise NativeServiceBlocked("native_research_content_required")
    raw = payload["content"].encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != payload["content_digest"] or len(raw) > MAX_SNAPSHOT_BYTES:
        raise NativeServiceBlocked("native_research_content_digest_changed")
    return raw


def _read_result(reference, content: bytes, payload):
    """One bounded UTF-8-aligned chunk at ``offset``; an empty chunk marks the end.

    The digest always covers the whole artifact, so the reader verifies the
    concatenation rather than trusting any single chunk.
    """
    offset, end = payload["offset"], min(len(content), payload["offset"] + payload["max_bytes"])
    if offset > len(content) or (offset < len(content) and content[offset] & 0xC0 == 0x80):
        raise NativeServiceBlocked("native_research_read_offset_invalid")
    while end < len(content) and end > offset and content[end] & 0xC0 == 0x80:
        end -= 1
    if end == offset and offset < len(content):
        raise NativeServiceBlocked("native_research_read_bound_exceeded")
    return succeeded("artifacts.read", {"artifact_ref": reference.artifact_id, "digest": reference.digest,
                                        "size_bytes": end - offset, "content": content[offset:end].decode("utf-8")})


def validate_child_plan(raw: bytes, request: dict):
    """Server validation of a provider plan against the Python-built request."""
    from src.guardian.research_plan_contracts import ArtifactRef, validate_goal_research_plan
    value = _strict_json(raw)
    if type(value) is not dict or any(value.get(key) != request[key] for key in PLAN_BINDING_KEYS):
        raise ValueError("research_plan_binding_changed")
    limits, ceiling = value.get("limits"), request["limits_ceiling"]
    if (type(limits) is not dict or set(limits) != set(ceiling)
            or any(type(limits[key]) is not int or limits[key] > ceiling[key] for key in ceiling)):
        raise ValueError("research_plan_limits_widened")
    plan = validate_goal_research_plan(value)
    if plan.steps[0].input_refs != [ArtifactRef.model_validate(request["public_brief_ref"])]:
        raise ValueError("research_plan_input_changed")
    if any(slot.max_bytes > request["output_caps"].get(slot.slot, 0) for step in plan.steps for slot in step.output_slots):
        raise ValueError("research_plan_output_cap_widened")
    return plan


async def dispatch_research(dispatcher, frame, payload, scope):
    if scope.binding.native_branch == "programme_planning":
        return await _planning(dispatcher, frame["method"], payload, scope)
    return await _discovery(dispatcher, frame["method"], payload, scope)


async def _planning(dispatcher, method, payload, scope):
    from src.guardian.research_plan_contracts import ArtifactRef
    from src.work_board.research_artifacts import json_bytes, read_discovery, stage_discovery_artifact
    from src.work_board.research_parent import PLANNING_KIND, planning_authority
    if method not in {"artifacts.read", "artifacts.stage"}:
        raise NativeServiceBlocked("native_method_candidate_not_bound")
    async with dispatcher.jobs._session() as db:
        await _writer(db)
        run, _, _ = await dispatcher.current(db, scope, method)
        if run.job_kind != PLANNING_KIND:
            raise NativeServiceBlocked("native_research_branch_changed")
        authority = planning_authority(run.declared_authority_json)
        candidate = _run_candidate(run)
        reference = ArtifactRef.model_validate(candidate["planning_request_ref"])
        programme_id = authority.programme_binding.programme_id
        request_bytes = read_discovery(candidate["planning_request_file_path"], reference.digest,
                                       programme_id=programme_id, max_bytes=65536)
        if method == "artifacts.read":
            if payload["artifact_ref"] != reference.artifact_id:
                raise NativeServiceBlocked("native_research_reference_not_bound")
            return _read_result(reference, request_bytes, payload)
        context = read_context(run)
        if payload["request_ref"] != PLAN_REQUEST:
            raise NativeServiceBlocked("native_research_stage_not_bound")
        if context["result"] is not None:
            raise NativeServiceBlocked("native_research_replay_denied")
        raw = _content(payload)
        try:
            plan = validate_child_plan(raw, _strict_json(request_bytes))
        except ValueError as exc:
            reason = str(exc) if str(exc).startswith("research_plan_") else "research_plan_rejected"
            raise NativeServiceBlocked(reason) from exc
        # Python's canonical bytes of the validated plan become the plan artifact.
        staged = stage_discovery_artifact(programme_id=programme_id, job_id=authority.discovery_job_id, kind="plan",
                                          slot=0, content=json_bytes(plan.model_dump(mode="json")))
        result = succeeded(method, {"artifact_ref": staged.reference.artifact_id, "digest": staged.reference.digest,
                                    "size_bytes": len(staged.content)})
        context["result"] = {"result_digest": _digest(result), "payload": result, "plan_file_path": staged.file_path,
                             "plan_ref": staged.reference.model_dump(mode="json"), "no_learning": True}
        # stage_discovery_artifact read the exact bytes back before returning.
        now = datetime.now(timezone.utc).isoformat()
        readback = {"effect_id": "research-plan:" + staged.reference.artifact_id, "receipt_kind": "readback",
                    "effect_type": "research_artifact_readback", "status": "succeeded",
                    "target_path": staged.file_path, "content_sha256": staged.reference.digest,
                    "target_digest": staged.reference.digest, "verified_at": now, "recorded_at": now,
                    "readback_id": "research-plan-readback:" + staged.reference.artifact_id,
                    "reconciled": True, "reconciliation_status": "resolved", "fencing_token": run.fencing_token,
                    "details": {"verified": True, "no_learning": True}}
        await cas_run(db, run, checkpoint_context_json=_canonical(context),
                      effect_receipts_json=_append_receipt(run.effect_receipts_json, readback, key="effect_id"))
        await db.commit()
        return result


def extraction_inputs(witness) -> tuple[dict, dict, dict]:
    """The adopted extraction request, raw sources by slot and snapshots by slot."""
    requests = [item for item in witness.artifacts.values() if item["kind"] == "extraction"]
    if len(requests) != 1:
        raise NativeServiceBlocked("native_research_request_missing")
    request = _strict_json(requests[0]["content"])
    raw = {item["slot"]: item for item in witness.artifacts.values() if item["kind"] == "raw_source"}
    snapshots = {item["slot"]: item for item in witness.artifacts.values() if item["kind"] == "snapshot"}
    if sorted(source["source_slot"] for source in request["sources"]) != sorted(raw) or any(
            raw[source["source_slot"]]["reference"].artifact_id != source["artifact_ref"] for source in request["sources"]):
        raise NativeServiceBlocked("native_research_request_changed")
    return {**request, "reference": requests[0]["reference"]}, raw, snapshots


def rejected_slots(context) -> dict[int, str]:
    return {op["slot"]: op["reason"] for op in context["operations"] if op.get("op") == "source_rejected"}


async def _discovery(dispatcher, method, payload, scope):
    from src.guardian.goal_discovery import write_discovery_output
    from src.guardian.research_plan_contracts import PublicSnapshotV1
    from src.work_board.research_parent import DISCOVERY_KIND
    from src.workflows.research_guard import discovery_writer_scope
    from src.workflows.research_sources import physical_discovery_inputs
    jobs, job_id = dispatcher.jobs, scope.witness["invocation_ref"]
    witness = await physical_discovery_inputs(jobs, job_id)
    async with discovery_writer_scope(witness=witness):
        async with jobs._session() as db:
            await _writer(db)
            run, _, _ = await dispatcher.current(db, scope, method)
            if run.job_kind != DISCOVERY_KIND:
                raise NativeServiceBlocked("native_research_branch_changed")
            context = read_context(run)
            request, raw, snapshots = extraction_inputs(witness)
            rejected = rejected_slots(context)
            if method == "tasks.inspect":
                if payload["job_ref"] != run.run_identity:
                    raise NativeServiceBlocked("native_job_reference_not_bound")
                refs = [item["artifact_id"] for item in json.loads(run.artifact_receipts_json or "[]")
                        if isinstance(item, dict) and item.get("artifact_id")]
                if len(refs) > 16:
                    raise NativeServiceBlocked("native_artifact_inventory_exceeded")
                return succeeded(method, {"job_ref": run.run_identity, "revision": int(run.revision or 1),
                                          "state": run.status, "artifact_refs": refs})
            if method == "artifacts.read":
                readable = {request["reference"].artifact_id: request["reference"]}
                readable.update({item["reference"].artifact_id: item["reference"] for item in [*raw.values(), *snapshots.values()]})
                reference = readable.get(payload["artifact_ref"])
                if reference is None:
                    raise NativeServiceBlocked("native_research_reference_not_bound")
                return _read_result(reference, witness.artifacts[reference.artifact_id]["content"], payload)
            if method == "tasks.checkpoint":
                match = REJECTION.fullmatch(payload["checkpoint_ref"])
                if match is None or match[2] not in SOURCE_REJECTIONS:
                    raise NativeServiceBlocked("native_checkpoint_reference_not_bound")
                slot = int(match[1])
                if slot not in raw or slot in snapshots or slot in rejected:
                    raise NativeServiceBlocked("native_receipt_replay_denied")
                context["operations"].append({"op": "source_rejected", "slot": slot, "reason": match[2], "no_learning": True})
                revision = await cas_run(db, run, checkpoint_context_json=_canonical(context))
                await db.commit()
                return succeeded(method, {"receipt_ref": "cordis-research:" + payload["checkpoint_ref"], "revision": revision})
            if method != "artifacts.stage":
                raise NativeServiceBlocked("native_method_candidate_not_bound")
            match = SNAPSHOT_REQUEST.fullmatch(payload["request_ref"])
            if match is None:
                raise NativeServiceBlocked("native_research_stage_not_bound")
            slot = int(match[1])
            if slot not in raw or slot in snapshots or slot in rejected:
                raise NativeServiceBlocked("native_research_replay_denied")
            try:
                value = _strict_json(_content(payload))
            except ValueError as exc:
                raise NativeServiceBlocked("native_research_snapshot_invalid") from exc
            pins = request["pins"]
            if (type(value) is not dict or set(value) != {"schema_version", "source_slot", "input_digest", "provider_digest",
                                                         "config_digest", "text"}
                    or value["schema_version"] != 1 or value["source_slot"] != slot or type(value["text"]) is not str):
                raise NativeServiceBlocked("native_research_snapshot_invalid")
            # Output from any other provider, configuration or source bytes is stale.
            if (value["input_digest"] != raw[slot]["reference"].digest
                    or value["provider_digest"] != pins["source_provider_digest"]
                    or value["config_digest"] != pins["source_config_digest"]):
                raise NativeServiceBlocked("stale_output")
            text = value["text"]
            if not text or len(text.encode("utf-8")) > MAX_SNAPSHOT_BYTES or "\r" in text:
                raise NativeServiceBlocked("source_output_oversize" if text else "source_normalized_empty")
            selection = next(item for item in witness.artifacts.values() if item["kind"] == "selection")["parsed"]
            manifest = next(item for item in witness.artifacts.values() if item["kind"] == "manifest")["parsed"]
            result_id = selection.selected_result_ids[slot]
            url = next(item.exact_url for item in manifest.results if item.result_id == result_id)
            envelope = _strict_json(raw[slot]["content"])
            try:
                snapshot = PublicSnapshotV1(result_id=result_id, url=url, digest=hashlib.sha256(text.encode()).hexdigest(),
                                            lines=text.split("\n"), fetched_at=datetime.now(timezone.utc),
                                            mime=envelope["media_type"])
            except ValueError as exc:
                raise NativeServiceBlocked("native_research_snapshot_invalid") from exc
            await db.rollback()
    # The existing artifact owner rechecks lease, fence, programme authority and caps.
    artifact = await write_discovery_output(jobs, job_id, scope.witness["lease_owner"], scope.witness["fence"],
                                            "snapshot", snapshot.model_dump(mode="json"), slot=slot)
    return succeeded("artifacts.stage", {"artifact_ref": artifact.reference.artifact_id,
                                         "digest": artifact.reference.digest, "size_bytes": len(artifact.content)})
