"""Repository-owned cost evidence; the remote broker alone executes providers.

The external lifecycle witness is written before transaction commit. Any crash
between those two writes deliberately blocks egress until reconciliation. This
is a continuity fence, not host-administrator tamper resistance.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
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
    return row.model_dump(mode="json")


def _provider_contact_denied(row: InferenceCostReservation) -> bool:
    """A committed denial retains its original, never-contacted allowance."""
    return (row.state == "reserved" and row.contact_started_at is None
        and row.recovery_reason == "provider_contact_denied"
        and any(item.get("kind") == "provider_contact_denied"
            for item in json.loads(row.evidence_json)))


def _operator_operation(row: InferenceCostReservation) -> dict[str, object]:
    payload = _operation_payload(row)
    payload["controls"] = ([{"action": "settle", "endpoint": "/api/settings/model-fabric/accounting/settle",
        "method": "POST", "expected_revision": row.revision, "operation_id": row.operation_id,
        "job_id": row.job_id, "authority_scope": "deployment_accounting"}]
        if row.state in {"unknown", "contact_started"} else [])
    return payload


def _ledger_digest(account: InferenceAccountingOwner, rows: list[InferenceCostReservation]) -> str:
    from src.workspace.accounting_witness import ledger_digest
    return ledger_digest(account.model_dump(mode="json"), [_operation_payload(row) for row in rows])


def _witness(account: InferenceAccountingOwner) -> dict[str, object]:
    return {"deployment_id": account.deployment_id, "revision": account.revision, "ledger_digest": account.ledger_digest}


@contextmanager
def _continuity_lock(root: Path, *, initialize: bool = False, held_workspace=None):
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

    async def _accounting_begin(self, db: Any) -> None:
        if db.get_bind().dialect.name != "sqlite":
            raise InferenceAccountingError("accounting_storage_unsupported")
        await db.execute(text("BEGIN IMMEDIATE"))

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
                self._assert_accounting_continuity(workspace, account, rows)
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
                return _operation_payload(row)

    async def recover_inference_accounting(self, *, now: datetime | None = None, job_id: str | None = None) -> list[dict[str, object]]:
        """Classify stale provider work without replaying ephemeral callbacks."""
        observed = _utc(now or datetime.now(timezone.utc))
        recovered = []
        async with self._session() as db:
            await self._accounting_begin(db)
            account, rows = await self._accounting_rows(db)
            if account is None:
                return []
            with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
                self._assert_accounting_continuity(workspace, account, rows)
                changed = False
                for row in rows:
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
                    if never_contacted and run.job_kind in {"calendar_meeting_prep", "mail_reply_draft"} and _utc(row.deadline_at) > observed:
                        from src.db.models import WorkBoardAttempt, WorkBoardTask, WorkBoardInputArtifact
                        from src.work_board.dispatcher import _parse_typed_input
                        from src.model_fabric.effective_policy import current_inference_policy
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
                            if current_inference_policy()[1] != row.policy_digest:
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
                        row.state = "unknown"
                        row.recovery_reason = "provider_cost_readback_required"
                        run.status = "cost_liability"
                    row.revision += 1
                    row.updated_at = observed.replace(tzinfo=None)
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
                if changed:
                    await self._persist_accounting_witness(db, workspace, account, rows)
        return recovered

    async def _accounting_resume_claim_allowed(self, db, run) -> bool:
        from src.model_fabric.effective_policy import current_inference_policy
        rows = list((await db.execute(select(InferenceCostReservation).where(InferenceCostReservation.job_id == run.run_identity))).scalars())
        return bool(run.job_kind in {"calendar_meeting_prep", "mail_reply_draft"} and len(rows) == 1
            and rows[0].state == "reserved" and rows[0].recovery_reason == "typed_owner_precontact_resume"
            and _utc(rows[0].deadline_at) > datetime.now(timezone.utc)
            and rows[0].policy_digest == current_inference_policy()[1])

    async def inference_precontact_resume_allowed(self, job_id: str) -> bool:
        async with self._session() as db:
            run = await self._fetch(db, job_id)
            return run.status == "queued" and await self._accounting_resume_claim_allowed(db, run)

    async def _accounting_rows(self, db: Any):
        account = await db.get(InferenceAccountingOwner, "deployment")
        rows = list((await db.execute(select(InferenceCostReservation))).scalars())
        return account, rows

    def _assert_accounting_continuity(self, workspace, account, rows):
        receipt = read_lifecycle_receipt(workspace)
        expected = receipt.get("inference_accounting") if receipt is not None else None
        if account is None or expected != _witness(account) or account.ledger_digest != _ledger_digest(account, rows):
            raise InferenceAccountingError("accounting_continuity_unavailable")

    async def _persist_accounting_witness(self, db, workspace, account, rows):
        base = _witness(account) if account.revision else None
        changed = [row for row in rows if inspect(row).modified or inspect(row).pending]
        account.revision += 1
        account.updated_at = datetime.now(timezone.utc)
        # Normalize SQLite datetimes before hashing so reopen has identical
        # bytes. All timestamps describe UTC regardless of storage timezone.
        account.updated_at = account.updated_at.replace(tzinfo=None)
        await db.flush()
        account.ledger_digest = _ledger_digest(account, rows)
        db.add(account)
        await db.flush()
        receipt = read_lifecycle_receipt(workspace) or {"secret_values_included": False}
        receipt["inference_accounting"] = _witness(account)
        write_accounting_checkpoint(workspace, {"schema_version": 1, "base": base,
            "account": account.model_dump(mode="json"), "operations": [_operation_payload(row) for row in changed],
            "witness": _witness(account), "secret_values_included": False})
        write_lifecycle_receipt(workspace, receipt, _accounting_lock_held=True)

    async def configure_inference_accounting(self, ceiling_microusd: int, *, reserve_review_microusd: int | None = None, continuity_workspace=None) -> dict[str, object]:
        """Explicit settings save bootstraps only a truly empty deployment."""
        ceiling = integer_amount(ceiling_microusd, positive=True)
        if reserve_review_microusd is not None:
            integer_amount(reserve_review_microusd, positive=True)
            if reserve_review_microusd > ceiling:
                raise InferenceAccountingError("accounting_amount_invalid")
        root = Path(settings.workspace_dir).resolve()
        async with self._session() as db:
            await self._accounting_begin(db)
            account, rows = await self._accounting_rows(db)
            with _continuity_lock(root, initialize=account is None, held_workspace=continuity_workspace) as workspace:
                if account is None:
                    prior = read_lifecycle_receipt(workspace)
                    if rows or (prior and "inference_accounting" in prior):
                        raise InferenceAccountingError("accounting_continuity_unavailable")
                    # Legacy contacted history is not proof of an empty ledger.
                    legacy = (await db.execute(select(WorkflowRunState.effect_receipts_json))).scalars()
                    if any("remote_inference" in str(effects or "") for effects in legacy):
                        raise InferenceAccountingError("accounting_legacy_reconciliation_required")
                    prior_route = (await db.execute(select(ModelRouteAttemptReceiptRecord.id).where(
                        (ModelRouteAttemptReceiptRecord.endpoint.like("https://openrouter.ai/%")
                         | ModelRouteAttemptReceiptRecord.endpoint.like("https://cloud-api.near.ai/%"))).limit(1))).first()
                    if prior_route is not None:
                        raise InferenceAccountingError("accounting_legacy_reconciliation_required")
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    account = InferenceAccountingOwner(ceiling_microusd=ceiling, created_at=now, updated_at=now, revision=0)
                    account.settings_history_json = _json([{"kind": "period_initialized", "period_id": period_id(now), "recorded_at": now.isoformat()}])
                    db.add(account)
                    await db.flush()
                else:
                    self._assert_accounting_continuity(workspace, account, rows)
                    if account.ceiling_microusd == ceiling and reserve_review_microusd is None:
                        return _witness(account)
                    account.settings_revision += 1
                    account.ceiling_microusd = ceiling
                history = json.loads(account.settings_history_json)
                history.append({"revision": account.settings_revision, "ceiling_microusd": ceiling, "recorded_at": datetime.now(timezone.utc).isoformat()})
                if reserve_review_microusd is not None:
                    from src.workspace.accounting_witness import unreviewed_overruns
                    overruns = unreviewed_overruns(account.model_dump(mode="json"), [_operation_payload(row) for row in rows])
                    covered = [row for row in overruns if row["actual_cost_microusd"] <= reserve_review_microusd][:128]
                    history.append({"kind": "request_reserve_review", "revision": account.settings_revision,
                        "accounting_revision": account.revision, "bound_microusd": reserve_review_microusd,
                        "operations": [{key: row[key] for key in ("operation_id", "sequence", "revision")} for row in covered],
                        "recorded_at": datetime.now(timezone.utc).isoformat()})
                account.settings_history_json = _json(history)
                await self._persist_accounting_witness(db, workspace, account, rows)
                return _witness(account)

    async def reserve_inference_cost(self, *, operation_id: str, job_id: str, owner_id: str,
                                     payload_digest: str, policy_digest: str, runtime_path: str,
                                     profile_id: str, bound_microusd: int, owner_ceiling_microusd: int | None,
                                     priority: int, deadline_at: float, owner: str, fencing_token: int,
                                     now: datetime | None = None) -> dict[str, object]:
        from src.workflows.research_accounting import discovery_accounting_scope
        async with discovery_accounting_scope(self, job_id=job_id):
            bound = integer_amount(bound_microusd, positive=True)
            if owner_ceiling_microusd is not None:
                integer_amount(owner_ceiling_microusd, positive=True)
            if not re.fullmatch(r"[0-9a-f]{64}", payload_digest or "") or not re.fullmatch(r"[0-9a-f]{64}", policy_digest or ""):
                raise InferenceAccountingError("accounting_operation_binding_invalid")
            observed = _utc(now or datetime.now(timezone.utc))
            deadline = datetime.fromtimestamp(deadline_at, timezone.utc)
            async with self._session() as db:
                await self._accounting_begin(db)
                account, rows = await self._accounting_rows(db)
                with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
                    self._assert_accounting_continuity(workspace, account, rows)
                    run = await self._fetch(db, job_id)
                    self._assert_lease(run, owner=owner, fencing_token=fencing_token)
                    if run.status != "running" or run.owner_principal_id != owner_id or deadline <= observed:
                        raise InferenceAccountingError("accounting_job_authority_invalid")
                    prior = next((row for row in rows if row.operation_id == operation_id), None)
                    from src.workflows.research_accounting import discovery_generation_budget
                    programme_budget = await discovery_generation_budget(self, db, run, rows,
                        new_bound=bound if prior is None else 0, operation_id=operation_id, payload_digest=payload_digest)
                    if runtime_path == "near_text_native":
                        if profile_id != "near.text" or run.job_kind != "inference.near-text.v1":
                            raise InferenceAccountingError("near_native_binding_invalid")
                        if any(row.operation_id != operation_id and row.owner_id == owner_id
                            and row.runtime_path == "near_text_native"
                            and row.state in {"reserved", "contact_started", "unknown"} for row in rows):
                            raise InferenceAccountingError("near_owner_outstanding_limit")
                    if prior is not None:
                        if (prior.state == "reserved" and prior.recovery_reason == "typed_owner_precontact_resume"
                            and prior.job_id == job_id and prior.owner_id == owner_id and prior.payload_digest == payload_digest
                            and prior.policy_digest == policy_digest and prior.runtime_path == runtime_path and prior.profile_id == profile_id
                            and prior.bound_microusd == bound and _utc(prior.deadline_at) > observed):
                            prior.job_fencing_token = fencing_token
                            prior.recovery_reason = None
                            prior.revision += 1
                            prior.updated_at = observed.replace(tzinfo=None)
                            db.add(prior)
                            await self._persist_accounting_witness(db, workspace, account, rows)
                            return _operation_payload(prior)
                        raise InferenceAccountingError("accounting_operation_already_reserved")
                    period = period_id(observed)
                    from src.workspace.accounting_witness import period_state, unreviewed_overruns
                    owner_data, operations = account.model_dump(mode="json"), [_operation_payload(item) for item in rows]
                    period_status = period_state(owner_data, operations, period)
                    if period_status["reason_code"]:
                        raise InferenceAccountingError(period_status["reason_code"])
                    if unreviewed_overruns(owner_data, operations):
                        raise InferenceAccountingError("provider_charge_exceeded_reservation")
                    def held(row):
                        if row.state in {"reserved", "contact_started", "unknown"}:
                            return row.bound_microusd
                        return (row.actual_cost_microusd or 0) if row.period_id >= period and row.state == "settled" else 0
                    if sum(held(row) for row in rows) + bound > account.ceiling_microusd:
                        raise InferenceAccountingError("deployment_cost_budget_exhausted")
                    if owner_ceiling_microusd is not None and sum(held(row) for row in rows if row.owner_id == owner_id) + bound > owner_ceiling_microusd:
                        raise InferenceAccountingError("owner_cost_budget_exhausted")
                    row = InferenceCostReservation(
                        operation_id=operation_id, deployment_id=account.deployment_id,
                        job_id=job_id, owner_id=owner_id, goal_id=run.goal_id, goal_revision=run.goal_revision,
                        payload_digest=payload_digest, policy_digest=policy_digest, runtime_path=runtime_path,
                        profile_id=profile_id, period_id=period, settings_revision=account.settings_revision,
                        ceiling_microusd=account.ceiling_microusd, bound_microusd=bound,
                        owner_ceiling_microusd=owner_ceiling_microusd, sequence=account.revision + 1,
                        priority=priority, deadline_at=deadline.replace(tzinfo=None),
                        job_fencing_token=fencing_token, created_at=observed.replace(tzinfo=None), updated_at=observed.replace(tzinfo=None),
                        evidence_json=_json([{"kind": "reservation", "bound_microusd": bound, "memory_status": "no_learning"}]),
                    )
                    if programme_budget is not None:
                        evidence = json.loads(row.evidence_json)
                        evidence.append({"kind": "goal_programme_binding", **programme_budget})
                        row.evidence_json = _json(evidence)
                    db.add(row)
                    rows.append(row)
                    await self._persist_accounting_witness(db, workspace, account, rows)
                    return _operation_payload(row)

    async def contact_inference_provider(self, operation_id: str, *, owner: str,
                                         fencing_token: int, policy_digest: str,
                                         near_contact_witness: object = None) -> dict[str, object]:
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
                account, rows = await self._accounting_rows(db)
                pending = next((item for item in rows if item.operation_id == operation_id), None)
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
                    self._assert_accounting_continuity(workspace, account, rows)
                    row = next((item for item in rows if item.operation_id == operation_id), None)
                    if row is None or row.state != "reserved" or row.policy_digest != policy_digest:
                        raise InferenceAccountingError("accounting_contact_fence_invalid")
                    run = await self._fetch(db, row.job_id)
                    self._assert_lease(run, owner=owner, fencing_token=fencing_token)
                    if run.job_kind == "readonly_research_child":
                        from src.workflows.research_guard import assert_research_parent_current
                        await assert_research_parent_current(db, run)
                    from src.workflows.job_runtime import _assert_canonical_goal_fence
                    await _assert_canonical_goal_fence(db, goal_id=run.goal_id, goal_revision=run.goal_revision,
                        owner_kind=run.owner_kind, owner_principal_id=run.owner_principal_id,
                        session_id=run.session_id, authority=run.declared_authority_json)
                    from src.workflows.research_accounting import discovery_generation_budget
                    await discovery_generation_budget(self, db, run, rows, operation_id=operation_id, payload_digest=row.payload_digest)
                    if run.owner_kind == "user":
                        from src.auth.service import authenticate_principal
                        await authenticate_principal(row.owner_id, db=db)
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    if run.status != "running" or row.job_fencing_token != fencing_token or _utc(row.deadline_at) <= _utc(now):
                        raise InferenceAccountingError("accounting_contact_fence_invalid")
                    opportunity_denial = None
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
                    from src.model_fabric.configuration import deployment_spend_ceiling
                    from src.workspace.accounting_witness import period_state, unreviewed_overruns
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
                    try:
                        if row.runtime_path == "near_text_native":
                            from src.model_fabric.effective_policy import current_near_text_policy
                            configured, current_digest = current_near_text_policy()
                        else:
                            configured, current_digest = current_inference_policy()
                    except PermissionError:
                        configured, current_digest = None, None
                    owner_data = account.model_dump(mode="json")
                    operations = [_operation_payload(item) for item in rows]
                    period = period_id(_utc(now))
                    period_status = period_state(owner_data, operations, period)
                    def held(item):
                        return item.bound_microusd if item.state in {"reserved", "contact_started", "unknown"} else (
                            (item.actual_cost_microusd or 0) if item.state == "settled" and item.period_id >= period else 0)
                    denial = (opportunity_denial or ("provider_contact_denied" if _provider_contact_denied(row)
                        else "provider_policy_revision_changed" if current_digest != policy_digest
                        else "accounting_settings_revision_unavailable" if (
                            account.ceiling_microusd != deployment_spend_ceiling(configured)
                            or row.settings_revision != account.settings_revision)
                        else period_status["reason_code"]
                        or ("provider_charge_exceeded_reservation" if unreviewed_overruns(owner_data, operations) else None)
                        or ("deployment_cost_budget_exhausted" if sum(held(item) for item in rows) > account.ceiling_microusd else None)
                        or ("owner_cost_budget_exhausted" if row.owner_ceiling_microusd is not None
                            and sum(held(item) for item in rows if item.owner_id == row.owner_id) > row.owner_ceiling_microusd else None)))
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
                        row.updated_at = now
                        row.revision += 1
                        row.evidence_json = _json(history)
                        db.add(row)
                        await self._persist_accounting_witness(db, workspace, account, rows)
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
            account, rows = await self._accounting_rows(db)
            with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
                self._assert_accounting_continuity(workspace, account, rows)
                row = next((item for item in rows if item.operation_id == operation_id), None)
                if row is None:
                    raise InferenceAccountingError("accounting_operation_not_found")
                near = row.runtime_path == "near_text_native"
                billing = None
                if near_billing_evidence is not None:
                    from src.model_fabric.near_text_billing import validate_near_billing_evidence
                    try:
                        billing = validate_near_billing_evidence(near_billing_evidence, original_operation_id=operation_id)
                    except (ValueError, TypeError) as exc:
                        raise InferenceAccountingError("near_billing_evidence_invalid") from exc
                    if not near or row.profile_id != "near.text" or actual_cost_microusd is not None or operator_id is not None or payload is not None:
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
                    row.recovery_reason = "provider_cost_readback_required" if actual is None else "provider_charge_exceeded_reservation" if actual > row.bound_microusd else None
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                history = json.loads(row.evidence_json)
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
                await self._persist_accounting_witness(db, workspace, account, rows)
                return _operation_payload(row)

    async def inference_accounting_snapshot(self, *, now: datetime | None = None, job_id: str | None = None, continuity_workspace=None) -> dict[str, object]:
        period = period_id(now or datetime.now(timezone.utc))
        try:
            async with self._session() as db:
                await self._accounting_begin(db)
                account, rows = await self._accounting_rows(db)
                with _continuity_lock(Path(settings.workspace_dir).resolve(), held_workspace=continuity_workspace) as workspace:
                    self._assert_accounting_continuity(workspace, account, rows)
                    from src.workspace.accounting_witness import period_state, unreviewed_overruns
                    owner_data, operations = account.model_dump(mode="json"), [_operation_payload(row) for row in rows]
                    period_status = period_state(owner_data, operations, period)
                    if period > period_status["period_high_water"]:
                        history = json.loads(account.settings_history_json)
                        history.append({"kind": "period_observed", "period_id": period, "recorded_at": datetime.now(timezone.utc).isoformat()})
                        account.settings_history_json = _json(history)
                        await self._persist_accounting_witness(db, workspace, account, rows)
                        owner_data = account.model_dump(mode="json")
                        period_status = period_state(owner_data, operations, period)
                    overruns = unreviewed_overruns(owner_data, operations)
                    reason = period_status["reason_code"] or ("provider_charge_exceeded_reservation" if overruns else None)
                    committed = sum(row.actual_cost_microusd or 0 for row in rows if row.state == "settled" and row.period_id >= period)
                    reserved = sum(row.bound_microusd for row in rows if row.state == "reserved")
                    unknown = sum(row.bound_microusd for row in rows if row.state in {"contact_started", "unknown"})
                    current_review = next((entry for entry in reversed(json.loads(account.settings_history_json))
                        if entry.get("kind") == "request_reserve_review"
                        and entry.get("revision") == account.settings_revision), None)
                    return {**period_status, "status": "blocked" if reason else "ready", "reason_code": reason,
                        "accounting_continuity_verified": True, "revision": account.revision,
                        "ledger_digest": account.ledger_digest,
                        "request_reserve_review": {"settings_revision": current_review["revision"],
                            "accounting_revision": current_review["accounting_revision"],
                            "bound_microusd": current_review["bound_microusd"]} if current_review else None,
                        "period_review": {"endpoint": "/api/settings/model-fabric/accounting/period", "method": "POST",
                            "period_id": period, "expected_revision": account.revision,
                            "authority_scope": "deployment_accounting"} if period_status["reason_code"] else None,
                        "deployment_id": account.deployment_id,
                        "period_id": period, "settings_revision": account.settings_revision,
                        "ceiling_microusd": account.ceiling_microusd, "committed_microusd": committed,
                        "reserved_microusd": reserved, "unknown_microusd": unknown,
                        "remaining_microusd": max(0, account.ceiling_microusd - committed - reserved - unknown),
                        "overrun_max_cost_microusd": max((row["actual_cost_microusd"] for row in overruns), default=0),
                        "operation_count": len(rows), "operations_truncated": len(rows) > 128,
                        "operations": [_operator_operation(row) for row in sorted((item for item in rows if job_id is None or item.job_id == job_id), key=lambda item: (item.state not in {"reserved", "contact_started", "unknown"}, -item.priority, item.sequence))[:128]],
                        "memory_status": "no_learning"}
        except Exception as exc:
            return {"status": "blocked", "reason_code": getattr(exc, "code", "accounting_continuity_unavailable"),
                "remaining_microusd": None, "period_id": period, "operations": [], "memory_status": "no_learning"}
