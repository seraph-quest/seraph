"""WorkBoard worker draft turns on the Cordis loop (#1020 PR 3).

The Mail reply draft (``work.mail-reply-draft.v1``) and the meeting brief
(``calendar.meeting-prep.v1``) are the dispatcher's only inline model calls.
On the Cordis owner each becomes one durable ``cordis_board_worker_turn_v1``
job under its reviewed worker preset: owned by the task owner and bound to
that owner's Root, and a durable child of the attempt's root job at that
job's lease and fence.

* The turn is admitted before the worker's first provider contact (the Gmail
  or Calendar source read). If the runtime is unavailable there, the attempt
  is refused with ``board_worker_runtime_unavailable``: nothing was contacted
  and a retry is safe.
* Python then makes exactly today's source reads and model call. After the
  route receipt, one short ``board-workers.startTurn`` step asks the reviewed
  sequencer to settle (the child sees only a sealed projection and may stage
  only ``settlement``), and the fenced settlement commits. Only then does the
  dispatcher parse the output, re-read the source and stage the draft.
* A refusal at that point (a stale epoch, a lost lease, a revoked Root, a
  parent attempt that moved) is ``board_worker_settlement_refused``: the
  inference was spent and stays on the attempt's ledger row, no external
  effect was made, and a retry is safe.

Routing follows the ``seraph.board-workers.v1`` owner row. A legacy owner runs
today's path holding the drain gate; a Cordis owner that is not ready, or whose
host or presets are unavailable, refuses the draft visibly and never falls back.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import secrets
from types import SimpleNamespace
import uuid

from .cutover import legacy_turns
from .dispatch import NativeServiceBlocked, _append_receipt, _canonical, _digest, _writer, cas_run, read_context

logger = logging.getLogger(__name__)
BOARD_WORKERS_DOMAIN = "seraph.board-workers.v1"
PRESETS_DOMAIN = "seraph.agent-presets.v1"
TURN_JOB_KIND = "cordis_board_worker_turn_v1"
STEP_JOB_KIND = "cordis_board_worker_step_v1"
AUTHORITY_TYPE = "board_worker_turn_v1"
CONTEXT_TAG = "cordis-board-worker-turn.v1"
STEP_TIMEOUT = timedelta(seconds=30)
SETTLEMENT_GRACE = timedelta(seconds=30)
LOOP_NAMES = {"cordis": "cordis.board-workers.v1", "legacy": "legacy.board-workers", "blocked": "blocked"}
RUNTIME_UNAVAILABLE = "board_worker_runtime_unavailable"
SETTLEMENT_REFUSED = "board_worker_settlement_refused"
SETTLEMENT_REFUSED_RECOVERY = "Inference was spent, no external effect was made; safe to retry."
# The #1019 cutover manifest lists this domain after the presets every turn
# binds; boot moves it once to Cordis and an operator rollback is sticky.


class BoardWorkerBlocked(Exception):
    """The draft did not run, or its output may not be used; both are retry-safe.

    ``reason_code`` is one of the two Board-visible codes; ``detail`` is the
    underlying owner, host or fence reason.
    """

    def __init__(self, reason_code: str, detail: str | None = None, recovery: str | None = None):
        self.reason_code = reason_code
        self.detail = detail or reason_code
        self.recovery = recovery or board_worker_recovery(reason_code, self.detail)
        super().__init__(reason_code)


def board_worker_recovery(reason_code: str, detail: str | None = None) -> str:
    if reason_code == SETTLEMENT_REFUSED:
        return SETTLEMENT_REFUSED_RECOVERY
    detail = detail or ""
    if detail.startswith("cordis_") or detail == "board_worker_host_unavailable":
        return ("Start the Cordis runtime (./manage.sh -e dev local up locally, or ./manage.sh -e prod up -d --build "
                "under Docker), then retry; nothing was contacted.")
    if detail == "composition_dependency_missing":
        return "Restart Seraph to seed the runtime inventory, then retry; nothing was contacted."
    if detail.startswith("board_workers_owner_") or detail.startswith("agent_presets_owner_"):
        return "Finish the board-workers owner transition (cutover or rollback), then retry; nothing was contacted."
    return "Nothing was contacted; retry once the runtime is ready."


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- routing and status

async def board_worker_runtime_status(host=None) -> dict:
    """Which path WorkBoard drafts take now, and the exact fix when they cannot run."""
    from src.db.engine import get_session
    from .bridge import cordis_host, cordis_recovery
    from .ownership import CompositionBindingError, inventory
    host = host or cordis_host
    try:
        async with get_session() as db:
            rows = await inventory(db)
    except CompositionBindingError:
        return {"loop": LOOP_NAMES["blocked"], "owner_epoch": None, "reason": "board_workers_inventory_invalid",
                "recovery": "Repair the runtime composition inventory (a malformed owner row fails closed)."}
    if not rows:
        # An entirely unseeded inventory has never been cut over.
        return {"loop": LOOP_NAMES["legacy"], "owner_epoch": None, "reason": None, "recovery": None}
    row = rows.get(BOARD_WORKERS_DOMAIN)
    if row is None:
        return {"loop": LOOP_NAMES["blocked"], "owner_epoch": None, "reason": "composition_dependency_missing",
                "recovery": board_worker_recovery(RUNTIME_UNAVAILABLE, "composition_dependency_missing")}
    if row.state != "ready":
        reason = f"board_workers_owner_{row.state}"
        return {"loop": LOOP_NAMES["blocked"], "owner_epoch": row.epoch, "reason": reason,
                "recovery": board_worker_recovery(RUNTIME_UNAVAILABLE, reason)}
    if row.owner_kind == "legacy":
        return {"loop": LOOP_NAMES["legacy"], "owner_epoch": row.epoch, "reason": None, "recovery": None}
    presets = rows.get(PRESETS_DOMAIN)
    if presets is None or presets.state != "ready":
        reason = "composition_dependency_missing" if presets is None else f"agent_presets_owner_{presets.state}"
        return {"loop": LOOP_NAMES["blocked"], "owner_epoch": row.epoch, "reason": reason,
                "recovery": board_worker_recovery(RUNTIME_UNAVAILABLE, reason)}
    if not host.admitting or host.reviewed is None:
        snapshot = host.snapshot()
        return {"loop": LOOP_NAMES["blocked"], "owner_epoch": row.epoch,
                "reason": "cordis_" + str(snapshot.get("reason") or "host_unavailable"),
                "recovery": snapshot.get("recovery") or cordis_recovery("blocked", snapshot.get("reason"))}
    return {"loop": LOOP_NAMES["cordis"], "owner_epoch": row.epoch, "reason": None, "recovery": None}


# ---------------------------------------------------------------- authority

async def _parent_live(db, authority: dict) -> None:
    """The attempt root the turn is a child of is still running at the admitted lease and fence."""
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    parent = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == authority.get("parent_job_id")).execution_options(populate_existing=True))
    now = datetime.now(timezone.utc)
    if (parent is None or parent.status != "running" or parent.lease_owner != authority.get("parent_lease_owner")
            or parent.fencing_token != authority.get("parent_fence") or parent.lease_expires_at is None
            or parent.lease_expires_at.replace(tzinfo=parent.lease_expires_at.tzinfo or timezone.utc) <= now):
        raise NativeServiceBlocked("native_board_worker_parent_inactive")


async def current_board_worker_authority(db, run, authority: dict) -> None:
    """A draft turn's authority: the task owner's live Root with unchanged grants, the live
    unsettled turn, and its live attempt root."""
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    from .dispatch import current_operator_root
    if (run.job_kind not in {TURN_JOB_KIND, STEP_JOB_KIND} or run.owner_kind != "user"
            or run.owner_principal_id != authority.get("principal") or run.service_id is not None):
        raise NativeServiceBlocked("native_board_worker_lineage_changed")
    await current_operator_root(db, run, authority)
    turn_id = authority.get("turn_job_id") if run.job_kind == STEP_JOB_KIND else run.run_identity
    turn = run if turn_id == run.run_identity else await db.scalar(
        select(WorkflowRunState).where(WorkflowRunState.run_identity == turn_id)
        .execution_options(populate_existing=True))
    now = datetime.now(timezone.utc)
    if (turn is None or turn.job_kind != TURN_JOB_KIND or turn.status != "running"
            or turn.lease_expires_at is None
            or turn.lease_expires_at.replace(tzinfo=turn.lease_expires_at.tzinfo or timezone.utc) <= now):
        raise NativeServiceBlocked("native_board_worker_turn_inactive")
    turn_authority = json.loads(turn.declared_authority_json or "{}")
    context = json.loads(turn.checkpoint_context_json) if turn.checkpoint_context_json else {}
    if turn_authority.get("authority_type") != AUTHORITY_TYPE or (context or {}).get("settlement") is not None:
        raise NativeServiceBlocked("native_board_worker_turn_inactive")
    await _parent_live(db, turn_authority)


# ---------------------------------------------------------------- the turn

class WorkerTurn:
    """Python owner of one WorkBoard draft turn."""

    def __init__(self, *, job_id: str, owner: str, fence: int, host, jobs, preset_id: str, preset_digest: str,
                 deadline: datetime, job_owner: dict):
        self.job_id, self.owner, self.fence = job_id, owner, fence
        self.job_owner = job_owner
        self.host, self.jobs, self.preset_id, self.preset_digest = host, jobs, preset_id, preset_digest
        self.deadline = deadline
        self.settled = False
        self.blocked_reason: str | None = None
        self._output_digest: str | None = None
        self._ref_key = secrets.token_bytes(32)

    @classmethod
    async def admit(cls, *, preset_id: str, owner_principal_id: str, owner_session_id: str, parent: dict,
                    timeout_seconds: float, host=None, jobs=None) -> "WorkerTurn":
        """Admit the draft turn as a durable child of the attempt root ``parent`` ({job_id, owner, fence})."""
        from src.agent.presets import PresetError, preset_digest, require_worker
        from src.auth.service import AuthFailure, authenticate_principal
        from src.db.engine import get_session
        from src.workflows.job_runtime import DurableJobError, DurableJobIdentity, DurableJobSpec
        from .bridge import cordis_host
        from .dispatch import current_operator_root
        from .ownership import CompositionBindingError, bind_invocation
        from .service_jobs import _grants, _jobs
        host, jobs = host or cordis_host, _jobs(jobs)
        try:
            require_worker(preset_id)
            digest = preset_digest(preset_id)
        except PresetError as exc:
            raise BoardWorkerBlocked(RUNTIME_UNAVAILABLE, exc.reason_code) from exc
        if not host.admitting or host.reviewed is None:
            raise BoardWorkerBlocked(RUNTIME_UNAVAILABLE, "board_worker_host_unavailable")
        job_owner = {"owner_kind": "user", "owner_principal_id": owner_principal_id, "session_id": owner_session_id,
                     "operator_session_id": owner_session_id}
        try:
            async with get_session() as db:
                try:
                    current = await authenticate_principal(owner_principal_id, db=db)
                except AuthFailure as exc:
                    raise NativeServiceBlocked("native_original_root_inactive") from exc
                owner_authority = {"principal": owner_principal_id, "owner_kind": "user",
                                   "session_id": owner_session_id, "grants": _grants(current.principal)}
                authority = {"authority_type": AUTHORITY_TYPE, **owner_authority, "preset_id": preset_id,
                             "preset_digest": digest, "parent_job_id": parent["job_id"],
                             "parent_lease_owner": parent["owner"], "parent_fence": int(parent["fence"])}
                await current_operator_root(db, SimpleNamespace(**job_owner), authority)
                await _parent_live(db, authority)
                binding = await bind_invocation(db, method="board-workers.startTurn", native_branch="board_workers",
                                                reviewed_composition=host.reviewed)
        except NativeServiceBlocked as exc:
            raise BoardWorkerBlocked(RUNTIME_UNAVAILABLE, exc.reason_code) from exc
        except CompositionBindingError as exc:
            raise BoardWorkerBlocked(RUNTIME_UNAVAILABLE, exc.reason_code) from exc
        if not any(item.runtime_domain == BOARD_WORKERS_DOMAIN and item.owner_kind == "cordis"
                   for item in binding.dependency_vector):
            raise BoardWorkerBlocked(RUNTIME_UNAVAILABLE, "board_workers_owner_not_cordis")
        key = uuid.uuid4().hex
        job_id = "cordis-board-worker:" + _digest({"preset": preset_id, "parent": parent["job_id"], "key": key})
        deadline = datetime.now(timezone.utc) + timedelta(seconds=float(timeout_seconds)) + SETTLEMENT_GRACE
        if parent.get("deadline") is not None:
            # A child never outlives the attempt root it belongs to.
            deadline = min(deadline, parent["deadline"])
        candidate = {"schema_version": 1, "preset_id": preset_id, "preset_digest": digest,
                     "timeout_seconds": float(timeout_seconds)}
        spec = DurableJobSpec(
            identity=DurableJobIdentity(job_id=job_id, owner_kind="user", owner_principal_id=owner_principal_id,
                                        job_kind=TURN_JOB_KIND, capability_version="1",
                                        idempotency_scope="cordis-board-worker-turn", idempotency_key=key),
            inputs=candidate, declared_authority=authority, deadline_at=deadline, max_attempts=1,
            composition_binding=binding, session_id=owner_session_id, operator_session_id=owner_session_id,
            parent_job_id=parent["job_id"], parent_fencing_token=int(parent["fence"]),
            run_fingerprint=_digest({"candidate": candidate, "kind": TURN_JOB_KIND, "key": key}))
        try:
            job = await jobs.admit_job(spec)
            queued = await jobs.transition_job(job_id, "queued", expected_revision=job["revision"])
            owner = "cordis-board-worker:" + host.boot_nonce[:24]
            lease = int((deadline - datetime.now(timezone.utc)).total_seconds()) + 1
            claimed = await jobs.claim_job(job_id, owner=owner, lease_seconds=max(lease, 1),
                                           expected_revision=queued["revision"])
        except DurableJobError as exc:
            # The attempt root moved before any contact: nothing ran.
            raise BoardWorkerBlocked(RUNTIME_UNAVAILABLE, "board_worker_parent_inactive") from exc
        turn = cls(job_id=job_id, owner=owner, fence=int(claimed["lease"]["fencing_token"]), host=host, jobs=jobs,
                   preset_id=preset_id, preset_digest=digest, deadline=deadline,
                   job_owner={**job_owner, "authority": owner_authority})
        await turn._write_context(initial=True)
        return turn

    # -------------------------------------------------------- fenced state

    async def _fetch_live(self, db, *, adopting: bool = False):
        from .dispatch import current_operator_root
        from .ownership import CompositionBindingError, RuntimeCompositionBinding, validate_invocation
        run = await self.jobs._fetch(db, self.job_id)
        now = datetime.now(timezone.utc)
        expiry = run.lease_expires_at
        if (run.status != "running" or run.lease_owner != self.owner or run.fencing_token != self.fence
                or expiry is None or expiry.replace(tzinfo=expiry.tzinfo or timezone.utc) <= now):
            raise BoardWorkerBlocked(SETTLEMENT_REFUSED, "board_worker_turn_attempt_changed")
        try:
            await validate_invocation(db, RuntimeCompositionBinding.from_json(run.composition_binding_json))
        except CompositionBindingError as exc:
            raise BoardWorkerBlocked(SETTLEMENT_REFUSED, "board_worker_turn_epoch_changed") from exc
        authority = json.loads(run.declared_authority_json or "{}")
        try:
            await _parent_live(db, authority)
            if adopting:
                # A revoked Root or changed grants adopt nothing; a blocked settlement still records the outcome.
                await current_operator_root(db, run, authority)
        except NativeServiceBlocked as exc:
            raise BoardWorkerBlocked(SETTLEMENT_REFUSED, exc.reason_code) from exc
        return run

    async def _write_context(self, *, initial: bool = False, settlement: dict | None = None, step: dict | None = None,
                             readback: dict | None = None):
        async with self.jobs._session() as db:
            await _writer(db)
            run = await self._fetch_live(db, adopting=(settlement or {}).get("outcome") == "succeeded")
            context = json.loads(run.checkpoint_context_json) if run.checkpoint_context_json else None
            if initial:
                context = {"schema_version": 1, "context_tag": CONTEXT_TAG, "preset_id": self.preset_id,
                           "preset_digest": self.preset_digest, "steps": [], "settlement": None}
            elif (type(context) is not dict or context.get("context_tag") != CONTEXT_TAG
                  or context.get("settlement") is not None):
                raise BoardWorkerBlocked(SETTLEMENT_REFUSED, "board_worker_turn_context_changed")
            if step is not None:
                context["steps"].append(step)
            if settlement is not None:
                context["settlement"] = settlement
            values = {"checkpoint_context_json": _canonical(context)}
            if readback is not None:
                values["effect_receipts_json"] = _append_receipt(run.effect_receipts_json, readback, key="effect_id")
            try:
                await cas_run(db, run, **values)
            except NativeServiceBlocked as exc:
                raise BoardWorkerBlocked(SETTLEMENT_REFUSED, "board_worker_turn_attempt_changed") from exc
            await db.commit()

    # -------------------------------------------------------- the one decision

    async def decide_final(self, output_digest: str) -> None:
        """One short sequencer invocation that may only stage ``settlement``."""
        from .bridge import HostBlocked
        from .service_jobs import _block, claim_service_job, open_invocation
        if datetime.now(timezone.utc) >= self.deadline:
            raise BoardWorkerBlocked(SETTLEMENT_REFUSED, "board_worker_turn_deadline_expired")
        projection = {"schema_version": 1, "kind": "final_answer", "step_index": 1, "max_steps": 1,
                      "preset_id": self.preset_id, "preset_digest": self.preset_digest,
                      "output_ref": "turn-output:" + hmac.new(self._ref_key, output_digest.encode(),
                                                              hashlib.sha256).hexdigest()[:48]}
        candidate = {"schema_version": 1, "method": "board-workers.startTurn", "turn_ref": self.job_id,
                     "projection": projection}
        scope = None
        try:
            job = await self._admit_step(candidate)
            owner, fence = await claim_service_job(job, host=self.host, jobs=self.jobs)
            scope = await open_invocation(job_id=job["job_id"], owner=owner, fence=fence, host=self.host,
                                          admission_replayed=False, candidate=candidate, jobs=self.jobs)
            result = await self.host.request_service("board-workers.startTurn", {"turn_ref": job["job_id"]},
                                                     original_scope=scope)
            if result["status"] != "succeeded":
                raise NativeServiceBlocked(result["reason_code"])
            async with self.jobs._session() as db:
                await _writer(db)
                run = await self.jobs._fetch(db, job["job_id"])
                decisions = [item["ref"] for item in read_context(run)["operations"]]
                echo = {"job_ref": run.run_identity, "revision": int(run.revision or 1), "state": run.status}
                if result["value"] != echo or decisions != ["settlement"]:
                    # Anything but Python's own single transition is never adopted.
                    raise NativeServiceBlocked("native_child_result_mismatch")
                digest = _digest({"projection": projection, "decisions": decisions})
                now = datetime.now(timezone.utc).isoformat()
                readback = {"effect_id": "cordis-board-worker-step:" + run.run_identity, "receipt_kind": "readback",
                            "effect_type": "cordis_board_worker_step", "status": "succeeded",
                            "target_path": "cordis-board-worker-step:" + digest, "content_sha256": digest,
                            "target_digest": digest, "verified_at": now, "recorded_at": now,
                            "readback_id": "cordis-board-worker-step-readback:" + run.run_identity,
                            "reconciled": True, "reconciliation_status": "resolved",
                            "fencing_token": run.fencing_token, "details": {"verified": True, "no_learning": True}}
                await cas_run(db, run, effect_receipts_json=_append_receipt(run.effect_receipts_json, readback,
                                                                             key="effect_id"))
                await db.commit()
            await self.jobs.transition_job(job["job_id"], "succeeded", owner=owner, fencing_token=fence,
                                           result={"no_learning": True, "decisions": decisions},
                                           result_summary="Cordis board worker step decided; no_learning")
        except (NativeServiceBlocked, HostBlocked) as exc:
            reason = getattr(exc, "reason_code", None) or "native_board_worker_host_unavailable"
            if scope is not None:
                await _block(self.jobs, scope, reason)
            raise BoardWorkerBlocked(SETTLEMENT_REFUSED, reason) from exc
        except asyncio.CancelledError:
            if scope is not None:
                await asyncio.shield(_block(self.jobs, scope, "native_board_worker_cancelled"))
            raise
        await self._write_context(step={"step_index": 1, "kind": "final_answer", "decisions": decisions,
                                        "step_job": job["job_id"]})
        self._output_digest = output_digest

    async def _admit_step(self, candidate: dict) -> dict:
        from src.db.engine import get_session
        from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec
        from .agent_turns import BOARD_WORKER_STEP_JOB_KIND
        from .ownership import CompositionBindingError, bind_invocation
        assert BOARD_WORKER_STEP_JOB_KIND == STEP_JOB_KIND
        try:
            async with get_session() as db:
                await self._fetch_live(db)
                binding = await bind_invocation(db, method="board-workers.startTurn", native_branch="board_workers",
                                                reviewed_composition=self.host.reviewed)
        except CompositionBindingError as exc:
            raise NativeServiceBlocked(exc.reason_code) from exc
        key = f"board-worker-step:{_sha(self.job_id)[:40]}:1"
        job_id = "cordis-svc:" + _digest({"board_worker_turn": self.job_id, "kind": STEP_JOB_KIND, "key": key})
        deadline = min(datetime.now(timezone.utc) + STEP_TIMEOUT, self.deadline)
        owner = {key: value for key, value in self.job_owner.items() if key != "authority"}
        authority = {"authority_type": AUTHORITY_TYPE, **self.job_owner["authority"], "turn_job_id": self.job_id}
        spec = DurableJobSpec(
            identity=DurableJobIdentity(job_id=job_id, owner_kind="user", owner_principal_id=owner["owner_principal_id"],
                                        job_kind=STEP_JOB_KIND, capability_version="1",
                                        idempotency_scope="cordis-service", idempotency_key=key),
            inputs=candidate, declared_authority=authority, deadline_at=deadline, max_attempts=1,
            composition_binding=binding, session_id=owner["session_id"],
            operator_session_id=owner["operator_session_id"],
            run_fingerprint=_digest({"candidate": candidate, "turn": self.job_id, "origin": "board-workers.startTurn",
                                     "deadline": deadline.isoformat()}))
        job = await self.jobs.admit_job(spec)
        if job["status"] != "accepted" or (job.get("receipt") or {}).get("status") == "deduped":
            raise NativeServiceBlocked("board_worker_turn_step_replayed")
        return job

    # -------------------------------------------------------- settlement

    async def settle(self, outcome: str, *, reason: str | None = None) -> bool:
        """Fenced settlement CAS, then the terminal transition. Returns whether it settled."""
        from src.workflows.job_runtime import DurableJobError
        if self.settled:
            return False
        readback = None
        if outcome == "succeeded":
            digest = self._output_digest
            if digest is None:
                self.blocked_reason = "board_worker_turn_not_decided"
                return False
            now = datetime.now(timezone.utc).isoformat()
            readback = {"effect_id": "cordis-board-worker-output:" + self.job_id, "receipt_kind": "readback",
                        "effect_type": "cordis_board_worker_output", "status": "succeeded",
                        "target_path": "cordis-board-worker-output:" + digest, "content_sha256": digest,
                        "target_digest": digest, "verified_at": now, "recorded_at": now,
                        "readback_id": "cordis-board-worker-output-readback:" + self.job_id, "reconciled": True,
                        "reconciliation_status": "resolved", "fencing_token": self.fence,
                        "details": {"verified": True, "no_learning": True}}
        try:
            await self._write_context(settlement={"outcome": outcome, "reason": reason}, readback=readback)
        except BoardWorkerBlocked as exc:
            # A stale owner never adopts output; stale-lease recovery owns the turn.
            self.blocked_reason = exc.detail
            return False
        self.settled = True
        terminal = "succeeded" if outcome == "succeeded" else "blocked"
        try:
            await self.jobs.transition_job(self.job_id, terminal, owner=self.owner, fencing_token=self.fence,
                                           reason=None if terminal == "succeeded" else (reason or outcome),
                                           result={"no_learning": True, "outcome": outcome},
                                           result_summary="Cordis board worker turn settled by its Python owner")
        except DurableJobError:
            logger.warning("Board worker turn %s settled but its terminal transition was refused", self.job_id,
                           exc_info=True)
        return True


# ---------------------------------------------------------------- the dispatcher's handle

class BoardWorkerRun:
    """One draft's runtime handle: a held legacy gate, or an admitted Cordis turn.

    ``adopt`` is called with the model output after the route receipt and
    before the output is parsed or anything is staged; ``abandon`` closes an
    admitted turn on any other exit; ``release`` always runs.
    """

    def __init__(self, *, turn: WorkerTurn | None = None, legacy_gate: bool = False):
        self.turn, self.legacy_gate = turn, legacy_gate
        self.loop = LOOP_NAMES["cordis"] if turn is not None else LOOP_NAMES["legacy"]

    async def adopt(self, output_text: str) -> None:
        if self.turn is None:
            return
        try:
            await self.turn.decide_final(_sha(output_text))
        except BoardWorkerBlocked as exc:
            await self.turn.settle("blocked", reason=exc.detail)
            raise
        if not await self.turn.settle("succeeded"):
            detail = self.turn.blocked_reason or "board_worker_turn_settlement_refused"
            # A refused adoption (a revoked Root) is still recorded; a lost lease leaves it to recovery.
            await self.turn.settle("blocked", reason=detail)
            raise BoardWorkerBlocked(SETTLEMENT_REFUSED, detail)

    async def abandon(self, reason: str) -> None:
        if self.turn is not None and not self.turn.settled:
            await asyncio.shield(self.turn.settle("blocked", reason=reason))

    def release(self) -> None:
        if self.legacy_gate:
            self.legacy_gate = False
            legacy_turns.leave(BOARD_WORKERS_DOMAIN)


async def begin_board_worker(preset_id: str, *, owner_principal_id: str, owner_session_id: str, parent: dict,
                             timeout_seconds: float, host=None, jobs=None) -> BoardWorkerRun:
    """Route one draft before its first provider contact.

    A legacy owner holds the drain gate and runs today's path. A Cordis owner
    admits the worker turn. Anything else refuses the draft with
    ``board_worker_runtime_unavailable``; nothing has been contacted yet.
    """
    from .ownership import CompositionBindingError
    status = await board_worker_runtime_status(host)
    if status["loop"] == LOOP_NAMES["legacy"]:
        try:
            legacy_turns.enter(BOARD_WORKERS_DOMAIN)
        except CompositionBindingError as exc:
            raise BoardWorkerBlocked(RUNTIME_UNAVAILABLE, exc.reason_code,
                                     "The draft runtime owner is changing; retry in a moment.") from exc
        # The owner may have moved between the read and the gate; the gate now holds it.
        status = await board_worker_runtime_status(host)
        if status["loop"] == LOOP_NAMES["legacy"]:
            return BoardWorkerRun(legacy_gate=True)
        legacy_turns.leave(BOARD_WORKERS_DOMAIN)
    if status["loop"] != LOOP_NAMES["cordis"]:
        raise BoardWorkerBlocked(RUNTIME_UNAVAILABLE, status["reason"], status.get("recovery"))
    turn = await WorkerTurn.admit(preset_id=preset_id, owner_principal_id=owner_principal_id,
                                  owner_session_id=owner_session_id, parent=parent, timeout_seconds=timeout_seconds,
                                  host=host, jobs=jobs)
    return BoardWorkerRun(turn=turn)


def output_text(raw) -> str:
    """The text a draft model call returned, whatever shape the model owner used."""
    if isinstance(raw, str):
        return raw
    try:
        return str(raw.choices[0].message.content or "")
    except (AttributeError, IndexError, KeyError, TypeError):
        pass
    content = getattr(raw, "content", None)
    return str(content) if content is not None else str(raw)


__all__ = ["BOARD_WORKERS_DOMAIN", "RUNTIME_UNAVAILABLE", "SETTLEMENT_REFUSED", "BoardWorkerBlocked", "BoardWorkerRun",
           "WorkerTurn", "begin_board_worker", "board_worker_runtime_status", "current_board_worker_authority",
           "output_text"]
