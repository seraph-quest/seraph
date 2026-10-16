"""Cordis agent-loop chat turns (#1019).

One durable ``cordis_chat_turn_v1`` job owns each chat ingress on the Cordis
route. The turn's loop runs in Python exactly as legacy chat does (the same
smolagents agent, prompts, model owner, wrapped tools, approvals and frames);
at every step boundary a short ``agent-loop.startTurn`` invocation on its own
step job asks the reviewed Cordis sequencer for the next transition. The child
sees only a sealed projection of Python's parse (kind and call count, never
text, a tool name or an argument) and may stage only finite decisions; Python
accepts them only when they equal its own parse.

Python keeps every effect: tool intents and outcomes are receipts on the turn
job, the transcript row is inserted in the same fenced writer transaction as
the turn's settlement CAS, and a stale epoch, lost lease, revoked Root or
cancelled turn can neither run another step nor adopt final output. Restart
never resumes a turn.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import secrets
import threading
from types import MethodType

from sqlalchemy import func, select

from .contracts import succeeded
from .dispatch import (NativeServiceBlocked, _append_receipt, _canonical, _digest, _run_candidate, _writer, cas_run,
                       checkpoint_receipt, read_context)

logger = logging.getLogger(__name__)
TURN_JOB_KIND = "cordis_chat_turn_v1"
STEP_JOB_KIND = "cordis_turn_step_v1"
TURN_CONTEXT_TAG = "cordis-turn.v1"
AGENT_LOOP_DOMAIN = "seraph.agent-loop.v1"
# Bounded by the child's 16-callback cap: one read, the decisions, one inspect.
MAX_TOOL_CALLS = 12
STEP_KINDS = ("tool_calls", "final_answer", "parse_error", "max_steps")
DECISION_REFS = frozenset({f"tool_intent:{index}" for index in range(MAX_TOOL_CALLS)}
                          | {"continue", "settlement", "final_call"})
PROFILES = frozenset({"agent", "onboarding", "direct"})
# Grace after the chat timeout so the timeout reply itself can settle.
SETTLEMENT_GRACE = timedelta(seconds=30)
LOOP_NAMES = {"cordis": "cordis.agent-loop.v1", "legacy": "legacy.smolagents", "blocked": "blocked"}


class TurnBlocked(Exception):
    """The Cordis turn cannot continue; the reason is operator-visible."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


TURN_BLOCKED_MESSAGES = {
    "agent_turn_cancelled": "This turn was cancelled before it finished.",
    "agent_turn_memory_revoked": ("A memory was forgotten while this turn ran, so Seraph stopped before using it again. "
                                  "Send the message again."),
    "agent_turn_tool_calls_exceeded": f"The model asked for more than {MAX_TOOL_CALLS} tool calls in one step.",
    "agent_turn_replayed": ("This message already started a turn that did not finish. "
                            "Send it again as a new message."),
    "agent_turn_profile_changed": "Chat settings changed while this turn started. Send the message again.",
    "delegation_pending_1020": ("Delegation is not available on the Cordis agent loop yet (#1020). "
                                "Turn off use_delegation to chat."),
}


def turn_blocked_message(reason: str) -> str:
    return TURN_BLOCKED_MESSAGES.get(reason, f"The Cordis agent loop stopped this turn ({reason}).")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def expected_decisions(projection: dict) -> list[str] | None:
    """Python's copy of the sequencer's transition table; the child must match it exactly."""
    kind, step, max_steps = projection["kind"], projection["step_index"], projection["max_steps"]
    if kind == "max_steps":
        return ["final_call"] if step > max_steps else None
    if step > max_steps:
        return None
    if kind == "final_answer":
        return ["settlement"]
    if kind == "parse_error":
        return ["continue"]
    return [f"tool_intent:{index}" for index in range(projection["call_count"])]


def validate_step_candidate(candidate) -> dict:
    if (type(candidate) is not dict or set(candidate) != {"schema_version", "method", "turn_ref", "projection"}
            or candidate["schema_version"] != 1 or candidate["method"] != "agent-loop.startTurn"
            or type(candidate["turn_ref"]) is not str or not candidate["turn_ref"].startswith("cordis-turn:")):
        raise NativeServiceBlocked("native_turn_candidate_invalid")
    projection = candidate["projection"]
    if (type(projection) is not dict
            or set(projection) != {"schema_version", "kind", "call_count", "step_index", "max_steps", "output_ref"}
            or projection["schema_version"] != 1 or projection["kind"] not in STEP_KINDS
            or any(type(projection[key]) is not int for key in ("call_count", "step_index", "max_steps"))
            or not 0 <= projection["call_count"] <= MAX_TOOL_CALLS
            or (projection["kind"] == "tool_calls") != (projection["call_count"] > 0)
            or projection["max_steps"] < 1 or not 1 <= projection["step_index"] <= projection["max_steps"] + 1
            or type(projection["output_ref"]) is not str or not projection["output_ref"].startswith("turn-output:")
            or expected_decisions(projection) is None):
        raise NativeServiceBlocked("native_turn_candidate_invalid")
    return dict(candidate)


