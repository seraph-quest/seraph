"""Unattended guardian turns on the Cordis loop (#1020).

Scheduled guardian work (the strategist decision, the scheduled reports and
memory consolidation) has no operator present. Each model call becomes one
durable ``cordis_guardian_turn_v1`` job under its reviewed unattended preset,
owned by the guardian service. A memory flush that runs inside an operator's
turn (post-response, compaction, session end, workflow completion) is the one
other admission: the same turn, owned by that operator and bound to its Root.

* Python makes exactly today's call: the same messages, model owner, runtime
  path, principal, broker and ledger binding. The turn adds lineage only.
* One short ``guardian.startTurn`` step then asks the reviewed sequencer to
  settle. The child sees only a sealed projection (preset, kind, step index)
  and may stage only ``settlement``; Python accepts nothing else.
* The output is returned to its caller only after the turn settled while the
  turn, its scheduler parent (if any) and the composition epoch were all
  still current. Otherwise the caller gets ``GuardianBlocked`` and delivers
  nothing. Restart never resumes a turn.

Unattended presets hold no tools (``require_unattended``), so no approval can
arise and no approval row is ever created by these turns.

Routing follows the ``seraph.guardian.v1`` owner row. A legacy owner runs
today's path; a Cordis owner that is not ready, or whose host or presets are
unavailable, blocks the call visibly and never falls back.
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
GUARDIAN_DOMAIN = "seraph.guardian.v1"
PRESETS_DOMAIN = "seraph.agent-presets.v1"
TURN_JOB_KIND = "cordis_guardian_turn_v1"
STEP_JOB_KIND = "cordis_guardian_step_v1"
GUARDIAN_SERVICE = "service:guardian"
AUTHORITY_TYPE = "guardian_turn_v1"
CONTEXT_TAG = "cordis-guardian-turn.v1"
STEP_TIMEOUT = timedelta(seconds=30)
SETTLEMENT_GRACE = timedelta(seconds=30)
LOOP_NAMES = {"cordis": "cordis.guardian.v1", "legacy": "legacy.guardian", "blocked": "blocked"}


class GuardianBlocked(Exception):
    """The guardian call did not run or its output may not be used; the reason is operator-visible."""

    def __init__(self, reason_code: str, recovery: str | None = None):
        self.reason_code = reason_code
        self.recovery = recovery or guardian_recovery(reason_code)
        super().__init__(reason_code)


def guardian_recovery(reason: str) -> str:
    if reason.startswith("cordis_") or reason == "guardian_host_unavailable":
        return "Start the Cordis runtime (npm ci --prefix runtime/cordis --ignore-scripts, then ./manage.sh local up)."
    if reason == "composition_dependency_missing":
        return "Restart Seraph to seed the runtime inventory."
    if reason.startswith("guardian_owner_") or reason.startswith("agent_presets_owner_"):
        return "Finish the guardian owner transition (cutover or rollback) at a higher epoch."
    return "The next scheduled run tries again; no external effect was made."


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- routing and status

async def guardian_runtime_status(host=None) -> dict:
    """Which path guardian calls take now, and the exact fix when they cannot run."""
    from src.db.engine import get_session
    from .bridge import cordis_host, cordis_recovery
    from .ownership import CompositionBindingError, inventory
    from sqlalchemy.exc import OperationalError
    host = host or cordis_host
    try:
        async with get_session() as db:
            rows = await inventory(db)
    except CompositionBindingError:
        return {"loop": LOOP_NAMES["blocked"], "owner_epoch": None, "reason": "guardian_inventory_invalid",
                "recovery": "Repair the runtime composition inventory (a malformed owner row fails closed)."}
    except OperationalError as exc:
        # A database that predates the inventory table has never been cut over;
        # any other storage fault propagates.
        if "no such table: runtime_composition_states" not in str(exc):
            raise
        rows = {}
    if not rows:
        # An entirely unseeded inventory has never been cut over.
        return {"loop": LOOP_NAMES["legacy"], "owner_epoch": None, "reason": None, "recovery": None}
    row = rows.get(GUARDIAN_DOMAIN)
    if row is None:
        return {"loop": LOOP_NAMES["blocked"], "owner_epoch": None, "reason": "composition_dependency_missing",
                "recovery": guardian_recovery("composition_dependency_missing")}
    if row.state != "ready":
        reason = f"guardian_owner_{row.state}"
        return {"loop": LOOP_NAMES["blocked"], "owner_epoch": row.epoch, "reason": reason,
                "recovery": guardian_recovery(reason)}
    if row.owner_kind == "legacy":
        return {"loop": LOOP_NAMES["legacy"], "owner_epoch": row.epoch, "reason": None, "recovery": None}
    presets = rows.get(PRESETS_DOMAIN)
    if presets is None or presets.state != "ready":
        reason = "composition_dependency_missing" if presets is None else f"agent_presets_owner_{presets.state}"
        return {"loop": LOOP_NAMES["blocked"], "owner_epoch": row.epoch, "reason": reason,
                "recovery": guardian_recovery(reason)}
    if not host.admitting or host.reviewed is None:
        snapshot = host.snapshot()
        return {"loop": LOOP_NAMES["blocked"], "owner_epoch": row.epoch,
                "reason": "cordis_" + str(snapshot.get("reason") or "host_unavailable"),
                "recovery": snapshot.get("recovery") or cordis_recovery("blocked", snapshot.get("reason"))}
    return {"loop": LOOP_NAMES["cordis"], "owner_epoch": row.epoch, "reason": None, "recovery": None}


async def runtime_domains_status(host=None) -> list[dict]:
    """Every runtime domain owner row, with the operator-visible reason a non-ready one blocks."""
    from src.db.engine import get_session
    from .ownership import DOMAINS, CompositionBindingError, inventory
    try:
        async with get_session() as db:
            rows = await inventory(db)
    except CompositionBindingError:
        return [{"domain": domain, "owner_kind": None, "epoch": None, "state": "blocked",
                 "reason": "composition_inventory_invalid",
                 "recovery": "Repair the runtime composition inventory (a malformed owner row fails closed)."}
                for domain in DOMAINS]
    result = []
    for domain in DOMAINS:
        row = rows.get(domain)
        if row is None:
            result.append({"domain": domain, "owner_kind": None, "epoch": None,
                           "state": "missing" if rows else "unseeded",
                           "reason": "composition_dependency_missing" if rows else None,
                           "recovery": guardian_recovery("composition_dependency_missing") if rows else None})
            continue
        reason = None if row.state == "ready" else f"owner_{row.state}"
        result.append({"domain": domain, "owner_kind": row.owner_kind, "epoch": row.epoch, "state": row.state,
                       "reason": reason,
                       "recovery": None if reason is None
                       else "Finish the owner transition (cutover or rollback) at a higher epoch."})
    return result


# ---------------------------------------------------------------- authority

async def current_guardian_authority(db, run, authority: dict) -> None:
    """The live unsettled turn (and its live scheduler parent) is an unattended turn's only authority.

    A turn admitted under an operator Root (a memory flush inside an operator's
    turn) also needs that Root live with unchanged grants, exactly as a chat turn.
    """
    if run.job_kind not in {TURN_JOB_KIND, STEP_JOB_KIND}:
        raise NativeServiceBlocked("native_guardian_lineage_changed")
    if run.owner_kind == "user":
        if run.owner_principal_id != authority.get("principal") or run.service_id is not None:
            raise NativeServiceBlocked("native_guardian_lineage_changed")
        from .dispatch import current_operator_root
        await current_operator_root(db, run, authority)
    elif (run.owner_kind != "service" or run.owner_principal_id != GUARDIAN_SERVICE
            or run.service_id != GUARDIAN_SERVICE):
        raise NativeServiceBlocked("native_guardian_lineage_changed")
    turn_id = authority.get("turn_job_id") if run.job_kind == STEP_JOB_KIND else run.run_identity
    from src.db.models import WorkflowRunState
    from sqlalchemy import select
    turn = run if turn_id == run.run_identity else await db.scalar(
        select(WorkflowRunState).where(WorkflowRunState.run_identity == turn_id)
        .execution_options(populate_existing=True))
    now = datetime.now(timezone.utc)
    if (turn is None or turn.job_kind != TURN_JOB_KIND or turn.status != "running"
            or turn.lease_expires_at is None
            or turn.lease_expires_at.replace(tzinfo=turn.lease_expires_at.tzinfo or timezone.utc) <= now):
        raise NativeServiceBlocked("native_guardian_turn_inactive")
    turn_authority = json.loads(turn.declared_authority_json or "{}")
    context = json.loads(turn.checkpoint_context_json) if turn.checkpoint_context_json else {}
    if turn_authority.get("authority_type") != AUTHORITY_TYPE or (context or {}).get("settlement") is not None:
        raise NativeServiceBlocked("native_guardian_turn_inactive")
    await _parent_live(db, turn_authority)


async def _parent_live(db, authority: dict) -> None:
    parent_id = authority.get("parent_job_id")
    if parent_id is None:
        return
    from src.db.models import WorkflowRunState
    from sqlalchemy import select
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == parent_id)
                             .execution_options(populate_existing=True))
    now = datetime.now(timezone.utc)
    if (parent is None or parent.status != "running" or parent.lease_owner != authority.get("parent_lease_owner")
            or parent.fencing_token != authority.get("parent_fence") or parent.lease_expires_at is None
            or parent.lease_expires_at.replace(tzinfo=parent.lease_expires_at.tzinfo or timezone.utc) <= now):
        raise NativeServiceBlocked("native_guardian_parent_inactive")


# ---------------------------------------------------------------- the turn

class GuardianTurn:
    """Python owner of one unattended guardian turn."""

    def __init__(self, *, job_id: str, owner: str, fence: int, host, jobs, preset_id: str, preset_digest: str,
                 deadline: datetime, job_owner: dict):
        self.job_id, self.owner, self.fence = job_id, owner, fence
        # Who owns the turn's rows: the guardian service, or the operator Root it was admitted under.
        self.job_owner = job_owner
        self.host, self.jobs, self.preset_id, self.preset_digest = host, jobs, preset_id, preset_digest
        self.deadline = deadline
        self.settled = False
        self.blocked_reason: str | None = None
        self._ref_key = secrets.token_bytes(32)

    @classmethod
    async def admit(cls, *, preset_id: str, timeout_seconds: float, parent: dict | None = None,
                    root=None, host=None, jobs=None) -> "GuardianTurn":
        """Admit one unattended turn.

        ``root`` is the operator principal of a trigger that runs inside an
        operator's turn; the turn is then owned by that operator and bound to
        its Root. Without one (a scheduled run) the guardian service owns it.
        """
        from src.agent.presets import PresetError, preset_digest, require_unattended
        from src.approval.runtime import get_current_trust_principal
        from src.db.engine import get_session
        from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec
        from .bridge import cordis_host
        from .ownership import CompositionBindingError, bind_invocation
        from .service_jobs import _grants, _jobs
        host, jobs = host or cordis_host, _jobs(jobs)
        try:
            require_unattended(preset_id)
            digest = preset_digest(preset_id)
        except PresetError as exc:
            raise GuardianBlocked(exc.reason_code) from exc
        if not host.admitting or host.reviewed is None:
            raise GuardianBlocked("guardian_host_unavailable")
        principal = get_current_trust_principal()
        root = _operator_root(root)
        if root is None:
            owner_authority = {"principal": GUARDIAN_SERVICE, "owner_kind": "service", "service_id": GUARDIAN_SERVICE}
            job_owner = {"owner_kind": "service", "owner_principal_id": GUARDIAN_SERVICE,
                         "service_id": GUARDIAN_SERVICE}
        else:
            owner_authority = {"principal": root.principal_id, "owner_kind": "user",
                               "session_id": root.operator_session_id, "grants": _grants(root)}
            job_owner = {"owner_kind": "user", "owner_principal_id": root.principal_id,
                         "session_id": root.operator_session_id, "operator_session_id": root.operator_session_id}
        authority = {"authority_type": AUTHORITY_TYPE, **owner_authority, "preset_id": preset_id,
                     "preset_digest": digest,
                     # Lineage only: the inference keeps today's principal and ledger owner.
                     "inference_principal_id": getattr(principal, "principal_id", None),
                     "parent_job_id": (parent or {}).get("job_id"),
                     "parent_lease_owner": (parent or {}).get("owner"), "parent_fence": (parent or {}).get("fence")}
        try:
            async with get_session() as db:
                await _parent_live(db, authority)
                if root is not None:
                    # The same Root check every callback of a user-owned job repeats.
                    from .dispatch import current_operator_root
                    await current_operator_root(db, SimpleNamespace(**job_owner), authority)
                binding = await bind_invocation(db, method="guardian.startTurn", native_branch="guardian",
                                                reviewed_composition=host.reviewed)
        except NativeServiceBlocked as exc:
            raise GuardianBlocked("guardian_parent_inactive" if exc.reason_code.startswith("native_guardian")
                                  else exc.reason_code) from exc
        except CompositionBindingError as exc:
            raise GuardianBlocked(exc.reason_code) from exc
        if not any(item.runtime_domain == GUARDIAN_DOMAIN and item.owner_kind == "cordis"
                   for item in binding.dependency_vector):
            raise GuardianBlocked("guardian_owner_not_cordis")
        key = uuid.uuid4().hex
        job_id = "cordis-guardian:" + _digest({"preset": preset_id, "key": key})
        deadline = datetime.now(timezone.utc) + timedelta(seconds=float(timeout_seconds)) + SETTLEMENT_GRACE
        candidate = {"schema_version": 1, "preset_id": preset_id, "preset_digest": digest,
                     "timeout_seconds": float(timeout_seconds)}
        spec = DurableJobSpec(
            identity=DurableJobIdentity(job_id=job_id, owner_kind=job_owner["owner_kind"],
                                        owner_principal_id=job_owner["owner_principal_id"],
                                        job_kind=TURN_JOB_KIND, capability_version="1",
                                        idempotency_scope="cordis-guardian-turn", idempotency_key=key),
            inputs=candidate, declared_authority=authority, deadline_at=deadline, max_attempts=1,
            composition_binding=binding, **_spec_owner(job_owner),
            run_fingerprint=_digest({"candidate": candidate, "kind": TURN_JOB_KIND, "key": key}))
        job = await jobs.admit_job(spec)
        queued = await jobs.transition_job(job_id, "queued", expected_revision=job["revision"])
        owner = "cordis-guardian:" + host.boot_nonce[:24]
        lease = int((deadline - datetime.now(timezone.utc)).total_seconds()) + 1
        claimed = await jobs.claim_job(job_id, owner=owner, lease_seconds=max(lease, 1),
                                       expected_revision=queued["revision"])
        turn = cls(job_id=job_id, owner=owner, fence=int(claimed["lease"]["fencing_token"]), host=host, jobs=jobs,
                   preset_id=preset_id, preset_digest=digest, deadline=deadline,
                   job_owner={**job_owner, "authority": owner_authority})
        await turn._write_context(initial=True)
        return turn

    # -------------------------------------------------------- fenced state

    async def _fetch_live(self, db, *, adopting: bool = False):
        from .ownership import CompositionBindingError, RuntimeCompositionBinding, validate_invocation
        run = await self.jobs._fetch(db, self.job_id)
        now = datetime.now(timezone.utc)
        expiry = run.lease_expires_at
        if (run.status != "running" or run.lease_owner != self.owner or run.fencing_token != self.fence
                or expiry is None or expiry.replace(tzinfo=expiry.tzinfo or timezone.utc) <= now):
            raise GuardianBlocked("guardian_turn_attempt_changed")
        try:
            await validate_invocation(db, RuntimeCompositionBinding.from_json(run.composition_binding_json))
        except CompositionBindingError as exc:
            raise GuardianBlocked("guardian_turn_epoch_changed") from exc
        authority = json.loads(run.declared_authority_json or "{}")
        try:
            await _parent_live(db, authority)
        except NativeServiceBlocked as exc:
            raise GuardianBlocked("guardian_parent_inactive") from exc
        if adopting and run.owner_kind == "user":
            # An operator-owned turn adopts nothing once its Root is revoked or its grants changed;
            # a blocked settlement still records the outcome.
            from .dispatch import current_operator_root
            try:
                await current_operator_root(db, run, authority)
            except NativeServiceBlocked as exc:
                raise GuardianBlocked(exc.reason_code) from exc
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
                raise GuardianBlocked("guardian_turn_context_changed")
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
                raise GuardianBlocked("guardian_turn_attempt_changed") from exc
            await db.commit()

    # -------------------------------------------------------- the one decision

    async def decide_final(self, output_digest: str) -> None:
        """One short sequencer invocation that may only stage ``settlement``."""
        from .bridge import HostBlocked
        from .service_jobs import _block, claim_service_job, open_invocation
        if datetime.now(timezone.utc) >= self.deadline:
            raise GuardianBlocked("guardian_turn_deadline_expired")
        projection = {"schema_version": 1, "kind": "final_answer", "step_index": 1, "max_steps": 1,
                      "preset_id": self.preset_id, "preset_digest": self.preset_digest,
                      "output_ref": "turn-output:" + hmac.new(self._ref_key, output_digest.encode(),
                                                              hashlib.sha256).hexdigest()[:48]}
        candidate = {"schema_version": 1, "method": "guardian.startTurn", "turn_ref": self.job_id,
                     "projection": projection}
        try:
            job = await self._admit_step(candidate)
            owner, fence = await claim_service_job(job, host=self.host, jobs=self.jobs)
            scope = await open_invocation(job_id=job["job_id"], owner=owner, fence=fence, host=self.host,
                                          admission_replayed=False, candidate=candidate, jobs=self.jobs)
        except NativeServiceBlocked as exc:
            raise GuardianBlocked(exc.reason_code) from exc
        try:
            result = await self.host.request_service("guardian.startTurn", {"turn_ref": job["job_id"]},
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
                readback = {"effect_id": "cordis-guardian-step:" + run.run_identity, "receipt_kind": "readback",
                            "effect_type": "cordis_guardian_step", "status": "succeeded",
                            "target_path": "cordis-guardian-step:" + digest, "content_sha256": digest,
                            "target_digest": digest, "verified_at": now, "recorded_at": now,
                            "readback_id": "cordis-guardian-step-readback:" + run.run_identity, "reconciled": True,
                            "reconciliation_status": "resolved", "fencing_token": run.fencing_token,
                            "details": {"verified": True, "no_learning": True}}
                await cas_run(db, run, effect_receipts_json=_append_receipt(run.effect_receipts_json, readback,
                                                                             key="effect_id"))
                await db.commit()
            await self.jobs.transition_job(job["job_id"], "succeeded", owner=owner, fencing_token=fence,
                                           result={"no_learning": True, "decisions": decisions},
                                           result_summary="Cordis guardian step decided; no_learning")
        except (NativeServiceBlocked, HostBlocked) as exc:
            reason = getattr(exc, "reason_code", None) or "native_guardian_host_unavailable"
            await _block(self.jobs, scope, reason)
            raise GuardianBlocked(reason) from exc
        except asyncio.CancelledError:
            await asyncio.shield(_block(self.jobs, scope, "native_guardian_cancelled"))
            raise
        await self._write_context(step={"step_index": 1, "kind": "final_answer", "decisions": decisions,
                                        "step_job": job["job_id"]})
        self._output_digest = output_digest

    async def _admit_step(self, candidate: dict) -> dict:
        from src.db.engine import get_session
        from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec
        from .agent_turns import GUARDIAN_STEP_JOB_KIND
        from .ownership import CompositionBindingError, bind_invocation
        assert GUARDIAN_STEP_JOB_KIND == STEP_JOB_KIND
        try:
            async with get_session() as db:
                await self._fetch_live(db)
                binding = await bind_invocation(db, method="guardian.startTurn", native_branch="guardian",
                                                reviewed_composition=self.host.reviewed)
        except CompositionBindingError as exc:
            raise NativeServiceBlocked(exc.reason_code) from exc
        key = f"guardian-step:{_sha(self.job_id)[:40]}:1"
        job_id = "cordis-svc:" + _digest({"guardian_turn": self.job_id, "kind": STEP_JOB_KIND, "key": key})
        deadline = min(datetime.now(timezone.utc) + STEP_TIMEOUT, self.deadline)
        owner = {key: value for key, value in self.job_owner.items() if key != "authority"}
        authority = {"authority_type": AUTHORITY_TYPE, **self.job_owner["authority"], "turn_job_id": self.job_id}
        spec = DurableJobSpec(
            identity=DurableJobIdentity(job_id=job_id, owner_kind=owner["owner_kind"],
                                        owner_principal_id=owner["owner_principal_id"],
                                        job_kind=STEP_JOB_KIND, capability_version="1",
                                        idempotency_scope="cordis-service", idempotency_key=key),
            inputs=candidate, declared_authority=authority, deadline_at=deadline, max_attempts=1,
            composition_binding=binding, **_spec_owner(owner),
            run_fingerprint=_digest({"candidate": candidate, "turn": self.job_id, "origin": "guardian.startTurn",
                                     "deadline": deadline.isoformat()}))
        job = await self.jobs.admit_job(spec)
        if job["status"] != "accepted" or (job.get("receipt") or {}).get("status") == "deduped":
            raise NativeServiceBlocked("guardian_turn_step_replayed")
        return job

    # -------------------------------------------------------- settlement

    async def settle(self, outcome: str, *, reason: str | None = None) -> bool:
        """Fenced settlement CAS, then the terminal transition. Returns whether it settled."""
        from src.workflows.job_runtime import DurableJobError
        if self.settled:
            return False
        readback = None
        if outcome == "succeeded":
            # The sealed output digest the sequencer settled is the turn's verified readback.
            digest = getattr(self, "_output_digest", None)
            if digest is None:
                self.blocked_reason = "guardian_turn_not_decided"
                return False
            now = datetime.now(timezone.utc).isoformat()
            readback = {"effect_id": "cordis-guardian-output:" + self.job_id, "receipt_kind": "readback",
                        "effect_type": "cordis_guardian_output", "status": "succeeded",
                        "target_path": "cordis-guardian-output:" + digest, "content_sha256": digest,
                        "target_digest": digest, "verified_at": now, "recorded_at": now,
                        "readback_id": "cordis-guardian-output-readback:" + self.job_id, "reconciled": True,
                        "reconciliation_status": "resolved", "fencing_token": self.fence,
                        "details": {"verified": True, "no_learning": True}}
        try:
            await self._write_context(settlement={"outcome": outcome, "reason": reason}, readback=readback)
        except GuardianBlocked as exc:
            # A stale owner never adopts output; stale-lease recovery owns the turn.
            self.blocked_reason = exc.reason_code
            return False
        self.settled = True
        terminal = "succeeded" if outcome == "succeeded" else "blocked"
        try:
            await self.jobs.transition_job(self.job_id, terminal, owner=self.owner, fencing_token=self.fence,
                                           reason=None if terminal == "succeeded" else (reason or outcome),
                                           result={"no_learning": True, "outcome": outcome},
                                           result_summary="Cordis guardian turn settled by its Python owner")
        except DurableJobError:
            logger.warning("Guardian turn %s settled but its terminal transition was refused", self.job_id, exc_info=True)
        return True


def _operator_root(principal):
    """The authenticated operator principal a trigger runs under, if it has a Root."""
    from src.security.trust_contract import PrincipalType
    if principal is None:
        return None
    try:
        kind = PrincipalType(principal.principal_type)
    except (TypeError, ValueError):
        return None
    if (kind is not PrincipalType.OPERATOR or not principal.authenticated or principal.revoked
            or not principal.principal_id or not principal.operator_session_id):
        return None
    return principal


def _spec_owner(owner: dict) -> dict:
    """The DurableJobSpec ownership fields of a service- or operator-owned turn."""
    if owner["owner_kind"] == "service":
        return {"service_id": owner["service_id"]}
    return {"session_id": owner["session_id"], "operator_session_id": owner["operator_session_id"]}


def _response_text(response) -> str:
    try:
        return str(response.choices[0].message.content or "")
    except (AttributeError, IndexError, KeyError, TypeError):
        return str(response)


async def guardian_completion(preset_id: str, call, *, timeout_seconds: float, runtime_path: str,
                              temperature: float, max_tokens: int, parent: dict | None = None,
                              root=None, host=None, jobs=None):
    """Run one guardian model call on the path its owner row selects.

    ``call`` is today's exact completion coroutine factory. On the legacy owner
    it simply runs. On the Cordis owner it runs inside one unattended turn and
    its response is returned only after that turn settled; otherwise
    ``GuardianBlocked`` is raised and the caller delivers nothing.
    """
    from src.agent.presets import PresetError, require_call
    try:
        # A preset never widens a call: today's parameters must be exactly the preset's.
        require_call(preset_id, runtime_path=runtime_path, temperature=temperature, max_tokens=max_tokens)
    except PresetError as exc:
        raise GuardianBlocked(exc.reason_code) from exc
    status = await guardian_runtime_status(host)
    if status["loop"] == LOOP_NAMES["legacy"]:
        # A legacy call is not bound to the owner row, so it holds the drain gate:
        # a boot cutover defers while it runs, and none starts during one.
        from .ownership import CompositionBindingError
        try:
            legacy_turns.enter(GUARDIAN_DOMAIN)
        except CompositionBindingError as exc:
            raise GuardianBlocked(exc.reason_code) from exc
        try:
            return await call()
        finally:
            legacy_turns.leave(GUARDIAN_DOMAIN)
    if status["loop"] != LOOP_NAMES["cordis"]:
        raise GuardianBlocked(status["reason"], status.get("recovery"))
    from .bridge import cordis_host
    owner_loop = getattr(host or cordis_host, "owner_loop", None)
    if owner_loop is not None and owner_loop is not asyncio.get_running_loop():
        # A sync caller (a workflow flush) runs its own loop in a worker thread;
        # the turn's child traffic must use the loop that owns the child's pipes.
        return await _on_loop(owner_loop, lambda: _cordis_completion(preset_id, call, timeout_seconds=timeout_seconds,
                                                                     parent=parent, root=root, host=host, jobs=jobs))
    return await _cordis_completion(preset_id, call, timeout_seconds=timeout_seconds, parent=parent, root=root,
                                    host=host, jobs=jobs)


async def _on_loop(loop, factory):
    """Run ``factory()`` on ``loop`` with the caller's context (principal, request id)."""
    import concurrent.futures
    import contextvars
    context = contextvars.copy_context()
    done: concurrent.futures.Future = concurrent.futures.Future()

    def start():
        task = loop.create_task(factory(), context=context)

        def finish(item):
            if item.cancelled():
                done.cancel()
            elif item.exception() is not None:
                done.set_exception(item.exception())
            else:
                done.set_result(item.result())
        task.add_done_callback(finish)
    loop.call_soon_threadsafe(start)
    return await asyncio.wrap_future(done)


