"""Operator-started, budgeted re-index of canonical memory into the NEAR TEE
embedding namespace (ADR-035, PR3d).

Never automatic. An authenticated settings action starts one finite run with
an explicit memory-egress consent and a spend cap. The run is one durable job
in the existing workflow job ledger (admit -> queue -> claim -> per-batch
checkpoint/heartbeat -> succeeded / blocked / cost_liability / cancelled);
every batch is an ordinary ``memory_embedding`` request through the shared
broker at that route's background priority (below chat), with its own
reservation and NEAR billing readback inside the lease. There is no new
scheduler lane, queue, budget or ledger.

Resumable and idempotent: the target namespace table itself is the
checkpoint. A later run skips canonical memories already present with the
same text and embeds only the rest; a stopped, blocked or interrupted run is
resumed by starting another run. Cancellation stops before the next batch (a
batch already admitted completes or settles through the broker). Until a run
covers every canonical memory, recall stays clearly labelled lexical: the
namespace is marked complete only then.

Source text is the canonical local memory (``Memory`` rows that are active,
not tombstoned and ordinary model memory). NEAR receives it in plaintext;
the vectors are derived workspace state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from config.settings import settings

logger = logging.getLogger(__name__)

JOB_KIND = "memory.near-embedding-reindex.v1"
CAPABILITY_ID = JOB_KIND
MAX_REINDEX_ITEMS = 10_000
LEASE_SECONDS = 300
STATE_SCHEMA = "seraph.near.tee.embedding.index.v1"
STATE_FILE = "near-tee-embedding-index.json"
LEXICAL_NOTICE = ("Semantic recall is unavailable: memory recall uses lexical search until the NEAR "
    "embedding re-index completes.")
# Reasons that mean contact happened without an authoritative cost.
UNKNOWN_REASONS = frozenset({"near_tee_cost_liability", "remote_inference_uncertain"})
_TERMINAL = {"succeeded", "blocked", "cancelled", "failed", "cost_liability", "degraded", "unknown_external_effect"}


class ReindexStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_policy_revision: int = Field(ge=1)
    budget_microusd: int = Field(ge=1, le=1_000_000_000)
    max_items: int = Field(ge=1, le=MAX_REINDEX_ITEMS)
    memory_egress_acknowledged: bool = False
    request_uuid: str = Field(min_length=36, max_length=36)

    @field_validator("request_uuid")
    @classmethod
    def canonical_uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("request_uuid must be canonical")
        return value


class ReindexCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: str = Field(min_length=1, max_length=128)


class ReindexRefused(RuntimeError):
    def __init__(self, status_code: int, code: str):
        self.status_code = status_code
        self.code = code
        super().__init__(code)


@dataclass
class _Runner:
    task: asyncio.Task | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)


_runners: dict[str, _Runner] = {}


# ── Derived index state (atomic workspace file) ─────────────────────────


def _state_path() -> Path:
    return Path(settings.workspace_dir).expanduser().resolve() / STATE_FILE


def read_index_state() -> dict[str, Any]:
    try:
        payload = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": STATE_SCHEMA, "namespaces": {}, "last_job_id": None}
    if not isinstance(payload, dict) or payload.get("schema_version") != STATE_SCHEMA:
        return {"schema_version": STATE_SCHEMA, "namespaces": {}, "last_job_id": None}
    if not isinstance(payload.get("namespaces"), dict):
        payload["namespaces"] = {}
    return payload


def _write_index_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def mark_namespace_incomplete(metadata, reason_code: str) -> None:
    """Completeness decays: recall degrades to lexical until another run."""
    entry = read_index_state()["namespaces"].get(metadata.namespace)
    if isinstance(entry, dict) and entry.get("status") == "complete":
        _update_namespace(metadata, status="incomplete", reason_code=reason_code)


def namespace_complete(namespace: str) -> bool:
    entry = read_index_state()["namespaces"].get(namespace)
    return isinstance(entry, dict) and entry.get("status") == "complete"


def _update_namespace(metadata, **values) -> None:
    state = read_index_state()
    entry = dict(state["namespaces"].get(metadata.namespace) or {})
    entry.update({"provider": metadata.provider, "model": metadata.model, "dimension": metadata.dimension,
        "schema_version": metadata.schema_version, "updated_at": datetime.now(timezone.utc).isoformat()}, **values)
    state["namespaces"][metadata.namespace] = entry
    _write_index_state(state)


def _remember_job(job_id: str) -> None:
    state = read_index_state()
    state["last_job_id"] = job_id
    _write_index_state(state)


# ── Canonical source ────────────────────────────────────────────────────


def _normalize(value) -> str:
    return " ".join(str(value or "").strip().split())


async def canonical_memory_items() -> list[tuple]:
    """(memory id, recall text, category, embedding id, allowed texts) for every
    active canonical memory, at most ``MAX_REINDEX_ITEMS + 1`` (over-cap probe).

    A vector written by ``add_memory`` is keyed by ``Memory.embedding_id``;
    hybrid recall accepts it when its text is the memory's summary or content.
    """
    from sqlmodel import col, select
    from src.db.engine import get_session
    from src.db.models import Memory, MemoryStatus
    from src.memory.hybrid_retrieval import _memory_hit_text
    from src.memory.repository import (
        _canonical_memory_deletion_marker, _canonical_memory_without_tombstone_clause,
        _ordinary_model_memory_clause,
    )
    async with get_session() as db:
        rows = (await db.execute(select(Memory).where(Memory.status == MemoryStatus.active)
            .where(_canonical_memory_without_tombstone_clause()).where(_ordinary_model_memory_clause())
            .order_by(col(Memory.id)).limit(MAX_REINDEX_ITEMS + 1))).scalars().all()
    items = []
    for memory in rows:
        if _canonical_memory_deletion_marker(memory) is not None:
            continue
        text = _memory_hit_text(memory)
        if text:
            allowed = frozenset(item for item in (_normalize(memory.summary), _normalize(memory.content)) if item)
            items.append((str(memory.id), text, str(getattr(memory.kind, "value", memory.kind) or "fact"),
                str(memory.embedding_id or "") or None, allowed))
    return items


def is_covered(item, stored: dict[str, str]) -> bool:
    """A canonical memory has a current vector under its id or its embedding id."""
    if item[0] in stored and _normalize(stored[item[0]]) == item[1]:
        return True
    return bool(item[3]) and item[3] in stored and _normalize(stored[item[3]]) in item[4]


def index_gaps(items, stored, *, max_input_tokens: int) -> tuple[bool, int]:
    """(over cap, uncovered non-oversize count) of the canonical set vs the index."""
    from src.model_fabric.near_tee_embeddings import estimate_input_tokens
    over_cap = len(items) > MAX_REINDEX_ITEMS
    missing = sum(1 for item in items[:MAX_REINDEX_ITEMS] if not is_covered(item, stored)
        and estimate_input_tokens([item[1]]) <= max_input_tokens)
    return over_cap, missing


def _batches(items, *, max_batch: int, max_tokens: int):
    from src.model_fabric.near_tee_embeddings import estimate_input_tokens
    batch: list = []
    for item in items:
        if estimate_input_tokens([item[1]]) > max_tokens:
            continue  # counted as oversize by the caller; never truncated
        if batch and (len(batch) >= max_batch or estimate_input_tokens([b[1] for b in batch] + [item[1]]) > max_tokens):
            yield batch
            batch = []
        batch.append(item)
    if batch:
        yield batch


# ── Projection ──────────────────────────────────────────────────────────


def _slot_and_metadata():
    from src.memory.embedder import near_tee_embedding_metadata
    from src.model_fabric.configuration import near_tee_slot, near_tee_slot_block_reason, read_model_fabric_configuration
    configured = read_model_fabric_configuration()
    slot = near_tee_slot(configured, "embedding")
    reason = near_tee_slot_block_reason(configured, "embedding") if slot is not None else "near_tee_slot_unconfigured"
    return configured, slot, reason, near_tee_embedding_metadata()


def _job_projection(job: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(job, dict):
        return None
    checkpoints = job.get("checkpoints") or []
    progress = {}
    for item in reversed(checkpoints):
        if isinstance(item, dict) and isinstance(item.get("payload"), dict) and item["payload"].get("kind") == "near_reindex_progress":
            progress = item["payload"]
            break
    status = job.get("status")
    live = job.get("job_id") in _runners
    return {"job_id": job.get("job_id"), "status": "interrupted" if status == "running" and not live else status,
        "reason_code": job.get("failure_reason"), "live": live,
        "planned_items": progress.get("planned_items"), "indexed_items": progress.get("indexed_items", 0),
        "skipped_existing": progress.get("skipped_existing", 0), "oversize_items": progress.get("oversize_items", 0),
        "batches": progress.get("batches", 0), "spent_microusd": progress.get("spent_microusd", 0),
        "budget_microusd": (job.get("declared_authority") or {}).get("budget_microusd"),
        "namespace": progress.get("namespace"), "updated_at": job.get("updated_at")}


async def reindex_status() -> dict[str, Any]:
    """Local-only projection: never contacts NEAR."""
    from src.workflows.job_runtime import durable_job_repository
    try:
        _configured, slot, reason, metadata = _slot_and_metadata()
    except Exception:
        slot, reason, metadata = None, "near_tee_embedding_metadata_unavailable", None
    state = read_index_state()
    job = None
    if state.get("last_job_id"):
        try:
            job = _job_projection(await durable_job_repository.get_job(state["last_job_id"]))
        except Exception:
            job = None
    complete = metadata is not None and namespace_complete(metadata.namespace)
    if complete and slot is not None:
        # Completeness decays when canonical memory changed without a vector
        # (created, edited, or an add_memory write that failed): compare the
        # canonical set with the stored index locally (no contact).
        try:
            from src.memory import vector_store
            items = await canonical_memory_items()
            stored = await asyncio.to_thread(vector_store.namespace_row_texts, metadata)
            over_cap, missing = index_gaps(items, stored, max_input_tokens=slot.max_input_tokens)
            if over_cap or missing:
                mark_namespace_incomplete(metadata, "near_tee_reindex_over_cap" if over_cap else "near_tee_index_stale")
                complete = False
        except Exception:
            complete = False
    entry = read_index_state()["namespaces"].get(metadata.namespace) if metadata else None
    semantic = complete and reason is None
    return {"schema_version": "seraph.near.tee.embedding.reindex.status.v1",
        "slot_configured": slot is not None, "slot_reason_code": reason,
        "namespace": metadata.namespace if metadata else None,
        "provider": "near_tee", "model": metadata.model if metadata else (slot.model_id if slot else None),
        "measured_dimension": metadata.dimension if metadata else None,
        "index_complete": complete, "recall_mode": "semantic" if semantic else "lexical",
        "index_reason_code": None if complete else (entry or {}).get("reason_code") if isinstance(entry, dict) else None,
        "notice": None if semantic else LEXICAL_NOTICE, "job": job, "max_items": MAX_REINDEX_ITEMS,
        "automatic": False}


# ── Actions ─────────────────────────────────────────────────────────────


async def _retire_interrupted(job_id: str | None) -> None:
    """A run left ``running`` with no live runner here is closed before a new run."""
    from src.workflows.job_runtime import durable_job_repository
    if not job_id or job_id in _runners:
        return
    job = await durable_job_repository.get_job(job_id)
    if not job or job.get("status") in _TERMINAL:
        return
    lease = job.get("lease") or {}
    try:
        await durable_job_repository.cancel_job(job_id, owner=lease.get("owner"),
            fencing_token=lease.get("fencing_token"), expected_revision=job.get("revision"),
            reason="near_tee_reindex_interrupted", result={"memory_status": "derived_index_only"},
            result_summary="Interrupted run closed before a new run")
    except Exception:
        logger.warning("could not close interrupted NEAR re-index job", exc_info=True)
        raise ReindexRefused(409, "near_tee_reindex_interrupted_job_unclosed") from None


async def start_reindex(principal, request: ReindexStartRequest) -> dict[str, Any]:
    from src.model_fabric.configuration import read_model_fabric_configuration
    from src.model_fabric.near_tee_catalog import catalog_receipt_block_reason
    from src.model_fabric.configuration import near_tee_profile_for_slot
    from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec, durable_job_repository

    configured = read_model_fabric_configuration()
    if configured.status != "ready" or request.expected_policy_revision != configured.egress_revision:
        raise ReindexRefused(409, "provider_policy_revision_changed")
    if not request.memory_egress_acknowledged:
        raise ReindexRefused(403, "near_tee_memory_egress_acknowledgement_required")
    _configured, slot, reason, metadata = _slot_and_metadata()
    if reason is not None:
        raise ReindexRefused(409, reason)
    profile = near_tee_profile_for_slot("embedding", slot)
    if block := catalog_receipt_block_reason("embedding", profile, revision=configured.egress_revision):
        raise ReindexRefused(409, block)
    if metadata is None:
        raise ReindexRefused(409, "near_tee_embedding_dimension_unmeasured")
    if request.budget_microusd < slot.request_cost_bound_microusd:
        raise ReindexRefused(422, "near_tee_reindex_budget_below_request_reserve")
    if configured.inference_lane is None or request.budget_microusd > configured.inference_lane.spend_ceiling_microusd:
        raise ReindexRefused(422, "near_tee_reindex_budget_exceeds_lane_ceiling")
    if any(runner.task is not None and not runner.task.done() for runner in _runners.values()):
        raise ReindexRefused(409, "near_tee_reindex_already_running")
    await _retire_interrupted(read_index_state().get("last_job_id"))

    owner = principal.principal_id
    job_id = "near-reindex:" + hashlib.sha256(f"{owner}:{request.request_uuid}".encode()).hexdigest()[:40]
    plan = {"namespace": metadata.namespace, "model": metadata.model, "dimension": metadata.dimension,
        "profile_hash": profile.contract_hash, "policy_revision": configured.egress_revision,
        "budget_microusd": request.budget_microusd, "max_items": request.max_items,
        "max_batch_size": slot.max_batch_size, "max_input_tokens": slot.max_input_tokens}
    authority = {"principal": owner, "session_id": principal.session_id, "owner_kind": "user",
        "operator_session_id": principal.session_id, "capability_id": CAPABILITY_ID,
        "permissions": ["canonical_memory_read", "near_tee_embedding_egress", "derived_vector_write"],
        "memory_egress_acknowledged": True, "budget_microusd": request.budget_microusd,
        "limits": {"max_items": request.max_items, "max_batch_size": slot.max_batch_size,
            "request_cost_bound_microusd": slot.request_cost_bound_microusd}}
    job = await durable_job_repository.admit_job(DurableJobSpec(
        identity=DurableJobIdentity(job_id, "user", owner, JOB_KIND, CAPABILITY_ID, "near-embedding-reindex",
            request.request_uuid),
        inputs=plan, session_id=principal.session_id, operator_session_id=principal.session_id,
        priority=20, declared_authority=authority, max_attempts=1, max_outstanding_jobs=1,
        budget_microusd=request.budget_microusd,
        run_fingerprint=hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()))
    if (job.get("receipt") or {}).get("status") == "deduped":
        return await reindex_status()
    job = await durable_job_repository.queue_job(job_id, expected_revision=job["revision"])
    lease_owner = f"near-reindex:{job_id}"
    job = await durable_job_repository.claim_job(job_id, owner=lease_owner, lease_seconds=LEASE_SECONDS,
        expected_revision=job["revision"])
    _remember_job(job_id)
    runner = _Runner()
    _runners[job_id] = runner
    runner.task = asyncio.create_task(_run(job_id, lease_owner, job["lease"]["fencing_token"], principal,
        plan, metadata, runner))
    return await reindex_status()


async def cancel_reindex(request: ReindexCancelRequest) -> dict[str, Any]:
    from src.workflows.job_runtime import durable_job_repository
    runner = _runners.get(request.job_id)
    if runner is not None:
        runner.cancel.set()
        if runner.task is not None:
            # A batch already admitted completes or settles through the broker.
            await asyncio.shield(runner.task)
        return await reindex_status()
    job = await durable_job_repository.get_job(request.job_id)
    if not job or job.get("job_kind") != JOB_KIND:
        raise ReindexRefused(404, "near_tee_reindex_job_not_found")
    await _retire_interrupted(request.job_id)
    return await reindex_status()


async def wait_for_reindex(job_id: str) -> None:
    runner = _runners.get(job_id)
    if runner is not None and runner.task is not None:
        await asyncio.shield(runner.task)


# ── Runner ──────────────────────────────────────────────────────────────


async def _unknown_embedding_operations() -> set[str]:
    from src.workflows.job_runtime import durable_job_repository
    snapshot = await durable_job_repository.inference_accounting_snapshot()
    return {str(row.get("operation_id")) for row in snapshot.get("operations", [])
        if row.get("runtime_path") == "memory_embedding" and row.get("state") == "unknown"}


async def _finish(job_id, owner, fence, status, *, reason=None, summary, result):
    from src.workflows.job_runtime import durable_job_repository
    current = await durable_job_repository.get_job(job_id)
    if not current or current.get("status") != "running":
        return
    progress = {key: value for key, value in result.items() if key != "memory_status"}
    progress["kind"] = "near_reindex_progress"
    current = await durable_job_repository.record_checkpoint(job_id, checkpoint_id="near-reindex:final",
        state=progress, checkpoint_payload=progress, owner=owner, fencing_token=fence,
        expected_revision=current["revision"])
    await durable_job_repository.transition_job(job_id, status, owner=owner, fencing_token=fence,
        expected_revision=current["revision"], reason=reason, result=result, result_summary=summary)


async def _run(job_id, owner, fence, principal, plan, metadata, runner: _Runner) -> None:
    from src.memory import vector_store
    from src.memory.embedder import EmbeddingError, embed_batch_near_tee
    from src.workflows.job_runtime import durable_job_repository

    progress = {"kind": "near_reindex_progress", "namespace": plan["namespace"], "planned_items": 0,
        "indexed_items": 0, "skipped_existing": 0, "oversize_items": 0, "batches": 0, "spent_microusd": 0}
    result = {"memory_status": "derived_index_only", "namespace": plan["namespace"]}
    try:
        probe = await canonical_memory_items()
        over_cap = len(probe) > MAX_REINDEX_ITEMS
        items = probe[:MAX_REINDEX_ITEMS]
        progress["over_cap"] = over_cap
        existing = await asyncio.to_thread(vector_store.namespace_row_texts, metadata)
        pending = [item for item in items if not is_covered(item, existing)]
        progress["skipped_existing"] = len(items) - len(pending)
        planned = pending[:plan["max_items"]]
        progress["planned_items"] = len(planned)
        from src.model_fabric.near_tee_embeddings import estimate_input_tokens
        progress["oversize_items"] = sum(1 for item in planned if estimate_input_tokens([item[1]]) > plan["max_input_tokens"])
        if not namespace_complete(metadata.namespace):
            # A completed namespace is never downgraded by an incremental run.
            _update_namespace(metadata, status="indexing", job_id=job_id)
        bound = plan["budget_microusd"]
        for batch in _batches(planned, max_batch=plan["max_batch_size"], max_tokens=plan["max_input_tokens"]):
            if runner.cancel.is_set():
                await _finish(job_id, owner, fence, "cancelled", reason="operator_cancelled",
                    summary="Re-index cancelled by the operator", result={**result, **progress})
                return
            from src.model_fabric.configuration import near_tee_slot, read_model_fabric_configuration
            slot = near_tee_slot(read_model_fabric_configuration(), "embedding")
            reserve = slot.request_cost_bound_microusd if slot is not None else bound + 1
            if progress["spent_microusd"] + reserve > bound:
                await _finish(job_id, owner, fence, "blocked", reason="near_tee_reindex_budget_exhausted",
                    summary="Budget reached; start another run to resume", result={**result, **progress})
                return
            # Keep the lease live before each (possibly long) broker call and
            # yield so queued chat is admitted first by the broker.
            await durable_job_repository.heartbeat_job(job_id, owner=owner, fencing_token=fence,
                lease_seconds=LEASE_SECONDS)
            await asyncio.sleep(0)
            unknown_before = await _unknown_embedding_operations()
            try:
                vectors, cost, used = await asyncio.to_thread(embed_batch_near_tee, [item[1] for item in batch],
                    principal=principal)
            except EmbeddingError as exc:
                progress["spent_microusd"] += int(getattr(exc, "cost_microusd", None) or 0)
                # The accounting ledger, not the error text, decides: a new
                # Unknown memory_embedding operation is a cost liability.
                new_unknown = (await _unknown_embedding_operations()) - unknown_before
                status = "cost_liability" if new_unknown or exc.reason_code in UNKNOWN_REASONS else "blocked"
                await _finish(job_id, owner, fence, status, reason=exc.reason_code,
                    summary="Re-index stopped: " + exc.reason_code, result={**result, **progress})
                return
            progress["spent_microusd"] += int(cost or 0)
            if used != metadata:
                await _finish(job_id, owner, fence, "blocked", reason="embedding_space_changed",
                    summary="Embedding namespace changed during re-index", result={**result, **progress})
                return
            await asyncio.to_thread(vector_store.write_namespace_rows, metadata,
                [{"id": item[0], "text": item[1], "category": item[2], "vector": vector}
                    for item, vector in zip(batch, vectors)])
            progress["indexed_items"] += len(batch)
            progress["batches"] += 1
            current = await durable_job_repository.get_job(job_id)
            await durable_job_repository.record_checkpoint(job_id, checkpoint_id=f"near-reindex:{progress['batches']}",
                state=dict(progress), checkpoint_payload=dict(progress), owner=owner, fencing_token=fence,
                expected_revision=current["revision"])
        # Verified local readback of the derived index before success: every
        # canonical memory not excluded as oversize is present with its text.
        if not over_cap:
            # Derived rows of memories that are no longer active canonical
            # memory (deleted, tombstoned, archived) are removed. A memory's
            # own id and its add_memory embedding id both count as canonical.
            canonical_ids = {item[0] for item in items} | {item[3] for item in items if item[3]}
            stale = [row_id for row_id in existing if row_id not in canonical_ids]
            if stale:
                await asyncio.to_thread(vector_store.delete_namespace_rows, metadata, stale)
        stored = await asyncio.to_thread(vector_store.namespace_row_texts, metadata)
        covered = [item for item in items if is_covered(item, stored)]
        digest = hashlib.sha256(json.dumps(sorted((item[0], hashlib.sha256(item[1].encode()).hexdigest())
            for item in covered)).encode()).hexdigest()
        current = await durable_job_repository.get_job(job_id)
        await durable_job_repository.record_readback(job_id, target_path=f"lance/memories__{metadata.namespace}",
            status="succeeded", effect_type="near_embedding_index", effect_id=f"near-reindex-index:{job_id}",
            target_digest=digest, readback_id=f"near-reindex-readback:{job_id}",
            verified_at=datetime.now(timezone.utc).isoformat(),
            details={"verified": True, "memory_status": "derived_index_only", "covered_items": len(covered),
                "canonical_items": len(items)},
            owner=owner, fencing_token=fence, expected_revision=current["revision"])
        oversize = {item[0] for item in items if estimate_input_tokens([item[1]]) > plan["max_input_tokens"]}
        covered_ids = {item[0] for item in covered}
        remaining = sum(1 for item in items if item[0] not in covered_ids and item[0] not in oversize)
        progress["covered_items"] = len(covered)
        if over_cap:
            # More canonical memories than one run may cover: never complete.
            _update_namespace(metadata, status="incomplete", job_id=job_id, reason_code="near_tee_reindex_over_cap",
                remaining_items=max(remaining, 0))
        elif remaining == 0:
            # Oversize memories (over the slot's input bound) are never
            # truncated; they stay reachable through lexical recall only.
            _update_namespace(metadata, status="complete", job_id=job_id, item_count=len(items), reason_code=None,
                excluded_oversize_items=progress["oversize_items"],
                completed_at=datetime.now(timezone.utc).isoformat())
        else:
            _update_namespace(metadata, status="incomplete", job_id=job_id, remaining_items=max(remaining, 0),
                reason_code="near_tee_reindex_items_remaining", oversize_items=progress["oversize_items"])
        await _finish(job_id, owner, fence, "succeeded", summary=f"Indexed {progress['indexed_items']} memories",
            result={**result, **progress, "remaining_items": max(remaining, 0)})
    except Exception:
        logger.exception("NEAR re-index run failed")
        try:
            await _finish(job_id, owner, fence, "blocked", reason="near_tee_reindex_failed",
                summary="Re-index failed; start another run to resume", result={**result, **progress})
        except Exception:
            logger.exception("could not record NEAR re-index failure")
    finally:
        _runners.pop(job_id, None)


def _reset_reindex_state() -> None:
    """Test isolation: forget process-local runners."""
    _runners.clear()


def new_request_uuid() -> str:
    return str(uuid4())