# ---------------------------------------------------------------- routing and status

async def chat_runtime_status(host=None) -> dict:
    """Which loop chat runs on now, and the exact fix when it cannot run."""
    from src.db.engine import get_session
    from .bridge import cordis_host, cordis_recovery
    from .ownership import inventory
    from .ownership import CompositionBindingError, _checked_row
    host = host or cordis_host
    try:
        async with get_session() as db:
            rows = await inventory(db)
            row = rows.get(AGENT_LOOP_DOMAIN)
            if row is not None:
                _checked_row(row)
    except CompositionBindingError:
        return {"loop": LOOP_NAMES["blocked"], "route": "chat_agent", "owner_epoch": None,
                "reason": "agent_loop_inventory_invalid",
                "recovery": "Repair the runtime composition inventory (a malformed owner row fails closed)."}
    if not rows:
        # Only an entirely unseeded inventory has never been cut over; startup seeds
        # every legacy owner row and only a fenced transition writes a Cordis owner.
        return {"loop": LOOP_NAMES["legacy"], "route": "chat_agent", "owner_epoch": None, "reason": None,
                "recovery": None}
    if row is None:
        return {"loop": LOOP_NAMES["blocked"], "route": "chat_agent", "owner_epoch": None,
                "reason": "agent_loop_owner_missing",
                "recovery": "Restore the seraph.agent-loop.v1 owner row with a fenced owner transition."}
    if row.state != "ready":
        return {"loop": LOOP_NAMES["blocked"], "route": "chat_agent", "owner_epoch": row.epoch,
                "reason": f"agent_loop_owner_{row.state}",
                "recovery": "Finish the agent-loop owner transition (cutover or rollback) at a higher epoch."}
    if row.owner_kind == "legacy":
        return {"loop": LOOP_NAMES["legacy"], "route": "chat_agent", "owner_epoch": row.epoch, "reason": None,
                "recovery": None}
    if not host.admitting or host.reviewed is None:
        snapshot = host.snapshot()
        return {"loop": LOOP_NAMES["blocked"], "route": "chat_agent", "owner_epoch": row.epoch,
                "reason": "cordis_" + str(snapshot.get("reason") or "host_unavailable"),
                "recovery": snapshot.get("recovery") or cordis_recovery("blocked", snapshot.get("reason"))}
    from config.settings import settings
    if settings.use_delegation:
        return {"loop": LOOP_NAMES["blocked"], "route": "chat_agent", "owner_epoch": row.epoch,
                "reason": "delegation_pending_1020", "recovery": turn_blocked_message("delegation_pending_1020")}
    return {"loop": LOOP_NAMES["cordis"], "route": "chat_agent", "owner_epoch": row.epoch, "reason": None,
            "recovery": None}


def chat_route(status: dict) -> str:
    if status["loop"] == LOOP_NAMES["cordis"]:
        return "cordis"
    if status["loop"] == LOOP_NAMES["legacy"]:
        return "legacy"
    return "blocked"


def chat_runtime_status_text(loop: str) -> str:
    """The WS status line names the actual loop and route."""
    if loop == "cordis":
        return "Seraph is using the governed OpenRouter chat runtime on the Cordis agent loop (cordis.agent-loop.v1)."
    return "Seraph is using the governed OpenRouter chat runtime on the legacy agent loop (legacy.smolagents)."


def chat_runtime_unavailable_message(status: dict) -> str:
    return ("Chat is unavailable: the Cordis agent loop is not ready "
            f"({status['reason']}). {status.get('recovery') or ''}").strip()


# ---------------------------------------------------------------- child callbacks