async def _cordis_completion(preset_id, call, *, timeout_seconds, parent, root, host, jobs):
    turn = await GuardianTurn.admit(preset_id=preset_id, timeout_seconds=timeout_seconds, parent=parent, root=root,
                                    host=host, jobs=jobs)
    try:
        response = await call()
    except BaseException as exc:
        reason = "guardian_call_timed_out" if isinstance(exc, asyncio.TimeoutError) else "guardian_call_failed"
        await asyncio.shield(turn.settle("blocked", reason=reason))
        raise
    try:
        await turn.decide_final(_sha(_response_text(response)))
    except GuardianBlocked as exc:
        await turn.settle("blocked", reason=exc.reason_code)
        raise
    if not await turn.settle("succeeded"):
        reason = turn.blocked_reason or "guardian_turn_settlement_refused"
        # Refused adoption (a revoked Root) is still recorded; a lost lease leaves it to recovery.
        await turn.settle("blocked", reason=reason)
        raise GuardianBlocked(reason)
    return response


def guardian_completion_fn(preset_id: str, completion_fn, *, root=None):
    """Wrap a ``completion_fn(**kwargs)`` dependency so its one call runs on the guardian loop.

    ``root`` is the operator principal of a trigger inside an operator's turn
    (the user-owned admission); without one the turn is the guardian service's.
    """
    async def guarded(**kwargs):
        return await guardian_completion(
            preset_id, lambda: completion_fn(**kwargs), timeout_seconds=float(kwargs["timeout"]),
            runtime_path=kwargs["runtime_path"], temperature=kwargs["temperature"], max_tokens=kwargs["max_tokens"],
            root=root)
    return guarded
