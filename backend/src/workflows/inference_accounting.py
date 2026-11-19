"""Repository-owned request records; the remote broker alone executes providers.

ADR-037 (#1284): Seraph keeps no cost limit. Each provider call has one durable
request record: it gives at-most-once execution, binds the consent revision
rechecked at contact, keeps a contact denial durable across restarts, and
records the charge NEAR reports, as history and display only.

The external lifecycle witness is written before transaction commit. Any crash
between those two writes deliberately blocks egress until reconciliation. This
is a continuity fence, not host-administrator tamper resistance.

Ledger verification is bounded (#1086, ``src.workflows.inference_ledger``):
hot writers check the witness, the root of stored row digests and every row
they read or change, and admit against running totals sealed in the account
record; the whole ledger is re-verified on the first writer of each process,
whenever the last full pass is over 20 minutes old, on rare writers, at
startup recovery and every 10 minutes in the background.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from sqlalchemy import text, inspect
from sqlmodel import select

from config.settings import settings
from src.db.models import InferenceAccountingOwner, InferenceCostReservation, WorkflowRunState, ModelRouteAttemptReceiptRecord
from src.workspace.production import (
    CANONICAL_CONTAINER_WORKSPACE, ProductionWorkspace, read_lifecycle_receipt,
    write_lifecycle_receipt,
    write_accounting_checkpoint,
)


ACCOUNTING_LEDGER_UNINITIALIZED = "accounting_ledger_uninitialized"
_LEDGER_CONTEXT_KEY = "inference_ledger_context"
_RESEAL_PENDING_KEY = "inference_ledger_reseal_pending"


class InferenceAccountingError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_CONTACT_DENIAL_SEAL = object()
_DENIAL_QUIESCENCE_SEAL = object()


class InferenceProviderContactDenied(InferenceAccountingError):
    """A sealed committed writer result, subsequently bound to its broker handle."""

    def __init__(self, code, *, _seal=None, _binding=None, _witness=None, _root_job_id=None, _live_root_digest=None):
        super().__init__(code)
        self._seal = _seal
        self._binding = _binding
        self._witness = _witness
        self._broker_request = None
        self._root_job_id, self._live_root_digest = _root_job_id, _live_root_digest

    def bind_broker_handle(self, handle):
        expected = (handle.request.operation_id, handle.job_id, handle.request.owner_id,
            handle.request.data_digest, handle.policy_digest, handle.owner, handle.fence)
        if (self._seal is _CONTACT_DENIAL_SEAL and self._binding == expected
            and isinstance(self._witness, dict) and self._witness.get("ledger_digest")):
            self._broker_request = handle.request


def proven_contact_denial_for_request(error, request) -> bool:
    return (isinstance(error, InferenceProviderContactDenied)
        and error._seal is _CONTACT_DENIAL_SEAL and error._broker_request is request)


class _DenialQuiescenceProof:
    def __init__(self, *, _seal=None, denial=None, receipt=None):
        self._seal, self.denial, self.receipt = _seal, denial, receipt


def _completed_denial_quiescence(denial, request, broker):
    """Called only by broker teardown with its actual closed operation object."""
    with broker._condition:
        operation = broker._operations.get(request.operation_id)
        if (not proven_contact_denial_for_request(denial, request) or operation is None
            or operation.request is not request or not operation.callback_completed
            or operation.status != "failed" or operation.reconciliation_required
            or broker._active_operation_id == request.operation_id):
            return None
        return _DenialQuiescenceProof(_seal=_DENIAL_QUIESCENCE_SEAL, denial=denial,
            receipt={"operation_id": request.operation_id, "job_id": request.job_id,
                "owner_id": request.owner_id, "broker_fence": operation.fencing_token,
                "callback_completed": True, "provider_never_contacted": True})


def integer_amount(value: object, *, positive: bool = False) -> int:
    if type(value) is not int or not (1 if positive else 0) <= value <= 1_000_000_000:
        raise InferenceAccountingError("accounting_amount_invalid")
    return value


def account_charge_microusd(payload: object) -> tuple[int | None, str | None]:
    """Use account usage.cost only; absence never means a zero-dollar call."""
    if isinstance(payload, tuple):
        payload = next((item for item in reversed(payload) if isinstance(item, Mapping)), None)
    if not isinstance(payload, Mapping):
        return None, None
    operation = payload.get("id")
    operation_id = operation if isinstance(operation, str) and re.fullmatch(r"(?:gen|embd)-[A-Za-z0-9_-]{1,240}", operation) else None
    usage = payload.get("usage")
    if not isinstance(usage, Mapping) or "cost" not in usage:
        return None, operation_id
    if usage.get("currency", "USD") != "USD" or payload.get("currency", "USD") != "USD":
        return None, operation_id
    value = usage["cost"]
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None, operation_id
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal(1000):
            return None, operation_id
        # Multiplication must not round away a positive fractional micro-dollar
        # before the authoritative upward rounding. Preserve every input digit.
        with localcontext() as context:
            context.prec = max(28, len(amount.as_tuple().digits) + 7)
            return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING)), operation_id
    except (ValueError, InvalidOperation, OverflowError):
        return None, operation_id


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def period_id(now: datetime) -> str:
    from src.workspace.accounting_witness import utc_period
    return utc_period(_utc(now))


def _operation_payload(row: InferenceCostReservation) -> dict[str, object]:
    return row.model_dump(mode="json", exclude={"group_lookup_key"})


def _provider_contact_denied(row: InferenceCostReservation) -> bool:
    """A committed denial retains its original, never-contacted allowance."""
    return (row.state == "reserved" and row.contact_started_at is None
        and row.recovery_reason == "provider_contact_denied"
        and any(item.get("kind") == "provider_contact_denied"
            for item in json.loads(row.evidence_json)))


def _operator_operation(row: InferenceCostReservation) -> dict[str, object]:
    payload = _operation_payload(row)
    if row.state == "unknown" and row.recovery_reason == "provider_cost_readback_required" and any(
            isinstance(item, dict) and item.get("kind") == "restart_recovery"
            for item in json.loads(row.evidence_json or "[]")):
        # Display only: a row restart recovery classified before #1284 split
        # the reasons lost its outcome; it is not "answered, not priced".
        payload["recovery_reason"] = "provider_outcome_unknown"
    payload["controls"] = ([{"action": "settle", "endpoint": "/api/settings/model-fabric/accounting/settle",
        "method": "POST", "expected_revision": row.revision, "operation_id": row.operation_id,
        "job_id": row.job_id, "authority_scope": "deployment_accounting"}]
        if row.state in {"unknown", "contact_started"} else [])
    return payload


def _ledger_digest(account: InferenceAccountingOwner, rows: list[InferenceCostReservation]) -> str:
    from src.workspace.accounting_witness import ledger_digest
    return ledger_digest(account.model_dump(mode="json"), [_operation_payload(row) for row in rows])


_HISTORY_GAP_ENTRIES_KEPT = 16


def _append_history_gap(history: list, reason: str) -> list:
    """Append a ``history_gap`` entry, keeping the first and the latest ones.

    The first gap dates "history may be incomplete since"; the latest show
    the current reason. Older middle entries are dropped so repeated reseals
    cannot grow the account record without bound (#1284).
    """
    history = [*history, {"kind": "history_gap", "reason": reason,
        "recorded_at": datetime.now(timezone.utc).isoformat(), "memory_status": "no_learning"}]
    gaps = [index for index, entry in enumerate(history)
        if isinstance(entry, dict) and entry.get("kind") == "history_gap"]
    if len(gaps) <= _HISTORY_GAP_ENTRIES_KEPT:
        return history
    dropped = set(gaps[1:len(gaps) - _HISTORY_GAP_ENTRIES_KEPT + 1])
    return [entry for index, entry in enumerate(history) if index not in dropped]


def _witness(account: InferenceAccountingOwner) -> dict[str, object]:
    return {"deployment_id": account.deployment_id, "revision": account.revision, "ledger_digest": account.ledger_digest}


@contextmanager
def _continuity_lock(root: Path, *, initialize: bool = False, held_workspace=None, exclusive: bool = True):
    if held_workspace is not None:
        from src.workspace.accounting_witness import assert_deployment_binding
        if held_workspace.host_root != root:
            raise InferenceAccountingError("accounting_continuity_unavailable")
        assert_deployment_binding(held_workspace)
        yield held_workspace
        return
    workspace = ProductionWorkspace(host_root=root)
    directory = workspace.lifecycle_directory
    try:
        from src.workspace.accounting_witness import assert_deployment_binding
        try:
            assert_deployment_binding(workspace)
        except (RuntimeError, ValueError) as exc:
            raise InferenceAccountingError(str(exc)) from exc
        if root == Path(CANONICAL_CONTAINER_WORKSPACE):
            # An ordinary image directory must not impersonate the retained
            # managed mount. Paid inference alone fails; core startup stays CPU.
            mounted = False
            for line in Path("/proc/self/mountinfo").read_text().splitlines():
                pre, separator, post = line.partition(" - ")
                fields = pre.split()
                if separator and len(fields) > 4 and fields[4] == str(directory) and not post.startswith("overlay "):
                    mounted = True
            if not mounted:
                raise InferenceAccountingError("accounting_continuity_unavailable")
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise InferenceAccountingError("accounting_continuity_unavailable")
        if not directory.exists():
            if not initialize:
                raise InferenceAccountingError("accounting_continuity_unavailable")
            directory.mkdir(mode=0o700, parents=False)
        if not exclusive:
            # Read-only verification: the receipt is replaced atomically, and a
            # reader must never make a writer's non-blocking flock fail busy.
            yield workspace
            return
        descriptor = os.open(directory / "accounting.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InferenceAccountingError("accounting_continuity_busy") from exc
            yield workspace
        finally:
            os.close(descriptor)
    except InferenceAccountingError:
        raise
    except (OSError, ValueError) as exc:
        raise InferenceAccountingError("accounting_continuity_unavailable") from exc


class InferenceAccountingRepositoryMixin:
    """Focused storage operations inherited by the canonical job repository."""

    async def _accounting_begin(self, db: Any, *, read_only: bool = False) -> None:
        if db.get_bind().dialect.name != "sqlite":
            raise InferenceAccountingError("accounting_storage_unsupported")
        # A deferred BEGIN pins one consistent WAL snapshot without taking the
        # writer lock, so status polls never queue behind a foreign writer.
        await db.execute(text("BEGIN" if read_only else "BEGIN IMMEDIATE"))

    async def record_provider_denial_quiescence(self, proof):
        """Durable completion proof from the real closed broker callback path."""
        if (not isinstance(proof, _DenialQuiescenceProof) or proof._seal is not _DENIAL_QUIESCENCE_SEAL
            or proof.denial._seal is not _CONTACT_DENIAL_SEAL):
            raise InferenceAccountingError("accounting_quiescence_proof_invalid")
        binding = proof.denial._binding
        async with self._session() as db:
            await self._accounting_begin(db)
            account, rows = await self._accounting_rows(db)
            with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
                self._assert_accounting_continuity(workspace, account, rows, db=db)
                row = next((item for item in rows if item.operation_id == binding[0]), None)
                if row is None or not _provider_contact_denied(row) or (
                    row.operation_id, row.job_id, row.owner_id, row.payload_digest,
                    row.policy_digest, binding[5], row.job_fencing_token) != binding:
                    raise InferenceAccountingError("accounting_quiescence_binding_invalid")
                run = await self._fetch(db, row.job_id)
                from src.workspace import canonical_workspace_root_identity
                if (run.root_run_identity != proof.denial._root_job_id or run.fencing_token != row.job_fencing_token
                    or account.deployment_id != proof.denial._witness.get("deployment_id")
                    or _digest(canonical_workspace_root_identity(settings.workspace_dir)) != proof.denial._live_root_digest):
                    raise InferenceAccountingError("accounting_quiescence_root_fence_invalid")
                history = json.loads(row.evidence_json)
                record = {"kind": "provider_contact_denial_quiesced", "job_id": row.job_id,
                    "root_job_id": run.root_run_identity, "job_fence": row.job_fencing_token,
                    **proof.receipt, "memory_status": "no_learning"}
                if record not in history:
                    history.append(record)
                    row.evidence_json = _json(history)
                    row.revision += 1
                    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                    db.add(row)
                    await self._persist_accounting_witness(db, workspace, account, rows)
                elif db.info.get(_RESEAL_PENDING_KEY):
                    await self._persist_accounting_witness(db, workspace, account, rows)
                return _operation_payload(row)

    async def recover_inference_accounting(self, *, now: datetime | None = None, job_id: str | None = None) -> list[dict[str, object]]:
        from src.workflows.repo_repair_source import _repository_startup_mutation_fence
        async with _repository_startup_mutation_fence():
            return await self._recover_inference_accounting_locked(now=now, job_id=job_id)

    async def _recover_inference_accounting_locked(self, *, now: datetime | None = None, job_id: str | None = None) -> list[dict[str, object]]:
        """Classify stale provider work without replaying ephemeral callbacks."""
        observed = _utc(now or datetime.now(timezone.utc))
        recovered = []
        async with self._session() as db:
            await self._accounting_begin(db)
            from src.workflows.repo_repair_source import _repository_startup_protected_lineage
            protected = await _repository_startup_protected_lineage(db)
            account, rows = await self._accounting_rows(db)
            if account is None:
                return []
            with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
                self._assert_accounting_continuity(workspace, account, rows, db=db)
                changed = False
                for row in rows:
                    if row.job_id in protected:
                        continue
                    if job_id is not None and row.job_id != job_id:
                        continue
                    if row.state not in {"reserved", "contact_started", "unknown"}:
                        continue
                    run = await self._fetch(db, row.job_id)
                    if run.status != "running" or (run.lease_expires_at is not None and _utc(run.lease_expires_at) > observed):
                        continue
                    never_contacted = row.state == "reserved"
                    if _provider_contact_denied(row):
                        # Broker teardown/restart cannot forgive a committed
                        # denial or turn it into a fresh provider opportunity.
                        if run.job_kind == "audio_transcription_v1":
                            from src.workflows.audio_native import guard_audio_pair, advance_audio_pair
                            _, audio = await guard_audio_pair(db, run, require_current=False)
                            await advance_audio_pair(db, run, audio, changes={"status": "blocked",
                                "error_code": "provider_contact_denied", "transcript": None})
                        run.status = "blocked"
                        run.failure_reason = "provider_contact_denied"
                        run.lease_owner = None
                        run.lease_expires_at = None
                        run.fencing_token += 1
                        run.revision += 1
                        run.updated_at = observed.replace(tzinfo=None)
                        db.add(run)
                        changed = True
                        recovered.append({"job_id": row.job_id, "operation_id": row.operation_id,
                            "status": "blocked", "reason": "provider_contact_denied"})
                        continue
                    # No closure or provider payload is retained by an
                    # ephemeral chat/probe/embedding job. Its owner must start
                    # a fresh bounded operation explicitly.
                    typed_resume = False
                    if never_contacted and run.job_kind == "audio_transcription_v1":
                        from src.workflows.audio_native import guard_audio_pair
                        try:
                            native, _audio = await guard_audio_pair(db, run, physical=True)
                            effects = json.loads(run.effect_receipts_json)
                            typed_resume = (row.operation_id == native.operation_id
                                and row.policy_digest == native.policy_digest
                                and _utc(row.deadline_at) > observed
                                and all(item.get("effect_type") == "remote_inference_admission"
                                    and item.get("status") in {"intent", "blocked"} for item in effects))
                        except (ValueError, RuntimeError, PermissionError, OSError):
                            typed_resume = False
                    if never_contacted and run.job_kind in {"calendar_meeting_prep", "mail_reply_draft"} and _utc(row.deadline_at) > observed:
                        from src.db.models import WorkBoardAttempt, WorkBoardTask, WorkBoardInputArtifact
                        from src.work_board.dispatcher import _parse_typed_input
                        from src.workflows.job_runtime import _assert_canonical_goal_fence
                        attempt = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.workflow_run_id == row.job_id))).scalar_one_or_none()
                        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == attempt.task_id))).scalar_one_or_none() if attempt is not None else None
                        artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id) if task is not None and task.input_artifact_id else None
                        effects = json.loads(run.effect_receipts_json)
                        try:
                            if (task is None or artifact is None or attempt.ended_at is not None or attempt.cancel_requested_at is not None
                                or task.owner_principal_id != row.owner_id or task.owner_session_id != run.session_id
                                or artifact.bound_task_id != task.task_id or artifact.owner_principal_id != row.owner_id
                                or artifact.owner_session_id != run.session_id or artifact.payload_sha256 != task.typed_input_digest
                                or artifact.typed_input_ref != task.typed_input_ref or artifact.state not in {"bound", "consumed"}
                                or _utc(artifact.expires_at) <= observed
                                or task.capability_id != {"calendar_meeting_prep": "calendar.meeting-prep.v1", "mail_reply_draft": "work.mail-reply-draft.v1"}[run.job_kind]
                                or sum(item.get("kind") == "restart_recovery" for item in json.loads(row.evidence_json)) >= 3
                                or any(item.get("effect_type") != "remote_inference_admission" or item.get("status") not in {"intent", "blocked"} for item in effects)
                                or json.loads(run.artifact_receipts_json) or json.loads(run.checkpoint_receipts_json)):
                                raise ValueError("typed reconstruction authority unavailable")
                            _parse_typed_input(task)
                            if _current_policy_digest_for(row) != row.policy_digest:
                                raise ValueError("provider policy changed")
                            from src.auth.service import authenticate_session
                            operator = await authenticate_session(run.session_id, touch=False)
                            if operator.principal.principal_id != row.owner_id:
                                raise ValueError("owner changed")
                            await _assert_canonical_goal_fence(db, goal_id=run.goal_id, goal_revision=run.goal_revision,
                                owner_kind=run.owner_kind, owner_principal_id=run.owner_principal_id,
                                session_id=run.session_id, authority=run.declared_authority_json)
                            typed_resume = True
                        except Exception as exc:
                            import logging
                            logging.getLogger(__name__).warning("Typed precontact recovery blocked: %s", type(exc).__name__, exc_info=True)
                            typed_resume = False
                    if typed_resume:
                        row.recovery_reason = "typed_owner_precontact_resume"
                        run.status = "queued"
                        for effect in effects:
                            effect["status"] = "blocked"
                            effect.setdefault("details", {})["never_contacted"] = True
                        run.effect_receipts_json = _json(effects)
                    elif never_contacted:
                        row.state = "released"
                        row.recovery_reason = "never_contacted_callback_unavailable"
                        run.status = "blocked"
                    else:
                        # Contacted with no callback: the outcome is unknown,
                        # so it is never replayed. No charge is needed to move
                        # on; recording one later is optional (#1284).
                        row.state = "unknown"
                        # Contact started and the outcome was lost: no answer
                        # was recorded, so it is not "answered, not priced".
                        row.recovery_reason = "provider_outcome_unknown"
                        run.status = "unknown_external_effect"
                    row.revision += 1
                    row.updated_at = observed.replace(tzinfo=None)
                    if run.job_kind == "audio_transcription_v1":
                        from src.workflows.audio_native import guard_audio_pair, advance_audio_pair
                        _binding, audio = await guard_audio_pair(db, run, require_current=False)
                        await advance_audio_pair(db, run, audio, changes={"status": "queued" if typed_resume else "blocked",
                            "transport_status": row.state, "transcript": None})
                    history = json.loads(row.evidence_json)
                    history.append({"kind": "restart_recovery", "state": row.state, "reason": row.recovery_reason,
                        "recorded_at": observed.isoformat(), "memory_status": "no_learning"})
                    row.evidence_json = _json(history)
                    run.failure_reason = row.recovery_reason
                    run.lease_owner = None
                    run.lease_expires_at = None
                    run.fencing_token += 1
                    run.revision += 1
                    run.updated_at = observed.replace(tzinfo=None)
                    db.add(run)
                    db.add(row)
                    changed = True
                    recovered.append({"job_id": row.job_id, "operation_id": row.operation_id,
                        "status": run.status, "reason": row.recovery_reason})
                from src.workflows import inference_ledger
                # Startup recovery also seals a v1 ledger (or restored rows
                # without stored digests) forward to v2 after its full check.
                if changed or db.info[inference_ledger._SESSION_KEY].backfill:
                    await self._persist_accounting_witness(db, workspace, account, rows)
        return recovered

    async def _accounting_resume_claim_allowed(self, db, run) -> bool:
        rows = list((await db.execute(select(InferenceCostReservation).where(InferenceCostReservation.job_id == run.run_identity))).scalars())
        return bool(run.job_kind in {"calendar_meeting_prep", "mail_reply_draft", "audio_transcription_v1"} and len(rows) == 1
            and rows[0].state == "reserved" and rows[0].recovery_reason == "typed_owner_precontact_resume"
            and _utc(rows[0].deadline_at) > datetime.now(timezone.utc)
            and rows[0].policy_digest == _current_policy_digest_for(rows[0]))

    async def inference_precontact_resume_allowed(self, job_id: str) -> bool:
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            return run.status == "queued" and await self._accounting_resume_claim_allowed(db, run)

    async def _accounting_rows(self, db: Any):
        """The whole ledger, for the rare writers and readers that verify in full."""
        from src.workflows import inference_ledger
        account = await db.get(InferenceAccountingOwner, "deployment")
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        db.info[inference_ledger._SESSION_KEY] = inference_ledger.full_state(account, rows)
        return account, rows

    def _assert_accounting_continuity(self, workspace, account, rows, db: Any = None):
        """Full verification: every row's evidence, key and content, digest, totals.

        ADR-037 (#1284): the ledger is spend history only, so a digest, totals,
        witness or row mismatch never blocks. The session state (already built
        from every row's content by ``_accounting_rows``) is resealed: the next
        persist rewrites row digests, totals and the external witness, and a
        ``history_gap`` entry is appended to the account history. Invalid row
        evidence (a general-task group binding) still raises: it guards call
        counts and lineage, not money. The deployment binding is checked by the
        continuity lock itself and still blocks (a consent fence).
        """
        from src.workflows import inference_ledger

        def expected():
            receipt = read_lifecycle_receipt(workspace)
            return receipt.get("inference_accounting") if receipt is not None else None

        try:
            inference_ledger.verify_full(account, rows, expected)
        except inference_ledger.LedgerVerificationError as exc:
            self._mark_history_gap(workspace, account, str(exc) or "ledger_verification_failed", db=db)
            return
        inference_ledger.record_full_verification(workspace, account)

    @staticmethod
    def _assert_witness_not_ahead(workspace, account) -> None:
        """Never reseal over records the database does not have.

        An external witness (or retained checkpoint) newer than the account
        means a committed-elsewhere write is missing here: a crash between the
        witness and the commit, or an older database restored under a newer
        witness. Those writes may record a started provider contact, which is
        what keeps an operation from running twice, so they stay a block until
        maintenance replays the checkpoint (at-most-once, not money; #1284).
        """
        receipt = read_lifecycle_receipt(workspace) or {}
        expected = receipt.get("inference_accounting")
        if isinstance(expected, dict) and (account is None
                or expected.get("deployment_id") != account.deployment_id
                or not isinstance(expected.get("revision"), int) or expected["revision"] > account.revision):
            raise InferenceAccountingError("accounting_continuity_unavailable")

    def _mark_history_gap(self, workspace, account, reason: str, *, db: Any = None) -> None:
        """Reseal the in-session ledger state from content and record the gap.

        Nothing durable is written here. The reseal is marked in session state
        (``_RESEAL_PENDING_KEY``) and sealed by the writer's single final
        ``_persist_accounting_witness``. A writer that then refuses rolls the
        reseal back with everything else, so the external witness never moves
        ahead of a transaction that did not commit (#1284).
        """
        import logging
        from src.workflows import inference_ledger
        if account is None:
            # Request records without their account: records may be missing,
            # and they keep an operation from running twice. Never start over.
            raise InferenceAccountingError("accounting_continuity_unavailable")
        self._assert_witness_not_ahead(workspace, account)
        logging.getLogger(__name__).warning(
            "inference spend ledger verification failed (%s); resealing from current content", reason)
        if db is None:
            # A reader without a writer transaction proceeds on the content it
            # read; the next writer reseals and records the gap.
            return
        state = db.info.get(inference_ledger._SESSION_KEY)
        if state is not None:
            state.backfill = True
        if db.info.get(_RESEAL_PENDING_KEY):
            # One gap entry per transaction, however many reads reseal.
            return
        account.settings_history_json = _json(_append_history_gap(
            json.loads(account.settings_history_json), str(reason)[:128]))
        db.info[_RESEAL_PENDING_KEY] = True

    async def _reseal_ledger(self, db: Any, reason: str):
        """Escalate a hot writer to the whole ledger and reseal it in session (#1284).

        Nothing is written: the writer's single final persist seals the reseal.
        """
        from src.workflows import inference_ledger
        from src.workflows.inference_group_lookup import classify_group_lookup
        from src.workflows.general_task_accounting import entry_for
        workspace, account = db.info[_LEDGER_CONTEXT_KEY]
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        # Group evidence guards call counts and lineage, not money: invalid
        # evidence still refuses, exactly as full verification does.
        for row in rows:
            entry_for(row)
        for row in rows:
            # The lookup key is an unhashed hint: rederive it from sealed evidence.
            key = classify_group_lookup(row)
            if row.group_lookup_key != key:
                row.group_lookup_key = key
        state = inference_ledger.full_state(account, rows)
        db.info[inference_ledger._SESSION_KEY] = state
        # Sealed by this writer's final persist, never before it (#1284).
        self._mark_history_gap(workspace, account, reason, db=db)
        return state

    async def _open_ledger(self, db: Any, workspace, account):
        """Verify continuity for one hot writer; full when due (R7) or required.

        A failed verification reseals the ledger instead of blocking (#1284).
        """
        from src.workflows import inference_ledger
        if account is None:
            raise InferenceAccountingError("accounting_continuity_unavailable")
        db.info[_LEDGER_CONTEXT_KEY] = (workspace, account)
        digests, usable = await inference_ledger.scan_row_digests(db)
        from src.workspace.accounting_witness import is_v2_digest
        if (usable and is_v2_digest(account.ledger_digest) and inference_ledger.totals_current(account)
                and not inference_ledger.full_verification_due(workspace, account)):
            receipt = read_lifecycle_receipt(workspace)
            expected = receipt.get("inference_accounting") if receipt is not None else None
            try:
                totals = inference_ledger.verify_fast(account, expected, digests)
            except inference_ledger.LedgerVerificationError as exc:
                return await self._reseal_ledger(db, str(exc) or "ledger_verification_failed")
            await self._assert_group_keys_present(db)
            state = inference_ledger.LedgerState(digests=digests, totals=totals, full=False)
            db.info[inference_ledger._SESSION_KEY] = state
            return state
        _account, rows = await self._accounting_rows(db)
        # A reseal stays pending until this writer's final persist (#1284).
        self._assert_accounting_continuity(workspace, account, rows, db=db)
        return db.info[inference_ledger._SESSION_KEY]

    async def _ledger_row(self, db: Any, state, operation_id: str):
        """One reservation by id, verified against its stored digest (R2)."""
        from src.workflows import inference_ledger
        state = db.info.get(inference_ledger._SESSION_KEY, state)
        row = await db.get(InferenceCostReservation, operation_id)
        try:
            inference_ledger.verify_loaded_rows(state, [row])
        except inference_ledger.LedgerVerificationError as exc:
            state = await self._reseal_ledger(db, str(exc))
            inference_ledger.verify_loaded_rows(state, [row])
        return row

    async def _ledger_rows(self, db: Any, state, *criteria):
        """Reservations matching ``criteria``, each verified against its digest."""
        from src.workflows import inference_ledger
        state = db.info.get(inference_ledger._SESSION_KEY, state)
        rows = list((await db.execute(select(InferenceCostReservation).where(*criteria)
            .order_by(InferenceCostReservation.operation_id))).scalars())
        try:
            inference_ledger.verify_loaded_rows(state, rows)
        except inference_ledger.LedgerVerificationError as exc:
            state = await self._reseal_ledger(db, str(exc))
            inference_ledger.verify_loaded_rows(state, rows)
        return rows

    async def _group_rows(self, db: Any, state, group_id: str):
        """Exact general-task group members via the private lookup key.

        Every row's key is a full-verification invariant; a missing or invalid
        key anywhere blocks, as the full-list ``assert_group_lookup`` did.
        """
        from src.workflows import inference_ledger
        from src.workspace.accounting_witness import evidence_group_key
        await self._assert_group_keys_present(db)
        for attempt in (0, 1):
            rows = await self._ledger_rows(db, state, InferenceCostReservation.group_lookup_key == "g:" + group_id)
            # The key is an unhashed hint: a member hidden by editing it on an
            # untouched row would under-count the group's call cap. Every
            # returned row is digest-verified, so requiring its sealed evidence
            # to name this group and the count to equal the sealed membership
            # makes the read exact. A disagreement reseals the whole ledger
            # (keys rederived from evidence, counts from content) and reads
            # again (#1284) instead of blocking.
            exact = (all(evidence_group_key(row.evidence_json) == "g:" + group_id for row in rows)
                and len(rows) == state.totals["rows_by_group"].get("g:" + group_id, 0))
            if exact:
                return rows
            if attempt:
                raise InferenceAccountingError("general_task_group_lookup_invalid")
            state = await self._reseal_ledger(db, "ledger_group_count_mismatch")
            state = db.info[inference_ledger._SESSION_KEY]

    async def _discovery_rows(self, db: Any, state, run):
        """Every row ``discovery_generation_budget`` can count or raise on."""
        from src.work_board.research_parent import DISCOVERY_KIND, DISCOVERY_SERVICE
        if run.job_kind != DISCOVERY_KIND:
            return []
        from sqlalchemy import or_
        discovery_jobs = select(WorkflowRunState.run_identity).where(WorkflowRunState.job_kind == DISCOVERY_KIND)
        rows = await self._ledger_rows(db, state, or_(
            InferenceCostReservation.owner_id == DISCOVERY_SERVICE,
            InferenceCostReservation.job_id.in_(discovery_jobs)))
        # Every programme row is owned by the discovery service; a row hidden
        # by editing its owner would skip the programme request limit. A
        # disagreement with the sealed count reseals and reads again (#1284).
        if sum(row.owner_id == DISCOVERY_SERVICE for row in rows) != state.totals["rows_by_owner"].get(DISCOVERY_SERVICE, 0):
            from src.workflows import inference_ledger
            await self._reseal_ledger(db, "ledger_owner_count_mismatch")
            state = db.info[inference_ledger._SESSION_KEY]
            rows = await self._ledger_rows(db, state, or_(
                InferenceCostReservation.owner_id == DISCOVERY_SERVICE,
                InferenceCostReservation.job_id.in_(discovery_jobs)))
        return rows

    async def _shadow_helper(self, db: Any, call, scoped_rows, *, compare=None) -> None:
        """Test-only: a helper fed scoped rows must equal it fed the whole ledger.

        With ``compare``, the full-ledger result is checked by that predicate
        instead (for decisions taken from sealed totals, not scoped rows).
        """
        from src.workflows import inference_ledger
        if not inference_ledger.SHADOW_FULL_LEDGER:
            return
        full = list((await db.execute(select(InferenceCostReservation))).scalars())
        if compare is not None:
            value = await call(full)
            if not compare(value):
                raise AssertionError(f"sealed-total decision drift: full-ledger result {value!r}")
            return
        await inference_ledger.shadow_compare_helper(lambda: call(scoped_rows), lambda: call(full))

    @staticmethod
    def _near_outstanding_sealed(ledger, prior, owner_id: str) -> bool:
        """Another in-flight near-text request for this owner, from sealed counts.

        Not a content query: an edit to an untouched row could empty that.
        ``prior`` (this operation's own row, already digest-verified) is not
        "another" request. A request NEAR has not priced yet is finished and
        never counts (#1284).
        """
        own_open = bool(prior is not None and prior.owner_id == owner_id
            and prior.runtime_path == "near_text_native"
            and prior.state in {"reserved", "contact_started"})
        return ledger.totals["owner_near_text_open"].get(owner_id, 0) - int(own_open) > 0

    @staticmethod
    async def _near_outstanding(rows, owner_id: str, operation_id: str) -> bool:
        return any(row.operation_id != operation_id and row.owner_id == owner_id
            and row.runtime_path == "near_text_native"
            and row.state in {"reserved", "contact_started"} for row in rows)

    async def _persist_accounting_witness(self, db, workspace, account, rows=None):
        """Seal the session's reservation changes and advance the witness.

        ``rows`` is accepted for existing callers but no longer trusted: the
        changed set and the totals come from the session (#1086 R3).
        """
        from src.workflows import inference_ledger
        resealed = bool(db.info.pop(_RESEAL_PENDING_KEY, None))
        base = _witness(account) if account.revision else None
        try:
            changed = await inference_ledger.persist(db, workspace, account)
        except inference_ledger.LedgerVerificationError as exc:
            raise InferenceAccountingError("accounting_continuity_unavailable") from exc
        receipt = read_lifecycle_receipt(workspace) or {"secret_values_included": False}
        receipt["inference_accounting"] = _witness(account)
        write_accounting_checkpoint(workspace, {"schema_version": 1, "base": base,
            "account": account.model_dump(mode="json"), "operations": [_operation_payload(row) for row in changed],
            "witness": _witness(account), "secret_values_included": False})
        write_lifecycle_receipt(workspace, receipt, _accounting_lock_held=True)
        if resealed:
            inference_ledger.record_full_verification(workspace, account)

    async def ensure_inference_ledger(self, *, continuity_workspace=None) -> dict[str, object]:
        """Create the deployment's spend ledger once; it holds no amount (#1284).

        Called by a NEAR settings save and before the first request. An
        existing ledger is verified and returned unchanged. A second account is
        never created when the external witness already names one.
        """
        root = Path(settings.workspace_dir).resolve()
        async with self._session() as db:
            await self._accounting_begin(db)
            account, rows = await self._accounting_rows(db)
            with _continuity_lock(root, initialize=account is None, held_workspace=continuity_workspace) as workspace:
                if account is not None:
                    self._assert_accounting_continuity(workspace, account, rows, db=db)
                    if db.info.get(_RESEAL_PENDING_KEY):
                        await self._persist_accounting_witness(db, workspace, account, rows)
                    return _witness(account)
                prior = read_lifecycle_receipt(workspace)
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                # ``ceiling_microusd`` is a retained NOT NULL column; never read.
                account = InferenceAccountingOwner(ceiling_microusd=0, created_at=now, updated_at=now, revision=0)
                if rows or (prior and "inference_accounting" in prior):
                    # Request records (or a witness of them) exist without the
                    # account: records may be missing, and they are what keeps
                    # an operation from running twice. Never start over.
                    raise InferenceAccountingError("accounting_continuity_unavailable")
                account.settings_history_json = _json([{"kind": "ledger_created", "recorded_at": now.isoformat()}])
                db.add(account)
                await db.flush()
                await self._persist_accounting_witness(db, workspace, account, rows)
                return _witness(account)

    async def reserve_inference_cost(self, *, operation_id: str, job_id: str, owner_id: str,
                                     payload_digest: str, policy_digest: str, runtime_path: str,
                                     profile_id: str,
                                     priority: int, deadline_at: float, owner: str, fencing_token: int,
                                     general_task_binding: object = None,
                                     now: datetime | None = None) -> dict[str, object]:
        from src.workflows.research_accounting import discovery_accounting_scope
        async with discovery_accounting_scope(self, job_id=job_id):
            if not re.fullmatch(r"[0-9a-f]{64}", payload_digest or "") or not re.fullmatch(r"[0-9a-f]{64}", policy_digest or ""):
                raise InferenceAccountingError("accounting_operation_binding_invalid")
            observed = _utc(now or datetime.now(timezone.utc))
            deadline = datetime.fromtimestamp(deadline_at, timezone.utc)
            async with self._session() as db:
                await self._accounting_begin(db)
                account = await db.get(InferenceAccountingOwner, "deployment")
                with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
                    ledger = await self._open_ledger(db, workspace, account)
                    run = await self._fetch(db, job_id)
                    self._assert_lease(run, owner=owner, fencing_token=fencing_token)
                    if run.status != "running" or run.owner_principal_id != owner_id or deadline <= observed:
                        raise InferenceAccountingError("accounting_job_authority_invalid")
                    prior = await self._ledger_row(db, ledger, operation_id)
                    if run.job_kind == "audio_transcription_v1":
                        from src.workflows.audio_native import accounting_pair, utc as audio_utc
                        native = await accounting_pair(db, run)
                        job_rows = await self._ledger_rows(db, ledger, InferenceCostReservation.job_id == job_id)
                        if (operation_id != native.operation_id or policy_digest != native.policy_digest
                            or profile_id != "openrouter.audio"
                            or _utc(deadline) != audio_utc(native.original_deadline_at)
                            or (prior is not None and prior.recovery_reason != "typed_owner_precontact_resume")
                            or any(item.operation_id != operation_id for item in job_rows)):
                            raise InferenceAccountingError("audio_original_reservation_invalid")
                    from src.workflows.research_accounting import discovery_generation_budget
                    discovery_rows = await self._discovery_rows(db, ledger, run)
                    programme_budget = await discovery_generation_budget(self, db, run, discovery_rows,
                        operation_id=operation_id, payload_digest=payload_digest)
                    await self._shadow_helper(db, lambda rows: discovery_generation_budget(self, db, run, rows,
                        operation_id=operation_id, payload_digest=payload_digest), discovery_rows)
                    if runtime_path == "near_text_native":
                        if profile_id != "near.text" or run.job_kind != "inference.near-text.v1":
                            raise InferenceAccountingError("near_native_binding_invalid")
                        # A read above may have resealed: use the current state.
                        from src.workflows import inference_ledger
                        outstanding = self._near_outstanding_sealed(
                            db.info.get(inference_ledger._SESSION_KEY, ledger), prior, owner_id)
                        await self._shadow_helper(db, lambda rows: self._near_outstanding(rows, owner_id, operation_id),
                            [] if not outstanding else None, compare=lambda value: value == outstanding)
                        if outstanding:
                            raise InferenceAccountingError("near_owner_outstanding_limit")
                    if prior is not None:
                        if (prior.state == "reserved" and prior.recovery_reason == "typed_owner_precontact_resume"
                            and prior.job_id == job_id and prior.owner_id == owner_id and prior.payload_digest == payload_digest
                            and prior.policy_digest == policy_digest and prior.runtime_path == runtime_path and prior.profile_id == profile_id
                            and _utc(prior.deadline_at) > observed):
                            from src.workflows.general_task_accounting import entry_for, validate_recovered_entry
                            prior_entry = entry_for(prior)
                            if prior_entry is not None or general_task_binding is not None:
                                group_rows = (await self._group_rows(db, ledger, prior_entry["group"]["group_id"])
                                    if prior_entry is not None else [prior])
                                await validate_recovered_entry(db, run, prior, group_rows, general_task_binding,
                                    runtime_path=runtime_path)
                                await self._shadow_helper(db, lambda rows: validate_recovered_entry(db, run, prior, rows,
                                    general_task_binding, runtime_path=runtime_path), group_rows)
                            prior.job_fencing_token = fencing_token
                            prior.recovery_reason = None
                            prior.revision += 1
                            prior.updated_at = observed.replace(tzinfo=None)
                            db.add(prior)
                            await self._persist_accounting_witness(db, workspace, account, [prior])
                            return _operation_payload(prior)
                        raise InferenceAccountingError("accounting_operation_already_reserved")
                    task_group_entry = None
                    if general_task_binding is not None:
                        from src.workflows.general_task_accounting import reserve_entry
                        group = general_task_binding.get("group") if isinstance(general_task_binding, dict) else None
                        group_rows = await self._group_rows(db, ledger, group.group_id) if hasattr(group, "group_id") else []
                        task_group_entry = await reserve_entry(db, run, group_rows, general_task_binding,
                            operation_id=operation_id, bound=0, runtime_path=runtime_path, deadline_at=deadline,
                            policy_digest=policy_digest, payload_digest=payload_digest)
                        await self._shadow_helper(db, lambda rows: reserve_entry(db, run, rows, general_task_binding,
                            operation_id=operation_id, bound=0, runtime_path=runtime_path, deadline_at=deadline,
                            policy_digest=policy_digest, payload_digest=payload_digest), group_rows)
                    # ADR-037 (#1284): no month, ceiling, reserve or overrun is
                    # checked. The record exists for at-most-once execution, the
                    # consent binding rechecked at contact, and the charge.
                    period = period_id(observed)
                    row = InferenceCostReservation(
                        operation_id=operation_id, deployment_id=account.deployment_id,
                        job_id=job_id, owner_id=owner_id, goal_id=run.goal_id, goal_revision=run.goal_revision,
                        payload_digest=payload_digest, policy_digest=policy_digest, runtime_path=runtime_path,
                        profile_id=profile_id, period_id=period, settings_revision=account.settings_revision,
                        # Retained NOT NULL columns, never compared (#1284).
                        ceiling_microusd=0, bound_microusd=0,
                        owner_ceiling_microusd=None, sequence=account.revision + 1,
                        priority=priority, deadline_at=deadline.replace(tzinfo=None),
                        job_fencing_token=fencing_token, created_at=observed.replace(tzinfo=None), updated_at=observed.replace(tzinfo=None),
                        evidence_json=_json([{"kind": "reservation", "memory_status": "no_learning"}]
                            + ([task_group_entry] if task_group_entry else [])),
                    )
                    if programme_budget is not None:
                        evidence = json.loads(row.evidence_json)
                        evidence.append({"kind": "goal_programme_binding", **programme_budget})
                        row.evidence_json = _json(evidence)
                    from src.workflows.inference_group_lookup import classify_group_lookup, assert_group_lookup
                    row.group_lookup_key = classify_group_lookup(row)
                    assert_group_lookup(row)
                    db.add(row)
                    await self._persist_accounting_witness(db, workspace, account, [row])
                    return _operation_payload(row)

    async def contact_inference_provider(self, operation_id: str, *, owner: str,
                                         fencing_token: int, policy_digest: str,
                                         near_contact_witness: object = None,
                                         general_task_binding: object = None,
                                         precontact_denial: str | None = None) -> dict[str, object]:
        from src.workflows.research_accounting import discovery_accounting_scope
        async with discovery_accounting_scope(self, operation_id=operation_id):
            denial = None
            result = None
            denial_binding = None
            denial_witness = None
            denial_root_job_id = None
            denial_live_root_digest = None
            async with self._session() as db:
                await self._accounting_begin(db)
                account = await db.get(InferenceAccountingOwner, "deployment")
                # Read before the continuity lock (the research prompt readback
                # below may take the witness lock itself) and used only for that
                # deny-only check; the same object is digest-verified under the
                # lock before any decision or write (#1086 R2).
                pending = await db.get(InferenceCostReservation, operation_id)
                if pending is not None and pending.state == "reserved" and pending.policy_digest == policy_digest:
                    research_run = await self._fetch(db, pending.job_id)
                    if research_run.job_kind == "readonly_research_child":
                        # The exact source permission/input/body check stays in
                        # this same serialized contact writer. Completed local
                        # source readback may acquire the witness lock itself, so
                        # finish it before acquiring our contact witness lock.
                        from src.workflows.research_sources import verify_current_prompt_in_db
                        from src.workflows.job_runtime import _serialize
                        self._assert_lease(research_run, owner=owner, fencing_token=fencing_token)
                        await verify_current_prompt_in_db(self, db, _serialize(research_run))
                with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
                    ledger = await self._open_ledger(db, workspace, account)
                    row = await self._ledger_row(db, ledger, operation_id)
                    if row is None or row.state != "reserved" or row.policy_digest != policy_digest:
                        raise InferenceAccountingError("accounting_contact_fence_invalid")
                    run = await self._fetch(db, row.job_id)
                    self._assert_lease(run, owner=owner, fencing_token=fencing_token)
                    if run.job_kind == "readonly_research_child":
                        from src.workflows.research_guard import assert_research_parent_current
                        await assert_research_parent_current(db, run)
                    if run.job_kind == "audio_transcription_v1":
                        from src.workflows.audio_native import guard_audio_pair
                        native, _audio = await guard_audio_pair(db, run, physical=True)
                        if operation_id != native.operation_id or row.policy_digest != native.policy_digest:
                            raise InferenceAccountingError("audio_contact_binding_invalid")
                    from src.workflows.job_runtime import _assert_canonical_goal_fence
                    await _assert_canonical_goal_fence(db, goal_id=run.goal_id, goal_revision=run.goal_revision,
                        owner_kind=run.owner_kind, owner_principal_id=run.owner_principal_id,
                        session_id=run.session_id, authority=run.declared_authority_json)
                    from src.workflows.research_accounting import discovery_generation_budget
                    discovery_rows = await self._discovery_rows(db, ledger, run)
                    await discovery_generation_budget(self, db, run, discovery_rows,
                        operation_id=operation_id, payload_digest=row.payload_digest)
                    await self._shadow_helper(db, lambda rows: discovery_generation_budget(self, db, run, rows,
                        operation_id=operation_id, payload_digest=row.payload_digest), discovery_rows)
                    if run.owner_kind == "user":
                        from src.auth.service import authenticate_principal
                        await authenticate_principal(row.owner_id, db=db)
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    if run.status != "running" or row.job_fencing_token != fencing_token or _utc(row.deadline_at) <= _utc(now):
                        raise InferenceAccountingError("accounting_contact_fence_invalid")
                    opportunity_denial = None
                    from src.workflows.general_task_accounting import entry_for, validate_contact
                    general_entry = entry_for(row)
                    if general_entry is not None:
                        group_rows = await self._group_rows(db, ledger, general_entry["group"]["group_id"])
                        await validate_contact(db, run, row, group_rows, binding=general_task_binding)
                        await self._shadow_helper(db, lambda rows: validate_contact(db, run, row, rows,
                            binding=general_task_binding), group_rows)
                    if run.job_kind == "work_board_proposal":
                        from src.guardian.opportunity_plans import guard_linked_plan_provider_contact
                        from src.guardian.opportunity_contracts import OpportunityError
                        try:
                            await guard_linked_plan_provider_contact(db, run)
                        except OpportunityError as exc:
                            opportunity_denial = exc.code
                    if run.job_kind == "guardian_opportunity_assess":
                        from src.guardian.opportunity_runtime import guard_provider_contact
                        from src.guardian.opportunity_contracts import OpportunityError
                        try:
                            await guard_provider_contact(db, run)
                        except OpportunityError as exc:
                            opportunity_denial = exc.code
                    # Reservations can predate another call's actual settlement.
                    # Recheck the canonical ledger under this SAME writer and
                    # witness lock before recording contact, including prefunding.
                    from src.model_fabric.effective_policy import current_inference_policy
                    if row.runtime_path == "near_text_native":
                        from src.work_board.near_text_native import recheck_provider_contact
                        from src.work_board.repository import BoardError
                        if row.profile_id != "near.text" or near_contact_witness is None:
                            opportunity_denial = "near_contact_authority_required"
                        else:
                            try:
                                await recheck_provider_contact(
                                    db, run, witness=near_contact_witness,
                                    contact_operation_id=row.operation_id,
                                )
                            except (BoardError, ValueError, PermissionError) as exc:
                                opportunity_denial = getattr(exc, "code", "near_contact_authority_changed")
                    from src.model_fabric.accounting import is_near_operation, near_tee_profile_slot
                    try:
                        if row.runtime_path == "near_text_native":
                            from src.model_fabric.effective_policy import current_near_text_policy
                            configured, current_digest = current_near_text_policy()
                        elif (tee_slot := near_tee_profile_slot(row.runtime_path, row.profile_id)) is not None:
                            from src.model_fabric.effective_policy import current_near_tee_policy
                            configured, current_digest = current_near_tee_policy(tee_slot)
                        else:
                            configured, current_digest = current_inference_policy()
                    except PermissionError:
                        configured, current_digest = None, None
                    from src.model_fabric.retirement import OPENROUTER_RETIRED_REASON, openrouter_retired
                    # ADR-035: narrowed to OpenRouter (every non-NEAR) rows.
                    retired_denial = (OPENROUTER_RETIRED_REASON
                        if not is_near_operation(row.runtime_path, row.profile_id) and openrouter_retired() else None)
                    # A caller precontact guard (e.g. the NEAR TEE billing
                    # window) denies here, durably, before contact is recorded.
                    guard_denial = (precontact_denial if isinstance(precontact_denial, str)
                        and re.fullmatch(r"[a-z0-9_:.-]{1,128}", precontact_denial) else
                        "precontact_guard_invalid" if precontact_denial is not None else None)
                    # ADR-037 (#1284): only consent, policy and authority deny
                    # here; no amount is compared.
                    denial = (guard_denial or opportunity_denial or retired_denial or ("provider_contact_denied" if _provider_contact_denied(row)
                        else "provider_policy_revision_changed" if current_digest != policy_digest else None))
                    if not _provider_contact_denied(row):
                        history = json.loads(row.evidence_json)
                        if denial:
                            row.recovery_reason = "provider_contact_denied"
                            history.append({"kind": "provider_contact_denied", "reason": denial,
                                "accounting_revision": account.revision, "fencing_token": fencing_token,
                                "never_contacted": True, "recorded_at": now.isoformat(), "memory_status": "no_learning"})
                        else:
                            row.state = "contact_started"
                            row.contact_started_at = now
                            history.append({"kind": "provider_contact_started", "fencing_token": fencing_token, "recorded_at": now.isoformat()})
                        if run.job_kind == "audio_transcription_v1":
                            from src.workflows.audio_native import accounting_pair
                            await accounting_pair(db, run, changes={
                                "admission_operation_id": operation_id if not denial else None,
                                "transport_status": "contact_started" if not denial else "blocked"})
                        row.updated_at = now
                        row.revision += 1
                        row.evidence_json = _json(history)
                        db.add(row)
                        await self._persist_accounting_witness(db, workspace, account, [row])
                    result = _operation_payload(row)
                    if denial:
                        denial_binding = (row.operation_id, row.job_id, row.owner_id,
                            row.payload_digest, row.policy_digest, owner, fencing_token)
                        denial_witness = _witness(account)
                        denial_root_job_id = run.root_run_identity
                        from src.workspace import canonical_workspace_root_identity
                        denial_live_root_digest = _digest(canonical_workspace_root_identity(settings.workspace_dir))
            # Raising inside the session would roll back the durable denial while
            # its external witness had already advanced. Commit before reporting it.
            if denial:
                raise InferenceProviderContactDenied(denial, _seal=_CONTACT_DENIAL_SEAL,
                    _binding=denial_binding, _witness=denial_witness, _root_job_id=denial_root_job_id,
                    _live_root_digest=denial_live_root_digest)
            return result

    async def settle_inference_cost(self, operation_id: str, *, payload: object = None,
                                    near_billing_evidence: object = None,
                                    actual_cost_microusd: int | None = None,
                                    evidence_digest: str | None = None,
                                    operator_id: str | None = None,
                                    expected_revision: int | None = None,
                                    job_id: str | None = None,
                                    idempotency_key: str | None = None,
                                    reason: str = "provider_account_usage") -> dict[str, object]:
        actual, provider_id = account_charge_microusd(payload)
        if actual_cost_microusd is not None:
            actual = integer_amount(actual_cost_microusd)
            if not operator_id or not re.fullmatch(r"[0-9a-f]{64}", evidence_digest or ""):
                raise InferenceAccountingError("accounting_settlement_evidence_required")
        async with self._session() as db:
            await self._accounting_begin(db)
            account = await db.get(InferenceAccountingOwner, "deployment")
            with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
                ledger = await self._open_ledger(db, workspace, account)
                row = await self._ledger_row(db, ledger, operation_id)
                if row is None:
                    raise InferenceAccountingError("accounting_operation_not_found")
                from src.model_fabric.accounting import near_tee_profile_slot
                near_tee_row = near_tee_profile_slot(row.runtime_path, row.profile_id) is not None
                near = row.runtime_path == "near_text_native" or near_tee_row
                billing = None
                if near_billing_evidence is not None:
                    from src.model_fabric.near_text_billing import validate_near_billing_evidence
                    try:
                        billing = validate_near_billing_evidence(near_billing_evidence, original_operation_id=operation_id)
                    except (ValueError, TypeError) as exc:
                        raise InferenceAccountingError("near_billing_evidence_invalid") from exc
                    if not near or (row.profile_id != "near.text" and not near_tee_row) or actual_cost_microusd is not None or operator_id is not None or payload is not None:
                        raise InferenceAccountingError("near_billing_operation_invalid")
                    if row.state not in {"contact_started", "unknown", "settled"}:
                        raise InferenceAccountingError("near_billing_contact_required")
                    actual, provider_id = billing.cost_microusd, billing.provider_request_id
                    previous = next((item for item in json.loads(row.evidence_json)
                        if item.get("provenance") == "near_billing_costs"), None)
                    if previous is not None and (previous.get("provider_operation_id"), previous.get("actual_cost_microusd"), previous.get("billing_response_sha256")) != (provider_id, actual, billing.response_sha256):
                        raise InferenceAccountingError("accounting_settlement_conflict")
                elif near and actual_cost_microusd is None:
                    # OpenRouter-shaped usage can never settle a NEAR call.
                    actual, provider_id = None, None
                if job_id is not None and row.job_id != job_id:
                    raise InferenceAccountingError("accounting_job_binding_invalid")
                if actual_cost_microusd is not None:
                    prior_evidence = json.loads(row.evidence_json)
                    repeated = next((item for item in prior_evidence if idempotency_key and item.get("idempotency_key") == idempotency_key), None)
                    if repeated is not None:
                        if repeated.get("actual_cost_microusd") != actual or repeated.get("evidence_digest") != evidence_digest:
                            raise InferenceAccountingError("accounting_settlement_conflict")
                        return _operation_payload(row)
                    if type(expected_revision) is not int or row.revision != expected_revision or not idempotency_key or len(idempotency_key) > 128:
                        raise InferenceAccountingError("accounting_settlement_revision_changed")
                if row.state in {"settled", "released"}:
                    if row.actual_cost_microusd != actual and actual is not None:
                        raise InferenceAccountingError("accounting_settlement_conflict")
                    return _operation_payload(row)
                if _provider_contact_denied(row) and actual is None:
                    # The broker's finally path must read the canonical held
                    # denial, not infer release from its transient contacted flag.
                    run = await self._fetch(db, row.job_id)
                    closed = (run.lease_owner is None and run.lease_expires_at is None and run.finished_at is not None
                        and run.fencing_token == row.job_fencing_token)
                    terminal_authority = ((reason == "cancelled_before_contact" and run.status == "cancelled")
                        or (reason == "expired_before_contact" and run.status == "failed"
                            and run.failure_reason == "deadline_expired" and _utc(row.deadline_at) <= datetime.now(timezone.utc)))
                    quiesced = any(item.get("kind") == "provider_contact_denial_quiesced"
                        and item.get("job_id") == row.job_id and item.get("root_job_id") == run.root_run_identity
                        and item.get("job_fence") == row.job_fencing_token and item.get("provider_never_contacted") is True
                        and item.get("callback_completed") is True for item in json.loads(row.evidence_json))
                    if not (closed and terminal_authority and quiesced):
                        return _operation_payload(row)
                if row.state == "reserved":
                    # Only callback-never-started cancellation/expiry releases.
                    if reason not in {"cancelled_before_contact", "expired_before_contact", "blocked_before_contact"}:
                        raise InferenceAccountingError("accounting_contact_not_started")
                    row.state, row.recovery_reason = "released", reason
                else:
                    row.state = "unknown" if actual is None else "settled"
                    row.actual_cost_microusd = actual
                    row.provider_operation_id = provider_id or row.provider_operation_id
                    # "Not priced yet" when NEAR reported no charge; nothing waits
                    # on it. A call that failed after contact says so (#1284).
                    row.recovery_reason = (None if actual is not None else "provider_call_failed"
                        if reason == "provider_call_failed" else "provider_cost_readback_required")
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                history = json.loads(row.evidence_json)
                audio_run = await self._fetch(db, row.job_id)
                if audio_run.job_kind == "audio_transcription_v1":
                    from src.workflows.audio_native import accounting_pair
                    await accounting_pair(db, audio_run, require_current=False,
                        changes={"transport_status": row.state})
                history.append({"kind": row.state, "reason": reason, "actual_cost_microusd": actual,
                    "provenance": "manual_externally_unverified" if operator_id else "near_billing_costs" if billing is not None else "provider_account_usage" if actual is not None else "unresolved_provider_contact",
                    **({"billing_response_sha256": billing.response_sha256,
                        "cost_nano_usd": billing.cost_nano_usd} if billing is not None else {}),
                    "provider_charge_verified": actual is not None and operator_id is None,
                    "upstream_cost_evidence": (str(payload.get("usage", {}).get("cost_details", {}).get("upstream_inference_cost"))[:64]
                        if isinstance(payload, Mapping) and isinstance(payload.get("usage"), Mapping)
                        and isinstance(payload["usage"].get("cost_details"), Mapping) else None),
                    "provider_operation_id": provider_id, "evidence_digest": evidence_digest or _digest({"actual_cost_microusd": actual, "provider_operation_id": provider_id}),
                    "operator_id": operator_id, "idempotency_key": idempotency_key,
                    "recorded_at": now.isoformat(), "memory_status": "no_learning"})
                row.evidence_json, row.updated_at = _json(history), now
                row.revision += 1
                db.add(row)
                await self._persist_accounting_witness(db, workspace, account, [row])
                return _operation_payload(row)

    async def inference_accounting_snapshot(self, *, now: datetime | None = None, job_id: str | None = None, continuity_workspace=None) -> dict[str, object]:
        """Spend history view (ADR-037): display only, it gates nothing.

        Reads use a deferred snapshot and escalate to the serialized writer
        transaction only when continuity does not verify on the fast path (a
        writer persists the witness before its commit), so either outcome
        matches the serialized read. Nothing is written by a read.
        """
        period = period_id(now or datetime.now(timezone.utc))
        try:
            if continuity_workspace is None:
                snapshot = await self._accounting_snapshot(period, job_id, read_only=True)
                if snapshot is not None:
                    return snapshot
            snapshot = await self._accounting_snapshot(period, job_id, continuity_workspace=continuity_workspace)
            assert snapshot is not None
            return snapshot
        except Exception as exc:
            return {"status": "unavailable", "reason_code": getattr(exc, "code", "accounting_continuity_unavailable"),
                "accounting_continuity_verified": False, "history_verified": False,
                "period_id": period, "spent_microusd": None, "unpriced_requests": None,
                "operation_count": 0, "operations_truncated": False, "operations": [], "memory_status": "no_learning"}

    async def _accounting_snapshot(self, period: str, job_id: str | None, *, read_only: bool = False,
                                   continuity_workspace=None) -> dict[str, object] | None:
        from src.workflows import inference_ledger
        async with self._session() as db:
            await self._accounting_begin(db, read_only=read_only)
            account = await db.get(InferenceAccountingOwner, "deployment")
            if account is None:
                raise InferenceAccountingError(ACCOUNTING_LEDGER_UNINITIALIZED)
            with _continuity_lock(Path(settings.workspace_dir).resolve(), held_workspace=continuity_workspace,
                                  exclusive=not read_only) as workspace:
                try:
                    ledger = await (self._read_ledger(db, workspace, account) if read_only
                        else self._open_ledger(db, workspace, account))
                except Exception:
                    # Unserialized, a concurrent writer may have persisted the
                    # witness ahead of its commit; only the serialized read may
                    # conclude continuity is broken.
                    if read_only:
                        return None
                    raise
                try:
                    listed = await self._snapshot_operations(db, ledger, job_id)
                except InferenceAccountingError:
                    if read_only:
                        return None
                    raise
                if db.info.get(_RESEAL_PENDING_KEY):
                    # This serialized read resealed: its single, final write.
                    await self._persist_accounting_witness(db, workspace, account)
                ledger = db.info[inference_ledger._SESSION_KEY]
                totals = ledger.totals
                gaps = [entry for entry in json.loads(account.settings_history_json)
                    if isinstance(entry, dict) and entry.get("kind") == "history_gap"]
                return {"status": "ready", "reason_code": None,
                    # Display only (#1284): a resealed ledger may be missing
                    # history; nothing is blocked by it.
                    "accounting_continuity_verified": True, "history_verified": not gaps,
                    "history_gap_since": gaps[0].get("recorded_at") if gaps else None,
                    "revision": account.revision, "ledger_digest": account.ledger_digest,
                    "deployment_id": account.deployment_id, "period_id": period,
                    # Settled charges of this UTC month, from the sealed totals.
                    "spent_microusd": totals["settled_by_period"].get(period, 0),
                    # Unpriced (``unknown``) requests over all months; the UI
                    # labels it so (#1284).
                    "unpriced_requests": totals.get("open_rows_by_state", {}).get("unknown", 0),
                    "operation_count": totals["row_count"], "operations_truncated": totals["row_count"] > 128,
                    "operations": [_operator_operation(row) for row in listed],
                    "memory_status": "no_learning"}

    async def verify_inference_ledger_in_background(self) -> dict[str, object]:
        """Background full verification (#1086 R6).

        Reads a deferred snapshot with stdlib sqlite3 on a worker thread and
        checks only DB-internal consistency (never the external witness, which
        writers persist before commit). A failure is re-checked with the full
        in-process verification under the writer lock, which reseals the ledger
        and records a ``history_gap`` instead of blocking (#1284).
        """
        import asyncio
        from types import SimpleNamespace
        from src.workflows import inference_ledger
        from src.workspace import canonical_workspace_database_path
        root = Path(settings.workspace_dir).resolve()
        database = canonical_workspace_database_path(settings.workspace_dir)
        if not Path(database).is_file():
            return {"status": "not_initialized"}
        deployment_id, failure, revision = await asyncio.to_thread(inference_ledger.verify_offline, database)
        if deployment_id is None:
            return {"status": "not_initialized"}
        key = (SimpleNamespace(host_root=root), SimpleNamespace(deployment_id=deployment_id))
        if failure == inference_ledger.V1_LEDGER_SKIPPED:
            return {"status": "skipped_v1_ledger", "revision": revision}
        if failure is None:
            inference_ledger.record_full_verification(*key)
            return {"status": "verified", "revision": revision}
        try:
            async with self._session() as db:
                await self._accounting_begin(db)
                account, rows = await self._accounting_rows(db)
                with _continuity_lock(root) as workspace:
                    self._assert_accounting_continuity(workspace, account, rows, db=db)
                    if db.info.get(_RESEAL_PENDING_KEY):
                        await self._persist_accounting_witness(db, workspace, account, rows)
                        return {"status": "resealed", "reason": failure}
        except InferenceAccountingError as exc:
            if exc.code == "accounting_continuity_busy":
                return {"status": "deferred", "reason": failure}
            raise
        del key
        return {"status": "verified_under_lock", "revision": revision}

    async def _read_ledger(self, db: Any, workspace, account):
        """Read-only fast verification; anything needing more escalates."""
        from src.workflows import inference_ledger
        from src.workspace.accounting_witness import is_v2_digest
        digests, usable = await inference_ledger.scan_row_digests(db)
        if not usable or not is_v2_digest(account.ledger_digest) or not inference_ledger.totals_current(account):
            raise InferenceAccountingError("accounting_continuity_unavailable")
        receipt = read_lifecycle_receipt(workspace)
        expected = receipt.get("inference_accounting") if receipt is not None else None
        try:
            totals = inference_ledger.verify_fast(account, expected, digests)
        except inference_ledger.LedgerVerificationError as exc:
            raise InferenceAccountingError("accounting_continuity_unavailable") from exc
        await self._assert_group_keys_present(db)
        state = inference_ledger.LedgerState(digests=digests, totals=totals, full=False)
        db.info[inference_ledger._SESSION_KEY] = state
        return state

    async def _assert_group_keys_present(self, db: Any) -> None:
        """A missing or invalid private group key anywhere blocks (indexed, O(1)).

        Restore and checkpoint repair write NULL keys until startup's
        serialized migration classifies them; the full-list check reported
        the same code.
        """
        from sqlalchemy import or_
        # Only indexed columns are selected, so SQLite answers from the
        # (owner_id, group_lookup_key) index without touching table rows.
        blocked = (await db.execute(select(InferenceCostReservation.owner_id).where(or_(
            InferenceCostReservation.group_lookup_key.is_(None),
            InferenceCostReservation.group_lookup_key == "invalid")).limit(1))).first()
        if blocked is not None:
            raise InferenceAccountingError("general_task_group_lookup_invalid")

    async def _snapshot_operations(self, db: Any, ledger, job_id: str | None):
        """The first 128 operations in the operator order, each verified."""
        from sqlalchemy import case, literal_column
        open_first = case((InferenceCostReservation.state.in_(("reserved", "contact_started", "unknown")), 0), else_=1)
        query = select(InferenceCostReservation)
        if job_id is not None:
            query = query.where(InferenceCostReservation.job_id == job_id)
        rows = list((await db.execute(query.order_by(open_first, InferenceCostReservation.priority.desc(),
            InferenceCostReservation.sequence.asc(), literal_column("inference_cost_reservations.rowid")).limit(128))).scalars())
        from src.workflows import inference_ledger
        ledger = db.info.get(inference_ledger._SESSION_KEY, ledger)
        try:
            inference_ledger.verify_loaded_rows(ledger, rows)
        except inference_ledger.LedgerVerificationError as exc:
            if _LEDGER_CONTEXT_KEY not in db.info:
                # A deferred read escalates to the serialized writer read.
                raise InferenceAccountingError("accounting_continuity_unavailable") from exc
            ledger = await self._reseal_ledger(db, str(exc))
            inference_ledger.verify_loaded_rows(ledger, rows)
        return rows


def _current_policy_digest_for(row) -> str | None:
    """The current owner policy digest for a reservation's own provider (#1159).

    A NEAR TEE row compares against the NEAR TEE slot authority projection;
    any other row against the retained OpenRouter projection. ``None`` when
    that policy is not current (never equal to a stored digest).
    """
    from src.model_fabric.accounting import near_tee_profile_slot
    from src.model_fabric.effective_policy import current_inference_policy, current_near_tee_policy
    try:
        if (slot := near_tee_profile_slot(row.runtime_path, row.profile_id)) is not None:
            return current_near_tee_policy(slot)[1]
        return current_inference_policy()[1]
    except PermissionError:
        return None