async def dispatch_turn_step(dispatcher, frame, payload, scope):
    """The step's only readable artifact is its own sealed projection; decisions are finite."""
    method = frame["method"]
    async with dispatcher.jobs._session() as db:
        await _writer(db)
        run, _, _ = await dispatcher.current(db, scope, method)
        if run.job_kind != STEP_JOB_KIND:
            raise NativeServiceBlocked("native_turn_candidate_not_bound")
        candidate = _run_candidate(run)
        context = read_context(run)
        if method == "artifacts.read":
            if payload != {"artifact_ref": run.run_identity, "max_bytes": 65536, "offset": 0}:
                raise NativeServiceBlocked("native_turn_reference_not_bound")
            content = _canonical(candidate["projection"])
            return succeeded(method, {"artifact_ref": run.run_identity, "digest": _sha(content),
                                      "size_bytes": len(content.encode()), "content": content})
        if method == "tasks.inspect":
            if payload["job_ref"] != run.run_identity:
                raise NativeServiceBlocked("native_job_reference_not_bound")
            return succeeded(method, {"job_ref": run.run_identity, "revision": int(run.revision or 1),
                                      "state": run.status, "artifact_refs": []})
        if method == "tasks.checkpoint":
            checkpoint_ref = payload["checkpoint_ref"]
            if checkpoint_ref not in DECISION_REFS or len(context["operations"]) >= MAX_TOOL_CALLS:
                raise NativeServiceBlocked("native_checkpoint_reference_not_bound")
            context["operations"].append({"op": "decision", "ref": checkpoint_ref})
            receipt_ref = "cordis-turn:" + checkpoint_ref
            revision = int(run.revision or 1) + 1
            receipt = checkpoint_receipt(f"{receipt_ref}:{len(context['operations'])}",
                                         {"checkpoint_ref": checkpoint_ref, "revision": revision, "no_learning": True},
                                         run.fencing_token)
            if await cas_run(db, run, checkpoint_context_json=_canonical(context), checkpoint_receipts_json=_append_receipt(
                    run.checkpoint_receipts_json, receipt, key="checkpoint_id")) != revision:
                raise NativeServiceBlocked("native_original_job_cas_changed")
            await db.commit()
            return succeeded(method, {"receipt_ref": receipt_ref, "revision": revision})
    raise NativeServiceBlocked("native_method_candidate_not_bound")


# ---------------------------------------------------------------- memory revocation

async def memory_revocation_revision(db) -> str:
    """Content-free marker that changes only when the operator revokes memory: a canonical
    delete (tombstone or delete-export marker) or an operator forget. Supersede, decay and
    other lifecycle archiving never change it."""
    from sqlalchemy import or_
    from src.db.models import Memory, MemoryStatus
    from src.memory.repository import _CANONICAL_MEMORY_DELETE_EXPORT_REASON, _memory_tombstone_revision
    count, latest = (await db.execute(select(func.count(Memory.id), func.max(Memory.updated_at)).where(
        Memory.status == MemoryStatus.archived,
        or_(Memory.metadata_json.contains("operator_forget"),
            Memory.metadata_json.contains(_CANONICAL_MEMORY_DELETE_EXPORT_REASON))))).one()
    return _digest({"tombstones": await _memory_tombstone_revision(db), "revoked": count,
                    "latest": latest.isoformat() if latest else None})


async def _token_bound(operator):
    """A WS operator is re-authenticated by session id each message; bind it to that
    session's current token so every Root check below is the exact REST check."""
    if getattr(operator, "_token_hash", None):
        return operator
    from dataclasses import replace
    from src.db.engine import get_session
    from src.db.models import OperatorSession
    async with get_session() as db:
        root = await db.get(OperatorSession, operator.session_id, populate_existing=True)
    if root is None or root.principal_id != operator.principal.principal_id or root.revoked_at is not None:
        raise TurnBlocked("native_original_root_inactive")
    return replace(operator, _token_hash=root.token_hash)


# ---------------------------------------------------------------- the turn

class CordisTurn:
    """Python owner of one Cordis chat turn."""

    def __init__(self, *, operator, job: dict, owner: str, fence: int, host, jobs, profile: str,
                 max_steps: int, deadline: datetime, memory_revision: str):
        self.operator, self.job_id, self.owner, self.fence = operator, job["job_id"], owner, fence
        self.host, self.jobs, self.profile, self.max_steps = host, jobs, profile, max_steps
        self.deadline, self.memory_revision = deadline, memory_revision
        self.loop = asyncio.get_running_loop()
        self.cancelled = threading.Event()
        self.blocked_reason: str | None = None
        self.settled = False
        self._lock = asyncio.Lock()
        self._steps = 0
        self._surface: str | None = None
        # Per-turn key: the child's output_ref never reveals a guessable digest of model text.
        self._ref_key = secrets.token_bytes(32)

    # -------------------------------------------------------- admission

    @classmethod
    async def admit(cls, *, operator, conversation_session_id: str, message_id: str, profile: str,
                    timeout_seconds: float, host=None, jobs=None) -> tuple["CordisTurn | None", bool]:
        """Admit and claim the one turn job for this ingress; returns (turn, replayed).

        A replayed ingress returns ``(None, True)``: the original turn is never re-run.
        """
        from config.settings import settings
        from src.auth.ownership import _current_root
        from src.auth.service import authenticate_principal
        from src.db.engine import get_session
        from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec
        from .bridge import cordis_host
        from .ownership import CompositionBindingError, bind_invocation
        from .service_jobs import _grants, _jobs
        host, jobs = host or cordis_host, _jobs(jobs)
        if profile not in PROFILES:
            raise TurnBlocked("agent_turn_profile_invalid")
        if settings.use_delegation:
            raise TurnBlocked("delegation_pending_1020")
        if not host.admitting or host.reviewed is None:
            raise TurnBlocked("agent_turn_host_unavailable")
        operator = await _token_bound(operator)
        max_steps = 1 if profile == "direct" else int(settings.agent_max_steps)
        candidate = {"schema_version": 1, "conversation_session_id": conversation_session_id, "message_id": message_id,
                     "profile": profile, "max_steps": max_steps,
                     "timeout_seconds": float(timeout_seconds)}
        job_id = "cordis-turn:" + _digest({"root": operator.session_id, "session": conversation_session_id,
                                           "message": message_id})
        deadline = datetime.now(timezone.utc) + timedelta(seconds=float(timeout_seconds)) + SETTLEMENT_GRACE
        from src.auth.service import AuthFailure
        try:
            async with get_session() as db:
                try:
                    await _current_root(db, operator)
                except AuthFailure as exc:
                    raise TurnBlocked("native_original_root_inactive") from exc
                current = await authenticate_principal(operator.principal.principal_id, db=db)
                if current.session_id != operator.session_id or _grants(current.principal) != _grants(operator.principal):
                    raise TurnBlocked("native_original_policy_changed")
                binding = await bind_invocation(db, method="agent-loop.startTurn", native_branch="agent_loop",
                                                reviewed_composition=host.reviewed)
                if not any(item.runtime_domain == AGENT_LOOP_DOMAIN and item.owner_kind == "cordis"
                           for item in binding.dependency_vector):
                    raise TurnBlocked("agent_loop_owner_not_cordis")
                memory_revision = await memory_revocation_revision(db)
        except CompositionBindingError as exc:
            raise TurnBlocked(exc.reason_code) from exc
        authority = {"principal": operator.principal.principal_id, "owner_kind": "user",
                     "session_id": operator.session_id, "grants": _grants(current.principal)}
        spec = DurableJobSpec(
            identity=DurableJobIdentity(job_id=job_id, owner_kind="user", owner_principal_id=operator.principal.principal_id,
                                        job_kind=TURN_JOB_KIND, capability_version="1",
                                        idempotency_scope="cordis-chat-turn", idempotency_key=_sha(job_id)[:64]),
            inputs=candidate, session_id=operator.session_id, conversation_id=conversation_session_id,
            operator_session_id=operator.session_id, declared_authority=authority, deadline_at=deadline,
            max_attempts=1, composition_binding=binding,
            run_fingerprint=_digest({"candidate": candidate, "root": operator.session_id, "kind": TURN_JOB_KIND}))
        from src.workflows.job_runtime import DurableJobIdempotencyConflict
        if await jobs.get_job(job_id) is not None:
            # A duplicate ingress resolves to its original turn, which never re-runs.
            return None, True
        try:
            job = await jobs.admit_job(spec)
        except DurableJobIdempotencyConflict:
            return None, True
        if (job.get("receipt") or {}).get("status") == "deduped" or job["status"] != "accepted":
            return None, True
        queued = await jobs.transition_job(job_id, "queued", expected_revision=job["revision"])
        owner = "cordis-turn:" + host.boot_nonce[:24]
        lease = int((deadline - datetime.now(timezone.utc)).total_seconds()) + 1
        claimed = await jobs.claim_job(job_id, owner=owner, lease_seconds=max(lease, 1),
                                       expected_revision=queued["revision"])
        turn = cls(operator=operator, job=job, owner=owner, fence=int(claimed["lease"]["fencing_token"]), host=host,
                   jobs=jobs, profile=profile, max_steps=max_steps, deadline=deadline, memory_revision=memory_revision)
        await turn._write_context(lambda context: None, initial=True)
        return turn, False

    # -------------------------------------------------------- fenced state

    async def _fetch_live(self, db):
        """The turn's row under the writer: still this owner's live, unsettled attempt."""
        from .ownership import CompositionBindingError, RuntimeCompositionBinding, validate_invocation
        run = await self.jobs._fetch(db, self.job_id)
        now = datetime.now(timezone.utc)
        expiry = run.lease_expires_at
        if (run.status != "running" or run.lease_owner != self.owner or run.fencing_token != self.fence
                or expiry is None or expiry.replace(tzinfo=expiry.tzinfo or timezone.utc) <= now):
            raise TurnBlocked("agent_turn_attempt_changed")
        try:
            await validate_invocation(db, RuntimeCompositionBinding.from_json(run.composition_binding_json))
        except CompositionBindingError as exc:
            raise TurnBlocked("agent_turn_epoch_changed") from exc
        return run

    def _context(self, run) -> dict:
        value = json.loads(run.checkpoint_context_json) if run.checkpoint_context_json else None
        if value is not None and (type(value) is not dict or value.get("context_tag") != TURN_CONTEXT_TAG):
            raise TurnBlocked("agent_turn_context_changed")
        return value

    async def _write_context(self, change, *, initial: bool = False, effects=None) -> None:
        async with self._lock:
            async with self.jobs._session() as db:
                await _writer(db)
                run = await self._fetch_live(db)
                context = self._context(run)
                if initial:
                    context = {"schema_version": 1, "context_tag": TURN_CONTEXT_TAG, "profile": self.profile,
                               "steps": [], "surface_digest": None, "settlement": None}
                elif context is None or context["settlement"] is not None:
                    raise TurnBlocked("agent_turn_context_changed")
                change(context)
                values = {"checkpoint_context_json": _canonical(context)}
                if effects is not None:
                    values["effect_receipts_json"] = effects(json.loads(run.effect_receipts_json or "[]"))
                try:
                    await cas_run(db, run, **values)
                except NativeServiceBlocked as exc:
                    raise TurnBlocked("agent_turn_attempt_changed") from exc
                await db.commit()

    async def check_live(self) -> None:
        """Every step boundary: not cancelled, still the live owner at the bound epoch, Root
        current, deadline open, and no memory revoked since the turn began."""
        if self.cancelled.is_set():
            raise TurnBlocked("agent_turn_cancelled")
        if datetime.now(timezone.utc) >= self.deadline:
            raise TurnBlocked("agent_turn_deadline_expired")
        from src.auth.ownership import _current_root
        from src.auth.service import AuthFailure
        async with self.jobs._session() as db:
            await self._fetch_live(db)
            try:
                await _current_root(db, self.operator)
            except AuthFailure as exc:
                raise TurnBlocked("native_original_root_inactive") from exc
            # Direct chat assembles no memory, so a revocation cannot reach its prompt.
            if self.profile != "direct" and await memory_revocation_revision(db) != self.memory_revision:
                raise TurnBlocked("agent_turn_memory_revoked")

    def run_sync(self, coroutine):
        """Run one owner coroutine from the agent's worker thread."""
        remaining = (self.deadline - datetime.now(timezone.utc)).total_seconds()
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=max(remaining, 1.0))
        except FutureTimeout as exc:
            future.cancel()
            raise TurnBlocked("agent_turn_deadline_expired") from exc

    def cancel(self) -> None:
        self.cancelled.set()

    # -------------------------------------------------------- one step decision

    async def decide(self, kind: str, *, step_index: int, call_count: int = 0, output_digest: str) -> list[str]:
        """One short sequencer invocation; returns the verified decisions."""
        from .bridge import HostBlocked
        from .service_jobs import _block, admit_service_job, claim_service_job, open_invocation
        try:
            await self.check_live()
            if call_count > MAX_TOOL_CALLS:
                raise TurnBlocked("agent_turn_tool_calls_exceeded")
            projection = {"schema_version": 1, "kind": kind, "call_count": call_count, "step_index": step_index,
                          "max_steps": self.max_steps,
                          "output_ref": "turn-output:" + hmac.new(self._ref_key, output_digest.encode(),
                                                                  hashlib.sha256).hexdigest()[:48]}
            expected = expected_decisions(projection)
            if expected is None:
                raise TurnBlocked("agent_turn_transition_invalid")
            candidate = {"schema_version": 1, "method": "agent-loop.startTurn", "turn_ref": self.job_id,
                         "projection": projection}
            key = f"turn-step:{_sha(self.job_id)[:40]}:{step_index}:{kind}"
            try:
                job, replayed = await admit_service_job(
                    operator=self.operator, origin_method="agent-loop.startTurn", native_branch="agent_loop",
                    candidate=candidate, idempotency_key=key, reviewed_composition=self.host.reviewed, jobs=self.jobs,
                    deadline_at=self.deadline)
            except NativeServiceBlocked as exc:
                raise TurnBlocked(exc.reason_code) from exc
            if job["status"] != "accepted":
                raise TurnBlocked("agent_turn_step_replayed")
            owner, fence = await claim_service_job(job, host=self.host, jobs=self.jobs)
            scope = await open_invocation(job_id=job["job_id"], owner=owner, fence=fence, host=self.host,
                                          admission_replayed=replayed, candidate=candidate, jobs=self.jobs)
        except NativeServiceBlocked as exc:
            raise TurnBlocked(exc.reason_code) from exc
        try:
            result = await self.host.request_service("agent-loop.startTurn", {"turn_ref": job["job_id"]},
                                                     original_scope=scope)
            if result["status"] != "succeeded":
                raise NativeServiceBlocked(result["reason_code"])
            async with self.jobs._session() as db:
                await _writer(db)
                run = await self.jobs._fetch(db, job["job_id"])
                decisions = [item["ref"] for item in read_context(run)["operations"]]
                echo = {"job_ref": run.run_identity, "revision": int(run.revision or 1), "state": run.status}
                if result["value"] != echo or decisions != expected:
                    # The child staged anything but Python's own parse: never adopted.
                    raise NativeServiceBlocked("native_child_result_mismatch")
                digest = _digest({"projection": projection, "decisions": decisions})
                now = datetime.now(timezone.utc).isoformat()
                readback = {"effect_id": "cordis-turn-step:" + run.run_identity, "receipt_kind": "readback",
                            "effect_type": "cordis_turn_step", "status": "succeeded",
                            "target_path": "cordis-turn-step:" + digest, "content_sha256": digest,
                            "target_digest": digest, "verified_at": now, "recorded_at": now,
                            "readback_id": "cordis-turn-step-readback:" + run.run_identity, "reconciled": True,
                            "reconciliation_status": "resolved", "fencing_token": run.fencing_token,
                            "details": {"verified": True, "no_learning": True}}
                await cas_run(db, run, effect_receipts_json=_append_receipt(run.effect_receipts_json, readback,
                                                                             key="effect_id"))
                await db.commit()
            await self.jobs.transition_job(job["job_id"], "succeeded", owner=owner, fencing_token=fence,
                                           result={"no_learning": True, "decisions": decisions},
                                           result_summary="Cordis agent-loop step decided; no_learning")
        except (NativeServiceBlocked, HostBlocked) as exc:
            reason = getattr(exc, "reason_code", None) or "native_turn_host_unavailable"
            await _block(self.jobs, scope, reason)
            raise TurnBlocked(reason) from exc
        except asyncio.CancelledError:
            await asyncio.shield(_block(self.jobs, scope, "native_turn_cancelled"))
            raise
        self._steps += 1
        await self._write_context(lambda context: context["steps"].append(
            {"step_index": step_index, "kind": kind, "decisions": decisions, "step_job": job["job_id"]}))
        return decisions

    async def pin_surface(self, digest: str) -> None:
        if self._surface is not None:
            if self._surface != digest:
                raise TurnBlocked("agent_turn_surface_changed")
            return

        def change(context):
            if context["surface_digest"] not in {None, digest}:
                raise TurnBlocked("agent_turn_surface_changed")
            context["surface_digest"] = digest
        await self._write_context(change)
        self._surface = digest

    # -------------------------------------------------------- tool effects

    def _effect_id(self, step_index: int, call_index: int) -> str:
        return f"cordis-turn-tool:{_sha(self.job_id)[:40]}:{step_index}:{call_index}"

    async def tool_intent(self, *, step_index: int, call_index: int, tool_name: str) -> None:
        await self.check_live()
        intent = {"effect_id": self._effect_id(step_index, call_index), "receipt_kind": "effect",
                  "effect_type": "cordis_turn_tool", "status": "intent", "target_path": "native_tool:" + tool_name,
                  "target_digest": _sha(tool_name), "recorded_at": datetime.now(timezone.utc).isoformat(),
                  "fencing_token": self.fence, "details": {"tool_name": tool_name, "no_learning": True}}
        await self._write_context(lambda context: None,
                                  effects=lambda items: _append_receipt(_canonical(items), intent, key="effect_id"))

    async def tool_outcome(self, *, step_index: int, call_index: int, tool_name: str, outcome: str) -> None:
        """Resolve one intent: ``succeeded``, ``failed`` (an error observation) or ``no_effect``."""
        effect_id = self._effect_id(step_index, call_index)
        now = datetime.now(timezone.utc).isoformat()

        def effects(items):
            kept = [item for item in items if not (isinstance(item, dict) and item.get("effect_id") == effect_id)]
            if len(kept) != len(items) - 1:
                raise TurnBlocked("agent_turn_effect_missing")
            return _canonical([*kept, {
                "effect_id": effect_id, "receipt_kind": "effect", "effect_type": "cordis_turn_tool",
                "status": outcome, "target_path": "native_tool:" + tool_name, "target_digest": _sha(tool_name),
                "recorded_at": now, "fencing_token": self.fence, "reconciled": True,
                "reconciliation_status": "resolved", "details": {"tool_name": tool_name, "no_learning": True}}])
        await self._write_context(lambda context: None, effects=effects)

    async def effects(self) -> list[dict]:
        job = await self.jobs.get_job(self.job_id)
        return [{"tool_name": (item.get("details") or {}).get("tool_name"), "status": item.get("status")}
                for item in job["effects"] if item.get("effect_type") == "cordis_turn_tool"]

    # -------------------------------------------------------- settlement

    async def settle(self, outcome: str, *, reason: str | None = None, message: dict | None = None) -> bool:
        """Fenced settlement: the transcript row (insert-if-absent) and the turn CAS commit
        in one writer transaction, then the terminal transition. Returns whether it settled."""
        from src.agent.session import session_manager
        from src.workflows.job_runtime import DurableJobError
        if self.settled:
            return False
        try:
            async with self._lock:
                async with self.jobs._session() as db:
                    await _writer(db)
                    run = await self._fetch_live(db)
                    context = self._context(run)
                    if context is None or context["settlement"] is not None:
                        raise TurnBlocked("agent_turn_context_changed")
                    if (outcome == "succeeded" and self.profile != "direct"
                            and await memory_revocation_revision(db) != self.memory_revision):
                        # Output assembled from a since-revoked memory is never adopted.
                        raise TurnBlocked("agent_turn_memory_revoked")
                    effects = json.loads(run.effect_receipts_json or "[]")
                    if message is not None:
                        from src.agent.session import MessageIngressConflictError
                        try:
                            row = await session_manager.add_message_if_absent_in_db(db, **message)
                        except MessageIngressConflictError as exc:
                            raise TurnBlocked("turn_transcript_conflict") from exc
                        digest = _sha(row.content)
                        now = datetime.now(timezone.utc).isoformat()
                        effects.append({"effect_id": "cordis-turn-reply:" + row.id, "receipt_kind": "readback",
                                        "effect_type": "cordis_turn_reply", "status": "succeeded",
                                        "target_path": "message:" + row.id, "content_sha256": digest,
                                        "target_digest": digest, "verified_at": now, "recorded_at": now,
                                        "readback_id": "cordis-turn-reply-readback:" + row.id, "reconciled": True,
                                        "reconciliation_status": "resolved", "fencing_token": self.fence,
                                        "details": {"verified": True, "no_learning": True}})
                    context["settlement"] = {"outcome": outcome, "reason": reason,
                                             "message_id": message["message_id"] if message else None}
                    try:
                        await cas_run(db, run, checkpoint_context_json=_canonical(context),
                                      effect_receipts_json=_canonical(effects))
                    except NativeServiceBlocked as exc:
                        raise TurnBlocked("agent_turn_attempt_changed") from exc
                    await db.commit()
            self.settled = True
            terminal = "succeeded" if outcome == "succeeded" else "blocked"
            try:
                await self.jobs.transition_job(self.job_id, terminal, owner=self.owner, fencing_token=self.fence,
                                               reason=None if terminal == "succeeded" else (reason or outcome),
                                               result={"no_learning": True, "outcome": outcome},
                                               result_summary="Cordis chat turn settled by its Python owner")
            except DurableJobError:
                # The fenced settlement and reply already committed; stale-lease recovery
                # closes the job, and the operator still gets the committed reply.
                logger.warning("Cordis turn %s settled but its terminal transition was refused", self.job_id)
        except TurnBlocked as exc:
            if exc.reason_code in {"turn_transcript_conflict", "agent_turn_memory_revoked"}:
                # A different row already holds this id, or the output used a revoked
                # memory: never silently accepted, and the turn is closed visibly.
                self.blocked_reason = exc.reason_code
                with suppress(DurableJobError):
                    await self.jobs.transition_job(self.job_id, "blocked", owner=self.owner, fencing_token=self.fence,
                                                   reason=exc.reason_code)
            # A stale owner never adopts output; recovery owns the turn now.
            return False
        except DurableJobError:
            return False
        self.settled = True
        return True

    async def interrupt(self, message: dict | None) -> bool:
        """WS disconnect: cancel the turn and write the one fenced interrupted row."""
        self.cancel()
        return await self.settle("cancelled", reason="agent_turn_cancelled", message=message)

    # -------------------------------------------------------- the legacy loop, sequenced

    def attach(self, agent) -> None:
        """Bind the smolagents agent's step boundaries to this turn.

        Same agent, prompts, model owner and wrapped tools as legacy. Tool calls
        run sequentially in call order (``max_tool_threads=1``); smolagents
        already records observations in call-id order, so memory and frames match.
        """
        from smolagents.utils import AgentExecutionError, AgentParsingError
        turn = self
        agent.max_tool_threads = 1

        def surface() -> str:
            # The turn's tool surface (names, schemas, descriptions), instructions (which carry
            # the onboarding URL allowlist) and temperature: pinned at step 1, compared after.
            return _sha(json.dumps([sorted(
                [name, str(getattr(tool, "description", "")), getattr(tool, "inputs", {}),
                 str(getattr(tool, "output_type", ""))] for name, tool in agent.tools.items()),
                _sha(str(getattr(agent, "instructions", "") or "")), str(getattr(agent.model, "temperature", None))],
                sort_keys=True, default=str))
        original_step = agent._step_stream
        original_process = agent.process_tool_calls
        original_execute = agent.execute_tool_call
        original_max_steps = agent._handle_max_steps_reached
        state = {"step": 0, "call": 0}

        def step_stream(self, memory_step):
            state["step"], state["call"] = memory_step.step_number, 0
            turn.run_sync(turn.check_live())
            # Pinned at the first step and compared at every later one.
            turn.run_sync(turn.pin_surface(surface()))
            try:
                yield from original_step(memory_step)
            except AgentParsingError:
                # A reply without a parsable tool call is an observation; the loop continues.
                output = memory_step.model_output or ""
                turn.run_sync(turn.decide("parse_error", step_index=memory_step.step_number, output_digest=_sha(output)))
                raise

        def process_tool_calls(self, chat_message, memory_step):
            calls = list(chat_message.tool_calls or [])
            names = [call.function.name for call in calls]
            only_final = names == ["final_answer"]
            output_digest = _digest([[call.function.name, call.function.arguments] for call in calls]
                                    + [str(chat_message.content or "")])
            if len(calls) > MAX_TOOL_CALLS:
                # Over the per-step bound: nothing runs, and the model sees why (an observation).
                turn.run_sync(turn.decide("parse_error", step_index=memory_step.step_number,
                                          output_digest=output_digest))
                raise AgentExecutionError(
                    f"You requested {len(calls)} tool calls in one step; at most {MAX_TOOL_CALLS} are allowed. "
                    "Make fewer calls per step.", self.logger)
            turn.run_sync(turn.decide("final_answer" if only_final else "tool_calls", step_index=memory_step.step_number,
                                      call_count=0 if only_final else len(calls), output_digest=output_digest))
            yield from original_process(chat_message, memory_step)

        def execute_tool_call(self, tool_name, arguments):
            if tool_name == "final_answer" or tool_name not in {**self.tools, **self.managed_agents}:
                # final_answer settles; an unknown tool fails before any effect (an observation).
                return original_execute(tool_name, arguments)
            step_index, call_index = state["step"], state["call"]
            state["call"] += 1
            if tool_name == "store_secret":
                # Disclosed regression: the value never enters a Cordis-route effect.
                return ("store_secret is not available on the Cordis agent loop. "
                        "Store the secret in Settings -> Vault instead.")
            turn.run_sync(turn.tool_intent(step_index=step_index, call_index=call_index, tool_name=tool_name))
            try:
                result = original_execute(tool_name, arguments)
            except BaseException as exc:
                from src.agent.exceptions import ClarificationRequired
                from src.approval.exceptions import ApprovalRequired
                no_effect = isinstance(exc, (ApprovalRequired, ClarificationRequired)) or isinstance(
                    getattr(exc, "__cause__", None), (ApprovalRequired, ClarificationRequired))
                turn.run_sync(turn.tool_outcome(step_index=step_index, call_index=call_index, tool_name=tool_name,
                                                outcome="no_effect" if no_effect else "failed"))
                raise
            turn.run_sync(turn.tool_outcome(step_index=step_index, call_index=call_index, tool_name=tool_name,
                                            outcome="succeeded"))
            return result

        def handle_max_steps(self, task, *args, **kwargs):
            turn.run_sync(turn.decide("max_steps", step_index=self.max_steps + 1, output_digest=_sha("max_steps")))
            turn.run_sync(turn.check_live())
            return original_max_steps(task, *args, **kwargs)

        agent._step_stream = MethodType(step_stream, agent)
        agent.process_tool_calls = MethodType(process_tool_calls, agent)
        agent.execute_tool_call = MethodType(execute_tool_call, agent)
        agent._handle_max_steps_reached = MethodType(handle_max_steps, agent)

    async def decide_direct(self, final_text: str) -> None:
        """Direct chat: one tool-less inference whose sealed output is final by construction."""
        await self.decide("final_answer", step_index=1, output_digest=_sha(final_text))


def turn_failure_reason(exc: BaseException) -> str | None:
    """The turn's own block reason if one caused ``exc`` (directly or wrapped by smolagents)."""
    seen = 0
    while exc is not None and seen < 8:
        if isinstance(exc, TurnBlocked):
            return exc.reason_code
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None
